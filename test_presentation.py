import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import presentation as P
import canonical as C
import matching as M


VENUES = ["kalshi", "polymarket_intl", "polymarket_us"]


class WideRowsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SimpleNamespace(
            canonical_fixture_id="2026-10-19:W:T20:a-b",
            key=SimpleNamespace(date="2026-10-19", gender="W", fmt="T20"),
            start_time="09:00", competition="Example cup", team_a="A", team_b="B",
        )
        self.match = SimpleNamespace(
            fixture=self.fixture, market_type="match_winner", outcome="a",
            venue_1="kalshi", venue_2="polymarket_intl",
            m1=self.market("kalshi"), m2=self.market("polymarket_intl"),
        )
        self.coverage = dict.fromkeys(VENUES, True)

    @staticmethod
    def market(venue):
        return SimpleNamespace(
            event_id=f"{venue}-event", market_id=f"{venue}-market",
            venue_fixture_id=f"{venue}-fixture",
            slug=f"{venue}-slug", title="A to win", url=f"https://{venue}.example/event",
        )

    def test_complete_collection_without_candidate_can_assert_absence(self):
        rows = P.build_wide_rows([self.match], [], self.coverage, VENUES)
        self.assertEqual(rows[0]["polymarket_us_status"], "Not currently open")

    def test_fixture_review_prevents_absence_without_matching_market_outcome(self):
        fixture_review = SimpleNamespace(
            canonical_fixture_id=self.fixture.canonical_fixture_id,
            by_venue={venue: SimpleNamespace(venue_fixture_id=f"{venue}-fixture")
                      for venue in ("kalshi", "polymarket_us")},
        )
        rows = P.build_wide_rows(
            [self.match], [], self.coverage, VENUES, [fixture_review])
        self.assertEqual(rows[0]["polymarket_us_status"], "Needs review")
        self.assertEqual(rows[0]["kalshi_status"], "Available")
        self.assertEqual(rows[0]["polymarket_intl_status"], "Available")
        self.assertEqual(rows[0]["venues_available"], "2")

    def test_review_for_different_fixture_does_not_change_absence(self):
        fixture_review = SimpleNamespace(
            canonical_fixture_id="other-fixture",
            by_venue={venue: SimpleNamespace(venue_fixture_id=f"other-{venue}-fixture")
                      for venue in ("kalshi", "polymarket_us")},
        )
        rows = P.build_wide_rows(
            [self.match], [], self.coverage, VENUES, [fixture_review])
        self.assertEqual(rows[0]["polymarket_us_status"], "Not currently open")

    def test_review_between_two_accepted_fixtures_marks_both_canonical_groups(self):
        fixtures = []
        for venue, fixture_id, day, opponent in [
            ("kalshi", "k", "22", "Zambia"),
            ("polymarket_intl", "i", "21", "Gambia"),
            ("polymarket_us", "u1", "22", "Zambia"),
            ("polymarket_us", "u2", "21", "Gambia"),
        ]:
            fixtures.append(C.Fixture(
                C.FixtureKey(f"2026-10-{day}", frozenset({"nigeria", opponent.lower()}),
                             "M", "T20"),
                venue, fixture_id, competition="International", start_time="08:00",
                team_a="Nigeria", team_b=opponent,
            ))
        markets = [C.Market(
            source.venue, source.venue_fixture_id, source.key, C.MATCH_WINNER, outcome,
            market_id=f"{source.venue_fixture_id}:{outcome}",
            event_id=source.venue_fixture_id, title=f"{outcome} wins",
        ) for source in fixtures for outcome in (source.team_a, source.team_b)]
        accepted, review, _ = M.match_fixtures(fixtures)
        market_accepted, market_review, _ = M.match_markets(accepted + review, markets)

        rows = P.build_wide_rows(
            market_accepted, market_review, self.coverage, VENUES, fixture_review=review)

        self.assertEqual(len(accepted), 2)
        self.assertTrue(review)
        self.assertEqual(len(rows), 4)
        for row in rows:
            with self.subTest(date=row["fixture_date"], outcome=row["canonical_outcome"]):
                if row["fixture_date"] == "2026-10-22":
                    self.assertEqual(row["kalshi_status"], "Available")
                    self.assertEqual(row["polymarket_intl_status"], "Needs review")
                    self.assertEqual(row["polymarket_us_event_id"], "u1")
                else:
                    self.assertEqual(row["kalshi_status"], "Needs review")
                    self.assertEqual(row["polymarket_intl_status"], "Available")
                    self.assertEqual(row["polymarket_us_event_id"], "u2")
                self.assertEqual(row["polymarket_us_status"], "Available")
                self.assertEqual(row["venues_available"], "2")


class ReportModeTests(unittest.TestCase):
    source_time = datetime(2026, 10, 18, 5, 0, tzinfo=timezone.utc)
    replay_time = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)

    def render(self, quality):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            with patch("presentation.datetime", wraps=datetime) as clock:
                clock.now.return_value = self.replay_time
                P.write_html(
                    path, [], VENUES, dict.fromkeys(VENUES, True),
                    self.source_time, quality,
                )
            return path.read_text(encoding="utf-8")

    def test_live_report_uses_collection_timestamp_for_freshness(self):
        document = self.render({"Run type": "LIVE"})
        self.assertIn("LIVE API REPORT", document)
        self.assertIn("Live collection completed", document)
        self.assertIn(f"new Date('{self.source_time.isoformat()}')", document)

    def test_replay_keeps_source_timestamp_separate_from_generation_age(self):
        document = self.render({"Run type": "REPLAY"})
        self.assertIn("SNAPSHOT REPLAY", document)
        self.assertIn("Cricket Market Map Replay", document)
        self.assertIn("Snapshot collected", document)
        self.assertIn("18 Oct 2026, 05:00:00 UTC", document)
        self.assertIn(f"new Date('{self.replay_time.isoformat()}')", document)
        self.assertIn("const ageLabel='Replay generated'", document)
        self.assertIn("Venue availability reflects the saved collection.", document)
        self.assertNotIn("LIVE API REPORT", document)
        self.assertNotIn("Live collection completed", document)
        self.assertNotIn("regenerated from live venue APIs", document)

    def test_replay_without_source_time_does_not_claim_collection_timestamp(self):
        document = self.render({
            "Run type": "REPLAY", "Source collection": "Unknown",
        })
        self.assertIn("Source collection time unavailable", document)
        self.assertNotIn("18 Oct 2026, 05:00:00 UTC", document)
        self.assertIn(f"new Date('{self.replay_time.isoformat()}')", document)


if __name__ == "__main__":
    unittest.main()
