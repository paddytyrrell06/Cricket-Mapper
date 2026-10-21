"""The independent quote budget must never turn discovery into blanket polling."""
import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

import canonical as C
import quotes as Q
from catalog_monitor import CatalogQuoteMonitor
from market_catalog import MarketCatalog
from research_store import ResearchStore, utc


class CatalogQuoteMonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ResearchStore(Path(self.directory.name) / "eventlens.sqlite3")
        self.catalog = MarketCatalog(self.store)
        self.now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
        self.tick = 1000
        self.calls = []

    def add(self, venue, identity, *, liquidity=None, volume="0", status="active", tokens=None):
        if venue == "kalshi":
            market = {"ticker": identity, "title": "Inflation above 3%?", "status": status,
                      "volume_fp": volume, "yes_bid_dollars": ".9", "yes_ask_dollars": ".91",
                      "yes_bid_size_fp": "9999", "yes_ask_size_fp": "9999"}
            event = {"event_ticker": "event:" + identity, "category": "Economics", "markets": [market]}
        else:
            market = {"id": identity, "question": "Bitcoin above threshold?", "active": status == "active",
                      "closed": status == "closed", "volume": volume, "outcomes": '["No","Yes"]',
                      "clobTokenIds": json.dumps(tokens or [str(int(identity) * 10), str(int(identity) * 10 + 1)])}
            event = {"id": "event:" + identity, "tags": [{"slug": "crypto"}], "markets": [market]}
        if liquidity is not None:
            market["liquidityClob"] = liquidity
        rows = self.catalog._normalise(event, market, venue, utc(self.now))
        with self.store.connect() as db:
            db.executemany("INSERT OR REPLACE INTO research_catalog VALUES(?,?,?,?,?,?,?,?)", rows)

    def collect(self, markets, raw, **kwargs):
        self.calls.append((markets, raw, kwargs))
        for market in markets:
            market.bid, market.ask = ".40", ".42"
            market.quote_time, market.quote_source, market.quote_status = utc(self.now), "Native actual response", "available"
            market.extra.update(quote_bid_size="1000", quote_ask_size="1000",
                                quote_exchange_time="2026-10-21T11:59:59+00:00")
        return [], []

    def monitor(self, **kwargs):
        return CatalogQuoteMonitor(self.catalog, self.store, collector=self.collect, clock=lambda: self.now,
                                   timer=lambda: self.tick, **kwargs)

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.tick += seconds

    def test_exact_reversed_outcomes_recorded_separately_without_matching_or_fee_requests(self):
        self.add("polymarket_intl", "1", liquidity="500", tokens=["111", "222"])
        monitor = self.monitor()
        self.assertEqual(self.calls, [])
        stats = monitor.refresh()
        self.assertEqual((stats["monitored_count"], stats["recorded_count"], stats["liquid_count"]), (2, 2, 2))
        self.assertEqual(self.calls[0][2], {"live": True, "refresh_fees": False})
        rows = self.store.history("catalog_quotes")
        self.assertEqual({(row["outcome"], row["instrument_id"]) for row in rows}, {("No", "111"), ("Yes", "222")})
        self.assertTrue(all(row["observed_at"] == utc(self.now) for row in rows))
        saved = self.store.load("catalog_quotes")[0][0]["rows"]
        self.assertTrue(all(row["comparison_status"] == "unmatched" for row in saved))
        self.assertTrue(all(row["mapping_status"] == "unmatched" for row in saved))
        self.assertTrue(all(row["category"] == "Crypto" for row in saved))

    def test_volume_never_qualifies_as_liquidity_and_unknowns_rotate_with_probe_backoff(self):
        for number in range(6):
            self.add("kalshi", "K" + str(number), volume=str(1000000 - number))
        monitor = self.monitor(max_instruments=4, probe_budget=2)
        first = monitor.refresh()
        self.assertEqual((first["liquid_count"], first["unknown_probe_count"]), (0, 2))
        first_ids = {market.market_id for market in self.calls[-1][0]}
        self.advance(15)
        second = monitor.refresh()
        # Fresh actual measured sizes qualify the first two; other unknowns
        # get the reserved exploration budget rather than starving forever.
        self.assertEqual(second["unknown_probe_count"], 2)
        self.assertEqual(second["monitored_count"], 4)
        self.assertEqual(len({market.market_id for market in self.calls[-1][0]} - first_ids), 2)

    def test_total_budget_and_runtime_asset_deduplication(self):
        for number in range(8):
            self.add("polymarket_intl", str(number + 1), liquidity="10000")
        class Runtime:
            def research_rows(self):
                return [], {("polymarket_intl", "1", "yes"): "11", ("kalshi", "covered", "yes"): "covered"}
        monitor = self.monitor(runtime=Runtime(), max_instruments=5, probe_budget=1)
        stats = monitor.refresh()
        self.assertEqual(stats["monitored_count"], 5)
        markets, raw, _ = self.calls[0]
        assets = [Q._intl_asset(market, next(record for record in raw[market.venue]
                  if record["id"] == market.market_id)) for market in markets]
        self.assertNotIn("11", assets)
        self.assertEqual(len(set(assets)), 5)

    def test_thin_actual_depth_overrides_aggregate_hint_then_is_reprobed(self):
        self.add("polymarket_intl", "1", liquidity="1000")
        def thin(markets, raw, **kwargs):
            self.collect(markets, raw, **kwargs)
            for market in markets:
                market.extra.update(quote_bid_size="1", quote_ask_size="1")
            return [], []
        monitor = self.monitor()
        monitor.collector = thin
        self.assertEqual(monitor.refresh()["monitored_count"], 2)
        self.advance(15)
        self.assertEqual(monitor.refresh()["monitored_count"], 0)
        self.assertEqual(len(self.calls), 1)
        self.advance(46)
        self.assertEqual(monitor.refresh()["monitored_count"], 2)
        self.assertEqual(len(self.calls), 2)

    def test_missing_closed_unknown_or_uncertain_tokens_do_not_spend_quote_budget(self):
        self.add("kalshi", "closed", status="settled")
        self.add("kalshi", "unknown", status="unknown")
        self.add("polymarket_intl", "1", tokens=["bad", "222"])
        self.add("polymarket_intl", "2", tokens=["333"])
        monitor = self.monitor()
        stats = monitor.refresh()
        self.assertEqual(stats["monitored_count"], 1)
        self.assertEqual(len(self.catalog.markets()), 6)
        self.assertEqual(stats["filtered_count"], 5)

    def test_failed_native_refresh_never_records_snapshot_fallback_as_fresh(self):
        self.add("kalshi", "K1", liquidity="1000")
        def failed(markets, raw, **kwargs):
            self.assertNotIn("yes_bid_dollars", raw["kalshi"][0])
            for market in markets:
                market.quote_time = utc(self.now)
                market.quote_source = "Kalshi market snapshot yes_bid_dollars/yes_ask_dollars"
                market.bid, market.ask = ".90", ".91"
            return [], ["Native batch unavailable"]
        monitor = self.monitor()
        monitor.collector = failed
        result = monitor.refresh()
        self.assertEqual(result["recorded_count"], 0)
        self.assertEqual(self.store.datasets(), [])
        self.assertEqual(result["warnings"], ["Native batch unavailable"])

    def test_actual_shared_normalizers_use_batch_ids_without_live_fee_requests(self):
        self.add("kalshi", "K1", liquidity="1000")
        self.add("polymarket_intl", "1", liquidity="1000", tokens=["111", "222"])
        observed = utc(self.now)
        native = {"ticker": "K1", "status": "active", "yes_bid_dollars": ".51",
                  "yes_ask_dollars": ".54", "yes_bid_size_fp": "50", "yes_ask_size_fp": "60",
                  "_collected_at_utc": observed}
        books = {token: ({"bids": [{"price": ".45", "size": "300"}],
                           "asks": [{"price": ".46", "size": "400"}],
                           "timestamp": "2026-10-21T11:59:59+00:00"}, observed)
                 for token in ("111", "222")}
        monitor = self.monitor()
        monitor.collector = Q.enrich_quotes
        with patch.object(Q, "_live_kalshi", return_value=({"K1": native}, [])) as kalshi, \
             patch.object(Q, "_live_books", return_value=(books, {}, [])) as polymarket, \
             patch.object(Q, "_live_fee_metadata") as fees:
            self.assertEqual(monitor.refresh()["recorded_count"], 3)
        kalshi.assert_called_once_with({"K1"})
        polymarket.assert_called_once_with({"111", "222"}, set())
        fees.assert_not_called()
        observations = self.store.history("catalog_quotes")
        self.assertEqual({row["instrument_id"] for row in observations}, {"K1", "111", "222"})
        self.assertTrue(all(datetime.fromisoformat(row["observed_at"]) == self.now for row in observations))
        self.assertTrue(all(row["book_kind"] == "recorded_levels" for row in observations
                            if row["exchange"] == "polymarket_intl"))

    def test_collector_failure_is_sanitized_and_concurrent_refresh_coalesces(self):
        self.add("kalshi", "K1")
        monitor = self.monitor()
        monitor._refresh_lock.acquire()
        try:
            self.assertEqual(monitor.refresh()["monitored_count"], 0)
        finally:
            monitor._refresh_lock.release()
        self.assertEqual(self.calls, [])
        def broken(*_args, **_kwargs):
            raise RuntimeError("secret transport address and token")
        monitor.collector = broken
        stats = monitor.refresh()
        self.assertIn("failed", stats["error"])
        self.assertNotIn("secret", json.dumps(stats))
        self.assertEqual(self.store.datasets(), [])


if __name__ == "__main__":
    unittest.main()
