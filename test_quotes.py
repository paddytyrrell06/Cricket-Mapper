import unittest
from threading import Barrier
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import canonical as C
import quotes as Q


COLLECTED = "2026-10-19T10:38:43+00:00"


def market(venue, outcome="Australia", market_id="123", **kwargs):
    return C.Market(venue=venue, venue_fixture_id="fixture", fixture_key=None,
                    market_type=C.MATCH_WINNER, outcome=outcome,
                    market_id=market_id, raw_outcome=outcome, **kwargs)


def us_record(**updates):
    return {"id": "123", "slug": "aus-nz", "marketSides": [
        {"description": "Australia", "long": True},
        {"description": "New Zealand", "long": False},
    ], "bestBidQuote": {"value": "0.31", "currency": "USD"},
        "bestAskQuote": {"value": "0.37", "currency": "USD"}, **updates}


def intl_record(**updates):
    return {"id": "123", "outcomes": '["Australia", "New Zealand"]',
            "clobTokenIds": '["101", "202"]', "version": "v1", **updates}


class RawAndReplayQuotesTests(unittest.TestCase):
    def test_kalshi_zero_is_valid_and_original_collection_time_is_preserved(self):
        m = market("kalshi")
        raw = {"kalshi": [{"ticker": "123", "yes_bid_dollars": "0.0000",
                           "yes_ask_dollars": "0.25", "yes_bid_size_fp": "8",
                           "_collected_at_utc": COLLECTED,
                           "updated_time": "2020-01-01T00:00:00Z"}]}
        entries, warnings = Q.enrich_quotes([m], raw, live=False)
        self.assertEqual(warnings, [])
        self.assertEqual((m.bid, m.ask, m.quote_status), ("0", "0.25", "available"))
        self.assertEqual(m.quote_time, COLLECTED)
        self.assertEqual(entries[0]["quote_time"], COLLECTED)

    def test_explicit_zero_sizes_remove_empty_book_sentinels(self):
        m = market("kalshi")
        Q.enrich_quotes([m], {"kalshi": [{"ticker": "123", "yes_bid_dollars": "0",
                          "yes_ask_dollars": "1", "yes_bid_size_fp": "0",
                          "yes_ask_size_fp": "0"}]}, live=False)
        self.assertEqual((m.bid, m.ask, m.quote_status), (None, None, "empty"))

    def test_internal_cross_is_preserved_for_reporting(self):
        m = market("kalshi")
        Q.enrich_quotes([m], {"kalshi": [{"ticker": "123", "yes_bid_dollars": "0.8",
                          "yes_ask_dollars": "0.6"}]}, live=False)
        self.assertEqual((m.bid, m.ask, m.quote_status), ("0.8", "0.6", "available"))

    def test_invalid_prices_never_become_quotes(self):
        for price in ("NaN", "Infinity", "-0.01", "1.01", "broken", True):
            with self.subTest(price=price):
                m = market("kalshi")
                _, warnings = Q.enrich_quotes([m], {"kalshi": [{
                    "ticker": "123", "yes_bid_dollars": price,
                    "yes_ask_dollars": price}]}, live=False)
                self.assertEqual((m.bid, m.ask, m.quote_status), (None, None, "unavailable"))
                self.assertTrue(warnings)

    def test_us_short_outcome_swaps_and_complements_both_sides(self):
        long, short = market("polymarket_us"), market("polymarket_us", "New Zealand")
        Q.enrich_quotes([long, short], {"polymarket_us": [us_record()]},
                        live=False, collected_at=COLLECTED)
        self.assertEqual((long.bid, long.ask), ("0.31", "0.37"))
        self.assertEqual((short.bid, short.ask), ("0.63", "0.69"))
        self.assertEqual(short.quote_time, COLLECTED)

    def test_us_one_sided_complement_changes_the_available_side(self):
        m = market("polymarket_us", "New Zealand")
        Q.enrich_quotes([m], {"polymarket_us": [us_record(bestAskQuote=None)]}, live=False)
        self.assertEqual((m.bid, m.ask, m.quote_status), (None, "0.69", "one-sided"))

    def test_unknown_us_orientation_is_unavailable(self):
        m = market("polymarket_us", "Other Team")
        _, warnings = Q.enrich_quotes([m], {"polymarket_us": [us_record()]}, live=False)
        self.assertEqual(m.quote_status, "unavailable")
        self.assertTrue(warnings)

    def test_replay_uses_only_matching_saved_outcome_and_never_fetches(self):
        m = market("polymarket_intl")
        saved = [{"venue": "polymarket_intl", "market_id": "123", "outcome": "Australia",
                  "bid": "0.4", "ask": "0.5", "quote_time": COLLECTED,
                  "quote_status": "available", "quote_source": "original source"},
                 {"venue": "polymarket_intl", "market_id": "123", "outcome": "New Zealand",
                  "bid": "0.7", "ask": "0.8", "quote_time": COLLECTED}]
        with patch.object(Q, "_request_json", side_effect=AssertionError("Replay requested network")):
            entries, warnings = Q.enrich_quotes([m], {}, live=False, saved_quotes=saved)
        self.assertEqual(warnings, [])
        self.assertEqual((m.bid, m.ask, m.quote_time), ("0.4", "0.5", COLLECTED))
        self.assertEqual(entries[0]["quote_source"], "original source")

    def test_replay_does_not_promote_display_or_last_prices(self):
        m = market("polymarket_intl")
        raw = intl_record(outcomePrices='["0.5", "0.5"]', lastTradePrice="0.6",
                          bestBid="0.4", bestAsk="0.6")
        with patch.object(Q, "_request_json", side_effect=AssertionError("Replay requested network")):
            Q.enrich_quotes([m], {"polymarket_intl": [raw]}, live=False)
        self.assertEqual((m.bid, m.ask, m.quote_status), (None, None, "unavailable"))

    def test_conflicting_saved_entries_are_not_guessed(self):
        m = market("kalshi")
        saved = [{"venue": "kalshi", "market_id": "123", "outcome": "Australia", "bid": bid}
                 for bid in ("0.2", "0.3")]
        _, warnings = Q.enrich_quotes([m], {}, live=False, saved_quotes=saved)
        self.assertEqual(m.quote_status, "unavailable")
        self.assertTrue(warnings)


class LiveQuotesTests(unittest.TestCase):
    def test_both_venues_refresh_concurrently(self):
        ready = Barrier(2, timeout=2)

        def kalshi(tickers):
            ready.wait()
            return {}, []

        def books(assets, slugs):
            ready.wait()
            return {}, {}, []

        with patch.object(Q, "_live_kalshi", side_effect=kalshi), patch.object(
                Q, "_live_books", side_effect=books):
            Q.enrich_quotes([], {}, live=True)

    def test_subscription_manifest_uses_exact_outcome_tokens_and_deduplicates(self):
        a, b = market("polymarket_intl"), market("polymarket_intl", "New Zealand")
        k = market("kalshi", market_id="KX-TICKER")
        entries = Q.stream_instruments([a, b, a, k], {"polymarket_intl": [intl_record()]})
        self.assertEqual(len(entries), 3)
        self.assertEqual({(row["outcome"], row["instrument_id"]) for row in entries},
                         {("australia", "101"), ("new zealand", "202"), ("australia", "KX-TICKER")})
        self.assertEqual(Q.stream_instruments([a], {}), [])

    def test_best_level_depth_is_aggregated_and_survives_replay(self):
        m = market("polymarket_intl")
        book = {"asset_id": "101", "bids": [
            {"price": "0.40", "size": "2"}, {"price": "0.4", "size": "3.5"},
            {"price": "0.3", "size": "90"}],
            "asks": [{"price": "0.5", "size": "4"}, {"price": "0.6", "size": "80"}]}
        with patch.object(Q, "_request_json", return_value=(200, [book], COLLECTED)):
            entries, _ = Q.enrich_quotes([m], {"polymarket_intl": [intl_record()]}, live=True)
        self.assertEqual((entries[0]["bid_size"], entries[0]["ask_size"]), ("5.5", "4"))
        restored = market("polymarket_intl")
        Q.enrich_quotes([restored], {}, live=False, saved_quotes=entries)
        self.assertEqual(restored.extra["quote_bid_size"], "5.5")
        self.assertEqual(restored.extra["quote_ask_size"], "4")

    def test_saved_zero_depth_cannot_restore_a_price(self):
        m = market("kalshi")
        entries, _ = Q.enrich_quotes([m], {}, live=False, saved_quotes=[{
            "venue": "kalshi", "market_id": "123", "outcome": "Australia",
            "bid": "0.4", "ask": "0.5", "bid_size": "0", "ask_size": "3",
            "quote_time": COLLECTED}])
        self.assertEqual((m.bid, m.ask, m.quote_status), (None, "0.5", "one-sided"))
        self.assertIsNone(entries[0]["bid"])

    def test_kalshi_refresh_batches_only_requested_tickers_in_groups_of_forty(self):
        tickers = {f"TICKER-{index:03d}" for index in range(83)}

        def response(method, url, payload):
            self.assertEqual(method, "GET")
            self.assertIsNone(payload)
            query = parse_qs(urlparse(url).query)
            batch = query["tickers"][0].split(",")
            self.assertLessEqual(len(batch), 40)
            self.assertEqual(int(query["limit"][0]), len(batch))
            return 200, {"markets": [{"ticker": ticker, "yes_bid_dollars": "0.2"}
                                      for ticker in batch] + [{"ticker": "UNREQUESTED"}]}, COLLECTED

        with patch.object(Q, "_request_json", side_effect=response) as request:
            refreshed, warnings = Q._live_kalshi(tickers)
        self.assertEqual(warnings, [])
        self.assertEqual(request.call_count, 3)
        self.assertEqual(set(refreshed), tickers)
        self.assertTrue(all(row["_collected_at_utc"] == COLLECTED for row in refreshed.values()))

    def test_kalshi_live_refresh_replaces_discovery_prices_and_observation_time(self):
        m = market("kalshi")
        original = {"ticker": "123", "status": "active", "yes_bid_dollars": "0.1",
                    "yes_ask_dollars": "0.2", "_collected_at_utc": "2026-10-19T09:00:00Z"}
        fresh = {"ticker": "123", "status": "active", "yes_bid_dollars": "0.7",
                 "yes_ask_dollars": "0.8", "yes_bid_size_fp": "5", "yes_ask_size_fp": "8"}
        with patch.object(Q, "_request_json", return_value=(200, {"markets": [fresh]}, COLLECTED)):
            entries, warnings = Q.enrich_quotes([m], {"kalshi": [original]}, live=True)
        self.assertEqual(warnings, [])
        self.assertEqual((m.bid, m.ask, m.quote_time), ("0.7", "0.8", COLLECTED))
        self.assertIn("refreshed market", m.quote_source)
        self.assertEqual(entries[0]["quote_time"], COLLECTED)
        self.assertEqual(original["yes_bid_dollars"], "0.1")

    def test_kalshi_closed_refresh_suppresses_the_old_open_quote(self):
        m = market("kalshi")
        original = {"ticker": "123", "status": "active", "yes_bid_dollars": "0.1",
                    "yes_ask_dollars": "0.2", "_collected_at_utc": "2026-10-19T09:00:00Z"}
        closed = {"ticker": "123", "status": "settled", "yes_bid_dollars": "0.7",
                  "yes_ask_dollars": "0.8"}
        with patch.object(Q, "_request_json", return_value=(200, {"markets": [closed]}, COLLECTED)):
            entries, warnings = Q.enrich_quotes([m], {"kalshi": [original]}, live=True)
        self.assertEqual((m.bid, m.ask, m.quote_status), (None, None, "unavailable"))
        self.assertEqual(m.quote_time, COLLECTED)
        self.assertEqual(entries[0]["quote_status"], "unavailable")
        self.assertIn("no longer open", m.quote_source)
        self.assertTrue(any("no longer open" in warning for warning in warnings))

    def test_kalshi_refresh_failure_keeps_the_original_observation_time(self):
        m = market("kalshi")
        original = {"ticker": "123", "status": "active", "yes_bid_dollars": "0.1",
                    "yes_ask_dollars": "0.2", "_collected_at_utc": "2026-10-19T09:00:00Z"}
        with patch.object(Q, "_request_json", return_value=(503, None, COLLECTED)):
            entries, warnings = Q.enrich_quotes([m], {"kalshi": [original]}, live=True,
                                                collected_at=COLLECTED)
        self.assertEqual((m.bid, m.ask), ("0.1", "0.2"))
        self.assertEqual(m.quote_time, "2026-10-19T09:00:00+00:00")
        self.assertEqual(entries[0]["quote_time"], m.quote_time)
        self.assertIn("market snapshot", m.quote_source)
        self.assertTrue(any("refresh unavailable" in warning for warning in warnings))

    def test_intl_observation_time_is_distinct_from_exchange_time_and_survives_replay(self):
        m = market("polymarket_intl")
        book = {"asset_id": "101", "timestamp": "1767225600000",
                "bids": [{"price": "0.2", "size": "3"}],
                "asks": [{"price": "0.3", "size": "4"}]}
        with patch.object(Q, "_request_json", return_value=(200, [book], COLLECTED)):
            entries, warnings = Q.enrich_quotes([m], {"polymarket_intl": [intl_record()]}, live=True)
        self.assertEqual(warnings, [])
        self.assertEqual(m.quote_time, COLLECTED)
        self.assertEqual(m.extra["quote_exchange_time"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(entries[0]["exchange_time"], m.extra["quote_exchange_time"])
        restored = market("polymarket_intl")
        with patch.object(Q, "_request_json", side_effect=AssertionError("Replay requested network")):
            saved, warnings = Q.enrich_quotes([restored], {}, live=False, saved_quotes=entries)
        self.assertEqual(warnings, [])
        self.assertEqual((restored.bid, restored.ask, restored.quote_time), ("0.2", "0.3", COLLECTED))
        self.assertEqual(restored.extra["quote_exchange_time"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(saved[0]["exchange_time"], entries[0]["exchange_time"])

    def test_intl_outcomes_use_their_own_books_and_extrema_ignore_zero_size(self):
        a, b = market("polymarket_intl"), market("polymarket_intl", "New Zealand")
        books = [{"asset_id": "101", "timestamp": "1792406323000",
                  "bids": [{"price": "0.1", "size": "20"}, {"price": "0.9", "size": "0"},
                           {"price": "0.4", "size": "3"}],
                  "asks": [{"price": "0.8", "size": "4"}, {"price": "0.5", "size": "5"}]},
                 {"asset_id": "202", "bids": [{"price": "0.48", "size": "1"}],
                  "asks": [{"price": "0.59", "size": "2"}]}]
        with patch.object(Q, "_request_json", return_value=(200, books, COLLECTED)) as request:
            _, warnings = Q.enrich_quotes([a, b], {"polymarket_intl": [intl_record()]}, live=True)
        self.assertEqual(warnings, [])
        self.assertEqual((a.bid, a.ask), ("0.4", "0.5"))
        self.assertEqual((b.bid, b.ask), ("0.48", "0.59"))
        request.assert_called_once_with("POST", Q.CLOB + "/books",
                                        [{"token_id": "101"}, {"token_id": "202"}])

    def test_intl_binary_target_selects_yes_even_if_yes_is_second(self):
        m = market("polymarket_intl", extra={"binary_target_market": True})
        raw = intl_record(outcomes='["No", "Yes"]')
        book = {"asset_id": "202", "bids": [], "asks": []}
        with patch.object(Q, "_request_json", return_value=(200, [book], COLLECTED)) as request:
            Q.enrich_quotes([m], {"polymarket_intl": [raw]}, live=True)
        self.assertEqual(request.call_args.args[2], [{"token_id": "202"}])
        self.assertEqual(m.quote_status, "empty")

    def test_v2_uses_position_ids_instead_of_legacy_tokens(self):
        m = market("polymarket_intl")
        raw = intl_record(version="v2", positionIds=["303", "404"])
        with patch.object(Q, "_request_json", return_value=(200, [], COLLECTED)) as request:
            Q.enrich_quotes([m], {"polymarket_intl": [raw]}, live=True)
        self.assertEqual(request.call_args.args[2], [{"token_id": "303"}])

    def test_us_fetches_bbo_once_per_instrument_and_preserves_short_orientation(self):
        a, b = market("polymarket_us"), market("polymarket_us", "New Zealand")
        body = {"marketData": {"marketSlug": "aus-nz", "bestBid": {"value": "0.41"},
                               "bestAsk": {"value": "0.48"}, "bidShares": "3", "askShares": "5"}}
        with patch.object(Q, "_request_json", return_value=(200, body, COLLECTED)) as request:
            _, warnings = Q.enrich_quotes([a, b], {"polymarket_us": [us_record()]}, live=True)
        self.assertEqual(warnings, [])
        request.assert_called_once()
        self.assertEqual((a.bid, a.ask), ("0.41", "0.48"))
        self.assertEqual((b.bid, b.ask), ("0.52", "0.59"))
        self.assertIn("/bbo", b.quote_source)

    def test_us_bbo_failure_falls_back_with_original_snapshot_timestamp(self):
        m = market("polymarket_us")
        raw = us_record(_collected_at_utc=COLLECTED)
        with patch.object(Q, "_request_json", return_value=(503, None, "2027-01-01T00:00:00Z")):
            _, warnings = Q.enrich_quotes([m], {"polymarket_us": [raw]}, live=True)
        self.assertEqual((m.bid, m.ask, m.quote_time), ("0.31", "0.37", COLLECTED))
        self.assertIn("event snapshot", m.quote_source)
        self.assertTrue(warnings)

    def test_us_explicit_empty_depth_removes_zero_one_sentinels(self):
        m = market("polymarket_us")
        body = {"marketData": {"marketSlug": "aus-nz", "bestBid": {"value": "0"},
                               "bestAsk": {"value": "1"}, "bidDepth": 0, "askDepth": 0}}
        with patch.object(Q, "_request_json", return_value=(200, body, COLLECTED)):
            _, warnings = Q.enrich_quotes([m], {"polymarket_us": [us_record()]}, live=True)
        self.assertEqual(warnings, [])
        self.assertEqual((m.bid, m.ask, m.quote_status), (None, None, "empty"))

    def test_wrong_us_instrument_is_never_used_for_its_quotes(self):
        m = market("polymarket_us")
        body = {"marketData": {"marketSlug": "some-other-match", "bestBid": {"value": "0.9"},
                               "bestAsk": {"value": "0.95"}}}
        with patch.object(Q, "_request_json", return_value=(200, body, COLLECTED)):
            _, warnings = Q.enrich_quotes([m], {"polymarket_us": [us_record()]}, live=True)
        self.assertEqual((m.bid, m.ask), ("0.31", "0.37"))
        self.assertTrue(any("instrument mismatch" in w for w in warnings))

    def test_intl_batch_failure_isolated_to_individual_books(self):
        m = market("polymarket_intl")
        book = {"asset_id": "101", "bids": [{"price": "0.2", "size": "2"}], "asks": []}
        with patch.object(Q, "_request_json", side_effect=[(400, None, COLLECTED),
                                                          (200, book, COLLECTED)]) as request:
            Q.enrich_quotes([m], {"polymarket_intl": [intl_record()]}, live=True)
        self.assertEqual(request.call_count, 2)
        self.assertEqual((m.bid, m.ask, m.quote_status), ("0.2", None, "one-sided"))

    def test_quote_network_failure_is_nonfatal_and_never_uses_last_trade(self):
        m = market("polymarket_intl")
        with patch.object(Q, "_request_json", side_effect=OSError("offline")):
            entries, warnings = Q.enrich_quotes([m], {"polymarket_intl": [
                intl_record(lastTradePrice="0.3")]}, live=True)
        self.assertEqual(entries[0]["quote_status"], "unavailable")
        self.assertTrue(warnings)


if __name__ == "__main__":
    unittest.main()
