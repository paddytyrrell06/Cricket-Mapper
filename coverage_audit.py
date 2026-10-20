"""Offline coverage and near-miss diagnostics; never collect quotes or send orders.

Run python3 coverage_audit.py after a normal saved-snapshot publication.
Counts use explicit units: source records, fixture links, outcome rows, routes.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import fees as F
import presentation as P
from liquidity import LiquidityPolicy

VENUES = ("kalshi", "polymarket_intl")


def read_csv(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def observed_routes(row, policy):
    """Independent of matching/fees: depth of each distinct, healthy BBO route."""
    result = []
    for sell, buy in (VENUES, VENUES[::-1]):
        invalid = any(P._price(row.get(v + "_bid")) is not None
                      and P._price(row.get(v + "_ask")) is not None
                      and P._price(row[v + "_bid"]) > P._price(row[v + "_ask"])
                      for v in (sell, buy))
        depth = policy.assess_route(row.get(sell + "_bid"), row.get(buy + "_ask"),
                                    row.get(sell + "_bid_size"), row.get(buy + "_ask_size"))
        result.append((sell, buy, depth, invalid))
    return result


def audit_rows(rows, *, as_of, max_age_seconds=300, max_skew_seconds=30):
    policy = LiquidityPolicy()  # Same $100 bound and 100-contract size as terminal.
    counts = Counter(dict.fromkeys(("outcome_rows", "contract_accepted_rows", "fresh_comparable_rows",
        "both_directions_evaluated_rows", "possible_directional_routes", "fresh_comparable_routes",
        "known_sized_fee_routes", "depth_bound_routes", "known_fees_and_depth_bound_routes",
        "known_fees_depth_and_minimum_routes", "positive_before_bounds_routes", "positive_after_filters_routes"), 0))
    exclusions, directions = Counter(), Counter()
    depth_counts = Counter(dict.fromkeys(("measured", "missing_size", "unpriced_or_invalid_book",
                                         "no_estimate_rows", "no_estimate_but_measured"), 0))
    routes, closest, positive = [], [], []
    for index, original in enumerate(rows):
        row = {**original, **P._eligible_quotes(original, list(VENUES), as_of,
                max_age_seconds=max_age_seconds, max_skew_seconds=max_skew_seconds)}
        accepted = row.get("mapping_status") == "accepted" and row.get("comparison_status") == "compatible"
        counts["outcome_rows"] += 1
        counts["contract_accepted_rows"] += accepted
        fresh = json.loads(row["eligible_routes"])
        counts["fresh_comparable_rows"] += bool(fresh)
        counts["both_directions_evaluated_rows"] += len(fresh) == 2
        depth = observed_routes(row, policy)
        measured = any(not invalid and value.paired_quantity is not None for _, _, value, invalid in depth)
        priced = any(not invalid and P._price(row.get(sell + "_bid")) is not None
                     and P._price(row.get(buy + "_ask")) is not None for sell, buy, _, invalid in depth)
        depth_counts["measured" if measured else "missing_size" if priced else "unpriced_or_invalid_book"] += 1
        if not fresh:
            depth_counts["no_estimate_rows"] += 1
            depth_counts["no_estimate_but_measured"] += measured
        best, positive_valid = None, False
        for sell, buy, capacity, invalid in depth:
            entry = {"row": index, "sport": row.get("sport", "cricket"),
                     "fixture": row.get("team_a", "") + " vs " + row.get("team_b", ""),
                     "outcome": row["canonical_outcome"], "buy": buy, "sell": sell,
                     "buy_ask": row.get(buy + "_ask", ""), "sell_bid": row.get(sell + "_bid", ""),
                     "buy_size": row.get(buy + "_ask_size", ""), "sell_size": row.get(sell + "_bid_size", ""),
                     "paired_top_quantity": F._text(capacity.paired_quantity),
                     "paired_top_notional_usd": F._text(capacity.limiting_notional_usd),
                     "conditional": row.get("contingency_status") != "aligned",
                     "execution_validated": False, "quote_check": row["quote_check"]}
            counts["possible_directional_routes"] += 1
            if not accepted:
                reason = "contract:" + row.get("comparison_status", "unknown")
            elif {"buy": buy, "sell": sell} not in fresh:
                reason = "quote:" + ("invalid_book" if invalid else "unpriced"
                    if P._price(entry["sell_bid"]) is None or P._price(entry["buy_ask"]) is None
                    else row["quote_check"])
            else:
                counts["fresh_comparable_routes"] += 1
                directions["buy_" + buy + "_sell_" + sell] += 1
                estimate = F.calculate_sized_taker_cross(row, buy, sell, policy=policy)
                entry.update(estimate)
                known = estimate.get("net_gap") is not None
                sufficient = estimate["liquidity_status"] == "sufficient"
                minimum_ok = estimate["minimum_order_status"] != "below_minimum"
                counts["known_sized_fee_routes"] += known
                counts["depth_bound_routes"] += sufficient
                counts["known_fees_and_depth_bound_routes"] += known and sufficient
                counts["known_fees_depth_and_minimum_routes"] += known and sufficient and minimum_ok
                if estimate.get("sized_quantity") is None or Decimal(estimate["sized_quantity"]) <= 0:
                    reason = "size:" + estimate["reason"]
                elif not known:
                    reason = "fees:" + estimate["reason"]
                elif estimate["liquidity_status"] != "sufficient":
                    reason = "liquidity:below_bound"
                elif estimate["minimum_order_status"] == "below_minimum":
                    reason = "minimum:below_minimum"
                elif Decimal(estimate["net_gap"]) <= 0:
                    reason = "net:not_positive"
                else:
                    reason = "positive_estimate_execution_unverified"
                    counts["positive_after_filters_routes"] += 1
                    positive_valid = True
                if known:
                    q = Decimal(estimate["sized_quantity"])
                    entry["gross_cents"] = F._text(Decimal(estimate["gross_gap"]) * 100)
                    entry["fees_cents"] = F._text(Decimal(estimate["total_fees"]) / q * 100)
                    entry["net_cents"] = F._text(Decimal(estimate["net_gap"]) * 100)
                    if Decimal(estimate["net_gap"]) > 0:
                        counts["positive_before_bounds_routes"] += 1
                        positive.append(entry)
                    elif (estimate["liquidity_status"] == "sufficient"
                          and estimate["minimum_order_status"] != "below_minimum"
                          and (best is None or Decimal(estimate["net_gap"]) > Decimal(best["net_gap"]))):
                        best = entry
            entry["exclusion"] = reason
            exclusions[reason] += 1
            routes.append(entry)
        if best and not positive_valid:
            closest.append(best)
    closest.sort(key=lambda entry: (-Decimal(entry["net_gap"]), -Decimal(entry["paired_top_notional_usd"]), entry["row"]))
    return {"counts": dict(counts), "direction_counts": dict(directions),
            "route_exclusions": dict(exclusions), "depth": dict(depth_counts),
            "policy": policy.to_dict(), "near_miss_criteria": "Closest nonpositive net per contract, one best route per outcome row; fresh comparable quotes, known sized fees, $100 paired top bound and provided order minimums retained.",
            "near_misses": closest[:20], "positive_estimates": positive, "routes": routes}


def audit_snapshot(data_dir):
    data_dir = Path(data_dir)
    metadata = json.loads((data_dir / "snapshot_meta.json").read_text())
    stamp = metadata.get("quotes_checked_at_utc")
    as_of = P._timestamp(stamp)
    if as_of is None:
        raise ValueError("Saved quote-check timestamp is required; do not freshen old quotes")
    # Existing metadata truncates check time to seconds, while quote times have
    # milliseconds. Use the end of that recorded second, never wall-clock now.
    if "." not in stamp.split("+")[0]:
        as_of = as_of.replace(microsecond=999999)
    rows = read_csv(data_dir / "market_map.csv")
    if not rows:
        raise ValueError("No saved market_map.csv rows")
    result = audit_rows(rows, as_of=as_of)
    runtime = audit_rows(rows, as_of=as_of, max_age_seconds=30, max_skew_seconds=5)
    result["runtime_policy_at_snapshot"] = {key: runtime[key] for key in ("counts", "route_exclusions")}
    runtime_routes = {(row["row"], row["buy"], row["sell"]): row for row in runtime["routes"]}
    for row in result["routes"]:
        live = runtime_routes[row["row"], row["buy"], row["sell"]]
        row.update(runtime_quote_check=live["quote_check"], runtime_exclusion=live["exclusion"])
    result["near_misses"] = runtime["near_misses"]
    result["near_miss_criteria"] = "Runtime limits (30s age, 5s cross-venue skew). " + runtime["near_miss_criteria"]
    review = read_csv(data_dir / "review_queue.csv")
    coverage = read_csv(data_dir / "coverage_status.csv")
    result["source_coverage"] = [{key: row.get(key) for key in
        ("sport", "venue", "collection_complete", "parse_complete", "records")}
        for row in coverage]
    parse_warnings = sorted({reason.strip() for row in coverage for reason in row.get("reason", "").split(";")
                             if reason.strip().startswith("COVERAGE_INCOMPLETE")})
    result["parser_exclusions"] = dict(Counter(
        "unverified_team_identity" if "identities" in warning else
        "missing_scheduled_start" if "no scheduled" in warning or "missing scheduled" in warning else
        "other" for warning in parse_warnings))
    result["parser_exclusion_examples"] = parse_warnings[:3]
    result["coverage_attribution_anomalies"] = [row["sport"] + ":" + row["venue"] for row in coverage
        if row.get("parse_complete") == "False" and row.get("reason")
        and not any(reason.strip().partition(": ")[0].endswith(" " + row["venue"])
                    for reason in row["reason"].split(";"))]
    result["review_reasons"] = {kind: dict(Counter(row["match_reason"] for row in review
        if row.get("record_kind") == kind)) for kind in ("fixture", "market")}
    mappings = read_csv(data_dir / "market_mappings.csv")
    result["counts"].update(
        fixture_links_accepted=len({row["canonical_fixture_id"] for row in mappings}),
        fixture_links_review=sum(row.get("record_kind") == "fixture" for row in review),
        market_links_review=sum(row.get("record_kind") == "market" for row in review),
        unmatched_fixtures=len(read_csv(data_dir / "unmatched_fixtures.csv")),
        unmatched_markets=len(read_csv(data_dir / "unmatched_markets.csv")),
        collected_source_records=sum(int(row.get("records") or 0) for row in coverage))
    run_report = data_dir / "run_report.txt"
    text = run_report.read_text() if run_report.exists() else ""
    normalized = re.findall(r"(\d+) records, (\d+) fixtures, (\d+) markets", text)
    result["normalized_counts"] = {"fixtures": sum(int(f) for _, f, _ in normalized),
                                   "markets": sum(int(m) for _, _, m in normalized)} if normalized else None
    result["quote_warnings"] = [line.strip() for line in text.splitlines() if line.strip().startswith("QUOTES ")]
    result["quote_sources"] = {venue: dict(Counter(row.get(venue + "_quote_source", "").split(" token_id=")[0]
                                                   for row in rows))
                               for venue in VENUES}
    result["quote_statuses"] = {venue: dict(Counter(row.get(venue + "_quote_status", "unknown") for row in rows))
                                for venue in VENUES}
    audit_path = data_dir / "audit_report.txt"
    accepted = re.search(r"Accepted: (\d+) fixture-pair links, (\d+) market-pair links",
                         audit_path.read_text() if audit_path.exists() else "")
    if accepted:
        result["counts"]["fixture_links_accepted"] = int(accepted[1])
    result["as_of"] = as_of.isoformat()
    result["discovery_as_of"] = metadata.get("collected_at_utc")
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["note"] = "Historical saved snapshot, not live opportunities. No API requests. Snapshot freshness uses the end of the recorded check second; report limits 300s/30s, runtime limits 30s/5s. Review reasons and route exclusions use different units and are not additive. Only configured scopes and returned records can be audited."
    return result


def write_reports(result, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    summary = {key: value for key, value in result.items() if key != "routes"}
    (destination / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    fields = ["row", "sport", "fixture", "outcome", "buy", "sell", "buy_ask", "sell_bid",
              "buy_size", "sell_size", "sized_quantity", "gross_cents", "fees_cents", "net_cents",
              "buy_fee", "sell_fee", "net_total", "paired_top_quantity", "paired_top_notional_usd",
              "minimum_order_status", "quote_check", "conditional", "exclusion", "execution_validated",
              "runtime_quote_check", "runtime_exclusion"]
    for filename, entries in (("near_misses.csv", result["near_misses"]), ("routes.csv", result["routes"])):
        with (destination / filename).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(entries)
    lines = ["# Saved coverage and opportunity audit", "", result["note"],
             "", "Quote observation checkpoint: " + result["as_of"], "", "## Pipeline counts", "",
             "| Stage / unit | Count |", "| --- | ---: |"]
    lines += [f"| {key} | {value} |" for key, value in result["counts"].items()]
    lines += ["", "Normalized source objects: " + str(result["normalized_counts"]),
              "", "Collection responses complete: " + str(sum(row.get("collection_complete") == "True"
                   for row in result["source_coverage"])) + "/" + str(len(result["source_coverage"])),
              "Parser exclusions (unique saved warning records): " + str(result["parser_exclusions"]),
              "Coverage-flag attribution anomalies (other venue's parser warning): " + str(result["coverage_attribution_anomalies"]),
              "", "## Depth classification", ""]
    lines += [f"- {key}: {value}" for key, value in result["depth"].items()]
    lines += ["", "No estimate is not evidence of missing depth. Review, skew, stale quotes, absent prices and missing quantities are distinct.",
              "", "## Directional exclusions (first failing gate)", ""]
    lines += [f"- {key}: {value}" for key, value in sorted(result["route_exclusions"].items())]
    lines += ["", "Both inventory trading directions evaluated: " + str(result["direction_counts"]),
              "Runtime 30s age / 5s skew at the SAME saved checkpoint: " + str(result["runtime_policy_at_snapshot"]["counts"]),
              "", "## Matching review causes (fixture links)", ""]
    lines += [f"- {reason}: {count}" for reason, count in
              sorted(result["review_reasons"]["fixture"].items(), key=lambda item: -item[1])[:8]]
    lines += ["", "## Quote/API warnings", "", "Saved quote sources: " + str(result["quote_sources"]), ""] + ["- " + warning for warning in result["quote_warnings"]]
    lines += ["", "## 20 closest near-misses", "", result["near_miss_criteria"],
              "All fee and edge columns below are cents per contract; PnL and liquidity are dollars. Conditional settlement caveats and unknown execution constraints remain in CSV/JSON.",
              "", "| Market / outcome | Buy → sell | Qty | Gross ¢ | Fees ¢ | Net ¢ | Net $ | Paired top $ |",
              "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    def fmt(value):
        return format(Decimal(str(value)), ".4f").rstrip("0").rstrip(".") if value is not None else "?"
    for entry in result["near_misses"]:
        name = (entry["fixture"] + " / " + entry["outcome"]).replace("|", "\\|").replace("\n", " ")
        values = [name, entry["buy"] + " → " + entry["sell"]] + [fmt(entry.get(key)) for key in
            ("sized_quantity", "gross_cents", "fees_cents", "net_cents", "net_total", "paired_top_notional_usd")]
        lines.append("| " + " | ".join(values) + " |")
    lines += ["", "Positive estimates (including blocked): " + str(len(result["positive_estimates"])),
              "", *["- " + entry["fixture"] + " / " + entry["outcome"] + ": net $" + fmt(entry["net_total"])
                       + ", paired top $" + fmt(entry["paired_top_notional_usd"]) + ", " + entry["exclusion"]
                       + ", " + entry["minimum_order_status"] for entry in result["positive_estimates"]],
              "", "Prices use native YES/outcome-token books; inventory-sale routes are not a proof that a short or a separate NO-token order can be executed. NO-token/basket coverage is not inferred from an opposing team's YES when a draw, tie or cancellation payout can differ. Fees and whole-contract sizing reuse fees.py and liquidity.py. Nothing is sent to venues."]
    (destination / "report.md").write_text("\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("output/data"))
    parser.add_argument("--output-dir", type=Path, default=Path("output/coverage_audit"))
    args = parser.parse_args(argv)
    result = audit_snapshot(args.data_dir)
    write_reports(result, args.output_dir)
    print(json.dumps({"as_of": result["as_of"], "counts": result["counts"],
                      "depth": result["depth"], "report": str(args.output_dir / "report.md")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
