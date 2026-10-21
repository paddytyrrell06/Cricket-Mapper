import itertools
import unittest

import audit
import canonical as C
import matching as M


def fixture(venue, fixture_id, teams=("Australia", "New Zealand"), *,
            date="2026-10-22", gender="W", fmt="T20", note=""):
    return C.Fixture(
        key=C.FixtureKey(date, frozenset(C.norm_team(t) for t in teams), gender, fmt),
        venue=venue, venue_fixture_id=fixture_id, competition="International",
        start_time="08:15", team_a=teams[0], team_b=teams[1], note=note,
    )


def pair_id(match):
    return tuple(sorted((f.venue, f.venue_fixture_id) for f in match.by_venue.values()))


class FixtureReviewRecallTests(unittest.TestCase):
    def test_sports_are_isolated_in_exact_loose_and_fuzzy_matching(self):
        for fmt, teams in [("T20", ("Australia", "New Zealand")),
                           ("", ("Australia", "New Zealand")),
                           ("T20", ("Australia Women", "New Zealand Women"))]:
            left = fixture("kalshi", "cricket")
            right = fixture("polymarket_intl", "basketball", teams=teams, fmt=fmt)
            right.key = C.FixtureKey(right.key.date, right.key.teams, right.key.gender,
                                     right.key.fmt, sport="basketball")
            accepted, review, unmatched = M.match_fixtures([left, right])
            self.assertEqual(accepted, [])
            self.assertEqual(review, [])
            self.assertEqual(unmatched, [left, right])
            self.assertFalse(M._recall_candidate(left, right)[0])

    def test_non_cricket_canonical_fixture_id_includes_sport(self):
        left, right = fixture("kalshi", "k"), fixture("polymarket_intl", "p")
        for f in (left, right):
            f.key = C.FixtureKey(f.key.date, f.key.teams, f.key.gender, f.key.fmt, sport="basketball")
        accepted, _, _ = M.match_fixtures([left, right])
        self.assertTrue(accepted[0].canonical_fixture_id.startswith("basketball:"))

    def test_womens_name_variants_are_reviewed_at_audit_recall_threshold(self):
        left = fixture("kalshi", "k")
        right = fixture("polymarket_intl", "p",
                        ("Australia Women", "New Zealand Women"), fmt="",
                        note="format not established")

        accepted, review, unmatched = M.match_fixtures([left, right])

        self.assertEqual(accepted, [])
        self.assertEqual(unmatched, [])
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0].status, "review")
        self.assertIn("similarity 0.75", review[0].reason)
        self.assertIn("format mismatch", review[0].reason)
        self.assertIn("format not established", review[0].reason)
        _, _, checks = audit.audit_runtime(
            [left, right], [], accepted, review, unmatched, [], [], [], [],
            {"kalshi": True, "polymarket_intl": True},
            ["kalshi", "polymarket_intl"],
        )
        self.assertTrue(next(c for c in checks
                             if c.name == "no plausible counterpart remains unmatched").passed)

    def test_third_venue_candidates_attach_to_accepted_fixture_in_any_input_order(self):
        left = fixture("kalshi", "k")
        right = fixture("polymarket_us", "u")
        third = fixture("polymarket_intl", "p",
                        ("Australia Women", "New Zealand Women"), fmt="")

        for ordered in itertools.permutations([left, right, third]):
            with self.subTest(order=[f.venue for f in ordered]):
                accepted, review, unmatched = M.match_fixtures(list(ordered))

                self.assertEqual(len(accepted), 1)
                self.assertEqual(len(review), 2)
                self.assertEqual(unmatched, [])
                self.assertTrue(all(len(m.by_venue) == 2 for m in review))
                self.assertEqual(len({pair_id(m) for m in accepted + review}), 3)
                self.assertTrue(all(m.key == accepted[0].key for m in review))
                self.assertTrue(all(m.canonical_fixture_id == accepted[0].canonical_fixture_id
                                    for m in review))

    def test_third_venue_metadata_mismatch_keeps_pairwise_reviews(self):
        fixtures = [fixture("kalshi", "k"), fixture("polymarket_us", "u"),
                    fixture("polymarket_intl", "p", fmt="",
                            note="format not established")]

        accepted, review, unmatched = M.match_fixtures(fixtures)

        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(review), 2)
        self.assertEqual(unmatched, [])
        self.assertTrue(all(m.key == accepted[0].key for m in review))
        self.assertTrue(all("format mismatch" in m.reason for m in review))
        self.assertTrue(all("format not established" in m.reason for m in review))

    def test_ambiguous_review_candidates_are_all_retained(self):
        fixtures = [fixture("kalshi", "k1"), fixture("kalshi", "k2"),
                    fixture("polymarket_intl", "p",
                            ("Australia Women", "New Zealand Women"))]

        accepted, review, unmatched = M.match_fixtures(fixtures)

        self.assertEqual(accepted, [])
        self.assertEqual(len(review), 2)
        self.assertEqual(unmatched, [])
        self.assertEqual(len({pair_id(m) for m in review}), 2)
        self.assertTrue(all("multiple candidate counterparts" in m.reason for m in review))

    def test_duplicate_exact_keys_are_reviewed_without_losing_source_fixtures(self):
        fixtures = [fixture("kalshi", "k1"), fixture("kalshi", "k2"),
                    fixture("polymarket_intl", "p")]

        accepted, review, unmatched = M.match_fixtures(fixtures)

        self.assertEqual(accepted, [])
        self.assertEqual(len(review), 2)
        self.assertEqual(unmatched, [])
        self.assertTrue(all("multiple fixtures share the key on kalshi" in m.reason
                            for m in review))
        self.assertEqual({f.venue_fixture_id for m in review for f in m.by_venue.values()},
                         {"k1", "k2", "p"})

    def test_fuzzy_recall_keeps_gender_format_date_and_venue_guards(self):
        left = fixture("kalshi", "k")
        variants = [
            fixture("polymarket_intl", "p", ("Australia Women", "New Zealand Women"),
                    gender="M"),
            fixture("polymarket_intl", "p", ("Australia Women", "New Zealand Women"),
                    fmt="LISTA"),
            fixture("polymarket_intl", "p", ("Australia Women", "New Zealand Women"),
                    date="2026-10-24"),
            fixture("kalshi", "p", ("Australia Women", "New Zealand Women")),
        ]
        for right in variants:
            with self.subTest(right=right):
                accepted, review, unmatched = M.match_fixtures([left, right])
                self.assertEqual(accepted, [])
                self.assertEqual(review, [])
                self.assertEqual(unmatched, [left, right])

    def test_unrelated_fixture_id_reused_by_another_venue_stays_unmatched(self):
        left = fixture("kalshi", "shared-id")
        right = fixture("polymarket_us", "shared-id")
        unrelated = fixture("polymarket_intl", "shared-id", ("India", "England"),
                            date="2026-11-01")

        accepted, review, unmatched = M.match_fixtures([left, right, unrelated])

        self.assertEqual(len(accepted), 1)
        self.assertEqual(review, [])
        self.assertEqual(unmatched, [unrelated])

    def test_unresolved_fixture_never_yields_accepted_markets(self):
        fixtures = [fixture("kalshi", "k"),
                    fixture("polymarket_intl", "p",
                            ("Australia Women", "New Zealand Women"))]
        accepted, review, _ = M.match_fixtures(fixtures)
        markets = [C.Market(f.venue, f.venue_fixture_id, f.key,
                            C.MATCH_WINNER, "Australia", market_id=f"{f.venue}:winner")
                   for f in fixtures]

        market_accepted, market_review, _ = M.match_markets(accepted + review, markets)

        self.assertEqual(market_accepted, [])
        self.assertEqual(len(market_review), 1)
        self.assertIn("fixture is unresolved", market_review[0].reason)
        self.assertIn(review[0].reason, market_review[0].reason)

    def test_market_review_preserves_the_published_start_time_conflict(self):
        left, right = fixture("kalshi", "k"), fixture("polymarket_intl", "p")
        right.start_time = "11:15"
        accepted, review, _ = M.match_fixtures([left, right])
        markets = [C.Market(f.venue, f.venue_fixture_id, f.key,
                            C.MATCH_WINNER, "Australia", market_id=f"{f.venue}:winner")
                   for f in (left, right)]

        market_accepted, market_review, _ = M.match_markets(accepted + review, markets)

        self.assertEqual(market_accepted, [])
        self.assertIn("fixture is unresolved: published start times differ by more than 30 minutes",
                      market_review[0].reason)


if __name__ == "__main__":
    unittest.main()
