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

import settlement as S

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
    "best_bid", "best_ask", "best_bid_venue", "best_ask_venue", "best_spread",
    "observed_crossed", "observed_cross_kind", "observed_cross_gap",
    "cross_eligible", "quote_check", "crossed", "cross_kind", "cross_gap",
    "eligible_bid", "eligible_ask", "eligible_bid_venue", "eligible_ask_venue",
]
VENUE_FIELDS = [
    "event_id", "market_id", "slug", "title", "url", "bid", "ask", "quote_time",
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


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _sport(fixture: Any) -> str:
    value = str(getattr(getattr(fixture, "key", None), "sport", "cricket") or "cricket").lower()
    return "basketball" if value == "nba" else value


def _profile(market: Any) -> dict:
    profile = (getattr(market, "extra", {}) or {}).get("settlement")
    return profile if isinstance(profile, dict) else {}


def _market_dict(market: Any) -> dict[str, str]:
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
    problems = []
    for bid_venue in venues:
        bid = _price(row.get(f"{bid_venue}_bid"))
        if bid is None:
            continue
        for ask_venue in venues:
            ask = _price(row.get(f"{ask_venue}_ask"))
            if ask_venue == bid_venue or ask is None:
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
    check = "fresh" if pairs else next((name for name in ("future", "stale", "skewed", "unknown")
                                        if name in problems), "unpriced")
    compatible = row.get("comparison_status") == "compatible" and row.get("mapping_status") == "accepted"
    eligible = compatible and bool(pairs)
    best = max(pairs, key=lambda pair: pair[0]) if pairs and eligible else None
    crossed = best is not None and best[0] > 0
    return {
        "cross_eligible": str(eligible).lower(), "quote_check": check,
        "crossed": str(crossed).lower(), "cross_kind": "across venues" if crossed else "",
        "cross_gap": _price_text(best[0] if crossed else None),
        "eligible_bid": _price_text(best[3]) if best else "",
        "eligible_ask": _price_text(best[4]) if best else "",
        "eligible_bid_venue": best[1] if best else "", "eligible_ask_venue": best[2] if best else "",
    }


def _comparison(markets: dict[str, Any], mapping_status: str, reasons: list[str], fixture: Any) -> tuple[str, str]:
    profiles = [_profile(market) for market in markets.values()]
    comparisons = [S.compare_profiles(left, right) for i, left in enumerate(profiles)
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
    return status, "; ".join(dict.fromkeys(reason for reason in details if reason)) or "Settlement profiles agree"


def build_wide_rows(accepted: list[Any], review: list[Any], coverage_complete: dict[str, bool],
                    venues: list[str], fixture_review: Optional[list[Any]] = None,
                    include_review: bool = False, as_of: Optional[datetime] = None,
                    sport_coverage: Optional[dict[str, dict[str, bool]]] = None) -> list[dict[str, str]]:
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
                current = _market_dict(market)
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
        comparison_status, reason = _comparison(record["sources"], record["mapping_status"], record["reasons"], fixture)
        row = {
            "sport": key[0], "canonical_fixture_id": fixture.canonical_fixture_id,
            "fixture_date": fixture.key.date, "start_time_utc": fixture.start_time,
            "competition": fixture.competition, "gender": fixture.key.gender, "format": fixture.key.fmt,
            "team_a": fixture.team_a, "team_b": fixture.team_b,
            "canonical_market_type": record["market_type"], "canonical_outcome": record["outcome"],
            "venues_available": str(len(markets)),
            "venues_checked": str(sum(bool(row_coverage.get(v)) for v in venues)),
            "mapping_status": record["mapping_status"], "comparison_status": comparison_status,
            "comparison_reason": reason,
        }
        for venue in venues:
            market = dict(markets[venue]) if venue in markets else None
            alternatives = record["alternatives"].get(venue)
            if alternatives and market:
                market["alternatives"] = _json([dict(market), *alternatives])
                market.update(bid="", ask="", quote_status="unavailable", crossed="false")
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
    return (f'<div class="quotes"><div><span>Bid</span><strong>{_cents(row.get(f"{venue}_bid", ""))}</strong></div>'
            f'<div><span>Ask</span><strong>{_cents(row.get(f"{venue}_ask", ""))}</strong></div></div>')


def _edge_cell(row: dict[str, str], index: int) -> str:
    status = row.get("comparison_status", "rules_unknown")
    labels = {"rules_differ": "Rules differ", "rules_unknown": "Rules unknown",
              "fixture_review": "Fixture review", "market_review": "Market review"}
    if row.get("crossed") == "true":
        main = f'<strong class="edge-positive">{_cents(row.get("cross_gap", ""))} edge</strong><span class="rule-state">Compatible rules</span>'
    elif status != "compatible":
        hint = _rule_hint(row) if status == "rules_differ" else "Comparison excluded"
        main = f'<span class="rule-review">{labels.get(status, "Needs review")}</span><span class="rule-state">{html.escape(hint)}</span>'
    elif row.get("quote_check") != "fresh":
        labels = {"stale": "Quotes stale", "future": "Quote time conflict", "skewed": "Quotes out of sync",
                  "unknown": "Quote time unknown", "unpriced": "Quotes incomplete"}
        main = f'<span class="muted">{labels.get(row.get("quote_check"), "Quotes unavailable")}</span><span class="rule-state">Compatible rules</span>'
    else:
        main = '<span class="edge-flat">No cross</span><span class="rule-state">Compatible rules</span>'
    return main + f'<button class="details-button" data-details="{index}" aria-haspopup="dialog">Details ↗</button>'


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
        profiles.append((VENUE_LABELS[venue], payouts, policies))
    for state, label in (("draw", "Draw"), ("tie_without_tiebreak", "Tie"),
                         ("cancelled_before_start", "Cancellation"), ("void", "Void")):
        values = [(venue, payouts.get(state)) for venue, payouts, _ in profiles if payouts.get(state) is not None]
        if len({str(value) for _, value in values}) > 1:
            return label + ": " + " / ".join(_cents(str(value)) if _price(value) is not None else
                                               {"fair_market_price": "fair price", "last_fair_market_price": "fair price"}.get(str(value), str(value))
                                               for _, value in values)
    for policy in ("postponement", "overtime", "period", "tiebreakers", "dls"):
        values = {str(policies.get(policy)) for _, _, policies in profiles if policies.get(policy) is not None}
        if len(values) > 1:
            return policy.replace("_", " ").capitalize() + " differs"
    return "Payout rules differ"


def _json_details(raw: str) -> str:
    try:
        return html.escape(json.dumps(json.loads(raw), indent=2, ensure_ascii=False))
    except (ValueError, TypeError):
        return html.escape(raw or "Not available")


def _details(row: dict[str, str], venues: list[str]) -> str:
    sections = [f'<p class="detail-reason">{html.escape(row.get("comparison_reason", ""))}</p>',
                '<p class="detail-note">Crosses require compatible known settlement rules, observations no older than '
                f'{QUOTE_MAX_AGE_SECONDS // 60} minutes and at most {QUOTE_MAX_SKEW_SECONDS} seconds apart. '
                'Displayed gaps exclude fees and executable depth.</p>']
    for venue in venues:
        if row.get(f"{venue}_status") != "Available":
            continue
        url = row.get(f"{venue}_url", "")
        link = (f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">Open venue event ↗</a>' if url else "")
        sections.append(f'<section class="detail-venue"><h3>{html.escape(VENUE_LABELS[venue])}</h3>{link}'
                        f'<p>{html.escape(row.get(f"{venue}_title", ""))}</p>'
                        f'<dl><dt>Market / outcome</dt><dd>{html.escape(row.get(f"{venue}_market_id", ""))}</dd>'
                        f'<dt>Settlement model</dt><dd>{html.escape(row.get(f"{venue}_settlement_profile", "") or "Unknown")}</dd>'
                        f'<dt>Rule summary</dt><dd>{html.escape(row.get(f"{venue}_settlement_label", "") or "Unknown")}</dd>'
                        f'<dt>Observed at</dt><dd>{html.escape(row.get(f"{venue}_quote_time", "") or "Unknown")}</dd>'
                        f'<dt>Quote source</dt><dd>{html.escape(row.get(f"{venue}_quote_source", "") or "Unavailable")}</dd></dl>'
                        f'<h4>Payouts by result</h4><pre>{_json_details(row.get(f"{venue}_settlement_payouts", ""))}</pre>'
                        f'<h4>Settlement policies</h4><pre>{_json_details(row.get(f"{venue}_settlement_policies", ""))}</pre>'
                        f'<h4>Rule evidence</h4><pre>{_json_details(row.get(f"{venue}_settlement_evidence", ""))}</pre>'
                        f'<h4>Unresolved rule fields</h4><pre>{_json_details(row.get(f"{venue}_settlement_missing", ""))}</pre>'
                        '<details><summary>Quote and depth diagnostics</summary>'
                        f'<pre>{_json_details(row.get(f"{venue}_quote_details", ""))}</pre>'
                        f'<pre>{_json_details(row.get(f"{venue}_alternatives", ""))}</pre></details></section>')
    return "".join(sections)


def write_html(path: Path, rows: list[dict[str, str]], venues: list[str], coverage_complete: dict[str, bool],
               generated_at: datetime, quality: Optional[dict[str, str]] = None) -> None:
    quality = quality or {}
    replay = str(quality.get("Run type", "")).upper() == "REPLAY"
    unknown = str(quality.get("Source collection", "")).lower() == "unknown"
    heading = "Market comparison · replay" if replay else "Market comparison"
    title = "Market Terminal · Snapshot Replay" if replay else "Market Terminal"
    badge = "SNAPSHOT REPLAY" if replay else "LIVE API REPORT"
    timestamp_label = "Source collection time unavailable" if unknown else "Snapshot collected" if replay else "Live collection completed"
    timestamp_value = "" if unknown else generated_at.strftime("%d %b %Y, %H:%M:%S UTC")
    freshness_time = datetime.now(timezone.utc) if replay else generated_at
    age_label = "Replay generated" if replay else "Generated"
    source_note = ("Venue availability reflects the saved collection. Replay generation does not refresh source quotes."
                   if replay else "Point-in-time venue observations. Refresh with python3 run.py.")
    matched = sum(row.get("mapping_status", "accepted") == "accepted" for row in rows)
    crosses = sum(row.get("crossed") == "true" for row in rows)
    reviews = sum(row.get("comparison_status") != "compatible" for row in rows)
    coverage = " · ".join(f'{VENUE_LABELS[v]} {"complete" if coverage_complete.get(v) else "coverage incomplete"}' for v in venues)
    body, templates = [], []
    for index, row in enumerate(rows):
        sport = row.get("sport", "cricket")
        searchable = " ".join(str(value) for value in row.values()).lower()
        quote_cells = "".join(f'<td class="venue-cell" data-label="{html.escape(VENUE_LABELS[v])}">{_venue_cell(row, v)}</td>' for v in venues)
        reviewed = row.get("comparison_status") != "compatible"
        pair_times = [_timestamp(row.get(f"{row.get(f'eligible_{side}_venue', '')}_quote_time", ""))
                      for side in ("bid", "ask")]
        expires = (int((min(pair_times).timestamp() + QUOTE_MAX_AGE_SECONDS) * 1000)
                   if all(pair_times) and not replay else 0)
        body.append(f'<tr data-sport="{html.escape(sport, quote=True)}" data-crossed="{row.get("crossed", "false")}" '
                    f'data-cross-expiry="{expires}" '
                    f'data-review="{str(reviewed).lower()}" data-search="{html.escape(searchable, quote=True)}">'
                    f'<td class="fixture-cell"><strong>{html.escape(row["team_a"])} <span class="versus">vs</span> {html.escape(row["team_b"])}</strong>'
                    f'<span class="fixture-meta">{html.escape(row["fixture_date"])} · {html.escape(row["start_time_utc"] or "Time unavailable")} UTC · {html.escape(row["competition"])}</span></td>'
                    f'<td class="outcome-cell"><strong>{html.escape(row["canonical_outcome"])}</strong>'
                    f'<span class="fixture-meta">{html.escape(row["canonical_market_type"].replace("_", " ").title())}</span></td>'
                    f'{quote_cells}<td class="edge-cell">{_edge_cell(row, index)}</td></tr>')
        templates.append(f'<template id="detail-{index}"><h2>{html.escape(row["team_a"])} vs {html.escape(row["team_b"])}</h2>'
                         f'<p class="detail-subtitle">{html.escape(row["canonical_outcome"])} · {html.escape(row["fixture_date"])}</p>{_details(row, venues)}</template>')
    headers = "".join(f'<th scope="col">{html.escape(VENUE_LABELS[v])} <span>bid / ask</span></th>' for v in venues)
    doc = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root{{--bg:#f6f8fa;--surface:#fff;--ink:#183046;--muted:#7b8996;--line:#e5ebef;--green:#087f6f;--amber:#98601a}}*{{box-sizing:border-box}}[hidden]{{display:none!important}}body{{margin:0;color:var(--ink);background:var(--bg);font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}button,input{{font:inherit}}button,a{{-webkit-tap-highlight-color:transparent}}button:focus-visible,a:focus-visible,input:focus-visible{{outline:2px solid #218b7d;outline-offset:3px}}button{{cursor:pointer}}a{{color:var(--green)}}
.topbar{{height:64px;display:flex;align-items:center;justify-content:space-between;gap:20px;padding:0 34px;border-bottom:1px solid var(--line);background:#fff}}.brand{{font-weight:750;font-size:15px;letter-spacing:-.3px}}.brand span{{display:inline-block;color:var(--green);margin-right:9px}}.source-age{{color:var(--muted);font-size:11px}}.source-age strong{{font-weight:550;color:#587084}}.mode{{font-size:9px;letter-spacing:.8px;color:var(--green);margin-left:13px}}main{{max-width:1640px;margin:auto;padding:32px 34px 40px}}.page-heading{{display:flex;align-items:center;justify-content:space-between;margin:0 0 26px}}h1{{font-size:18px;letter-spacing:-.4px;font-weight:650;margin:0}}.summary{{color:var(--muted);font-size:11px}}.summary strong{{font-weight:650;color:var(--ink)}}
.toolbar{{display:flex;justify-content:space-between;gap:24px;align-items:center;border-bottom:1px solid var(--line);margin-bottom:18px}}.tabs{{display:flex;gap:26px;align-items:center}}.tab{{padding:12px 0 14px;border:0;border-bottom:2px solid transparent;background:transparent;color:var(--muted);font-size:12px;font-weight:600;white-space:nowrap}}.tab.active{{color:var(--green);border-color:var(--green)}}.tab-count{{font-size:9px;background:#e7f1ed;padding:2px 5px;border-radius:4px;margin-left:5px;color:var(--green)}}.search{{position:relative;width:270px;margin-bottom:7px}}.search input{{width:100%;padding:9px 12px 9px 30px;background:#fff;border:1px solid var(--line);border-radius:6px;font-size:11px;color:var(--ink)}}.search::before{{content:'⌕';position:absolute;left:11px;top:5px;font-size:18px;color:var(--muted)}}.subtoolbar{{display:flex;justify-content:space-between;color:var(--muted);font-size:10px;margin:0 0 13px;gap:15px}}.review-toggle{{display:flex;align-items:center;gap:6px}}.review-toggle input{{accent-color:var(--green);margin:0}}.tab-description{{color:var(--muted)}}
.table-shell{{border:1px solid var(--line);border-radius:8px;overflow:auto;background:#fff}}table{{width:100%;border-collapse:collapse;min-width:940px}}th{{text-align:left;background:#fbfcfd;padding:15px 20px;border-bottom:1px solid var(--line);font-size:9px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted);font-weight:600}}th span{{font-size:9px;text-transform:none;letter-spacing:0;font-weight:400;margin-left:5px}}td{{padding:23px 20px;border-bottom:1px solid var(--line);vertical-align:middle}}tbody tr:last-child td{{border-bottom:0}}tbody tr:hover{{background:#fcfefd}}tr[data-crossed="true"] .edge-cell{{background:#f6fbf8}}.fixture-cell{{width:34%;min-width:270px}}.fixture-cell>strong{{font-size:12px;font-weight:600;display:block}}.versus{{color:#9ba6af;font-weight:400;margin:0 5px}}.fixture-meta{{display:block;color:var(--muted);font-size:9px;margin-top:6px}}.outcome-cell{{min-width:160px}}.outcome-cell>strong{{font-size:12px;font-weight:600;text-transform:capitalize}}.venue-cell{{min-width:155px}}.quotes{{display:flex;gap:25px;font-variant-numeric:tabular-nums}}.quotes span{{display:block;font-size:8px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted);margin-bottom:4px}}.quotes strong{{display:block;font-size:17px;font-weight:550;letter-spacing:-.3px;min-width:40px}}.edge-cell{{min-width:160px}}.edge-positive{{display:block;font-size:16px;color:var(--green);font-weight:650}}.edge-flat{{font-size:11px;color:#627488}}.rule-state{{display:block;color:var(--muted);font-size:9px;margin-top:4px}}.rule-review{{font-size:10px;color:var(--amber);background:#faf3e8;padding:4px 7px;border-radius:4px;display:inline-block}}.muted{{color:var(--muted);font-size:10px}}.details-button{{display:block;background:none;border:0;padding:0;font-size:9px;color:#728898;margin-top:10px}}.details-button:hover{{color:var(--green)}}.empty{{padding:66px 25px;text-align:center;color:var(--muted);font-size:12px}}.empty strong{{display:block;color:#526b7b;font-size:13px;font-weight:550;margin-bottom:7px}}footer{{display:flex;justify-content:space-between;gap:25px;font-size:9px;line-height:1.7;color:var(--muted);margin-top:17px}}.source-note{{max-width:700px}}.coverage{{text-align:right}}
.dialog{{position:fixed;inset:0 0 0 auto;width:min(560px,100%);height:100dvh;max-height:100dvh;max-width:100%;margin:0;border:0;border-left:1px solid var(--line);padding:29px;background:#fff;color:var(--ink);box-shadow:-12px 0 60px #18304612;overflow:auto}}.dialog::backdrop{{background:#18304640}}.close-dialog{{position:sticky;top:0;float:right;background:#f1f5f7;color:#637789;border:0;width:30px;height:30px;border-radius:5px;font-size:18px}}.dialog h2{{font-size:19px;line-height:1.4;letter-spacing:-.3px;margin:15px 32px 5px 0}}.detail-subtitle{{color:var(--muted);font-size:11px;margin:0 0 24px}}.detail-reason{{font-size:12px;line-height:1.8;color:#526b7b;background:#f6f8fa;border-radius:6px;padding:12px}}.detail-note{{font-size:10px;color:var(--muted);line-height:1.7}}.detail-venue{{border-top:1px solid var(--line);margin-top:25px;padding-top:20px}}.detail-venue h3{{font-size:14px;display:inline-block;margin:0 20px 10px 0}}.detail-venue>a{{font-size:10px;text-decoration:none}}.detail-venue p{{font-size:11px;color:#637789}}dl{{display:grid;grid-template-columns:120px minmax(0,1fr);gap:8px;font-size:10px}}dt{{color:var(--muted)}}dd{{margin:0;overflow-wrap:anywhere}}h4{{font-size:10px;font-weight:650;margin:18px 0 7px}}pre{{font:10px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace;background:#f7f9fb;border:1px solid #edf1f4;border-radius:5px;padding:10px;white-space:pre-wrap;overflow-wrap:anywhere;color:#566e81}}summary{{cursor:pointer;font-size:10px;color:#637789;margin-top:17px}}
@media(max-width:900px){{main{{padding:25px 18px}}.topbar{{padding:0 18px}}.page-heading{{align-items:flex-start;gap:10px;flex-direction:column;margin-bottom:18px}}.toolbar{{gap:15px}}.tabs{{gap:18px}}.search{{width:225px}}.mode{{display:none}}.source-age{{font-size:9px}}footer{{flex-direction:column;gap:6px}}.coverage{{text-align:left}}}}@media(max-width:620px){{.topbar{{height:auto;min-height:60px;align-items:flex-start;flex-direction:column;gap:3px;padding:12px 16px}}.brand{{font-size:14px}}main{{padding:22px 13px}}h1{{font-size:16px}}.toolbar{{flex-direction:column;align-items:stretch;gap:5px}}.tabs{{justify-content:space-between;gap:10px}}.tab{{font-size:11px;padding-top:0}}.search{{width:100%;margin:6px 0 13px}}.subtoolbar{{font-size:9px}}.table-shell{{border-radius:6px}}th,td{{padding-left:15px;padding-right:15px}}.fixture-cell{{min-width:235px}}table{{min-width:880px}}.dialog{{padding:22px}}}}
</style></head><body>
<header class="topbar"><div class="brand"><span aria-hidden="true">≋</span>Market terminal<span class="mode">{badge}</span></div><div class="source-age"><span id="reportAge">Source age…</span> <span aria-hidden="true">·</span> <strong>{timestamp_label} {html.escape(timestamp_value)}</strong></div></header>
<main><div class="page-heading"><h1>{heading}</h1><div class="summary"><strong>{matched}</strong> comparable · <strong id="crossSummary">{crosses}</strong> compatible crosses · <strong>{reviews}</strong> reviews</div></div>
<div class="toolbar"><div class="tabs" role="tablist" aria-label="Sports and crosses"><button class="tab active" id="tab-crosses" role="tab" aria-selected="true" aria-controls="marketPanel" data-tab="crosses">All crosses <span class="tab-count">{crosses}</span></button><button class="tab" id="tab-cricket" role="tab" aria-selected="false" aria-controls="marketPanel" data-tab="cricket">Cricket</button><button class="tab" id="tab-basketball" role="tab" aria-selected="false" aria-controls="marketPanel" data-tab="basketball">Basketball <span class="muted">NBA</span></button></div><label class="search"><input id="search" type="search" aria-label="Search fixtures and outcomes" placeholder="Search fixtures or outcomes"></label></div>
<div class="subtoolbar"><span id="tabDescription" class="tab-description">Compatible settlement rules · fresh quotes only</span><label class="review-toggle"><input id="showReviews" type="checkbox" checked>Show rule reviews</label><span id="resultCount" aria-live="polite">{len(rows)} outcomes</span></div>
<section id="marketPanel" role="tabpanel" aria-labelledby="tab-crosses"><div class="table-shell"><table><thead><tr><th scope="col">Fixture / UTC</th><th scope="col">Outcome</th>{headers}<th scope="col">Edge / rules</th></tr></thead><tbody id="rows">{''.join(body)}</tbody></table><div id="empty" class="empty" hidden><strong id="emptyHeading">No compatible crosses</strong><span id="emptyMessage">Open a sport tab to see matched markets and rule reviews.</span></div></div></section>
<footer><div class="source-note">{source_note} Observed edges exclude fees and depth; check venue settlement rules. A cross is not a guarantee of profit.</div><div class="coverage">{html.escape(coverage)} · Audited data beside this report</div></footer>
</main><dialog id="detailsDialog" class="dialog" aria-label="Market comparison details"><button id="closeDetails" class="close-dialog" aria-label="Close details">×</button><div id="detailContent"></div></dialog>{''.join(templates)}
<script>
const search=document.getElementById('search'),rows=[...document.querySelectorAll('#rows tr')],empty=document.getElementById('empty'),count=document.getElementById('resultCount'),reviews=document.getElementById('showReviews'),tabs=[...document.querySelectorAll('[data-tab]')];let activeTab='crosses';
function expireCrosses(){{let changed=false;rows.forEach(r=>{{const expiry=Number(r.dataset.crossExpiry);if(r.dataset.crossed==='true'&&expiry&&Date.now()>expiry){{r.dataset.crossed='false';const edge=r.querySelector('.edge-positive');if(edge){{edge.className='muted';edge.textContent='Quotes stale'}}changed=true}}}});if(changed){{const n=rows.filter(r=>r.dataset.crossed==='true').length;document.getElementById('crossSummary').textContent=n;document.querySelector('#tab-crosses .tab-count').textContent=n;apply()}}}}
const generatedAt=new Date('{freshness_time.isoformat()}');const ageLabel='{age_label}';const age=document.getElementById('reportAge');function updateAge(){{const ageMinutes=Math.max(0,Math.floor((Date.now()-generatedAt.getTime())/60000));age.textContent=ageMinutes<2?ageLabel+' just now':ageMinutes<60?`${{ageLabel}} ${{ageMinutes}}m ago`:`${{ageLabel}} ${{Math.floor(ageMinutes/60)}}h ago`}}updateAge();
function apply(){{const q=search.value.trim().toLowerCase();let visible=0;rows.forEach(r=>{{const sport=activeTab==='crosses'?r.dataset.crossed==='true':r.dataset.sport===activeTab;const review=reviews.checked||r.dataset.review!=='true';r.hidden=!(sport&&review&&(!q||r.dataset.search.includes(q)));if(!r.hidden)visible++}});empty.hidden=visible!==0;count.textContent=`${{visible}} outcome${{visible===1?'':'s'}}`;document.getElementById('emptyHeading').textContent=activeTab==='crosses'?'No compatible crosses':'No matching outcomes';document.getElementById('emptyMessage').textContent=activeTab==='crosses'?'Open a sport tab to see matched markets and rule reviews.':'Try another search or show rule reviews.';document.getElementById('tabDescription').textContent=activeTab==='crosses'?'Compatible settlement rules · fresh quotes only':'Prices in cents · rules and quote details available per row';}}
function selectTab(tab){{activeTab=tab.dataset.tab;tabs.forEach(t=>{{const selected=t===tab;t.classList.toggle('active',selected);t.setAttribute('aria-selected',String(selected));t.tabIndex=selected?0:-1}});document.getElementById('marketPanel').setAttribute('aria-labelledby',tab.id);apply()}}
tabs.forEach((tab,index)=>{{tab.addEventListener('click',()=>selectTab(tab));tab.addEventListener('keydown',e=>{{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){{e.preventDefault();const next=e.key==='Home'?0:e.key==='End'?tabs.length-1:(index+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;selectTab(tabs[next]);tabs[next].focus()}}}})}});search.addEventListener('input',apply);reviews.addEventListener('change',apply);
const dialog=document.getElementById('detailsDialog'),content=document.getElementById('detailContent');document.querySelectorAll('[data-details]').forEach(button=>button.addEventListener('click',()=>{{content.replaceChildren(document.getElementById('detail-'+button.dataset.details).content.cloneNode(true));dialog.showModal()}}));document.getElementById('closeDetails').addEventListener('click',()=>dialog.close());dialog.addEventListener('click',e=>{{if(e.target===dialog&&e.clientX<dialog.getBoundingClientRect().left)dialog.close()}});expireCrosses();selectTab(rows.some(r=>r.dataset.crossed==='true')?tabs[0]:(tabs.find(t=>rows.some(r=>r.dataset.sport===t.dataset.tab))||tabs[0]));setInterval(()=>{{updateAge();expireCrosses()}},10000);
</script></body></html>'''
    path.write_text(doc, encoding="utf-8")


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
