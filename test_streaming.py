import json
import random
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import canonical as C
import matching as M
import settlement as S
from streaming import Comparison, LiveQuoteEngine, QuoteUpdate, _Deadlines, instrument_key


class Clock:
    def __init__(self):
        self.elapsed = 0
        self.start = datetime(2026, 10, 20, 1, 0, tzinfo=timezone.utc)

    def ns(self):
        return int(self.elapsed * 1_000_000_000)

    def now(self):
        return self.start + timedelta(seconds=self.elapsed)

    def advance(self, seconds):
        self.elapsed += seconds


LEFT = ("kalshi", "KXTEST-A", "australia")
RIGHT = ("polymarket_intl", "pm-a", "australia")


def pair(identifier="pair", left=LEFT, right=RIGHT):
    return Comparison(identifier, left, right, True, "normal_completion", "aligned", ())


def profile():
    return S.make_profile(
        sport="cricket", target_kind="team",
        payouts={state: "1" if state in {"win", "forfeit_before_start_win", "forfeit_after_start_win"}
                 else "0" if state in {"loss", "forfeit_before_start_loss", "forfeit_after_start_loss"}
                 else "0.5" for state in S.CRICKET_STATES},
        policies={"period": "full_match", "tiebreakers": "included_if_played",
                  "postponement": "wait_until_completed", "dls": "official_result"},
        evidence=[{"field": "synthetic contract", "text": "All test states specified"}],
    )


def matched():
    key = C.FixtureKey("2026-10-20", frozenset({"australia", "new zealand"}), "M", "T20")
    fixtures = [C.Fixture(key, venue, venue, team_a="Australia", team_b="New Zealand")
                for venue in ("kalshi", "polymarket_intl")]
    accepted, _, _ = M.match_fixtures(fixtures)
    markets = [C.Market(f.venue, f.venue_fixture_id, key, C.MATCH_WINNER, "Australia",
                        market_id=identifier, extra={"settlement": profile()})
               for f, identifier in zip(fixtures, (LEFT[1], RIGHT[1]))]
    matches, _, _ = M.match_markets(accepted, markets)
    return matches, markets


class LiveQuoteTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.engine = LiveQuoteEngine([pair()], clock_ns=self.clock.ns, wall_clock=self.clock.now)

    def update(self, key, **kwargs):
        return self.engine.apply(QuoteUpdate(key, **kwargs))

    def crossed(self):
        self.update(LEFT, bid="0.70", ask="0.80", bid_size="12", ask_size="8", sequence=10)
        return self.update(RIGHT, bid="0.30", ask="0.40", bid_size="9", ask_size="7", sequence=100)

    def test_atomic_bbo_cross_and_known_depth(self):
        result = self.crossed()
        self.assertEqual(len(result.comparisons), 1)
        cross = result.comparisons[0]
        self.assertTrue(cross.crossed)
        self.assertEqual(cross.gap, Decimal("0.30"))
        self.assertEqual(cross.bid_key, LEFT)
        self.assertEqual(cross.ask_key, RIGHT)
        self.assertTrue(cross.depth_confirmed)
        self.assertEqual(cross.available_size, Decimal("7"))
        # Replacing both sides is one operation: no intermediate local cross.
        result = self.update(LEFT, snapshot=False, bid="0.20", ask="0.25",
                             sequence=15, previous_sequence=10)
        self.assertTrue(result.applied)
        self.assertEqual(result.comparisons[0].bid_key, RIGHT)
        self.assertEqual(result.comparisons[0].gap, Decimal("0.05"))
        # A changed price without a new size cannot retain size at the old price.
        self.assertFalse(result.comparisons[0].depth_confirmed)
        self.assertIsNone(self.engine.quote(LEFT).ask_size)

    def test_decimal_edges_and_unknown_depth_remain_explicit(self):
        self.update(LEFT, bid="0.60000000000000000001", ask="0.8")
        result = self.update(RIGHT, bid="0.3", ask="0.60000000000000000000")
        self.assertEqual(result.comparisons[0].gap, Decimal("0.00000000000000000001"))
        self.assertTrue(result.comparisons[0].crossed)
        self.assertFalse(result.comparisons[0].depth_confirmed)
        self.assertIsNone(result.comparisons[0].available_size)
        json.dumps(self.engine.snapshot(), allow_nan=False)

    def test_snapshot_removes_missing_sides_and_zero_depth_is_absent(self):
        self.crossed()
        self.update(LEFT, bid="0.7", sequence=11)
        self.assertIsNone(self.engine.quote(LEFT).ask)
        result = self.update(RIGHT, ask="0.4", ask_size="0", sequence=101)
        self.assertIsNone(self.engine.quote(RIGHT).ask)
        self.assertFalse(result.comparisons[0].crossed)
        self.assertEqual(result.comparisons[0].reason, "unpriced")
        # Real boundary prices survive; omitted sides never become 0/1.
        self.update(RIGHT, bid="0", ask="1", bid_size="1", ask_size="1", sequence=102)
        self.assertEqual(self.engine.quote(RIGHT).bid, Decimal("0"))
        self.assertEqual(self.engine.quote(RIGHT).ask, Decimal("1"))

    def test_malformed_or_local_crossed_book_invalidates_until_snapshot(self):
        self.crossed()
        for value in ("NaN", "Infinity", "1.2", "-0.1", True):
            result = self.update(LEFT, bid=value, ask="0.8", sequence=11)
            self.assertEqual(result.reason, "invalid_book")
            self.assertFalse(result.comparisons[0].crossed)
            self.assertIsNone(self.engine.quote(LEFT).bid)
        result = self.update(LEFT, bid="0.9", ask="0.8", sequence=11)
        self.assertEqual(result.reason, "invalid_book")
        self.assertEqual(self.update(LEFT, snapshot=False, bid="0.6", sequence=12).reason, "snapshot_required")
        result = self.update(LEFT, bid="0.6", ask="0.8", sequence=13)
        self.assertTrue(result.applied)
        result = self.update(LEFT, bid="0.6", bid_size="-1", ask="0.8", sequence=14)
        self.assertEqual(result.reason, "invalid_book")

    def test_sparse_channel_sequences_previous_link_and_resynchronization(self):
        self.crossed()
        # Other instruments can occupy sequence 11..16 in the source channel.
        result = self.update(LEFT, snapshot=False, bid="0.71", sequence=17, previous_sequence=10)
        self.assertTrue(result.applied)
        result = self.update(LEFT, snapshot=False, bid="0.72", sequence=20, previous_sequence=19)
        self.assertEqual(result.reason, "sequence_gap")
        self.assertFalse(result.comparisons[0].crossed)
        self.assertEqual(self.update(LEFT, snapshot=False, sequence=21).reason, "snapshot_required")
        result = self.update(LEFT, bid="0.7", ask="0.8", sequence=22)
        self.assertTrue(result.comparisons[0].crossed)

    def test_out_of_order_and_duplicate_packets_do_not_refresh_deadline(self):
        self.crossed()
        self.clock.advance(3)
        for sequence in (9, 10):
            result = self.update(LEFT, bid="0.2", ask="0.3", sequence=sequence)
            self.assertEqual(result.reason, "out_of_order")
            self.assertEqual(result.comparisons, ())
        self.assertEqual(self.update(LEFT, bid="0.2", ask="0.3").reason, "missing_sequence")
        self.clock.advance(2)
        self.assertFalse(self.engine.snapshot()["comparisons"][0]["crossed"])

    def test_disconnect_and_generation_reject_old_callbacks(self):
        self.crossed()
        result = self.engine.disconnect("kalshi")
        self.assertEqual(result.comparisons[0].reason, "disconnected")
        self.assertEqual(self.update(LEFT, bid="0.7", ask="0.8", sequence=11).reason, "disconnected")
        generation = self.engine.begin_session("kalshi")
        self.assertEqual(generation, 1)
        self.assertEqual(self.update(LEFT, bid="0.7", ask="0.8", sequence=11).reason, "old_generation")
        result = self.update(LEFT, generation=1, snapshot=False, bid="0.7", sequence=1)
        self.assertEqual(result.reason, "snapshot_required")
        result = self.update(LEFT, generation=1, bid="0.7", ask="0.8", sequence=1)
        self.assertTrue(result.comparisons[0].crossed)

    def test_expiry_without_messages_withdraws_cross_and_requires_snapshot(self):
        self.crossed()
        self.clock.advance(5)
        result = self.engine.expire()
        self.assertEqual(len(result.comparisons), 1)
        self.assertFalse(result.comparisons[0].crossed)
        self.assertEqual(result.comparisons[0].reason, "stale")
        self.assertEqual(self.update(LEFT, snapshot=False, bid="0.7", sequence=11).reason, "snapshot_required")
        self.assertEqual(self.engine.expire().comparisons, ())

    def test_snapshot_can_restore_unchanged_sequence_after_invalidation(self):
        self.crossed()
        self.engine.invalidate([LEFT], "channel_sequence_gap")
        result = self.update(LEFT, bid="0.7", ask="0.8", sequence=10)
        self.assertTrue(result.applied)
        self.assertTrue(result.comparisons[0].crossed)
        self.assertEqual(self.update(LEFT, bid="0.7", ask="0.8", sequence=10).reason, "out_of_order")
        self.engine.invalidate([LEFT])
        self.assertEqual(self.update(LEFT, bid="0.7", ask="0.8", sequence=9).reason, "out_of_order")

    def test_empty_delta_keeps_sequence_linkage_without_extending_freshness(self):
        self.crossed()
        original = self.engine.quote(LEFT)
        self.clock.advance(4)
        result = self.update(LEFT, snapshot=False, sequence=11, previous_sequence=10)
        self.assertEqual(result.reason, "sequence_only")
        self.assertEqual(result.comparisons, ())
        self.assertEqual(self.engine.quote(LEFT).observed_at, original.observed_at)
        self.assertEqual(self.engine.quote(LEFT).sequence, 11)
        self.clock.advance(1)
        self.assertEqual(self.engine.expire().comparisons[0].reason, "stale")

    def test_timestamps_distinguish_observation_exchange_and_future(self):
        self.update(LEFT, bid="0.7", ask="0.8", exchange_time="2000-01-01T00:00:00Z")
        self.clock.advance(2)
        result = self.update(RIGHT, bid="0.3", ask="0.4")
        self.assertEqual(result.comparisons[0].reason, "skewed")
        result = self.update(RIGHT, bid="0.3", ask="0.4",
                             observed_at=self.clock.now() + timedelta(seconds=1))
        self.assertEqual(result.reason, "future")
        result = self.update(RIGHT, bid="0.3", ask="0.4", observed_at="2026-10-20T01:00:00")
        self.assertEqual(result.reason, "unknown_time")
        self.assertEqual(self.engine.quote(LEFT).exchange_time, "2000-01-01T00:00:00Z")

    def test_saved_quotes_keep_original_time_and_depth(self):
        matches, markets = matched()
        self.engine = LiveQuoteEngine.from_matches(matches, clock_ns=self.clock.ns, wall_clock=self.clock.now)
        for market, bid, ask in zip(markets, ("0.7", "0.3"), ("0.8", "0.4")):
            market.bid, market.ask = bid, ask
            market.quote_time = self.clock.now().isoformat()
        markets[0].extra["quote_bid_size"] = "2.5"
        markets[1].extra["quote_ask_size"] = "10"
        result = self.engine.seed(markets)
        self.assertTrue(result.comparisons[0].crossed)
        self.assertEqual(result.comparisons[0].available_size, Decimal("2.5"))
        self.clock.advance(6)
        result = self.engine.seed(markets)
        self.assertFalse(result.applied)
        self.assertFalse(result.comparisons[0].crossed)
        self.assertEqual(result.comparisons[0].reason, "stale")

    def test_rules_and_mapping_are_rechecked_when_compiling(self):
        matches, markets = matched()
        markets[1].extra["settlement"]["payouts"]["draw"] = "0"
        self.engine = LiveQuoteEngine.from_matches(matches, clock_ns=self.clock.ns, wall_clock=self.clock.now)
        self.crossed()
        self.assertEqual(self.engine.snapshot()["comparisons"][0]["reason"], "rules_or_mapping_unverified")
        matches, markets = matched()
        matches[0].status = "review"
        self.engine = LiveQuoteEngine.from_matches(matches, clock_ns=self.clock.ns, wall_clock=self.clock.now)
        self.crossed()
        self.assertFalse(self.engine.snapshot()["comparisons"][0]["eligible"])

    def test_contingencies_survive_and_strict_mode_blocks_differences(self):
        matches, markets = matched()
        markets[1].extra["settlement"]["payouts"]["cancelled_before_start"] = "fair_market_price"
        for strict in (False, True):
            self.engine = LiveQuoteEngine.from_matches(matches, strict_settlement=strict,
                                                       clock_ns=self.clock.ns, wall_clock=self.clock.now)
            result = self.crossed().comparisons[0]
            self.assertEqual(result.crossed, not strict)
            self.assertTrue(result.caveats)
            self.assertEqual(result.contingency_status, "different")

    def test_same_venue_guard_and_unknown_instrument(self):
        with self.assertRaises(ValueError):
            LiveQuoteEngine([pair(right=(LEFT[0], "other", LEFT[2]))])
        self.assertEqual(self.update(("kalshi", "unknown", "australia"), bid="0.7").reason,
                         "unknown_instrument")
        self.assertEqual(instrument_key(C.Market("kalshi", "f", None, "m", "Australia", market_id="1")),
                         ("kalshi", "1", "australia"))

    def test_only_affected_comparisons_are_visited_and_deadlines_stay_bounded(self):
        shared = ("polymarket_us", "pm-us", "australia")
        pairs = [pair(), pair("shared", left=LEFT, right=shared)]
        pairs += [pair(str(i), ("kalshi", "k" + str(i), "team"),
                       ("polymarket_intl", "p" + str(i), "team")) for i in range(1000)]
        self.engine = LiveQuoteEngine(pairs, clock_ns=self.clock.ns, wall_clock=self.clock.now)

        class IndexedOnly(dict):
            def __iter__(self):
                raise AssertionError("hot path iterated all instruments/comparisons")

            def values(self):
                raise AssertionError("hot path iterated all instruments/comparisons")

        self.engine._comparisons = IndexedOnly(self.engine._comparisons)
        self.engine._quotes = IndexedOnly(self.engine._quotes)
        for sequence in range(1000):
            result = self.update(LEFT, bid="0.7", ask="0.8", sequence=sequence)
            self.assertEqual({c.comparison_id for c in result.comparisons}, {"pair", "shared"})
        self.assertEqual(len(self.engine._deadlines.heap), 1)


class DeadlineTests(unittest.TestCase):
    def test_indexed_heap_matches_sorted_reference_after_reschedules_and_removals(self):
        heap, reference = _Deadlines(), {}
        randomizer = random.Random(427)
        keys = [("venue", str(i), "yes") for i in range(80)]
        for _ in range(2000):
            key = randomizer.choice(keys)
            if randomizer.random() < 0.2:
                heap.discard(key)
                reference.pop(key, None)
            else:
                deadline = randomizer.randint(0, 1000)
                heap.set(key, deadline)
                reference[key] = deadline
            self.assertEqual(len(heap.heap), len(reference))
            if reference:
                self.assertEqual(heap.heap[0], min((stamp, key) for key, stamp in reference.items()))
        expected = [key for _, key in sorted((stamp, key) for key, stamp in reference.items())]
        actual = [heap.pop_due(1000) for _ in expected]
        self.assertEqual(actual, expected)
        self.assertIsNone(heap.pop_due(1000))


if __name__ == "__main__":
    unittest.main()
