# This file conservatively matches equivalent fixtures and outcomes across venues, sending uncertain cases to review instead of guessing.

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
from difflib import SequenceMatcher

from canonical import (Fixture, FixtureKey, Market, OUTRIGHT, UNKNOWN,
                       TEAM_TOTAL, TOTAL_SIXES, TOTAL_FOURS, norm_team)


PARAMETERISED_TYPES = {TEAM_TOTAL, TOTAL_SIXES, TOTAL_FOURS}


@dataclass
class FixtureMatch:
    canonical_fixture_id: str
    key: FixtureKey
    competition: str
    start_time: str
    team_a: str
    team_b: str
    by_venue: dict = field(default_factory=dict)
    status: str = "accepted"
    confidence: str = "high"
    reason: str = ""


def _canon_id(key: FixtureKey) -> str:
    teams = sorted(key.teams)
    a, b = (teams + ["unknown", "unknown"])[:2]
    slug = f"{a}-{b}".replace(" ", "_")
    return f"{key.date}:{key.gender or 'NA'}:{key.fmt or 'NA'}:{slug}"


def _norm_competition(value: str) -> str:
    s = (value or "").lower()
    s = re.sub(r"\b(metro bank|vitality|betway|tata|dream11)\b", " ", s)
    s = re.sub(r"\b(men|women|mens|womens)\b", " ", s)
    s = re.sub(r"\b(?:19|20)\d{2}\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    aliases = {
        "one day cup": "one day cup",
        "the hundred": "the hundred",
        "lanka premier league": "lanka premier league",
        "t20 global super league": "global super league",
        "global super league": "global super league",
        "county championship": "county championship",
    }
    return aliases.get(s, s)


def _time_minutes(value: str) -> Optional[int]:
    if not value or not re.fullmatch(r"\d{2}:\d{2}", value):
        return None
    h, m = map(int, value.split(":"))
    if h > 23 or m > 59:
        return None
    return h * 60 + m


def _time_gap(a: int, b: int) -> int:
    d = abs(a - b)
    return min(d, 24 * 60 - d)


def _date_gap_days(a: str, b: str) -> Optional[int]:
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except (TypeError, ValueError):
        return None


def _exact_group_problems(key: FixtureKey, group: list[Fixture]) -> list[str]:
    problems: list[str] = []
    per_venue: dict[str, int] = defaultdict(int)
    for fixture in group:
        per_venue[fixture.venue] += 1
    dup = sorted(v for v, n in per_venue.items() if n > 1)
    if dup:
        problems.append(f"multiple fixtures share the key on {', '.join(dup)}")
    if not key.date or len(key.teams) != 2 or not key.gender or not key.fmt:
        problems.append("canonical fixture key is incomplete")

    competitions = {_norm_competition(f.competition) for f in group if f.competition}
    competitions.discard("")
    if len(competitions) > 1:
        problems.append(f"competition mismatch {sorted(competitions)}")

    times = [_time_minutes(f.start_time) for f in group]
    times = [t for t in times if t is not None]
    if len(times) >= 2 and max(_time_gap(a, b) for a, b in itertools.combinations(times, 2)) > 30:
        problems.append("published start times differ by more than 30 minutes")

    notes = sorted({f.note for f in group if f.note})
    problems.extend(notes)
    return problems


def _fixture_match_from_group(key: FixtureKey, group: list[Fixture]) -> FixtureMatch:
    display_by_norm: dict[str, str] = {}


    for fixture in group:
        for alias in (fixture.extra.get("trusted_aliases") or []):
            canonical = str(alias.get("canonical_name") or "").strip()
            if canonical:
                display_by_norm[norm_team(canonical)] = canonical
    for fixture in group:
        for name in (fixture.team_a, fixture.team_b):
            normal = norm_team(name)
            if normal in key.teams and normal not in display_by_norm:
                display_by_norm[normal] = name
    team_norms = sorted(key.teams)
    team_a = display_by_norm.get(team_norms[0], team_norms[0]) if team_norms else ""
    team_b = display_by_norm.get(team_norms[1], team_norms[1]) if len(team_norms) > 1 else ""
    return FixtureMatch(
        canonical_fixture_id=_canon_id(key), key=key,
        competition=next((f.competition for f in group if f.competition), ""),
        start_time=next((f.start_time for f in group if f.start_time), ""),
        team_a=team_a, team_b=team_b,
        by_venue={f.venue: f for f in group})


def _name_similarity(a: str, b: str) -> float:
    a, b = norm_team(a), norm_team(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return max(jaccard, SequenceMatcher(None, a, b).ratio())


def _pair_similarity_components(a: Fixture, b: Fixture) -> tuple[float, float]:
    left = [a.team_a, a.team_b]
    right = [b.team_a, b.team_b]
    straight_values = (_name_similarity(left[0], right[0]),
                       _name_similarity(left[1], right[1]))
    reversed_values = (_name_similarity(left[0], right[1]),
                       _name_similarity(left[1], right[0]))
    straight = (min(straight_values), max(straight_values))
    reversed_score = (min(reversed_values), max(reversed_values))
    return max(straight, reversed_score, key=lambda x: (x[0], x[1]))


def _pair_similarity(a: Fixture, b: Fixture) -> float:
    weaker, _ = _pair_similarity_components(a, b)
    ac = set(a.extra.get("team_codes") or [])
    bc = set(b.extra.get("team_codes") or [])
    if len(ac) == 2 and ac == bc:
        weaker = max(weaker, 0.96)
    return weaker


def _recall_candidate(a: Fixture, b: Fixture, threshold: float = 0.65) -> tuple[bool, float, str]:
    # Review must retain the same plausible candidates checked by the safety audit.
    # This threshold never authorises an accepted fixture or market link.
    weaker, stronger = _pair_similarity_components(a, b)
    ac = set(a.extra.get("team_codes") or [])
    bc = set(b.extra.get("team_codes") or [])
    same_codes = len(ac) == 2 and ac == bc
    if same_codes:
        return True, max(weaker, 0.96), "same venue-published team codes"
    if weaker >= threshold:
        return True, weaker, "both team names are similar"
    if stronger >= 0.98 and weaker >= 0.25:
        return True, weaker, "one team matches exactly and the opponent differs"
    return False, weaker, ""


def match_fixtures(fixtures: list[Fixture]
                   ) -> tuple[list[FixtureMatch], list[FixtureMatch], list[Fixture]]:
    by_key: dict[FixtureKey, list[Fixture]] = defaultdict(list)
    for fixture in fixtures:
        by_key[fixture.key].append(fixture)

    accepted: list[FixtureMatch] = []
    review: list[FixtureMatch] = []
    linked_pairs: set[frozenset[int]] = set()


    for key, group in by_key.items():
        by_venue: dict[str, list[Fixture]] = defaultdict(list)
        for fixture in group:
            by_venue[fixture.venue].append(fixture)
        if len(by_venue) < 2 or len(key.teams) != 2:
            continue
        for venue_a, venue_b in itertools.combinations(sorted(by_venue), 2):
            pair = by_venue[venue_a] + by_venue[venue_b]
            problems = _exact_group_problems(key, pair)
            for left, right in itertools.product(by_venue[venue_a], by_venue[venue_b]):
                fm = _fixture_match_from_group(key, [left, right])
                if problems:
                    fm.status, fm.confidence = "review", "low"
                    fm.reason = "; ".join(problems)
                    review.append(fm)
                else:
                    alias_rules = sorted({
                        str(alias.get("rule_id") or "")
                        for fixture in (left, right)
                        for alias in (fixture.extra.get("trusted_aliases") or [])
                        if alias.get("rule_id")
                    })
                    if alias_rules:
                        fm.reason = ("exact agreement after trusted competition/date-scoped "
                                     "team identity normalisation: " + ", ".join(alias_rules))
                    else:
                        fm.reason = ("exact agreement on date, published teams, gender, "
                                     "format and compatible metadata")
                    accepted.append(fm)
                linked_pairs.add(frozenset((id(left), id(right))))

    accepted_keys = {
        id(fixture): match.key
        for match in accepted
        for fixture in match.by_venue.values()
    }

    def review_key(left: Fixture, right: Fixture) -> FixtureKey:
        # Keep a possible third venue attached to the existing report fixture.
        return accepted_keys.get(id(left), accepted_keys.get(id(right), left.key))


    by_loose: dict[tuple, list[Fixture]] = defaultdict(list)
    for fixture in fixtures:
        by_loose[(fixture.key.date, fixture.key.teams)].append(fixture)
    for (_, teams), group in by_loose.items():
        if len({f.venue for f in group}) < 2 or len(teams) != 2:
            continue
        for left, right in itertools.combinations(group, 2):
            pair_id = frozenset((id(left), id(right)))
            if left.venue == right.venue or pair_id in linked_pairs:
                continue
            reasons: list[str] = []
            genders = {left.key.gender, right.key.gender}
            formats = {left.key.fmt, right.key.fmt}
            if len(genders) > 1:
                reasons.append(f"gender mismatch {sorted(genders)}")
            if len(formats) > 1:
                reasons.append(f"format mismatch {sorted(formats)}")
            key = review_key(left, right)
            reasons.extend(_exact_group_problems(key, [left, right]))
            fm = _fixture_match_from_group(key, [left, right])
            fm.status, fm.confidence = "review", "low"
            fm.reason = ("; ".join(dict.fromkeys(reasons)) or
                         "same date and teams but unresolved metadata")
            review.append(fm)
            linked_pairs.add(pair_id)


    candidates: list[tuple[float, Fixture, Fixture, str]] = []
    for a, b in itertools.combinations(fixtures, 2):
        if frozenset((id(a), id(b))) in linked_pairs:
            continue
        if a.venue == b.venue or a.key.gender != b.key.gender:
            continue
        date_gap = _date_gap_days(a.key.date, b.key.date)
        if date_gap is None or date_gap > 1:
            continue
        if a.key.fmt and b.key.fmt and a.key.fmt != b.key.fmt:
            continue
        is_candidate, score, evidence = _recall_candidate(a, b)
        if not is_candidate:
            continue
        discrepancies: list[str] = []
        if date_gap:
            discrepancies.append(f"published dates differ by {date_gap} day")
        if a.key.fmt != b.key.fmt:
            discrepancies.append(f"format mismatch {sorted({a.key.fmt, b.key.fmt})}")
        competitions = {_norm_competition(a.competition),
                        _norm_competition(b.competition)} - {""}
        if len(competitions) > 1:
            discrepancies.append(f"competition mismatch {sorted(competitions)}")
        ta, tb = _time_minutes(a.start_time), _time_minutes(b.start_time)
        if ta is not None and tb is not None and _time_gap(ta, tb) > 30:
            discrepancies.append("published start times differ by more than 30 minutes")
        discrepancies.extend(sorted({f.note for f in (a, b) if f.note}))
        detail = evidence
        if discrepancies:
            detail += "; " + "; ".join(discrepancies)
        candidates.append((score, a, b, detail))

    candidate_counts: dict[tuple[int, str], int] = defaultdict(int)
    for _, a, b, _ in candidates:
        candidate_counts[(id(a), b.venue)] += 1
        candidate_counts[(id(b), a.venue)] += 1
    for score, a, b, evidence in sorted(candidates, key=lambda x: -x[0]):
        reason = (f"probable team identity variant: {evidence} "
                  f"(weaker-team similarity {score:.2f})")
        if (candidate_counts[(id(a), b.venue)] > 1 or
                candidate_counts[(id(b), a.venue)] > 1):
            reason += "; multiple candidate counterparts"
        fm = _fixture_match_from_group(review_key(a, b), [a, b])
        fm.status, fm.confidence = "review", "low"
        fm.reason = reason
        review.append(fm)

    matched_fixture_ids = {
        id(f)
        for fm in accepted + review
        for f in fm.by_venue.values()
    }
    unmatched = [f for f in fixtures
                 if id(f) not in matched_fixture_ids]
    return accepted, review, unmatched


@dataclass
class MarketMatch:
    fixture: FixtureMatch
    market_type: str
    outcome: str
    venue_1: str
    m1: Market
    venue_2: str
    m2: Market
    status: str = "accepted"
    confidence: str = "high"
    reason: str = ""


def _out_key(market: Market) -> str:
    value = (market.outcome or "").strip().lower()
    if value in {"draw", "tie", "yes", "no"}:
        return "draw" if value == "tie" else value
    return norm_team(value)


def match_markets(fixture_matches: list[FixtureMatch], markets: list[Market]
                  ) -> tuple[list[MarketMatch], list[MarketMatch], list[Market]]:
    by_fixture: dict[str, list[Market]] = defaultdict(list)
    for market in markets:
        if market.venue_fixture_id:
            by_fixture[market.venue_fixture_id].append(market)

    accepted: list[MarketMatch] = []
    review: list[MarketMatch] = []
    used: set[int] = set()

    for fixture_match in fixture_matches:
        pool: list[Market] = []
        for fixture in fixture_match.by_venue.values():
            pool.extend(by_fixture.get(fixture.venue_fixture_id, []))
        grouped: dict[tuple[str, str], list[Market]] = defaultdict(list)
        for market in pool:
            grouped[(market.market_type, _out_key(market))].append(market)

        for (market_type, outcome), group in grouped.items():
            by_venue: dict[str, list[Market]] = defaultdict(list)
            for market in group:
                by_venue[market.venue].append(market)
            if len(by_venue) < 2:
                continue
            for venue_1, venue_2 in itertools.combinations(sorted(by_venue), 2):
                left, right = by_venue[venue_1], by_venue[venue_2]
                m1, m2 = left[0], right[0]
                mm = MarketMatch(
                    fixture=fixture_match, market_type=market_type,
                    outcome=outcome, venue_1=venue_1, m1=m1,
                    venue_2=venue_2, m2=m2)
                problems: list[str] = []
                if len(left) != 1 or len(right) != 1:
                    problems.append("duplicate canonical market on a venue")
                if market_type == UNKNOWN:
                    problems.append("uncertain market subtype")
                if market_type in PARAMETERISED_TYPES:
                    left_params = {str(m.extra.get("canonical_parameters") or "")
                                   for m in left}
                    right_params = {str(m.extra.get("canonical_parameters") or "")
                                    for m in right}
                    if (left_params != right_params or not left_params or
                            left_params == {""}):
                        problems.append(
                            "parameterised market lacks equal canonical line/side parameters")
                if not outcome:
                    problems.append("empty outcome")
                if fixture_match.status != "accepted":
                    problems.append("fixture is unresolved")
                problems.extend(sorted({m.note for m in (m1, m2) if m.note}))
                if problems:
                    mm.status, mm.confidence = "review", "low"
                    mm.reason = "; ".join(dict.fromkeys(problems))
                    review.append(mm)
                else:
                    mm.reason = ("confirmed fixture; canonical market type and "
                                 f"outcome agree ({market_type}/{outcome})")
                    if "trusted competition/date-scoped" in fixture_match.reason:
                        mm.reason += "; " + fixture_match.reason
                    accepted.append(mm)
                used.update(id(m) for m in left + right)

    unmatched = [m for m in markets
                 if id(m) not in used and m.market_type != OUTRIGHT]
    return accepted, review, unmatched
