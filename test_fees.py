import json
import unittest
from decimal import Decimal

import fees as F


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


if __name__ == "__main__":
    unittest.main()
