import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from liquidity import LiquidityPolicy


class LiquidityTests(unittest.TestCase):
    def setUp(self):
        self.policy = LiquidityPolicy()
        self.now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    def decision(self, venue, extra, **kwargs):
        return self.policy.discovery_decision(venue, extra, now=self.now, **kwargs)

    def test_paired_depth_must_clear_dollar_bound_on_both_selected_legs(self):
        # A large sell book cannot disguise a tiny paired buy leg.
        result = self.policy.assess_route(".9", ".1", "5000", "200")
        self.assertEqual(result.paired_quantity, Decimal("200"))
        self.assertEqual(result.sell_notional_usd, Decimal("180"))
        self.assertEqual(result.buy_notional_usd, Decimal("20"))
        self.assertEqual(result.limiting_notional_usd, Decimal("20"))
        self.assertEqual(result.status, "insufficient")
        sufficient = self.policy.assess_route(".9", ".1", "5000", "1000")
        self.assertTrue(sufficient.sufficient)
        self.assertEqual(sufficient.limiting_notional_usd, Decimal("100"))

    def test_quantity_is_whole_and_unknown_depth_never_becomes_zero(self):
        thin = self.policy.assess_route(".7", ".5", "200", "199.99")
        self.assertEqual(thin.paired_quantity, Decimal("199"))
        self.assertEqual(thin.status, "insufficient")
        for bad in (None, "", True, "NaN", "Infinity", "-1"):
            unknown = self.policy.assess_route(".7", ".5", "200", bad)
            self.assertEqual(unknown.status, "unknown")
            self.assertIsNone(unknown.limiting_notional_usd)
        self.assertEqual(self.policy.assess_route(".7", ".5", "0", "200").status, "insufficient")

    def test_fresh_explicit_low_clob_liquidity_avoids_book_probe(self):
        result = self.decision("polymarket_intl", {"liquidityClob": "10"}, observed_at=self.now)
        self.assertTrue(result.skip_quotes)
        self.assertEqual(result.verified_notional_usd, Decimal("10"))
        self.assertFalse(self.decision("polymarket_intl", {"liquidityClob": "100"}, observed_at=self.now).skip_quotes)
        self.assertTrue(self.decision("polymarket_intl", {"liquidityClob": "0"}, observed_at=self.now).skip_quotes)

    def test_missing_stale_future_and_volume_proxy_never_exclude(self):
        for raw in ({}, {"liquidity": "1"}, {"volume": "1", "open_interest": "1"},
                    {"liquidityClob": "NaN"}, {"liquidityClob": None}):
            self.assertFalse(self.decision("polymarket_intl", raw, observed_at=self.now).skip_quotes)
        for stamp in (None, self.now - timedelta(seconds=300), self.now + timedelta(seconds=1),
                      "2026-10-21T00:00:00"):
            self.assertFalse(self.decision("polymarket_intl", {"liquidityClob": "10"}, observed_at=stamp).skip_quotes)
        # Exact TTL reprobes the market so a once-small book can return.
        self.assertFalse(self.decision("polymarket_intl", {"liquidityClob": "10"},
                                       observed_at=self.now - timedelta(seconds=60), max_age_seconds=60).skip_quotes)

    def test_kalshi_requires_both_current_sides_below_bound(self):
        raw = {"yes_bid_dollars": ".7", "yes_ask_dollars": ".8",
               "yes_bid_size_fp": "10", "yes_ask_size_fp": "10",
               "liquidity_observed_at": self.now.isoformat()}
        self.assertTrue(self.decision("kalshi", raw).skip_quotes)
        raw["yes_ask_size_fp"] = "1000"
        self.assertFalse(self.decision("kalshi", raw).skip_quotes)
        raw["yes_ask_size_fp"] = None
        self.assertFalse(self.decision("kalshi", raw).skip_quotes)
        raw["yes_ask_size_fp"], raw["yes_ask_dollars"] = "10", ".6"
        self.assertFalse(self.decision("kalshi", raw).skip_quotes)

    def test_large_pm_aggregate_does_not_hide_thin_current_top_books(self):
        raw = {"liquidityClob": "100000", "liquidity_observed_at": self.now.isoformat()}
        kwargs = {"bid": ".4", "ask": ".5", "bid_size": "20", "ask_size": "20",
                  "observed_at": self.now}
        result = self.decision("polymarket_intl", raw, **kwargs)
        self.assertTrue(result.skip_quotes)
        self.assertEqual(result.reason, "below_discovery_top_bound")
        self.assertEqual(result.source, "polymarket_intl_current_bbo")
        # A large side can still participate as the buy/sell leg of a route.
        kwargs["ask_size"] = "1000"
        self.assertFalse(self.decision("polymarket_intl", raw, **kwargs).skip_quotes)
        kwargs["ask_size"] = "20"
        kwargs["observed_at"] = self.now - timedelta(seconds=300)
        self.assertFalse(self.decision("polymarket_intl", raw, **kwargs).skip_quotes)

    def test_pm_aggregate_and_book_freshness_cannot_refresh_each_other(self):
        raw = {"liquidityClob": "10",
               "liquidity_observed_at": (self.now - timedelta(seconds=300)).isoformat()}
        # Fresh large book cannot make an old low aggregate safe to prune.
        kwargs = {"bid": ".4", "ask": ".5", "bid_size": "1000", "ask_size": "1000",
                  "observed_at": self.now}
        self.assertFalse(self.decision("polymarket_intl", raw, **kwargs).skip_quotes)
        # Missing aggregate time likewise cannot borrow the BBO's time.
        raw.pop("liquidity_observed_at")
        self.assertFalse(self.decision("polymarket_intl", raw, **kwargs).skip_quotes)
        raw["liquidityClob"] = "10000"
        raw["liquidity_observed_at"] = self.now.isoformat()
        kwargs.update(bid_size="20", ask_size="20", observed_at=None)
        self.assertFalse(self.decision("polymarket_intl", raw, **kwargs).skip_quotes)
        # A fresh aggregate independently below the bound remains sufficient.
        raw["liquidityClob"] = "10"
        self.assertTrue(self.decision("polymarket_intl", raw, **kwargs).skip_quotes)
        # The explicit BBO stamp can independently restore a current top screen.
        raw["liquidityClob"] = "10000"
        kwargs["bbo_observed_at"] = self.now
        self.assertTrue(self.decision("polymarket_intl", raw, **kwargs).skip_quotes)

    def test_policy_validation_and_disabled_filter(self):
        for field in ("min_top_notional_usd", "desired_contracts", "max_capital_usd"):
            for bad in (True, "NaN", "Infinity", "-1", ""):
                with self.assertRaises(ValueError):
                    LiquidityPolicy(**{field: bad})
        policy = LiquidityPolicy(min_top_notional_usd="0", desired_contracts="100", max_capital_usd="200")
        self.assertFalse(policy.discovery_decision("polymarket_intl", {"liquidityClob": 0}).skip_quotes)
        self.assertTrue(policy.assess_route(".5", ".4", 1, 1).sufficient)
        json.dumps(policy.to_dict(), allow_nan=False)


if __name__ == "__main__":
    unittest.main()
