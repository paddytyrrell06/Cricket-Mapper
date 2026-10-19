import csv
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import run as R
from test_settlement import known_profile


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
            "ENABLED_SPORTS": ("cricket",),
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

    def install_matched_markets(self):
        key = R.C.FixtureKey("2026-10-22", frozenset({"australia", "new zealand"}),
                             "W", "T20")
        fixtures = [R.C.Fixture(key, venue, fixture_id, competition="International",
                                start_time="08:15", team_a="Australia", team_b="New Zealand")
                    for venue, fixture_id in (("kalshi", "k"), ("polymarket_intl", "p"))]
        markets = []
        for source in fixtures:
            for outcome in ("Australia", "New Zealand"):
                url = ("https://kalshi.com/markets/series/title/k" if source.venue == "kalshi"
                       else "https://polymarket.com/event/p")
                markets.append(R.C.Market(
                    source.venue, source.venue_fixture_id, source.key, R.C.MATCH_WINNER,
                    outcome, market_id=f"{source.venue}:{outcome}",
                    event_id=source.venue_fixture_id, title=f"{outcome} wins", url=url,
                    extra={"settlement": known_profile()},
                ))
        unmatched = R.C.Market("kalshi", "k", key, R.C.TOSS_WINNER, "Australia",
                               market_id="unmatched-toss")
        self.kalshi_parser.return_value = ([fixtures[0]], markets[:2] + [unmatched], [])
        self.stack.enter_context(patch.object(
            R.C, "parse_polymarket", return_value=([fixtures[1]], markets[2:], [])))
        return markets, unmatched

    def saved_quotes(self, markets, *, bid="0.4000", ask="0.6000"):
        return [{"venue": market.venue, "market_id": market.market_id,
                 "outcome": market.outcome, "bid": bid, "ask": ask,
                 "quote_time": "2026-10-18T05:01:00Z", "quote_status": "available",
                 "quote_source": "saved public order book"}
                for market in markets]

    def output_rows(self):
        with (self.output / "data" / "market_map.csv").open(newline="") as handle:
            return list(csv.DictReader(handle))

    def test_quote_snapshot_inside_build_replays_offline_before_cleanup(self):
        self.write_snapshot(self.data)
        markets, unmatched = self.install_matched_markets()
        entries = self.saved_quotes(markets + [unmatched])
        (self.data / "quotes.json").write_text(json.dumps(entries), encoding="utf-8")
        with patch.object(R, "SNAPSHOT_DIR", self.data), patch.object(
                R.Q, "_request_json", side_effect=AssertionError("replay made an HTTP request")) as request:
            self.assertEqual(R.main(), 0)
        request.assert_not_called()
        saved = json.loads((self.output / "data" / "quotes.json").read_text())
        self.assertEqual(len(saved), 4)
        self.assertNotIn(unmatched.market_id, {entry["market_id"] for entry in saved})
        self.assertTrue(all(entry["bid"] == "0.4" and entry["ask"] == "0.6" for entry in saved))
        self.assertTrue(all(entry["quote_time"] == "2026-10-18T05:01:00+00:00" for entry in saved))
        self.assertEqual(len(self.output_rows()), 2)
        for row in self.output_rows():
            for venue in ("kalshi", "polymarket_intl"):
                self.assertEqual(row[f"{venue}_bid"], "0.4")
                self.assertEqual(row[f"{venue}_ask"], "0.6")
                self.assertEqual(row[f"{venue}_quote_source"], "saved public order book")
        self.assertIsNone(unmatched.bid)

    def test_invalid_quote_json_warns_without_blocking_fixture_mapping(self):
        self.write_snapshot()
        self.install_matched_markets()
        for content in ("{", "{}", "[1]"):
            with self.subTest(content=content):
                (self.source / "quotes.json").write_text(content, encoding="utf-8")
                with patch.object(R.Q, "_request_json") as request:
                    self.assertEqual(R.main(), 0)
                request.assert_not_called()
                self.assertEqual(len(self.output_rows()), 2)
                self.assertIn("quote snapshot invalid", (self.output / "data" / "run_report.txt").read_text())
                self.assertTrue(all(row["polymarket_intl_quote_status"] == "unavailable"
                                    for row in self.output_rows()))

    def test_invalid_saved_probability_is_explicit_and_nonfatal(self):
        self.write_snapshot()
        markets, _ = self.install_matched_markets()
        entries = self.saved_quotes(markets)
        entries[0]["bid"] = "NaN"
        (self.source / "quotes.json").write_text(json.dumps(entries), encoding="utf-8")
        with patch.object(R.Q, "_request_json") as request:
            self.assertEqual(R.main(), 0)
        request.assert_not_called()
        row = next(row for row in self.output_rows() if row["canonical_outcome"] == "australia")
        self.assertEqual(row["kalshi_bid"], "")
        self.assertEqual(row["kalshi_ask"], "0.6")
        self.assertEqual(row["kalshi_quote_status"], "one-sided")
        self.assertIn("invalid price outside finite 0..1", (self.output / "data" / "run_report.txt").read_text())

    def test_failed_live_quote_requests_leave_confirmed_mapping_available(self):
        markets, unmatched = self.install_matched_markets()
        raw = {venue: [] for venue in R.VENUES}
        for index, item in enumerate(markets):
            if item.venue == "kalshi":
                raw[item.venue].append({"ticker": item.market_id,
                                        "yes_bid_dollars": "0.4", "yes_ask_dollars": "0.6"})
            else:
                raw[item.venue].append({"id": item.market_id,
                                        "outcomes": [item.outcome, "Other"],
                                        "clobTokenIds": [str(1000 + index), str(2000 + index)]})
        coverage = {venue: {"complete": True, "method": "test collection", "reason": ""}
                    for venue in R.VENUES}
        result = (raw, dict.fromkeys(R.VENUES, "test collection"), [], coverage)
        with patch.object(R, "SNAPSHOT_DIR", None), patch.object(R, "collect", return_value=result), patch.object(
                R.Q, "_request_json", return_value=(503, None, "2026-10-19T10:53:00+00:00")) as request:
            self.assertEqual(R.main(), 0)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].args[0], "GET")
        self.assertIn("kalshi.com/trade-api/v2/markets?tickers=", request.call_args_list[0].args[1])
        self.assertEqual(request.call_args_list[1].args[0], "POST")
        self.assertEqual(request.call_args_list[1].args[1], "https://clob.polymarket.com/books")
        self.assertEqual({entry["token_id"] for entry in request.call_args_list[1].args[2]}, {"1002", "1003"})
        rows = self.output_rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["polymarket_intl_status"] == "Available" for row in rows))
        self.assertTrue(all(row["polymarket_intl_quote_status"] == "unavailable" for row in rows))
        self.assertTrue(all(row["kalshi_quote_status"] == "available" for row in rows))
        self.assertTrue(all("market snapshot" in row["kalshi_quote_source"] for row in rows))
        self.assertIsNone(unmatched.bid)
        self.assertIn("CLOB batch unavailable", (self.output / "data" / "run_report.txt").read_text())
        self.assertIn("discovery snapshot retained", (self.output / "data" / "run_report.txt").read_text())

    def test_replay_inside_build_is_loaded_before_build_is_replaced(self):
        self.write_snapshot(self.data)
        with patch.object(R, "SNAPSHOT_DIR", self.data):
            self.assertEqual(R.main(), 0)
        self.kalshi_parser.assert_called_once_with([{"venue": "kalshi"}], market_types={R.C.MATCH_WINNER})
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
                self.assertTrue(coverage["polymarket_intl"]["complete"])

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
        (self.source / "collection_meta.json").write_text(json.dumps(metadata))
        _, _, _, coverage = R.load_snapshot(self.source)
        self.assertEqual(coverage["kalshi"]["reason"], "pagination stopped early")
        self.assertFalse(coverage["kalshi"]["complete"])
        self.assertNotIn("parse_complete", coverage["kalshi"])
        self.assertEqual(coverage["polymarket_intl"]["reason"], "")
        self.assertTrue(coverage["polymarket_intl"]["complete"])

    def test_registered_sport_replays_extra_evidence_and_keeps_coverage_scoped(self):
        metadata = self.write_snapshot()
        metadata["sports"] = {"football": {venue: {"complete": True, "method": "saved_league"}
                                           for venue in R.VENUES}}
        (self.source / "collection_meta.json").write_text(json.dumps(metadata))
        for key in (*R.VENUES, "_teams", "_milestones"):
            (self.source / f"raw_football_{key}.json").write_text(json.dumps([{"kind": key}]))
        parsed = []

        def parse_league(raw):
            parsed.append(raw)
            return [], [], []

        adapter = R.S.SportAdapter("football", "NFL", tuple(R.VENUES),
                                  lambda: self.fail("replay collected live data"), parse_league)
        with patch.dict(R.S.ADAPTERS, {"football": adapter}), patch.object(
                R, "ENABLED_SPORTS", ("cricket", "football")), patch.object(
                R.Q, "_request_json", side_effect=AssertionError("replay requested quotes")):
            self.assertEqual(R.main(), 0)
            self.assertEqual(parsed[0]["_milestones"], [{"kind": "_milestones"}])
            self.assertTrue(self.output_metadata()["sports"]["football"]["kalshi"]["mapping_coverage_complete"])
            self.assertTrue((self.output / "data" / "raw_football__teams.json").exists())
            (self.source / "raw_football_kalshi.json").unlink()
            self.assertEqual(R.main(), 0)
            coverage = self.output_metadata()
            self.assertFalse(coverage["sports"]["football"]["kalshi"]["mapping_coverage_complete"])
            self.assertTrue(coverage["sports"]["cricket"]["kalshi"]["mapping_coverage_complete"])

    def test_explicit_collection_note_survives_successful_reparse(self):
        metadata = self.write_snapshot()
        metadata["polymarket_intl"].update(
            collection_reason="saved collection note",
            reason="saved collection note; COVERAGE_INCOMPLETE polymarket_intl: old parser issue",
        )
        (self.source / "collection_meta.json").write_text(json.dumps(metadata))
        self.assertEqual(R.main(), 0)
        coverage = self.output_metadata()
        self.assertEqual(coverage["polymarket_intl"]["reason"], "saved collection note")
        self.assertTrue(coverage["polymarket_intl"]["mapping_coverage_complete"])
        self.assertTrue(all(coverage[venue]["parse_complete"] for venue in R.VENUES))

    def test_parse_exception_is_reported_in_coverage_and_run_report(self):
        self.write_snapshot()
        self.kalshi_parser.side_effect = ValueError("broken source record")
        self.assertEqual(R.main(), 4)
        metadata = json.loads((self.data / "collection_meta.json").read_text())["kalshi"]
        self.assertFalse(metadata["parse_complete"])
        self.assertFalse(metadata["mapping_coverage_complete"])
        self.assertIn("parse failed ValueError('broken source record')", metadata["reason"])
        report = (self.data / "run_report.txt").read_text()
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
