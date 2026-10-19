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

    def test_matching_known_draw_terms_do_not_hide_missing_cancellation_rules(self):
        a = S.profile_market("kalshi", KALSHI_HALF, outcome="Australia")
        b = S.profile_market("polymarket_intl", intl_description(), outcome="Australia")
        self.assertEqual(a["payouts"]["draw"], b["payouts"]["draw"])
        self.assertEqual(a["status"], "unknown")
        self.assertIn("policy:postponement", a["missing"])
        self.assertEqual(S.compare_profiles(a, b)["status"], "incompatible")
        self.assertIn("forfeit_before_start", S.compare_profiles(a, b)["reason"])

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
                self.assertEqual(S.compare_profiles(original, copy.deepcopy(original))["status"], "unknown")
                made = S.make_profile(sport="cricket", target_kind="team",
                                       payouts=original["payouts"], policies=original["policies"],
                                       evidence=original["evidence"])
                self.assertEqual(made["status"], "unknown")
                self.assertIn("payout:cancelled_before_start", made["missing"])

    def test_extra_symbolic_state_is_not_silently_excluded_from_comparison(self):
        a = known_profile()
        a["payouts"]["venue_discretionary_event"] = "fair_market_price"
        self.assertEqual(S.compare_profiles(a, copy.deepcopy(a))["status"], "unknown")

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
            self.assertEqual(S.compare_profiles(a, a)["status"], "unknown")
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
        self.assertEqual(S.compare_profiles(a, b)["status"], "incompatible")
        self.assertIn("cancelled_before_start", S.compare_profiles(a, b)["reason"])

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

    def test_both_unknown_models_are_reviewed(self):
        (accepted, review, _), _ = self.pair({}, {})
        self.assertEqual(accepted, [])
        self.assertEqual(len(review), 1)
        self.assertIn("unverified", review[0].reason)


if __name__ == "__main__":
    unittest.main()
