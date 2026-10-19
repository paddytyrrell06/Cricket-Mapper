import csv
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import run as R


class ReviewExportTests(unittest.TestCase):
    def fixture(self, venue, fixture_id, teams=("Australia", "New Zealand")):
        return R.C.Fixture(
            R.C.FixtureKey("2026-10-22", frozenset(R.C.norm_team(t) for t in teams),
                           "W", "T20"),
            venue, fixture_id, competition="International", start_time="08:15",
            team_a=teams[0], team_b=teams[1],
        )

    def market(self, fixture, event_id, title="Australia wins"):
        return R.C.Market(
            fixture.venue, fixture.venue_fixture_id, fixture.key,
            R.C.MATCH_WINNER, fixture.team_a, event_id=event_id,
            market_id=event_id + ":market", title=title,
            url=f"https://example.com/event/{event_id}",
        )

    def test_duplicate_key_fixture_reviews_keep_distinct_source_identities(self):
        left_one = self.fixture("kalshi", "kalshi:first")
        left_two = self.fixture("kalshi", "kalshi:second")
        right = self.fixture("polymarket_intl", "intl:fixture")
        _, reviews, _ = R.M.match_fixtures([left_one, left_two, right])
        markets = [self.market(left_one, "event-one"),
                   self.market(left_two, "event-two"),
                   self.market(right, "event-intl")]

        rows = R.review_mapping_rows([], reviews, markets)

        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(row) == len(R.REVIEW_COLS) for row in rows))
        records = [dict(zip(R.REVIEW_COLS, row)) for row in rows]
        self.assertEqual({row["venue_1_fixture_id"] for row in records},
                         {"kalshi:first", "kalshi:second"})
        self.assertEqual({row["venue_1_event_id"] for row in records},
                         {"event-one", "event-two"})
        self.assertEqual({row["venue_1_url"] for row in records},
                         {"https://example.com/event/event-one",
                          "https://example.com/event/event-two"})
        self.assertTrue(all(row["venue_2_fixture_id"] == "intl:fixture" for row in records))
        self.assertTrue(all(row["venue_2_event_id"] == "event-intl" for row in records))
        self.assertTrue(all(row["record_kind"] == "fixture" for row in records))
        self.assertTrue(all(row["venue_1_market_id"] == "" for row in records))

    def test_fuzzy_fixture_review_preserves_both_published_team_names_and_titles(self):
        left = self.fixture("kalshi", "shared-id")
        right = self.fixture("polymarket_intl", "shared-id",
                             ("Australia Women", "New Zealand Women"))
        _, reviews, _ = R.M.match_fixtures([left, right])
        markets = [self.market(right, "intl-event", "Will Australia Women win?"),
                   self.market(left, "kalshi-event", "Australia wins")]

        rows = R.review_mapping_rows([], reviews, markets)

        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), len(R.REVIEW_COLS))
        row = dict(zip(R.REVIEW_COLS, rows[0]))
        self.assertEqual(row["venue_1_event_id"], "kalshi-event")
        self.assertEqual(row["venue_2_event_id"], "intl-event")
        self.assertEqual(row["venue_1_title"], "Australia v New Zealand | Australia wins")
        self.assertEqual(row["venue_2_title"],
                         "Australia Women v New Zealand Women | Will Australia Women win?")
        self.assertEqual(row["venue_2_url"], "https://example.com/event/intl-event")

    def test_market_review_retains_existing_mapping_and_adds_fixture_ids(self):
        left = self.fixture("kalshi", "k")
        right = self.fixture("polymarket_intl", "p")
        accepted, _, _ = R.M.match_fixtures([left, right])
        first, second = self.market(left, "k-event"), self.market(right, "p-event")
        review = R.M.MarketMatch(accepted[0], R.C.MATCH_WINNER, "Australia",
                                left.venue, first, right.venue, second,
                                status="review", reason="test review")

        rows = R.review_mapping_rows([review], [], [first, second])

        self.assertEqual(rows[0][:len(R.MAPPING_COLS)], R.mapping_row(review))
        self.assertEqual(rows[0][len(R.MAPPING_COLS):], ["market", "k", "p"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review_queue.csv"
            R.write_csv(path, R.REVIEW_COLS, rows)
            with path.open(newline="") as handle:
                saved = list(csv.DictReader(handle))
            self.assertEqual(saved[0]["venue_1_fixture_id"], "k")
            self.assertNotIn(None, saved[0])

    def test_fixture_without_markets_keeps_its_own_identity(self):
        left = self.fixture("kalshi", "k")
        left.extra["event_ticker"] = "KXWT20MATCH-EVENT"
        right = self.fixture("polymarket_intl", "p",
                             ("Australia Women", "New Zealand Women"))
        _, reviews, _ = R.M.match_fixtures([left, right])

        rows = R.review_mapping_rows([], reviews, [])

        self.assertEqual(len(rows[0]), len(R.REVIEW_COLS))
        row = dict(zip(R.REVIEW_COLS, rows[0]))
        self.assertEqual(row["venue_1_fixture_id"], "k")
        self.assertEqual(row["venue_1_event_id"], "KXWT20MATCH-EVENT")
        self.assertEqual(row["venue_2_fixture_id"], "p")
        self.assertEqual(row["venue_2_title"], "Australia Women v New Zealand Women")


class ReplayRunTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.build = self.root / "build"
        self.data = self.build / "data"
        self.output = self.root / "output"
        self.previous = self.root / "previous"
        self.history = self.root / "history"
        self.source = self.root / "source"
        for name, value in {
            "BUILD_DIR": self.build, "DATA_DIR": self.data,
            "OUTPUT_DIR": self.output, "PREVIOUS_DIR": self.previous,
            "HISTORY_DIR": self.history, "SNAPSHOT_DIR": self.source,
            "NO_OPEN": True,
        }.items():
            self.stack.enter_context(patch.object(R, name, value))
        self.stack.enter_context(patch.object(R, "say"))
        self.browser = self.stack.enter_context(patch.object(R.webbrowser, "open"))
        self.kalshi_parser = self.stack.enter_context(
            patch.object(R.C, "parse_kalshi", return_value=([], [], [])))
        self.stack.enter_context(
            patch.object(R.C, "parse_polymarket", return_value=([], [], [])))
        self.stack.enter_context(
            patch.object(R.C, "parse_polymarket_us", return_value=([], [], [])))

    def write_snapshot(self, directory=None):
        directory = directory or self.source
        directory.mkdir(parents=True, exist_ok=True)
        metadata = {}
        for venue in R.VENUES:
            (directory / f"raw_{venue}.json").write_text(
                json.dumps([{"venue": venue}]), encoding="utf-8")
            metadata[venue] = {
                "complete": True, "method": "saved_collection", "reason": "",
            }
        (directory / "collection_meta.json").write_text(
            json.dumps(metadata), encoding="utf-8")
        return metadata

    def output_metadata(self):
        return json.loads((self.output / "data" / "collection_meta.json").read_text())

    def test_replay_inside_build_is_loaded_before_build_is_replaced(self):
        self.write_snapshot(self.data)
        with patch.object(R, "SNAPSHOT_DIR", self.data):
            self.assertEqual(R.main(), 0)
        self.kalshi_parser.assert_called_once_with([{"venue": "kalshi"}])
        for venue in R.VENUES:
            saved = self.output / "data" / f"raw_{venue}.json"
            self.assertEqual(json.loads(saved.read_text()), [{"venue": venue}])
        self.assertFalse(self.build.exists())
        self.browser.assert_not_called()

    def test_missing_or_invalid_raw_cannot_inherit_complete_metadata(self):
        for label, content in [
            ("missing", None), ("bad-json", "{"),
            ("object", "{}"), ("bad-record", '["invalid"]'),
        ]:
            with self.subTest(label=label):
                directory = self.root / label
                self.write_snapshot(directory)
                path = directory / "raw_kalshi.json"
                if content is None:
                    path.unlink()
                else:
                    path.write_text(content, encoding="utf-8")
                raw, status, warnings, coverage = R.load_snapshot(directory)
                self.assertFalse(coverage["kalshi"]["complete"])
                self.assertEqual(coverage["kalshi"]["records"], 0)
                self.assertEqual(raw["kalshi"], [])
                self.assertIn("snapshot", status["kalshi"])
                self.assertTrue(any("kalshi" in warning for warning in warnings))
                self.assertTrue(coverage["polymarket_us"]["complete"])

    def test_old_parse_findings_are_cleared_and_collection_reasons_retained(self):
        metadata = self.write_snapshot()
        metadata["kalshi"].update(
            complete=False,
            reason="pagination stopped early; COVERAGE_INCOMPLETE kalshi: old parser issue",
            parse_complete=False, mapping_coverage_complete=False,
        )
        metadata["polymarket_intl"].update(
            reason="COVERAGE_INCOMPLETE polymarket_intl: old parser issue",
            parse_complete=False, mapping_coverage_complete=False,
        )
        metadata["polymarket_us"].update(
            collection_reason="saved collection note",
            reason="saved collection note; COVERAGE_INCOMPLETE polymarket_us: old parser issue",
        )
        (self.source / "collection_meta.json").write_text(json.dumps(metadata))
        self.assertEqual(R.main(), 0)
        coverage = self.output_metadata()
        self.assertEqual(coverage["kalshi"]["reason"], "pagination stopped early")
        self.assertFalse(coverage["kalshi"]["mapping_coverage_complete"])
        self.assertEqual(coverage["polymarket_intl"]["reason"], "")
        self.assertTrue(coverage["polymarket_intl"]["mapping_coverage_complete"])
        self.assertEqual(coverage["polymarket_us"]["reason"], "saved collection note")
        self.assertTrue(all(item["parse_complete"] for item in coverage.values()))

    def test_parse_exception_is_reported_in_coverage_and_run_report(self):
        self.write_snapshot()
        self.kalshi_parser.side_effect = ValueError("broken source record")
        self.assertEqual(R.main(), 0)
        metadata = self.output_metadata()["kalshi"]
        self.assertFalse(metadata["parse_complete"])
        self.assertFalse(metadata["mapping_coverage_complete"])
        self.assertIn("parse failed ValueError('broken source record')", metadata["reason"])
        report = (self.output / "data" / "run_report.txt").read_text()
        self.assertIn("broken source record", report)
        self.assertIn("parse_complete=False", report)

    def test_original_collection_time_survives_repeated_replays(self):
        self.write_snapshot()
        expected = datetime(2026, 10, 18, 5, 0, tzinfo=timezone.utc)
        (self.source / "snapshot_meta.json").write_text(json.dumps({
            "collected_at_utc": expected.isoformat(),
        }))
        for source in (self.source, self.output / "data"):
            with self.subTest(source=source), patch.object(R, "SNAPSHOT_DIR", source):
                self.assertEqual(R.main(), 0)
                self.assertEqual(R.snapshot_collection_time(self.output / "data"), expected)
                metadata = json.loads((self.output / "data" / "snapshot_meta.json").read_text())
                self.assertEqual(metadata["collected_at_utc"], expected.isoformat())
                report = (self.output / "data" / "run_report.txt").read_text()
                self.assertIn(f"data_fetched_utc {expected.isoformat()}", report)

    def test_legacy_run_report_supplies_original_collection_time(self):
        self.write_snapshot()
        (self.source / "run_report.txt").write_text(
            "data_fetched_utc 2026-10-18T18:00:00+13:00\n", encoding="utf-8")
        self.assertEqual(R.main(), 0)
        expected = datetime(2026, 10, 18, 5, 0, tzinfo=timezone.utc)
        self.assertEqual(R.snapshot_collection_time(self.output / "data"), expected)

    def test_unknown_source_time_remains_unknown_across_replays(self):
        self.write_snapshot()
        for source in (self.source, self.output / "data"):
            with self.subTest(source=source), patch.object(R, "SNAPSHOT_DIR", source):
                self.assertEqual(R.main(), 0)
                self.assertIsNone(R.snapshot_collection_time(self.output / "data"))
                metadata = json.loads((self.output / "data" / "snapshot_meta.json").read_text())
                self.assertIsNone(metadata["collected_at_utc"])
                self.assertIn("data_fetched_utc unknown",
                              (self.output / "data" / "run_report.txt").read_text())
                self.assertIn("Source collection time unavailable",
                              (self.output / R.REPORT_NAME).read_text())

    def test_critical_failure_preserves_existing_published_report(self):
        metadata = self.write_snapshot()
        metadata["kalshi"]["complete"] = False
        metadata["polymarket_intl"]["complete"] = False
        (self.source / "collection_meta.json").write_text(json.dumps(metadata))
        self.output.mkdir()
        published = self.output / R.REPORT_NAME
        published.write_text("previous audited report", encoding="utf-8")
        self.assertEqual(R.main(), 4)
        self.assertEqual(published.read_text(), "previous audited report")
        self.assertIn("OVERALL FAIL", (self.data / "audit_report.txt").read_text())
        self.assertFalse(self.history.exists())
        self.browser.assert_not_called()

    def test_prepare_restores_previous_report_after_interrupted_publish(self):
        self.previous.mkdir()
        (self.previous / R.REPORT_NAME).write_text("recovered report", encoding="utf-8")
        self.data.mkdir(parents=True)
        (self.data / "stale.txt").write_text("old build", encoding="utf-8")
        R.prepare_build()
        self.assertEqual((self.output / R.REPORT_NAME).read_text(), "recovered report")
        self.assertFalse(self.previous.exists())
        self.assertTrue(self.data.is_dir())
        self.assertFalse((self.data / "stale.txt").exists())


if __name__ == "__main__":
    unittest.main()
