# Build a self-contained terminal and CSV from matched markets and rule reviews.
from __future__ import annotations

import csv
import html
import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import settlement as S
import fees as F

VENUE_LABELS = {
    "kalshi": "Kalshi",
    "polymarket_intl": "Polymarket",
    "polymarket_us": "Polymarket US",
}
QUOTE_MAX_AGE_SECONDS = 300
QUOTE_MAX_SKEW_SECONDS = 30
BASE_COLS = [
    "sport", "canonical_fixture_id", "fixture_date", "start_time_utc", "competition",
    "gender", "format", "team_a", "team_b", "canonical_market_type", "canonical_outcome",
    "venues_available", "venues_checked", "mapping_status", "comparison_status", "comparison_reason",
    "comparison_basis", "contingency_status", "contingency_caveats", "strict_comparison_status",
    "strict_comparison_reason", "cross_condition",
    "best_bid", "best_ask", "best_bid_venue", "best_ask_venue", "best_spread",
    "observed_crossed", "observed_cross_kind", "observed_cross_gap",
    "cross_eligible", "quote_check", "crossed", "cross_kind", "cross_gap",
    "eligible_bid", "eligible_ask", "eligible_bid_venue", "eligible_ask_venue",
    "net_crossed", "net_cross_eligible", "net_gap", "net_total", "net_quantity",
    "net_buy_venue", "net_sell_venue", "net_buy_fee", "net_sell_fee", "net_total_fees",
    "net_fee_status", "net_fee_reason", "net_top_size", "net_top_quantity", "net_depth_confirmed",
    "net_top_total", "net_top_gap", "net_gross_gap",
]
VENUE_FIELDS = [
    "event_id", "market_id", "slug", "title", "url", "bid", "ask", "bid_size", "ask_size", "fee_schedule", "fee_metadata", "quote_time",
    "quote_status", "quote_source", "crossed", "settlement_profile", "settlement_status",
    "settlement_payouts", "settlement_policies", "settlement_evidence", "settlement_missing",
    "settlement_label", "quote_details", "alternatives",
]


def _price(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return price if price.is_finite() and Decimal(0) <= price <= Decimal(1) else None


def _price_text(value: Optional[Decimal]) -> str:
    return format(value.normalize(), "f") if value is not None else ""


def _size(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        size = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return size if size.is_finite() and size >= 0 else None


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _sport(fixture: Any) -> str:
    value = str(getattr(getattr(fixture, "key", None), "sport", "cricket") or "cricket").lower()
    return {"nba": "basketball", "nfl": "football", "nhl": "hockey", "mlb": "baseball"}.get(value, value)


def _profile(market: Any) -> dict:
    profile = (getattr(market, "extra", {}) or {}).get("settlement")
    return profile if isinstance(profile, dict) else {}


def _market_dict(market: Any, venue: str = "") -> dict[str, str]:
    bid, ask = _price(getattr(market, "bid", None)), _price(getattr(market, "ask", None))
    if bid is not None and ask is not None:
        quote_status = "available"
    elif bid is not None or ask is not None:
        quote_status = "one-sided"
    else:
        quote_status = "empty" if getattr(market, "quote_status", "") == "empty" else "unavailable"
    profile = _profile(market)
    extra = getattr(market, "extra", {}) or {}
    return {
        "event_id": str(market.event_id or ""), "market_id": str(market.market_id or ""),
        "slug": str(market.slug or ""), "title": str(market.title or ""), "url": str(market.url or ""),
        "bid": _price_text(bid), "ask": _price_text(ask),
        "bid_size": _price_text(_size(extra.get("quote_bid_size"))) if bid is not None else "",
        "ask_size": _price_text(_size(extra.get("quote_ask_size"))) if ask is not None else "",
        "fee_schedule": _json(F.compile_schedule(venue or getattr(market, "venue", ""), extra).to_dict()),
        "fee_metadata": _json({key: value for key, value in extra.items()
                                if "fee" in key.lower() or key == "_kalshi_series"}),
        "quote_time": str(getattr(market, "quote_time", "") or ""),
        "quote_status": quote_status, "quote_source": str(getattr(market, "quote_source", "") or ""),
        "crossed": str(bid is not None and ask is not None and bid > ask).lower(),
        "settlement_profile": str(profile.get("profile_id") or ""),
        "settlement_status": str(profile.get("status") or "unknown"),
        "settlement_payouts": _json(profile.get("payouts") or {}),
        "settlement_policies": _json(profile.get("policies") or {}),
        "settlement_evidence": _json(profile.get("evidence") or []),
        "settlement_missing": _json(profile.get("missing") or []),
        "settlement_label": str(profile.get("label") or "Settlement requires review"),
        "quote_details": _json({key: value for key, value in extra.items()
                                if key != "settlement" and any(term in key.lower()
                                                               for term in ("quote", "depth", "book", "price"))}),
        "alternatives": "",
    }


def _quote_summary(row: dict[str, str], venues: list[str]) -> dict[str, str]:
    bids = [(venue, price) for venue in venues if (price := _price(row.get(f"{venue}_bid"))) is not None]
    asks = [(venue, price) for venue in venues if (price := _price(row.get(f"{venue}_ask"))) is not None]
    bid_venue, best_bid = max(bids, key=lambda item: item[1]) if bids else ("", None)
    ask_venue, best_ask = min(asks, key=lambda item: item[1]) if asks else ("", None)
    local = any(row.get(f"{venue}_crossed") == "true" for venue in venues)
    across = any(bid > ask for bv, bid in bids for av, ask in asks if bv != av)
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    return {
        "best_bid": _price_text(best_bid), "best_ask": _price_text(best_ask),
        "best_bid_venue": bid_venue, "best_ask_venue": ask_venue, "best_spread": _price_text(spread),
        "observed_crossed": str(local or across).lower(),
        "observed_cross_kind": " and ".join(name for name, active in
                                             (("within venue", local), ("across venues", across)) if active),
        "observed_cross_gap": _price_text(-spread if (local or across) and spread is not None else None),
    }


def _timestamp(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _eligible_quotes(row: dict[str, str], venues: list[str], as_of: datetime) -> dict[str, str]:
    pairs = []
    invalid_books = {venue for venue in venues
                     if (bid := _price(row.get(f"{venue}_bid"))) is not None
                     and (ask := _price(row.get(f"{venue}_ask"))) is not None and bid > ask}
    problems = ["invalid_book"] if invalid_books else []
    for bid_venue in venues:
        bid = _price(row.get(f"{bid_venue}_bid"))
        if bid is None or bid_venue in invalid_books:
            continue
        for ask_venue in venues:
            ask = _price(row.get(f"{ask_venue}_ask"))
            if ask_venue == bid_venue or ask is None or ask_venue in invalid_books:
                continue
            times = [_timestamp(row.get(f"{venue}_quote_time", "")) for venue in (bid_venue, ask_venue)]
            if any(stamp is None for stamp in times) or as_of.tzinfo is None:
                problems.append("unknown")
                continue
            ages = [(as_of - stamp).total_seconds() for stamp in times]
            if min(ages) < 0:
                problems.append("future")
            elif max(ages) > QUOTE_MAX_AGE_SECONDS:
                problems.append("stale")
            elif abs((times[0] - times[1]).total_seconds()) > QUOTE_MAX_SKEW_SECONDS:
                problems.append("skewed")
            else:
                pairs.append((bid - ask, bid_venue, ask_venue, bid, ask))
    check = "fresh" if pairs else next((name for name in ("invalid_book", "future", "stale", "skewed", "unknown")
                                        if name in problems), "unpriced")
    compatible = row.get("comparison_status") == "compatible" and row.get("mapping_status") == "accepted"
    eligible = compatible and bool(pairs)
    best = max(pairs, key=lambda pair: pair[0]) if pairs and eligible else None
    crossed = best is not None and best[0] > 0
    result = {
        "cross_eligible": str(eligible).lower(), "quote_check": check,
        "crossed": str(crossed).lower(), "cross_kind": "across venues" if crossed else "",
        "cross_gap": _price_text(best[0] if crossed else None),
        "eligible_bid": _price_text(best[3]) if best else "",
        "eligible_ask": _price_text(best[4]) if best else "",
        "eligible_bid_venue": best[1] if best else "", "eligible_ask_venue": best[2] if best else "",
        "cross_condition": ("conditional" if row.get("contingency_status") != "aligned" else "aligned") if crossed else "",
    }
    result.update(_net_quotes(row, pairs if eligible else []))
    return result


def _net_quotes(row: dict[str, str], pairs: list[tuple]) -> dict[str, str]:
    """Price every fresh route after both taker fees; preserve gross diagnostics."""
    result = {field: "" for field in BASE_COLS if field.startswith("net_")}
    result.update(net_crossed="false", net_cross_eligible="false", net_quantity="1",
                  net_depth_confirmed="false", net_fee_status="ineligible",
                  net_fee_reason="A fresh, comparable pair of quotes is required")
    if not pairs:
        return result
    estimates = [(F.calculate_taker_cross(row, ask_venue, bid_venue), bid_venue, ask_venue)
                 for _, bid_venue, ask_venue, _, _ in pairs]
    known = [item for item in estimates if item[0].get("status") == "estimated"
             and item[0].get("net_gap") is not None]
    estimate, sell_venue, buy_venue = (max(known, key=lambda item: Decimal(item[0]["net_gap"]))
                                       if known else max(estimates, key=lambda item: Decimal(item[0].get("gross_gap") or "-1")))
    result.update(net_fee_status=str(estimate.get("status", "unavailable")),
                  net_fee_reason=str(estimate.get("reason", "")), net_buy_venue=buy_venue, net_sell_venue=sell_venue,
                  net_cross_eligible=str(bool(known)).lower(),
                  net_crossed=str(bool(known) and estimate.get("net_positive") is True).lower(),
                  net_depth_confirmed=str(estimate.get("depth_confirmed") is True).lower())
    for target, source in (("net_gap", "net_gap"), ("net_total", "net_total"), ("net_quantity", "quantity"),
                           ("net_buy_fee", "buy_fee"), ("net_sell_fee", "sell_fee"), ("net_total_fees", "total_fees"),
                           ("net_top_size", "top_size"), ("net_top_quantity", "top_quantity"),
                           ("net_top_total", "top_net_total"), ("net_top_gap", "top_net_gap"), ("net_gross_gap", "gross_gap")):
        value = estimate.get(source)
        result[target] = _price_text(Decimal(str(value))) if value is not None else ""
    return result


def _comparison(markets: dict[str, Any], mapping_status: str, reasons: list[str], fixture: Any,
                strict_settlement: bool = False) -> dict[str, str]:
    profiles = [_profile(market) for market in markets.values()]
    comparisons = [S.compare_profiles(left, right, strict=strict_settlement) for i, left in enumerate(profiles)
                   for right in profiles[i + 1:]]
    strict_results = [S.compare_profiles(left, right, strict=True) for i, left in enumerate(profiles)
                      for right in profiles[i + 1:]]
    if any(result["status"] == "incompatible" for result in comparisons):
        status = "rules_differ"
    elif not comparisons or any(result["status"] != "compatible" for result in comparisons):
        status = "rules_unknown"
    elif mapping_status != "accepted":
        status = "fixture_review" if getattr(fixture, "status", "accepted") != "accepted" else "market_review"
    else:
        status = "compatible"
    details = [result.get("reason", "") for result in comparisons if result["status"] != "compatible"]
    if mapping_status != "accepted":
        details.extend(reasons)
    contingency = ("different" if any(result.get("contingency_status") == "different" for result in comparisons)
                   else "unknown" if not comparisons or any(result.get("contingency_status") != "aligned" for result in comparisons)
                   else "aligned")
    strict_status = ("rules_differ" if any(result["status"] == "incompatible" for result in strict_results)
                     else "rules_unknown" if not strict_results or any(result["status"] != "compatible" for result in strict_results)
                     else "compatible")
    caveats = list(dict.fromkeys(str(caveat) for result in comparisons for caveat in result.get("caveats", [])))
    return {
        "comparison_status": status,
        "comparison_reason": "; ".join(dict.fromkeys(reason for reason in details if reason)) or "Normal-result payout terms agree",
        "comparison_basis": "full_contract" if strict_settlement else "normal_completion",
        "contingency_status": contingency, "contingency_caveats": _json(caveats),
        "strict_comparison_status": strict_status,
        "strict_comparison_reason": "; ".join(dict.fromkeys(result.get("reason", "") for result in strict_results if result.get("reason"))),
    }


def build_wide_rows(accepted: list[Any], review: list[Any], coverage_complete: dict[str, bool],
                    venues: list[str], fixture_review: Optional[list[Any]] = None,
                    include_review: bool = False, as_of: Optional[datetime] = None,
                    sport_coverage: Optional[dict[str, dict[str, bool]]] = None,
                    strict_settlement: bool = False) -> list[dict[str, str]]:
    as_of = as_of or datetime.now(timezone.utc)
    rows: dict[tuple, dict[str, Any]] = {}
    review_venues: dict[tuple, set[str]] = defaultdict(set)
    fixture_review_venues: dict[tuple[str, str], set[str]] = defaultdict(set)
    anchors: dict[tuple[str, str, str], set[tuple[str, str]]] = defaultdict(set)
    for match in accepted:
        sport = _sport(match.fixture)
        for venue, market in ((match.venue_1, match.m1), (match.venue_2, match.m2)):
            anchors[(sport, venue, market.venue_fixture_id)].add((sport, match.fixture.canonical_fixture_id))
    for match in fixture_review or []:
        sport = _sport(match)
        keys = {(sport, match.canonical_fixture_id)}
        for venue, fixture in match.by_venue.items():
            keys.update(anchors.get((sport, venue, fixture.venue_fixture_id), set()))
        for key in keys:
            fixture_review_venues[key].update(match.by_venue)
    for match in review:
        key = (_sport(match.fixture), match.fixture.canonical_fixture_id, match.market_type, match.outcome)
        review_venues[key].update((match.venue_1, match.venue_2))
    for mapping_status, matches in (("accepted", accepted), ("review", review if include_review else [])):
        for match in matches:
            key = (_sport(match.fixture), match.fixture.canonical_fixture_id, match.market_type, match.outcome)
            record = rows.setdefault(key, {"fixture": match.fixture, "market_type": match.market_type,
                                          "outcome": match.outcome, "markets": {}, "sources": {},
                                          "mapping_status": mapping_status, "reasons": [], "alternatives": defaultdict(list)})
            record["reasons"].append(str(getattr(match, "reason", "") or ""))
            for venue, market in ((match.venue_1, match.m1), (match.venue_2, match.m2)):
                current = _market_dict(market, venue)
                previous = record["markets"].get(venue)
                if previous and previous != current:
                    if mapping_status == "accepted":
                        raise ValueError(f"conflicting accepted markets for {key} on {venue}")
                    if record["mapping_status"] == "accepted":
                        continue
                    record["alternatives"][venue].append(current)
                    continue
                record["markets"][venue] = current
                record["sources"][venue] = market
    output = []
    for key, record in rows.items():
        fixture, markets = record["fixture"], record["markets"]
        row_coverage = (sport_coverage or {}).get(key[0], coverage_complete)
        comparison = _comparison(record["sources"], record["mapping_status"], record["reasons"], fixture, strict_settlement)
        row = {
            "sport": key[0], "canonical_fixture_id": fixture.canonical_fixture_id,
            "fixture_date": fixture.key.date, "start_time_utc": fixture.start_time,
            "competition": fixture.competition, "gender": fixture.key.gender, "format": fixture.key.fmt,
            "team_a": fixture.team_a, "team_b": fixture.team_b,
            "canonical_market_type": record["market_type"], "canonical_outcome": record["outcome"],
            "venues_available": str(len(markets)),
            "venues_checked": str(sum(bool(row_coverage.get(v)) for v in venues)),
            "mapping_status": record["mapping_status"], **comparison,
        }
        for venue in venues:
            market = dict(markets[venue]) if venue in markets else None
            alternatives = record["alternatives"].get(venue)
            if alternatives and market:
                market["alternatives"] = _json([dict(market), *alternatives])
                market.update(bid="", ask="", bid_size="", ask_size="", quote_status="unavailable", crossed="false")
                row["comparison_status"] = "market_review"
                row["comparison_reason"] += "; multiple market candidates on " + venue
            if market:
                status = "Available"
            elif venue in review_venues.get(key, set()) or venue in fixture_review_venues.get(key[:2], set()):
                status = "Needs review"
            else:
                status = "Not currently open" if row_coverage.get(venue, False) else "Coverage incomplete"
            row[f"{venue}_status"] = status
            for field in VENUE_FIELDS:
                row[f"{venue}_{field}"] = market.get(field, "") if market else ""
        row.update(_quote_summary(row, venues))
        row.update(_eligible_quotes(row, venues, as_of))
        output.append(row)
    return sorted(output, key=lambda row: (row["sport"], row["fixture_date"], row["start_time_utc"],
                                          row["team_a"], row["team_b"], row["canonical_market_type"], row["canonical_outcome"]))


def _columns(venues: list[str]) -> list[str]:
    return BASE_COLS + [f"{venue}_{field}" for venue in venues for field in ["status", *VENUE_FIELDS]]


def write_wide_csv(path: Path, rows: list[dict[str, str]], venues: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_columns(venues))
        writer.writeheader()
        writer.writerows(rows)


def _cents(value: str) -> str:
    if not value:
        return "—"
    try:
        price = Decimal(value)
        return format(price * 100, ".2f").rstrip("0").rstrip(".") + "¢" if price.is_finite() else "—"
    except (InvalidOperation, ValueError):
        return "—"


def _venue_cell(row: dict[str, str], venue: str) -> str:
    if row.get(f"{venue}_status") != "Available":
        return f'<span class="muted">{html.escape(row.get(f"{venue}_status", "Unavailable"))}</span>'
    def quote(side: str) -> str:
        size = _size(row.get(f"{venue}_{side}_size"))
        text = _price_text(size) if size is not None else "?"
        return (f'<div class="quote-side"><strong class="{side}" data-side="{side}">{_cents(row.get(f"{venue}_{side}", ""))}</strong>'
                f'<small class="quote-size" title="Top {side} depth in contracts; ? means unavailable">×{html.escape(text)}</small></div>')
    return f'<div class="quotes">{quote("bid")}<span class="quote-divider">/</span>{quote("ask")}</div>'


def _rule_hint(row: dict[str, str]) -> str:
    profiles = []
    for venue in VENUE_LABELS:
        if row.get(f"{venue}_status") != "Available":
            continue
        try:
            payouts = json.loads(row.get(f"{venue}_settlement_payouts", "{}"))
            policies = json.loads(row.get(f"{venue}_settlement_policies", "{}"))
        except (ValueError, TypeError):
            continue
        profiles.append((payouts, policies))
    for state, label in (("draw", "Draw"), ("tie_without_tiebreak", "Tie"), ("win", "Win")):
        values = [payouts.get(state) for payouts, _ in profiles if payouts.get(state) is not None]
        if len({str(value) for value in values}) > 1:
            return label + ": " + " / ".join(_cents(str(value)) if _price(value) is not None else str(value) for value in values)
    for policy in ("period", "overtime", "tiebreakers", "dls"):
        if len({str(policies.get(policy)) for _, policies in profiles if policies.get(policy) is not None}) > 1:
            return policy.replace("_", " ").capitalize() + " differs"
    return "Core payout terms differ"


def _edge_cell(row: dict[str, str], index: int) -> str:
    if row.get("crossed") == "true":
        condition = ' <span class="conditional" title="Conditional: contingency terms differ or remain unspecified">C</span>' if row.get("cross_condition") == "conditional" else ""
        return f'<span class="edge-positive">{_cents(row.get("cross_gap", ""))}{condition}</span><span class="expired-label" hidden>Stale</span>'
    if row.get("comparison_status") != "compatible":
        hint = _rule_hint(row) if row.get("comparison_status") == "rules_differ" else "Comparison requires review"
        return f'<span class="muted" title="{html.escape(hint, quote=True)}">—</span>'
    if row.get("quote_check") != "fresh":
        labels = {"invalid_book": "Invalid book", "stale": "Stale", "future": "Time conflict", "skewed": "Out of sync", "unknown": "No time", "unpriced": "—"}
        return f'<span class="muted">{labels.get(row.get("quote_check"), "—")}</span>'
    return '<span class="muted">—</span>'


def _net_edge_cell(row: dict[str, str], index: int) -> str:
    if row.get("net_fee_status") == "estimated":
        positive = row.get("net_crossed") == "true"
        cls = "edge-positive" if positive else "edge-negative"
        caveat = ' <span class="conditional">C</span>' if positive and row.get("contingency_status") != "aligned" else ""
        net = (f'<span class="{cls}" title="After both taker fees; 1 contract per leg">'
               f'{_cents(row.get("net_gap", ""))}{caveat}</span>')
    elif row.get("cross_eligible") == "true":
        net = f'<span class="muted" title="{html.escape(row.get("net_fee_reason", "Taker fees unavailable"), quote=True)}">Fee ?</span>'
    else:
        return _edge_cell(row, index)
    gross = _cents(row.get("net_gross_gap", ""))
    return (f'<span class="net-estimate">{net}<small class="gross-edge" title="Gross spread before fees">G {gross}</small></span>'
            '<span class="expired-label" hidden>Stale</span>')


def _rules_cell(row: dict[str, str]) -> str:
    status = row.get("comparison_status", "rules_unknown")
    if status == "compatible":
        caveat = row.get("contingency_status", "unknown") != "aligned"
        return ('<span class="rule-ok">Core ✓</span>' +
                (' <span class="conditional" title="Contingency terms differ or remain unspecified; inspect details">C</span>' if caveat else ""))
    labels = {"rules_differ": "Rules differ", "rules_unknown": "Rules unknown", "fixture_review": "Fixture review", "market_review": "Market review"}
    return f'<span class="rule-review" title="{html.escape(row.get("comparison_reason", ""), quote=True)}">{labels.get(status, "Review")}</span>'


def _json_details(raw: str) -> str:
    try:
        return html.escape(json.dumps(json.loads(raw), indent=2, ensure_ascii=False))
    except (ValueError, TypeError):
        return html.escape(raw or "Not available")


def _net_details(row: dict[str, str]) -> str:
    title = '<section class="fee-estimate"><h3>Taker edge · 1 contract per leg</h3>'
    if row.get("net_fee_status") != "estimated":
        return title + f'<p class="detail-note">{html.escape(row.get("net_fee_reason", "Fee schedule unavailable; net edge cannot be estimated."))}</p></section>'
    buy_venue, sell_venue = row.get("net_buy_venue", ""), row.get("net_sell_venue", "")
    entries = [
        ("Buy ask", f'{VENUE_LABELS.get(buy_venue, buy_venue)} · {_cents(row.get(f"{buy_venue}_ask", ""))}'),
        ("Sell bid", f'{VENUE_LABELS.get(sell_venue, sell_venue)} · {_cents(row.get(f"{sell_venue}_bid", ""))}'),
        ("Gross", _cents(row.get("net_gross_gap", ""))),
        ("Buy taker fee", _cents(row.get("net_buy_fee", ""))),
        ("Sell taker fee", _cents(row.get("net_sell_fee", ""))),
        ("Total fees", _cents(row.get("net_total_fees", ""))),
        ("Net / contract", _cents(row.get("net_gap", ""))),
    ]
    depth = _size(row.get("net_top_size"))
    if depth is not None:
        entries += [("Top capacity", f'{_price_text(depth)} contracts across both legs'),
                    ("Whole quantity", row.get("net_top_quantity", "") or "0")]
        if row.get("net_top_total"):
            entries.append(("Net at capacity", _cents(row["net_top_total"])))
    else:
        entries.append(("Top capacity", "Unavailable on one or both legs"))
    return (title + '<dl class="fee-breakdown">' + "".join(f'<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd>' for label, value in entries) + '</dl>'
            '<p class="detail-note">Net = sell bid − buy ask − both taker fees. Fees use venue rounding at the stated quantity. '
            'The 1 contract estimate may be below venue minimum order sizes. Top capacity uses the smaller displayed side; prices and depth do not guarantee fills. '
            'Selling requires inventory or a supported complementary position.</p></section>')


def _details(row: dict[str, str], venues: list[str]) -> str:
    basis = "Full contract" if row.get("comparison_basis") == "full_contract" else "Normal completion"
    sections = [f'<div class="detail-basis">{basis} comparison</div><p class="detail-reason">{html.escape(row.get("comparison_reason", ""))}</p>']
    try:
        caveats = json.loads(row.get("contingency_caveats", "[]"))
    except (ValueError, TypeError):
        caveats = []
    if caveats:
        sections.append('<details class="caveats"><summary>Contingency caveats <span class="conditional">C</span></summary><ul>' +
                        "".join(f'<li>{html.escape(str(caveat))}</li>' for caveat in caveats) + '</ul></details>')
    sections.append(_net_details(row))
    sections.append(f'<p class="detail-note">Cross eligibility requires quotes at most {QUOTE_MAX_AGE_SECONDS // 60} minutes old and '
                    f'{QUOTE_MAX_SKEW_SECONDS} seconds apart. Net edges estimate both taker fees at 1 contract per leg. '
                    'Conditional comparisons assume normal completion; cancellation and other contingency terms can differ.</p>')
    for venue in venues:
        if row.get(f"{venue}_status") != "Available":
            continue
        url = row.get(f"{venue}_url", "")
        link = f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">Open event ↗</a>' if url else ""
        try:
            payouts = json.loads(row.get(f"{venue}_settlement_payouts", "{}"))
        except (ValueError, TypeError):
            payouts = {}
        core_rows = "".join(f'<tr><td>{state.replace("_", " ").title()}</td><td>{_cents(str(payouts[state])) if _price(payouts[state]) is not None else html.escape(str(payouts[state]))}</td></tr>'
                            for state in ("win", "loss", "draw", "tie_without_tiebreak") if payouts.get(state) is not None)
        # Quote prices and settlement payouts describe different things. Only
        # normalized row quotes may populate bid/ask; a missing side stays absent.
        bid = _price_text(_price(row.get(f"{venue}_bid")))
        ask = _price_text(_price(row.get(f"{venue}_ask")))
        quote_status = row.get(f"{venue}_quote_status", "unavailable") or "unavailable"
        sections.append(f'<section class="detail-venue"><h3>{html.escape(VENUE_LABELS[venue])}</h3>{link}'
                        f'<p>{html.escape(row.get(f"{venue}_title", ""))}</p>'
                        f'<div class="detail-quotes" data-venue="{html.escape(venue, quote=True)}" aria-label="Observed bid and ask">'
                        f'<div><span>Bid</span><strong class="bid" data-side="bid">{_cents(bid)}</strong>'
                        f'<small class="detail-depth">×{html.escape(row.get(f"{venue}_bid_size", "") or "?")} ct</small></div>'
                        f'<div><span>Ask</span><strong class="ask" data-side="ask">{_cents(ask)}</strong>'
                        f'<small class="detail-depth">×{html.escape(row.get(f"{venue}_ask_size", "") or "?")} ct</small></div>'
                        f'<span class="quote-status">{html.escape(quote_status)}</span></div>'
                        f'<dl><dt>Outcome ID</dt><dd>{html.escape(row.get(f"{venue}_market_id", ""))}</dd>'
                        f'<dt>Observed</dt><dd>{html.escape(row.get(f"{venue}_quote_time", "") or "Unknown")}</dd>'
                        f'<dt>Source</dt><dd>{html.escape(row.get(f"{venue}_quote_source", "") or "Unavailable")}</dd></dl>'
                        '<details><summary>Settlement payouts at resolution</summary>'
                        f'<p class="detail-note">{html.escape(row.get(f"{venue}_settlement_label", ""))}</p>'
                        '<table class="payout-table"><caption>Payout per $1 contract at resolution</caption>'
                        f'<thead><tr><th scope="col">Result</th><th scope="col">Payout</th></tr></thead><tbody>{core_rows}</tbody></table></details>'
                        '<details><summary>Source rules and settlement evidence</summary>'
                        f'<pre>{_json_details(row.get(f"{venue}_settlement_evidence", ""))}</pre></details>'
                        '<details><summary>Full payout model and contingency policies</summary>'
                        f'<pre>{_json_details(row.get(f"{venue}_settlement_payouts", ""))}</pre>'
                        f'<pre>{_json_details(row.get(f"{venue}_settlement_policies", ""))}</pre>'
                        f'<pre>{_json_details(row.get(f"{venue}_settlement_missing", ""))}</pre></details>'
                        '<details><summary>Quote and depth diagnostics</summary>'
                        f'<pre>{_json_details(row.get(f"{venue}_quote_details", ""))}</pre>'
                        f'<pre>{_json_details(row.get(f"{venue}_alternatives", ""))}</pre></details>'
                        '<details><summary>Taker fee schedule and source</summary>'
                        f'<pre>{_json_details(row.get(f"{venue}_fee_schedule", ""))}</pre></details></section>')
    return "".join(sections)


def _report_refresh_script(version: str, version_filename: str) -> str:
    # Classic script loads work for file:// reports, unlike fetch() of local files.
    # Keep the marker separate from quote data: only a completed report publication
    # should reload the snapshot. run.py publishes the complete directory together.
    filename = json.dumps(version_filename, ensure_ascii=True).replace("<", "\\u003c").replace("&", "\\u0026")
    return f"const reportVersion={json.dumps(version)},reportVersionFile={filename};\n" + r'''
const viewKey='market-terminal-view:'+location.protocol+'//'+location.host+location.pathname;
function saveView(){
    try{sessionStorage.setItem(viewKey,JSON.stringify({tab:activeTab,search:search.value,
        reviews:reviewToggle.checked,strict:strictToggle.checked,sort:sortBy.value,
        direction,pageSize:pageSize.value,page}));}catch(error){}
}
function restoreView(){
    let saved={};
    try{const value=JSON.parse(sessionStorage.getItem(viewKey));if(value&&typeof value==='object')saved=value;}catch(error){}
    if(typeof saved.search==='string')search.value=saved.search;
    if(typeof saved.reviews==='boolean')reviewToggle.checked=saved.reviews;
    if(typeof saved.strict==='boolean')strictToggle.checked=saved.strict;
    if(['date','name','edge'].includes(saved.sort))sortBy.value=saved.sort;
    if(['25','50','100'].includes(saved.pageSize))pageSize.value=saved.pageSize;
    direction=saved.direction===-1?-1:1;
    document.getElementById('sortDirection').textContent=direction===1?'↑':'↓';
    selectTab(tabs.find(tab=>tab.dataset.tab===saved.tab)||tabs[0]);
    if(Number.isSafeInteger(saved.page)&&saved.page>=0){page=saved.page;apply();}
}
window.addEventListener('pagehide',saveView);
restoreView();
let pendingReportCheck=null,reloadingReport=false,reportCheckNumber=0;
window.marketTerminalReportVersion=function(version){
    if(typeof version==='string'&&version!==reportVersion&&!reloadingReport){
        reloadingReport=true;saveView();
        if(typeof window.marketTerminalBeforeReportReload==='function')window.marketTerminalBeforeReportReload();
        location.reload();
    }
};
function checkForNewReport(){
    if(pendingReportCheck||reloadingReport)return;
    const script=document.createElement('script');
    const url=new URL(encodeURIComponent(reportVersionFile),location.href);
    url.searchParams.set('check',Date.now()+'-'+(++reportCheckNumber));
    script.src=url.href;script.async=true;pendingReportCheck=script;
    const cleanup=()=>{clearTimeout(timeout);script.remove();if(pendingReportCheck===script)pendingReportCheck=null;};
    const timeout=setTimeout(cleanup,5000);
    script.onload=script.onerror=cleanup;
    document.head.appendChild(script);
}
// A missing marker during the directory swap leaves this snapshot visible and
// is retried. No venue prices are polled by the HTML report.
setInterval(checkForNewReport,2000);
'''


def _terminal_service_script() -> str:
    """Connect a served report to the local refresh worker without file privileges."""
    return r'''
(() => {
    if(!['http:','https:'].includes(location.protocol))return;
    const button=document.getElementById('refreshPrices'),label=document.getElementById('refreshStatus');
    const skipKey='market-terminal-publication-reload:'+location.pathname;
    let canSave=false,skipRefresh=false,detected=false,polling=false,requesting=false,first=true;
    try{skipRefresh=sessionStorage.getItem(skipKey)==='1';sessionStorage.removeItem(skipKey);
        sessionStorage.setItem(skipKey,'probe');sessionStorage.removeItem(skipKey);canSave=true;}catch(error){}
    window.marketTerminalBeforeReportReload=()=>{try{sessionStorage.setItem(skipKey,'1');}catch(error){}};
    function show(data){
        detected=true;button.hidden=false;label.hidden=false;button.disabled=data.running||requesting;
        label.textContent=data.running?'Refreshing…':data.state==='error'?'Refresh failed':'HTTP · reload refreshes prices';
        label.title=data.error||'Local quote refresh service';
        if(data.running)saveView();
    }
    async function refreshPrices(){
        if(requesting)return;requesting=true;button.disabled=true;label.textContent='Refreshing…';saveView();
        try{
            const response=await fetch('/api/refresh',{method:'POST',headers:{'Content-Type':'application/json','X-Terminal-Request':'refresh'},body:'{}',cache:'no-store'});
            if(!response.ok)throw Error('Refresh request failed ('+response.status+')');
            const data=await response.json();requesting=false;show(data);
        }catch(error){requesting=false;button.disabled=false;label.textContent='Refresh failed';label.title=String(error);}
    }
    async function poll(){
        if(polling)return;polling=true;
        try{
            const response=await fetch('/api/status',{cache:'no-store'});
            if(!response.ok)return;
            const data=await response.json();
            if(!['idle','running','error'].includes(data.state)||typeof data.revision!=='number')return;
            show(data);
            if(first){first=false;const navigation=performance.getEntriesByType('navigation')[0];
                if(canSave&&!skipRefresh&&navigation&&navigation.type==='reload')refreshPrices();}
            if(!data.running)checkForNewReport();
        }catch(error){if(detected){label.textContent='Service offline';label.title=String(error);button.disabled=true;}}
        finally{polling=false;}
    }
    button.addEventListener('click',refreshPrices);poll();setInterval(()=>{if(detected)poll();},1000);
})();
'''


def write_html(path: Path, rows: list[dict[str, str]], venues: list[str], coverage_complete: dict[str, bool],
               generated_at: datetime, quality: Optional[dict[str, str]] = None) -> None:
    quality = quality or {}
    replay = str(quality.get("Run type", "")).upper() == "REPLAY"
    unknown = str(quality.get("Source collection", "")).lower() == "unknown"
    title = "Market Terminal · Snapshot Replay" if replay else "Market Terminal"
    badge = "SNAPSHOT REPLAY" if replay else "LIVE API REPORT"
    timestamp_label = "Source collection time unavailable" if unknown else "Snapshot collected" if replay else "Live collection completed"
    timestamp_value = "" if unknown else generated_at.strftime("%d %b %Y, %H:%M:%S UTC")
    freshness_time = datetime.now(timezone.utc) if replay else generated_at
    age_label = "Replay generated" if replay else "Generated"
    collection_note = "" if unknown else "Source discovery completed " + generated_at.strftime("%d %b %Y, %H:%M:%S UTC")
    checked_at = None if replay else _timestamp(quality.get("Quotes checked at", ""))
    if checked_at is not None:
        timestamp_label, age_label = "Quotes checked", "Quotes checked"
        timestamp_value = checked_at.strftime("%d %b %Y, %H:%M:%S UTC")
        freshness_time = checked_at
    source_note = ("Venue availability reflects the saved collection. Replay generation does not refresh source quotes."
                   if replay else "Point-in-time observations · run python3 run.py to refresh")
    sport_labels = {"cricket": "Cricket", "soccer": "Football", "basketball": "Basketball",
                    "football": "Am. football", "hockey": "Hockey", "baseball": "Baseball",
                    "tennis": "Tennis", "mma": "MMA"}
    configured = quality.get("Sports", "")
    configured = {value.strip() for value in configured.split(",")} if isinstance(configured, str) and configured else set(sport_labels)
    present = {row.get("sport", "cricket") for row in rows}
    sports = [sport for sport in sport_labels if sport in configured or sport in present]
    sports += sorted(present - set(sports))
    labels = {sport: sport_labels.get(sport, sport.replace("_", " ").title()) for sport in sports}
    records = []
    for index, row in enumerate(rows):
        sport = row.get("sport", "cricket")
        fixture_name = f'{row["team_a"]} vs {row["team_b"]}'
        name = f'{fixture_name} {row["canonical_outcome"]}'
        date = f'{row["fixture_date"]} {row["start_time_utc"]}'
        quoted_cells = "".join(f'<td class="venue-cell" data-venue="{html.escape(venue, quote=True)}" '
                               f'data-market-id="{html.escape(row.get(f"{venue}_market_id", ""), quote=True)}">'
                               f'{_venue_cell(row, venue)}</td>' for venue in venues)
        quote_venues = {row.get("eligible_bid_venue", ""), row.get("eligible_ask_venue", ""),
                        row.get("net_buy_venue", ""), row.get("net_sell_venue", "")} - {""}
        pair_times = [_timestamp(row.get(f"{venue}_quote_time", "")) for venue in quote_venues]
        expiry = int((min(pair_times).timestamp() + QUOTE_MAX_AGE_SECONDS) * 1000) if pair_times and all(pair_times) and not replay else 0
        reviewed = row.get("comparison_status") != "compatible"
        row_html = (f'<tr data-id="{index}" data-sport="{html.escape(sport, quote=True)}" data-crossed="{row.get("crossed", "false")}" data-net-crossed="{row.get("net_crossed", "false")}" data-review="{str(reviewed).lower()}" data-cross-expiry="{expiry}">'
                    f'<td class="league-cell">{html.escape(labels.get(sport, sport))}</td>'
                    f'<td class="competition-cell" title="{html.escape(row.get("competition", ""), quote=True)}">{html.escape(row.get("competition", "") or "—")}</td>'
                    f'<td class="fixture-cell" title="{html.escape(fixture_name, quote=True)}">{html.escape(fixture_name)}</td>'
                    f'<td class="date-cell" title="{html.escape(date + " UTC", quote=True)}">{html.escape(row["fixture_date"][5:])} {html.escape(row["start_time_utc"] or "—")}</td>'
                    f'<td class="outcome-cell" title="{html.escape(row["canonical_outcome"], quote=True)}">{html.escape(row["canonical_outcome"])}</td>'
                    f'{quoted_cells}<td class="edge-cell">{_net_edge_cell(row, index)}</td><td class="rules-cell">{_rules_cell(row)}</td>'
                    f'<td class="detail-cell"><button class="details-button" data-details="{index}" aria-haspopup="dialog" aria-label="Details for {html.escape(name, quote=True)}">↗</button></td></tr>')
        searchable = " ".join([sport, row.get("competition", ""), name, date] + [row.get(f"{venue}_market_id", "") for venue in venues]).lower()
        records.append({"id": index, "sport": sport, "name": name.lower(), "date": date, "search": searchable,
                        "crossed": row.get("crossed") == "true", "netCrossed": row.get("net_crossed") == "true", "review": reviewed,
                        "strict": row.get("strict_comparison_status") == "compatible" and not reviewed,
                        "edge": row.get("net_gap") or "", "expiry": expiry, "stale": False,
                        "html": row_html, "details": f'<h2>{html.escape(fixture_name)}</h2><p class="detail-subtitle">{html.escape(row["canonical_outcome"])} · {html.escape(date)} UTC</p>{_details(row, venues)}'})
    crosses = sum(record["crossed"] for record in records)
    net_crosses = sum(record["netCrossed"] for record in records)
    matched = sum(row.get("mapping_status", "accepted") == "accepted" for row in rows)
    reviews = sum(record["review"] for record in records)
    tabs = [('all', 'All'), ('net', 'Net +'), ('crosses', 'Gross crosses'), *[(sport, labels[sport]) for sport in sports]]
    tabs_html = "".join(f'<button class="tab{" active" if key == "all" else ""}" id="tab-{html.escape(key, quote=True)}" role="tab" aria-selected="{str(key == "all").lower()}" aria-controls="marketPanel" data-tab="{html.escape(key, quote=True)}">{html.escape(label)}'
                        + (f'<span id="crossCount">{crosses}</span>' if key == "crosses" else f'<span id="netCrossCount">{net_crosses}</span>' if key == "net" else '') + '</button>' for key, label in tabs)
    coverage = " · ".join(f'{VENUE_LABELS[venue]} {"complete" if coverage_complete.get(venue) else "incomplete"}' for venue in venues)
    headers = "".join(f'<th scope="col" class="price-heading">{html.escape(VENUE_LABELS[venue])}<span>bid / ask ×ct</span></th>' for venue in venues)
    payload = json.dumps(records, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace("&", "\\u0026")
    initial = "".join(record["html"] for record in records[:100])
    version = uuid4().hex
    version_path = path.with_suffix(".version.js")
    refresh_script = _report_refresh_script(version, version_path.name)
    service_script = _terminal_service_script()
    table_min_width = max(1100, 1020 + 142 * len(venues))
    doc = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root{{--bg:#050505;--surface:#0b0b0b;--hover:#1d1b15;--ink:#e4e4e4;--muted:#8c8c8c;--line:#2d2d2d;--green:#52db8c;--amber:#ffab00;--blue:#78bbff;--red:#ff6374}}
*{{box-sizing:border-box}}[hidden]{{display:none!important}}html,body{{height:100%;margin:0;overflow:hidden}}body{{display:flex;flex-direction:column;background:var(--bg);color:var(--ink);font:11px/1.35 "SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace;font-variant-numeric:tabular-nums}}button,input,select{{font:inherit}}button{{cursor:pointer}}button:disabled{{opacity:.35;cursor:default}}button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible{{outline:2px solid var(--amber);outline-offset:1px}}button,input,select{{border:1px solid #454545;border-radius:0;background:#111;color:var(--ink)}}button{{padding:3px 6px}}a{{color:var(--blue)}}
.topbar{{height:33px;flex:none;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:0 10px;border-bottom:1px solid #6e4a00;background:#14120d}}h1{{font-size:12px;letter-spacing:.5px;display:inline;margin:0;font-weight:700;text-transform:uppercase;color:var(--amber)}}.brand-mark{{color:var(--amber);font-size:17px;margin-right:7px}}.mode{{font-size:8px;letter-spacing:.4px;color:var(--amber);margin-left:10px;border-left:1px solid #655125;padding-left:10px}}.source-age{{color:var(--muted);font-size:9px;white-space:nowrap}}.source-age strong{{font-weight:400}}.summary{{display:flex;gap:14px;font-size:10px;color:var(--muted)}}.summary b{{color:var(--amber);font-weight:700}}#crossSummary{{color:var(--green)}}
main{{flex:1;min-height:0;display:flex;flex-direction:column;padding:0 8px}}.navline{{flex:none;height:32px;display:flex;justify-content:space-between;align-items:stretch;gap:12px;border-bottom:1px solid var(--line)}}.tabs{{display:flex;gap:2px;align-items:stretch;overflow-x:auto;scrollbar-width:none}}.tab{{border:0;border-bottom:2px solid transparent;background:transparent;color:#b8b8b8;padding:0 8px;font-size:10px;white-space:nowrap;text-transform:uppercase}}.tab.active{{color:#070707;border-color:var(--amber);background:var(--amber);font-weight:700}}.tab span{{font-size:9px;color:var(--green);margin-left:5px}}.tab.active span{{color:#153d18}}.controls{{display:flex;align-items:center;gap:6px;flex:none}}.search{{width:240px;padding:3px 6px;font-size:10px;background:#030303;color:var(--amber)}}.search::placeholder{{color:#999}}.toolline{{height:28px;flex:none;display:flex;align-items:center;justify-content:space-between;gap:10px;color:var(--muted);font-size:9px}}.toggles{{display:flex;gap:14px;align-items:center}}.toggle{{display:flex;align-items:center;gap:4px;white-space:nowrap}}.toggle input{{margin:0;accent-color:var(--amber);width:11px;height:11px}}.sort-control{{display:flex;gap:5px;align-items:center}}select{{padding:2px 4px;font-size:9px}}.sort-direction{{font-size:10px;padding:1px 6px}}.legend{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.conditional{{display:inline-block;color:var(--amber);font-size:8px;border:1px solid #735000;padding:0 3px;line-height:11px;vertical-align:middle}}
#marketPanel{{display:flex;flex-direction:column;flex:1;min-height:0}}.table-shell{{flex:1;min-height:0;overflow:auto;border:1px solid var(--line);background:var(--surface);position:relative}}table{{border-collapse:separate;border-spacing:0;width:100%;min-width:{table_min_width}px;table-layout:fixed}}th{{position:sticky;top:0;z-index:2;height:27px;background:#242016;border-bottom:1px solid #735000;color:var(--amber);font-size:9px;font-weight:600;text-transform:uppercase;text-align:left;padding:0 6px;white-space:nowrap}}th span{{font-size:8px;color:#b7a57d;margin-left:5px;text-transform:none}}td{{height:25px;padding:0 6px;border-bottom:1px solid #222;border-right:1px solid #191919;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:10px}}tbody tr:last-child td{{border-bottom:0}}tbody tr:nth-child(even){{background:#101010}}tbody tr:hover{{background:var(--hover)}}tr[data-net-crossed="true"] .edge-cell{{background:#12351f}}.league-cell{{color:#a0a0a0;font-size:9px}}.league-head{{width:86px}}.competition-head{{width:112px}}.competition-cell{{color:#b8a170;font-size:9px}}.fixture-head{{width:auto}}.date-head{{width:102px}}.outcome-head{{width:156px}}.price-heading{{width:142px}}.edge-head{{width:83px}}.rules-head{{width:100px}}.detail-head{{width:27px}}.fixture-cell{{color:#f0c373}}.outcome-cell{{text-transform:capitalize;color:#dedede}}.date-cell{{font-size:9px;color:var(--muted)}}.quotes{{display:flex;align-items:center;justify-content:flex-end;gap:5px;font-size:11px}}.quotes strong{{font-weight:500;min-width:35px;text-align:right}}.quote-side{{display:flex;flex-direction:column;align-items:flex-end;line-height:1.1}}.quote-size{{font-size:8px;color:#aaa;max-width:68px;overflow:hidden;text-overflow:ellipsis}}.net-estimate{{display:flex;flex-direction:column;line-height:1.15}}.gross-edge{{font-size:8px;color:#aaa}}.edge-negative{{font-size:11px;color:var(--red)}}#refreshStatus{{font-size:8px;color:var(--amber);max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}#refreshPrices{{font-size:9px;color:var(--amber);height:23px;white-space:nowrap}}.bid{{color:var(--green)}}.ask{{color:var(--blue)}}.quote-divider{{color:#777;font-size:9px}}.edge-positive{{color:var(--green);font-size:11px;font-weight:700}}.expired-label,.muted{{color:var(--muted);font-size:9px}}.rule-ok{{font-size:9px;color:#9dbba6}}.rule-review{{font-size:9px;color:var(--amber)}}.details-button{{border:0;background:transparent;color:var(--amber);font-size:13px;padding:1px 2px}}.details-button:hover{{background:var(--amber);color:#000}}.empty{{position:sticky;left:0;padding:40px 16px;text-align:center;color:var(--muted);font-size:11px}}.empty strong{{display:block;color:var(--amber);margin-bottom:7px}}.pager{{height:29px;flex:none;display:flex;align-items:center;justify-content:space-between;gap:12px;color:var(--muted);font-size:9px}}.page-buttons{{display:flex;gap:3px;align-items:center}}.page-buttons button{{height:21px;min-width:23px;font-size:10px;padding:1px 5px}}#pageStatus{{min-width:70px;text-align:center;font-size:9px}}.page-size{{display:flex;gap:6px;align-items:center}}footer{{height:23px;flex:none;border-top:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:12px;padding:0 10px;color:#929292;font-size:8px;white-space:nowrap;overflow:hidden}}footer span{{overflow:hidden;text-overflow:ellipsis}}.coverage{{text-align:right}}
.dialog{{position:fixed;inset:0 0 0 auto;width:min(540px,100%);height:100dvh;max-height:100dvh;max-width:100%;margin:0;border:0;border-left:1px solid #805400;padding:16px;background:#0b0b0b;color:var(--ink);box-shadow:-12px 0 50px #0009;overflow:auto}}.dialog::backdrop{{background:#0009}}.close-dialog{{position:sticky;top:0;float:right;width:24px;height:24px;font-size:17px;padding:0;background:#252014;color:var(--amber)}}.dialog h2{{font-size:14px;line-height:1.4;margin:9px 28px 3px 0;font-weight:600;color:var(--amber)}}.detail-subtitle{{color:var(--muted);font-size:10px;margin-bottom:17px}}.detail-basis{{font-size:9px;text-transform:uppercase;letter-spacing:.5px;color:var(--amber)}}.detail-reason{{font-size:11px;line-height:1.6;color:#d0d0d0}}.detail-note{{font-size:9px;line-height:1.6;color:var(--muted)}}.detail-venue{{border-top:1px solid #5e451a;margin-top:17px;padding-top:12px}}.detail-venue h3{{font-size:12px;font-weight:600;display:inline;margin-right:16px;color:var(--amber)}}.detail-venue>a{{font-size:9px;text-decoration:none}}.detail-venue p{{font-size:10px;color:#b6b6b6}}.detail-quotes{{display:flex;align-items:center;gap:14px;border:1px solid var(--line);background:#111;padding:9px 10px;margin:10px 0}}.detail-quotes>div{{display:flex;align-items:center;gap:7px;flex-wrap:wrap}}.detail-depth{{font-size:9px;color:#aaa}}.fee-estimate{{border:1px solid #66501e;background:#14120d;padding:9px 10px;margin-top:12px}}.fee-estimate h3{{margin:0 0 9px;color:var(--amber);font-size:11px;font-weight:500}}.fee-breakdown{{grid-template-columns:105px minmax(0,1fr)}}.detail-quotes>div>span{{text-transform:uppercase;font-size:9px;color:#b8b8b8}}.detail-quotes strong{{font-size:18px;font-weight:500}}.quote-status{{margin-left:auto;font-size:8px;text-transform:uppercase;color:var(--muted)}}dl{{display:grid;grid-template-columns:68px minmax(0,1fr);gap:5px;font-size:9px}}dt{{color:var(--muted)}}dd{{margin:0;overflow-wrap:anywhere}}summary{{cursor:pointer;color:#c6a66e;font-size:10px;margin-top:12px}}.caveats li{{font-size:10px;color:var(--muted);margin:7px 0}}pre{{font:9px/1.6 "SFMono-Regular",Consolas,Menlo,monospace;background:#050505;border:1px solid var(--line);padding:8px;white-space:pre-wrap;overflow-wrap:anywhere;color:#b5b5b5}}.payout-table{{min-width:0;table-layout:auto;width:100%;margin-top:10px}}.payout-table caption{{text-align:left;font-size:9px;color:var(--muted);padding-bottom:7px}}.payout-table th{{position:static;height:23px;font-size:9px;background:#151515;color:#999;border-bottom:1px solid var(--line)}}.payout-table td{{height:23px;font-size:10px;color:#bbb}}.payout-table td:last-child,.payout-table th:last-child{{text-align:right}}
@media(max-width:1050px){{.summary{{gap:9px}}.source-age strong{{display:none}}.legend{{display:none}}.search{{width:190px}}.tab{{padding:0 7px}}.fixture-head{{width:250px}}}}@media(max-width:620px){{.topbar{{padding:0 7px;height:32px}}h1{{font-size:11px}}.summary{{font-size:8px;gap:8px}}.summary .review-summary{{display:none}}.mode{{display:none}}.source-age{{font-size:8px}}main{{padding:0 4px}}.navline{{height:auto;min-height:59px;flex-wrap:wrap;gap:0;padding-bottom:5px}}.tabs{{width:100%;height:30px}}.controls{{width:100%}}.search{{width:100%}}.toolline{{height:29px}}.toggles{{gap:9px;font-size:8px}}.sort-control label{{display:none}}footer{{height:22px;padding:0 6px;font-size:7px}}footer .coverage{{display:none}}.page-size{{gap:3px}}.pager{{gap:6px}}.dialog{{padding:13px}}.detail-quotes{{gap:16px}}}}
</style></head><body>
<header class="topbar"><div><span class="brand-mark" aria-hidden="true">≋</span><h1>Market Terminal</h1><span class="mode">{badge}</span></div><div class="summary"><span><b>{matched}</b> comparable</span><span><b id="netCrossSummary">{net_crosses}</b> net +</span><span><b id="crossSummary">{crosses}</b> gross</span><span class="review-summary"><b>{reviews}</b> reviews</span></div><div class="source-age" title="{html.escape(collection_note, quote=True)}"><span id="reportAge">Source age…</span> <strong>· {timestamp_label} {html.escape(timestamp_value)}</strong></div></header>
<main><div class="navline"><nav class="tabs" role="tablist" aria-label="Sports and crosses">{tabs_html}</nav><div class="controls"><span id="refreshStatus" role="status" hidden></span><button id="refreshPrices" hidden>Refresh prices</button><input class="search" id="search" type="search" aria-label="Search fixtures and outcomes" placeholder="Search fixture, team, outcome  /" autocomplete="off"></div></div>
<div class="toolline"><div class="toggles"><label class="toggle"><input id="showReviews" type="checkbox" checked>Reviews</label><label class="toggle"><input id="strictRules" type="checkbox">Full contract only</label><span class="legend"><span class="conditional">C</span> contingency caveat · core payouts must agree</span></div><div class="sort-control"><label for="sortBy">Sort</label><select id="sortBy" aria-label="Sort markets"><option value="date">Date</option><option value="edge">Net edge</option><option value="name">Name</option></select><button class="sort-direction" id="sortDirection" aria-label="Reverse sort direction" title="Reverse sort">↑</button></div></div>
<section id="marketPanel" role="tabpanel" aria-labelledby="tab-all"><div class="table-shell" id="tableScroll"><table><thead><tr><th scope="col" class="league-head">Sport</th><th scope="col" class="competition-head">League</th><th scope="col" class="fixture-head">Fixture</th><th scope="col" class="date-head">Date / UTC</th><th scope="col" class="outcome-head">Outcome</th>{headers}<th scope="col" class="edge-head">Net / gross<span>1 ct</span></th><th scope="col" class="rules-head">Rules</th><th scope="col" class="detail-head"><span class="muted">↗</span></th></tr></thead><tbody id="rows">{initial}</tbody></table><div id="empty" class="empty" hidden><strong id="emptyHeading">No matching markets</strong><span id="emptyMessage">Change sport, search, or settlement filter.</span></div></div>
<div class="pager"><span id="resultCount" aria-live="polite">{len(rows)} outcomes</span><div class="page-buttons"><button id="firstPage" aria-label="First page">«</button><button id="prevPage" aria-label="Previous page">‹</button><span id="pageStatus">1 / 1</span><button id="nextPage" aria-label="Next page">›</button><button id="lastPage" aria-label="Last page">»</button></div><label class="page-size" for="pageSize">Rows<select id="pageSize" aria-label="Rows per page"><option value="25">25</option><option value="50">50</option><option value="100" selected>100</option></select></label></div></section></main>
<footer><span title="{html.escape(source_note, quote=True)}">{source_note} · Net: both taker fees, 1 ct/leg estimate; ×ct: top depth; check venue settlement rules. A cross is not a guarantee of profit.</span><span class="coverage">{html.escape(coverage)}</span></footer>
<dialog id="detailsDialog" class="dialog" aria-label="Market comparison details"><button id="closeDetails" class="close-dialog" aria-label="Close details">×</button><div id="detailContent"></div></dialog>
<script type="application/json" id="marketData">{payload}</script><script>
const records=JSON.parse(document.getElementById('marketData').textContent),tbody=document.getElementById('rows'),search=document.getElementById('search'),reviewToggle=document.getElementById('showReviews'),strictToggle=document.getElementById('strictRules'),sortBy=document.getElementById('sortBy'),pageSize=document.getElementById('pageSize'),tabs=[...document.querySelectorAll('[data-tab]')];let activeTab='all',page=0,direction=1,filtered=[];
function matchingRecords(){{const q=search.value.trim().toLowerCase();return records.filter(r=>(activeTab==='all'||(activeTab==='net'?r.netCrossed:activeTab==='crosses'?r.crossed:r.sport===activeTab))&&(reviewToggle.checked||!r.review)&&(!strictToggle.checked||r.strict)&&(!q||r.search.includes(q))).sort((a,b)=>{{let result=0;if(sortBy.value==='edge'){{const av=a.edge===''?-Infinity:Number(a.edge),bv=b.edge===''?-Infinity:Number(b.edge);result=av===bv?0:av>bv?-1:1}}else{{result=String(a[sortBy.value]||'').localeCompare(String(b[sortBy.value]||''))}}return direction*result||a.id-b.id}})}}
function apply(reset=false){{if(reset)page=0;filtered=matchingRecords();const size=Number(pageSize.value),pages=Math.max(1,Math.ceil(filtered.length/size));page=Math.min(page,pages-1);const start=page*size,visible=filtered.slice(start,start+size);tbody.innerHTML=visible.map(r=>r.html).join('');visible.forEach(r=>{{if(r.stale){{const tr=tbody.querySelector(`[data-id="${{r.id}}"]`);tr.dataset.crossed='false';tr.dataset.netCrossed='false';const edge=tr.querySelector('.net-estimate')||tr.querySelector('.edge-positive'),label=tr.querySelector('.expired-label');if(edge)edge.hidden=true;if(label)label.hidden=false}}}});document.getElementById('empty').hidden=filtered.length!==0;document.getElementById('emptyHeading').textContent=activeTab==='net'?'No positive net estimates':activeTab==='crosses'?'No eligible gross crosses':'No matching markets';document.getElementById('emptyMessage').textContent=['net','crosses'].includes(activeTab)?'Try All or a sport tab to inspect prices, fees and comparisons.':'Change sport, search, or settlement filter.';document.getElementById('resultCount').textContent=filtered.length?`${{start+1}}–${{Math.min(start+size,filtered.length)}} of ${{filtered.length}} outcomes`:'0 outcomes';document.getElementById('pageStatus').textContent=`${{page+1}} / ${{pages}}`;document.getElementById('firstPage').disabled=document.getElementById('prevPage').disabled=page===0;document.getElementById('nextPage').disabled=document.getElementById('lastPage').disabled=page>=pages-1;if(reset)document.getElementById('tableScroll').scrollTop=0;}}
function selectTab(tab){{activeTab=tab.dataset.tab;tabs.forEach(t=>{{const selected=t===tab;t.classList.toggle('active',selected);t.setAttribute('aria-selected',String(selected));t.tabIndex=selected?0:-1}});document.getElementById('marketPanel').setAttribute('aria-labelledby',tab.id);apply(true)}}
tabs.forEach((tab,index)=>{{tab.addEventListener('click',()=>selectTab(tab));tab.addEventListener('keydown',e=>{{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){{e.preventDefault();const next=e.key==='Home'?0:e.key==='End'?tabs.length-1:(index+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;selectTab(tabs[next]);tabs[next].focus()}}}})}});
search.addEventListener('input',()=>apply(true));reviewToggle.addEventListener('change',()=>apply(true));strictToggle.addEventListener('change',()=>apply(true));pageSize.addEventListener('change',()=>apply(true));sortBy.addEventListener('change',()=>{{direction=1;document.getElementById('sortDirection').textContent='↑';apply(true)}});document.getElementById('sortDirection').addEventListener('click',()=>{{direction*=-1;document.getElementById('sortDirection').textContent=direction===1?'↑':'↓';apply(true)}});document.getElementById('firstPage').addEventListener('click',()=>{{page=0;apply(true)}});document.getElementById('prevPage').addEventListener('click',()=>{{page=Math.max(0,page-1);apply();document.getElementById('tableScroll').scrollTop=0}});document.getElementById('nextPage').addEventListener('click',()=>{{page++;apply();document.getElementById('tableScroll').scrollTop=0}});document.getElementById('lastPage').addEventListener('click',()=>{{page=Math.max(0,Math.ceil(filtered.length/Number(pageSize.value))-1);apply();document.getElementById('tableScroll').scrollTop=0}});
const dialog=document.getElementById('detailsDialog'),content=document.getElementById('detailContent');tbody.addEventListener('click',e=>{{const button=e.target.closest('[data-details]');if(!button)return;content.innerHTML=records[Number(button.dataset.details)].details;dialog.showModal()}});document.getElementById('closeDetails').addEventListener('click',()=>dialog.close());dialog.addEventListener('click',e=>{{if(e.target===dialog&&e.clientX<dialog.getBoundingClientRect().left)dialog.close()}});
document.addEventListener('keydown',e=>{{const editing=['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName);if(e.key==='/'&&!editing&&!dialog.open){{e.preventDefault();search.focus()}}else if(e.key==='Escape'&&document.activeElement===search){{search.value='';apply(true);search.blur()}}else if(!editing&&!dialog.open&&['e','d','n'].includes(e.key.toLowerCase())){{sortBy.value={{e:'edge',d:'date',n:'name'}}[e.key.toLowerCase()];direction=1;apply(true)}}}});
const generatedAt=new Date('{freshness_time.isoformat()}');const ageLabel='{age_label}';function updateAge(){{const minutes=Math.max(0,Math.floor((Date.now()-generatedAt.getTime())/60000));document.getElementById('reportAge').textContent=minutes<2?ageLabel+' just now':minutes<60?`${{ageLabel}} ${{minutes}}m ago`:`${{ageLabel}} ${{Math.floor(minutes/60)}}h ago`}}
function expireCrosses(){{let changed=false;records.forEach(r=>{{if(r.expiry&&!r.stale&&Date.now()>r.expiry){{r.crossed=false;r.netCrossed=false;r.edge='';r.stale=true;changed=true}}}});if(changed){{const n=records.filter(r=>r.crossed).length;document.getElementById('crossSummary').textContent=n;document.getElementById('crossCount').textContent=n;const net=records.filter(r=>r.netCrossed).length;document.getElementById('netCrossSummary').textContent=net;document.getElementById('netCrossCount').textContent=net;apply()}}}}updateAge();expireCrosses();setInterval(()=>{{updateAge();expireCrosses()}},10000);
</script><script id="reportRefresh">{refresh_script}</script><script id="terminalService">{service_script}</script></body></html>'''
    html_temp = path.with_name(f".{path.name}.{version}.tmp")
    version_temp = version_path.with_name(f".{version_path.name}.{version}.tmp")
    try:
        html_temp.write_text(doc, encoding="utf-8")
        version_temp.write_text(f"window.marketTerminalReportVersion({json.dumps(version)});\n", encoding="utf-8")
        html_temp.replace(path)
        version_temp.replace(version_path)
    finally:
        html_temp.unlink(missing_ok=True)
        version_temp.unlink(missing_ok=True)


def write_outputs(outdir: Path, accepted: list[Any], review: list[Any], coverage_complete: dict[str, bool],
                  venues: list[str], generated_at: datetime, quality: Optional[dict[str, str]] = None,
                  fixture_review: Optional[list[Any]] = None, include_review: bool = False,
                  as_of: Optional[datetime] = None,
                  sport_coverage: Optional[dict[str, dict[str, bool]]] = None) -> list[dict[str, str]]:
    rows = build_wide_rows(accepted, review, coverage_complete, venues, fixture_review,
                           include_review, as_of, sport_coverage)
    write_wide_csv(outdir / "market_map.csv", rows, venues)
    write_html(outdir / "CricketMatchingReport.html", rows, venues, coverage_complete, generated_at, quality)
    return rows
