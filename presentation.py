# This file builds the clean CSV outputs and self-contained HTML report from the confirmed cross-venue market matches.

from __future__ import annotations

import csv
import html
from collections import defaultdict
from datetime import datetime
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
]


def _market_dict(market: Any) -> dict[str, str]:
    return {
        "status": "Available",
        "event_id": str(market.event_id or ""),
        "market_id": str(market.market_id or ""),
        "slug": str(market.slug or ""),
        "title": str(market.title or ""),
        "url": str(market.url or ""),
    }


def build_wide_rows(accepted: list[Any], review: list[Any],
                    coverage_complete: dict[str, bool],
                    venues: list[str]) -> list[dict[str, str]]:
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    review_venues: dict[tuple[str, str, str], set[str]] = defaultdict(set)

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
            elif venue in review_venues.get(key, set()):
                status = "Needs review"
            elif coverage_complete.get(venue, False):
                status = "Not currently open"
            else:
                status = "Coverage incomplete"
            row[f"{prefix}_status"] = status
            for field in ("event_id", "market_id", "slug", "title", "url"):
                row[f"{prefix}_{field}"] = market.get(field, "") if market else ""
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
    content = [_status_badge(status)]
    title = row.get(f"{venue}_title", "")
    market_id = row.get(f"{venue}_market_id", "")
    slug = row.get(f"{venue}_slug", "")
    url = row.get(f"{venue}_url", "")
    if title:
        content.append(f'<div class="market-title">{html.escape(title)}</div>')
    identifier = market_id or slug
    if identifier:
        content.append(
            '<div class="id-label">Venue market / outcome ID</div>'
            f'<div class="market-id" title="{html.escape(identifier)}">'
            f'{html.escape(identifier)}</div>')
    if url:
        content.append(
            f'<a class="open-link" href="{html.escape(url, quote=True)}" '
            f'target="_blank" rel="noopener">Open shared event ↗</a>'
            '<div class="event-note">The venue opens the shared fixture page; '
            'the mapped outcome is identified by this row and ID.</div>')
    elif status == "Not currently open":
        content.append('<div class="absence">Full venue collection checked.</div>')
    elif status == "Coverage incomplete":
        content.append('<div class="absence">Absence not asserted.</div>')
    return "".join(content)


def write_html(path: Path, rows: list[dict[str, str]], venues: list[str],
               coverage_complete: dict[str, bool], generated_at: datetime,
               quality: Optional[dict[str, str]] = None) -> None:
    fixture_count = len({r["canonical_fixture_id"] for r in rows})
    three_count = sum(1 for r in rows if r["venues_available"] == "3")
    two_count = sum(1 for r in rows if r["venues_available"] == "2")
    quality = quality or {}
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
        body_rows.append(f'''<tr data-search="{html.escape(searchable, quote=True)}"
            data-venues="{row['venues_available']}">
          <td class="date"><strong>{html.escape(row['fixture_date'])}</strong>
            <span>{html.escape(row['start_time_utc'] or 'Time unavailable')} UTC</span></td>
          <td><div class="fixture"><strong>{html.escape(row['team_a'])}</strong>
            <span>vs</span><strong>{html.escape(row['team_b'])}</strong></div>
            <div class="meta">{html.escape(row['competition'])} · {html.escape(row['gender'])}/{html.escape(row['format'])}</div></td>
          <td><strong>{html.escape(row['canonical_market_type'].replace('_', ' ').title())}</strong>
            <div class="outcome">{html.escape(row['canonical_outcome'])}</div></td>
          <td>{_venue_cell(row, 'kalshi')}</td>
          <td>{_venue_cell(row, 'polymarket_intl')}</td>
          <td>{_venue_cell(row, 'polymarket_us')}</td>
        </tr>''')

    generated_iso = generated_at.isoformat()
    doc = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cricket Exchange Matching · Live Report</title>
<style>
:root{{--bg:#07111f;--panel:#0d1b2a;--panel2:#12243a;--text:#edf4ff;--muted:#91a4bd;--line:#233a55;--accent:#5de1c2;--gold:#f1c75b;--danger:#ff7d8a}}
*{{box-sizing:border-box}} body{{margin:0;background:linear-gradient(145deg,#06101d,#0a1830 55%,#07111f);color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.wrap{{max-width:1680px;margin:auto;padding:38px 28px 60px}} h1{{font-size:34px;margin:0 0 7px;letter-spacing:-.8px}} .lede{{color:var(--muted);font-size:16px;margin:0}}
.hero{{display:flex;justify-content:space-between;gap:25px;align-items:end;margin-bottom:25px}} .stamp{{color:var(--muted);text-align:right}} .stamp strong{{color:var(--text)}} .live-label{{display:inline-flex;margin-bottom:9px;padding:4px 9px;border-radius:999px;background:rgba(93,225,194,.14);border:1px solid rgba(93,225,194,.35);color:var(--accent);font-size:11px;font-weight:800;letter-spacing:.8px}}
.cards{{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:12px;margin:22px 0}} .card{{background:rgba(13,27,42,.92);border:1px solid var(--line);border-radius:14px;padding:17px}} .card strong{{display:block;font-size:27px}} .card span{{color:var(--muted)}}
.coverage{{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 14px}} .coverage-item{{display:flex;align-items:center;gap:9px;background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:8px 11px}}
.quality{{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 20px}} .quality-item{{display:flex;align-items:center;gap:8px;background:rgba(93,225,194,.08);border:1px solid rgba(93,225,194,.28);border-radius:10px;padding:9px 12px}} .quality-item span{{color:var(--muted)}} .quality-item strong{{color:var(--accent)}}
.controls{{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 16px}} input,button{{border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:10px;padding:11px 13px}} input{{min-width:320px;flex:1}} button{{cursor:pointer}} button.active{{border-color:var(--accent);color:var(--accent)}}
.table-shell{{overflow:auto;border:1px solid var(--line);border-radius:15px;background:rgba(13,27,42,.9);box-shadow:0 22px 70px rgba(0,0,0,.28)}} table{{border-collapse:collapse;width:100%;min-width:1480px}} th{{position:sticky;top:0;background:#102238;z-index:2;text-align:left;color:#b9c8da;padding:13px;border-bottom:1px solid var(--line);font-size:12px;text-transform:uppercase;letter-spacing:.55px}} td{{vertical-align:top;padding:14px;border-bottom:1px solid #1b3047}} tbody tr:hover{{background:#10243a}} .date span,.meta,.absence{{display:block;color:var(--muted);font-size:12px;margin-top:4px}} .fixture{{display:flex;gap:8px;align-items:center;white-space:nowrap}} .fixture span{{color:var(--muted)}} .outcome{{margin-top:5px;color:var(--accent);font-weight:650;text-transform:capitalize}}
.badge{{display:inline-flex;border-radius:999px;padding:3px 8px;font-size:11px;font-weight:750;letter-spacing:.2px}} .available{{background:rgba(93,225,194,.14);color:var(--accent);border:1px solid rgba(93,225,194,.35)}} .missing{{background:rgba(145,164,189,.1);color:#b8c5d4;border:1px solid #344c66}} .review{{background:rgba(241,199,91,.12);color:var(--gold);border:1px solid rgba(241,199,91,.35)}} .incomplete{{background:rgba(255,125,138,.12);color:var(--danger);border:1px solid rgba(255,125,138,.35)}}
.market-title{{font-weight:650;margin:8px 0 4px;max-width:330px}} .id-label{{margin-top:7px;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.45px}} .market-id{{max-width:300px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted);font:11px ui-monospace,SFMono-Regular,Menlo,monospace}} .open-link{{display:inline-block;margin-top:8px;color:var(--accent);text-decoration:none;font-weight:700}} .open-link:hover{{text-decoration:underline}} .event-note{{max-width:310px;margin-top:4px;color:var(--muted);font-size:11px}} .freshness{{display:flex;justify-content:space-between;gap:16px;align-items:center;margin:0 0 18px;padding:12px 14px;border:1px solid rgba(93,225,194,.28);border-radius:12px;background:rgba(93,225,194,.07)}} .freshness.stale{{border-color:rgba(241,199,91,.4);background:rgba(241,199,91,.08)}} .freshness strong{{color:var(--accent)}} .freshness.stale strong{{color:var(--gold)}} .empty{{padding:40px;text-align:center;color:var(--muted)}}
footer{{color:var(--muted);margin-top:18px;font-size:12px}} @media(max-width:800px){{.hero{{display:block}}.stamp{{text-align:left;margin-top:10px}}.cards{{grid-template-columns:1fr 1fr}}.wrap{{padding:24px 14px}}}}
</style></head><body><div class="wrap">
<div class="hero"><div><div class="live-label">LIVE API REPORT</div><h1>Live Cricket Market Map</h1><p class="lede">Confirmed equivalent outcomes across Kalshi, Polymarket International and Polymarket US. This file is regenerated from live venue APIs on every successful run.</p></div><div class="stamp">Live collection completed<br><strong>{html.escape(generated_at.strftime('%d %b %Y, %H:%M:%S UTC'))}</strong></div></div>
<div id="freshness" class="freshness"><div><strong id="reportAge">Calculating report age…</strong><div class="meta">This HTML is a point-in-time live snapshot. Run <code>python3 run.py</code> again to refresh it.</div></div><div class="meta">Venue links open shared event pages; outcome identity is shown in each row.</div></div>
<div class="cards"><div class="card"><strong>{fixture_count}</strong><span>cross-venue fixtures</span></div><div class="card"><strong>{len(rows)}</strong><span>mapped outcome rows</span></div><div class="card"><strong>{three_count}</strong><span>listed on all 3 venues</span></div><div class="card"><strong>{two_count}</strong><span>listed on exactly 2</span></div></div>
<div class="coverage">{coverage_cards}</div><div class="quality">{quality_cards}</div>
<div class="controls"><input id="search" placeholder="Search teams, competition, outcome or market ID…"><button class="filter active" data-filter="all">All rows</button><button class="filter" data-filter="3">All 3 venues</button><button class="filter" data-filter="2">Exactly 2 venues</button></div>
<div class="table-shell"><table><thead><tr><th>Date / UTC</th><th>Fixture</th><th>Mapped market</th><th>Kalshi</th><th>Polymarket International</th><th>Polymarket US</th></tr></thead><tbody id="rows">{''.join(body_rows)}</tbody></table><div id="empty" class="empty" hidden>No rows match the current filters.</div></div>
<footer>This file was published only after all critical safety checks passed. Incomplete venue coverage and unresolved candidates are shown explicitly and are never forced into accepted mappings. “Not currently open” appears only when that venue's collection and parsing completed successfully. Venue links intentionally open shared event pages; the specific mapped outcome is represented by the row and venue ID. Exact source records, review items, and audit diagnostics are stored in the adjacent <strong>data</strong> folder.</footer>
</div><script>
const search=document.getElementById('search'), rows=[...document.querySelectorAll('tbody tr')], empty=document.getElementById('empty');let venueFilter='all';
const generatedAt=new Date('{html.escape(generated_iso, quote=True)}');const ageMinutes=Math.max(0,Math.floor((Date.now()-generatedAt.getTime())/60000));const age=document.getElementById('reportAge'),freshness=document.getElementById('freshness');if(ageMinutes<2){{age.textContent='Generated just now'}}else if(ageMinutes<60){{age.textContent=`Generated ${{ageMinutes}} minutes ago`}}else{{const hours=Math.floor(ageMinutes/60);age.textContent=`Generated ${{hours}} hour${{hours===1?'':'s'}} ago`;freshness.classList.add('stale')}}
function apply(){{const q=search.value.trim().toLowerCase();let visible=0;rows.forEach(r=>{{const okText=!q||r.dataset.search.includes(q),okVenue=venueFilter==='all'||r.dataset.venues===venueFilter;r.hidden=!(okText&&okVenue);if(!r.hidden)visible++}});empty.hidden=visible!==0}}
search.addEventListener('input',apply);document.querySelectorAll('.filter').forEach(b=>b.addEventListener('click',()=>{{document.querySelectorAll('.filter').forEach(x=>x.classList.remove('active'));b.classList.add('active');venueFilter=b.dataset.filter;apply()}}));
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
