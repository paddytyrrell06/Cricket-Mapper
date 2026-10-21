import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import canonical as C
from research import ResearchService
from research_store import ResearchStore, pair_key
from server import TerminalServer
from test_runtime import write_runtime_snapshot


class ResearchAPITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.rows = write_runtime_snapshot(self.root, stamp="2026-10-21T12:00:00+00:00")
        manifest = json.loads((self.root / "output/data/stream_manifest.json").read_text())
        self.assets = {(item["venue"], item["market_id"], C.norm_team(item["outcome"])): item["instrument_id"] for item in manifest["instruments"]}
        self.store = ResearchStore(self.root / "data/eventlens.sqlite3")
        runtime = SimpleNamespace(research_rows=lambda: (copy.deepcopy(self.rows), self.assets))
        self.service = ResearchService(self.root, self.store, runtime)
        self.catalog = self.service.catalog = Mock()
        self.catalog.markets.return_value = []
        self.catalog.lookup.return_value = None
        self.catalog.summary.return_value = {"markets": 0, "by_category": {}, "by_exchange": {}}
        self.provider = self.service._provider = Mock()
        self.provider.fetch_book.return_value = {"venue": "polymarket_intl", "instrument_id": "100", "source": "pmxt_local",
            "observed_at": "2026-10-21T12:00:01+00:00", "exchange_time": "", "provider_time": "2026-10-21T12:00:00+00:00",
            "bids": [{"price": format(.48 - i * .01, ".2f"), "size": "1000" if i == 0 else "50"} for i in range(12)], "asks": [{"price": ".51", "size": "900"}]}
        self.provider.fetch_candles.return_value = {"venue": "polymarket_intl", "instrument_id": "100", "source": "pmxt_local",
            "observed_at": "2026-10-21T12:00:01+00:00", "resolution": "1h", "data_type": "price_candles", "executable": False,
            "candles": [{"time": "2026-10-21T11:00:00+00:00", "open": ".48", "high": ".51", "low": ".47", "close": ".50", "volume": None}]}
        self.body = {"pair": pair_key(self.rows[0]), "venue": "polymarket_intl"}
        self.server = None

    def unmatched(self, venue="polymarket_intl"):
        instrument = "987654321" if venue == "polymarket_intl" else "KXINDEPENDENT-26-YES"
        row = {"sport": "politics", "domain": "politics", "research_title": "Independent election market",
               "canonical_fixture_id": "catalog:" + venue + ":election", "canonical_market_type": "binary",
               "canonical_outcome": "Yes", "mapping_status": "unmatched", "comparison_status": "unmatched",
               venue + "_market_id": "native-election", venue + "_event_id": "native-election-event"}
        assets = {(venue, "native-election", C.norm_team("Yes")): instrument}
        self.service.runtime = None
        self.catalog.lookup.side_effect = lambda identity: (copy.deepcopy(row), assets) if identity == pair_key(row) else None
        return row, instrument, {"pair": pair_key(row), "venue": venue}

    def request(self, method="GET", path="/api/research/datasets", body=None, headers=None):
        if self.server is None:
            self.server = TerminalServer(0, root=self.root, refresh=Mock(), research=self.service)
            worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            worker.start()
            def cleanup():
                self.server.shutdown()
                self.server.server_close()
                worker.join(2)
            self.addCleanup(cleanup)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_selected_book_persists_exact_token_levels_and_preserves_review(self):
        self.rows[0]["mapping_status"] = "review"
        self.service.post("/api/research/book", self.body)
        self.provider.fetch_book.assert_called_once_with("polymarket_intl", "100")
        frames, info = self.store.load("selected_books")
        row = frames[0]["rows"][0]
        details = json.loads(row["polymarket_intl_quote_details"])
        self.assertEqual(row["mapping_status"], "review")
        self.assertEqual(info["kind"], "recorded_order_books")
        self.assertEqual(details["quote_exchange_time"], "")
        self.assertEqual(details["quote_provider_time"], "2026-10-21T12:00:00+00:00")
        self.assertEqual(len(details["quote_order_book"]["bids"]), 12)
        self.assertEqual(row["polymarket_intl_quote_source"], "pmxt_local book 100")

    def test_candles_are_typed_separate_from_quote_frames(self):
        result = self.service.post("/api/research/candles", self.body)
        self.assertEqual(result["data_type"], "price_candles")
        self.assertFalse(result["executable"])
        self.assertEqual(self.store.datasets(), [])
        self.assertEqual(self.service.get("/api/research/candles", {"instrument": ["100"]})["candles"][0]["volume"], None)
        self.provider.fetch_candles.assert_called_once_with("polymarket_intl", "100", limit=24)

    def test_independent_discovery_and_coverage_are_local_and_create_no_quote_frames(self):
        unmatched, instrument, _body = self.unmatched()
        self.service.runtime = SimpleNamespace(research_rows=lambda: (copy.deepcopy(self.rows), self.assets))
        entry = {"id": pair_key(unmatched), "name": "Independent election market / Yes", "category": "politics",
                 "comparison_status": "unmatched", "instruments": {"polymarket_intl": instrument}}
        summary = {"markets": 1, "by_category": {"politics": 1}, "by_exchange": {"polymarket_intl": 1}}
        self.catalog.markets.return_value = [entry]
        self.catalog.summary.return_value = summary
        markets = self.service.get("/api/research/universe", {})["markets"]
        self.assertEqual({market["id"] for market in markets}, {pair_key(row) for row in self.rows} | {entry["id"]})
        self.assertEqual(next(market for market in markets if market["id"] == entry["id"]), entry)
        self.assertEqual(self.service.get("/api/research/coverage", {}), summary)
        metadata = self.service.get("/api/research/catalog-market", {"pair": [entry["id"]]})
        self.assertEqual(metadata["row"], unmatched)
        self.assertEqual(metadata["source"], "catalog_metadata")
        self.assertFalse(metadata["has_quote_history"])
        with self.assertRaises(ValueError):
            self.service.get("/api/research/catalog-market", {"pair": ["unknown"]})
        self.assertEqual(self.store.datasets(), [])
        self.catalog.refresh.assert_not_called()
        self.provider.fetch_book.assert_not_called()
        self.provider.fetch_candles.assert_not_called()

    def test_discover_uses_guarded_post_and_does_not_record_quotes(self):
        self.catalog.refresh.return_value = {"markets": 7, "by_category": {"politics": 7}}
        self.request()
        headers = {"Origin": "http://127.0.0.1:" + str(self.server.server_port),
                   "Content-Type": "application/json", "X-Terminal-Request": "refresh"}
        status, body = self.request("POST", "/api/research/discover", {}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), self.catalog.refresh.return_value)
        self.catalog.refresh.assert_called_once_with()
        self.assertEqual(self.store.datasets(), [])
        self.provider.fetch_book.assert_not_called()
        self.provider.fetch_candles.assert_not_called()

    def test_unmatched_book_uses_exact_token_without_runtime_or_counterpart(self):
        row, instrument, body = self.unmatched()
        self.provider.fetch_book.return_value["instrument_id"] = instrument
        self.service.post("/api/research/book", body)
        self.provider.fetch_book.assert_called_once_with("polymarket_intl", instrument)
        frames, _metadata = self.store.load("selected_books")
        saved = frames[0]["rows"][0]
        self.assertEqual(saved["mapping_status"], "unmatched")
        self.assertEqual(saved["comparison_status"], "unmatched")
        self.assertNotIn("net_gap", saved)
        self.assertNotIn("kalshi_market_id", saved)
        observations = self.store.history("selected_books", pair=pair_key(row))
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["exchange"], "polymarket_intl")
        self.assertEqual(observations[0]["instrument_id"], instrument)
        self.assertEqual(observations[0]["source"], "pmxt_local book " + instrument)
        self.assertEqual(self.store.history("selected_books", exchange="kalshi"), [])

    def test_unmatched_candles_use_exact_token_without_creating_quote_frames(self):
        _row, instrument, body = self.unmatched()
        self.provider.fetch_candles.return_value["instrument_id"] = instrument
        result = self.service.post("/api/research/candles", body)
        self.provider.fetch_candles.assert_called_once_with("polymarket_intl", instrument, limit=24)
        self.assertFalse(result["executable"])
        self.assertEqual(self.store.datasets(), [])
        self.assertEqual(len(self.store.candle_history(instrument)), 1)

    def test_unmatched_kalshi_book_retains_native_source_and_no_polymarket_observation(self):
        row, instrument, body = self.unmatched("kalshi")
        book = {**self.provider.fetch_book.return_value, "venue": "kalshi", "instrument_id": instrument,
                "source": "native_kalshi_public"}
        with patch("research.Q.fetch_public_kalshi_book", return_value=book) as native:
            self.service.post("/api/research/book", body)
        native.assert_called_once_with(instrument)
        self.provider.fetch_book.assert_not_called()
        records = self.store.history("selected_books", pair=pair_key(row))
        self.assertEqual([(record["exchange"], record["instrument_id"], record["source"]) for record in records],
                         [("kalshi", instrument, "native_kalshi_public book " + instrument)])
        self.assertEqual(self.store.history("selected_books", exchange="polymarket_intl"), [])

    def test_invalid_venues_and_universe_ids_never_reach_provider(self):
        for body in ({**self.body, "venue": "https://attacker.invalid/book"}, {**self.body, "pair": "file:///tmp/private"}):
            with self.assertRaises(ValueError):
                self.service.post("/api/research/book", body)
        self.provider.fetch_book.assert_not_called()

    def test_gets_and_exports_are_local_and_backtests_are_absent(self):
        self.store.record(self.rows, available_at="2026-10-21T12:00:00Z", assets=self.assets)
        (self.root / "research.html").write_text("<html>Research data</html>")
        with patch("server.ROOT", self.root):
            self.assertEqual(self.request(path="/research"), (200, b"<html>Research data</html>"))
        with patch("research.Q.fetch_public_kalshi_book") as native:
            for path in ("universe", "coverage", "datasets", "pairs?dataset=recorded", "series?dataset=recorded", "history?dataset=recorded", "export?dataset=recorded", "candles?instrument=100"):
                self.assertEqual(self.request(path="/api/research/" + path)[0], 200)
            markets = json.loads(self.request(path="/api/research/universe")[1])["markets"]
            self.assertEqual({market["id"] for market in markets}, {pair_key(row) for row in self.rows})
            self.assertEqual(self.request(path="/api/research/book?url=https://attacker.invalid")[0], 404)
            self.assertEqual(self.request(path="/api/research/backtest")[0], 404)
            self.assertEqual(self.request("POST", "/api/research/backtest", {})[0], 404)
            native.assert_not_called()
        self.provider.fetch_book.assert_not_called()
        self.provider.fetch_candles.assert_not_called()

    def test_all_research_posts_require_local_origin_and_custom_header(self):
        self.request()
        valid = {"Origin": "http://127.0.0.1:" + str(self.server.server_port), "Content-Type": "application/json", "X-Terminal-Request": "refresh"}
        for path in ("import", "book", "candles", "discover"):
            for headers in ({}, {**valid, "Origin": "https://attacker.invalid"}, {**valid, "X-Terminal-Request": "other"}):
                self.assertEqual(self.request("POST", "/api/research/" + path, self.body, headers)[0], 403)
        self.provider.fetch_book.assert_not_called()
        self.catalog.refresh.assert_not_called()
        self.assertEqual(self.request("POST", "/api/research/book", self.body, valid)[0], 200)
        self.provider.fetch_book.side_effect = RuntimeError("secret https://private.invalid/token")
        status, body = self.request("POST", "/api/research/book", self.body, valid)
        self.assertEqual(status, 503)
        self.assertNotIn(b"secret", body)
        self.assertNotIn(b"private.invalid", body)


if __name__ == "__main__":
    unittest.main()
