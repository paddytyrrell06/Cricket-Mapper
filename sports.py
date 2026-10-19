"""Sport adapters using native game discovery and exact league-scoped identities.

Add a collector and parser to ADAPTERS to extend the terminal. Collectors return
original venue records plus coverage; parsers retain settlement evidence and use
the shared Fixture/Market model. No adapter may accept a fuzzy team identity.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import canonical as C
import collectors as K


GAME_SLUG = re.compile(r"^nba-(?P<a>[a-z0-9]+)-(?P<b>[a-z0-9]+)-(?P<date>\d{4}-\d{2}-\d{2})$")
NBA_SERIES = "KXNBAGAME"
NBA_TERMS = "https://assets.kalshi.com/contract_terms/BASKETBALLGAMEWIN.pdf"
NBA_DOCUMENT = {"url": NBA_TERMS,
                "text": "regulation time and any officially designated overtime periods"}
TEAM_CONFIG = json.loads((Path(__file__).parent / "config" / "nba_teams.json").read_text())


class TeamRegistry:
    """Resolve only an exact code and published alias in a verified league."""

    def __init__(self, metadata: list[dict] | None = None):
        self.teams = {row["code"]: row for row in TEAM_CONFIG["teams"]}
        self.aliases = {code: {C.norm_team(name) for name in [row["canonical"], *row["aliases"]]}
                        for code, row in self.teams.items()}
        for row in metadata or []:
            if not isinstance(row, dict) or str(row.get("league") or "").lower() != "nba":
                continue
            code = str(row.get("abbreviation") or "").upper()
            if code in self.teams and C.norm_team(row.get("name") or "") in self.aliases[code]:
                self.aliases[code].update(name for field in ("name", "alias")
                                          if (name := C.norm_team(row.get(field) or "")))

    def resolve(self, published: str, code: str, *, league: str = "nba") -> str:
        code = str(code).upper()
        if league.lower() != "nba" or code not in self.teams:
            return ""
        return self.teams[code]["canonical"] if C.norm_team(published) in self.aliases[code] else ""

    def evidence(self, published: str, code: str) -> dict:
        return {"rule_id": f"nba-team-registry:{code.lower()}", "published_name": published,
                "canonical_name": self.resolve(published, code), "competition": "NBA",
                "evidence": "Exact NBA team code and published alias; " + TEAM_CONFIG["source"]}


def _coverage(complete: bool, method: str, records: int, reasons: list[str]) -> dict:
    return {"complete": complete, "method": method, "records": records,
            "reason": "; ".join(dict.fromkeys(reasons))}


def _collect_kalshi() -> tuple[list[dict], dict, list[str], list[dict]]:
    records, reasons, series_meta = [], [], []
    status, body = K.http.get(K.KALSHI + "/series/" + NBA_SERIES, quiet=True,
                              note="NBA series metadata")
    if status == 200 and isinstance(body, dict) and isinstance(body.get("series"), dict):
        series_meta = [body["series"]]
    else:
        reasons.append("NBA series rule metadata unavailable")
    cursor, seen_cursors, seen_events, seen_markets = "", set(), set(), set()
    complete = False
    for _ in range(K.PAGE_GUARD):
        params = {"series_ticker": NBA_SERIES, "status": "open",
                  "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        status, body = K.http.get(K.KALSHI + "/events", params, quiet=True,
                                  note="NBA full-game winner events")
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("events"), list):
            reasons.append("NBA events request failed or returned invalid data")
            break
        events = body["events"]
        added = 0
        for event in events:
            if not isinstance(event, dict):
                reasons.append("NBA event record malformed")
                continue
            event_id = str(event.get("event_ticker") or "")
            if event.get("series_ticker") != NBA_SERIES or not event_id.startswith(NBA_SERIES + "-"):
                reasons.append("NBA endpoint returned an unexpected series")
                continue
            if event_id in seen_events:
                continue
            seen_events.add(event_id)
            added += 1
            if not isinstance(event.get("markets"), list):
                reasons.append("NBA event has no nested market data")
                continue
            for original in event["markets"]:
                if not isinstance(original, dict):
                    reasons.append("NBA market record malformed")
                    continue
                if K._record_explicitly_closed(original):
                    continue
                ticker = str(original.get("ticker") or "")
                if not ticker.startswith(event_id + "-") or original.get("market_type") != "binary":
                    reasons.append("NBA game market has an unexpected identifier/type")
                    continue
                if ticker in seen_markets:
                    continue
                seen_markets.add(ticker)
                record = dict(original)
                record.update(_event_title=event.get("title") or "",
                              _event_ticker=event_id, _event_id=event_id,
                              _event_metadata=event.get("product_metadata") or {},
                              _event_start_time=original.get("occurrence_datetime") or "",
                              _series_title="NBA Game", _native_sport="basketball",
                              _settlement_sources=event.get("settlement_sources") or [],
                              _contract_terms_url=(series_meta[0].get("contract_terms_url")
                                                   if series_meta else ""))
                if record["_contract_terms_url"] == NBA_TERMS:
                    record["_settlement_documents"] = [dict(NBA_DOCUMENT)]
                records.append(record)
        next_cursor = str(body.get("cursor") or "")
        if not next_cursor:
            complete = not any("metadata unavailable" not in reason for reason in reasons)
            break
        if not events or not added or next_cursor in seen_cursors:
            reasons.append("NBA event pagination made no progress or repeated its cursor")
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        reasons.append("NBA event pagination guard reached")
    warnings = [f"basketball kalshi: {reason}" for reason in reasons]
    return records, _coverage(complete, "KXNBAGAME_nested_open_events", len(records), reasons), warnings, series_meta


def _collect_gamma() -> tuple[list[dict], dict, list[str]]:
    records, reasons = [], []
    cursor, seen_cursors, seen_events, seen_markets = "", set(), set(), set()
    complete = False
    for _ in range(K.PAGE_GUARD):
        params = {"tag_slug": "nba", "closed": "false", "limit": 100}
        if cursor:
            params["after_cursor"] = cursor
        status, body = K.http.get(K.GAMMA + "/events/keyset", params, quiet=True,
                                  note="NBA native event keyset")
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("events"), list):
            reasons.append("NBA event keyset request failed or returned invalid data")
            break
        events = body["events"]
        added = 0
        for event in events:
            if not isinstance(event, dict):
                reasons.append("NBA event record malformed")
                continue
            event_id = str(event.get("id") or event.get("slug") or "")
            if event_id in seen_events:
                continue
            seen_events.add(event_id)
            added += 1
            if not GAME_SLUG.fullmatch(str(event.get("slug") or "")):
                continue
            if K._record_explicitly_closed(event):
                continue
            if not isinstance(event.get("markets"), list):
                reasons.append("NBA game event has no market data")
                continue
            for original in event["markets"]:
                if not isinstance(original, dict):
                    reasons.append("NBA market record malformed")
                    continue
                if (K._record_explicitly_closed(original) or
                        original.get("sportsMarketType") != "moneyline" or
                        original.get("slug") != event.get("slug")):
                    continue
                market_id = str(original.get("id") or "")
                if not market_id:
                    reasons.append("NBA moneyline market has no identifier")
                    continue
                if market_id in seen_markets:
                    continue
                seen_markets.add(market_id)
                record = dict(original)
                record.update(_event_title=event.get("title") or "",
                              _event_slug=event.get("slug") or "", _event_id=event_id,
                              _event_start_time=event.get("startTime") or "",
                              _event_teams=event.get("teams") or [],
                              _native_sport="basketball", _native_league="nba")
                records.append(record)
        next_cursor = str(body.get("next_cursor") or "")
        if not next_cursor:
            complete = not reasons
            break
        if not events or not added or next_cursor in seen_cursors:
            reasons.append("NBA keyset pagination made no progress or repeated its cursor")
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        reasons.append("NBA keyset pagination guard reached")
    return records, _coverage(complete, "events_keyset_tag_slug_nba_moneyline", len(records), reasons), [
        f"basketball polymarket_intl: {reason}" for reason in reasons]


def collect_basketball() -> tuple[dict, dict, list[str]]:
    """Fetch complete open NBA game winners from two venues, retaining team metadata."""
    with ThreadPoolExecutor(max_workers=3) as pool:
        kalshi = pool.submit(_collect_kalshi)
        gamma = pool.submit(_collect_gamma)
        teams = pool.submit(K.http.get, K.GAMMA + "/teams", {"league": "nba", "limit": 100},
                            quiet=True, note="NBA authoritative team metadata")
        k_records, k_coverage, k_warnings, series = kalshi.result()
        p_records, p_coverage, p_warnings = gamma.result()
        team_status, team_body = teams.result()
    warnings = k_warnings + p_warnings
    team_rows = team_body if team_status == 200 and isinstance(team_body, list) else []
    if not team_rows:
        warnings.append("basketball: NBA /teams metadata unavailable; exact event team metadata and approved registry retained")
    raw = {"kalshi": k_records, "polymarket_intl": p_records,
           "_teams": team_rows, "_kalshi_series": series}
    return raw, {"kalshi": k_coverage, "polymarket_intl": p_coverage}, warnings


def _start(record: dict, venue: str) -> tuple[str, str, str, list[str]]:
    notes = []
    native = record.get("occurrence_datetime") if venue == "kalshi" else record.get("gameStartTime")
    native_date, native_time = C._iso_utc_parts(str(native or ""))
    event_date, event_time = C._iso_utc_parts(str(record.get("_event_start_time") or ""))
    if venue == "kalshi":
        rules_date, rules_time, basis, _ = C._kalshi_start_utc_parts(record)
        if rules_date:
            if native_date and (rules_date, rules_time) != (native_date, native_time):
                notes.append("published rules start conflicts with native occurrence timestamp")
            return rules_date, rules_time, basis, notes
    if native_date and event_date and (native_date, native_time) != (event_date, event_time):
        notes.append("native game and event start timestamps disagree")
    if not native_date and event_date:
        if native:
            notes.append("native game start is invalid or lacks a timezone; using published timezone-aware event start")
        return event_date, event_time, "native_event_start_timestamp_to_utc", notes
    if not native_date:
        notes.append("NBA scheduled start time not established")
    return native_date, native_time, "native_scheduled_timestamp_to_utc", notes


def _profile(venue: str, raw: dict, outcome: str) -> dict:
    import settlement
    return settlement.profile_market(venue, raw, sport="basketball", outcome=outcome)


def parse_basketball(raw: dict) -> tuple[list[C.Fixture], list[C.Market], list[str]]:
    registry = TeamRegistry(raw.get("_teams") or [])
    fixtures, markets, warnings = [], [], []
    kalshi_events: dict[str, list[dict]] = defaultdict(list)
    for record in raw.get("kalshi") or []:
        if not isinstance(record, dict):
            warnings.append("COVERAGE_INCOMPLETE basketball kalshi: malformed record")
            continue
        if K._record_explicitly_closed(record):
            continue
        event = str(record.get("event_ticker") or record.get("_event_ticker") or "")
        if event.startswith(NBA_SERIES + "-"):
            kalshi_events[event].append(record)
        else:
            warnings.append("COVERAGE_INCOMPLETE basketball kalshi: unexpected event series")
    for event, records in kalshi_events.items():
        resolved = {}
        notes = []
        for record in records:
            code = str(record.get("ticker") or "").rsplit("-", 1)[-1]
            published = str(record.get("yes_sub_title") or "").strip()
            canonical = registry.resolve(published, code)
            if canonical:
                resolved[code] = (published, canonical)
        if len(resolved) != 2:
            warnings.append(f"COVERAGE_INCOMPLETE basketball kalshi: {event} does not resolve to exactly two NBA teams")
            continue
        codes = sorted(resolved)
        date, start_time, basis, start_notes = _start(records[0], "kalshi")
        notes.extend(start_notes)
        for record in records[1:]:
            other_date, other_time, _, _ = _start(record, "kalshi")
            if (other_date, other_time) != (date, start_time):
                notes.append("NBA event markets publish conflicting start timestamps")
        if not date:
            warnings.append(f"COVERAGE_INCOMPLETE basketball kalshi: {event} has no scheduled UTC start")
            continue
        key = C.FixtureKey(date, frozenset(C.norm_team(resolved[c][1]) for c in codes), "M", "NBA", sport="basketball")
        fixture_id = "kalshi:basketball:" + event
        aliases = [registry.evidence(resolved[c][0], c) for c in codes]
        note = "; ".join(dict.fromkeys(notes))
        fixtures.append(C.Fixture(key, "kalshi", fixture_id, "NBA", start_time,
                                   resolved[codes[0]][0], resolved[codes[1]][0], note,
                                   {"team_codes": codes, "event_ticker": event,
                                    "trusted_aliases": aliases, "start_time_basis": basis}))
        for record in records:
            code = str(record.get("ticker") or "").rsplit("-", 1)[-1]
            if code not in resolved:
                warnings.append(f"COVERAGE_INCOMPLETE basketball kalshi: unresolved outcome {code}")
                continue
            outcome = resolved[code][1]
            markets.append(C.Market(
                "kalshi", fixture_id, key, C.MATCH_WINNER, outcome, event_id=event,
                market_id=str(record.get("ticker") or ""), slug=str(record.get("ticker") or ""),
                title=str(record.get("title") or ""),
                url=f"https://kalshi.com/markets/kxnbagame/nba-game/{event.lower()}",
                raw_outcome=resolved[code][0], note=note,
                extra={"settlement": _profile("kalshi", record, outcome),
                       "resolution_evidence": {field: record.get(field) for field in
                                               ("rules_primary", "rules_secondary", "_contract_terms_url")}},
            ))
    for record in raw.get("polymarket_intl") or []:
        if not isinstance(record, dict):
            warnings.append("COVERAGE_INCOMPLETE basketball polymarket_intl: malformed record")
            continue
        if K._record_explicitly_closed(record):
            continue
        slug = str(record.get("_event_slug") or record.get("slug") or "")
        match = GAME_SLUG.fullmatch(slug)
        if not match or record.get("sportsMarketType") != "moneyline" or record.get("slug") != slug:
            warnings.append("COVERAGE_INCOMPLETE basketball polymarket_intl: unsupported game market")
            continue
        metadata = {str(team.get("abbreviation") or "").upper(): team
                    for team in record.get("_event_teams") or [] if isinstance(team, dict)}
        codes = [match.group("a").upper(), match.group("b").upper()]
        outcomes = C._j(record.get("outcomes"))
        resolved = {}
        for code in codes:
            team = metadata.get(code, {})
            name = str(team.get("name") or "")
            canonical = registry.resolve(name, code, league=str(team.get("league") or ""))
            if canonical:
                resolved[code] = (name, canonical)
        if (len(resolved) != 2 or not isinstance(outcomes, list) or len(outcomes) != 2 or
                len({C.norm_team(str(outcome)) for outcome in outcomes}) != 2):
            warnings.append(f"COVERAGE_INCOMPLETE basketball polymarket_intl: unresolved NBA team metadata for {slug}")
            continue
        outcome_pairs = []
        for outcome in outcomes:
            choices = [(code, registry.resolve(str(outcome), code)) for code in codes]
            choices = [(code, name) for code, name in choices if name]
            if len(choices) == 1:
                outcome_pairs.append((str(outcome), choices[0][1]))
        if len(outcome_pairs) != 2 or len({name for _, name in outcome_pairs}) != 2:
            warnings.append(f"COVERAGE_INCOMPLETE basketball polymarket_intl: outcome teams disagree for {slug}")
            continue
        date, start_time, basis, notes = _start(record, "polymarket_intl")
        if not date:
            warnings.append(f"COVERAGE_INCOMPLETE basketball polymarket_intl: {slug} has no scheduled UTC start")
            continue
        key = C.FixtureKey(date, frozenset(C.norm_team(resolved[c][1]) for c in codes), "M", "NBA", sport="basketball")
        fixture_id = "polymarket_intl:basketball:" + slug
        note = "; ".join(notes)
        fixtures.append(C.Fixture(
            key, "polymarket_intl", fixture_id, "NBA", start_time,
            resolved[codes[0]][0], resolved[codes[1]][0], note,
            {"team_codes": sorted(codes), "event_slug": slug,
             "trusted_aliases": [registry.evidence(resolved[c][0], c) for c in codes],
             "start_time_basis": basis}))
        for published, outcome in outcome_pairs:
            markets.append(C.Market(
                "polymarket_intl", fixture_id, key, C.MATCH_WINNER, outcome,
                event_id=str(record.get("_event_id") or ""), market_id=str(record.get("id") or ""),
                slug=str(record.get("slug") or ""), title=str(record.get("question") or ""),
                url="https://polymarket.com/event/" + slug, raw_outcome=published, note=note,
                extra={"settlement": _profile("polymarket_intl", record, outcome),
                       "resolution_evidence": {field: record.get(field) for field in
                                               ("description", "resolutionSource")}},
            ))
    return fixtures, markets, warnings


def _collect_cricket() -> tuple[dict, dict, list[str]]:
    raw, coverage, warnings = {}, {}, []
    for venue, fetch in (("kalshi", K.fetch_kalshi), ("polymarket_intl", K.fetch_polymarket_intl)):
        try:
            result = fetch()
            raw[venue] = result[0] if isinstance(result, tuple) else result
            coverage[venue] = dict(K.COLLECTION_META.get(venue) or {})
        except Exception as exc:
            raw[venue] = []
            coverage[venue] = _coverage(False, "collection_failed", 0, [str(exc)])
            warnings.append(f"cricket {venue}: {exc}")
    return raw, coverage, warnings


def _parse_cricket(raw: dict) -> tuple[list[C.Fixture], list[C.Market], list[str]]:
    fixtures, markets, warnings = [], [], []
    for venue, parser in (("kalshi", C.parse_kalshi),
                          ("polymarket_intl", lambda rows, **kwargs: C.parse_polymarket(
                              rows, "polymarket_intl", **kwargs))):
        parsed_fixtures, parsed_markets, notes = parser(
            raw.get(venue) or [], market_types={C.MATCH_WINNER})
        fixtures.extend(parsed_fixtures)
        markets.extend(market for market in parsed_markets if market.market_type == C.MATCH_WINNER)
        warnings.extend(notes)
    return fixtures, markets, warnings


@dataclass(frozen=True)
class SportAdapter:
    key: str
    label: str
    venues: tuple[str, ...]
    collect: Callable
    parse: Callable


ADAPTERS = {
    "cricket": SportAdapter("cricket", "Cricket", ("kalshi", "polymarket_intl"), _collect_cricket, _parse_cricket),
    "basketball": SportAdapter("basketball", "Basketball (NBA)", ("kalshi", "polymarket_intl"),
                               collect_basketball, parse_basketball),
}
