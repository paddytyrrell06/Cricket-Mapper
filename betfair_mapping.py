"""Conservative NBA fixture/outcome links, independent of hedge verification.

Betfair runners remain native back/lay selections. A fixture link establishes
which Polymarket outcome to research; it never establishes equivalent monetary
payouts, complementary YES/NO exposure, or executable arbitrage.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

import canonical as C
import quotes as Q
import settlement as S
import sports


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _json(value, fallback):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value if value is not None else fallback


def _time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def _team(name, registry):
    normal = C.norm_team(str(name or ""))
    codes = [code for code, aliases in registry.aliases.items()
             if normal in aliases or normal == code.lower()]
    return registry.teams[codes[0]]["canonical"] if len(codes) == 1 else ""


def _handicap(value):
    try:
        number = Decimal(str(value))
        return format(number.normalize(), "f") if number.is_finite() else None
    except (ValueError, InvalidOperation, TypeError):
        return None


def _candidates(rows, assets, raw_markets, registry):
    """Prefer native Polymarket fixture metadata; retain unmatched runtime legs."""
    raw = raw_markets.get("polymarket_intl", []) if isinstance(raw_markets, dict) else raw_markets or []
    raw_by_id = defaultdict(dict)
    for record in raw:
        if isinstance(record, dict) and record.get("id"):
            # Repeated archive records are harmless only when identity/rules
            # agree. Live prices and collection clocks do not alter a mapping.
            material = {key: record.get(key) for key in (
                "id", "slug", "sportsMarketType", "outcomes", "clobTokenIds", "positionIds", "version",
                "gameStartTime", "_event_start_time", "_event_teams", "description", "resolutionSource",
                "conditionId", "condition_id", "active", "closed")}
            raw_by_id[str(record["id"])][_hash(material)] = record
    candidates = {}
    native_ids = set(raw_by_id)
    for market_id, versions in raw_by_id.items():
        if len(versions) != 1:
            continue
        record = next(iter(versions.values()))
        tokens = Q._json_list(record.get("positionIds") if record.get("version") == "v2" else record.get("clobTokenIds"))
        if len(tokens) != 2 or len(set(map(str, tokens))) != 2 or not all(str(token).isdigit() for token in tokens):
            continue
        fixtures, markets, _ = sports.parse_basketball({"polymarket_intl": [record]})
        fixture_by_id = {fixture.venue_fixture_id: fixture for fixture in fixtures}
        for market in markets:
            fixture = fixture_by_id[market.venue_fixture_id]
            if fixture.note or market.note:
                continue
            start = _time(fixture.key.date + "T" + fixture.start_time + ":00+00:00")
            if start is None:
                continue
            candidate = candidates.setdefault(market_id, {
                "market_id": market_id, "event_id": market.event_id, "fixture_id": market.venue_fixture_id,
                "title": " vs ".join(sorted(fixture.key.teams)), "teams": sorted(fixture.key.teams),
                "start_time": start.isoformat(), "condition_id": record.get("conditionId") or record.get("condition_id"),
                "outcomes": {}, "source": "native_polymarket_metadata"})
            asset = Q._intl_asset(market, record)
            published_asset = assets.get(("polymarket_intl", market_id, C.norm_team(market.outcome)))
            if asset and (not published_asset or str(published_asset) == asset):
                candidate["outcomes"][C.norm_team(market.outcome)] = {
                    "asset_id": asset, "profile": market.extra.get("settlement") or {}}
        if market_id in candidates and len(candidates[market_id]["outcomes"]) != 2:
            del candidates[market_id]

    grouped = defaultdict(list)
    for row in rows:
        market_id = str(row.get("polymarket_intl_market_id") or "")
        if (not market_id or market_id in native_ids or row.get("sport") != "basketball" or
                str(row.get("competition") or "").upper() != "NBA" or
                row.get("canonical_market_type") != C.MATCH_WINNER or
                row.get("polymarket_intl_status") != "Available" or
                str(row.get("polymarket_intl_market_status") or "").lower() in {"closed", "settled", "resolved"}):
            continue
        grouped[market_id].append(row)
    for market_id, group in grouped.items():
        identities = set()
        outcomes = defaultdict(dict)
        for row in group:
            teams = sorted(C.norm_team(_team(row.get(key), registry)) for key in ("team_a", "team_b"))
            start = _time(str(row.get("fixture_date")) + "T" + str(row.get("start_time_utc")) + "+00:00")
            if not all(teams) or len(set(teams)) != 2 or start is None:
                continue
            identities.add((tuple(teams), start.isoformat(), row.get("canonical_fixture_id")))
            outcome = C.norm_team(_team(row.get("canonical_outcome"), registry))
            asset = assets.get(("polymarket_intl", market_id, C.norm_team(row.get("canonical_outcome"))))
            if not outcome or not asset or outcome not in teams:
                continue
            profile = {"sport": "basketball", "target_kind": "team",
                       **{part: _json(row.get("polymarket_intl_settlement_" + part), {} if part != "evidence" else [])
                          for part in ("payouts", "policies", "evidence")}}
            value = {"asset_id": str(asset), "profile": profile}
            outcomes[outcome][_hash(value)] = value
        if len(identities) != 1 or any(len(values) != 1 for values in outcomes.values()):
            continue
        teams, start, fixture_id = next(iter(identities))
        candidates[market_id] = {"market_id": market_id, "event_id": group[0].get("polymarket_intl_event_id"),
            "fixture_id": fixture_id, "title": " vs ".join(teams), "teams": list(teams), "start_time": start,
            "condition_id": None, "outcomes": {key: next(iter(values.values())) for key, values in outcomes.items()},
            "source": "published_runtime_mapping"}
    # A shared token cannot identify two mutually exclusive team outcomes.
    return [candidate for candidate in candidates.values()
            if candidate["outcomes"] and len({item["asset_id"] for item in candidate["outcomes"].values()}) == len(candidate["outcomes"])]


def map_markets(markets, rows, assets=None, raw_markets=None, *, max_start_delta_seconds=900):
    """Return every runner with an exact native identity and conservative link.

    Inputs are normalized Betfair catalogues and existing runtime research rows.
    Optional original Gamma records also discover single-venue counterparts and
    provide authoritative token order, condition IDs and scheduled start times.
    The fifteen-minute tolerance proposes a fixture link only; settlement and
    economic equivalence remain explicitly unverified in this milestone.
    """
    if not 0 <= max_start_delta_seconds <= 900:
        raise ValueError("start tolerance must be between zero and 900 seconds")
    registry = sports.TeamRegistry()
    candidates = _candidates(rows, assets or {}, raw_markets, registry)
    result = []
    counts = Counter((str(market.get("market_id")), str(runner.get("selection_id")))
                     for market in markets for runner in market.get("runners") or [])
    for market in markets:
        runners = market.get("runners") or []
        teams = [_team(runner.get("runner_name") or runner.get("name"), registry) for runner in runners]
        start = _time(market.get("start_time"))
        problems = []
        competition = market.get("competition") or {}
        if (str(market.get("sport") or "").lower() != "basketball" or
                str(market.get("league") or "").upper() != "NBA" or
                str(competition.get("name") or "").upper() not in {"NBA", "NATIONAL BASKETBALL ASSOCIATION"}):
            problems.append("Only verified NBA basketball competitions are supported")
        scope = str(market.get("market_name") or "")
        if market.get("market_type") != "MATCH_ODDS" or re.search(r"\b(?:half|quarter|regulation|handicap)\b", scope, re.I):
            problems.append("Only full-game MATCH_ODDS markets are supported")
        if len(runners) != 2 or not all(teams) or len(set(teams)) != 2:
            problems.append("Exactly two distinct teams must resolve through the NBA registry")
        if start is None:
            problems.append("A timezone-aware scheduled start is required")
        if any(_handicap(runner.get("handicap")) != "0" for runner in runners):
            problems.append("Full-game winner runners require an explicit zero handicap")
        if not market.get("market_id") or not market.get("event_id"):
            problems.append("Native market and event identifiers are required")
        possible = [] if problems else [candidate for candidate in candidates
            if candidate["teams"] == sorted(C.norm_team(team) for team in teams) and
            abs((_time(candidate["start_time"]) - start).total_seconds()) <= max_start_delta_seconds]
        material = {key: market.get(key) for key in ("market_id", "event_id", "competition", "event_type", "sport", "league",
                    "market_name", "start_time", "market_type", "rules", "description")}
        material["runners"] = [{key: runner.get(key) for key in ("selection_id", "handicap", "runner_name", "name")} for runner in runners]
        for runner, team in zip(runners, teams):
            selection_id = str(runner.get("selection_id") or "")
            handicap = _handicap(runner.get("handicap"))
            key = [str(market.get("market_id") or ""), selection_id, handicap]
            reasons = list(problems)
            if not selection_id or counts[(key[0], selection_id)] > 1:
                reasons.append("Runner selection identifiers are missing or duplicated")
            state = "rejected" if reasons else "ambiguous" if len(possible) > 1 else "unmatched"
            if not reasons:
                reasons.append("Multiple Polymarket fixtures satisfy the identity/time checks" if len(possible) > 1
                               else "No Polymarket fixture satisfies the exact NBA identity/time checks")
            item = {"key": key, "mapping_id": _hash(key)[:24], "betfair_market_id": key[0], "selection_id": selection_id,
                    "handicap": handicap, "runner_name": runner.get("runner_name") or runner.get("name") or "",
                    "canonical_outcome": team or None, "state": state, "status": state,
                    "pm_market_id": None, "pm_asset_id": None, "pm_condition_id": None, "pm_event_id": None,
                    "fixture_id": None, "fixture_title": None, "start_delta_seconds": None,
                    "settlement_status": "unverified", "normal_result_status": "unknown", "arbitrage_eligible": False,
                    "candidate_market_ids": [candidate["market_id"] for candidate in possible],
                    "evidence": {"team_registry": registry.source, "betfair": material,
                                 "polymarket_candidates": possible, "max_start_delta_seconds": max_start_delta_seconds}}
            if not problems and len(possible) == 1 and selection_id and counts[(key[0], selection_id)] == 1:
                candidate = possible[0]
                outcome = candidate["outcomes"].get(C.norm_team(team))
                if outcome:
                    profile = S.profile_market("betfair", {"description": str(market.get("rules") or "")},
                                               sport="basketball", outcome=team)
                    comparison = S.compare_profiles(profile, outcome["profile"])
                    normal = {"compatible": "aligned", "unknown": "unknown", "incompatible": "incompatible"}[comparison["status"]]
                    state = "rejected" if normal == "incompatible" else "fixture_linked"
                    reasons = ["Exact NBA participants, unique fixture within the UTC time tolerance and exact outcome token",
                               "Betfair stake, void/refund and commission settlement remains unverified", comparison["reason"]]
                    item.update(state=state, status=state, pm_market_id=candidate["market_id"],
                        pm_asset_id=outcome["asset_id"], pm_condition_id=candidate["condition_id"],
                        pm_event_id=candidate["event_id"], fixture_id=candidate["fixture_id"], fixture_title=candidate["title"],
                        start_delta_seconds=abs((_time(candidate["start_time"]) - start).total_seconds()),
                        normal_result_status=normal, settlement_status="incompatible" if normal == "incompatible" else "unverified")
                    item["evidence"]["normal_result_comparison"] = comparison
                else:
                    reasons = ["Fixture found, but an exact token for this runner outcome is unavailable; no complementary token inferred"]
            item["reasons"] = reasons
            item["reason"] = "; ".join(reasons)
            item["fingerprint"] = _hash(item["evidence"])
            result.append(item)
    linked = defaultdict(set)
    for item in result:
        if item["state"] == "fixture_linked":
            linked[item["pm_asset_id"]].add(item["betfair_market_id"])
    for item in result:
        if item["state"] == "fixture_linked" and len(linked[item["pm_asset_id"]]) > 1:
            item.update(state="ambiguous", status="ambiguous", reason="Multiple Betfair fixtures map to the same Polymarket outcome",
                        reasons=["Multiple Betfair fixtures map to the same Polymarket outcome"])
        item["fingerprint"] = _hash({"evidence": item["evidence"], "state": item["state"],
                                     "reason": item["reason"], "settlement_status": item["settlement_status"]})
    return result
