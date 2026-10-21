import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pmxt_data as P


class PmxtDataTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client = Mock()
        self.client.fetch_order_book.return_value = SimpleNamespace(
            bids=[SimpleNamespace(price="0.40", size="2"), {"price": ".4", "size": "3"},
                  {"price": ".39", "size": "1"}],
            asks=[{"price": ".43", "size": "2"}, {"price": ".42", "size": "4"}],
            timestamp=1792548000000, source_metadata={"min_order_size": "5"})
        self.sdk = SimpleNamespace(Polymarket=Mock(return_value=self.client),
                                   Kalshi=Mock(return_value=self.client))
        self.importer = patch.object(P.importlib, "import_module", return_value=self.sdk).start()
        self.addCleanup(patch.stopall)

    def test_lazy_local_client_and_exact_outcome_call(self):
        provider = P.PmxtData()
        self.importer.assert_not_called()
        with patch.dict(os.environ, {"PMXT_BASE_URL": "https://api.pmxt.dev"}):
            result = provider.fetch_book("polymarket_intl", "exact-token")
            provider.fetch_book("polymarket", "second-token")
        self.sdk.Polymarket.assert_called_once_with(
            base_url="http://localhost:3847", auto_start_server=True)
        self.assertEqual(self.client.method_calls, [
            unittest.mock.call.fetch_order_book("exact-token"),
            unittest.mock.call.fetch_order_book("second-token")])
        self.assertEqual(result["bids"], [{"price": "0.40", "size": "5"},
                                          {"price": "0.39", "size": "1"}])
        self.assertEqual(result["asks"][0], {"price": "0.42", "size": "4"})
        self.assertEqual(result["minimum_order_size"], "5")
        self.assertEqual(result["source"], "pmxt_local")
        self.assertEqual(result["instrument_id"], "exact-token")

    def test_sdk_snapshot_time_is_never_claimed_as_exchange_time(self):
        result = P.PmxtData().fetch_book("kalshi", "EXACT-TICKER")
        self.assertTrue(result["provider_time"].endswith("+00:00"))
        self.assertTrue(result["observed_at"].endswith("+00:00"))
        self.assertEqual(result["exchange_time"], "")
        self.client.fetch_order_book.return_value.source_metadata = {
            "exchange_timestamp": "2026-10-21T01:02:03Z"}
        result = P.PmxtData().fetch_book("kalshi", "EXACT-TICKER")
        self.assertEqual(result["exchange_time"], "2026-10-21T01:02:03+00:00")

    def test_empty_invalid_and_zero_depth_never_create_prices(self):
        self.client.fetch_order_book.return_value = {
            "bids": [{"price": "NaN", "size": "1"}, {"price": ".5", "size": "0"},
                     {"price": True, "size": "4"}, {"price": ".5", "size": "Infinity"}],
            "asks": [{"price": "1.1", "size": "2"}], "source_metadata": {}}
        result = P.PmxtData().fetch_book("kalshi", "TICKER")
        self.assertEqual((result["bids"], result["asks"]), ([], []))
        self.assertEqual(result["provider_time"], "")
        self.assertNotIn("minimum_order_size", result)

    def test_missing_sdk_is_optional_and_actionable(self):
        self.importer.side_effect = ModuleNotFoundError("pmxt")
        with self.assertRaisesRegex(P.PmxtUnavailable, "requirements-pmxt.txt"):
            P.PmxtData().fetch_book("kalshi", "TICKER")

    def test_hosted_environment_key_is_rejected_without_import_or_request(self):
        with patch.dict(os.environ, {"PMXT_API_KEY": "test-only"}):
            with self.assertRaisesRegex(P.PmxtUnavailable, "Unset PMXT_API_KEY"):
                P.PmxtData().fetch_book("kalshi", "TICKER")
        self.importer.assert_not_called()

    def test_kalshi_sdk_auth_failure_never_requests_credentials_or_falls_back(self):
        self.client.fetch_order_book.side_effect = RuntimeError("Trading operations require authentication")
        with self.assertRaisesRegex(P.PmxtUnavailable, "native public REST"):
            P.PmxtData().fetch_book("kalshi", "TICKER")
        self.sdk.Kalshi.assert_called_once_with(
            base_url="http://localhost:3847", auto_start_server=True)

    def test_candles_are_bounded_and_explicitly_non_executable(self):
        self.client.fetch_ohlcv.return_value = [{"timestamp": 1792548000000, "open": ".4",
                                               "high": ".5", "low": ".3", "close": ".45"}]
        result = P.PmxtData().fetch_candles("polymarket_intl", "exact-token", limit=3)
        self.assertEqual(result["data_type"], "price_candles")
        self.assertIs(result["executable"], False)
        self.assertEqual(result["candles"][0]["close"], "0.45")
        self.assertIsNone(result["candles"][0]["volume"])
        call = self.client.fetch_ohlcv.call_args
        self.assertEqual(call.args, ("exact-token",))
        self.assertEqual(call.kwargs["limit"], 3)
        self.assertEqual((call.kwargs["end"] - call.kwargs["start"]).total_seconds(), 3 * 3600)
        with self.assertRaises(ValueError):
            P.PmxtData().fetch_candles("polymarket_intl", "exact-token", limit=101)
        with self.assertRaises(ValueError):
            P.PmxtData().fetch_candles("kalshi", "TICKER")
        self.assertEqual(self.client.fetch_ohlcv.call_count, 1)

    def test_only_loopback_urls_supported_venues_and_nonempty_ids(self):
        for url in ("https://api.pmxt.dev", "http://example.com", "http://localhost@evil.test",
                    "http://localhost/api", "http://localhost?url=remote"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                P.PmxtData(url)
        for venue, identity in (("polymarket_us", "id"), ("kalshi", ""), ("kalshi", "x" * 257)):
            with self.subTest(venue=venue, identity=identity), self.assertRaises(ValueError):
                P.PmxtData().fetch_book(venue, identity)
        self.importer.assert_not_called()

    def test_discovery_is_bounded_and_only_review_suggestions(self):
        self.client.fetch_markets.return_value = [{"market_id": str(i), "info": {"rules": "raw"}}
                                                 for i in range(5)]
        provider = P.PmxtData()
        result = provider.fetch_markets("kalshi", "Chicago", limit=2)
        self.client.fetch_markets.assert_called_once_with(query="Chicago", limit=2)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["status"], "review")
        self.assertEqual(result[0]["raw"]["info"], {"rules": "raw"})
        for limit in (0, 4, True):
            with self.assertRaises(ValueError):
                provider.fetch_markets("kalshi", "Chicago", limit=limit)
        with self.assertRaises(ValueError):
            provider.fetch_markets("kalshi", "")
        self.assertEqual(self.client.fetch_markets.call_count, 1)


if __name__ == "__main__":
    unittest.main()
