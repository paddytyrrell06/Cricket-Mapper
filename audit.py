# This file checks the completed live result for duplicates, invalid links, incomplete coverage, unresolved candidates, and other conditions that would make the report unsafe to publish.

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import canonical as C
import matching as M


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    severity: str = "critical"


VALID_STATUSES = {"Available", "Not currently open", "Coverage incomplete", "Needs review"}


def _time_minutes(value: str) -> Optional[int]:
    if not value or not re.fullmatch(r"\d{2}:\d{2}", value):
        return None
    hour, minute = map(int, value.split(":"))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _gap(a: int, b: int) -> int:
    distance = abs(a - b)
    return min(distance, 1440 - distance)


def _valid_public_url(venue: str, url: str, event_id: str = "") -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    if venue == "kalshi":

        parts = [p for p in path.split("/") if p]
        return (host == "kalshi.com" and len(parts) == 4
                and parts[0] == "markets"
                and parts[-1].lower() == str(event_id).lower())
    if venue == "polymarket_intl":
        return host == "polymarket.com" and path.startswith("/event/")
    if venue == "polymarket_us":
        return host == "polymarket.us" and path.startswith("/event/")
    return False


def _fixture_pair_id(match: M.FixtureMatch) -> tuple[str, str, str, str]:
    venues = sorted(match.by_venue)
    if len(venues) != 2:
        return ("", "", "", "")
    return (venues[0], match.by_venue[venues[0]].venue_fixture_id,
            venues[1], match.by_venue[venues[1]].venue_fixture_id)


def _market_pair_id(match: M.MarketMatch) -> tuple[str, ...]:
    left = (match.venue_1, match.m1.market_id)
    right = (match.venue_2, match.m2.market_id)
    pair = (*left, *right) if left <= right else (*right, *left)
    return (*pair, match.market_type, C.norm_team(match.outcome))


def audit_runtime(fixtures: list[C.Fixture], markets: list[C.Market],
                  fixture_accepted: list[M.FixtureMatch],
                  fixture_review: list[M.FixtureMatch],
                  fixture_unmatched: list[C.Fixture],
                  market_accepted: list[M.MarketMatch],
                  market_review: list[M.MarketMatch],
                  market_unmatched: list[C.Market],
                  wide_rows: list[dict[str, str]],
                  coverage_complete: dict[str, bool],
                  venues: list[str]) -> tuple[bool, str, list[Check]]:
    checks: list[Check] = []

    complete_venues = [v for v in venues if coverage_complete.get(v, False)]
    incomplete = [v for v in venues if not coverage_complete.get(v, False)]
    checks.append(Check(
        "at least two venue collections complete",
        len(complete_venues) >= 2,
        f"{len(complete_venues)}/{len(venues)} complete: "
        + (", ".join(complete_venues) if complete_venues else "none"),
    ))
    checks.append(Check("all venue coverage complete", not incomplete,
                        "complete" if not incomplete else ", ".join(incomplete),
                        "advisory"))
    checks.append(Check("no unresolved fixture links", not fixture_review,
                        f"{len(fixture_review)} review links", "advisory"))
    checks.append(Check("no unresolved market links", not market_review,
                        f"{len(market_review)} review links", "advisory"))

    fixture_ids = [_fixture_pair_id(x) for x in fixture_accepted]
    checks.append(Check("fixture links are unique",
                        len(fixture_ids) == len(set(fixture_ids)),
                        f"{len(fixture_ids)} links / {len(set(fixture_ids))} unique"))
    market_ids = [_market_pair_id(x) for x in market_accepted]
    checks.append(Check("market links are unique",
                        len(market_ids) == len(set(market_ids)),
                        f"{len(market_ids)} links / {len(set(market_ids))} unique"))

    bad_fixture_links: list[str] = []
    for match in fixture_accepted:
        if len(match.by_venue) != 2:
            bad_fixture_links.append(f"{match.canonical_fixture_id}: not pairwise")
            continue
        pair = list(match.by_venue.values())
        if pair[0].venue == pair[1].venue:
            bad_fixture_links.append(f"{match.canonical_fixture_id}: same venue")
        if pair[0].key != pair[1].key:
            bad_fixture_links.append(f"{match.canonical_fixture_id}: key disagreement")
        times = [_time_minutes(f.start_time) for f in pair]
        if all(t is not None for t in times) and _gap(times[0], times[1]) > 30:
            bad_fixture_links.append(
                f"{match.canonical_fixture_id}: time gap {_gap(times[0], times[1])}m")
    checks.append(Check("accepted fixture pairs agree exactly",
                        not bad_fixture_links,
                        "all pairs agree" if not bad_fixture_links
                        else "; ".join(bad_fixture_links[:10])))

    bad_market_links: list[str] = []
    for match in market_accepted:
        if match.fixture.status != "accepted":
            bad_market_links.append(f"{match.m1.market_id}: unresolved fixture")
        if match.venue_1 == match.venue_2:
            bad_market_links.append(f"{match.m1.market_id}: same venue")
        if match.m1.market_type != match.m2.market_type:
            bad_market_links.append(f"{match.m1.market_id}: type mismatch")
        if C.norm_team(match.m1.outcome) != C.norm_team(match.m2.outcome):
            bad_market_links.append(f"{match.m1.market_id}: outcome mismatch")
    checks.append(Check("accepted market pairs agree exactly",
                        not bad_market_links,
                        "all pairs agree" if not bad_market_links
                        else "; ".join(bad_market_links[:10])))


    row_keys: list[tuple[str, str, str]] = []
    bad_rows: list[str] = []
    per_fixture: dict[str, list[dict[str, str]]] = {}
    for row in wide_rows:
        key = (row.get("canonical_fixture_id", ""),
               row.get("canonical_market_type", ""),
               C.norm_team(row.get("canonical_outcome", "")))
        row_keys.append(key)
        per_fixture.setdefault(key[0], []).append(row)
        available = 0
        for venue in venues:
            status = row.get(f"{venue}_status", "")
            if status not in VALID_STATUSES:
                bad_rows.append(f"{key}: invalid {venue} status {status!r}")
            if status == "Available":
                available += 1
                if not row.get(f"{venue}_market_id"):
                    bad_rows.append(f"{key}: {venue} missing market id")
                if not _valid_public_url(
                        venue, row.get(f"{venue}_url", ""),
                        row.get(f"{venue}_event_id", "")):
                    bad_rows.append(f"{key}: {venue} invalid public URL")
            elif status == "Not currently open" and not coverage_complete.get(venue, False):
                bad_rows.append(f"{key}: false absence claim on {venue}")
        if available < 2:
            bad_rows.append(f"{key}: only {available} venues available")
    checks.append(Check("wide rows are unique",
                        len(row_keys) == len(set(row_keys)),
                        f"{len(row_keys)} rows / {len(set(row_keys))} unique"))
    checks.append(Check("wide table statuses, identifiers and links are valid",
                        not bad_rows,
                        "all rows valid" if not bad_rows
                        else "; ".join(bad_rows[:15])))

    bad_outcomes: list[str] = []
    for fixture_id, rows in per_fixture.items():
        winners = [r for r in rows
                   if r.get("canonical_market_type") == C.MATCH_WINNER]
        if not winners:
            continue
        outcomes = {C.norm_team(r.get("canonical_outcome", "")) for r in winners}
        teams = {C.norm_team(winners[0].get("team_a", "")),
                 C.norm_team(winners[0].get("team_b", ""))}
        allowed = teams | {"draw"}
        if not teams.issubset(outcomes) or not outcomes.issubset(allowed):
            bad_outcomes.append(
                f"{fixture_id}: winner outcomes={sorted(outcomes)}, "
                f"teams={sorted(teams)}")
    checks.append(Check("match-winner fixtures contain both teams and no unknown outcomes",
                        not bad_outcomes,
                        "all winner fixtures complete" if not bad_outcomes
                        else "; ".join(bad_outcomes[:10])))


    plausible: list[str] = []
    for left, right in itertools.combinations(fixture_unmatched, 2):
        if left.venue == right.venue:
            continue
        if left.key.gender != right.key.gender:
            continue
        date_gap = M._date_gap_days(left.key.date, right.key.date)
        if date_gap is None or date_gap > 1:
            continue
        if left.key.fmt and right.key.fmt and left.key.fmt != right.key.fmt:
            continue
        is_candidate, score, evidence = M._recall_candidate(
            left, right, threshold=0.65)
        if is_candidate:
            discrepancies: list[str] = []
            if date_gap:
                discrepancies.append(f"dates differ by {date_gap} day")
            competitions = {M._norm_competition(left.competition),
                            M._norm_competition(right.competition)} - {""}
            if len(competitions) > 1:
                discrepancies.append(f"competition mismatch {sorted(competitions)}")
            lt, rt = _time_minutes(left.start_time), _time_minutes(right.start_time)
            if lt is not None and rt is not None and _gap(lt, rt) > 30:
                discrepancies.append("start times differ by more than 30 minutes")
            detail = evidence + (("; " + "; ".join(discrepancies))
                                 if discrepancies else "")
            plausible.append(
                f"{score:.2f} ({detail}): {left.venue} "
                f"{left.team_a} v {left.team_b} <> "
                f"{right.venue} {right.team_a} v {right.team_b}")
    checks.append(Check("no plausible counterpart remains unmatched",
                        not plausible,
                        "none" if not plausible else "; ".join(plausible[:10])))


    accepted_keys = {match.key for match in fixture_accepted}
    hidden_markets: list[str] = []
    for market in market_unmatched:
        if (market.market_type == C.MATCH_WINNER and
                market.fixture_key in accepted_keys and
                C.norm_team(market.outcome)):
            hidden_markets.append(
                f"{market.venue} {market.market_id}: "
                f"{market.outcome} on confirmed fixture")
    checks.append(Check("no match-winner market is stranded on a confirmed fixture",
                        not hidden_markets,
                        "none" if not hidden_markets
                        else "; ".join(hidden_markets[:10])))


    expected_wide = {
        (m.fixture.canonical_fixture_id, m.market_type, C.norm_team(m.outcome))
        for m in market_accepted
    }
    actual_wide = set(row_keys)
    checks.append(Check("wide table is lossless relative to accepted links",
                        actual_wide == expected_wide,
                        f"expected={len(expected_wide)} actual={len(actual_wide)}"))

    passed = all(check.passed for check in checks if check.severity == "critical")
    lines = ["Cricket exchange matching live-run safety audit", ""]
    for check in checks:
        if check.passed:
            label = "PASS"
        elif check.severity == "advisory":
            label = "ADVISORY"
        else:
            label = "FAIL"
        lines.append(f"[{label}] {check.name}")
        lines.append(f"       {check.detail}")
    lines.extend([
        "",
        f"Objects: {len(fixtures)} fixtures, {len(markets)} markets",
        (f"Accepted: {len(fixture_accepted)} fixture-pair links, "
         f"{len(market_accepted)} market-pair links"),
        (f"Unmatched: {len(fixture_unmatched)} fixtures, "
         f"{len(market_unmatched)} markets"),
        f"OVERALL {'PASS' if passed else 'FAIL'}",
    ])
    return passed, "\n".join(lines) + "\n", checks


def write_report(path: Path, report: str) -> None:
    path.write_text(report, encoding="utf-8")
