import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import coverage_audit as A
import fees as F
from test_runtime import write_runtime_snapshot


NOW = datetime(2026, 10, 21, 1, tzinfo=timezone.utc)


class CoverageAuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.rows = write_runtime_snapshot(self.root, stamp=NOW.isoformat())

    def test_both_directions_use_sell_bid_buy_ask_and_their_own_depth(self):
        row = self.rows[0]
        row.update(kalshi_bid=".44", kalshi_ask=".45", kalshi_bid_size="5000",
                   kalshi_ask_size="8.4", polymarket_intl_bid=".48",
                   polymarket_intl_ask=".49", polymarket_intl_bid_size="15.8")
        result = A.audit_rows([row], as_of=NOW)
        routes = {(r["buy"], r["sell"]): r for r in result["routes"]}
        forward = routes["kalshi", "polymarket_intl"]
        self.assertEqual(Decimal(forward["gross_gap"]), Decimal(".03"))
        self.assertEqual(forward["sized_quantity"], "8")
        self.assertEqual(Decimal(forward["net_total"]), Decimal(".24"))
        self.assertEqual(Decimal(forward["paired_top_notional_usd"]), Decimal("3.60"))
        self.assertEqual(forward["exclusion"], "liquidity:below_bound")
        reverse = routes["polymarket_intl", "kalshi"]
        self.assertEqual(Decimal(reverse["gross_gap"]), Decimal("-.05"))
        self.assertEqual(reverse["sized_quantity"], "100")
        self.assertEqual(result["counts"]["both_directions_evaluated_rows"], 1)
        self.assertEqual(result["counts"]["positive_after_filters_routes"], 0)

    def test_actual_order_fees_are_recomputed_and_subtracted(self):
        row = self.rows[0]
        row.update(kalshi_bid=".52", kalshi_ask=".55", polymarket_intl_bid=".48",
                   polymarket_intl_ask=".50",
                   kalshi_fee_metadata=json.dumps({"taker_fee": {
                       "model": "kalshi_quadratic", "multiplier": "1", "balance_precision": ".01"}}),
                   polymarket_intl_fee_metadata=json.dumps({"taker_fee": {
                       "model": "polymarket_curve", "rate": ".05", "exponent": 1}}))
        route = A.audit_rows([row], as_of=NOW)["near_misses"][0]
        self.assertEqual(Decimal(route["gross_total"]), Decimal("2"))
        self.assertEqual(Decimal(route["total_fees"]), Decimal("3"))
        self.assertEqual(Decimal(route["net_total"]), Decimal("-1"))
        self.assertEqual(Decimal(route["net_cents"]), Decimal("-1"))

    def test_yes_sale_and_no_purchase_have_complementary_prices_and_equal_modeled_fees(self):
        schedules = (F.compile_schedule("kalshi", {"taker_fee": {
            "model": "kalshi_quadratic", "multiplier": "1", "balance_precision": ".01"}}),
            F.compile_schedule("polymarket_intl", {"taker_fee": {
                "model": "polymarket_curve", "rate": ".05", "exponent": 1}}))
        for schedule in schedules:
            for price in (Decimal(".054"), Decimal(".52"), Decimal(".713")):
                sale = F.estimate_taker_fee(schedule, price, 14, side="sell")
                no_buy = F.estimate_taker_fee(schedule, 1 - price, 14, side="buy")
                self.assertEqual(sale.amount, no_buy.amount)
                self.assertEqual(Decimal(14) * (1 - price), Decimal(14) - Decimal(14) * price)

    def test_review_and_skew_are_not_missing_depth_or_profitable_signals(self):
        self.rows[0]["comparison_status"] = "fixture_review"
        self.rows[1]["polymarket_intl_quote_time"] = (NOW - timedelta(seconds=31)).isoformat()
        result = A.audit_rows(self.rows, as_of=NOW)
        self.assertEqual(result["depth"]["no_estimate_but_measured"], 2)
        self.assertEqual(result["depth"]["missing_size"], 0)
        self.assertEqual(result["near_misses"], [])
        self.assertEqual(result["positive_estimates"], [])
        self.assertEqual(result["route_exclusions"], {"contract:fixture_review": 2, "quote:skewed": 2})

    def test_missing_quantities_and_unpriced_direction_are_distinct(self):
        row = self.rows[0]
        row["kalshi_bid_size"] = row["kalshi_ask_size"] = ""
        self.assertEqual(A.audit_rows([row], as_of=NOW)["depth"]["missing_size"], 1)
        row["kalshi_bid"] = ""
        result = A.audit_rows([row], as_of=NOW)
        self.assertIn("quote:unpriced", result["route_exclusions"])
        self.assertNotIn("quote:fresh", result["route_exclusions"])

    def test_near_misses_are_twenty_distinct_outcomes_ranked_below_profitability(self):
        row = self.rows[0]
        row.update(kalshi_bid=".48", kalshi_ask=".50", polymarket_intl_bid=".49",
                   polymarket_intl_ask=".51")
        rows = [{**row, "canonical_outcome": "Team " + str(i)} for i in range(24)]
        result = A.audit_rows(rows, as_of=NOW)
        self.assertEqual(len(result["near_misses"]), 20)
        self.assertEqual(len({r["row"] for r in result["near_misses"]}), 20)
        self.assertTrue(all(Decimal(r["net_gap"]) == Decimal("-.01") for r in result["near_misses"]))
        self.assertTrue(all(r["buy"] == "kalshi" for r in result["near_misses"]))

    def test_stale_future_and_invalid_books_cannot_enter_ranked_results(self):
        for field, value in (("kalshi_quote_time", (NOW - timedelta(seconds=301)).isoformat()),
                             ("kalshi_quote_time", (NOW + timedelta(seconds=1)).isoformat()),
                             ("kalshi_bid", ".99")):
            with self.subTest(field=field, value=value):
                result = A.audit_rows([{**self.rows[0], field: value}], as_of=NOW)
                self.assertEqual(result["near_misses"], [])
                self.assertEqual(result["positive_estimates"], [])

    def test_snapshot_is_offline_uses_saved_time_and_writes_separate_reports(self):
        data = self.root / "output/data"
        meta_path = data / "snapshot_meta.json"
        meta = json.loads(meta_path.read_text())
        with self.assertRaisesRegex(ValueError, "timestamp"):
            A.audit_snapshot(data)
        meta["quotes_checked_at_utc"] = NOW.isoformat()
        meta_path.write_text(json.dumps(meta))
        before = {p: p.read_bytes() for p in data.iterdir() if p.is_file()}
        with patch("urllib.request.urlopen", side_effect=AssertionError("Audit requested network")):
            result = A.audit_snapshot(data)
            A.write_reports(result, self.root / "diagnostics")
        self.assertEqual(result["as_of"], NOW.replace(microsecond=999999).isoformat())
        self.assertEqual(result["counts"]["fresh_comparable_rows"], 2)
        self.assertEqual(before, {p: p.read_bytes() for p in data.iterdir() if p.is_file()})
        self.assertEqual({p.name for p in (self.root / "diagnostics").iterdir()},
                         {"summary.json", "routes.csv", "near_misses.csv", "report.md"})


if __name__ == "__main__":
    unittest.main()
