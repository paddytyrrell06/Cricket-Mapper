import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import canonical as C
import runtime as R
from research_store import ResearchStore
from test_runtime import write_runtime_snapshot


class ResearchStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.stamp = "2026-10-21T12:00:00+00:00"
        self.rows = write_runtime_snapshot(self.root, stamp=self.stamp)
        self.path = self.root / "data" / "eventlens.sqlite3"
        self.store = ResearchStore(self.path)
        manifest = json.loads((self.root / "output/data/stream_manifest.json").read_text())
        self.assets = {(item["venue"], item["market_id"], C.norm_team(item["outcome"])): item["instrument_id"]
                       for item in manifest["instruments"]}

    def test_persistence_deduplication_frame_boundary_and_independent_storage(self):
        first = self.store.record(self.rows, available_at=self.stamp, assets=self.assets)
        self.assertEqual(self.store.record(self.rows, available_at="2026-10-21T12:00:01Z"), first)
        changed = copy.deepcopy(self.rows)
        changed[0]["kalshi_bid"] = ".53"
        self.store.record(changed, available_at="2026-10-21T12:00:02Z", assets=self.assets)
        reopened = ResearchStore(self.path)
        frames, metadata = reopened.load("recorded", end_frame_id=first)
        self.assertEqual(frames[0]["rows"], self.rows)
        self.assertEqual(len(frames), 1)
        self.assertEqual(metadata["end_frame_id"], first)
        self.assertEqual(reopened.datasets()[0]["frames"], 2)
        self.assertNotIn("output", self.path.parts)
        self.assertNotEqual(frames[0]["ingested_at"], frames[0]["available_at"])
        self.assertIsNotNone(datetime.fromisoformat(frames[0]["ingested_at"]).tzinfo)

    def test_normalized_native_assets_clocks_and_full_book_are_preserved(self):
        book = {"bids": [[".52", "1000"], [".51", "40"]], "asks": [[".55", "1000"]]}
        self.rows[0]["kalshi_quote_details"] = json.dumps({"quote_order_book": book,
            "quote_exchange_time": "2026-10-21T11:59:59.900000+00:00"})
        self.store.record(self.rows, available_at=self.stamp, assets=self.assets)
        with self.store.connect() as db:
            records = [dict(row) for row in db.execute("SELECT * FROM observations ORDER BY exchange,outcome")]
        tokens = {row["outcome"]: row["instrument_id"] for row in records if row["exchange"] == "polymarket_intl"}
        self.assertEqual(tokens, {"Alpha Academy": "100", "Beta Academy": "200"})
        native = next(row for row in records if row["exchange"] == "kalshi" and row["outcome"] == "Alpha Academy")
        self.assertEqual(native["market_id"], native["instrument_id"])
        self.assertEqual(native["observed_at"], self.stamp)
        self.assertEqual(native["exchange_time"], "2026-10-21T11:59:59.900000+00:00")
        self.assertEqual(native["book_kind"], "recorded_levels")
        self.assertEqual(records[-1]["book_kind"], "recorded_bbo")
        frames, _ = self.store.load("recorded")
        self.assertEqual(json.loads(frames[0]["rows"][0]["kalshi_quote_details"])["quote_order_book"], book)

    def test_missing_prices_and_depth_stay_unknown(self):
        self.rows[0].update(kalshi_bid="", kalshi_ask=None, kalshi_bid_size="")
        self.rows[0].pop("kalshi_ask_size")
        self.store.record(self.rows, available_at=self.stamp, select=False)
        with self.store.connect() as db:
            row = db.execute("SELECT bid,ask,bid_size,ask_size,instrument_id FROM observations WHERE exchange='kalshi' AND outcome='Alpha Academy'").fetchone()
        self.assertEqual(tuple(row), ("", None, "", None, None))

    def test_default_selection_requires_accepted_comparable_hundred_dollar_depth(self):
        good = self.rows[0]
        review, thin, unknown = [copy.deepcopy(good) for _ in range(3)]
        review.update(canonical_outcome="review", mapping_status="review")
        thin["canonical_outcome"], unknown["canonical_outcome"] = "thin", "unknown"
        for venue in R.VENUES:
            thin.update({venue + "_bid_size": "1", venue + "_ask_size": "1"})
            unknown.update({venue + "_bid_size": "", venue + "_ask_size": ""})
        self.store.record([good, review, thin, unknown], available_at=self.stamp)
        self.assertEqual(self.store.load("recorded")[0][0]["rows"], [good])

    def test_explicit_unmatched_books_do_not_create_phantom_counterpart_observations(self):
        for venue in R.VENUES:
            with self.subTest(venue=venue):
                row = {"sport": "economics", "research_title": "Independent inflation outcome",
                       "canonical_fixture_id": "catalog:" + venue, "canonical_market_type": "binary",
                       "canonical_outcome": "No", "mapping_status": "unmatched", "comparison_status": "unmatched",
                       venue + "_market_id": "inflation", venue + "_quote_time": self.stamp,
                       venue + "_bid": ".30", venue + "_ask": ".33", venue + "_bid_size": "1"}
                assets = {(venue, "inflation", C.norm_team("No")): "exact-no-asset"}
                # Basic discovery has no liquidity or matching prerequisites;
                # explicit capture still must not enter the monitored pair set.
                self.assertIsNone(self.store.record([row], available_at=self.stamp, assets=assets))
                self.store.record([row], available_at=self.stamp, dataset_id="selected_" + venue,
                                  kind="recorded_order_books", assets=assets, select=False)
                records = self.store.history("selected_" + venue)
                self.assertEqual(len(records), 1)
                self.assertEqual((records[0]["exchange"], records[0]["instrument_id"], records[0]["outcome"]),
                                 (venue, "exact-no-asset", "No"))
                saved = self.store.load("selected_" + venue)[0][0]["rows"][0]
                self.assertEqual(saved["mapping_status"], "unmatched")
                self.assertEqual(saved["comparison_status"], "unmatched")

    def test_import_preserves_original_checkpoint_and_ignores_saved_replay(self):
        source = self.root / "output/data"
        meta = source / "snapshot_meta.json"
        meta.write_text(json.dumps({"quote_mode": "saved_replay", "quotes_checked_at_utc": self.stamp}))
        self.assertIsNone(self.store.import_snapshot(source))
        self.assertEqual(self.store.datasets(), [])
        meta.write_text(json.dumps({"quote_mode": "live", "quotes_checked_at_utc": self.stamp}))
        identity = self.store.import_snapshot(source)
        self.assertEqual(self.store.import_snapshot(source), identity)
        frames, metadata = self.store.load("saved_snapshots")
        self.assertEqual(metadata["kind"], "saved_quote_snapshots")
        self.assertEqual(frames[0]["available_at"], "2026-10-21T12:00:00.999999+00:00")

    def test_recorder_disk_failure_keeps_runtime_quotes_available(self):
        class FullDisk:
            def record(self, *_args, **_kwargs):
                raise OSError("disk full")
        runtime = R.MarketRuntime(self.root, collector=lambda *_a, **_kw: ([], []), recorder=FullDisk())
        runtime.refresh()
        snapshot = runtime.snapshot()
        self.assertEqual(len(snapshot["records"]), 2)
        self.assertIn("recording_error", snapshot)

    def test_history_filters_and_frequency_retain_real_rows_and_distinct_outcomes(self):
        for seconds in (0, 5, 35, 95):
            rows = copy.deepcopy(self.rows)
            stamp = (datetime.fromisoformat(self.stamp) + timedelta(seconds=seconds)).isoformat()
            for row in rows:
                for venue in R.VENUES:
                    row[venue + "_quote_time"] = stamp
            self.store.record(rows, available_at=stamp, assets=self.assets)
        history = self.store.history("recorded", exchange="polymarket_intl", market_id="101",
                                     start="2026-10-21T12:00:05Z", end="2026-10-21T12:00:35Z")
        self.assertEqual(len(history), 4)
        self.assertEqual({row["exchange"] for row in history}, {"polymarket_intl"})
        self.assertEqual({row["outcome"] for row in history}, {"Alpha Academy", "Beta Academy"})
        self.assertEqual(len(self.store.history("recorded", exchange="polymarket_intl", pair=history[0]["pair_key"])), 4)
        sampled = self.store.history("recorded", exchange="polymarket_intl", every_seconds=30)
        self.assertEqual(len(sampled), 6)
        self.assertEqual({datetime.fromisoformat(row["available_at"]).strftime("%M:%S") for row in sampled}, {"00:05", "00:35", "01:35"})
        self.assertTrue(all(row["kind"] == "recorded_quotes" for row in sampled))
        self.assertTrue(all(row["observed_at"] and row["ingested_at"] for row in sampled))
        self.assertEqual(self.store.history("recorded", market_id="absent"), [])
        for arguments in ({"start": "2026-10-21T12:00:00"}, {"every_seconds": -1}, {"limit": 20001}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.store.history("recorded", **arguments)


if __name__ == "__main__":
    unittest.main()
