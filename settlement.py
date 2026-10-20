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
FOOTBALL_STATES = NBA_STATES + ("tie",)
HOCKEY_STATES = NBA_STATES + ("tie",)
BASEBALL_STATES = NBA_STATES + ("tie",)
SOCCER_STATES = NBA_STATES + ("draw",)
TENNIS_STATES = NBA_STATES + ("walkover_win", "walkover_loss", "retirement_win", "retirement_loss")
MMA_STATES = NBA_STATES + ("draw", "no_contest")
ESPORTS_STATES = NBA_STATES + ("tie",)
SPORTS = {"cricket", "basketball", "football", "hockey", "baseball", "soccer", "tennis", "mma", "esports"}
CORE_STATES = {"cricket": ("win", "loss", "draw", "tie_without_tiebreak"),
               "basketball": ("win", "loss"), "football": ("win", "loss", "tie"),
                             "hockey": ("win", "loss"), "baseball": ("win", "loss"),
                             "soccer": ("win", "loss", "draw"),
                             "tennis": ("win", "loss"), "mma": ("win", "loss", "draw", "no_contest"),
                             "esports": ("win", "loss", "tie")}
CORE_POLICIES = {"cricket": ("period", "tiebreakers", "dls"),
                 "basketball": ("period", "overtime"),
                 "football": ("period", "overtime"),
                 "hockey": ("period", "overtime", "shootout"),
                                 "baseball": ("period", "extra_innings"),
                                 "soccer": ("period",), "tennis": ("period",), "mma": ("period",),
                                 "esports": ("period",)}
POLICY_VALUES = {"period": {"full_match", "regulation", "regulation_time"}, "overtime": {"included", "excluded"},
                 "shootout": {"included", "excluded"}, "extra_innings": {"included", "excluded"},
                 "tiebreakers": {"included_if_played", "excluded", "not_applicable"},
                 "dls": {"official_result", "excluded", "not_applicable"}}
OFFICIAL_FORFEIT_STATES = {"forfeit_after_start_win", "forfeit_after_start_loss",
                          "retirement_win", "retirement_loss"}


def resolution_evidence(raw: dict) -> dict:
    evidence = {key: str(raw.get(key) or "") for key in
                ("rules_primary", "rules_secondary", "description", "resolutionSource")}
    documents = raw.get("_settlement_documents") or []
    evidence["documents"] = [dict(item) for item in documents if isinstance(item, dict)]
    return evidence


def _states(sport: str) -> tuple:
    return {"basketball": NBA_STATES, "football": FOOTBALL_STATES,
            "hockey": HOCKEY_STATES, "baseball": BASEBALL_STATES,
            "soccer": SOCCER_STATES, "tennis": TENNIS_STATES,
            "mma": MMA_STATES, "esports": ESPORTS_STATES}.get(sport, CRICKET_STATES)


def _policies(sport: str) -> tuple:
    return {"basketball": NBA_POLICIES, "football": NBA_POLICIES,
            "hockey": ("period", "overtime", "shootout", "postponement"),
            "baseball": ("period", "extra_innings", "postponement"),
            "soccer": ("period", "postponement"),
            "tennis": ("period", "postponement", "retirement", "walkover"),
            "mma": ("period", "postponement"),
            "esports": ("period", "postponement", "overtime")}.get(sport, CRICKET_POLICIES)


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
    if sport not in SPORTS or target_kind not in {"team", "draw"}:
        missing.append("supported sport/target")
    signature = json.dumps({"sport": sport, "target_kind": target_kind,
                            "payouts": payouts, "policies": policies}, sort_keys=True)
    profile = {"profile_id": hashlib.sha256(signature.encode()).hexdigest()[:16],
            "status": "unknown" if missing else "known", "sport": sport,
            "target_kind": target_kind, "payouts": payouts, "policies": policies,
            "evidence": evidence, "missing": missing, "label": label}
    profile["core_missing"] = _core_missing(profile, sport)
    profile["core_status"] = "unknown" if profile["core_missing"] else "known"
    return profile


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
                         (sport == "tennis" and re.search(r"\badvances against\b", text)) or
                         (target == "draw" and "draw or tie" in text))
    if winner_stated:
        payouts.update(win="0" if target == "draw" else "1", loss="0")
        policies["period"] = "full_match"

    if sport == "esports":
        # A winner can refer to a map, round, game, or a complete series. Only
        # explicit full-match language establishes the shared result period.
        full_match = ("refers to the full match" in text if venue == "kalshi"
                      else "win the match against" in text)
        policies["period"] = "full_match" if winner_stated and full_match else None
        if venue == "kalshi":
            formula_document = any(
                doc.get("url") == "https://assets.kalshi.com/contract_terms/ESPORTSWIN.pdf"
                and "number of tied participants" in str(doc.get("text") or "")
                and "no “Tie” strike listed" in str(doc.get("text") or "")
                for doc in evidence["documents"])
            if raw.get("_esports_two_participant_contract") is True and formula_document:
                payouts["tie"] = "0.5"
            if "not started within 48 hours" in text and "fair market price" in text:
                policies["postponement"] = "fair_market_price_if_not_started_within_48_hours"
            if "cancelled before play begins" in text and "fair market price" in text:
                payouts["cancelled_before_start"] = "fair_market_price"
            if "forfeited before any play occurs" in text and "fair market price" in text:
                payouts.update(forfeit_before_start_win="fair_market_price",
                               forfeit_before_start_loss="fair_market_price")
            if "begins and is subsequently forfeited" in text and "official result" in text:
                payouts.update(forfeit_after_start_win="1", forfeit_after_start_loss="0")
        elif venue == "polymarket_intl":
            if re.search(r"ends in a tie[^.]{0,100}resolve to 50[- ]50", text):
                payouts["tie"] = "0.5"
            if re.search(r"match is canceled \(not played at all\)[^.]{0,250}resolve to 50[- ]50", text):
                payouts["cancelled_before_start"] = "0.5"
            if "delayed beyond 7 days" in text and "50-50" in text:
                policies["postponement"] = "half_if_no_winner_within_7_days"
            elif "14 calendar days after the originally scheduled start" in text and "rescheduled start time" in text:
                policies["postponement"] = "half_if_rescheduled_start_after_published_14_day_deadline"
            if "team withdraws before the start and the other wins automatically" in text and re.search(
                    r"team withdraws before the start[^.]{0,180}resolve to 50[- ]50", text):
                payouts.update(forfeit_before_start_win="0.5", forfeit_before_start_loss="0.5")
            if ("match begins but is not completed" in text and "opponent's forfeiture" in text
                    and "resolve to the team who wins" in text):
                payouts.update(forfeit_after_start_win="1", forfeit_after_start_loss="0")
        # Native full-match descriptions identify the official winner but do
        # not separately state overtime treatment. Keep that contingency open.
        if re.search(r"overtime (?:is |periods are )?included", text):
            policies["overtime"] = "included"
        elif re.search(r"(?:excluding|not including) overtime", text):
            policies["overtime"] = "excluded"
        return make_profile(sport=sport, target_kind=target, payouts=payouts,
                            policies=policies, evidence=citations,
                            label="Full series winner; cancellation and forfeit terms differ")

    if sport == "soccer":
        scope_terms = re.sub(r"\s+", " ", text)
        regulation_only = bool(re.search(
            r"(?:first 90 minutes|90 minutes plus stoppage time|regulation time)"
            r"[^.]{0,120}(?:does not include|excluding|not include)"
            r"[^.]{0,80}(?:extra time|penalt)", scope_terms) or
            re.search(r"only (?:to )?(?:the )?outcome within the first 90 minutes", scope_terms))
        full_match = ("full match" in scope_terms and
                      "officially confirmed by the organizer" in scope_terms)
        if regulation_only:
            policies["period"] = "regulation_time"
        elif full_match:
            policies["period"] = "full_match"
        if target == "draw":
            if ("game ends in a draw" in scope_terms or
                    'market called "tie" resolves to yes' in scope_terms or
                    "if tie is the result" in scope_terms):
                payouts.update(win="0", loss="0", draw="1")
        elif winner_stated:
            payouts.update(win="1", loss="0", draw="0")
        if target != "draw" and ("market called \"tie\" resolves to yes" in scope_terms or
                                  ("otherwise" in scope_terms and
                                   re.search(r"otherwise.{0,40}resolve to [\"']no", scope_terms))):
            payouts["draw"] = "0"
        if venue == "kalshi" and "within 48 hours" in scope_terms and "last fair price" in scope_terms:
            policies["postponement"] = "last_fair_market_price_if_not_resumed_within_48_hours"
            payouts["cancelled_before_start"] = "last_fair_market_price"
        elif venue == "polymarket_intl" and "remain open until the game has been completed" in scope_terms:
            policies["postponement"] = "wait_until_completed"
            if re.search(r"game is canceled entirely[^.]{0,120}resolve\s+[\"']?no\b", scope_terms):
                payouts["cancelled_before_start"] = "0"
        label = ("Regulation-time result" if policies["period"] == "regulation_time"
                 else "Full-match result")
        return make_profile(sport=sport, target_kind=target, payouts=payouts,
                            policies=policies, evidence=citations, label=label)

    if sport in {"tennis", "mma"}:
        if sport == "tennis":
            # Native Gamma match-winner contracts specify advancing; neither
            # that clause nor a completed-match winner establishes a walkover.
            if "walkover" in text and re.search(r"walkover[^.]{0,180}50[- ]50", text):
                payouts.update(walkover_win="0.5", walkover_loss="0.5")
                policies["walkover"] = "half_if_before_start"
            if "retirement, default, or disqualification" in text and "player who advances" in text:
                payouts.update(retirement_win="1", retirement_loss="0")
                policies["retirement"] = "advancing_player_after_start"
            if venue == "kalshi" and "all before the match starts" in text and "fair price" in text:
                payouts.update(walkover_win="fair_market_price", walkover_loss="fair_market_price",
                               cancelled_before_start="fair_market_price")
                policies["walkover"] = "fair_market_price_before_start"
            if "14 days after the scheduled start" in text:
                policies["postponement"] = "half_if_no_winner_within_14_days"
            elif "within two weeks" in text:
                policies["postponement"] = "wait_up_to_two_weeks"
            label = "Match winner; retirement and walkover terms retained"
        else:
            # Bind both states to the same explicit payout clause. Separate
            # draw and no-contest sentences can have different payments.
            if re.search(r"draw\s*/\s*no contest[^.]{0,100}\$0\.50", text):
                payouts.update(draw="0.5", no_contest="0.5")
            elif re.search(r"draw or technical draw,\s*ruled a no contest[^.]{0,250}"
                           r"resolv\w*[^.]{0,60}[\"']?50[- ]50", text):
                payouts.update(draw="0.5", no_contest="0.5")
            if "within 48 hours" in text and "fair price" in text:
                policies["postponement"] = "fair_market_price_if_not_started_within_48_hours"
                payouts["cancelled_before_start"] = "fair_market_price"
            label = "Official fight winner; draw and no-contest terms retained"
        return make_profile(sport=sport, target_kind=target, payouts=payouts,
                            policies=policies, evidence=citations, label=label)

    if sport in {"basketball", "football", "hockey", "baseball"}:
        native_text = re.sub(r"\s+", " ", " ".join(value for key, value in parts
                            if key in {"rules_primary", "rules_secondary", "description"})).lower()
        exclude_ot = r"regulation(?: time)? only|excluding overtime|without overtime|not including overtime|overtime (?:is |will be )?not included"
        include_ot = r"including (?:any )?overtime|overtime(?: periods)? (?:are |is |will be )?included|regulation time and any officially designated overtime periods|regulation time, overtime, and the shootout"
        if "overtime" in policies:
            if re.search(exclude_ot, native_text):
                policies["overtime"] = "excluded"
            elif re.search(include_ot, text):
                policies["overtime"] = "included"
            elif re.search(exclude_ot, text):
                policies["overtime"] = "excluded"
        if sport == "hockey":
            if re.search(r"(?:excluding|without|not including) (?:the )?shootouts?|shootouts? (?:is |are )?not included", native_text):
                policies["shootout"] = "excluded"
            elif re.search(r"including[^.]{0,100}shootouts?|regulation time, overtime, and the shootout", text):
                policies["shootout"] = "included"
        if sport == "baseball":
            if re.search(r"(?:excluding|without|not including) extra innings|regulation innings only", native_text):
                policies["extra_innings"] = "excluded"
            elif re.search(r"including[^.]{0,100}extra innings|extra innings[^.]{0,80}(?:are |is )?included", text):
                policies["extra_innings"] = "included"
        if sport == "football":
            if re.search(r"(?:ends|end) in a tie[^.]{0,150}(?:resolv|settl)\w*[^.]{0,60}(?:50[- ]50|\$0\.50)", native_text):
                payouts["tie"] = "0.5"
            elif re.search(r"no [\"']?tie[\"']? strike is listed[^.]{0,180}(?:\$0\.50|50[- ]50)", text):
                payouts["tie"] = "0.5"
            elif re.search(r"(?:ends|end) in a tie[^.]{0,150}(?:resolv|settl)\w*[^.]{0,60}(?:\"|')?no\b", native_text):
                payouts["tie"] = "0"
        if sport in {"basketball", "football"} and policies.get("overtime"):
            label = "Winner including overtime" if policies["overtime"] == "included" else "Regulation winner"
        elif sport == "hockey" and policies.get("overtime") == policies.get("shootout") == "included":
            label = "Winner including overtime and shootout"
        elif sport == "baseball" and policies.get("extra_innings") == "included":
            label = "Winner including extra innings"
        if venue == "kalshi" and "within 48 hours" in text and re.search(r"resolve to (?:a )?fair (?:market )?price", text):
            policies["postponement"] = "fair_market_price_if_not_started_within_48_hours"
            payouts["cancelled_before_start"] = "fair_market_price"
        if venue == "polymarket_intl":
            if "remain open until the game is completed" in text or "remain open until the game has been completed" in text:
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


def _equal_payout(a: Any, b: Any) -> bool:
    first, second = _fixed_payout(a), _fixed_payout(b)
    return first == second if first is not None and second is not None else a == b


def _known_policy(key: str, value: Any) -> bool:
    if key in POLICY_VALUES:
        return isinstance(value, str) and value in POLICY_VALUES[key]
    return isinstance(value, str) and bool(value) and value != "unknown"


def _core_missing(profile: dict, sport: str) -> list[str]:
    missing = []
    if sport not in SPORTS or profile.get("sport") != sport:
        missing.append("supported sport")
    if profile.get("target_kind") not in {"team", "draw"}:
        missing.append("target")
    if not profile.get("evidence"):
        missing.append("resolution evidence")
    missing.extend("payout:" + key for key in CORE_STATES.get(sport, ())
                   if _fixed_payout((profile.get("payouts") or {}).get(key)) is None)
    missing.extend("policy:" + key for key in CORE_POLICIES.get(sport, ())
                   if not _known_policy(key, (profile.get("policies") or {}).get(key)))
    return missing


def compare_profiles(left: dict, right: dict, *, strict: bool = False) -> dict:
    """Compare normal completed results, while preserving exceptional risks.

    Normal-completion compatibility is conditional on the event completing with
    its ordinary official result. Strict mode also requires identical, fixed
    payments for every exceptional state. Neither profile hashes nor stored
    status labels establish either kind of compatibility.
    """
    left = left if isinstance(left, dict) else {}
    right = right if isinstance(right, dict) else {}
    sport = str(left.get("sport") or right.get("sport") or "")
    core_differences, contingent_differences, unknown_contingencies = [], [], []
    for key in ("sport", "target_kind"):
        if left.get(key) and right.get(key) and left[key] != right[key]:
            core_differences.append(f"{key}: {left[key]} versus {right[key]}")
    for section in ("payouts", "policies"):
        a, b = left.get(section) or {}, right.get(section) or {}
        required = _states(sport) if section == "payouts" else _policies(sport)
        core_keys = CORE_STATES.get(sport, ()) if section == "payouts" else CORE_POLICIES.get(sport, ())
        for key in sorted(set(required) | set(a) | set(b)):
            values = (a.get(key), b.get(key))
            known = (all(_valid_payout(value) for value in values) if section == "payouts"
                     else all(_known_policy(key, value) for value in values))
            equal = _equal_payout(*values) if section == "payouts" else values[0] == values[1]
            core = key in core_keys
            # A different payment despite an official post-start winner cannot
            # be explained away by a pre-start cancellation caveat. An omitted
            # exceptional forfeit clause still remains a visible contingency.
            official_forfeit_difference = section == "payouts" and key in OFFICIAL_FORFEIT_STATES and known and not equal
            if known and not equal:
                difference = f"{key}: {a[key]} versus {b[key]}"
                (core_differences if core or official_forfeit_difference else contingent_differences).append(difference)
            if not core and (not known or (section == "payouts" and
                             any(_fixed_payout(value) is None for value in values))):
                unknown_contingencies.append(key)

    core_missing = sorted(set(_core_missing(left, sport) + _core_missing(right, sport)))
    core_status = "incompatible" if core_differences else "unknown" if core_missing else "compatible"
    contingency_status = "different" if contingent_differences else "unknown" if unknown_contingencies else "aligned"
    caveats = list(contingent_differences)
    if unknown_contingencies:
        caveats.append("Unverified or variable contingency payouts: " + ", ".join(sorted(set(unknown_contingencies))))
    differences = core_differences + (contingent_differences if strict else [])
    status = ("incompatible" if differences else "unknown" if core_missing or
              (strict and unknown_contingencies) else "compatible")
    if differences:
        reason = ("settlement rules differ" if strict else "normal-result settlement rules differ") + ": " + "; ".join(differences)
    elif status == "unknown":
        reason = ("settlement rules are incomplete or unverified" if strict else
                  "normal-result settlement rules are incomplete or unverified")
        if core_missing:
            reason += ": " + ", ".join(core_missing)
    else:
        reason = "equal evidenced payouts and settlement policies" if strict else "equal evidenced normal-completion payouts and policies"
        if not strict and caveats:
            reason += "; contingent-event terms require a conditional comparison"
    return {"status": status, "reason": reason, "differences": differences,
            "comparison_basis": "full_contract" if strict else "normal_completion",
            "core_status": core_status, "core_differences": core_differences,
            "core_missing": core_missing, "contingency_status": contingency_status,
            "contingency_differences": contingent_differences, "caveats": caveats}
