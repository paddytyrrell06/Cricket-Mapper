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
KALSHI_DOCUMENTS = {
    "basketball": NBA_DOCUMENT,
    "football": {
        "url": "https://assets.kalshi.com/contract_terms/FOOTBALLGAMEWIN.pdf",
        "text": (
            "Unless otherwise specified, a football game covers full regulation time, including any overtime. "
            "All overtime periods are included in determining the winner. If the game ends in a tie and no Tie "
            "strike is listed, Yes holders and No holders are each paid $0.50."
        ),
    },
    "hockey": {
        "url": "https://assets.kalshi.com/contract_terms/HOCKEYWINNINGINPERIOD.pdf",
        "text": (
            "Unless otherwise specified, an entire hockey game includes regulation time, overtime, and the "
            "shootout. Statistics during regulation time, overtime periods, and the shootout count. A team must "
            "score strictly more goals to win; if teams are drawn, the team contract may resolve No and a Tie "
            "strike, if present, may resolve Yes."
        ),
    },
    "baseball": {
        "url": "https://assets.kalshi.com/contract_terms/BASEBALLGAMEWIN.pdf",
        "text": (
            "Unless otherwise specified, a baseball game covers the full game, including all regulation innings "
            "and extra innings or tie-breaking procedures. All extra innings are included in determining the "
            "winner. If the game ends in a tie and no Tie strike is listed, each team strike pays $0.50."
        ),
    },
    "soccer": {
        "url": "https://assets.kalshi.com/contract_terms/SOCCERGAMEWIN.pdf",
        "text": (
            "Soccer regulation time is two halves of 45 minutes plus stoppage time. "
            "Full match includes extra time, penalty shootout, or other competition tiebreakers. "
            "A Draw iteration resolves Yes when neither participant wins within the specified result scope. "
            "For regulation time, a draw does not constitute a team win."
        ),
    },
}
SPORT_CONFIGS = {
    "basketball": {"league": "nba", "series": "KXNBAGAME", "series_id": 10345,
                   "config": "nba_teams.json"},
    "football": {"league": "nfl", "series": "KXNFLGAME", "series_id": 12185,
                 "config": "nfl_teams.json"},
    "hockey": {"league": "nhl", "series": "KXNHLGAME", "series_id": 10346,
               "config": "nhl_teams.json"},
    "baseball": {"league": "mlb", "series": "KXMLBGAME", "series_id": 3,
                 "config": "mlb_teams.json"},
}
SOCCER_COMPETITIONS = {
    "epl": {"label": "EPL", "name": "English Premier League", "series": "KXEPLGAME", "series_id": 10188},
    "lal": {"label": "LaLiga", "name": "La Liga", "series": "KXLALIGAGAME", "series_id": 10193},
    "bun": {"label": "Bundesliga", "name": "Bundesliga", "series": "KXBUNDESLIGAGAME", "series_id": 10194},
    "sea": {"label": "Serie A", "name": "Serie A", "series": "KXSERIEAGAME", "series_id": 10203},
    "fl1": {"label": "Ligue 1", "name": "Ligue 1", "series": "KXLIGUE1GAME", "series_id": 10195},
    "ucl": {"label": "UCL", "name": "UEFA Champions League", "series": "KXUCLGAME", "series_id": 10204},
    "mls": {"label": "MLS", "name": "Major League Soccer", "series": "KXMLSGAME", "series_id": 10189},
}
TEAM_CONFIGS = {
    sport: json.loads((Path(__file__).parent / "config" / details["config"])
                      .read_text(encoding="utf-8"))
    for sport, details in SPORT_CONFIGS.items()
}
TEAM_CONFIG = TEAM_CONFIGS["basketball"]


class TeamRegistry:
    """Resolve only an exact code and published alias in a verified league."""

    def __init__(self, metadata: list[dict] | None = None, *, config: dict | None = None):
        config = config or TEAM_CONFIG
        self.league = config["league"]
        self.source = config["source"]
        self.code_aliases = config.get("code_aliases") or {}
        self.teams = {row["code"]: row for row in config["teams"]}
        self.aliases = {code: {C.norm_team(name) for name in [row["canonical"], *row["aliases"]]}
                        for code, row in self.teams.items()}
        for row in metadata or []:
            if not isinstance(row, dict) or str(row.get("league") or "").lower() != self.league:
                continue
            code = self.code(row.get("abbreviation") or "")
            if code in self.teams and C.norm_team(row.get("name") or "") in self.aliases[code]:
                self.aliases[code].update(name for field in ("name", "alias")
                                          if (name := C.norm_team(row.get(field) or "")))

    def code(self, value: str) -> str:
        value = str(value).upper()
        return self.code_aliases.get(value, value)

    def resolve(self, published: str, code: str, *, league: str | None = None) -> str:
        code = self.code(code)
        if (self.league if league is None else league.lower()) != self.league or code not in self.teams:
            return ""
        return self.teams[code]["canonical"] if C.norm_team(published) in self.aliases[code] else ""

    def evidence(self, published: str, code: str) -> dict:
        return {"rule_id": f"{self.league}-team-registry:{self.code(code).lower()}", "published_name": published,
                "canonical_name": self.resolve(published, code), "competition": self.league.upper(),
                "evidence": f"Exact {self.league.upper()} team code and published alias; " + self.source}


def _coverage(complete: bool, method: str, records: int, reasons: list[str]) -> dict:
    return {"complete": complete, "method": method, "records": records,
            "reason": "; ".join(dict.fromkeys(reasons))}


def _collect_kalshi(*, series: str = NBA_SERIES, league: str = "nba",
                     document: dict = NBA_DOCUMENT) -> tuple[list[dict], dict, list[str], list[dict], list[dict]]:
    records, reasons, series_meta, milestones = [], [], [], []
    status, body = K.http.get(K.KALSHI + "/series/" + series, quiet=True,
                              note=f"{league.upper()} series metadata")
    if status == 200 and isinstance(body, dict) and isinstance(body.get("series"), dict):
        series_meta = [body["series"]]
    else:
        reasons.append(f"{league.upper()} series rule metadata unavailable")
    cursor, seen_cursors, seen_events, seen_markets = "", set(), set(), set()
    complete = False
    for _ in range(K.PAGE_GUARD):
        params = {"series_ticker": series, "status": "open",
                  "with_nested_markets": "true", "with_milestones": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        status, body = K.http.get(K.KALSHI + "/events", params, quiet=True,
                                  note=f"{league.upper()} full-game winner events")
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("events"), list):
            reasons.append(f"{league.upper()} events request failed or returned invalid data")
            break
        events = body["events"]
        page_milestones = [item for item in body.get("milestones") or [] if isinstance(item, dict)]
        milestones.extend(page_milestones)
        added = 0
        for event in events:
            if not isinstance(event, dict):
                reasons.append(f"{league.upper()} event record malformed")
                continue
            event_id = str(event.get("event_ticker") or "")
            if event.get("series_ticker") != series or not event_id.startswith(series + "-"):
                reasons.append(f"{league.upper()} endpoint returned an unexpected series")
                continue
            if event_id in seen_events:
                continue
            seen_events.add(event_id)
            added += 1
            if not isinstance(event.get("markets"), list):
                reasons.append(f"{league.upper()} event has no nested market data")
                continue
            linked = [item for item in page_milestones
                      if str((item.get("details") or {}).get("league") or "").lower() == league
                      and event_id in ((item.get("primary_event_tickers") or []) +
                                       (item.get("related_event_tickers") or []))
                      and (item.get("details") or {}).get("main_game_event_ticker") in (None, "", event_id)
                      and C._iso_utc_parts(str(item.get("start_date") or ""))[0]]
            milestone = linked[0] if linked and len({item["start_date"] for item in linked}) == 1 else {}
            for original in event["markets"]:
                if not isinstance(original, dict):
                    reasons.append(f"{league.upper()} market record malformed")
                    continue
                if K._record_explicitly_closed(original):
                    continue
                ticker = str(original.get("ticker") or "")
                if not ticker.startswith(event_id + "-") or original.get("market_type") != "binary":
                    reasons.append(f"{league.upper()} game market has an unexpected identifier/type")
                    continue
                if ticker in seen_markets:
                    continue
                seen_markets.add(ticker)
                record = dict(original)
                record.update(_event_title=event.get("title") or "",
                              _event_ticker=event_id, _event_id=event_id,
                              _event_metadata=event.get("product_metadata") or {},
                              _event_start_time=original.get("occurrence_datetime") or "",
                              _event_milestone=({key: milestone.get(key) for key in
                                                 ("id", "start_date", "type", "primary_event_tickers", "related_event_tickers")}
                                                | {"details": {"league": league,
                                                               "main_game_event_ticker": event_id}} if milestone else {}),
                              _series_title=series_meta[0].get("title", league.upper() + " Game") if series_meta else league.upper() + " Game",
                              _native_league=league,
                              _settlement_sources=event.get("settlement_sources") or [],
                              _contract_terms_url=(series_meta[0].get("contract_terms_url")
                                                   if series_meta else ""))
                if document and record["_contract_terms_url"] == document["url"]:
                    record["_settlement_documents"] = [dict(document)]
                records.append(record)
        next_cursor = str(body.get("cursor") or "")
        if not next_cursor:
            complete = not any("metadata unavailable" not in reason for reason in reasons)
            break
        if not events or not added or next_cursor in seen_cursors:
            reasons.append(f"{league.upper()} event pagination made no progress or repeated its cursor")
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        reasons.append(f"{league.upper()} event pagination guard reached")
    warnings = [f"{league} kalshi: {reason}" for reason in reasons]
    return records, _coverage(complete, series + "_nested_open_events", len(records), reasons), warnings, series_meta, milestones


def _collect_gamma(*, league: str = "nba") -> tuple[list[dict], dict, list[str]]:
    records, reasons = [], []
    cursor, seen_cursors, seen_events, seen_markets = "", set(), set(), set()
    complete = False
    for _ in range(K.PAGE_GUARD):
        params = {"tag_slug": league, "closed": "false", "limit": 100}
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
            if not game_slug(league).fullmatch(str(event.get("slug") or "")):
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
                              _native_league=league)
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
    return records, _coverage(complete, f"events_keyset_tag_slug_{league}_moneyline", len(records), reasons), [
        f"{league} polymarket_intl: {reason}" for reason in reasons]


def game_slug(league: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(league)}-(?P<a>[a-z0-9]+)-(?P<b>[a-z0-9]+)-"
                      r"(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<game>\d+))?$")


def collect_basketball() -> tuple[dict, dict, list[str]]:
    """Fetch complete open NBA game winners from two venues, retaining team metadata."""
    return collect_team_sport("basketball")


def _collect_gamma_series(*, league: str, series_id: int
                          ) -> tuple[list[dict], dict, list[str]]:
    """Paginate the verified Gamma league series, retaining exact game moneylines."""
    records: list[dict] = []
    reasons: list[str] = []
    seen_events: set[str] = set()
    seen_markets: set[str] = set()
    offset = 0
    limit = 100
    complete = False
    matcher = game_slug(league)
    for _ in range(K.PAGE_GUARD):
        status, page = K.http.get(
            K.GAMMA + "/events",
            {"series_id": series_id, "closed": "false", "limit": limit,
             "offset": offset},
            quiet=offset > 0, note=f"{league.upper()} event series",
        )
        if status != 200 or not isinstance(page, list):
            reasons.append("league event request failed or returned invalid data")
            break
        added = 0
        for event in page:
            if not isinstance(event, dict):
                reasons.append("league event record malformed")
                continue
            slug = str(event.get("slug") or "")
            if not matcher.fullmatch(slug) or K._record_explicitly_closed(event):
                continue
            event_id = str(event.get("id") or slug)
            if event_id in seen_events:
                continue
            seen_events.add(event_id)
            added += 1
            nested = event.get("markets")
            if not isinstance(nested, list):
                reasons.append(f"{league.upper()} game event has no market data: {slug}")
                continue
            for original in nested:
                if not isinstance(original, dict) or K._record_explicitly_closed(original):
                    continue
                market_slug = str(original.get("slug") or "")
                is_match_winner = market_slug == slug
                if league in SOCCER_COMPETITIONS:
                    suffix = market_slug[len(slug) + 1:] if market_slug.startswith(slug + "-") else ""
                    team_codes = {str(team.get("abbreviation") or "").lower()
                                  for team in event.get("teams") or []
                                  if isinstance(team, dict) and
                                  str(team.get("league") or "").lower() == league}
                    is_match_winner = is_match_winner or suffix == "draw" or suffix in team_codes
                if original.get("sportsMarketType") != "moneyline" or not is_match_winner:
                    continue
                market_id = str(original.get("id") or "")
                if not market_id:
                    reasons.append(f"{league.upper()} moneyline has no market ID: {slug}")
                    continue
                if market_id in seen_markets:
                    continue
                seen_markets.add(market_id)
                record = dict(original)
                record.update(
                    _event_title=event.get("title") or "",
                    _event_slug=slug,
                    _event_id=event_id,
                    _event_start_time=event.get("startTime") or "",
                    _event_teams=event.get("teams") or [],
                    _native_league=league,
                )
                records.append(record)
        if len(page) < limit:
            complete = not reasons
            break
        if not added:
            reasons.append("league event pagination made no progress")
            break
        offset += len(page)
    else:
        reasons.append("league event pagination guard reached")
    return records, _coverage(
        complete, f"events_series_id_{series_id}_moneyline", len(records), reasons
    ), [f"{league} polymarket_intl: {reason}" for reason in reasons]


def collect_team_sport(sport: str) -> tuple[dict, dict, list[str]]:
    """Collect one configured full-game-winner league from both active venues."""
    details = SPORT_CONFIGS[sport]
    league, series = details["league"], details["series"]
    document = KALSHI_DOCUMENTS[sport]
    with ThreadPoolExecutor(max_workers=3) as pool:
        kalshi = pool.submit(_collect_kalshi, series=series, league=league,
                             document=document)
        gamma = pool.submit(_collect_gamma_series, league=league,
                            series_id=details["series_id"])
        teams = pool.submit(
            K.http.get, K.GAMMA + "/teams", {"league": league}, quiet=True,
            note=f"{league.upper()} authoritative team metadata",
        )
        k_records, k_coverage, k_warnings, series_rows, milestones = kalshi.result()
        p_records, p_coverage, p_warnings = gamma.result()
        team_status, team_body = teams.result()
    warnings = k_warnings + p_warnings
    team_rows = team_body if team_status == 200 and isinstance(team_body, list) else []
    if not team_rows:
        warnings.append(f"{sport}: {league.upper()} /teams metadata unavailable; "
                        "exact event team metadata and approved registry retained")
    raw = {"kalshi": k_records, "polymarket_intl": p_records,
           "_teams": team_rows, "_kalshi_series": series_rows,
           "_milestones": milestones}
    return raw, {"kalshi": k_coverage, "polymarket_intl": p_coverage}, warnings


def _start(record: dict, venue: str) -> tuple[str, str, str, list[str]]:
    notes = []
    native = record.get("occurrence_datetime") if venue == "kalshi" else record.get("gameStartTime")
    native_date, native_time = C._iso_utc_parts(str(native or ""))
    event_date, event_time = C._iso_utc_parts(str(record.get("_event_start_time") or ""))
    if venue == "kalshi":
        milestone = record.get("_event_milestone") or {}
        details = milestone.get("details") or {}
        event_ticker = str(record.get("event_ticker") or record.get("_event_ticker") or "")
        linked = event_ticker and event_ticker in ((milestone.get("primary_event_tickers") or []) +
                                                 (milestone.get("related_event_tickers") or []))
        milestone_date, milestone_time = C._iso_utc_parts(str(milestone.get("start_date") or ""))
        trusted_milestone = (linked and milestone_date and
                             str(details.get("league") or "").lower() == str(record.get("_native_league") or "nba").lower()
                             and details.get("main_game_event_ticker") in (None, "", event_ticker))
        rules_date, rules_time, basis, _ = C._kalshi_start_utc_parts(record)
        if trusted_milestone:
            if rules_date and (rules_date, rules_time) != (milestone_date, milestone_time):
                notes.append("published rules start conflicts with linked game milestone")
            return milestone_date, milestone_time, "kalshi_linked_milestone_start_date", notes
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


def _profile(venue: str, raw: dict, outcome: str, sport: str = "basketball") -> dict:
    import settlement
    return settlement.profile_market(venue, raw, sport=sport, outcome=outcome)


def parse_basketball(raw: dict) -> tuple[list[C.Fixture], list[C.Market], list[str]]:
    return parse_league(raw, sport="basketball", league="nba", series=NBA_SERIES, config=TEAM_CONFIG)


def _collect_soccer_competition(competition: str
                                ) -> tuple[dict, dict, list[str]]:
    details = SOCCER_COMPETITIONS[competition]
    league, series = competition, details["series"]
    document = KALSHI_DOCUMENTS["soccer"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        kalshi = pool.submit(_collect_kalshi, series=series, league=league,
                             document=document)
        gamma = pool.submit(_collect_gamma_series, league=league,
                            series_id=details["series_id"])
        teams = pool.submit(
            K.http.get, K.GAMMA + "/teams", {"league": league}, quiet=True,
            note=f"{details['label']} authoritative team metadata",
        )
        k_records, k_coverage, k_warnings, series_rows, milestones = kalshi.result()
        p_records, p_coverage, p_warnings = gamma.result()
        team_status, team_body = teams.result()

    warnings = k_warnings + p_warnings
    team_rows = team_body if team_status == 200 and isinstance(team_body, list) else []
    if not team_rows:
        warnings.append(f"soccer:{competition}: official team metadata unavailable")
    raw = {
        "kalshi": k_records, "polymarket_intl": p_records,
        "_teams": team_rows, "_kalshi_series": series_rows,
        "_milestones": milestones,
    }
    return raw, {"kalshi": k_coverage, "polymarket_intl": p_coverage}, warnings


def _collect_soccer() -> tuple[dict, dict, list[str]]:
    raw: dict = {"kalshi": [], "polymarket_intl": []}
    coverage: dict = {}
    warnings: list[str] = []
    for competition in SOCCER_COMPETITIONS:
        try:
            section, scope, notes = _collect_soccer_competition(competition)
        except Exception as exc:
            section = {"kalshi": [], "polymarket_intl": [], "_teams": [],
                       "_kalshi_series": [], "_milestones": []}
            scope = {venue: _coverage(False, "collection_failed", 0, [repr(exc)])
                     for venue in ("kalshi", "polymarket_intl")}
            notes = [f"soccer:{competition}: collection failed: {exc!r}"]
        for key, records in section.items():
            if key in {"kalshi", "polymarket_intl"}:
                for record in records:
                    record["_soccer_competition"] = competition
                raw[key].extend(records)
            else:
                raw[f"_{competition}_{key[1:]}"] = records
        for venue, state in scope.items():
            current = coverage.setdefault(venue, {
                "complete": True, "method": "selected_soccer_league_series",
                "records": 0, "reason": "",
            })
            current["complete"] = bool(current["complete"] and state.get("complete"))
            current["records"] += int(state.get("records") or 0)
            if state.get("reason"):
                current.setdefault("reasons", []).append(
                    f"{competition}: {state['reason']}")
        warnings.extend(notes)
    for meta in coverage.values():
        reasons = meta.pop("reasons", [])
        meta["reason"] = "; ".join(reasons)
    return raw, coverage, warnings


def _parse_soccer(raw: dict) -> tuple[list[C.Fixture], list[C.Market], list[str]]:
    fixtures: list[C.Fixture] = []
    markets: list[C.Market] = []
    warnings: list[str] = []
    for competition, details in SOCCER_COMPETITIONS.items():
        scoped_kalshi = [record for record in raw.get("kalshi", [])
                         if record.get("_soccer_competition") == competition]
        scoped_gamma = [record for record in raw.get("polymarket_intl", [])
                        if record.get("_soccer_competition") == competition]
        parsed_fixtures, parsed_markets, notes = _parse_soccer_competition(
            competition, details, scoped_kalshi, scoped_gamma)
        fixtures.extend(parsed_fixtures)
        markets.extend(parsed_markets)
        warnings.extend(notes)
    return fixtures, markets, warnings


def _soccer_identity_key(value: str) -> str:
    """Normalize only documented soccer club-name suffix variations."""
    normal = C.norm_team(value)
    normal = re.sub(r"\b(?:fc|afc|sc)\b", " ", normal)
    return re.sub(r"\s+", " ", normal).strip()


def _soccer_canonical_name(name: str, teams: list[dict]) -> str:
    wanted = _soccer_identity_key(name)
    if not wanted:
        return ""
    matches = set()
    for team in teams:
        candidates = [str(team.get("name") or ""), str(team.get("alias") or "")]
        if any(_soccer_identity_key(candidate) == wanted for candidate in candidates):
            canonical = str(team.get("name") or "").strip()
            if canonical:
                matches.add(canonical)
    return next(iter(matches)) if len(matches) == 1 else ""


def _parse_soccer_competition(
        competition: str, details: dict, kalshi_rows: list[dict],
        gamma_rows: list[dict]
        ) -> tuple[list[C.Fixture], list[C.Market], list[str]]:
    fixtures: dict[tuple[str, str], C.Fixture] = {}
    markets: list[C.Market] = []
    warnings: list[str] = []
    series, comp_name = details["series"], details["name"]

    kalshi_events: dict[str, list[dict]] = defaultdict(list)
    for record in kalshi_rows:
        if K._record_explicitly_closed(record):
            continue
        event = str(record.get("event_ticker") or "")
        if event.startswith(series + "-"):
            kalshi_events[event].append(record)

    for event, records in kalshi_events.items():
        team_records: dict[str, dict] = {}
        tie_record: dict | None = None
        for record in records:
            code = str(record.get("ticker") or "").rsplit("-", 1)[-1]
            name = str(record.get("yes_sub_title") or "").strip()
            if code == "TIE" or name.lower() == "tie":
                tie_record = record
            elif code and name:
                team_records[code] = record
        if len(team_records) != 2:
            warnings.append(f"COVERAGE_INCOMPLETE soccer:{competition} kalshi: {event} does not have exactly two team outcomes")
            continue
        canonical_by_code: dict[str, str] = {}
        for reference_gamma in gamma_rows:
            event_teams = [team for team in
                           (reference_gamma.get("_event_teams") or [])
                           if isinstance(team, dict) and
                           str(team.get("league") or "").lower() == competition]
            candidate = {
                code: _soccer_canonical_name(
                    str(record.get("yes_sub_title") or "").strip(), event_teams)
                for code, record in team_records.items()
            }
            if (len(candidate) == 2 and all(candidate.values()) and
                    len(set(candidate.values())) == 2):
                canonical_by_code = candidate
                break
        if len(canonical_by_code) != 2:
            warnings.append(
                f"COVERAGE_INCOMPLETE soccer:{competition} kalshi: "
                f"team identities cannot be verified against official competition names in {event}")
            continue
        date, start_time, basis, start_notes = _start(records[0], "kalshi")
        if not date:
            warnings.append(f"COVERAGE_INCOMPLETE soccer:{competition} kalshi: {event} has no scheduled UTC start")
            continue
        names = [canonical_by_code[code] for code in team_records]
        key = C.FixtureKey(date, frozenset(C.norm_team(name) for name in names),
                           "M", "SOCCER", sport="soccer")
        fixture_id = f"kalshi:soccer:{competition}:{event}"
        fixture = C.Fixture(
            key, "kalshi", fixture_id, comp_name, start_time,
            names[0], names[1], "; ".join(start_notes),
            {"event_ticker": event, "series": series,
             "start_time_basis": basis},
        )
        fixtures[("kalshi", fixture_id)] = fixture
        for record in [*team_records.values(), *([tie_record] if tie_record else [])]:
            is_draw = record is tie_record
            outcome = "draw" if is_draw else str(record.get("yes_sub_title") or "").strip()
            markets.append(C.Market(
                "kalshi", fixture_id, key, C.MATCH_WINNER, outcome,
                event_id=event, market_id=str(record.get("ticker") or ""),
                slug=str(record.get("ticker") or ""),
                title=str(record.get("title") or ""),
                url=(f"https://kalshi.com/markets/{series.lower()}/"
                     f"{C._url_slug(comp_name + ' Game')}/{event.lower()}"),
                raw_outcome=str(record.get("yes_sub_title") or ""),
                note="; ".join(start_notes),
                extra={"settlement": _profile("kalshi", record, outcome, "soccer"),
                       "resolution_evidence": {
                           field: record.get(field) for field in
                           ("rules_primary", "rules_secondary", "_contract_terms_url")
                       }},
            ))

    for record in gamma_rows:
        if not isinstance(record, dict) or K._record_explicitly_closed(record):
            continue
        slug = str(record.get("_event_slug") or "")
        market_slug = str(record.get("slug") or "")
        match = game_slug(competition).fullmatch(slug)
        if (not match or record.get("sportsMarketType") != "moneyline" or
            not market_slug.startswith(slug + "-")):
            warnings.append(f"COVERAGE_INCOMPLETE soccer:{competition} polymarket_intl: unsupported full-time moneyline {slug}")
            continue
        teams = [team for team in record.get("_event_teams") or []
                 if isinstance(team, dict)]
        by_code = {str(team.get("abbreviation") or "").lower(): team
                   for team in teams}
        codes = (match.group("a"), match.group("b"))
        participants: list[str] = []
        valid_teams = True
        for code in codes:
            team = by_code.get(code)
            if (not team or str(team.get("league") or "").lower() != competition or
                    not str(team.get("name") or "").strip()):
                valid_teams = False
                break
            participants.append(str(team["name"]).strip())
        if not valid_teams or len({C.norm_team(name) for name in participants}) != 2:
            warnings.append(f"COVERAGE_INCOMPLETE soccer:{competition} polymarket_intl: unverified participant metadata for {slug}")
            continue
        outcomes = C._j(record.get("outcomes"))
        if (not isinstance(outcomes, list) or len(outcomes) != 2 or
                {str(value).strip().lower() for value in outcomes} != {"yes", "no"}):
            warnings.append(f"COVERAGE_INCOMPLETE soccer:{competition} polymarket_intl: expected binary Yes/No moneyline {slug}")
            continue
        suffix = market_slug[len(slug) + 1:]
        if suffix.lower() == "draw":
            target = "draw"
        else:
            matching_codes = [code for code in codes if code == suffix.lower()]
            if len(matching_codes) != 1:
                warnings.append(f"COVERAGE_INCOMPLETE soccer:{competition} polymarket_intl: cannot identify Yes target for {market_slug}")
                continue
            target = participants[codes.index(matching_codes[0])]
        date, start_time, basis, time_notes = _start(record, "polymarket_intl")
        if not date:
            warnings.append(f"COVERAGE_INCOMPLETE soccer:{competition} polymarket_intl: missing scheduled UTC start {slug}")
            continue
        key = C.FixtureKey(date, frozenset(C.norm_team(name) for name in participants),
                           "M", "SOCCER", sport="soccer")
        fixture_id = f"polymarket_intl:soccer:{competition}:{slug}"
        fixture = C.Fixture(
            key, "polymarket_intl", fixture_id, comp_name, start_time,
            participants[0], participants[1], "; ".join(time_notes),
            {"team_codes": sorted(codes), "event_slug": slug,
             "trusted_aliases": [], "start_time_basis": basis},
        )
        fixtures[("polymarket_intl", fixture_id)] = fixture
        markets.append(C.Market(
            "polymarket_intl", fixture_id, key, C.MATCH_WINNER, target,
            event_id=str(record.get("_event_id") or ""),
            market_id=str(record.get("id") or ""), slug=market_slug,
            title=str(record.get("question") or ""),
            url="https://polymarket.com/event/" + slug,
            raw_outcome="Yes", note="; ".join(time_notes),
            extra={"binary_target_market": True,
                   "binary_target_outcome": "yes",
                   "settlement": _profile("polymarket_intl", record, target, "soccer"),
                   "resolution_evidence": {
                       field: record.get(field) for field in
                       ("description", "resolutionSource")
                   }},
        ))
    return list(fixtures.values()), markets, warnings


def parse_league(raw: dict, *, sport: str, league: str, series: str,
                config: dict) -> tuple[list[C.Fixture], list[C.Market], list[str]]:
    registry = TeamRegistry(raw.get("_teams") or [], config=config)
    fixtures, markets, warnings = [], [], []
    kalshi_events: dict[str, list[dict]] = defaultdict(list)
    for record in raw.get("kalshi") or []:
        if not isinstance(record, dict):
            warnings.append("COVERAGE_INCOMPLETE basketball kalshi: malformed record")
            continue
        if K._record_explicitly_closed(record):
            continue
        event = str(record.get("event_ticker") or record.get("_event_ticker") or "")
        if event.startswith(series + "-"):
            kalshi_events[event].append(record)
        else:
            warnings.append("COVERAGE_INCOMPLETE basketball kalshi: unexpected event series")
    for event, records in kalshi_events.items():
        resolved = {}
        notes = []
        for record in records:
            code = registry.code(str(record.get("ticker") or "").rsplit("-", 1)[-1])
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
        key = C.FixtureKey(date, frozenset(C.norm_team(resolved[c][1]) for c in codes), "M", league.upper(), sport=sport)
        fixture_id = f"kalshi:{sport}:" + event
        aliases = [registry.evidence(resolved[c][0], c) for c in codes]
        note = "; ".join(dict.fromkeys(notes))
        fixtures.append(C.Fixture(key, "kalshi", fixture_id, league.upper(), start_time,
                                   resolved[codes[0]][0], resolved[codes[1]][0], note,
                                   {"team_codes": codes, "event_ticker": event,
                                    "trusted_aliases": aliases, "start_time_basis": basis}))
        for record in records:
            code = registry.code(str(record.get("ticker") or "").rsplit("-", 1)[-1])
            if code not in resolved:
                warnings.append(f"COVERAGE_INCOMPLETE basketball kalshi: unresolved outcome {code}")
                continue
            outcome = resolved[code][1]
            markets.append(C.Market(
                "kalshi", fixture_id, key, C.MATCH_WINNER, outcome, event_id=event,
                market_id=str(record.get("ticker") or ""), slug=str(record.get("ticker") or ""),
                title=str(record.get("title") or ""),
                url=f"https://kalshi.com/markets/{series.lower()}/{C._url_slug(str(record.get('_series_title') or league.upper() + ' Game'))}/{event.lower()}",
                raw_outcome=resolved[code][0], note=note,
                extra={"settlement": _profile("kalshi", record, outcome, sport),
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
        match = game_slug(league).fullmatch(slug)
        if not match or record.get("sportsMarketType") != "moneyline" or record.get("slug") != slug:
            warnings.append("COVERAGE_INCOMPLETE basketball polymarket_intl: unsupported game market")
            continue
        metadata = {registry.code(team.get("abbreviation") or ""): team
                    for team in record.get("_event_teams") or [] if isinstance(team, dict)}
        codes = [registry.code(match.group("a")), registry.code(match.group("b"))]
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
        key = C.FixtureKey(date, frozenset(C.norm_team(resolved[c][1]) for c in codes), "M", league.upper(), sport=sport)
        fixture_id = f"polymarket_intl:{sport}:" + slug
        note = "; ".join(notes)
        fixtures.append(C.Fixture(
            key, "polymarket_intl", fixture_id, league.upper(), start_time,
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
                extra={"settlement": _profile("polymarket_intl", record, outcome, sport),
                       "resolution_evidence": {field: record.get(field) for field in
                                               ("description", "resolutionSource")}},
            ))
    return fixtures, markets, [warning.replace("basketball", sport).replace("NBA", league.upper()) for warning in warnings]


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
    "soccer": SportAdapter("soccer", "Soccer", ("kalshi", "polymarket_intl"), _collect_soccer, _parse_soccer),
    "basketball": SportAdapter("basketball", "Basketball (NBA)", ("kalshi", "polymarket_intl"),
                               collect_basketball, parse_basketball),
}


for sport, label in (("football", "Football (NFL)"),
                     ("hockey", "Hockey (NHL)"),
                     ("baseball", "Baseball (MLB)")):
    details = SPORT_CONFIGS[sport]
    ADAPTERS[sport] = SportAdapter(
        sport, label, ("kalshi", "polymarket_intl"),
        lambda sport=sport: collect_team_sport(sport),
        lambda raw, sport=sport, details=details: parse_league(
            raw, sport=sport, league=details["league"],
            series=details["series"], config=TEAM_CONFIGS[sport]),
    )
