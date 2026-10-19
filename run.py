# Run this file to collect the latest cricket markets from all three venues, match equivalent outcomes, audit the result, and create the browser report.

from __future__ import annotations

import csv
import json
import shutil
import sys
import time
import traceback
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import audit as A
import canonical as C
import matching as M
import presentation as P

VENUES = ["kalshi", "polymarket_intl", "polymarket_us"]
OUTPUT_DIR = Path("output")
BUILD_DIR = Path(".cricket_matching_build")
PREVIOUS_DIR = Path(".cricket_matching_previous_output")
HISTORY_DIR = Path(".cricket_matching_history")
DATA_DIR = BUILD_DIR / "data"
REPORT_NAME = "CricketMatchingReport.html"
NO_OPEN = "--no-open" in sys.argv
SNAPSHOT_DIR = next(
    (Path(arg.split("=", 1)[1]) for arg in sys.argv[1:]
     if arg.startswith("--snapshot-dir=")),
    None,
)


def say(message: str) -> None:
    print(message, flush=True)


def prepare_build() -> None:
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    if PREVIOUS_DIR.exists():
        shutil.rmtree(PREVIOUS_DIR)
    DATA_DIR.mkdir(parents=True)


def archive_audited_snapshot(collected_at: datetime) -> Path:
    HISTORY_DIR.mkdir(exist_ok=True)
    stem = collected_at.strftime("%Y%m%dT%H%M%SZ")
    destination = HISTORY_DIR / stem
    suffix = 1
    while destination.exists():
        destination = HISTORY_DIR / f"{stem}-{suffix}"
        suffix += 1
    shutil.copytree(DATA_DIR, destination)
    return destination


def publish_build() -> None:
    if PREVIOUS_DIR.exists():
        shutil.rmtree(PREVIOUS_DIR)
    if OUTPUT_DIR.exists():
        OUTPUT_DIR.replace(PREVIOUS_DIR)
    try:
        BUILD_DIR.replace(OUTPUT_DIR)
    except Exception:
        if not OUTPUT_DIR.exists() and PREVIOUS_DIR.exists():
            PREVIOUS_DIR.replace(OUTPUT_DIR)
        raise
    if PREVIOUS_DIR.exists():
        shutil.rmtree(PREVIOUS_DIR)


def write_csv(path: Path, columns: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)


def collect() -> tuple[dict, dict, list[str], dict]:
    raw: dict = {}
    status: dict = {}
    warnings: list[str] = []
    coverage: dict = {}


    if SNAPSHOT_DIR is not None:
        meta_path = SNAPSHOT_DIR / "collection_meta.json"
        saved_meta = {}
        if meta_path.exists():
            try:
                saved_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved_meta = {}
        for venue in VENUES:
            source = SNAPSHOT_DIR / f"raw_{venue}.json"
            if source.exists():
                raw[venue] = json.loads(source.read_text(encoding="utf-8"))
                status[venue] = f"snapshot replay, {len(raw[venue])} records"
            else:
                raw[venue] = []
                status[venue] = "snapshot missing"
                warnings.append(f"{venue}: snapshot missing")
            coverage[venue] = saved_meta.get(venue) or {
                "complete": False,
                "method": "snapshot_without_coverage_metadata",
                "reason": "snapshot does not prove complete collection",
                "records": len(raw[venue]),
            }
            coverage[venue]["records"] = len(raw[venue])
            (DATA_DIR / f"raw_{venue}.json").write_text(
                json.dumps(raw[venue], indent=2), encoding="utf-8")
        (DATA_DIR / "collection_meta.json").write_text(
            json.dumps(coverage, indent=2), encoding="utf-8")
        return raw, status, warnings, coverage

    try:
        import collectors
        collectors.COLLECTION_META.clear()
    except Exception as exc:
        for venue in VENUES:
            raw[venue] = []
            status[venue] = f"collector import failed: {exc!r}"
            coverage[venue] = {
                "complete": False,
                "method": "failed",
                "reason": repr(exc),
                "records": 0,
            }
        return raw, status, [f"collector import failed: {exc!r}"], coverage

    fetchers = {
        "kalshi": collectors.fetch_kalshi,
        "polymarket_intl": collectors.fetch_polymarket_intl,
        "polymarket_us": collectors.fetch_polymarket_us,
    }
    for venue in VENUES:
        say(f"  {venue} ...")
        started = time.time()
        try:
            result = fetchers[venue]()
            items = result[0] if isinstance(result, tuple) else result
            raw[venue] = items or []
            coverage[venue] = dict(collectors.COLLECTION_META.get(venue) or {
                "complete": False,
                "method": "collector_without_coverage_metadata",
                "reason": "collector did not report completeness",
                "records": len(raw[venue]),
            })
            coverage[venue]["records"] = len(raw[venue])
            flag = "COMPLETE" if coverage[venue].get("complete") else "INCOMPLETE"
            status[venue] = (
                f"{flag}, {len(raw[venue])} records in "
                f"{time.time() - started:.1f}s via "
                f"{coverage[venue].get('method', 'unknown')}"
            )
        except Exception as exc:
            raw[venue] = []
            status[venue] = f"FAILED: {exc!r}"
            coverage[venue] = {
                "complete": False,
                "method": "failed",
                "reason": repr(exc),
                "records": 0,
            }
            warnings.append(f"{venue}: {exc!r}")
            traceback.print_exc(limit=1)
        say(f"    {status[venue]}")
        (DATA_DIR / f"raw_{venue}.json").write_text(
            json.dumps(raw[venue], indent=2), encoding="utf-8")

    (DATA_DIR / "collection_meta.json").write_text(
        json.dumps(coverage, indent=2), encoding="utf-8")
    return raw, status, warnings, coverage


MAPPING_COLS = [
    "canonical_fixture_id", "fixture_date", "start_time", "competition",
    "gender", "format", "team_a", "team_b", "canonical_market_type",
    "canonical_outcome", "venue_1", "venue_1_event_id", "venue_1_market_id",
    "venue_1_market_slug", "venue_1_title", "venue_1_url", "venue_2",
    "venue_2_event_id", "venue_2_market_id", "venue_2_market_slug",
    "venue_2_title", "venue_2_url", "match_status", "match_confidence",
    "match_reason",
]


def mapping_row(match: M.MarketMatch) -> list:
    fixture = match.fixture
    return [
        fixture.canonical_fixture_id, fixture.key.date, fixture.start_time,
        fixture.competition, fixture.key.gender, fixture.key.fmt,
        fixture.team_a, fixture.team_b, match.market_type, match.outcome,
        match.venue_1, match.m1.event_id, match.m1.market_id, match.m1.slug,
        match.m1.title, match.m1.url,
        match.venue_2, match.m2.event_id, match.m2.market_id, match.m2.slug,
        match.m2.title, match.m2.url,
        match.status, match.confidence, match.reason,
    ]


def main() -> int:
    run_started = datetime.now(timezone.utc)
    mode = "snapshot replay" if SNAPSHOT_DIR is not None else "LIVE"
    say("Cricket exchange matching — live run")
    say(f"Run started: {run_started.isoformat(timespec='seconds')}")
    prepare_build()

    say("\n[1/6] Collect all three venues")
    raw, status, warnings, coverage = collect()
    data_fetched_at = datetime.now(timezone.utc)

    say("\n[2/6] Parse venue records")
    fixtures: list = []
    markets: list = []
    counts: dict = {}
    parse_complete: dict[str, bool] = {venue: True for venue in VENUES}
    parse_incomplete_reasons: dict[str, list[str]] = {venue: [] for venue in VENUES}
    for venue in VENUES:
        items = raw.get(venue) or []
        try:
            if venue == "kalshi":
                parsed_fixtures, parsed_markets, parse_warnings = C.parse_kalshi(items)
            elif venue == "polymarket_us":
                parsed_fixtures, parsed_markets, parse_warnings = C.parse_polymarket_us(items)
            else:
                parsed_fixtures, parsed_markets, parse_warnings = C.parse_polymarket(
                    items, venue)
        except Exception as exc:
            parsed_fixtures, parsed_markets, parse_warnings = [], [], []
            warnings.append(f"{venue}: parse failed {exc!r}")
            parse_complete[venue] = False
        coverage_warnings = [
            str(w) for w in parse_warnings
            if str(w).startswith("COVERAGE_INCOMPLETE")
        ]
        if coverage_warnings:
            parse_complete[venue] = False
            parse_incomplete_reasons[venue].extend(coverage_warnings)
        fixtures.extend(parsed_fixtures)
        markets.extend(parsed_markets)
        warnings.extend(parse_warnings[:100])
        counts[venue] = (len(items), len(parsed_fixtures), len(parsed_markets))
        say(
            f"  {venue}: {len(items)} source records -> "
            f"{len(parsed_fixtures)} fixtures, {len(parsed_markets)} markets"
        )

    coverage_complete: dict[str, bool] = {}
    for venue in VENUES:
        coverage_complete[venue] = (
            bool(coverage.get(venue, {}).get("complete"))
            and parse_complete[venue]
        )
        coverage.setdefault(venue, {})["parse_complete"] = parse_complete[venue]
        coverage[venue]["mapping_coverage_complete"] = coverage_complete[venue]
        if parse_incomplete_reasons[venue]:
            existing_reason = str(coverage[venue].get("reason") or "").strip()
            parse_reason = "; ".join(dict.fromkeys(parse_incomplete_reasons[venue]))
            coverage[venue]["reason"] = "; ".join(
                part for part in (existing_reason, parse_reason) if part)
    (DATA_DIR / "collection_meta.json").write_text(
        json.dumps(coverage, indent=2), encoding="utf-8")
    for venue in VENUES:
        if not coverage_complete[venue]:
            reason = str(coverage.get(venue, {}).get("reason") or "unspecified").strip()
            say(f"  {venue} coverage incomplete: {reason}")

    say("\n[3/6] Match equivalent fixtures")
    fixture_accepted, fixture_review, fixture_unmatched = M.match_fixtures(fixtures)
    say(
        f"  {len(fixture_accepted)} accepted venue-pair links, "
        f"{len(fixture_review)} review, {len(fixture_unmatched)} single-venue"
    )

    say("\n[4/6] Match equivalent markets")
    market_accepted, market_review, market_unmatched = M.match_markets(
        fixture_accepted + fixture_review, markets)
    say(
        f"  {len(market_accepted)} accepted venue-pair links, "
        f"{len(market_review)} review, {len(market_unmatched)} unmatched"
    )

    say("\n[5/6] Build report data")
    write_csv(
        DATA_DIR / "market_mappings.csv",
        MAPPING_COLS,
        [mapping_row(match) for match in market_accepted],
    )
    review_rows = [mapping_row(match) + ["market"] for match in market_review]
    for fixture_match in fixture_review:
        review_rows.append([
            fixture_match.canonical_fixture_id,
            fixture_match.key.date,
            fixture_match.start_time,
            fixture_match.competition,
            fixture_match.key.gender,
            fixture_match.key.fmt,
            fixture_match.team_a,
            fixture_match.team_b,
            "", "", *[""] * 12,
            fixture_match.status,
            fixture_match.confidence,
            fixture_match.reason,
            "fixture",
        ])
    write_csv(
        DATA_DIR / "review_queue.csv",
        MAPPING_COLS + ["record_kind"],
        review_rows,
    )

    wide_rows = P.build_wide_rows(
        market_accepted, market_review, coverage_complete, VENUES)
    P.write_wide_csv(DATA_DIR / "market_map.csv", wide_rows, VENUES)

    unmatched_market_rows = []
    for market in market_unmatched:
        targets = [venue for venue in VENUES if venue != market.venue]
        safe_absence = bool(targets) and all(
            coverage_complete.get(venue, False) for venue in targets)
        absence = (
            "no_counterpart_found_in_complete_collections"
            if safe_absence
            else "absence_not_asserted_incomplete_collection"
        )
        unmatched_market_rows.append([
            market.venue, market.venue_fixture_id, market.market_type,
            market.outcome, market.market_id, market.slug, market.title,
            market.url, absence, market.note,
        ])
    write_csv(
        DATA_DIR / "unmatched_markets.csv",
        ["venue", "venue_fixture_id", "canonical_market_type",
         "canonical_outcome", "market_id", "slug", "title", "url",
         "absence_status", "note"],
        unmatched_market_rows,
    )

    unmatched_fixture_rows = []
    for fixture in fixture_unmatched:
        targets = [venue for venue in VENUES if venue != fixture.venue]
        safe_absence = bool(targets) and all(
            coverage_complete.get(venue, False) for venue in targets)
        absence = (
            "no_counterpart_found_in_complete_collections"
            if safe_absence
            else "absence_not_asserted_incomplete_collection"
        )
        unmatched_fixture_rows.append([
            fixture.venue, fixture.venue_fixture_id, fixture.key.date,
            fixture.key.gender, fixture.key.fmt, fixture.competition,
            fixture.team_a, fixture.team_b, fixture.start_time,
            absence, fixture.note,
        ])
    write_csv(
        DATA_DIR / "unmatched_fixtures.csv",
        ["venue", "venue_fixture_id", "date", "gender", "format",
         "competition", "team_a", "team_b", "start_time",
         "absence_status", "note"],
        unmatched_fixture_rows,
    )
    write_csv(
        DATA_DIR / "coverage_status.csv",
        ["venue", "collection_complete", "parse_complete",
         "mapping_coverage_complete", "method", "records", "reason"],
        [[
            venue,
            bool(coverage.get(venue, {}).get("complete")),
            parse_complete.get(venue, False),
            coverage_complete.get(venue, False),
            coverage.get(venue, {}).get("method", ""),
            coverage.get(venue, {}).get("records", 0),
            coverage.get(venue, {}).get("reason", ""),
        ] for venue in VENUES],
    )

    say("\n[6/6] Audit the newly collected live result")
    audit_ok, audit_report, audit_checks = A.audit_runtime(
        fixtures, markets,
        fixture_accepted, fixture_review, fixture_unmatched,
        market_accepted, market_review, market_unmatched,
        wide_rows, coverage_complete, VENUES,
    )
    A.write_report(DATA_DIR / "audit_report.txt", audit_report)
    critical_failures = [
        check for check in audit_checks
        if not check.passed and check.severity == "critical"
    ]
    advisories = [
        check for check in audit_checks
        if not check.passed and check.severity == "advisory"
    ]
    if audit_ok:
        suffix = (f"; {len(advisories)} advisory" if len(advisories) == 1
                  else f"; {len(advisories)} advisories")
        if not advisories:
            suffix = "; no advisories"
        say(f"  PASS: all critical checks passed{suffix}")
    else:
        say(f"  FAIL: {len(critical_failures)} critical check(s) failed")

    run_finished = datetime.now(timezone.utc)
    unique_fixtures = len({
        match.fixture.canonical_fixture_id for match in market_accepted
    })
    report_lines = [
        "Cricket exchange matching — live run",
        f"run_started_utc  {run_started.isoformat(timespec='seconds')}",
        f"data_fetched_utc {data_fetched_at.isoformat(timespec='seconds')}",
        f"run_finished_utc {run_finished.isoformat(timespec='seconds')}",
        f"duration_seconds {(run_finished - run_started).total_seconds():.1f}",
        f"mode             {mode}",
        "",
        "VENUE COVERAGE",
    ]
    for venue in VENUES:
        records, fixture_count, market_count = counts.get(venue, (0, 0, 0))
        state = "COMPLETE" if coverage_complete.get(venue) else "INCOMPLETE"
        report_lines.extend([
            f"  {venue:18} {state}",
            f"  {'':18} {status.get(venue, 'not run')}",
            f"  {'':18} {records} records, {fixture_count} fixtures, "
            f"{market_count} markets",
        ])
    report_lines.extend([
        "",
        "RESULT",
        f"  confirmed fixtures       {unique_fixtures}",
        f"  mapped outcome rows      {len(wide_rows)}",
        f"  fixture links requiring review {len(fixture_review)}",
        f"  market links requiring review  {len(market_review)}",
        f"  critical safety audit    {'PASS' if audit_ok else 'FAIL'}",
        f"  advisory findings        {len(advisories)}",
    ])
    if warnings:
        report_lines.extend(["", f"WARNINGS ({len(warnings)})"])
        report_lines.extend(f"  {warning}" for warning in warnings[:60])
    (DATA_DIR / "run_report.txt").write_text(
        "\n".join(report_lines), encoding="utf-8")

    if not audit_ok:
        say("\nRUN NOT PUBLISHED: a critical safety check failed.")
        say(f"Inspect {DATA_DIR / 'audit_report.txt'}")
        if OUTPUT_DIR.exists():
            say(f"The previous report remains unchanged at {OUTPUT_DIR / REPORT_NAME}")
        return 4

    complete_count = sum(bool(coverage_complete.get(venue)) for venue in VENUES)
    review_count = len(fixture_review) + len(market_review)
    quality = {
        "Run type": "LIVE" if SNAPSHOT_DIR is None else "REPLAY",
        "Venue coverage": f"{complete_count}/{len(VENUES)} complete",
        "Critical safety audit": "PASS",
        "Review queue": f"{review_count} item" + ("" if review_count == 1 else "s"),
    }
    P.write_html(
        BUILD_DIR / REPORT_NAME,
        wide_rows,
        VENUES,
        coverage_complete,
        data_fetched_at,
        quality,
    )
    snapshot_path = archive_audited_snapshot(data_fetched_at)
    publish_build()

    report_path = OUTPUT_DIR / REPORT_NAME
    say("\nSUCCESS — latest audited live report published")
    say(f"  {len(wide_rows)} mapped outcome rows across {unique_fixtures} fixtures")
    if advisories:
        say(f"  {len(advisories)} advisory finding(s); see output/data/audit_report.txt")
    say(f"  Open: {report_path.resolve()}")
    say(f"  Auditable snapshot: {snapshot_path.resolve()}")
    if not NO_OPEN:
        try:
            opened = webbrowser.open(report_path.resolve().as_uri())
            say("  Opened in your default browser." if opened
                else "  Open the HTML file in your browser.")
        except Exception:
            say("  Open the HTML file in your browser.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
