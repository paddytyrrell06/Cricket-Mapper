import unittest

import audit as A
import canonical as C
import matching as M
import presentation as P


VENUES = ["kalshi", "polymarket_intl", "polymarket_us"]
STRANDED_CHECK = "no match-winner market is stranded on a confirmed fixture"
ROW_CHECK = "wide table statuses, identifiers and links are valid"


def fixture(venue, fixture_id, *, teams=("Australia", "New Zealand"), fmt="T20"):
    return C.Fixture(
        C.FixtureKey("2026-10-22", frozenset(C.norm_team(t) for t in teams), "W", fmt),
        venue, fixture_id, competition="International", start_time="08:15",
        team_a=teams[0], team_b=teams[1],
    )


def market(source, outcome):
    if source.venue == "kalshi":
        url = f"https://kalshi.com/markets/series/title/{source.venue_fixture_id}"
    else:
        domain = "polymarket.com" if source.venue == "polymarket_intl" else "polymarket.us"
        url = f"https://{domain}/event/{source.venue_fixture_id}"
    return C.Market(
        source.venue, source.venue_fixture_id, source.key, C.MATCH_WINNER, outcome,
        market_id=f"{source.venue_fixture_id}:{outcome}",
        event_id=source.venue_fixture_id, title=f"{outcome} wins", url=url,
    )


class StrandedMarketTests(unittest.TestCase):
    def setUp(self):
        self.left = fixture("kalshi", "k")
        self.right = fixture("polymarket_intl", "p")
        self.accepted, _, _ = M.match_fixtures([self.left, self.right])

    def audit_markets(self, markets, unmatched, *, fixtures=None):
        passed, _, checks = A.audit_runtime(
            fixtures or [self.left, self.right], markets,
            self.accepted, [], [], [], [], unmatched, [],
            dict.fromkeys(VENUES, True), VENUES,
        )
        return passed, next(check for check in checks if check.name == STRANDED_CHECK)

    def test_single_venue_draw_is_legitimately_unmatched(self):
        draw = market(self.left, "draw")
        passed, check = self.audit_markets([draw], [draw])
        self.assertTrue(check.passed, check.detail)
        self.assertTrue(passed)

    def test_published_counterpart_on_confirmed_fixture_pair_is_stranded(self):
        left = market(self.left, "draw")
        right = market(self.right, "tie")
        passed, check = self.audit_markets([left, right], [left])
        self.assertFalse(check.passed)
        self.assertIn(left.market_id, check.detail)
        self.assertFalse(passed)

    def test_same_key_on_unconfirmed_venue_pair_does_not_prove_counterpart(self):
        third = fixture("polymarket_us", "u")
        right = market(self.right, "draw")
        other = market(third, "draw")
        passed, check = self.audit_markets(
            [right, other], [right, other], fixtures=[self.left, self.right, third])
        self.assertTrue(check.passed, check.detail)
        self.assertTrue(passed)

    def test_same_venue_fixture_id_on_another_venue_is_not_confirmed(self):
        third = fixture("polymarket_us", self.left.venue_fixture_id)
        right = market(self.right, "draw")
        other = market(third, "draw")
        passed, check = self.audit_markets(
            [right, other], [right, other], fixtures=[self.left, self.right, third])
        self.assertTrue(check.passed, check.detail)
        self.assertTrue(passed)


class FixtureReviewAbsenceTests(unittest.TestCase):
    def test_review_between_two_accepted_groups_prevents_absence_on_both(self):
        fixtures = [
            fixture("kalshi", "k", teams=("Nigeria", "Zambia")),
            fixture("polymarket_intl", "i", teams=("Nigeria", "Gambia")),
            fixture("polymarket_us", "u1", teams=("Nigeria", "Zambia")),
            fixture("polymarket_us", "u2", teams=("Nigeria", "Gambia")),
        ]
        for index in (1, 3):
            source = fixtures[index]
            source.key = C.FixtureKey("2026-10-21", source.key.teams, "W", "T20")
        accepted, review, unmatched = M.match_fixtures(fixtures)
        markets = [market(source, outcome) for source in fixtures
                   for outcome in (source.team_a, source.team_b)]
        market_accepted, market_review, market_unmatched = M.match_markets(
            accepted + review, markets)
        coverage = dict.fromkeys(VENUES, True)
        rows = P.build_wide_rows(
            market_accepted, market_review, coverage, VENUES, fixture_review=review)
        older_rows = [row for row in rows if row["fixture_date"] == "2026-10-21"]
        self.assertEqual(len(older_rows), 2)
        self.assertTrue(all(row["kalshi_status"] == "Needs review" for row in older_rows))

        def audit_rows():
            return A.audit_runtime(
                fixtures, markets, accepted, review, unmatched,
                market_accepted, market_review, market_unmatched,
                rows, coverage, VENUES,
            )

        passed, report, _ = audit_rows()
        self.assertTrue(passed, report)
        older_rows[0]["kalshi_status"] = "Not currently open"
        passed, _, checks = audit_rows()
        check = next(check for check in checks if check.name == ROW_CHECK)
        self.assertFalse(check.passed)
        self.assertIn("unresolved fixture candidate on kalshi", check.detail)
        self.assertFalse(passed)

    def test_unresolved_third_venue_cannot_be_reported_absent(self):
        fixtures = [fixture("kalshi", "k"), fixture("polymarket_intl", "p"),
                    fixture("polymarket_us", "u",
                            teams=("Australia Women", "New Zealand Women"), fmt="")]
        accepted, review, unmatched = M.match_fixtures(fixtures)
        markets = [market(source, outcome)
                   for source in fixtures[:2]
                   for outcome in ("Australia", "New Zealand")]
        market_accepted, market_review, market_unmatched = M.match_markets(
            accepted + review, markets)
        coverage = dict.fromkeys(VENUES, True)
        rows = P.build_wide_rows(
            market_accepted, market_review, coverage, VENUES, fixture_review=review)

        def audit_rows(candidate_rows):
            return A.audit_runtime(
                fixtures, markets, accepted, review, unmatched,
                market_accepted, market_review, market_unmatched,
                candidate_rows, coverage, VENUES,
            )

        self.assertTrue(rows)
        self.assertTrue(all(row["polymarket_us_status"] == "Needs review" for row in rows))
        passed, report, _ = audit_rows(rows)
        self.assertTrue(passed, report)
        rows[0]["polymarket_us_status"] = "Not currently open"
        passed, _, checks = audit_rows(rows)
        check = next(check for check in checks if check.name == ROW_CHECK)
        self.assertFalse(check.passed)
        self.assertIn("unresolved fixture candidate on polymarket_us", check.detail)
        self.assertFalse(passed)


if __name__ == "__main__":
    unittest.main()
