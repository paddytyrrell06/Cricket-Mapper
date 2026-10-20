# Collect Kalshi and Polymarket match winners, audit contract compatibility, and publish the sports terminal.
##update: python3 run.py --snapshot-dir=output/data --refresh-quotes
# Browser terminal with reload-to-refresh: python3 server.py
from __future__ import annotations

import csv
import json
import shutil
import sys
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import audit as A
import canonical as C
import matching as M
import presentation as P
import quotes as Q
import sports as S

VENUES = ["kalshi", "polymarket_intl"]
DISCOVERY_WORKERS = 3
ENABLED_SPORTS = tuple(filter(None, next(
    (arg.split("=", 1)[1] for arg in sys.argv[1:] if arg.startswith("--sports=")),
    ",".join(S.ADAPTERS)).split(",")))
SOCCER_LEAGUES = next((tuple(filter(None, arg.split("=", 1)[1].split(",")))
                      for arg in sys.argv[1:] if arg.startswith("--soccer-leagues=")), None)
OUTPUT_DIR = Path("output")
BUILD_DIR = Path(".cricket_matching_build")
PREVIOUS_DIR = Path(".cricket_matching_previous_output")
HISTORY_DIR = Path(".cricket_matching_history")
DATA_DIR = BUILD_DIR / "data"
REPORT_NAME = "CricketMatchingReport.html"
NO_OPEN = "--no-open" in sys.argv
REFRESH_QUOTES = "--refresh-quotes" in sys.argv
SNAPSHOT_DIR = next(
    (Path(arg.split("=", 1)[1]) for arg in sys.argv[1:]
     if arg.startswith("--snapshot-dir=")),
    None,
)


def say(message: str) -> None:
    print(message, flush=True)


def prepare_build() -> None:
    if PREVIOUS_DIR.exists() and not OUTPUT_DIR.exists():
        PREVIOUS_DIR.replace(OUTPUT_DIR)
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


def raw_key(sport: str, key: str) -> str:
    """Preserve existing cricket snapshot filenames; namespace every other sport."""
    return key if sport == "cricket" else f"{sport}_{key}"


def scope_raw(raw: dict, sport: str) -> dict:
    if sport == "cricket":
        return {key: raw.get(key, []) for key in VENUES}
    prefix = sport + "_"
    return {key[len(prefix):]: items for key, items in raw.items() if key.startswith(prefix)}


def collection_metadata(saved: object) -> dict:
    meta = dict(saved) if isinstance(saved, dict) else {
        "complete": False, "method": "snapshot_without_coverage_metadata",
        "reason": "snapshot does not prove complete collection",
    }
    meta["complete"] = meta.get("complete") is True
    meta["reason"] = str(meta.get("collection_reason", str(meta.get("reason") or "").split(
        "COVERAGE_INCOMPLETE", 1)[0].rstrip("; ")))
    for field in ("parse_complete", "mapping_coverage_complete"):
        meta.pop(field, None)
    return meta


def load_snapshot(snapshot_dir: Path) -> tuple[dict, dict, list[str], dict]:
    """Read saved inputs before preparing the build, which may contain them."""
    raw: dict = {}
    status: dict = {}
    warnings: list[str] = []
    coverage: dict = {"sports": {}}
    saved_meta = {}
    meta_path = snapshot_dir / "collection_meta.json"
    if meta_path.exists():
        try:
            saved_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(saved_meta, dict):
                saved_meta = {}
        except (OSError, ValueError):
            saved_meta = {}
    sport_meta = saved_meta.get("sports") or {}
    if not isinstance(sport_meta, dict):
        sport_meta = {}
    for sport in ENABLED_SPORTS:
        saved_scope = sport_meta.get(sport, saved_meta if sport == "cricket" else {})
        if not isinstance(saved_scope, dict):
            saved_scope = {}
        coverage["sports"][sport] = {}
        # Adapters may persist additional evidence, such as team registries or
        # linked game milestones. Replay every namespaced metadata file.
        prefix = f"raw_{sport}_"
        extras = {path.stem[len(prefix):] for path in snapshot_dir.glob(prefix + "*.json")} if sport != "cricket" else set()
        for key in (*VENUES, *sorted(extras - set(VENUES))):
            name = raw_key(sport, key)
            path = snapshot_dir / f"raw_{name}.json"
            try:
                items = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                    raise ValueError("expected a list of records")
                raw[name] = items
                reason = ""
            except (OSError, ValueError) as exc:
                raw[name] = []
                reason = "snapshot missing" if isinstance(exc, FileNotFoundError) else f"snapshot invalid: {exc}"
            if key in VENUES:
                meta = collection_metadata(saved_scope.get(key))
                if reason:
                    meta.update(complete=False, reason=reason)
                meta["records"] = len(raw[name])
                coverage["sports"][sport][key] = meta
                status[name] = reason or f"snapshot replay, {len(raw[name])} records"
                if sport == "cricket":
                    coverage[key] = dict(meta)
            if reason:
                warnings.append(f"{sport} {key}: {reason}")
    return raw, status, warnings, coverage


def snapshot_collection_time(snapshot_dir: Path) -> datetime | None:
    meta_path = snapshot_dir / "snapshot_meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        value = meta.get("collected_at_utc") if isinstance(meta, dict) else None
    except (OSError, ValueError):
        value = None
    if not meta_path.exists():
        # Older snapshots recorded the source timestamp only in the text report.
        try:
            value = next((line.split(maxsplit=1)[1]
                          for line in (snapshot_dir / "run_report.txt").read_text(
                              encoding="utf-8").splitlines()
                          if line.startswith("data_fetched_utc ")), None)
        except (OSError, IndexError):
            value = None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except ValueError:
        return None


def load_saved_quotes(snapshot_dir: Path) -> tuple[list[dict] | None, list[str]]:
    path = snapshot_dir / "quotes.json"
    if not path.exists():
        return None, []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("quote snapshot must contain a list of records")
        return entries, []
    except (OSError, ValueError) as exc:
        return [], [f"quote snapshot invalid: {exc}"]


def collect(snapshot: tuple[dict, dict, list[str], dict] | None = None
            ) -> tuple[dict, dict, list[str], dict]:
    if snapshot is None and SNAPSHOT_DIR is not None:
        snapshot = load_snapshot(SNAPSHOT_DIR)
    if snapshot is not None:
        raw, status, warnings, coverage = snapshot
        for key, items in raw.items():
            (DATA_DIR / f"raw_{key}.json").write_text(
                json.dumps(items, indent=2), encoding="utf-8")
        (DATA_DIR / "collection_meta.json").write_text(
            json.dumps(coverage, indent=2), encoding="utf-8")
        return raw, status, warnings, coverage

    raw: dict = {}
    status: dict = {}
    warnings: list[str] = []
    coverage: dict = {}
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
    }
    # Discovery is a slow control-plane operation. Independent sport/venue
    # scopes overlap, while each adapter keeps its own cursor chain serial.
    def collect_cricket(venue: str):
        started = time.perf_counter()
        result = fetchers[venue]()
        items = (result[0] if isinstance(result, tuple) else result) or []
        observed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        for record in items:
            record.setdefault("_collected_at_utc", observed_at)
        return items, time.perf_counter() - started

    def collect_sport(sport: str):
        result = (S._collect_soccer(competitions=SOCCER_LEAGUES)
                  if sport == "soccer" and SOCCER_LEAGUES is not None else
                  S.ADAPTERS[sport].collect())
        observed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        for items in result[0].values():
            for record in items:
                record.setdefault("_collected_at_utc", observed_at)
        return result

    pool = ThreadPoolExecutor(max_workers=DISCOVERY_WORKERS)
    cricket_jobs = {venue: pool.submit(collect_cricket, venue) for venue in VENUES
                    if "cricket" in ENABLED_SPORTS}
    sport_jobs = {sport: pool.submit(collect_sport, sport)
                  for sport in ENABLED_SPORTS if sport != "cricket"}
    # Consume in configured order to keep saved reports deterministic. Workers
    # stamp records at receipt; waiting here must never make old prices newer.
    for venue in VENUES:
        say(f"  {venue} ...")
        try:
            items, elapsed = cricket_jobs[venue].result() if venue in cricket_jobs else ([], 0.0)
            raw[venue] = items
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
                f"{elapsed:.1f}s via "
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

    coverage["sports"] = {"cricket": {venue: dict(coverage[venue]) for venue in VENUES}}
    for sport in ENABLED_SPORTS:
        if sport == "cricket":
            continue
        adapter = S.ADAPTERS[sport]
        say(f"  {adapter.label} ...")
        try:
            sport_raw, sport_meta, sport_warnings = sport_jobs[sport].result()
            warnings.extend(sport_warnings)
        except Exception as exc:
            sport_raw = {venue: [] for venue in VENUES}
            sport_meta = {venue: {"complete": False, "reason": repr(exc), "records": 0}
                          for venue in VENUES}
            warnings.append(f"{sport} collection failed: {exc!r}")
        observed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        for key, items in sport_raw.items():
            for record in items:
                record.setdefault("_collected_at_utc", observed_at)
            raw[raw_key(sport, key)] = items
            (DATA_DIR / f"raw_{raw_key(sport, key)}.json").write_text(
                json.dumps(items, indent=2), encoding="utf-8")
        coverage["sports"][sport] = sport_meta
        for venue in VENUES:
            say(f"    {venue}: {len(sport_raw.get(venue, []))} records")
    pool.shutdown(wait=True)
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
REVIEW_COLS = MAPPING_COLS + [
    "record_kind", "venue_1_fixture_id", "venue_2_fixture_id",
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


def review_mapping_rows(market_review: list[M.MarketMatch],
                        fixture_review: list[M.FixtureMatch],
                        markets: list[C.Market]) -> list[list]:
    rows = [mapping_row(match) + ["market", match.m1.venue_fixture_id,
                                  match.m2.venue_fixture_id]
            for match in market_review]
    markets_by_fixture: dict[tuple[str, str], list[C.Market]] = {}
    for market in markets:
        markets_by_fixture.setdefault((market.venue, market.venue_fixture_id), []).append(market)

    for match in fixture_review:
        source_fields: list[str] = []
        fixture_ids: list[str] = []
        for venue, fixture in sorted(match.by_venue.items()):
            fixture_ids.append(fixture.venue_fixture_id)
            published_teams = f"{fixture.team_a} v {fixture.team_b}"
            candidates = markets_by_fixture.get((venue, fixture.venue_fixture_id), [])
            source = next((market for market in candidates
                           if market.market_type == C.MATCH_WINNER),
                          candidates[0] if candidates else None)
            event_id = str((source.event_id if source else "") or
                           fixture.extra.get("event_ticker") or "")
            title = published_teams
            if source and source.title:
                title += f" | {source.title}"
            source_fields.extend([
                venue, event_id, "", "", title, source.url if source else "",
            ])
        rows.append([
            match.canonical_fixture_id, match.key.date, match.start_time,
            match.competition, match.key.gender, match.key.fmt,
            match.team_a, match.team_b, "", "", *source_fields,
            match.status, match.confidence, match.reason, "fixture", *fixture_ids,
        ])
    return rows


def _main() -> int:
    if not ENABLED_SPORTS or set(ENABLED_SPORTS) - set(S.ADAPTERS):
        say("Choose --sports=" + ",".join(S.ADAPTERS) + " (any subset).")
        return 2
    if SOCCER_LEAGUES is not None and (not SOCCER_LEAGUES or
                                       set(SOCCER_LEAGUES) - set(S.SOCCER_COMPETITIONS)):
        say("Choose --soccer-leagues=" + ",".join(S.SOCCER_COMPETITIONS) + " (any subset).")
        return 2
    if REFRESH_QUOTES and SNAPSHOT_DIR is None:
        say("Use --refresh-quotes with --snapshot-dir=output/data to reuse discovery and refresh prices.")
        return 2
    run_started = datetime.now(timezone.utc)
    run_clock = phase_clock = time.perf_counter()
    timings = {}
    live_quotes = SNAPSHOT_DIR is None or REFRESH_QUOTES
    mode = ("saved discovery + live quotes" if REFRESH_QUOTES else
            "snapshot replay" if SNAPSHOT_DIR is not None else "LIVE")
    say(f"Sports market terminal — {mode.lower()}")
    say(f"Run started: {run_started.isoformat(timespec='seconds')}")
    snapshot = load_snapshot(SNAPSHOT_DIR) if SNAPSHOT_DIR is not None else None
    source_collected_at = (snapshot_collection_time(SNAPSHOT_DIR)
                           if SNAPSHOT_DIR is not None else None)
    saved_quotes, snapshot_quote_warnings = (load_saved_quotes(SNAPSHOT_DIR)
                                             if SNAPSHOT_DIR is not None and not REFRESH_QUOTES else (None, []))
    prepare_build()

    say("\n[1/6] " + ("Load saved venue data" if snapshot is not None
                         else "Collect Kalshi and Polymarket"))
    raw, status, warnings, coverage = collect(snapshot)
    timings["discovery_ms"] = round((time.perf_counter() - phase_clock) * 1000, 3)
    phase_clock = time.perf_counter()
    warnings.extend(snapshot_quote_warnings)
    data_fetched_at = source_collected_at or datetime.now(timezone.utc)
    source_time_known = SNAPSHOT_DIR is None or source_collected_at is not None
    (DATA_DIR / "snapshot_meta.json").write_text(json.dumps({
        "collected_at_utc": (data_fetched_at.isoformat(timespec="seconds")
                             if source_time_known else None),
    }, indent=2), encoding="utf-8")

    say("\n[2/6] Parse venue records")
    fixtures: list = []
    markets: list = []
    counts: dict = {}
    parse_complete: dict[str, bool] = {venue: True for venue in VENUES}
    parse_incomplete_reasons: dict[str, list[str]] = {venue: [] for venue in VENUES}
    for venue in VENUES:
        items = (raw.get(venue) or []) if "cricket" in ENABLED_SPORTS else []
        try:
            if venue == "kalshi":
                parsed_fixtures, parsed_markets, parse_warnings = C.parse_kalshi(
                    items, market_types={C.MATCH_WINNER})
            else:
                parsed_fixtures, parsed_markets, parse_warnings = C.parse_polymarket(
                    items, venue, market_types={C.MATCH_WINNER})
        except Exception as exc:
            parsed_fixtures, parsed_markets = [], []
            parse_warnings = [f"COVERAGE_INCOMPLETE {venue}: parse failed {exc!r}"]
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

    scope_parse = {"cricket": dict(parse_complete)}
    scope_parse_reasons = {"cricket": parse_incomplete_reasons}
    for sport in ENABLED_SPORTS:
        if sport == "cricket":
            continue
        try:
            sport_fixtures, sport_markets, sport_warnings = S.ADAPTERS[sport].parse(scope_raw(raw, sport))
        except Exception as exc:
            sport_fixtures, sport_markets = [], []
            sport_warnings = [f"COVERAGE_INCOMPLETE {sport}: parse failed {exc!r}"]
        fixtures.extend(sport_fixtures)
        markets.extend(sport_markets)
        warnings.extend(sport_warnings)
        scope_parse_reasons[sport] = {
            venue: [str(w) for w in sport_warnings if str(w).startswith("COVERAGE_INCOMPLETE")
                    and (venue in str(w) or f"{sport}:" in str(w))] for venue in VENUES}
        scope_parse[sport] = {venue: not scope_parse_reasons[sport][venue] for venue in VENUES}
        say(f"  {sport}: {len(sport_fixtures)} fixtures, {len(sport_markets)} outcomes")

    sport_coverage: dict[str, dict[str, bool]] = {}
    scoped_meta = coverage.setdefault("sports", {
        "cricket": {venue: dict(coverage.get(venue, {})) for venue in VENUES}})
    for sport in ENABLED_SPORTS:
        sport_coverage[sport] = {}
        for venue in VENUES:
            meta = scoped_meta.setdefault(sport, {}).setdefault(venue, {})
            meta["collection_reason"] = str(meta.get("collection_reason", meta.get("reason") or "")).strip()
            meta["parse_complete"] = scope_parse.get(sport, {}).get(venue, False)
            meta["mapping_coverage_complete"] = bool(meta.get("complete")) and meta["parse_complete"]
            reasons = scope_parse_reasons.get(sport, {}).get(venue, [])
            meta["reason"] = "; ".join(filter(None, [meta["collection_reason"], *dict.fromkeys(reasons)]))
            sport_coverage[sport][venue] = meta["mapping_coverage_complete"]

    coverage_complete = {venue: all(states.get(venue, False) for states in sport_coverage.values())
                         for venue in VENUES}
    for venue in VENUES:
        scopes = [scoped_meta[sport][venue] for sport in ENABLED_SPORTS]
        coverage[venue] = dict(scopes[0]) if len(scopes) == 1 else {
            "complete": all(meta.get("complete") for meta in scopes),
            "parse_complete": all(meta.get("parse_complete") for meta in scopes),
            "mapping_coverage_complete": coverage_complete[venue],
            "method": "sport_adapters",
            "records": sum(meta.get("records", 0) for meta in scopes),
            "collection_reason": "; ".join(f"{sport}: {scoped_meta[sport][venue]['collection_reason']}"
                                           for sport in ENABLED_SPORTS if scoped_meta[sport][venue]["collection_reason"]),
            "reason": "; ".join(f"{sport}: {scoped_meta[sport][venue]['reason']}"
                                for sport in ENABLED_SPORTS if scoped_meta[sport][venue]["reason"]),
        }
    (DATA_DIR / "collection_meta.json").write_text(
        json.dumps(coverage, indent=2), encoding="utf-8")
    for venue in VENUES:
        if not coverage_complete[venue]:
            reason = str(coverage.get(venue, {}).get("reason") or "unspecified").strip()
            reasons = reason.split("; ")
            preview = "; ".join(reasons[:3])
            if len(reasons) > 3:
                preview += f"; {len(reasons) - 3} more (see data/coverage_status.csv)"
            say(f"  {venue} coverage incomplete: {preview}")

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

    say("  Collect bid / ask quotes for confirmed outcomes ...")
    timings["parse_and_match_ms"] = round((time.perf_counter() - phase_clock) * 1000, 3)
    phase_clock = time.perf_counter()
    quote_markets = list({id(market): market
                          for match in market_accepted + market_review
                          for market in (match.m1, match.m2)}.values())
    quote_raw = {venue: [record for sport in ENABLED_SPORTS
                         for record in raw.get(raw_key(sport, venue), [])]
                 for venue in VENUES}
    quote_entries, quote_warnings = Q.enrich_quotes(
        quote_markets, quote_raw, live=live_quotes, saved_quotes=saved_quotes,
        fee_context=raw, refresh_fees=live_quotes,
        collected_at=(data_fetched_at.isoformat(timespec="seconds")
                      if source_time_known else ""))
    warnings.extend(quote_warnings)
    timings["quote_refresh_ms"] = round((time.perf_counter() - phase_clock) * 1000, 3)
    phase_clock = time.perf_counter()
    (DATA_DIR / "quotes.json").write_text(
        json.dumps(quote_entries, indent=2), encoding="utf-8")
    quotes_checked_at = datetime.now(timezone.utc)
    (DATA_DIR / "snapshot_meta.json").write_text(json.dumps({
        "collected_at_utc": data_fetched_at.isoformat(timespec="seconds") if source_time_known else None,
        "quotes_checked_at_utc": quotes_checked_at.isoformat(timespec="seconds"),
        "quote_mode": "live_rest" if live_quotes else "saved_replay",
        "sports": list(ENABLED_SPORTS), "venues": VENUES,
    }, indent=2), encoding="utf-8")
    two_sided_quotes = sum(market.bid is not None and market.ask is not None
                           for market in quote_markets)
    say(f"  {two_sided_quotes}/{len(quote_markets)} outcome quotes have both sides")

    say("\n[5/6] Build report data")
    write_csv(
        DATA_DIR / "market_mappings.csv",
        MAPPING_COLS,
        [mapping_row(match) for match in market_accepted],
    )
    review_rows = review_mapping_rows(market_review, fixture_review, markets)
    write_csv(
        DATA_DIR / "review_queue.csv",
        REVIEW_COLS,
        review_rows,
    )

    wide_rows = P.build_wide_rows(
        market_accepted, market_review, coverage_complete, VENUES,
        fixture_review=fixture_review, include_review=True,
        sport_coverage=sport_coverage, as_of=quotes_checked_at)
    P.write_wide_csv(DATA_DIR / "market_map.csv", wide_rows, VENUES)

    unmatched_market_rows = []
    for market in market_unmatched:
        targets = [venue for venue in VENUES if venue != market.venue]
        scope = sport_coverage.get(market.fixture_key.sport if market.fixture_key else "cricket", {})
        safe_absence = bool(targets) and all(
            scope.get(venue, False) for venue in targets)
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
        scope = sport_coverage.get(fixture.key.sport, {})
        safe_absence = bool(targets) and all(
            scope.get(venue, False) for venue in targets)
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
        ["sport", "venue", "collection_complete", "parse_complete",
         "mapping_coverage_complete", "method", "records", "reason"],
        [[
            sport, venue,
            bool(scoped_meta.get(sport, {}).get(venue, {}).get("complete")),
            scope_parse.get(sport, {}).get(venue, False),
            sport_coverage.get(sport, {}).get(venue, False),
            scoped_meta.get(sport, {}).get(venue, {}).get("method", ""),
            scoped_meta.get(sport, {}).get(venue, {}).get("records", 0),
            scoped_meta.get(sport, {}).get(venue, {}).get("reason", ""),
        ] for sport in ENABLED_SPORTS for venue in VENUES],
    )

    say("\n[6/6] Audit the result")
    audit_ok, audit_report, audit_checks = A.audit_runtime(
        fixtures, markets,
        fixture_accepted, fixture_review, fixture_unmatched,
        market_accepted, market_review, market_unmatched,
        wide_rows, coverage_complete, VENUES,
        sport_coverage=sport_coverage,
        as_of=quotes_checked_at,
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
        for check in critical_failures:
            say(f"    {check.name}: {check.detail}")

    run_finished = datetime.now(timezone.utc)
    unique_fixtures = len({
        row["canonical_fixture_id"] for row in wide_rows
    })
    report_lines = [
        f"Sports market terminal — {mode.lower()}",
        f"run_started_utc  {run_started.isoformat(timespec='seconds')}",
        "data_fetched_utc " + (data_fetched_at.isoformat(timespec="seconds")
                               if source_time_known else "unknown"),
        f"run_finished_utc {run_finished.isoformat(timespec='seconds')}",
        f"duration_seconds {(run_finished - run_started).total_seconds():.1f}",
        f"mode             {mode}",
        "",
        "VENUE COVERAGE",
    ]
    for venue in VENUES:
        records = sum(len(raw.get(raw_key(sport, venue), [])) for sport in ENABLED_SPORTS)
        fixture_count = sum(fixture.venue == venue for fixture in fixtures)
        market_count = sum(market.venue == venue for market in markets)
        state = "COMPLETE" if coverage_complete.get(venue) else "INCOMPLETE"
        report_lines.extend([
            f"  {venue:18} {state}",
            f"  {'':18} " + "; ".join(f"{sport}: {scoped_meta.get(sport, {}).get(venue, {}).get('method', 'snapshot')}"
                                        for sport in ENABLED_SPORTS),
            f"  {'':18} {records} records, {fixture_count} fixtures, "
            f"{market_count} markets",
            f"  {'':18} collection_complete={bool(coverage.get(venue, {}).get('complete'))}, "
            f"parse_complete={coverage[venue].get('parse_complete', False)}",
        ])
        if coverage.get(venue, {}).get("reason"):
            report_lines.append(f"  {'':18} {coverage[venue]['reason']}")
    report_lines.extend([
        "",
        "SPORT SCOPES",
        *[f"  {sport}: " + ", ".join(
            f"{venue} {'complete' if states.get(venue) else 'incomplete'}" for venue in VENUES)
          for sport, states in sport_coverage.items()],
        "",
        "RESULT",
        f"  displayed fixtures       {unique_fixtures}",
        f"  compatible outcome rows  {sum(row.get('comparison_status') == 'compatible' for row in wide_rows)}",
        f"  mapped outcome rows      {len(wide_rows)}",
        f"  two-sided outcome quotes {two_sided_quotes}/{len(quote_markets)}",
        f"  crossed outcome rows     {sum(row.get('crossed') == 'true' for row in wide_rows)}",
        f"  fixture links requiring review {len(fixture_review)}",
        f"  market links requiring review  {len(market_review)}",
        f"  critical safety audit    {'PASS' if audit_ok else 'FAIL'}",
        f"  advisory findings        {len(advisories)}",
    ])
    if snapshot_quote_warnings or quote_warnings:
        report_lines.extend(["", "QUOTE WARNINGS"])
        report_lines.extend(f"  {warning}" for warning in snapshot_quote_warnings + quote_warnings)
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
        "Run type": "LIVE" if live_quotes else "REPLAY",
        "Quotes checked at": quotes_checked_at.isoformat(timespec="seconds"),
        "Venue coverage": f"{complete_count}/{len(VENUES)} complete",
        "Critical safety audit": "PASS",
        "Review queue": f"{review_count} item" + ("" if review_count == 1 else "s"),
        "Sports": ", ".join(ENABLED_SPORTS),
    }
    if not source_time_known:
        quality["Source collection"] = "Unknown"
    # The audited discovery output seeds a separate incremental price path.
    # Future websocket transports resolve subscription IDs here and send only
    # normalized QuoteUpdate objects through streaming.LiveQuoteEngine.
    import streaming
    stream_engine = streaming.LiveQuoteEngine.from_matches(market_accepted + market_review)
    stream_engine.seed(quote_markets)
    instruments = Q.stream_instruments(quote_markets, quote_raw)
    (DATA_DIR / "stream_manifest.json").write_text(json.dumps({
        "version": 1, "mode": "rest_snapshot" if live_quotes else "replay",
        "discovery_reused": SNAPSHOT_DIR is not None,
        "instruments": instruments,
        "subscriptions": {
            venue: sorted({item["instrument_id"] for item in instruments if item["venue"] == venue})
            for venue in VENUES},
        "websockets_connected": False,
    }, indent=2), encoding="utf-8")
    (DATA_DIR / "live_state.json").write_text(
        json.dumps(stream_engine.snapshot(), indent=2), encoding="utf-8")
    P.write_html(
        BUILD_DIR / REPORT_NAME,
        wide_rows,
        VENUES,
        coverage_complete,
        data_fetched_at,
        quality,
    )
    timings["audit_and_render_ms"] = round((time.perf_counter() - phase_clock) * 1000, 3)
    timings["before_archive_total_ms"] = round((time.perf_counter() - run_clock) * 1000, 3)
    (DATA_DIR / "latency.json").write_text(json.dumps({
        "mode": "rest_snapshot" if live_quotes else "replay",
        "discovery_reused": SNAPSHOT_DIR is not None,
        "timings": timings, "discovery_workers": DISCOVERY_WORKERS,
        "quote_workers_per_venue": Q.WORKERS,
        "instruments": len(instruments), "comparison_pairs": len(market_accepted + market_review),
    }, indent=2), encoding="utf-8")
    snapshot_path = archive_audited_snapshot(data_fetched_at)
    publish_build()

    report_path = OUTPUT_DIR / REPORT_NAME
    say(f"\nSUCCESS — audited {mode.lower()} report published")
    say(f"  {len(wide_rows)} mapped outcome rows across {unique_fixtures} fixtures")
    if advisories:
        say(f"  {len(advisories)} advisory finding(s); see output/data/audit_report.txt")
    say(f"  Open: {report_path.resolve()}")
    say(f"  Auditable snapshot: {snapshot_path.resolve()}")
    if NO_OPEN:
        say("  Browser opening skipped (--no-open). An open updated report follows new publications automatically.")
    else:
        try:
            opened = webbrowser.open(report_path.resolve().as_uri())
            say("  Opened in your default browser." if opened
                else "  Open the HTML file in your browser.")
        except Exception:
            say("  Open the HTML file in your browser.")
    return 0


def main() -> int:
    # Browser refreshes and a separate CLI run must never share a mutable build.
    from build_lock import BuildBusy, BuildLock
    try:
        with BuildLock(BUILD_DIR.parent / ".market_terminal.lock"):
            return _main()
    except BuildBusy as exc:
        say(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
