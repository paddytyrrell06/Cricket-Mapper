import unittest
import json
from datetime import datetime, timezone, timedelta

import audit as A
import canonical as C
import matching as M
import presentation as P
import settlement as S
from test_settlement import known_profile


VENUES = ["kalshi", "polymarket_intl"]
ALL_VENUES = VENUES + ["polymarket_us"]
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
        extra={"settlement": known_profile()},
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
        coverage = dict.fromkeys(ALL_VENUES, True)
        rows = P.build_wide_rows(
            market_accepted, market_review, coverage, ALL_VENUES, fixture_review=review)
        older_rows = [row for row in rows if row["fixture_date"] == "2026-10-21"]
        self.assertEqual(len(older_rows), 2)
        self.assertTrue(all(row["kalshi_status"] == "Needs review" for row in older_rows))

        def audit_rows():
            return A.audit_runtime(
                fixtures, markets, accepted, review, unmatched,
                market_accepted, market_review, market_unmatched,
                rows, coverage, ALL_VENUES,
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
        coverage = dict.fromkeys(ALL_VENUES, True)
        rows = P.build_wide_rows(
            market_accepted, market_review, coverage, ALL_VENUES, fixture_review=review)

        def audit_rows(candidate_rows):
            return A.audit_runtime(
                fixtures, markets, accepted, review, unmatched,
                market_accepted, market_review, market_unmatched,
                candidate_rows, coverage, ALL_VENUES,
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


class QuoteAuditTests(unittest.TestCase):
    now = datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.fixtures = [fixture("kalshi", "k"), fixture("polymarket_intl", "p")]
        self.fixture_accepted, _, _ = M.match_fixtures(self.fixtures)
        self.markets = [market(source, outcome) for source in self.fixtures
                        for outcome in ("Australia", "New Zealand")]
        for item in self.markets:
            item.bid, item.ask = "0.4", "0.6"
            item.quote_source = "public order book"
            item.quote_time = self.now.isoformat()
        self.accepted, _, _ = M.match_markets(self.fixture_accepted, self.markets)
        self.coverage = dict.fromkeys(VENUES, True)

    def rows(self):
        return P.build_wide_rows(self.accepted, [], self.coverage, VENUES, as_of=self.now)

    def audit_rows(self, rows):
        passed, report, checks = A.audit_runtime(
            self.fixtures, self.markets, self.fixture_accepted, [], [],
            self.accepted, [], [], rows, self.coverage, VENUES, as_of=self.now,
        )
        return passed, report, {check.name: check for check in checks}

    def test_finite_two_sided_prices_pass_with_zero_and_one_boundaries(self):
        for item in self.markets:
            item.bid, item.ask = "0", "1"
        passed, report, checks = self.audit_rows(self.rows())
        self.assertTrue(passed, report)
        self.assertTrue(checks["all mapped outcomes have bid and ask"].passed)
        self.assertTrue(checks["no crossed quotes observed"].passed)

    def test_wrong_net_edge_fee_or_top_capacity_blocks_publication(self):
        for item in self.markets:
            item.extra.update(quote_bid_size="12.7", quote_ask_size="8.4")
            if item.venue == "kalshi":
                item.bid, item.ask = "0.6", "0.7"
                item.extra["taker_fee"] = {"model": "kalshi_quadratic", "multiplier": "1"}
            else:
                item.bid, item.ask = "0.4", "0.5"
                item.extra["taker_fee"] = {"model": "polymarket_curve", "rate": ".05", "exponent": 1}
        passed, report, checks = self.audit_rows(self.rows())
        self.assertTrue(passed, report)
        name = "net edges agree with taker fees and top-level depth"
        for mutation in ({"net_gap": ".1"}, {"net_sell_fee": "0"}, {"net_total_fees": "0"},
                         {"net_top_quantity": "1000"}, {"net_top_total": "100"},
                         {"net_sell_venue": "polymarket_intl", "net_buy_venue": "polymarket_intl"}):
            with self.subTest(mutation=mutation):
                rows = self.rows()
                rows[0].update(mutation)
                passed, _, checks = self.audit_rows(rows)
                self.assertFalse(passed)
                self.assertFalse(checks[name].passed)
        rows = self.rows()
        schedule = json.loads(rows[0]["polymarket_intl_fee_schedule"])
        schedule["rate"] = "0"
        rows[0]["polymarket_intl_fee_schedule"] = json.dumps(schedule)
        self.assertFalse(self.audit_rows(rows)[2][name].passed)

    def test_unknown_fee_cannot_be_labeled_net_positive(self):
        rows = self.rows()
        self.assertEqual(rows[0]["net_crossed"], "false")
        rows[0].update(net_crossed="true", net_cross_eligible="true", net_gap=".5")
        passed, _, checks = self.audit_rows(rows)
        self.assertFalse(passed)
        self.assertFalse(checks["net edges agree with taker fees and top-level depth"].passed)

    def test_invalid_top_level_depth_blocks_publication(self):
        for value in ("NaN", "-1", "0", "Infinity", None, True):
            with self.subTest(value=value):
                rows = self.rows()
                rows[0]["kalshi_bid_size"] = value
                passed, _, checks = self.audit_rows(rows)
                self.assertFalse(passed)
                self.assertFalse(checks["quote prices, statuses and sources are valid"].passed)

    def test_invalid_probability_or_non_string_price_blocks_publication(self):
        for value in (None, True, 0.5, "NaN", "Infinity", "-0.01", "1.01", "bad"):
            with self.subTest(value=value):
                rows = self.rows()
                rows[0]["kalshi_bid"] = value
                passed, _, checks = self.audit_rows(rows)
                self.assertFalse(passed)
                self.assertFalse(checks["quote prices, statuses and sources are valid"].passed)

    def test_prices_on_unmapped_market_and_inconsistent_status_fail(self):
        mutations = [
            {"polymarket_intl_status": "Not currently open"},
            {"kalshi_quote_status": "one-sided"},
            {"kalshi_quote_source": ""},
            {"kalshi_quote_time": "2026-10-19T10:53:00"},
            {"kalshi_quote_time": "not-a-timestamp"},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                rows = self.rows()
                rows[0].update(mutation)
                passed, _, checks = self.audit_rows(rows)
                self.assertFalse(passed)
                self.assertFalse(checks["quote prices, statuses and sources are valid"].passed)

    def test_unknown_replayed_quote_time_is_allowed(self):
        for item in self.markets:
            if item.venue == "kalshi":
                item.quote_time = ""
        rows = self.rows()
        passed, report, _ = self.audit_rows(rows)
        self.assertTrue(passed, report)
        self.assertTrue(all(row["quote_check"] == "unknown" for row in rows))
        self.assertTrue(all(row["cross_eligible"] == "false" for row in rows))

    def test_crossed_quotes_are_flagged_advisories_and_remain_publishable(self):
        cases = [
            (("0.7", "0.6"), ("0.4", "0.8"), "within venue"),
            (("0.7", "0.8"), ("0.4", "0.6"), "across venues"),
            (("0.7", "0.6"), ("0.4", "0.5"), "within venue and across venues"),
        ]
        for left, right, kind in cases:
            with self.subTest(kind=kind):
                for item in self.markets:
                    item.bid, item.ask = left if item.venue == "kalshi" else right
                rows = self.rows()
                passed, report, checks = self.audit_rows(rows)
                self.assertTrue(passed, report)
                self.assertTrue(all(row["observed_cross_kind"] == kind for row in rows))
                eligible_cross = kind == "across venues"
                self.assertTrue(all(row["crossed"] == str(eligible_cross).lower() for row in rows))
                self.assertEqual(checks["no crossed quotes observed"].passed, not eligible_cross)
                self.assertEqual(checks["no crossed quotes observed"].severity, "advisory")

    def test_tied_prices_and_non_crossed_locked_quotes_are_not_crossed(self):
        for item in self.markets:
            item.bid, item.ask = "0.5", "0.5"
        rows = self.rows()
        passed, report, checks = self.audit_rows(rows)
        self.assertTrue(passed, report)
        self.assertTrue(all(row["best_bid_venue"] == "kalshi" for row in rows))
        self.assertTrue(checks["no crossed quotes observed"].passed)

    def test_wrong_summary_price_venue_gap_or_flags_blocks_publication(self):
        mutations = [
            {"best_bid": "0.400"}, {"best_ask": "0.7"},
            {"best_bid_venue": "polymarket_intl"}, {"best_ask_venue": "polymarket_us"},
            {"best_spread": "0.1"}, {"cross_gap": "0.1"},
            {"crossed": "true"}, {"cross_kind": "across venues"},
            {"kalshi_crossed": "true"},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                rows = self.rows()
                rows[0].update(mutation)
                passed, _, checks = self.audit_rows(rows)
                self.assertFalse(passed)
                self.assertFalse(checks["quote summaries and crossed flags agree"].passed)

    def test_one_sided_empty_and_unavailable_quotes_are_explicit_advisories(self):
        for status, bid, ask in (("one-sided", "0.4", None),
                                 ("empty", None, None),
                                 ("unavailable", None, None)):
            with self.subTest(status=status):
                for item in self.markets:
                    item.bid, item.ask, item.quote_status = bid, ask, status
                passed, report, checks = self.audit_rows(self.rows())
                self.assertTrue(passed, report)
                self.assertFalse(checks["all mapped outcomes have bid and ask"].passed)
                self.assertEqual(checks["all mapped outcomes have bid and ask"].severity,
                                 "advisory")

    def test_legacy_rows_without_quote_fields_skip_quote_checks(self):
        rows = self.rows()
        for row in rows:
            for key in list(row):
                if any(key.startswith(f"{venue}_") and
                       key.removeprefix(f"{venue}_") in
                       {"bid", "ask", "quote_time", "quote_status", "quote_source", "crossed"}
                       for venue in VENUES):
                    row.pop(key)
        passed, report, checks = self.audit_rows(rows)
        self.assertTrue(passed, report)
        self.assertNotIn("quote prices, statuses and sources are valid", checks)

    def test_settlement_mismatch_is_reviewable_but_cannot_advertise_a_cross(self):
        for item in self.markets:
            item.bid, item.ask = (("0.7", "0.8") if item.venue == "kalshi" else ("0.4", "0.6"))
            if item.venue == "polymarket_intl":
                item.extra["settlement"]["payouts"]["draw"] = "0"
        accepted, review, unmatched = M.match_markets(self.fixture_accepted, self.markets)
        self.assertEqual(accepted, [])
        self.assertEqual(len(review), 2)
        rows = P.build_wide_rows(accepted, review, self.coverage, VENUES,
                                 include_review=True, as_of=self.now)

        def audit_rows():
            return A.audit_runtime(
                self.fixtures, self.markets, self.fixture_accepted, [], [],
                accepted, review, unmatched, rows, self.coverage, VENUES, as_of=self.now)

        passed, report, _ = audit_rows()
        self.assertTrue(passed, report)
        self.assertTrue(all(row["observed_crossed"] == "true" for row in rows))
        self.assertTrue(all(row["comparison_status"] == "rules_differ" for row in rows))
        self.assertTrue(all(row["crossed"] == "false" for row in rows))
        rows[0]["crossed"] = "true"
        passed, _, checks = audit_rows()
        self.assertFalse(passed)
        self.assertFalse(next(check for check in checks
                              if check.name == "quote summaries and crossed flags agree").passed)

    def test_row_cannot_claim_compatible_rules_without_its_evidence(self):
        rows = self.rows()
        rows[0]["polymarket_intl_settlement_evidence"] = "[]"
        passed, _, checks = self.audit_rows(rows)
        self.assertFalse(passed)
        self.assertFalse(checks["quote prices, statuses and sources are valid"].passed)

    def test_cancellation_caveat_cannot_be_removed_from_a_conditional_cross(self):
        for item in self.markets:
            item.bid, item.ask = (("0.7", "0.8") if item.venue == "kalshi" else ("0.4", "0.6"))
            if item.venue == "kalshi":
                item.extra["settlement"]["payouts"]["cancelled_before_start"] = "fair_market_price"
        rows = self.rows()
        self.assertTrue(all(row["comparison_status"] == "compatible" for row in rows))
        self.assertTrue(all(row["cross_condition"] == "conditional" for row in rows))
        passed, report, _ = self.audit_rows(rows)
        self.assertTrue(passed, report)
        for mutation in ({"cross_condition": "aligned"}, {"contingency_caveats": "[]"},
                         {"strict_comparison_status": "compatible"}):
            with self.subTest(mutation=mutation):
                tampered = [dict(row) for row in rows]
                tampered[0].update(mutation)
                passed, _, _ = self.audit_rows(tampered)
                self.assertFalse(passed)

    def test_forged_cross_from_stale_observations_blocks_publication(self):
        for item in self.markets:
            item.bid, item.ask = (("0.7", "0.8") if item.venue == "kalshi" else ("0.4", "0.6"))
        rows = self.rows()
        self.assertTrue(all(row["crossed"] == "true" for row in rows))
        old_time = (self.now - timedelta(seconds=P.QUOTE_MAX_AGE_SECONDS + 1)).isoformat()
        for row in rows:
            for venue in VENUES:
                row[f"{venue}_quote_time"] = old_time
        passed, _, checks = self.audit_rows(rows)
        self.assertFalse(passed)
        self.assertFalse(checks["quote summaries and crossed flags agree"].passed)


class SportCoverageAuditTests(unittest.TestCase):
    def audit_empty(self, overall, scopes):
        return A.audit_runtime([], [], [], [], [], [], [], [], [], overall, VENUES,
                                sport_coverage=scopes)

    def test_one_complete_sport_scope_can_publish_with_other_scope_incomplete(self):
        scopes = {"cricket": dict.fromkeys(VENUES, True),
                  "basketball": dict.fromkeys(VENUES, False)}
        passed, report, checks = self.audit_empty(dict.fromkeys(VENUES, False), scopes)
        self.assertTrue(passed, report)
        gate = next(check for check in checks if check.name == "at least two venue collections complete")
        self.assertIn("complete sport scopes: cricket", gate.detail)

    def test_one_complete_venue_per_sport_does_not_prove_any_complete_scope(self):
        scopes = {"cricket": {"kalshi": True, "polymarket_intl": False},
                  "basketball": {"kalshi": False, "polymarket_intl": True}}
        passed, _, checks = self.audit_empty(dict.fromkeys(VENUES, True), scopes)
        self.assertFalse(passed)
        self.assertFalse(next(check for check in checks
                              if check.name == "at least two venue collections complete").passed)

    def test_other_sport_coverage_cannot_justify_false_absence(self):
        fixtures = [fixture("kalshi", "k"), fixture("polymarket_intl", "p")]
        fixture_accepted, _, _ = M.match_fixtures(fixtures)
        markets = [market(source, outcome) for source in fixtures
                   for outcome in ("Australia", "New Zealand")]
        accepted, review, unmatched = M.match_markets(fixture_accepted, markets)
        overall = dict.fromkeys(ALL_VENUES, True)
        scopes = {"cricket": {"kalshi": True, "polymarket_intl": True, "polymarket_us": False},
                  "basketball": dict.fromkeys(ALL_VENUES, True)}
        rows = P.build_wide_rows(accepted, review, overall, ALL_VENUES, sport_coverage=scopes)

        def audit_rows():
            return A.audit_runtime(fixtures, markets, fixture_accepted, [], [], accepted, review,
                                    unmatched, rows, overall, ALL_VENUES, sport_coverage=scopes)

        self.assertTrue(all(row["polymarket_us_status"] == "Coverage incomplete" for row in rows))
        passed, report, _ = audit_rows()
        self.assertTrue(passed, report)
        rows[0]["polymarket_us_status"] = "Not currently open"
        passed, _, checks = audit_rows()
        self.assertFalse(passed)
        self.assertFalse(next(check for check in checks if check.name == ROW_CHECK).passed)


if __name__ == "__main__":
    unittest.main()
