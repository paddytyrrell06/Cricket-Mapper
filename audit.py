# This file checks the completed live result for duplicates, invalid links, incomplete coverage, unresolved candidates, and other conditions that would make the report unsafe to publish.

from __future__ import annotations

import itertools
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import canonical as C
import matching as M
import settlement as S
import fees as F


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


def _net_check(row: dict, pairs: list[tuple], eligible: bool, label: str) -> list[str]:
    """Reprice the selected fee route and verify it wins among fresh routes."""
    if "net_crossed" not in row:
        return []
    errors = []
    if not eligible:
        if (row.get("net_crossed") != "false" or row.get("net_cross_eligible") != "false" or
                row.get("net_fee_status") != "ineligible" or row.get("net_gap")):
            errors.append(f"{label}: net edge on an ineligible comparison")
        return errors
    estimates = [(F.calculate_taker_cross(row, buy, sell), sell, buy)
                 for _, sell, buy, _, _ in pairs]
    known = [item for item in estimates if item[0]["status"] == "estimated" and
             item[0].get("net_gap") is not None]
    selected = next((item for item in estimates if item[1:] ==
                     (row.get("net_sell_venue"), row.get("net_buy_venue"))), None)
    if selected is None:
        return [f"{label}: net edge uses a missing, stale, or same-venue route"]
    estimate = selected[0]
    expected = {"net_cross_eligible": str(bool(known)).lower(),
                "net_crossed": str(estimate.get("net_positive") is True and bool(known)).lower(),
                "net_depth_confirmed": str(estimate.get("depth_confirmed") is True).lower(),
                "net_fee_status": estimate["status"]}
    if known and (estimate["status"] != "estimated" or
                  Decimal(estimate["net_gap"]) != max(Decimal(item[0]["net_gap"]) for item in known)):
        errors.append(f"{label}: selected fee route does not have the best known net edge")
    for field, value in expected.items():
        if row.get(field) != value:
            errors.append(f"{label}: {field} disagrees with current fee parameters")
    for field, key in (("net_gap", "net_gap"), ("net_total", "net_total"),
                       ("net_buy_fee", "buy_fee"), ("net_sell_fee", "sell_fee"),
                       ("net_quantity", "quantity"), ("net_total_fees", "total_fees"),
                       ("net_top_size", "top_size"), ("net_top_quantity", "top_quantity"),
                       ("net_top_total", "top_net_total"), ("net_top_gap", "top_net_gap"),
                       ("net_gross_gap", "gross_gap")):
        actual, expected_number = row.get(field, ""), estimate.get(key)
        try:
            valid = actual == "" if expected_number is None else (
                isinstance(actual, str) and Decimal(actual).is_finite() and
                Decimal(actual) == Decimal(expected_number))
        except (InvalidOperation, ValueError, TypeError):
            valid = False
        if not valid:
            errors.append(f"{label}: {field} disagrees with fee/depth calculation")
    return errors


def _quote_checks(rows: list[dict[str, str]], venues: list[str],
                  as_of: Optional[datetime] = None) -> list[Check]:
    priced_rows = [row for row in rows if any(f"{venue}_bid" in row for venue in venues)]
    if not priced_rows:
        return []
    invalid: list[str] = []
    inconsistent: list[str] = []
    crossed_count = 0
    quoted_count = 0
    mapped_count = 0
    fee_inconsistent: list[str] = []

    def price(value: Any, label: str) -> Optional[Decimal]:
        if value == "":
            return None
        try:
            parsed = Decimal(value) if isinstance(value, str) else None
        except InvalidOperation:
            parsed = None
        if parsed is None or not parsed.is_finite() or not Decimal(0) <= parsed <= Decimal(1):
            invalid.append(f"{label}: invalid probability {value!r}")
            return None
        return parsed

    def number_text(value: Optional[Decimal]) -> str:
        return format(value.normalize(), "f") if value is not None else ""

    for index, row in enumerate(priced_rows, start=1):
        label = f"row {index} ({row.get('canonical_outcome', '')})"
        bids: list[tuple[str, Decimal]] = []
        asks: list[tuple[str, Decimal]] = []
        local_cross = False
        for venue in venues:
            prefix = f"{label} {venue}"
            bid = price(row.get(f"{venue}_bid", ""), f"{prefix} bid")
            ask = price(row.get(f"{venue}_ask", ""), f"{prefix} ask")
            for side, value in (("bid", bid), ("ask", ask)):
                quantity = row.get(f"{venue}_{side}_size", "")
                if quantity != "":
                    try:
                        depth = Decimal(quantity) if isinstance(quantity, str) else None
                        if depth is None or not depth.is_finite() or depth < 0 or value is None or depth == 0:
                            raise ValueError
                    except (InvalidOperation, ValueError):
                        invalid.append(f"{prefix}: invalid {side} depth or depth without price")
            if bid is not None:
                bids.append((venue, bid))
            if ask is not None:
                asks.append((venue, ask))
            mapped = row.get(f"{venue}_status") == "Available"
            status = row.get(f"{venue}_quote_status", "")
            source = row.get(f"{venue}_quote_source", "")
            quote_time = row.get(f"{venue}_quote_time", "")
            if not isinstance(source, str) or not isinstance(quote_time, str):
                invalid.append(f"{prefix}: quote source and time must be strings")
            if quote_time:
                try:
                    stamp = datetime.fromisoformat(str(quote_time).replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        raise ValueError("timezone missing")
                except ValueError:
                    invalid.append(f"{prefix}: invalid quote time {quote_time!r}")
            both = bid is not None and ask is not None
            any_price = bid is not None or ask is not None
            if mapped:
                mapped_count += 1
                quoted_count += int(both)
                expected_statuses = ({"available"} if both else {"one-sided"} if any_price
                                     else {"unavailable", "empty"})
                if status not in expected_statuses:
                    invalid.append(f"{prefix}: quote status {status!r} disagrees with prices")
                if (any_price or status == "empty") and not (isinstance(source, str) and source.strip()):
                    invalid.append(f"{prefix}: observed quote has no source")
            elif any(row.get(f"{venue}_{field}", "") != ""
                     for field in ("bid", "ask", "quote_status", "quote_source", "quote_time", "crossed")):
                invalid.append(f"{prefix}: quote data on a market that is not Available")
            is_crossed = both and bid > ask
            local_cross = local_cross or is_crossed
            expected_crossed = str(is_crossed).lower() if mapped else ""
            if row.get(f"{venue}_crossed", "") != expected_crossed:
                inconsistent.append(f"{prefix}: incorrect crossed flag")

        bid_venue, best_bid = max(bids, key=lambda item: item[1]) if bids else ("", None)
        ask_venue, best_ask = min(asks, key=lambda item: item[1]) if asks else ("", None)
        across_cross = any(bid > ask for bv, bid in bids for av, ask in asks if bv != av)
        is_crossed = local_cross or across_cross
        kinds = (["within venue"] if local_cross else []) + (["across venues"] if across_cross else [])
        spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
        expected = {
            "best_bid": number_text(best_bid), "best_ask": number_text(best_ask),
            "best_bid_venue": bid_venue, "best_ask_venue": ask_venue,
            "best_spread": number_text(spread), "crossed": str(is_crossed).lower(),
            "cross_kind": " and ".join(kinds),
            "cross_gap": number_text(-spread if is_crossed and spread is not None else None),
        }
        if "comparison_status" in row:
            expected["observed_crossed"] = expected.pop("crossed")
            expected["observed_cross_kind"] = expected.pop("cross_kind")
            expected["observed_cross_gap"] = expected.pop("cross_gap")
            pairs, problems = [], []
            reference = as_of or datetime.now(timezone.utc)
            invalid_books = {venue for venue, bid in bids for ask_venue, ask in asks
                             if venue == ask_venue and bid > ask}
            if invalid_books:
                problems.append("invalid_book")
            for bid_venue, bid in bids:
                for ask_venue, ask in asks:
                    if bid_venue == ask_venue:
                        continue
                    if bid_venue in invalid_books or ask_venue in invalid_books:
                        problems.append("invalid_book")
                        continue
                    stamps = []
                    for venue in (bid_venue, ask_venue):
                        try:
                            stamp = datetime.fromisoformat(row.get(f"{venue}_quote_time", "").replace("Z", "+00:00"))
                            stamps.append(stamp if stamp.tzinfo is not None else None)
                        except ValueError:
                            stamps.append(None)
                    if any(stamp is None for stamp in stamps):
                        problems.append("unknown")
                        continue
                    ages = [(reference - stamp).total_seconds() for stamp in stamps]
                    if min(ages) < 0:
                        problems.append("future")
                    elif max(ages) > 300:
                        problems.append("stale")
                    elif abs((stamps[0] - stamps[1]).total_seconds()) > 30:
                        problems.append("skewed")
                    else:
                        pairs.append((bid - ask, bid_venue, ask_venue, bid, ask))
            quote_check = "fresh" if pairs else next((name for name in ("invalid_book", "future", "stale", "skewed", "unknown")
                                                       if name in problems), "unpriced")
            compatible = row.get("comparison_status") == "compatible" and row.get("mapping_status") == "accepted"
            profiles = []
            for venue in venues:
                if row.get(f"{venue}_status") != "Available":
                    continue
                try:
                    profiles.append({"sport": row.get("sport", "cricket"),
                                     "target_kind": "draw" if C.norm_team(row.get("canonical_outcome", "")) in {"draw", "tie"} else "team",
                                     "status": row.get(f"{venue}_settlement_status", "unknown"),
                                     "payouts": json.loads(row.get(f"{venue}_settlement_payouts", "{}")),
                                     "policies": json.loads(row.get(f"{venue}_settlement_policies", "{}")),
                                     "evidence": json.loads(row.get(f"{venue}_settlement_evidence", "[]"))})
                except (ValueError, TypeError):
                    invalid.append(f"{label}: invalid settlement evidence JSON")
            comparison_basis = row.get("comparison_basis", "normal_completion")
            if comparison_basis not in {"normal_completion", "full_contract"}:
                invalid.append(f"{label}: invalid settlement comparison basis")
            comparisons = [S.compare_profiles(left, right, strict=comparison_basis == "full_contract")
                           for left, right in itertools.combinations(profiles, 2)]
            strict_results = [S.compare_profiles(left, right, strict=True)
                              for left, right in itertools.combinations(profiles, 2)]
            if compatible and (len(profiles) < 2 or any(
                    result["status"] != "compatible" for result in comparisons)):
                invalid.append(f"{label}: compatible row has unverified or differing settlement profiles")
            if "contingency_status" in row:
                contingency = ("different" if any(result.get("contingency_status") == "different" for result in comparisons)
                               else "unknown" if not comparisons or any(result.get("contingency_status") != "aligned" for result in comparisons)
                               else "aligned")
                expected["contingency_status"] = contingency
                caveats = list(dict.fromkeys(str(caveat) for result in comparisons for caveat in result.get("caveats", [])))
                try:
                    if json.loads(row.get("contingency_caveats", "[]")) != caveats:
                        invalid.append(f"{label}: contingency caveats disagree with supporting rules")
                except (ValueError, TypeError):
                    invalid.append(f"{label}: invalid contingency caveat JSON")
                expected["strict_comparison_status"] = (
                    "rules_differ" if any(result["status"] == "incompatible" for result in strict_results)
                    else "rules_unknown" if not strict_results or any(result["status"] != "compatible" for result in strict_results)
                    else "compatible")
            eligible = compatible and bool(pairs)
            best = max(pairs, key=lambda pair: pair[0]) if eligible else None
            is_crossed = best is not None and best[0] > 0
            expected.update({"cross_eligible": str(eligible).lower(), "quote_check": quote_check,
                             "crossed": str(is_crossed).lower(), "cross_kind": "across venues" if is_crossed else "",
                             "cross_gap": number_text(best[0] if is_crossed else None),
                             "eligible_bid": number_text(best[3]) if best else "",
                             "eligible_ask": number_text(best[4]) if best else "",
                             "eligible_bid_venue": best[1] if best else "",
                             "eligible_ask_venue": best[2] if best else ""})
            if "cross_condition" in row:
                expected["cross_condition"] = ("conditional" if row.get("contingency_status") != "aligned" else "aligned") if is_crossed else ""
            fee_inconsistent.extend(_net_check(row, pairs, eligible, label))
        crossed_count += int(is_crossed)
        for field, value in expected.items():
            if row.get(field, "") != value:
                inconsistent.append(f"{label}: {field} should be {value!r}")

    return [
        Check("quote prices, statuses and sources are valid", not invalid,
              "; ".join(invalid[:15]) if invalid else "all quote fields valid"),
        Check("quote summaries and crossed flags agree", not inconsistent,
              "; ".join(inconsistent[:15]) if inconsistent else "all summaries agree"),
        Check("net edges agree with taker fees and top-level depth", not fee_inconsistent,
              "; ".join(fee_inconsistent[:15]) if fee_inconsistent else "all net estimates agree"),
        Check("no crossed quotes observed", crossed_count == 0,
              f"{crossed_count} crossed outcome rows" if crossed_count else "none", "advisory"),
        Check("all mapped outcomes have bid and ask", quoted_count == mapped_count,
              f"{quoted_count}/{mapped_count} mapped venue outcomes have two-sided quotes", "advisory"),
    ]


def audit_runtime(fixtures: list[C.Fixture], markets: list[C.Market],
                  fixture_accepted: list[M.FixtureMatch],
                  fixture_review: list[M.FixtureMatch],
                  fixture_unmatched: list[C.Fixture],
                  market_accepted: list[M.MarketMatch],
                  market_review: list[M.MarketMatch],
                  market_unmatched: list[C.Market],
                  wide_rows: list[dict[str, str]],
                  coverage_complete: dict[str, bool],
                  venues: list[str], *,
                  sport_coverage: Optional[dict[str, dict[str, bool]]] = None,
                  as_of: Optional[datetime] = None
                  ) -> tuple[bool, str, list[Check]]:
    checks: list[Check] = []

    complete_venues = [v for v in venues if coverage_complete.get(v, False)]
    incomplete = [v for v in venues if not coverage_complete.get(v, False)]
    complete_sports = [sport for sport, states in (sport_coverage or {}).items()
                       if sum(bool(states.get(venue)) for venue in venues) >= 2]
    checks.append(Check(
        "at least two venue collections complete",
        bool(complete_sports) if sport_coverage is not None else len(complete_venues) >= 2,
        f"{len(complete_venues)}/{len(venues)} complete: "
        + (", ".join(complete_venues) if complete_venues else "none")
        + (f"; complete sport scopes: {', '.join(complete_sports) or 'none'}" if sport_coverage is not None else ""),
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
        compatibility = S.compare_profiles(match.m1.extra.get("settlement"),
                                            match.m2.extra.get("settlement"))
        if compatibility["status"] != "compatible":
            bad_market_links.append(f"{match.m1.market_id}: settlement {compatibility['status']}")
    checks.append(Check("accepted market pairs agree exactly",
                        not bad_market_links,
                        "all pairs agree" if not bad_market_links
                        else "; ".join(bad_market_links[:10])))


    row_keys: list[tuple[str, str, str]] = []
    bad_rows: list[str] = []
    per_fixture: dict[str, list[dict[str, str]]] = {}
    fixture_anchors: dict[tuple[str, str], set[str]] = defaultdict(set)
    for match in fixture_accepted:
        for fixture in match.by_venue.values():
            fixture_anchors[(fixture.venue, fixture.venue_fixture_id)].add(
                match.canonical_fixture_id)
    reviewed_venues: dict[str, set[str]] = defaultdict(set)
    for match in fixture_review:
        anchors = {match.canonical_fixture_id}
        for fixture in match.by_venue.values():
            anchors.update(fixture_anchors.get((fixture.venue, fixture.venue_fixture_id), set()))
        for anchor in anchors:
            reviewed_venues[anchor].update(match.by_venue)
    for row in wide_rows:
        key = (row.get("canonical_fixture_id", ""),
               row.get("canonical_market_type", ""),
               C.norm_team(row.get("canonical_outcome", "")))
        row_keys.append(key)
        per_fixture.setdefault(key[0], []).append(row)
        row_coverage = (sport_coverage or {}).get(row.get("sport", "cricket"), coverage_complete)
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
            elif status == "Not currently open" and not row_coverage.get(venue, False):
                bad_rows.append(f"{key}: false absence claim on {venue}")
            elif (status == "Not currently open" and
                  venue in reviewed_venues.get(key[0], set())):
                bad_rows.append(f"{key}: unresolved fixture candidate on {venue}")
        if available < 2:
            bad_rows.append(f"{key}: only {available} venues available")
    checks.append(Check("wide rows are unique",
                        len(row_keys) == len(set(row_keys)),
                        f"{len(row_keys)} rows / {len(set(row_keys))} unique"))
    checks.append(Check("wide table statuses, identifiers and links are valid",
                        not bad_rows,
                        "all rows valid" if not bad_rows
                        else "; ".join(bad_rows[:15])))
    checks.extend(_quote_checks(wide_rows, venues, as_of=as_of))

    bad_outcomes: list[str] = []
    for fixture_id, rows in per_fixture.items():
        winners = [r for r in rows
                   if r.get("canonical_market_type") == C.MATCH_WINNER
                   and r.get("comparison_status", "compatible") == "compatible"]
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
        if left.key.sport != right.key.sport:
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


    markets_by_fixture: dict[tuple[str, str], list[C.Market]] = defaultdict(list)
    confirmed_counterparts: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for market in markets:
        markets_by_fixture[(market.venue, market.venue_fixture_id)].append(market)
    for match in fixture_accepted:
        sources = [(fixture.venue, fixture.venue_fixture_id)
                   for fixture in match.by_venue.values()]
        for source in sources:
            confirmed_counterparts[source].update(other for other in sources if other != source)
    hidden_markets: list[str] = []
    for market in market_unmatched:
        outcome = M._out_key(market)
        if market.market_type != C.MATCH_WINNER or not outcome:
            continue
        # An outcome offered by only one venue (for example a draw) is legitimately
        # unmatched. Only a published counterpart on a confirmed pair is stranded.
        counterparts = confirmed_counterparts.get((market.venue, market.venue_fixture_id), set())
        if any(candidate.market_type == C.MATCH_WINNER and M._out_key(candidate) == outcome
               for source in counterparts for candidate in markets_by_fixture.get(source, [])):
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
    actual_wide = {(row.get("canonical_fixture_id", ""),
                    row.get("canonical_market_type", ""),
                    C.norm_team(row.get("canonical_outcome", ""))) for row in wide_rows
                   if row.get("comparison_status", "compatible") == "compatible"}
    review_wide = {(m.fixture.canonical_fixture_id, m.market_type, C.norm_team(m.outcome))
                   for m in market_review}
    unexplained_rows = set(row_keys) - expected_wide - review_wide
    checks.append(Check("wide table is lossless relative to accepted links",
                        actual_wide == expected_wide and not unexplained_rows,
                        f"expected={len(expected_wide)} actual={len(actual_wide)}"))

    passed = all(check.passed for check in checks if check.severity == "critical")
    lines = ["Sports market terminal safety audit", ""]
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
