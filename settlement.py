"""Evidence-based payout comparison. Missing rules never establish equivalence.

State names describe the event result relative to the selected team. A draw
contract instead pays for the draw state. Conditional abandonment/forfeit states
are separate because a toss, first ball, and declared winner can change payment.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any


CRICKET_STATES = (
    "win", "loss", "draw", "tie_without_tiebreak", "no_result_after_start",
    "cancelled_before_start", "abandoned_before_toss_no_winner",
    "abandoned_after_toss_before_ball_no_winner", "abandoned_after_start_no_winner",
    "forfeit_before_start_win", "forfeit_before_start_loss",
    "forfeit_before_start_no_winner", "forfeit_after_start_win",
    "forfeit_after_start_loss", "void",
)
NBA_STATES = ("win", "loss", "cancelled_before_start",
              "abandoned_after_start_no_winner", "forfeit_before_start_win",
              "forfeit_before_start_loss", "forfeit_after_start_win",
              "forfeit_after_start_loss", "void")
CRICKET_POLICIES = ("period", "tiebreakers", "postponement", "dls")
NBA_POLICIES = ("period", "overtime", "postponement")


def resolution_evidence(raw: dict) -> dict:
    evidence = {key: str(raw.get(key) or "") for key in
                ("rules_primary", "rules_secondary", "description", "resolutionSource")}
    documents = raw.get("_settlement_documents") or []
    evidence["documents"] = [dict(item) for item in documents if isinstance(item, dict)]
    return evidence


def _states(sport: str) -> tuple:
    return NBA_STATES if sport == "basketball" else CRICKET_STATES


def _policies(sport: str) -> tuple:
    return NBA_POLICIES if sport == "basketball" else CRICKET_POLICIES


def _fixed_payout(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and 0 <= number <= 1 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _valid_payout(value: Any) -> bool:
    return (value in ("fair_market_price", "last_fair_market_price", "refund") or
            _fixed_payout(value) is not None)


def _normalized_payout(value: Any) -> Any:
    number = _fixed_payout(value)
    if number is not None:
        return "0" if number == 0 else format(number.normalize(), "f")
    return None if value is None else str(value)


def make_profile(*, sport: str, target_kind: str, payouts: dict, policies: dict,
                 evidence: list, label: str = "Settlement requires review") -> dict:
    """Build a checked profile; callers must provide evidence for every value."""
    payouts = {key: _normalized_payout(value) for key, value in payouts.items()}
    missing = [f"payout:{key}" for key in sorted(set(_states(sport)) | set(payouts))
               if _fixed_payout(payouts.get(key)) is None]
    missing.extend(f"policy:{key}" for key in _policies(sport)
                   if not policies.get(key) or policies[key] == "unknown")
    if not evidence:
        missing.append("resolution evidence")
    if sport not in {"cricket", "basketball"} or target_kind not in {"team", "draw"}:
        missing.append("supported sport/target")
    signature = json.dumps({"sport": sport, "target_kind": target_kind,
                            "payouts": payouts, "policies": policies}, sort_keys=True)
    return {"profile_id": hashlib.sha256(signature.encode()).hexdigest()[:16],
            "status": "unknown" if missing else "known", "sport": sport,
            "target_kind": target_kind, "payouts": payouts, "policies": policies,
            "evidence": evidence, "missing": missing, "label": label}


def profile_market(venue: str, raw: dict, *, sport: str = "cricket", outcome: str = "",
                   binary_target: bool = False, market_type: str = "match_winner") -> dict:
    """Extract only supported, explicit rule clauses; all other branches stay unknown."""
    target = "draw" if outcome.strip().lower() in {"draw", "tie", "draw/tie"} else "team"
    payouts = {key: None for key in _states(sport)}
    policies = {key: None for key in _policies(sport)}
    evidence = resolution_evidence(raw)
    parts = [(key, text) for key, text in evidence.items() if isinstance(text, str) and text]
    parts.extend((str(doc.get("url") or "rule document"), str(doc.get("text") or ""))
                 for doc in evidence["documents"] if doc.get("text"))
    citations = [{"field": key, "text": value} for key, value in parts]
    text = re.sub(r"\s+", " ", " ".join(value for _, value in parts)).lower()
    label = "Settlement requires review"
    if market_type != "match_winner" or not text:
        return make_profile(sport=sport, target_kind=target, payouts=payouts,
                            policies=policies, evidence=citations, label=label)

    # The parser has classified a winner contract; require actual winner language.
    winner_stated = bool(re.search(r"\bwin(?:s|ner)?\b", text) or
                         (target == "draw" and "draw or tie" in text))
    if winner_stated:
        payouts.update(win="0" if target == "draw" else "1", loss="0")
        policies["period"] = "full_match"

    if sport == "basketball":
        if re.search(r"including (?:any )?overtime|overtime (?:is |will be )?included|regulation time and any officially designated overtime periods", text):
            policies["overtime"] = "included"
        elif "regulation only" in text or "excluding overtime" in text:
            policies["overtime"] = "excluded"
        if venue == "kalshi" and "within 48 hours" in text and re.search(r"resolve to (?:a )?fair (?:market )?price", text):
            policies["postponement"] = "fair_market_price_if_not_started_within_48_hours"
            payouts["cancelled_before_start"] = "fair_market_price"
        if venue == "polymarket_intl":
            if "remain open until the game is completed" in text:
                policies["postponement"] = "wait_until_completed"
            if re.search(r"cancell?ed entirely.*?50[- ]50", text):
                payouts["cancelled_before_start"] = "0.5"
        return make_profile(sport=sport, target_kind=target, payouts=payouts,
                            policies=policies, evidence=citations, label=label)

    if venue == "kalshi":
        half = re.search(r"ends in a tie, draw, no result.*?no official winner is declared, all markets will resolve to \$0\.50", text)
        three_way = 'the market titled "draw/tie" will resolve yes' in text
        if half:
            for state in ("draw", "tie_without_tiebreak", "no_result_after_start",
                          "cancelled_before_start", "abandoned_before_toss_no_winner",
                          "abandoned_after_toss_before_ball_no_winner", "abandoned_after_start_no_winner"):
                payouts[state] = "0.5"
            label = "Draw / no winner pays 0.50"
        if three_way:
            payouts["draw"] = payouts["tie_without_tiebreak"] = "1" if target == "draw" else "0"
            label = "Draw / tie outcome" if target == "draw" else "Team loses on draw / tie"
            if "abandoned prior to the toss" in text and "resolve to a fair market price" in text:
                payouts["cancelled_before_start"] = "fair_market_price"
                payouts["abandoned_before_toss_no_winner"] = "fair_market_price"
            if "delayed for more than 5 days" in text:
                policies["postponement"] = "fair_market_price_if_not_started_within_5_days"
        if re.search(r"before the match begins, all markets will resolve to \$0\.50", text):
            for state in ("forfeit_before_start_win", "forfeit_before_start_loss", "forfeit_before_start_no_winner"):
                payouts[state] = "0.5"
        if "after the match has begun" in text and "governing body declares an official winner" in text:
            payouts.update(forfeit_after_start_win="1", forfeit_after_start_loss="0")

    elif venue == "polymarket_intl":
        half = bool(re.search(r"(?:resolve|resolves) (?:to )?50[- ]50", text))
        draw_target = bool(re.search(r"(?:resolve|resolves) to (?:\"|')?draw", text))
        if half:
            label = "Draw / no winner pays 0.50"
        elif draw_target:
            label = "Draw outcome" if target == "draw" else "Team loses on draw"
        if half or draw_target:
            no_winner = "0.5" if half else "1" if target == "draw" else "0"
            if "ends tied and no on-field tiebreak" in text:
                payouts["tie_without_tiebreak"] = no_winner
            if "completed without a winner" in text or "otherwise is completed without a winner" in text:
                payouts["draw"] = payouts["no_result_after_start"] = no_winner
            if "permanently canceled or abandoned" in text or "permanently cancelled or abandoned" in text:
                for state in ("cancelled_before_start", "abandoned_before_toss_no_winner",
                              "abandoned_after_toss_before_ball_no_winner", "abandoned_after_start_no_winner"):
                    payouts[state] = no_winner
            if "forfeit/walkover" in text and "treated as ordinary wins" in text:
                payouts.update(forfeit_before_start_win="1" if target != "draw" else "0",
                               forfeit_before_start_loss="0", forfeit_after_start_win="1" if target != "draw" else "0",
                               forfeit_after_start_loss="0", forfeit_before_start_no_winner=no_winner)
        if "winner determined by that tiebreak will be used for resolution" in text:
            policies["tiebreakers"] = "included_if_played"
        if "dls/drs" in text and "treated as ordinary wins" in text:
            policies["dls"] = "official_result"
        if "remains open until the listed fixture is completed" in text or "remain open until the listed fixture is completed" in text:
            policies["postponement"] = "wait_until_completed"

    elif venue == "polymarket_us":
        if "first ball is bowled" in text and "market will settle to $0.50" in text:
            for state in ("draw", "tie_without_tiebreak", "no_result_after_start", "abandoned_after_start_no_winner"):
                payouts[state] = "0.5"
            label = "Draw after first ball pays 0.50"
        if "before the first ball is bowled" in text and "last fair market price" in text:
            for state in ("cancelled_before_start", "abandoned_before_toss_no_winner",
                          "abandoned_after_toss_before_ball_no_winner", "forfeit_before_start_win",
                          "forfeit_before_start_loss", "forfeit_before_start_no_winner"):
                payouts[state] = "last_fair_market_price"
        if "a team then concedes" in text and "team officially declared the winner" in text:
            payouts.update(forfeit_after_start_win="1", forfeit_after_start_loss="0")
        if "super over or other official tie-breaker is included if played" in text:
            policies["tiebreakers"] = "included_if_played"
        if "not within two days" in text:
            policies["postponement"] = "last_fair_market_price_outside_2_days"

    # Explicit supplementary clauses may complete a profile. They are never
    # supplied as defaults, and timing exceptions take precedence over a general
    # statement that postponed fixtures stay open.
    if "winner determined by that tiebreak will be used for resolution" in text:
        policies["tiebreakers"] = "included_if_played"
    if "dls/drs" in text and "treated as ordinary wins" in text:
        policies["dls"] = "official_result"
    if not policies["postponement"] and ("remain open until the listed fixture is completed" in text or
                                         "remains open until the listed fixture is completed" in text):
        policies["postponement"] = "wait_until_completed"
    if re.search(r"before the match begins, all markets will resolve to \$0\.50", text):
        for state in ("forfeit_before_start_win", "forfeit_before_start_loss", "forfeit_before_start_no_winner"):
            payouts[state] = "0.5"
    if re.search(r"\bvoid(?:ed)?\b[^.]{0,120}(?:resolve|settle)\w*[^.]{0,50}\$0\.50", text):
        payouts["void"] = "0.5"

    # A generic administrative void, an omitted postponement rule, or an omitted
    # tiebreak rule remains unknown even when ordinary win/loss payments are clear.
    return make_profile(sport=sport, target_kind=target, payouts=payouts,
                        policies=policies, evidence=citations, label=label)


def compare_profiles(left: dict, right: dict) -> dict:
    """Return compatible only for fully evidenced, equal state/policy models."""
    left = left if isinstance(left, dict) else {}
    right = right if isinstance(right, dict) else {}
    differences = []
    for key in ("sport", "target_kind"):
        if left.get(key) and right.get(key) and left[key] != right[key]:
            differences.append(f"{key}: {left[key]} versus {right[key]}")
    for section in ("payouts", "policies"):
        a, b = left.get(section) or {}, right.get(section) or {}
        for key in sorted(set(a) | set(b)):
            values = (a.get(key), b.get(key))
            known = (all(_valid_payout(value) for value in values) if section == "payouts"
                     else all(value not in (None, "", "unknown") for value in values))
            equal = a.get(key) == b.get(key)
            if section == "payouts" and known and not equal:
                try:
                    equal = Decimal(str(a[key])) == Decimal(str(b[key]))
                except InvalidOperation:
                    pass
            if known and not equal:
                differences.append(f"{key}: {a[key]} versus {b[key]}")
    if differences:
        return {"status": "incompatible", "reason": "settlement rules differ: " + "; ".join(differences),
                "differences": differences}
    sport = str(left.get("sport") or right.get("sport") or "cricket")
    complete = all(profile.get("status") == "known" and profile.get("evidence")
                   and profile.get("sport") == sport and profile.get("target_kind") in {"team", "draw"}
                   and all(_fixed_payout((profile.get("payouts") or {}).get(key)) is not None for key in _states(sport))
                   and all(_fixed_payout(value) is not None for value in (profile.get("payouts") or {}).values())
                   and all((profile.get("policies") or {}).get(key) not in (None, "", "unknown")
                           for key in _policies(sport)) for profile in (left, right))
    if not complete:
        return {"status": "unknown", "reason": "settlement rules are incomplete, unverified, or contain non-fixed payouts",
                "differences": []}
    return {"status": "compatible", "reason": "equal evidenced payouts and settlement policies",
            "differences": []}
