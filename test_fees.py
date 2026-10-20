import json
import unittest
from decimal import Decimal

import fees as F
from liquidity import LiquidityPolicy


def kalshi(multiplier="1", precision="0.01"):
    return F.compile_schedule("kalshi", {"taker_fee": {
        "model": "kalshi_quadratic", "multiplier": multiplier,
        "balance_precision": precision, "source": "test_series_api", "checked_at": "2026-10-20"}})


def polymarket(rate="0.05", exponent=1):
    return F.compile_schedule("polymarket_intl", {"feesEnabled": True, "feeSchedule": {
        "rate": rate, "exponent": exponent, "takerOnly": True}})


class FeeTests(unittest.TestCase):
    def test_current_sports_curve_and_both_taker_sides(self):
        schedule = polymarket()
        for side in ("buy", "sell"):
            self.assertEqual(F.estimate_taker_fee(schedule, ".5", 100, side=side).amount, Decimal("1.25"))
            self.assertEqual(F.estimate_taker_fee(schedule, ".3", 100, side=side).amount, Decimal("1.05"))
            self.assertEqual(F.estimate_taker_fee(schedule, ".7", 100, side=side).amount, Decimal("1.05"))

    def test_explicit_nonlinear_curve_uses_market_exponent(self):
        schedule = polymarket("0.25", 2)
        self.assertEqual(F.estimate_taker_fee(schedule, ".5", 100).amount, Decimal("1.56250"))
        self.assertEqual(F.estimate_taker_fee(schedule, ".3", 100).amount, Decimal("1.10250"))
        self.assertEqual(F.estimate_taker_fee(schedule, ".1", 100).amount, Decimal("0.20250"))

    def test_pm_rounding_and_subminimum_fee(self):
        self.assertEqual(F.estimate_taker_fee(polymarket(), ".25", 1).amount, Decimal(".00938"))
        self.assertEqual(F.estimate_taker_fee(polymarket(), ".00001", 1).amount, Decimal("0"))
        self.assertEqual(F.estimate_taker_fee(polymarket(), ".5", ".001").amount, Decimal(".00001"))

    def test_kalshi_account_precision_and_series_multiplier(self):
        self.assertEqual(F.estimate_taker_fee(kalshi(), ".5").amount, Decimal(".02"))
        self.assertEqual(F.estimate_taker_fee(kalshi(), ".5", 100).amount, Decimal("1.75"))
        self.assertEqual(F.estimate_taker_fee(kalshi(".5"), ".5", 100).amount, Decimal(".88"))
        self.assertEqual(F.estimate_taker_fee(kalshi(precision=".0001"), ".5").amount, Decimal(".0175"))
        self.assertEqual(F.estimate_taker_fee(kalshi("0"), ".5").amount, Decimal("0"))

    def test_fractional_kalshi_cost_respects_signed_cash_alignment(self):
        # Official fee-rounding example: buy revenue -.055; model .00363825.
        fee = F.estimate_taker_fee(kalshi(), ".055")
        self.assertEqual(fee.model_fee, Decimal(".00363825"))
        self.assertEqual(fee.trade_fee, Decimal(".003639"))
        self.assertEqual(fee.rounding_fee, Decimal(".001361"))
        self.assertEqual(fee.amount, Decimal(".005"))
        # Selling aligns positive revenue down, not the buy's position cost up.
        sell = F.estimate_taker_fee(kalshi(), ".055", side="sell")
        self.assertEqual(sell.amount, Decimal(".005"))
        self.assertEqual(F.estimate_taker_fee(kalshi(), ".054", side="buy").amount, Decimal(".006"))
        self.assertEqual(F.estimate_taker_fee(kalshi(), ".054", side="sell").amount, Decimal(".004"))

    def test_unknown_fees_do_not_become_free(self):
        for venue in ("kalshi", "polymarket_intl"):
            missing = F.compile_schedule(venue, {})
            self.assertFalse(missing.known)
            self.assertIsNone(F.estimate_taker_fee(missing, ".5").amount)
        self.assertFalse(F.compile_schedule("polymarket_intl", {"feesEnabled": True, "base_fee": 1000}).known)
        self.assertFalse(F.compile_schedule("kalshi", {"fee_type": "quadratic"}).known)
        self.assertFalse(F.compile_schedule("kalshi", {"fee_type": "flat", "fee_multiplier": 1}).known)
        failed = F.compile_schedule("kalshi", {"taker_fee": {"model": "unknown", "reason": "refresh_failed"}})
        self.assertEqual(failed.reason, "refresh_failed")

    def test_explicit_fee_free_is_known_and_not_missing(self):
        schedule = F.compile_schedule("polymarket_intl", {"feesEnabled": False})
        self.assertTrue(schedule.known)
        self.assertEqual(F.estimate_taker_fee(schedule, ".5", 100).amount, Decimal("0"))
        self.assertFalse(F.compile_schedule("unknown_venue", {"taker_fee": {"model": "zero"}}).known)

    def test_metadata_round_trip_and_raw_series_metadata(self):
        original = kalshi(".5")
        self.assertEqual(F.compile_schedule("kalshi", json.dumps(original.to_dict())), original)
        self.assertEqual(F.compile_schedule("kalshi", {"_kalshi_series": {
            "fee_type": "quadratic_with_maker_fees", "fee_multiplier": ".5"}}).multiplier, Decimal(".5"))
        self.assertEqual(F.compile_schedule("polymarket_intl", json.dumps(polymarket().to_dict())), polymarket())

    def test_invalid_inputs_cannot_produce_a_fee_or_edge(self):
        for bad in (True, None, "", "NaN", "Infinity", "-.1", "1.1"):
            self.assertIsNone(F.estimate_taker_fee(kalshi(), bad).amount, bad)
        for bad in (True, None, "NaN", "Infinity", "-1"):
            self.assertIsNone(F.estimate_taker_fee(kalshi(), ".5", bad).amount, bad)
        self.assertIsNone(F.estimate_taker_fee(kalshi(), ".5", side="maker").amount)
        self.assertIsNone(F.taker_edge(kalshi(), ".6", polymarket(), ".5", 0).net_gap)
        for raw in ({"feeSchedule": {"rate": "NaN", "exponent": 1}},
                    {"feeSchedule": {"rate": ".05"}},
                    {"feeSchedule": {"rate": ".05", "exponent": -1}}):
            self.assertFalse(F.compile_schedule("polymarket_intl", raw).known)

    def test_valid_price_endpoints_and_zero_quantity_have_zero_fees(self):
        for schedule in (kalshi(), polymarket()):
            for endpoint in ("0", "1"):
                self.assertEqual(F.estimate_taker_fee(schedule, endpoint, 100).amount, Decimal("0"))
            self.assertEqual(F.estimate_taker_fee(schedule, ".5", 0).amount, Decimal("0"))

    def test_net_edge_charges_both_legs_and_preserves_gross_cross(self):
        result = F.taker_edge(kalshi(), ".51", polymarket(), ".5")
        self.assertEqual(result.gross_gap, Decimal(".01"))
        self.assertEqual(result.sell_fee, Decimal(".02"))
        self.assertEqual(result.buy_fee, Decimal(".01250"))
        self.assertEqual(result.net_gap, Decimal("-.02250"))
        self.assertFalse(result.net_positive)

    def test_top_depth_uses_whole_paired_capacity_and_rounds_order_totals(self):
        result = F.taker_edge(kalshi(), ".6", polymarket(), ".5", sell_size="12.7", buy_size="8.4")
        self.assertEqual(result.top_size, Decimal("8.4"))
        self.assertEqual(result.top_quantity, Decimal("8"))
        self.assertEqual(result.net_gap, Decimal(".06750"))
        self.assertEqual(result.top_net_total, Decimal(".56"))
        # Per-order rounding means size 8 is not 8 times the one-contract edge.
        self.assertNotEqual(result.top_net_total, result.net_total * result.top_quantity)
        self.assertTrue(result.to_dict()["within_depth"])

    def test_unknown_depth_and_partial_depth_are_explicit(self):
        for sell_size, buy_size in ((None, None), ("10", None), ("NaN", "8")):
            edge = F.taker_edge(kalshi(), ".6", polymarket(), ".5", sell_size=sell_size, buy_size=buy_size)
            self.assertIsNone(edge.top_size)
            self.assertIsNone(edge.top_net_total)
            self.assertIsNone(edge.to_dict()["within_depth"])
        edge = F.taker_edge(kalshi(), ".6", polymarket(), ".5", sell_size=".5", buy_size="8")
        self.assertEqual(edge.top_quantity, Decimal("0"))
        self.assertIsNone(edge.top_net_total)
        self.assertFalse(edge.to_dict()["within_depth"])

    def test_unknown_leg_hides_net_but_retains_gross_and_known_leg_fee(self):
        edge = F.taker_edge(kalshi(), ".6", F.compile_schedule("polymarket_intl"), ".5")
        self.assertEqual(edge.status, "unavailable")
        self.assertEqual(edge.gross_gap, Decimal(".1"))
        self.assertEqual(edge.sell_fee, Decimal(".02"))
        self.assertIsNone(edge.total_fees)
        self.assertIsNone(edge.net_total)
        self.assertIn("buy:missing_fee_metadata", edge.reason)

    def test_flat_csv_api_decodes_metadata_without_binary_float_errors(self):
        row = {"kalshi_bid": ".6", "kalshi_bid_size": "8", "polymarket_intl_ask": ".5",
               "polymarket_intl_ask_size": "7", "kalshi_fee_schedule": json.dumps(kalshi().to_dict()),
               "polymarket_intl_fee_schedule": json.dumps(polymarket().to_dict())}
        edge = F.calculate_taker_cross(row, "polymarket_intl", "kalshi")
        self.assertEqual(Decimal(edge["net_gap"]), Decimal(".06750"))
        self.assertEqual(Decimal(edge["top_quantity"]), Decimal("7"))
        self.assertTrue(edge["net_positive"])
        json.dumps(edge, allow_nan=False)


class SizedFeeTests(unittest.TestCase):
    def size(self, **kwargs):
        return F.sized_taker_edge(kalshi(), ".6", polymarket(), ".5",
                                  sell_size="1000", buy_size="1000", **kwargs)

    def test_requested_quantity_fees_gross_net_and_capital_are_separate(self):
        estimate = self.size(desired_contracts=100)
        result = estimate.to_dict()
        self.assertEqual(result["sized_quantity"], "100")
        self.assertEqual(Decimal(result["gross_total"]), Decimal("10"))
        self.assertEqual(Decimal(result["sell_fee"]), Decimal("1.68"))
        self.assertEqual(Decimal(result["buy_fee"]), Decimal("1.25"))
        self.assertEqual(Decimal(result["net_total"]), Decimal("7.07"))
        self.assertEqual(Decimal(result["break_even_gap"]), Decimal(".0293"))
        self.assertEqual(Decimal(result["capital_required"]), Decimal("92.93"))
        self.assertEqual(result["capital_basis"], "fully_collateralized_pair_estimate")
        self.assertTrue(result["actionable_estimate"])
        self.assertFalse(result["execution_validated"])
        self.assertAlmostEqual(float(result["return_on_capital_percent"]), 7.6078769)

    def test_two_and_three_cent_crosses_can_be_consumed_by_both_taker_fees(self):
        for bid, gross, net in ((".52", "2", "-1"), (".53", "3", "0")):
            result = F.sized_taker_edge(kalshi(), bid, polymarket(), ".50",
                desired_contracts=100, sell_size=1000, buy_size=1000).to_dict()
            self.assertEqual(Decimal(result["gross_total"]), Decimal(gross))
            self.assertEqual(Decimal(result["total_fees"]), Decimal("3"))
            self.assertEqual(Decimal(result["net_total"]), Decimal(net))
            self.assertFalse(result["actionable_estimate"])
        # Near a price extreme the exact same 2c gross gap survives lower fees.
        result = F.sized_taker_edge(kalshi(), ".12", polymarket(), ".10",
            desired_contracts=100, sell_size=1000, buy_size=1000).to_dict()
        self.assertEqual(Decimal(result["net_total"]), Decimal(".81"))
        self.assertTrue(result["actionable_estimate"])

    def test_size_caps_to_confirmed_whole_paired_depth_and_reprices_fees(self):
        result = F.sized_taker_edge(kalshi(), ".6", polymarket(), ".5",
            desired_contracts=100, sell_size="12.7", buy_size="8.4",
            policy=LiquidityPolicy(min_top_notional_usd=0)).to_dict()
        self.assertEqual(result["desired_quantity"], "100")
        self.assertEqual(result["sized_quantity"], "8")
        self.assertEqual(result["cap_reasons"], ["top_depth"])
        self.assertEqual(Decimal(result["net_total"]), Decimal(".56"))
        self.assertNotEqual(Decimal(result["net_total"]), Decimal(".0675") * 8)
        self.assertTrue(result["within_depth"])

    def test_budget_caps_using_both_fees_and_collateral_without_orderbook_walk(self):
        result = self.size(desired_contracts=100, max_capital_usd="9.29").to_dict()
        self.assertEqual(result["sized_quantity"], "9")
        self.assertEqual(result["cap_reasons"], ["capital_budget"])
        self.assertLessEqual(Decimal(result["capital_required"]), Decimal("9.29"))
        next_size = self.size(desired_contracts=10).to_dict()
        self.assertGreater(Decimal(next_size["capital_required"]), Decimal("9.29"))
        tiny = self.size(max_capital_usd=".01").to_dict()
        self.assertEqual(tiny["sized_quantity"], "0")
        self.assertIsNone(tiny["net_total"])
        self.assertFalse(tiny["actionable_estimate"])

    def test_unknown_depth_or_fees_does_not_become_zero_cost_actionable_pnl(self):
        unknown_depth = F.sized_taker_edge(kalshi(), ".6", polymarket(), ".5", buy_size=1000).to_dict()
        self.assertEqual(Decimal(unknown_depth["gross_gap"]), Decimal(".1"))
        self.assertIsNone(unknown_depth["sized_quantity"])
        self.assertIsNone(unknown_depth["net_total"])
        self.assertEqual(unknown_depth["liquidity_status"], "unknown")
        unknown_fees = F.sized_taker_edge(kalshi(), ".6", F.FeeSchedule(), ".5",
            buy_size=1000, sell_size=1000).to_dict()
        self.assertIsNone(unknown_fees["net_total"])
        self.assertIsNone(unknown_fees["capital_required"])
        self.assertFalse(unknown_fees["actionable_estimate"])
        budget_unknown = F.sized_taker_edge(kalshi(), ".6", F.FeeSchedule(), ".5",
            buy_size=1000, sell_size=1000, max_capital_usd=10).to_dict()
        self.assertEqual(budget_unknown["reason"], "unknown_fees_for_budget")
        self.assertIsNone(budget_unknown["sized_quantity"])

    def test_thin_liquidity_remains_a_diagnostic_but_is_screened_out(self):
        result = F.sized_taker_edge(kalshi(), ".6", polymarket(), ".5",
            sell_size=20, buy_size=20).to_dict()
        self.assertEqual(Decimal(result["paired_top_notional_usd"]), Decimal("10"))
        self.assertGreater(Decimal(result["net_total"]), 0)
        self.assertEqual(result["liquidity_status"], "insufficient")
        self.assertFalse(result["actionable_estimate"])

    def test_known_minimum_order_size_and_notional_are_checked_after_budget(self):
        result = self.size(desired_contracts=100, max_capital_usd="9.29", buy_min_order_size="10").to_dict()
        self.assertEqual(result["minimum_order_status"], "below_minimum")
        self.assertFalse(result["actionable_estimate"])
        result = self.size(desired_contracts=10, buy_min_order_notional_usd="5.01").to_dict()
        self.assertEqual(result["minimum_order_reason"], "below:buy_notional")
        result = self.size(desired_contracts=10, buy_min_order_size="10", buy_min_order_notional_usd="5").to_dict()
        self.assertEqual(result["minimum_order_status"], "known_minimums_met")

    def test_top_preset_and_row_wrapper_are_json_safe(self):
        row = {"kalshi_bid": ".6", "kalshi_bid_size": "8.7", "polymarket_intl_ask": ".5",
               "polymarket_intl_ask_size": "12", "kalshi_fee_schedule": json.dumps(kalshi().to_dict()),
               "polymarket_intl_fee_schedule": json.dumps(polymarket().to_dict()),
               "polymarket_intl_minimum_order_size": "10"}
        result = F.calculate_sized_taker_cross(row, "polymarket_intl", "kalshi", desired_contracts="top",
                                              min_top_notional_usd=0)
        self.assertEqual(result["sized_quantity"], "8")
        self.assertEqual(result["minimum_order_status"], "below_minimum")
        self.assertFalse(result["actionable_estimate"])
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
