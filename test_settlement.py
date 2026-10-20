import copy
import unittest
from decimal import Decimal

import canonical as C
import matching as M
import settlement as S


KALSHI_HALF = {
    "rules_primary": "If Australia wins the Australia vs New Zealand cricket match, then the market resolves to Yes.",
    "rules_secondary": (
        "If the match ends in a tie, draw, no result (NR), abandonment, or cancellation, "
        "and no official winner is declared, all markets will resolve to $0.50. "
        "In the event of a forfeit, disqualification, or concession: "
        "If it occurs before the match begins, all markets will resolve to $0.50. "
        "If it occurs after the match has begun, and the governing body declares an official winner, "
        "the market will resolve based on that result."
    ),
}
KALSHI_THREE = {
    "rules_primary": KALSHI_HALF["rules_primary"],
    "rules_secondary": (
        'If the match ends in a draw or tie, the market titled "Draw/Tie" will resolve Yes. '
        "If the match is cancelled prior to scheduled commencement, or is abandoned prior to the toss "
        "and no official result is declared, then all markets will resolve to a fair market price. "
        "If the match is delayed for more than 5 days from its original scheduled start time before "
        "commencement, then all markets will resolve to a fair market price."
    ),
}


def intl_description(no_winner="50-50"):
    return {"description": (
        "DLS/DRS, over-rate penalties, forfeit/walkover, or any other on-field ruling that leads "
        "the competition to declare a winner are treated as ordinary wins. "
        "If the match ends tied and the playing conditions provide an on-field tiebreak (e.g., Super Over), "
        "the winner determined by that tiebreak will be used for resolution. "
        f"If the match ends tied and no on-field tiebreak is used or available, the market will resolve to {no_winner}. "
        "If the match is postponed/rescheduled, the market remains open until the listed fixture is completed. "
        "If the match is permanently canceled or abandoned or otherwise is completed without a winner, "
        f"the market resolves to {no_winner}."
    )}


def known_profile():
    return S.make_profile(
        sport="cricket", target_kind="team",
        payouts={state: "1" if state in {"win", "forfeit_before_start_win", "forfeit_after_start_win"}
                 else "0" if state in {"loss", "forfeit_before_start_loss", "forfeit_after_start_loss"}
                 else "0.5" for state in S.CRICKET_STATES},
        policies={"period": "full_match", "tiebreakers": "included_if_played",
                  "postponement": "wait_until_completed", "dls": "official_result"},
        evidence=[{"field": "synthetic test contract", "text": "All test states explicitly specified"}],
    )


class SettlementProfileTests(unittest.TestCase):
    def test_missing_incidental_states_do_not_block_evidenced_normal_completion(self):
        a, b = known_profile(), known_profile()
        for profile in (a, b):
            profile["payouts"].pop("void")
            profile["payouts"].pop("abandoned_after_start_no_winner")
            profile["policies"].pop("postponement")
            profile["status"] = "unknown"
        result = S.compare_profiles(a, b)
        self.assertEqual(result["status"], "compatible")
        self.assertEqual(result["contingency_status"], "unknown")
        self.assertTrue(result["caveats"])
        strict = S.compare_profiles(a, b, strict=True)
        self.assertEqual(strict["status"], "unknown")
        self.assertEqual(strict["comparison_basis"], "full_contract")

    def test_unknown_win_loss_period_or_tiebreak_logic_still_blocks(self):
        for section, key in [("payouts", "win"), ("payouts", "loss"),
                             ("payouts", "tie_without_tiebreak"),
                             ("policies", "period"), ("policies", "tiebreakers")]:
            with self.subTest(section=section, key=key):
                a, b = known_profile(), known_profile()
                b[section][key] = None
                self.assertEqual(S.compare_profiles(a, b)["status"], "unknown")

    def test_post_start_official_winner_disagreement_is_not_a_pre_start_caveat(self):
        a, b = known_profile(), known_profile()
        b["payouts"]["forfeit_after_start_win"] = "0.5"
        result = S.compare_profiles(a, b)
        self.assertEqual(result["status"], "incompatible")
        self.assertIn("forfeit_after_start_win", result["reason"])
        b["payouts"]["forfeit_after_start_win"] = None
        result = S.compare_profiles(a, b)
        self.assertEqual(result["status"], "compatible")
        self.assertEqual(result["contingency_status"], "unknown")

    def test_reported_three_way_versus_half_draw_difference(self):
        for left, right in [(KALSHI_THREE, intl_description()),
                            (KALSHI_HALF, intl_description("draw"))]:
            with self.subTest(left=left):
                a = S.profile_market("kalshi", left, outcome="Australia")
                b = S.profile_market("polymarket_intl", right, outcome="Australia")
                comparison = S.compare_profiles(a, b)
                self.assertEqual(comparison["status"], "incompatible")
                self.assertTrue(any("draw:" in difference for difference in comparison["differences"]))
                self.assertNotEqual(a["payouts"]["draw"], b["payouts"]["draw"])

    def test_draw_contract_pays_one_where_team_contract_pays_zero(self):
        team = S.profile_market("kalshi", KALSHI_THREE, outcome="Australia")
        draw = S.profile_market("kalshi", KALSHI_THREE, outcome="draw")
        self.assertEqual(team["payouts"]["draw"], "0")
        self.assertEqual(draw["payouts"]["draw"], "1")
        self.assertEqual(draw["payouts"]["win"], "0")

    def test_unknown_is_never_compatible_with_unknown(self):
        a = S.profile_market("kalshi", {}, outcome="Australia")
        b = S.profile_market("polymarket_intl", {}, outcome="Australia")
        self.assertEqual(S.compare_profiles(a, b)["status"], "unknown")
        self.assertEqual(S.compare_profiles({}, {})["status"], "unknown")

    def test_missing_normal_result_rules_stay_unknown_and_incidental_differences_are_caveats(self):
        a = S.profile_market("kalshi", KALSHI_HALF, outcome="Australia")
        b = S.profile_market("polymarket_intl", intl_description(), outcome="Australia")
        self.assertEqual(a["payouts"]["draw"], b["payouts"]["draw"])
        self.assertEqual(a["status"], "unknown")
        self.assertIn("policy:postponement", a["missing"])
        comparison = S.compare_profiles(a, b)
        self.assertEqual(comparison["status"], "unknown")
        self.assertEqual(comparison["contingency_status"], "different")
        self.assertIn("policy:tiebreakers", comparison["core_missing"])
        self.assertTrue(any("forfeit_before_start" in caveat for caveat in comparison["caveats"]))
        self.assertEqual(S.compare_profiles(a, b, strict=True)["status"], "incompatible")

    def test_equal_complete_models_are_compatible_with_exact_numeric_payments(self):
        a = known_profile()
        b = copy.deepcopy(a)
        b["payouts"]["draw"] = "0.5000"
        self.assertEqual(a["status"], "known")
        self.assertEqual(S.compare_profiles(a, b)["status"], "compatible")

    def test_numeric_equivalent_values_have_equal_profiles_and_hashes(self):
        original = known_profile()
        values = copy.deepcopy(original["payouts"])
        values["draw"] = "0.50"
        values["win"] = Decimal("1.000")
        values["loss"] = Decimal("0.00")
        other = S.make_profile(sport="cricket", target_kind="team", payouts=values,
                               policies=original["policies"], evidence=original["evidence"])
        self.assertEqual(original["profile_id"], other["profile_id"])
        self.assertEqual(original["payouts"], other["payouts"])
        self.assertEqual(S.compare_profiles(original, other)["status"], "compatible")

    def test_equal_variable_price_or_refund_policies_cannot_establish_payoff_equality(self):
        for symbolic in ("fair_market_price", "last_fair_market_price", "refund"):
            with self.subTest(symbolic=symbolic):
                original = known_profile()
                original["payouts"]["cancelled_before_start"] = symbolic
                # Even an externally claimed known status cannot make two
                # discretionary venue prices equal in the cancellation state.
                normal = S.compare_profiles(original, copy.deepcopy(original))
                self.assertEqual(normal["status"], "compatible")
                self.assertEqual(normal["comparison_basis"], "normal_completion")
                self.assertEqual(normal["contingency_status"], "unknown")
                self.assertTrue(normal["caveats"])
                self.assertEqual(S.compare_profiles(original, copy.deepcopy(original), strict=True)["status"], "unknown")
                made = S.make_profile(sport="cricket", target_kind="team",
                                       payouts=original["payouts"], policies=original["policies"],
                                       evidence=original["evidence"])
                self.assertEqual(made["status"], "unknown")
                self.assertIn("payout:cancelled_before_start", made["missing"])

    def test_extra_symbolic_state_is_not_silently_excluded_from_comparison(self):
        a = known_profile()
        a["payouts"]["venue_discretionary_event"] = "fair_market_price"
        result = S.compare_profiles(a, copy.deepcopy(a))
        self.assertEqual(result["status"], "compatible")
        self.assertIn("venue_discretionary_event", result["caveats"][0])
        self.assertEqual(S.compare_profiles(a, copy.deepcopy(a), strict=True)["status"], "unknown")

    def test_explicit_full_rule_text_can_establish_compatible_models(self):
        # No part of this supplemental contract is inferred from a venue name.
        supplemental = (
            "DLS/DRS rulings that declare a winner are treated as ordinary wins. "
            "The winner determined by that tiebreak will be used for resolution. "
            "Postponed markets remain open until the listed fixture is completed. "
            "If a market is voided, all contracts settle to $0.50."
        )
        kalshi = {**KALSHI_HALF, "description": supplemental}
        intl = intl_description()
        intl["description"] += (
            " In the event of a forfeit, if it occurs before the match begins, all markets will resolve to $0.50. "
            "If it occurs after the match has begun, and the governing body declares an official winner, "
            "the market will resolve based on that result. " + supplemental
        )
        a = S.profile_market("kalshi", kalshi, outcome="Australia")
        b = S.profile_market("polymarket_intl", intl, outcome="Australia")
        self.assertEqual(a["missing"], [])
        self.assertEqual(b["missing"], [])
        self.assertEqual(S.compare_profiles(a, b)["status"], "compatible")

    def test_claimed_known_status_cannot_override_missing_or_invalid_evidence(self):
        for remove in ("void", "draw"):
            a = known_profile()
            a["payouts"].pop(remove)
            self.assertEqual(S.compare_profiles(a, a, strict=True)["status"], "unknown")
            self.assertEqual(S.compare_profiles(a, a)["status"], "unknown" if remove == "draw" else "compatible")
        a = known_profile()
        a["evidence"] = []
        self.assertEqual(S.compare_profiles(a, a)["status"], "unknown")

    def test_tiebreak_and_dls_differences_are_incompatible(self):
        for field, different in [("tiebreakers", "excluded"), ("dls", "excluded")]:
            a, b = known_profile(), known_profile()
            b["policies"][field] = different
            self.assertEqual(S.compare_profiles(a, b)["status"], "incompatible")

    def test_abandonment_before_first_ball_is_distinct_from_after_start(self):
        raw = {"description": (
            "This market will settle to the winner. A Super Over or other official tie-breaker is included if played. "
            "If the first ball is bowled and the match ends in a tie, draw, or no result, the market will settle to $0.50. "
            "If the match is cancelled or abandoned before the first ball is bowled, it settles to the last fair market price. "
            "If delayed to a time not within two days of the original start it settles to the last fair market price."
        )}
        profile = S.profile_market("polymarket_us", raw, outcome="Australia")
        self.assertEqual(profile["payouts"]["abandoned_before_toss_no_winner"], "last_fair_market_price")
        self.assertEqual(profile["payouts"]["abandoned_after_start_no_winner"], "0.5")
        self.assertEqual(profile["policies"]["postponement"], "last_fair_market_price_outside_2_days")

    def test_nba_overtime_and_cancellation_are_read_from_evidence(self):
        kalshi = {"rules_primary": "If Boston wins the basketball game, then the market resolves Yes.",
                  "rules_secondary": "If postponed but begins within 48 hours markets remain open. If cancelled, all markets resolve to a fair price.",
                  "_settlement_documents": [{"url": "https://assets.kalshi.com/contract_terms/BASKETBALLGAMEWIN.pdf",
                                             "text": "regulation time and any officially designated overtime periods"}]}
        intl = {"description": "If Celtics win, Celtics. If postponed, remain open until the game is completed. If canceled entirely with no make-up, resolves 50-50. Final score including any overtime periods."}
        a = S.profile_market("kalshi", kalshi, sport="basketball", outcome="Boston")
        b = S.profile_market("polymarket_intl", intl, sport="basketball", outcome="Boston")
        self.assertEqual(a["policies"]["overtime"], "included")
        self.assertEqual(b["policies"]["overtime"], "included")
        comparison = S.compare_profiles(a, b)
        self.assertEqual(comparison["status"], "compatible")
        self.assertEqual(comparison["contingency_status"], "different")
        self.assertTrue(any("cancelled_before_start" in caveat for caveat in comparison["caveats"]))
        self.assertEqual(S.compare_profiles(a, b, strict=True)["status"], "incompatible")

    def test_nba_native_regulation_override_beats_overtime_document_default(self):
        raw = {"rules_primary": "If Boston wins the basketball game, the market resolves Yes.",
               "rules_secondary": "This market covers regulation time only, excluding overtime.",
               "_settlement_documents": [{"url": "https://example.com/default", "text":
                                          "regulation time and any officially designated overtime periods"}]}
        a = S.profile_market("kalshi", raw, sport="basketball", outcome="Boston")
        b = S.profile_market("polymarket_intl", {"description": "If Boston wins the game. Including any overtime periods."},
                             sport="basketball", outcome="Boston")
        self.assertEqual(a["policies"]["overtime"], "excluded")
        self.assertEqual(S.compare_profiles(a, b)["status"], "incompatible")

    def test_nfl_native_tied_game_and_overtime_are_core(self):
        kalshi = {"rules_primary": "If Washington wins the Pro Football game, the market resolves Yes.",
                  "rules_secondary": "If the game ends in a tie, the market will resolve to $0.50 for each team.",
                  "_settlement_documents": [{"url": "https://assets.kalshi.com/contract_terms/FOOTBALLGAMEWIN.pdf",
                                             "text": "all overtime periods are included in determining the winner"}]}
        intl = {"description": "If the Commanders win the game. Overtime is included if played. If the game ends in a tie, this market will resolve 50-50."}
        a = S.profile_market("kalshi", kalshi, sport="football", outcome="Washington")
        b = S.profile_market("polymarket_intl", intl, sport="football", outcome="Washington")
        self.assertEqual(a["payouts"]["tie"], "0.5")
        self.assertEqual(a["core_status"], "known")
        self.assertEqual(S.compare_profiles(a, b)["status"], "compatible")
        b["payouts"]["tie"] = "0"
        self.assertEqual(S.compare_profiles(a, b)["status"], "incompatible")
        b["payouts"]["tie"] = None
        self.assertEqual(S.compare_profiles(a, b)["status"], "unknown")

    def test_nhl_requires_both_overtime_and_shootout_agreement(self):
        kalshi = {"rules_primary": "If New York wins the hockey game, the market resolves Yes.",
                  "_settlement_documents": [{"url": "https://assets.kalshi.com/contract_terms/HOCKEYWINNINGINPERIOD.pdf",
                                             "text": "regulation time, overtime, and the shootout"}]}
        intl = {"description": "If New York wins the game. The final score includes any overtime periods and shootouts."}
        a = S.profile_market("kalshi", kalshi, sport="hockey", outcome="New York")
        # The actual Gamma template uses 'including', not this incomplete wording.
        unknown = S.profile_market("polymarket_intl", intl, sport="hockey", outcome="New York")
        self.assertEqual(S.compare_profiles(a, unknown)["status"], "unknown")
        intl["description"] = "If New York wins the game. Final score including any overtime periods and shootouts."
        b = S.profile_market("polymarket_intl", intl, sport="hockey", outcome="New York")
        self.assertEqual(S.compare_profiles(a, b)["status"], "compatible")
        b["policies"]["shootout"] = "excluded"
        self.assertEqual(S.compare_profiles(a, b)["status"], "incompatible")

    def test_soccer_three_way_regulation_markets_have_equal_core_payouts(self):
        kalshi_team = {
            "rules_primary": (
                "If Ipswich Town wins the match after 90 minutes plus stoppage time "
                "(does not include extra time or penalties), then the market resolves Yes."
            ),
            "rules_secondary": (
                'If the game ends in a tie, the market called "Tie" resolves to Yes. '
                "If postponed beyond 48 hours, resolve to a fair price."
            ),
        }
        polymarket_team = {
            "description": (
                'If Ipswich Town FC wins, this market will resolve to "Yes". '
                'Otherwise, this market will resolve to "No". '
                "This market refers only to the outcome within the first 90 minutes "
                "of regular play plus stoppage time. If postponed, remain open until "
                "the game has been completed."
            )
        }
        kalshi = S.profile_market("kalshi", kalshi_team, sport="soccer",
                                  outcome="Ipswich Town")
        polymarket = S.profile_market("polymarket_intl", polymarket_team,
                                      sport="soccer", outcome="Ipswich Town FC")
        self.assertEqual(kalshi["payouts"]["draw"], "0")
        self.assertEqual(polymarket["payouts"]["draw"], "0")
        self.assertEqual(kalshi["policies"]["period"], "regulation_time")
        self.assertEqual(polymarket["policies"]["period"], "regulation_time")
        comparison = S.compare_profiles(kalshi, polymarket)
        self.assertEqual(comparison["status"], "compatible")
        self.assertNotEqual(comparison["contingency_status"], "aligned")

    def test_tennis_advancement_retains_different_prestart_walkover_payouts(self):
        kalshi = S.profile_market("kalshi", {
            "rules_primary": "If Terence Atmane wins the tennis match after a ball has been played, resolve Yes.",
            "rules_secondary": "If the match does not occur due to a walkover (all before the match starts), resolve to a fair price. If delayed, remain open within two weeks.",
        }, sport="tennis", outcome="Terence Atmane")
        polymarket = S.profile_market("polymarket_intl", {
            "description": "If Terence Atmane advances against Rinky Hijikata, resolve to Terence Atmane. If the match ends in a walkover, this market resolves 50-50. If the match begins but is not completed, and one player advances due to the opponent's retirement, default, or disqualification, this market will resolve to the player who advances. A winner must be determined within 14 days after the scheduled start.",
        }, sport="tennis", outcome="Terence Atmane")
        self.assertEqual(polymarket["payouts"]["retirement_win"], "1")
        self.assertEqual(polymarket["payouts"]["walkover_win"], "0.5")
        self.assertEqual(kalshi["payouts"]["walkover_win"], "fair_market_price")
        compared = S.compare_profiles(kalshi, polymarket)
        self.assertEqual(compared["status"], "compatible")
        self.assertEqual(compared["contingency_status"], "different")
        self.assertEqual(S.compare_profiles(kalshi, polymarket, strict=True)["status"], "incompatible")

    def test_known_tennis_retirement_payout_difference_cannot_be_accepted(self):
        raw = {"description": "If Terence Atmane advances against Rinky Hijikata, resolve to Terence Atmane. If one player advances due to the opponent's retirement, default, or disqualification, this market will resolve to the player who advances."}
        left = S.profile_market("polymarket_intl", raw, sport="tennis", outcome="Terence Atmane")
        right = copy.deepcopy(left)
        right["payouts"]["retirement_win"] = "0.5"
        comparison = S.compare_profiles(left, right)
        self.assertEqual(comparison["status"], "incompatible")
        self.assertTrue(any("retirement_win" in reason for reason in comparison["differences"]))

    def test_mma_draw_and_no_contest_payouts_are_required_core_evidence(self):
        kalshi = S.profile_market("kalshi", {
            "rules_primary": "If Jasmin Lucindo wins the professional MMA fight, resolve Yes.",
            "rules_secondary": "If the fight ends in a draw/no contest, both markets resolve to $0.50.",
        }, sport="mma", outcome="Jasmin Lucindo")
        polymarket = S.profile_market("polymarket_intl", {
            "description": "This market resolves to the officially declared winner. If declared a draw or technical draw, ruled a No Contest, this market resolves 50-50.",
        }, sport="mma", outcome="Jasmin Lucindo")
        self.assertEqual(S.compare_profiles(kalshi, polymarket)["status"], "compatible")
        polymarket["payouts"]["no_contest"] = None
        self.assertEqual(S.compare_profiles(kalshi, polymarket)["status"], "unknown")
        polymarket["payouts"]["no_contest"] = "0"
        self.assertEqual(S.compare_profiles(kalshi, polymarket)["status"], "incompatible")

    def test_mma_separate_draw_payout_does_not_infer_no_contest_payment(self):
        kalshi = S.profile_market("kalshi", {
            "rules_primary": "If Jasmin Lucindo wins the fight, resolve Yes.",
            "rules_secondary": "If the fight ends in a draw/no contest, both markets resolve to $0.50.",
        }, sport="mma", outcome="Jasmin Lucindo")
        polymarket = S.profile_market("polymarket_intl", {
            "description": "The market resolves to the officially declared winner. If the fight is declared a draw or technical draw, this market will resolve 50-50. If the fight is ruled a No Contest, this market will resolve to No.",
        }, sport="mma", outcome="Jasmin Lucindo")
        self.assertIsNone(polymarket["payouts"]["no_contest"])
        self.assertNotEqual(S.compare_profiles(kalshi, polymarket)["status"], "compatible")

    def test_soccer_draw_target_is_not_a_team_win(self):
        kalshi = S.profile_market(
            "kalshi", {"rules_primary": "If Tie is the result, the market resolves Yes.",
                       "rules_secondary": "After 90 minutes plus stoppage time (does not include extra time or penalties)."},
            sport="soccer", outcome="draw",
        )
        self.assertEqual(kalshi["payouts"]["draw"], "1")
        self.assertEqual(kalshi["payouts"]["win"], "0")
        self.assertEqual(kalshi["payouts"]["loss"], "0")

    def test_mlb_does_not_guess_extra_innings_from_official_final_statistics(self):
        kalshi = {"rules_primary": "If Cleveland wins the baseball game, the market resolves Yes.",
                  "_settlement_documents": [{"url": "https://assets.kalshi.com/contract_terms/BASEBALLGAMEWIN.pdf",
                                             "text": "including all regulation innings and any extra innings or tie-breaking procedures employed by the governing body"}]}
        intl = {"description": "If Cleveland wins the game. The source is official final statistics. A tied game resolves 50-50."}
        a = S.profile_market("kalshi", kalshi, sport="baseball", outcome="Cleveland")
        b = S.profile_market("polymarket_intl", intl, sport="baseball", outcome="Cleveland")
        self.assertEqual(a["policies"]["extra_innings"], "included")
        self.assertEqual(S.compare_profiles(a, b)["status"], "unknown")
        intl["description"] += " All extra innings are included in determining the winner."
        b = S.profile_market("polymarket_intl", intl, sport="baseball", outcome="Cleveland")
        self.assertEqual(S.compare_profiles(a, b)["status"], "compatible")

    def test_official_kalshi_terms_establish_nfl_overtime_and_tie_rules(self):
        document = {
            "url": "https://assets.kalshi.com/contract_terms/FOOTBALLGAMEWIN.pdf",
            "text": (
                "All overtime periods are included in determining the winner. "
                "If the game ends in a tie and no Tie strike is listed, each side "
                "is paid $0.50."
            ),
        }
        profile = S.profile_market(
            "kalshi", {"rules_primary": "If Kansas City wins the football game, the market resolves Yes.",
                       "_settlement_documents": [document]},
            sport="football", outcome="Kansas City",
        )
        self.assertEqual(profile["policies"]["overtime"], "included")
        self.assertEqual(profile["payouts"]["tie"], "0.5")
        self.assertNotIn("policy:overtime", profile["core_missing"])

    def test_official_kalshi_terms_establish_nhl_shootout_and_mlb_extra_innings(self):
        hockey = S.profile_market(
            "kalshi", {"rules_primary": "If Boston wins the hockey game, the market resolves Yes.",
                       "_settlement_documents": [{
                           "url": "https://assets.kalshi.com/contract_terms/HOCKEYWINNINGINPERIOD.pdf",
                           "text": "An entire hockey game includes regulation time, overtime, and the shootout.",
                       }]},
            sport="hockey", outcome="Boston",
        )
        baseball = S.profile_market(
            "kalshi", {"rules_primary": "If Boston wins the baseball game, the market resolves Yes.",
                       "_settlement_documents": [{
                           "url": "https://assets.kalshi.com/contract_terms/BASEBALLGAMEWIN.pdf",
                           "text": "All regulation innings and any extra innings are included in determining the winner.",
                       }]},
            sport="baseball", outcome="Boston",
        )
        self.assertEqual(hockey["policies"]["overtime"], "included")
        self.assertEqual(hockey["policies"]["shootout"], "included")
        self.assertEqual(baseball["policies"]["extra_innings"], "included")

    def test_evidence_is_retained_without_truncating_rules(self):
        raw = {**KALSHI_THREE, "description": "description", "resolutionSource": "https://example.com/rules"}
        saved = S.resolution_evidence(raw)
        self.assertEqual(saved["rules_secondary"], raw["rules_secondary"])
        self.assertEqual(saved["resolutionSource"], raw["resolutionSource"])


class SettlementMarketMatchingTests(unittest.TestCase):
    def pair(self, first_profile, second_profile):
        key = C.FixtureKey("2026-10-20", frozenset({"australia", "new zealand"}), "M", "T20")
        fixtures = [C.Fixture(key, venue, venue, competition="International",
                              team_a="Australia", team_b="New Zealand")
                    for venue in ("kalshi", "polymarket_intl")]
        accepted, _, _ = M.match_fixtures(fixtures)
        markets = [C.Market(f.venue, f.venue_fixture_id, f.key, C.MATCH_WINNER, "Australia",
                            market_id=f.venue, extra={"settlement": profile})
                   for f, profile in zip(fixtures, (first_profile, second_profile))]
        markets[0].bid, markets[0].ask = "0.7", "0.8"
        markets[1].bid, markets[1].ask = "0.3", "0.4"
        return M.match_markets(accepted, markets), markets

    def test_incompatible_payouts_never_enter_accepted_cross_scanner(self):
        a, b = known_profile(), known_profile()
        b["payouts"]["draw"] = "0"
        (accepted, review, unmatched), markets = self.pair(a, b)
        self.assertGreater(Decimal(markets[0].bid), Decimal(markets[1].ask))
        self.assertEqual(accepted, [])
        self.assertEqual(len(review), 1)
        self.assertEqual(unmatched, [])
        self.assertIn("draw:", review[0].reason)

    def test_equal_models_retain_true_quote_crosses(self):
        (accepted, review, unmatched), markets = self.pair(known_profile(), known_profile())
        self.assertEqual(len(accepted), 1)
        self.assertEqual(review, [])
        self.assertEqual(unmatched, [])
        self.assertGreater(Decimal(markets[0].bid), Decimal(markets[1].ask))

    def test_pre_start_cancellation_difference_allows_explicitly_conditional_comparison(self):
        a, b = known_profile(), known_profile()
        a["payouts"]["cancelled_before_start"] = "fair_market_price"
        (accepted, review, unmatched), _ = self.pair(a, b)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(review, [])
        self.assertEqual(unmatched, [])
        self.assertEqual(accepted[0].comparison_basis, "normal_completion")
        self.assertEqual(accepted[0].contingency_status, "different")
        self.assertTrue(accepted[0].caveats)
        self.assertIn("conditional", accepted[0].reason)

    def test_both_unknown_models_are_reviewed(self):
        (accepted, review, _), _ = self.pair({}, {})
        self.assertEqual(accepted, [])
        self.assertEqual(len(review), 1)
        self.assertIn("unverified", review[0].reason)


if __name__ == "__main__":
    unittest.main()
