import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from betfair_data import BetfairData, Config
from betfair_service import BetfairService
from research import ResearchService
from research_store import ResearchStore
from test_betfair_mapping import betfair_market, runtime_candidates


class BetfairServiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = ResearchStore(self.root / "eventlens.sqlite3")
        self.now = datetime(2026, 10, 20, 20, tzinfo=timezone.utc)
        self.rows, self.assets = runtime_candidates()
        self.runtime = SimpleNamespace(research_rows=lambda: (self.rows, self.assets))
        self.market = betfair_market()
        self.market.update(book_available=True, market_status="OPEN", inplay=False, bet_delay=0,
                           data_mode="live", is_market_data_delayed=False, currency="GBP",
                           observed_at=self.now.isoformat(), source_publish_time=None,
                           received_monotonic_ns=123456, market_version=12)
        for runner in self.market["runners"]:
            runner.update(back=[{"odds": "2.2", "size": "100"}], lay=[{"odds": "2.24", "size": "80"}],
                          observed_at=self.now.isoformat(), status="ACTIVE")
        self.provider = Mock()
        self.provider.status.return_value = {"state": "ready", "mode": "unknown"}
        self.provider.refresh.side_effect = lambda: {"state": "ok", "mode": "live", "markets": [copy.deepcopy(self.market)]}
        self.service = BetfairService(self.root, self.store, self.runtime, self.provider, clock=lambda: self.now)

    def test_native_books_and_mapping_versions_persist_without_binary_observations(self):
        status = self.service.refresh()
        self.assertEqual(status["market_count"], 1)
        self.assertEqual(status["mapping_counts"], {"fixture_linked": 2})
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["markets"][0]["runners"][0]["back"][0], {"odds": "2.2", "size": "100"})
        self.assertTrue(all(not mapping["arbitrage_eligible"] for mapping in snapshot["mappings"]))
        history = self.service.history(self.market["market_id"])
        self.assertEqual(history["dataset"], "exchange")
        self.assertEqual(history["books"][0]["market_version"], 12)
        self.assertIsNone(history["books"][0]["source_publish_time"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM betfair_mapping_versions").fetchone()[0], 2)
        restarted = BetfairService(self.root, self.store, provider=self.provider)
        self.assertEqual(restarted.snapshot()["markets"][0]["observed_at"], self.now.isoformat())
        self.assertEqual(restarted.status()["state"], "ready")

    def test_unchanged_books_update_real_receipt_time_but_only_changes_enter_history(self):
        self.service.refresh()
        self.market["observed_at"] = (self.now + timedelta(seconds=15)).isoformat()
        for runner in self.market["runners"]:
            runner["observed_at"] = self.market["observed_at"]
        self.service.refresh()
        self.assertEqual(len(self.service.history(self.market["market_id"])["books"]), 1)
        self.assertEqual(self.service.snapshot()["markets"][0]["observed_at"], self.market["observed_at"])
        self.market["runners"][0]["back"][0]["size"] = "99"
        self.service.refresh()
        self.assertEqual(len(self.service.history(self.market["market_id"])["books"]), 2)

    def test_failed_refresh_retains_original_clocks_and_cannot_create_new_observations(self):
        self.service.refresh()
        self.provider.refresh.return_value = {"state": "error", "message": "Betfair request failed", "markets": []}
        self.provider.refresh.side_effect = None
        self.assertEqual(self.service.refresh()["state"], "error")
        self.assertEqual(self.service.snapshot()["markets"][0]["observed_at"], self.now.isoformat())
        self.assertEqual(len(self.service.history(self.market["market_id"])["books"]), 1)

    def test_exact_asset_attachment_preserves_existing_fees_alerts_and_profit(self):
        self.service.refresh()
        original = {"id": "pm-boston", "venues": ["kalshi", "polymarket_intl"], "legs": [
            {"exchange": "polymarket_intl", "instrument_id": "100"}], "quoted": True,
            "match_status": "matched", "alert": True, "net_cents": "1.5", "pnl": "1.50", "fees_cents": "2"}
        rows = self.service.decorate([copy.deepcopy(original)])
        self.assertEqual(rows[0]["betfair"]["selection_id"], "11")
        self.assertEqual(rows[0]["betfair"]["mapping"]["pm_asset_id"], "100")
        self.assertEqual(rows[0]["betfair"]["currency"], "GBP")
        for field in ("fees_cents", "net_cents", "pnl", "alert", "match_status"):
            self.assertEqual(rows[0][field], original[field])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["venues"], ["betfair"])
        self.assertFalse(rows[1]["alert"])
        self.assertIsNone(rows[1]["pnl"])

    def test_disabled_source_makes_no_requests_and_does_not_auto_display_saved_synthetic_data(self):
        fixture = Path(__file__).parent / "fixtures/betfair/nba_synthetic.json"
        transport = Mock(side_effect=AssertionError("Synthetic replay requested network data"))
        replay = BetfairService(self.root, self.store,
                               provider=BetfairData(Config(enabled=True, replay_file=str(fixture)), transport))
        self.assertEqual(replay.refresh()["mode"], "synthetic")
        native = replay.snapshot()["markets"][0]
        self.assertEqual(native["data_mode"], "synthetic")
        self.assertEqual(native["observed_at"], "2026-10-22T00:00:00.000+00:00")
        self.assertTrue(replay.history(native["market_id"])["books"])
        disabled = BetfairService(self.root, self.store, provider=BetfairData(Config(), transport))
        self.assertEqual(disabled.refresh()["state"], "not_configured")
        self.assertEqual(disabled.snapshot()["markets"], [])
        self.assertEqual(disabled.snapshot()["mappings"], [])
        self.assertEqual(disabled.history(native["market_id"])["books"], [])
        transport.assert_not_called()

    def test_local_api_reads_and_history_validation_never_refresh_the_provider(self):
        self.service.refresh()
        research = ResearchService(self.root, self.store, self.runtime)
        research.betfair = self.service
        before = self.provider.refresh.call_count
        self.assertEqual(research.get("/api/research/betfair", {})["status"]["runner_count"], 2)
        history = research.get("/api/research/betfair/history", {"market_id": [self.market["market_id"]], "limit": ["1"]})
        self.assertEqual(len(history["books"]), 1)
        self.assertEqual(self.provider.refresh.call_count, before)
        with self.assertRaises(ValueError):
            research.get("/api/research/betfair/history", {"limit": ["1001"]})


if __name__ == "__main__":
    unittest.main()
