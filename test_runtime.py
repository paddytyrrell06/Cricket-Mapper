import copy
import csv
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import canonical as C
import quotes as Q
import runtime as R


def write_runtime_snapshot(root, *, stamp=None, depth="1000", version="v1", binary=False):
    """A tiny publication with reversed native outcomes and audited aliases."""
    data = Path(root) / "output" / "data"
    data.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now(timezone.utc).isoformat()
    names = ("Alpha Academy",) if binary else ("Alpha Academy", "Beta Academy")
    rows, manifest, kalshi = [], [], []
    tokens = {"Alpha Academy": "100", "Beta Academy": "200"}
    for number, name in enumerate(names):
        identity = "KXCS2GAME-26OCT211400ALPBET-" + str(number)
        row = {
            "sport": "esports", "domain": "esports", "canonical_fixture_id": "exact-esports-fixture",
            "fixture_date": "2026-10-21", "start_time_utc": "18:00", "competition": "CS2",
            "gender": "X", "format": "CS2", "team_a": "Alpha Academy", "team_b": "Beta Academy",
            "canonical_market_type": "match_winner", "canonical_outcome": name,
            "mapping_status": "accepted", "comparison_status": "compatible",
            "strict_comparison_status": "rules_unknown", "contingency_status": "different",
            "contingency_caveats": "[]", "venues_available": "2", "venues_checked": "2",
        }
        for venue in R.VENUES:
            market_id = identity if venue == "kalshi" else "101"
            for field, value in {
                "status": "Available", "market_id": market_id, "event_id": "native-event",
                "slug": "native-market", "title": "Published winner", "url": "https://example.com/market",
                "bid": "0.52" if venue == "kalshi" else "0.48",
                "ask": "0.55" if venue == "kalshi" else "0.51",
                "bid_size": depth or "", "ask_size": depth or "",
                "quote_time": stamp, "quote_status": "available", "quote_source": "original observation",
                "fee_metadata": json.dumps({"taker_fee": {"model": "zero", "source": "fixture"}}),
                "settlement_payouts": '{"win":"1","loss":"0","tie":"0.5"}',
                "settlement_policies": '{"period":"full_match"}',
                "settlement_evidence": '[{"field":"description","text":"Full match winner"}]',
                "settlement_label": "Full series winner",
                "minimum_order_size": "5", "minimum_order_notional_usd": "2",
            }.items():
                row[venue + "_" + field] = value
            manifest.append({"venue": venue, "market_id": market_id, "outcome": C.norm_team(name),
                             "instrument_id": market_id if venue == "kalshi" else tokens[name]})
        kalshi.append({"ticker": identity, "event_ticker": "native-event", "status": "active",
                       "yes_bid_dollars": "0.52", "yes_ask_dollars": "0.55",
                       "yes_bid_size_fp": depth, "yes_ask_size_fp": depth,
                       "_collected_at_utc": stamp})
        rows.append(row)
    with (data / "market_map.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    gamma = {"id": "101", "version": version,
             "outcomes": '["No","Yes"]' if binary else '["Beta esports","Alpha esports"]',
             "positionIds" if version == "v2" else "clobTokenIds": '["200","100"]',
             "_collected_at_utc": stamp}
    (data / "raw_esports_kalshi.json").write_text(json.dumps(kalshi))
    (data / "raw_esports_polymarket_intl.json").write_text(json.dumps([gamma]))
    (data / "stream_manifest.json").write_text(json.dumps({"instruments": manifest}))
    (data / "snapshot_meta.json").write_text(json.dumps({"sports": ["esports"], "created_at": stamp}))
    return rows


class MarketRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.calls = []
        write_runtime_snapshot(self.root)

    def collector(self, markets, raw, **kwargs):
        self.calls.append((list(markets), raw, kwargs))
        return [], []

    def runtime(self):
        return R.MarketRuntime(self.root, collector=self.collector)

    def test_reconstructs_published_alias_and_reversed_token_order_for_both_versions(self):
        for version in ("v1", "v2"):
            with self.subTest(version=version):
                write_runtime_snapshot(self.root, version=version)
                runtime = self.runtime()
                runtime.refresh()
                selected, raw, kwargs = self.calls[-1]
                gamma = [market for market in selected if market.venue == "polymarket_intl"]
                original = raw["polymarket_intl"][0]
                self.assertEqual({market.outcome: market.raw_outcome for market in gamma},
                                 {"Alpha Academy": "Alpha esports", "Beta Academy": "Beta esports"})
                self.assertEqual({market.outcome: Q._intl_asset(market, original) for market in gamma},
                                 {"Alpha Academy": "100", "Beta Academy": "200"})
                self.assertEqual(kwargs, {"live": True, "fee_context": {}, "refresh_fees": True})

    def test_reconstructs_binary_yes_target_without_treating_no_as_the_team_token(self):
        write_runtime_snapshot(self.root, binary=True)
        runtime = self.runtime()
        runtime.refresh()
        selected, raw, _ = self.calls[-1]
        market = next(market for market in selected if market.venue == "polymarket_intl")
        self.assertEqual(market.raw_outcome, "Yes")
        self.assertTrue(market.extra["binary_target_market"])
        self.assertEqual(Q._intl_asset(market, raw["polymarket_intl"][0]), "100")

    def test_loading_and_noop_refresh_preserve_original_quote_timestamps(self):
        stamp = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        write_runtime_snapshot(self.root, stamp=stamp)
        runtime = self.runtime()
        self.assertEqual(runtime.snapshot()["as_of"], stamp)
        self.assertTrue(all(market.quote_time == stamp for market in runtime._market_by_key.values()))
        runtime.refresh()
        self.assertTrue(all(market.quote_time == stamp for market in runtime._market_by_key.values()))
        self.assertEqual(runtime.snapshot()["as_of"], stamp)
        self.assertTrue(all(not record["crossed"] for record in runtime.snapshot()["records"]))

    def test_empty_watchlist_scan_and_failed_fetch_cannot_freshen_the_data_header(self):
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        write_runtime_snapshot(self.root, stamp=stamp)
        runtime = self.runtime()
        runtime.configure({"watch_only": True, "watchlist": []})
        runtime.refresh()
        self.assertEqual(self.calls[-1][0], [])
        self.assertEqual(runtime.snapshot()["as_of"], stamp)
        self.assertEqual(runtime.snapshot()["monitored_count"], 0)
        runtime.configure({"watch_only": False})
        runtime.collector = lambda *_args, **_kwargs: ([], ["mock all venue requests failed"])
        runtime.refresh()
        self.assertEqual(runtime.snapshot()["as_of"], stamp)
        self.assertEqual(runtime.snapshot()["warnings"], ["mock all venue requests failed"])

    def test_reconstructs_minimum_order_bounds_from_the_published_rows(self):
        runtime = self.runtime()
        self.assertTrue(all(market.extra["minimum_order_size"] == "5"
                            and market.extra["minimum_order_notional_usd"] == "2"
                            for market in runtime._market_by_key.values()))

    def test_refresh_performs_no_publication_or_snapshot_disk_writes(self):
        runtime = self.runtime()
        def files():
            return {str(path.relative_to(self.root)): (path.stat().st_mtime_ns,
                    hashlib.sha256(path.read_bytes()).hexdigest())
                    for path in self.root.rglob("*") if path.is_file()}
        before = files()
        runtime.refresh()
        self.assertEqual(files(), before)
        self.assertEqual(len(self.calls), 1)

    def test_watchlist_limits_selected_instruments_and_discards_unknown_keys(self):
        runtime = self.runtime()
        key = runtime.snapshot()["records"][0]["key"]
        settings = runtime.configure({"watchlist": [key, "unknown-market"], "watch_only": True})
        self.assertEqual(settings["watchlist"], [key])
        runtime.refresh()
        selected = self.calls[-1][0]
        self.assertEqual(len(selected), 2)
        self.assertTrue(all(market.outcome == "Alpha Academy" for market in selected))
        self.assertEqual(runtime.snapshot()["filtered_count"], 2)
        runtime.configure({"watch_only": False})
        runtime.refresh()
        self.assertEqual(len(self.calls[-1][0]), 4)

    def test_explicit_thin_depth_is_reprobed_after_interval(self):
        write_runtime_snapshot(self.root, depth="1")
        runtime = self.runtime()
        with patch("runtime.time.monotonic", side_effect=[100, 110, 161]):
            runtime.refresh()
            runtime.refresh()
            runtime.refresh()
        self.assertEqual([len(call[0]) for call in self.calls], [4, 0, 4])

    def test_unknown_current_depth_cannot_borrow_stale_raw_depth_and_be_pruned(self):
        write_runtime_snapshot(self.root, depth="1")
        runtime = self.runtime()
        for market in runtime._market_by_key.values():
            market.extra["quote_bid_size"] = None
            market.extra["quote_ask_size"] = None
        with patch("runtime.time.monotonic", side_effect=[100, 110]):
            runtime.refresh()
            runtime.refresh()
        self.assertEqual([len(call[0]) for call in self.calls], [4, 4])

    def test_stale_depth_gets_a_probe_even_inside_thin_backoff(self):
        write_runtime_snapshot(self.root, depth="1")
        runtime = self.runtime()
        with patch("runtime.time.monotonic", side_effect=[100, 110]):
            runtime.refresh()
            stamp = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
            for market in runtime._market_by_key.values():
                market.quote_time = stamp
            runtime.refresh()
        self.assertEqual([len(call[0]) for call in self.calls], [4, 4])

    def test_disabling_liquidity_bound_reenables_recently_pruned_instruments(self):
        write_runtime_snapshot(self.root, depth="1")
        runtime = self.runtime()
        with patch("runtime.time.monotonic", side_effect=[100, 110]):
            runtime.refresh()
            runtime.configure({"min_liquidity_usd": "0"})
            runtime.refresh()
        self.assertEqual([len(call[0]) for call in self.calls], [4, 4])

    def test_failed_collector_does_not_leak_partial_mutations_into_next_refresh(self):
        runtime = self.runtime()
        before = copy.deepcopy(runtime.snapshot())
        original = {key: (market.bid, copy.deepcopy(market.extra))
                    for key, market in runtime._market_by_key.items()}
        def fail(markets, *_args, **_kwargs):
            markets[0].bid = "0.99"
            markets[0].extra["taker_fee"]["model"] = "corrupted"
            raise OSError("mock interrupted quote batch")
        runtime.collector = fail
        with self.assertRaisesRegex(OSError, "interrupted"):
            runtime.refresh()
        self.assertEqual(runtime.snapshot(), before)
        actual = {key: (market.bid, market.extra) for key, market in runtime._market_by_key.items()}
        self.assertEqual(actual, original)
        runtime.collector = self.collector
        runtime.refresh()
        self.assertTrue(all(market.bid != "0.99" for market in self.calls[-1][0]))

    def test_view_calculation_failure_keeps_rows_quotes_revision_and_probe_times(self):
        runtime = self.runtime()
        before = copy.deepcopy(runtime.snapshot())
        markets = copy.deepcopy(runtime._market_by_key)
        def change(selected, *_args, **_kwargs):
            selected[0].bid = "0.53"
            return [], []
        runtime.collector = change
        with patch("runtime.P.build_terminal_records", side_effect=ValueError("mock view failure")):
            with self.assertRaisesRegex(ValueError, "view failure"):
                runtime.refresh()
        self.assertEqual(runtime.snapshot(), before)
        self.assertEqual(runtime._market_by_key, markets)
        self.assertEqual(runtime._probe_times, {})

    def test_settings_validation_is_atomic(self):
        runtime = self.runtime()
        before = runtime.settings()
        for invalid in ({"min_liquidity_usd": "NaN"}, {"min_liquidity_usd": "-1"},
                        {"watch_only": "true"}, {"watchlist": [123]}, {"unknown": 1}):
            with self.subTest(invalid=invalid):
                with self.assertRaises((ValueError, TypeError)):
                    runtime.configure(invalid)
                self.assertEqual(runtime.settings(), before)

    def test_same_revision_is_a_small_heartbeat_and_missed_revision_returns_full_rows(self):
        runtime = self.runtime()
        initial = runtime.snapshot()
        self.assertIsNone(runtime.snapshot(str(initial["revision"]))["records"])
        runtime.refresh()
        runtime.refresh()
        missed = runtime.snapshot(initial["revision"])
        self.assertFalse(missed.get("partial", False))
        self.assertEqual(len(missed["records"]), 2)

    def test_consecutive_revision_sends_only_changed_records(self):
        runtime = self.runtime()
        initial = runtime.snapshot()
        def change(markets, *_args, **_kwargs):
            market = next(market for market in markets
                          if market.venue == "kalshi" and market.outcome == "Alpha Academy")
            market.bid = "0.53"
            return [], []
        runtime.collector = change
        runtime.refresh()
        delta = runtime.snapshot(initial["revision"])
        self.assertTrue(delta["partial"])
        self.assertEqual([record["id"] for record in delta["records"]], [0])

    def test_unchanged_refresh_has_an_empty_consecutive_delta(self):
        runtime = self.runtime()
        initial = runtime.revision
        runtime.refresh()
        delta = runtime.snapshot(initial)
        self.assertTrue(delta["partial"])
        self.assertEqual(delta["records"], [])

    def test_new_observation_times_send_fresh_expiry_heartbeats_without_full_rows(self):
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        write_runtime_snapshot(self.root, stamp=stamp)
        runtime = self.runtime()
        initial = runtime.snapshot()
        def touch(markets, *_args, **_kwargs):
            for market in markets:
                market.quote_time = datetime.now(timezone.utc).isoformat()
            return [], []
        runtime.collector = touch
        runtime.refresh()
        delta = runtime.snapshot(initial["revision"])
        self.assertTrue(delta["partial"])
        self.assertEqual(delta["records"], [])
        self.assertEqual([record["id"] for record in delta["heartbeats"]], [0, 1])
        self.assertTrue(all(beat["expiry"] > initial["records"][beat["id"]]["expiry"]
                            for beat in delta["heartbeats"]))

    def test_discovery_republication_returns_full_new_universe_after_row_ids_change(self):
        runtime = self.runtime()
        initial = runtime.snapshot()
        write_runtime_snapshot(self.root, binary=True)
        metadata = self.root / "output" / "data" / "snapshot_meta.json"
        os.utime(metadata, ns=(runtime._fingerprint + 1000000, runtime._fingerprint + 1000000))
        runtime.refresh()
        update = runtime.snapshot(initial["revision"])
        self.assertFalse(update["partial"])
        self.assertEqual(len(update["records"]), 1)
        self.assertEqual(len(runtime._market_by_key), 2)

    def test_failed_discovery_reload_retains_published_frame_tokens_and_recovers_atomically(self):
        runtime = self.runtime()
        runtime.configure({"watchlist": [record["key"] for record in runtime.snapshot()["records"]]})
        runtime.refresh()
        before = copy.deepcopy(runtime.snapshot())
        markets = copy.deepcopy(runtime._market_by_key)
        raw = copy.deepcopy(runtime._raw)
        probes = dict(runtime._probe_times)
        fingerprint = runtime._fingerprint
        write_runtime_snapshot(self.root, binary=True)
        metadata = self.root / "output" / "data" / "snapshot_meta.json"
        os.utime(metadata, ns=(fingerprint + 1000000, fingerprint + 1000000))

        with patch("runtime.P.build_terminal_records", side_effect=ValueError("mock discovery view failure")):
            with self.assertRaisesRegex(ValueError, "discovery view failure"):
                runtime.refresh()

        self.assertEqual(runtime.snapshot(), before)
        self.assertEqual(runtime._market_by_key, markets)
        self.assertEqual(runtime._raw, raw)
        self.assertEqual(runtime._probe_times, probes)
        self.assertEqual(runtime._fingerprint, fingerprint)
        self.assertEqual(len(self.calls), 1)
        original = runtime._raw["polymarket_intl"][0]
        self.assertEqual({market.outcome: Q._intl_asset(market, original)
                          for market in runtime._market_by_key.values()
                          if market.venue == "polymarket_intl"},
                         {"Alpha Academy": "100", "Beta Academy": "200"})

        runtime.refresh()
        self.assertEqual(len(runtime.snapshot()["records"]), 1)
        self.assertGreater(runtime.revision, before["revision"])
        self.assertNotEqual(runtime._fingerprint, fingerprint)
        self.assertEqual(len(runtime.settings()["watchlist"]), 1)
        self.assertEqual(set(runtime._probe_times), set(runtime._market_by_key))
        self.assertTrue(all(market.outcome == "Alpha Academy" for market in self.calls[-1][0]))
        gamma = next(market for market in runtime._market_by_key.values()
                     if market.venue == "polymarket_intl")
        self.assertEqual(gamma.raw_outcome, "Yes")
        self.assertTrue(gamma.extra["binary_target_market"])
        self.assertEqual(Q._intl_asset(gamma, runtime._raw["polymarket_intl"][0]), "100")


if __name__ == "__main__":
    unittest.main()
