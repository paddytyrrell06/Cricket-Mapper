# This file builds the clean CSV outputs and self-contained HTML report from the confirmed cross-venue market matches.

from __future__ import annotations

import csv
import html
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

VENUE_LABELS = {
    "kalshi": "Kalshi",
    "polymarket_intl": "Polymarket International",
    "polymarket_us": "Polymarket US",
}

BASE_COLS = [
    "canonical_fixture_id", "fixture_date", "start_time_utc", "competition",
    "gender", "format", "team_a", "team_b", "canonical_market_type",
    "canonical_outcome", "venues_available", "venues_checked",
    "best_bid", "best_ask", "best_bid_venue", "best_ask_venue", "best_spread",
    "crossed", "cross_kind", "cross_gap",
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


def _market_dict(market: Any) -> dict[str, str]:
    bid = _price(getattr(market, "bid", None))
    ask = _price(getattr(market, "ask", None))
    if bid is not None and ask is not None:
        quote_status = "available"
    elif bid is not None or ask is not None:
        quote_status = "one-sided"
    else:
        quote_status = ("empty" if getattr(market, "quote_status", "") == "empty"
                        else "unavailable")
    return {
        "status": "Available",
        "event_id": str(market.event_id or ""),
        "market_id": str(market.market_id or ""),
        "slug": str(market.slug or ""),
        "title": str(market.title or ""),
        "url": str(market.url or ""),
        "bid": _price_text(bid),
        "ask": _price_text(ask),
        "quote_time": str(getattr(market, "quote_time", "") or ""),
        "quote_status": quote_status,
        "quote_source": str(getattr(market, "quote_source", "") or ""),
        "crossed": str(bid is not None and ask is not None and bid > ask).lower(),
    }


def _quote_summary(row: dict[str, str], venues: list[str]) -> dict[str, str]:
    bids = [(venue, _price(row.get(f"{venue}_bid"))) for venue in venues]
    asks = [(venue, _price(row.get(f"{venue}_ask"))) for venue in venues]
    bids = [(venue, price) for venue, price in bids if price is not None]
    asks = [(venue, price) for venue, price in asks if price is not None]
    bid_venue, best_bid = max(bids, key=lambda item: item[1]) if bids else ("", None)
    ask_venue, best_ask = min(asks, key=lambda item: item[1]) if asks else ("", None)
    local_cross = any(row.get(f"{venue}_crossed") == "true" for venue in venues)
    across_cross = any(bid > ask for bv, bid in bids for av, ask in asks if bv != av)
    crossed = local_cross or across_cross
    kinds = []
    if local_cross:
        kinds.append("within venue")
    if across_cross:
        kinds.append("across venues")
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    return {
        "best_bid": _price_text(best_bid), "best_ask": _price_text(best_ask),
        "best_bid_venue": bid_venue, "best_ask_venue": ask_venue,
        "best_spread": _price_text(spread), "crossed": str(crossed).lower(),
        "cross_kind": " and ".join(kinds),
        "cross_gap": _price_text(-spread if crossed and spread is not None else None),
    }


def build_wide_rows(accepted: list[Any], review: list[Any],
                    coverage_complete: dict[str, bool],
                    venues: list[str],
                    fixture_review: Optional[list[Any]] = None) -> list[dict[str, str]]:
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    review_venues: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    fixture_review_venues: dict[str, set[str]] = defaultdict(set)
    accepted_anchors: dict[tuple[str, str], set[str]] = defaultdict(set)

    for match in accepted:
        for venue, market in ((match.venue_1, match.m1),
                              (match.venue_2, match.m2)):
            accepted_anchors[(venue, market.venue_fixture_id)].add(
                match.fixture.canonical_fixture_id)

    for match in fixture_review or []:
        anchors = {match.canonical_fixture_id}
        for venue, fixture in match.by_venue.items():
            anchors.update(accepted_anchors.get((venue, fixture.venue_fixture_id), set()))
        for canonical_fixture_id in anchors:
            fixture_review_venues[canonical_fixture_id].update(match.by_venue)

    for match in review:
        key = (match.fixture.canonical_fixture_id, match.market_type, match.outcome)
        review_venues[key].update((match.venue_1, match.venue_2))

    for match in accepted:
        key = (match.fixture.canonical_fixture_id, match.market_type, match.outcome)
        record = rows.setdefault(key, {
            "fixture": match.fixture,
            "market_type": match.market_type,
            "outcome": match.outcome,
            "markets": {},
        })
        for venue, market in ((match.venue_1, match.m1),
                              (match.venue_2, match.m2)):
            existing = record["markets"].get(venue)
            current = _market_dict(market)
            if existing and existing != current:
                raise ValueError(
                    f"conflicting accepted markets for {key} on {venue}")
            record["markets"][venue] = current

    output: list[dict[str, str]] = []
    for key, record in rows.items():
        fixture = record["fixture"]
        venue_markets = record["markets"]
        row: dict[str, str] = {
            "canonical_fixture_id": fixture.canonical_fixture_id,
            "fixture_date": fixture.key.date,
            "start_time_utc": fixture.start_time,
            "competition": fixture.competition,
            "gender": fixture.key.gender,
            "format": fixture.key.fmt,
            "team_a": fixture.team_a,
            "team_b": fixture.team_b,
            "canonical_market_type": record["market_type"],
            "canonical_outcome": record["outcome"],
            "venues_available": str(len(venue_markets)),
            "venues_checked": str(sum(bool(coverage_complete.get(v)) for v in venues)),
        }
        for venue in venues:
            prefix = venue
            market = venue_markets.get(venue)
            if market:
                status = "Available"
            elif (venue in review_venues.get(key, set())
                  or venue in fixture_review_venues.get(key[0], set())):
                status = "Needs review"
            elif coverage_complete.get(venue, False):
                status = "Not currently open"
            else:
                status = "Coverage incomplete"
            row[f"{prefix}_status"] = status
            for field in ("event_id", "market_id", "slug", "title", "url",
                          "bid", "ask", "quote_time", "quote_status", "quote_source", "crossed"):
                row[f"{prefix}_{field}"] = market.get(field, "") if market else ""
        row.update(_quote_summary(row, venues))
        output.append(row)

    return sorted(output, key=lambda r: (
        r["fixture_date"], r["start_time_utc"], r["competition"],
        r["team_a"], r["team_b"], r["canonical_market_type"],
        r["canonical_outcome"],
    ))


def _columns(venues: list[str]) -> list[str]:
    cols = list(BASE_COLS)
    for venue in venues:
        cols.extend([
            f"{venue}_status", f"{venue}_event_id", f"{venue}_market_id",
            f"{venue}_slug", f"{venue}_title", f"{venue}_url",
            f"{venue}_bid", f"{venue}_ask", f"{venue}_quote_time",
            f"{venue}_quote_status", f"{venue}_quote_source", f"{venue}_crossed",
        ])
    return cols


def write_wide_csv(path: Path, rows: list[dict[str, str]],
                   venues: list[str]) -> None:
    cols = _columns(venues)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=cols)
        writer.writeheader()
        writer.writerows(rows)


def _status_badge(status: str) -> str:
    cls = {
        "Available": "available",
        "Not currently open": "missing",
        "Needs review": "review",
        "Coverage incomplete": "incomplete",
    }.get(status, "incomplete")
    return f'<span class="badge {cls}">{html.escape(status)}</span>'


def _venue_cell(row: dict[str, str], venue: str) -> str:
    status = row[f"{venue}_status"]
    content = [f'<div class="venue-name">{html.escape(VENUE_LABELS[venue])}</div>',
               _status_badge(status)]
    title = row.get(f"{venue}_title", "")
    market_id = row.get(f"{venue}_market_id", "")
    slug = row.get(f"{venue}_slug", "")
    url = row.get(f"{venue}_url", "")
    time_label = "Quote time unavailable"
    if status == "Available":
        local_cross = row.get(f"{venue}_crossed") == "true"
        content.append(
            f'<div class="quote-pair{" local-cross" if local_cross else ""}">'
            f'<div><span>Bid</span><strong>{_cents(row.get(f"{venue}_bid", ""))}</strong></div>'
            f'<div><span>Ask</span><strong>{_cents(row.get(f"{venue}_ask", ""))}</strong></div></div>')
        if local_cross:
            content.append('<span class="badge crossed">Crossed on venue</span>')
        quote_status = row.get(f"{venue}_quote_status", "unavailable")
        label = {"available": "Two-sided quote", "one-sided": "One-sided book",
                 "empty": "No orders available", "unavailable": "Quote unavailable"}.get(
                     quote_status, "Quote unavailable")
        quote_time = row.get(f"{venue}_quote_time", "")
        if quote_time:
            try:
                stamp = datetime.fromisoformat(quote_time.replace("Z", "+00:00"))
                time_label = (stamp.astimezone(timezone.utc).strftime("%d %b %H:%M:%S UTC")
                              if stamp.tzinfo is not None else quote_time)
            except ValueError:
                time_label = quote_time
        content.append(f'<div class="quote-meta">{html.escape(label)}</div>')
    if title:
        content.append(f'<div class="market-title">{html.escape(title)}</div>')
    identifier = market_id or slug
    if identifier:
        content.append(
            '<details class="identifiers"><summary>Outcome details</summary>'
            f'<div class="market-id">{html.escape(identifier)}</div>'
            f'<div class="quote-meta">{html.escape(time_label)}</div>'
            f'<div class="quote-meta">{html.escape(row.get(f"{venue}_quote_source", ""))}</div>'
            '</details>')
    if url:
        content.append(
            f'<a class="open-link" href="{html.escape(url, quote=True)}" '
            f'target="_blank" rel="noopener">Open event <span aria-hidden="true">↗</span></a>')
    elif status == "Not currently open":
        content.append('<div class="absence">Full venue collection checked.</div>')
    elif status == "Coverage incomplete":
        content.append('<div class="absence">Absence not asserted.</div>')
    elif status == "Needs review":
        content.append('<div class="absence">A possible counterpart needs review.</div>')
    return "".join(content)


def _cents(value: str) -> str:
    if not value:
        return "—"
    try:
        price = Decimal(value)
        if not price.is_finite():
            return "—"
        return format(price * 100, ".2f").rstrip("0").rstrip(".") + "¢"
    except (InvalidOperation, ValueError):
        return "—"


def _best_quote_cell(row: dict[str, str]) -> str:
    crossed = row.get("crossed") == "true"
    kind = row.get("cross_kind", "")
    badge = (f'<span class="badge crossed">Crossed · {html.escape(kind)}</span>' if crossed
             else '<span class="quote-label">Best available</span>')
    sides = []
    for side in ("bid", "ask"):
        venue = VENUE_LABELS.get(row.get(f"best_{side}_venue", ""), "No quote")
        sides.append(f'<div><span>Best {side}</span>'
                     f'<strong>{_cents(row.get(f"best_{side}", ""))}</strong>'
                     f'<small>{html.escape(venue)}</small></div>')
    gap = row.get("cross_gap", "") if crossed else row.get("best_spread", "")
    label = "Quote gap" if crossed else "Spread"
    return (badge + '<div class="best-pair">' + "".join(sides) + '</div>'
            f'<div class="spread">{label} <strong>{_cents(gap)}</strong></div>')


def write_html(path: Path, rows: list[dict[str, str]], venues: list[str],
               coverage_complete: dict[str, bool], generated_at: datetime,
               quality: Optional[dict[str, str]] = None) -> None:
    fixture_count = len({r["canonical_fixture_id"] for r in rows})
    three_count = sum(1 for r in rows if r["venues_available"] == "3")
    two_count = sum(1 for r in rows if r["venues_available"] == "2")
    quality = quality or {}
    is_replay = str(quality.get("Run type", "")).upper() == "REPLAY"
    source_time_unknown = str(quality.get("Source collection", "")).lower() == "unknown"
    if is_replay:
        page_title = "Cricket Exchange Matching · Snapshot Replay"
        badge_label = "SNAPSHOT REPLAY"
        heading = "Cricket Market Map Replay"
        description = (
            "Confirmed equivalent outcomes across Kalshi, Polymarket International "
            "and Polymarket US, rebuilt from saved venue records. "
            "Venue availability reflects the saved collection."
        )
        timestamp_label = ("Source collection time unavailable" if source_time_unknown
                           else "Snapshot collected")
        timestamp_value = ("" if source_time_unknown
                           else generated_at.strftime('%d %b %Y, %H:%M:%S UTC'))
        freshness_description = (
            "This replay uses saved venue records. Report generation time is separate "
            "from source collection time. Run <code>python3 run.py</code> to collect "
            "current markets."
        )
        freshness_time = datetime.now(timezone.utc)
        age_label = "Replay generated"
    else:
        page_title = "Cricket Exchange Matching · Live Report"
        badge_label = "LIVE API REPORT"
        heading = "Live Cricket Market Map"
        description = (
            "Confirmed equivalent outcomes across Kalshi, Polymarket International "
            "and Polymarket US. This file is regenerated from live venue APIs on "
            "every successful run."
        )
        timestamp_label = "Live collection completed"
        timestamp_value = generated_at.strftime('%d %b %Y, %H:%M:%S UTC')
        freshness_description = (
            "This HTML is a point-in-time live snapshot. Run <code>python3 run.py</code> "
            "again to refresh it."
        )
        freshness_time = generated_at
        age_label = "Generated"
    quality_cards = "".join(
        f'<div class="quality-item"><span>{html.escape(label)}</span>'
        f'<strong>{html.escape(str(value))}</strong></div>'
        for label, value in quality.items())
    coverage_cards = "".join(
        f'<div class="coverage-item"><span>{html.escape(VENUE_LABELS[v])}</span>'
        f'<span class="badge {"available" if coverage_complete.get(v) else "incomplete"}">'
        f'{"Collection complete" if coverage_complete.get(v) else "Coverage incomplete"}</span>'
        f'</div>' for v in venues)

    body_rows = []
    for row in rows:
        searchable = " ".join(str(v) for v in row.values()).lower()
        is_crossed = row.get("crossed") == "true"
        venue_cells = "".join(
            f'<td class="venue-cell">{_venue_cell(row, venue)}</td>' for venue in venues)
        body_rows.append(f'''<tr data-search="{html.escape(searchable, quote=True)}"
            data-venues="{row['venues_available']}" data-crossed="{str(is_crossed).lower()}"
            class="{'crossed-row' if is_crossed else ''}">
          <td class="fixture-cell"><div class="date">{html.escape(row['fixture_date'])}
            <span> · {html.escape(row['start_time_utc'] + ' UTC' if row['start_time_utc'] else 'Time unavailable')}</span></div>
            <div class="fixture"><strong>{html.escape(row['team_a'])}</strong>
            <span>vs</span><strong>{html.escape(row['team_b'])}</strong></div>
            <div class="meta">{html.escape(row['competition'])} · {html.escape(row['gender'])}/{html.escape(row['format'])}</div></td>
          <td class="outcome-cell"><span class="quote-label">{html.escape(row['canonical_market_type'].replace('_', ' ').title())}</span>
            <div class="outcome">{html.escape(row['canonical_outcome'])}</div>
            <div class="meta">{row['venues_available']} venues mapped</div></td>
          <td class="best-cell">{_best_quote_cell(row)}</td>{venue_cells}
        </tr>''')

    crossed_count = sum(row.get("crossed") == "true" for row in rows)
    quoted_count = sum(bool(row.get("best_bid") or row.get("best_ask")) for row in rows)
    venue_headers = "".join(f'<th scope="col">{html.escape(VENUE_LABELS[v])}</th>' for v in venues)
    generated_iso = freshness_time.isoformat()
    doc = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(page_title)}</title>
<style>
:root{{--bg:#f3f6fa;--panel:#fff;--text:#172a42;--muted:#687a90;--line:#e0e7ef;--accent:#087e75;--navy:#132d49;--gold:#9b5a06;--danger:#ab3d24}}
*{{box-sizing:border-box}}[hidden]{{display:none!important}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}a{{color:var(--accent)}}button,input{{font:inherit}}button:focus-visible,input:focus-visible,summary:focus-visible,a:focus-visible{{outline:3px solid #5ca99e;outline-offset:3px}}
.topbar{{background:var(--navy);color:#fff;padding:16px 32px;display:flex;align-items:center;justify-content:space-between}}.brand{{font-size:14px;font-weight:750;letter-spacing:.2px;display:flex;align-items:center;gap:10px}}.brand-icon{{display:grid;place-items:center;border:1px solid #567086;width:29px;height:29px;border-radius:8px;color:#8fe0ce}}.topbar small{{color:#bdcbd8;font-size:11px;letter-spacing:1.5px;text-transform:uppercase}}
.wrap{{max-width:1840px;margin:auto;padding:32px 32px 50px}}.hero{{display:flex;justify-content:space-between;gap:32px;align-items:center}}h1{{font-size:32px;line-height:1.2;letter-spacing:-1.1px;margin:8px 0 12px;font-weight:730}}.lede{{color:var(--muted);max-width:780px;margin:0;line-height:1.65}}.live-label{{font-size:10px;font-weight:800;letter-spacing:1.2px;color:var(--accent)}}.stamp{{text-align:right;color:var(--muted);font-size:11px;white-space:nowrap}}.stamp strong{{display:block;color:var(--text);font-size:12px;margin-top:3px}}
.cards{{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr));gap:14px;margin:28px 0 20px}}.card{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:18px 21px}}.card strong{{display:block;font-size:30px;letter-spacing:-1px;font-variant-numeric:tabular-nums;line-height:1.2;margin:7px 0}}.card span{{color:var(--muted);font-size:12px}}.card-label{{font-size:10px!important;letter-spacing:1.2px;text-transform:uppercase;font-weight:750}}.card.cross-card{{background:#fff8ed;border-color:#efdab7}}.cross-card strong{{color:var(--gold)}}
.coverage,.quality{{display:flex;gap:10px;flex-wrap:wrap}}.coverage{{margin-bottom:10px}}.coverage-item{{display:flex;align-items:center;gap:9px;background:#fff;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:11px}}.quality{{font-size:11px;color:var(--muted);margin:0 0 18px}}.quality-item{{display:flex;gap:5px;padding:0 10px 0 0;border-right:1px solid var(--line)}}.quality-item strong{{color:var(--text);font-weight:650}}.freshness{{display:flex;justify-content:space-between;gap:20px;align-items:center;padding:12px 0;font-size:11px;border-top:1px solid var(--line)}}.freshness strong{{font-size:12px}}.freshness.stale strong{{color:var(--gold)}}.freshness .meta{{font-size:11px}}.quote-notice{{color:#76603b;background:#fbf5e9;border:1px solid #efe2c8;border-radius:9px;padding:11px 14px;font-size:11px;margin:8px 0 24px}}.quote-notice strong{{color:#885317}}
.section-heading{{display:flex;justify-content:space-between;align-items:baseline;gap:15px;margin:26px 0 12px}}h2{{font-size:17px;letter-spacing:-.3px;margin:0}}.section-heading span{{font-size:11px;color:var(--muted)}}.controls{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:13px}}input{{flex:1;min-width:240px;max-width:520px;background:#fff;border:1px solid var(--line);border-radius:8px;padding:10px 13px;color:var(--text)}}button{{cursor:pointer;background:#fff;border:1px solid var(--line);color:var(--muted);border-radius:8px;padding:9px 12px;font-size:11px;font-weight:650}}button.active{{color:#fff;border-color:var(--navy);background:var(--navy)}}button.cross-filter.active{{background:#9b5a06;border-color:#9b5a06}}.result-count{{margin-left:auto;color:var(--muted);font-size:11px}}
.table-shell{{overflow:auto;background:#fff;border:1px solid var(--line);border-radius:12px;box-shadow:0 3px 10px #17304c04}}table{{border-collapse:collapse;width:100%;min-width:1330px}}th{{background:#f9fbfd;text-align:left;padding:14px;font-size:10px;font-weight:750;text-transform:uppercase;letter-spacing:.8px;border-bottom:1px solid var(--line);color:var(--muted)}}td{{vertical-align:top;padding:18px 14px;border-bottom:1px solid var(--line)}}tbody tr:last-child td{{border-bottom:0}}tbody tr:hover{{background:#fafcfb}}.crossed-row{{background:#fffdf9}}.crossed-row .best-cell{{background:#fff7e8}}.fixture-cell{{min-width:210px;max-width:280px}}.fixture{{margin:8px 0;display:flex;flex-direction:column;line-height:1.3;gap:3px}}.fixture strong{{font-size:13px;font-weight:700}}.fixture>span{{font-size:10px;color:var(--muted)}}.date{{font-size:10px;color:var(--muted);font-weight:600}}.date span{{font-weight:400}}.meta,.absence{{font-size:10px;color:var(--muted);line-height:1.6;margin-top:5px}}.outcome-cell{{min-width:125px;max-width:170px}}.outcome{{font-size:15px;font-weight:750;color:var(--accent);margin-top:9px;text-transform:capitalize}}.quote-label{{font-size:10px;color:var(--muted);font-weight:650}}.best-cell{{min-width:215px;background:#f7fafc}}.best-pair,.quote-pair{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}}.best-pair span,.quote-pair span{{display:block;font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}}.best-pair strong,.quote-pair strong{{display:block;font-size:23px;letter-spacing:-.8px;line-height:1.6;font-weight:730;font-variant-numeric:tabular-nums}}.best-pair small{{display:block;color:var(--muted);font-size:9px;max-width:100px}}.spread{{margin-top:13px;color:var(--muted);font-size:10px;border-top:1px solid var(--line);padding-top:9px}}.spread strong{{float:right;color:var(--text);font-size:12px}}
.venue-cell{{min-width:205px;max-width:250px}}.venue-name{{display:none;font-size:11px;font-weight:750;margin-bottom:9px}}.quote-pair{{border:1px solid var(--line);background:#fbfcfe;border-radius:8px;margin-top:10px;padding:8px 11px;gap:8px}}.quote-pair>div+div{{border-left:1px solid var(--line);padding-left:12px}}.quote-pair strong{{font-size:21px}}.local-cross{{border-color:#ead0a4;background:#fff8ed}}.quote-meta{{color:var(--muted);font-size:9px;line-height:1.5;margin-top:6px}}.market-title{{font-size:10px;font-weight:550;margin:9px 0 5px;line-height:1.5;overflow-wrap:anywhere}}.identifiers{{color:var(--muted);font-size:9px;margin-top:6px}}summary{{cursor:pointer}}.market-id{{font:9px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere;margin-top:4px}}.open-link{{display:inline-block;margin-top:10px;font-size:10px;text-decoration:none;font-weight:700}}.open-link:hover{{text-decoration:underline}}
.badge{{display:inline-flex;border-radius:5px;padding:3px 6px;font-size:9px;font-weight:700;line-height:1.4}}.available{{background:#eaf5f1;color:#207465}}.missing{{background:#edf1f5;color:#718095}}.review{{background:#fff0d6;color:#8b5a14}}.incomplete{{background:#fdece6;color:#a14b32}}.crossed{{background:#fff0d0;color:#9b5500;border:1px solid #eace98}}.empty{{padding:48px;text-align:center;color:var(--muted);font-size:13px}}footer{{font-size:10px;line-height:1.8;color:var(--muted);margin-top:22px;max-width:1000px}}
@media(max-width:1050px){{.wrap{{padding:25px 20px}}.hero{{align-items:flex-start}}h1{{font-size:28px}}.stamp{{white-space:normal;min-width:150px}}table{{min-width:0;display:block}}thead{{display:none}}tbody{{display:grid;gap:16px;background:var(--bg)}}tr{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));background:#fff;border:1px solid var(--line);border-radius:12px;overflow:hidden}}.table-shell{{border:0;background:transparent;box-shadow:none}}td{{max-width:none!important;min-width:0!important}}.fixture-cell{{grid-column:1/-1;background:#f9fbfd}}.fixture{{flex-direction:row;align-items:center;flex-wrap:wrap;gap:8px}}.outcome-cell{{grid-column:span 1}}.best-cell{{grid-column:span 2}}.best-pair{{margin-top:8px}}.best-pair small{{max-width:none}}.venue-name{{display:block}}.venue-cell{{border-bottom:0}}}}
@media(max-width:640px){{.topbar{{padding:14px 18px}}.topbar small{{display:none}}.wrap{{padding:23px 14px 35px}}.hero{{display:block}}.stamp{{text-align:left;margin-top:14px}}.stamp strong{{display:inline;margin-left:4px}}h1{{font-size:27px}}.cards{{grid-template-columns:1fr 1fr;gap:9px;margin-top:21px}}.card{{padding:13px 15px}}.card strong{{font-size:25px}}.freshness{{display:block}}.freshness>div+div{{margin-top:7px}}.coverage-item{{flex:1;min-width:230px;justify-content:space-between}}.controls{{gap:6px}}input{{max-width:none;flex-basis:100%;min-width:0}}.result-count{{width:100%;margin-top:4px}}.section-heading{{align-items:flex-start;flex-direction:column;gap:3px}}tr{{grid-template-columns:1fr 1fr}}.outcome-cell,.best-cell{{grid-column:span 1}}.venue-cell{{grid-column:1/-1;border-bottom:1px solid var(--line)}}.quote-pair{{max-width:260px}}.venue-name{{float:right}}.best-pair{{gap:5px}}.best-pair strong{{font-size:21px}}}}
</style></head><body>
<header class="topbar"><div class="brand"><span class="brand-icon" aria-hidden="true">≋</span> Cricket / Markets</div><small>Cross-venue intelligence</small></header>
<main class="wrap">
<div class="hero"><div><div class="live-label">{badge_label}</div><h1>{heading}</h1><p class="lede">{html.escape(description)}</p></div><div class="stamp">{timestamp_label}<br><strong>{html.escape(timestamp_value)}</strong></div></div>
<div class="cards"><div class="card"><span class="card-label">Fixtures</span><strong>{fixture_count}</strong><span>confirmed across venues</span></div><div class="card"><span class="card-label">Outcomes</span><strong>{len(rows)}</strong><span>equivalent markets mapped</span></div><div class="card"><span class="card-label">Quoted outcomes</span><strong>{quoted_count}</strong><span>at least one bid or ask</span></div><div class="card cross-card"><span class="card-label">Crossed quotes</span><strong>{crossed_count}</strong><span>bid exceeds an ask</span></div></div>
<div class="coverage">{coverage_cards}</div><div class="quality">{quality_cards}</div>
<div id="freshness" class="freshness"><div><strong id="reportAge">Calculating report age…</strong><div class="meta">{freshness_description}</div></div><div class="meta">Prices in cents per outcome. Bid = sell quote · Ask = buy quote.</div></div>
<div class="quote-notice"><strong>Crossed = bid &gt; ask.</strong> Observed quotes before fees and depth; check venue settlement rules. These quotes may be stale. Crossed flags are not a guarantee of profit.</div>
<div class="section-heading"><h2>Outcome market board</h2><span>{three_count} outcomes on all 3 venues · {two_count} on 2 venues</span></div>
<div class="controls"><input id="search" type="search" aria-label="Search market board" placeholder="Search teams, competition, outcome…"><button class="filter active" data-filter="all" aria-pressed="true">All outcomes</button><button class="filter" data-filter="3" aria-pressed="false">All 3 venues</button><button class="filter" data-filter="2" aria-pressed="false">2 venues</button><button id="crossOnly" class="cross-filter" aria-pressed="false">Crossed only · {crossed_count}</button><span id="resultCount" class="result-count" aria-live="polite">{len(rows)} outcomes</span></div>
<div class="table-shell"><table><thead><tr><th scope="col">Fixture / UTC</th><th scope="col">Outcome</th><th scope="col">Best quotes</th>{venue_headers}</tr></thead><tbody id="rows">{''.join(body_rows)}</tbody></table><div id="empty" class="empty" hidden>No outcomes match these filters. Try another team or clear the crossed filter.</div></div>
<footer>Published after all critical safety checks passed. Coverage and unresolved candidates remain explicit. “Not currently open” appears only after complete collection and parsing, with no unresolved fixture candidate. Venue links open shared event pages; outcome details identify the mapped contract. Quotes compare the same mapped outcome and can arrive at different times; fees, size and settlement terms require separate assessment. Exact source records, review items, quote diagnostics and the safety audit are in the adjacent <strong>data</strong> folder.</footer>
</main><script>
const search=document.getElementById('search'),rows=[...document.querySelectorAll('#rows tr')],empty=document.getElementById('empty'),count=document.getElementById('resultCount'),crossOnly=document.getElementById('crossOnly');let venueFilter='all',crossFilter=false;
const generatedAt=new Date('{html.escape(generated_iso, quote=True)}');const ageLabel='{age_label}';const ageMinutes=Math.max(0,Math.floor((Date.now()-generatedAt.getTime())/60000));const age=document.getElementById('reportAge'),freshness=document.getElementById('freshness');if(ageMinutes<2){{age.textContent=ageLabel+' just now'}}else if(ageMinutes<60){{age.textContent=`${{ageLabel}} ${{ageMinutes}} minutes ago`}}else{{const hours=Math.floor(ageMinutes/60);age.textContent=`${{ageLabel}} ${{hours}} hour${{hours===1?'':'s'}} ago`;freshness.classList.add('stale')}}
function apply(){{const q=search.value.trim().toLowerCase();let visible=0;rows.forEach(r=>{{const okText=!q||r.dataset.search.includes(q),okVenue=venueFilter==='all'||r.dataset.venues===venueFilter,okCross=!crossFilter||r.dataset.crossed==='true';r.hidden=!(okText&&okVenue&&okCross);if(!r.hidden)visible++}});empty.hidden=visible!==0;count.textContent=`${{visible}} of ${{rows.length}} outcome${{rows.length===1?'':'s'}}`}}
search.addEventListener('input',apply);document.querySelectorAll('.filter').forEach(b=>b.addEventListener('click',()=>{{document.querySelectorAll('.filter').forEach(x=>{{x.classList.remove('active');x.setAttribute('aria-pressed','false')}});b.classList.add('active');b.setAttribute('aria-pressed','true');venueFilter=b.dataset.filter;apply()}}));crossOnly.addEventListener('click',()=>{{crossFilter=!crossFilter;crossOnly.classList.toggle('active',crossFilter);crossOnly.setAttribute('aria-pressed',String(crossFilter));apply()}});apply();
</script></body></html>'''
    path.write_text(doc, encoding="utf-8")


def write_outputs(outdir: Path, accepted: list[Any], review: list[Any],
                  coverage_complete: dict[str, bool], venues: list[str],
                  generated_at: datetime,
                  quality: Optional[dict[str, str]] = None) -> list[dict[str, str]]:
    rows = build_wide_rows(accepted, review, coverage_complete, venues)
    write_wide_csv(outdir / "market_map.csv", rows, venues)
    write_html(outdir / "CricketMatchingReport.html", rows, venues, coverage_complete, generated_at, quality)
    return rows
