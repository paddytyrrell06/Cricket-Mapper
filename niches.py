"""Category adapters: esports is the first expansion beyond physical sports.

Discovery remains separate from quotes. Native IDs and original outcome order
flow into the same canonical/quote pipeline used by the sports adapters.
"""

from __future__ import annotations

from collections import defaultdict

import canonical as C
import collectors as K
import settlement as S


ESPORTS_TERMS = "https://assets.kalshi.com/contract_terms/ESPORTSWIN.pdf"
# A short extract retaining the formula, used only for checked two-team markets.
ESPORTS_TIE_DOCUMENT = {
    "url": ESPORTS_TERMS,
    "text": "Where there is no “Tie” strike listed, […] ($1/[the number of tied participants])",
}
ESPORTS_COMPETITIONS = {
    "cs2": {"sport": "esports", "label": "CS2", "name": "Counter-Strike 2",
            "series": "KXCS2GAME", "series_id": 10310,
            "team_leagues": {"cs2", "csgo"}, "kalshi_games": {"cs2"}},
    "lol": {"sport": "esports", "label": "LoL", "name": "League of Legends",
            "series": "KXLOLGAME", "series_id": 10311,
            "team_leagues": {"lol"}, "kalshi_games": {"league of legends"}},
}


def collect_esports(competitions: list[str] | tuple[str, ...] | None = None
                   ) -> tuple[dict, dict, list[str]]:
    """Fetch only verified match-winner series, using three discovery workers."""
    import sports

    selected = tuple(ESPORTS_COMPETITIONS) if competitions is None else tuple(competitions)
    unknown = set(selected) - set(ESPORTS_COMPETITIONS)
    if unknown:
        raise ValueError("Unknown esports competitions: " + ", ".join(sorted(unknown)))
    raw, coverage, warnings = sports._collect_competitions(
        {key: ESPORTS_COMPETITIONS[key] for key in selected},
        scope_field="_esports_competition")
    for league in selected:
        details = ESPORTS_COMPETITIONS[league]
        milestones = raw.get(f"_{league}_milestones") or []
        for record in raw.get("kalshi") or []:
            if record.get("_esports_competition") != league:
                continue
            event = str(record.get("event_ticker") or record.get("_event_ticker") or "")
            linked = [item for item in milestones if _linked_milestone(item, event, details)]
            if linked and len({item["start_date"] for item in linked}) == 1:
                milestone = dict(linked[0])
                # sports._start checks the normalized adapter league. Keep the
                # originally published game/league fields as separate evidence.
                milestone["published_details"] = dict(milestone.get("details") or {})
                milestone["details"] = {**milestone["published_details"], "league": league}
                record["_event_milestone"] = milestone
            elif linked:
                record["_esports_identity_notes"] = [
                    "linked esports milestones publish conflicting scheduled starts"]
            if record.get("_contract_terms_url") == ESPORTS_TERMS:
                record["_settlement_documents"] = [dict(ESPORTS_TIE_DOCUMENT)]
    return raw, coverage, warnings


def _linked_milestone(item: dict, event: str, details: dict) -> bool:
    if not isinstance(item, dict):
        return False
    metadata = item.get("details") or {}
    return bool(
        item.get("type") == "esports_match"
        and event in ((item.get("primary_event_tickers") or []) +
                      (item.get("related_event_tickers") or []))
        and metadata.get("main_game_event_ticker") == event
        and str(metadata.get("game") or "").lower() in details["kalshi_games"]
        and C._iso_utc_parts(str(item.get("start_date") or ""))[0])


def _participants(record: dict, league: str, details: dict) -> tuple[list, str]:
    import sports

    slug = str(record.get("_event_slug") or "")
    match = sports.game_slug(league).fullmatch(slug)
    if not match:
        return [], "unsupported native match slug"
    metadata = defaultdict(list)
    for item in record.get("_event_teams") or []:
        if isinstance(item, dict):
            metadata[str(item.get("abbreviation") or "").lower()].append(item)
    participants = []
    for code in (match.group("a"), match.group("b")):
        candidates = metadata.get(code) or []
        if len(candidates) != 1:
            return [], "missing or ambiguous exact participant metadata"
        item = candidates[0]
        name = str(item.get("name") or "").strip()
        if str(item.get("league") or "").lower() not in details["team_leagues"] or not name:
            return [], "participant metadata belongs to another game or has no name"
        names = {C.norm_team(str(item[field])) for field in ("name", "alias") if item.get(field)}
        participants.append((code, name, names))
    if len({C.norm_team(item[1]) for item in participants}) != 2:
        return [], "participants are not two distinct identities"
    return participants, ""


def _full_winner(record: dict, venue: str) -> bool:
    if venue == "kalshi":
        text = str(record.get("rules_secondary") or "").lower()
        return "refers to the full match" in text and "wins" in str(record.get("rules_primary") or "").lower()
    slug = str(record.get("_event_slug") or "")
    text = str(record.get("description") or "").lower()
    return (record.get("sportsMarketType") == "moneyline" and record.get("slug") == slug
            and "win the match against" in text)


def _market(record: dict, venue: str, fixture: C.Fixture, outcome: str,
            published: str, league: str) -> C.Market:
    raw = dict(record)
    raw["_esports_two_participant_contract"] = True
    market_id = str((record.get("ticker") if venue == "kalshi" else record.get("id")) or "")
    slug = str((record.get("ticker") if venue == "kalshi" else record.get("slug")) or "")
    return C.Market(
        venue, fixture.venue_fixture_id, fixture.key, C.MATCH_WINNER, outcome,
        event_id=str((record.get("event_ticker") or record.get("_event_ticker")
                      if venue == "kalshi" else record.get("_event_id")) or ""),
        market_id=market_id, slug=slug,
        title=str((record.get("title") if venue == "kalshi" else record.get("question")) or ""),
        url=(C._kalshi_market_url(record, ESPORTS_COMPETITIONS[league]["series"], market_id)
             if venue == "kalshi" else "https://polymarket.com/event/" + slug),
        raw_outcome=published, note=fixture.note,
        extra={"category": "esports", "game": league,
               "settlement": S.profile_market(venue, raw, sport="esports", outcome=outcome),
               "resolution_evidence": S.resolution_evidence(raw)})


def parse_esports(raw: dict) -> tuple[list[C.Fixture], list[C.Market], list[str]]:
    """Match two-team full series only; unverified names never get aliases."""
    import sports

    fixtures, markets, warnings = [], [], []
    for league, details in ESPORTS_COMPETITIONS.items():
        gamma = [row for row in raw.get("polymarket_intl") or []
                 if isinstance(row, dict) and not K._record_explicitly_closed(row)
                 and (row.get("_esports_competition") or row.get("_native_league")) == league]
        identity_index = defaultdict(set)
        checked = []
        for record in gamma:
            participants, error = _participants(record, league, details)
            outcomes = C._j(record.get("outcomes"))
            if (error or not _full_winner(record, "polymarket_intl")
                    or not isinstance(outcomes, list) or len(outcomes) != 2):
                warnings.append(f"COVERAGE_INCOMPLETE esports:{league} polymarket_intl: {error or 'unsupported full-match winner'} for {record.get('slug')}")
                continue
            resolved = []
            for published in outcomes:
                options = [name for _, name, aliases in participants if C.norm_team(str(published)) in aliases]
                if len(options) == 1:
                    resolved.append((str(published), options[0]))
            if len(resolved) != 2 or len({name for _, name in resolved}) != 2:
                warnings.append(f"COVERAGE_INCOMPLETE esports:{league} polymarket_intl: outcomes disagree with exact participant metadata for {record.get('slug')}")
                continue
            for _, name, aliases in participants:
                for alias in aliases:
                    identity_index[alias].add(name)
            checked.append((record, participants, resolved))

        events = defaultdict(list)
        for record in raw.get("kalshi") or []:
            if not isinstance(record, dict) or K._record_explicitly_closed(record):
                continue
            event = str(record.get("event_ticker") or record.get("_event_ticker") or "")
            if event.startswith(details["series"] + "-"):
                events[event].append(record)
        for event, records in events.items():
            names = [str(record.get("yes_sub_title") or "").strip() for record in records]
            if (len(records) != 2 or len({C.norm_team(name) for name in names if name}) != 2
                    or not all(_full_winner(record, "kalshi") for record in records)
                    or not all(record.get("market_type") == "binary"
                               and str(record.get("ticker") or "").startswith(event + "-") for record in records)):
                warnings.append(f"COVERAGE_INCOMPLETE esports:{league} kalshi: expected exactly two full-match winner outcomes for {event}")
                continue
            canonical_names = []
            identity_notes = []
            for name in names:
                options = identity_index.get(C.norm_team(name), set())
                canonical_names.append(next(iter(options)) if len(options) == 1 else name)
                if len(options) > 1:
                    identity_notes.append("published participant alias is ambiguous within the game")
            if len({C.norm_team(name) for name in canonical_names}) != 2:
                warnings.append(f"COVERAGE_INCOMPLETE esports:{league} kalshi: participant aliases collapse both outcomes for {event}")
                continue
            starts = [sports._start(record, "kalshi") for record in records]
            date, start, basis, notes = starts[0]
            notes.extend(note for _, _, _, other_notes in starts[1:] for note in other_notes)
            notes.extend(identity_notes)
            notes.extend(note for row in records for note in row.get("_esports_identity_notes") or [])
            if any(item[:2] != (date, start) for item in starts[1:]):
                notes.append("native event outcomes publish conflicting start timestamps")
            if not date:
                warnings.append(f"COVERAGE_INCOMPLETE esports:{league} kalshi: no scheduled UTC start for {event}")
                continue
            key = C.FixtureKey(date, frozenset(C.norm_team(name) for name in canonical_names), "X", league.upper(), sport="esports")
            fixture = C.Fixture(key, "kalshi", f"kalshi:esports:{league}:{event}", details["name"], start,
                                *canonical_names, "; ".join(dict.fromkeys(notes)),
                                {"category": "esports", "game": league, "event_ticker": event,
                                 "series": details["series"], "start_time_basis": basis,
                                 "published_participant_names": names})
            fixtures.append(fixture)
            markets.extend(_market(record, "kalshi", fixture, outcome, name, league)
                           for record, outcome, name in zip(records, canonical_names, names))

        for record, participants, resolved in checked:
            date, start, basis, notes = sports._start(record, "polymarket_intl")
            if not date:
                warnings.append(f"COVERAGE_INCOMPLETE esports:{league} polymarket_intl: no scheduled UTC start for {record.get('slug')}")
                continue
            key = C.FixtureKey(date, frozenset(C.norm_team(row[1]) for row in participants), "X", league.upper(), sport="esports")
            fixture = C.Fixture(key, "polymarket_intl", f"polymarket_intl:esports:{league}:{record['slug']}",
                                details["name"], start, participants[0][1], participants[1][1], "; ".join(notes),
                                {"category": "esports", "game": league, "event_slug": record["slug"],
                                 "team_codes": [row[0] for row in participants], "start_time_basis": basis})
            fixtures.append(fixture)
            markets.extend(_market(record, "polymarket_intl", fixture, outcome, published, league)
                           for published, outcome in resolved)
    return fixtures, markets, warnings
