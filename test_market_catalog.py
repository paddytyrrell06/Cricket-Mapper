"""Catalog discovery must not inherit the matched sports universe's gates."""
import json
import tempfile
import unittest
from pathlib import Path

import canonical as C
from market_catalog import MarketCatalog
from research_store import ResearchStore, pair_key


def kalshi_event(ticker="KXGDP-26", category="Economics", **market_fields):
    return {"event_ticker": ticker, "category": category, "title": "GDP growth",
            "markets": [{"ticker": ticker + "-2", "title": "GDP above 2%?", "status": "active",
                         "volume_fp": "0.00", "rules_primary": "Official GDP release", **market_fields}]}


def polymarket_event(identifier="event-1", **market_fields):
    return {"id": identifier, "title": "Bitcoin milestone", "tags": [{"slug": "crypto", "label": "Crypto"}],
            "markets": [{"id": "pm-1", "question": "Bitcoin over threshold?", "outcomes": '["No","Yes"]',
                         "clobTokenIds": '["exact-no","exact-yes"]', "outcomePrices": '["0.40","0.60"]',
                         "active": True, "volume": "0", "description": "Settlement source", **market_fields}]}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ResearchStore(Path(self.directory.name) / "research.sqlite3")

    def tearDown(self):
        self.directory.cleanup()

    def test_unmatched_zero_liquidity_exact_outcomes_and_provenance_are_preserved(self):
        calls = []
        def request(url, params):
            calls.append((url, params))
            return (200, {"events": [kalshi_event()], "cursor": ""}) if "/trade-api/" in url else (200, {"events": [polymarket_event()], "next_cursor": ""})
        catalog = MarketCatalog(self.store, request)
        self.assertEqual(calls, [])
        summary = catalog.refresh(max_pages=1)
        self.assertEqual(len(calls), 2)
        for _, params in calls:
            self.assertNotIn("category", params)
            self.assertNotIn("tag_id", params)
            self.assertNotIn("liquidity", params)
        self.assertEqual((summary["markets"], summary["outcomes"]), (2, 3))
        self.assertEqual({row["category"] for row in catalog.markets()}, {"Economics", "Crypto"})
        for choice in catalog.markets():
            row, assets = catalog.lookup(choice["id"])
            self.assertEqual(choice["id"], pair_key(row))
            self.assertEqual(row["comparison_status"], "unmatched")
            self.assertNotIn("polymarket_intl_bid", row)
            self.assertNotIn("polymarket_intl_ask", row)
            self.assertIn("raw_metadata", row)
            self.assertEqual(choice["status"], "active")
            if choice.get("exchange") == "polymarket_intl":
                expected = "exact-no" if row["canonical_outcome"] == "No" else "exact-yes"
                self.assertEqual(assets[("polymarket_intl", "pm-1", C.norm_team(row["canonical_outcome"]))], expected)
        self.assertIsNone(catalog.lookup("unknown"))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 0)
        self.assertTrue(all(item["complete"] for item in summary["exchanges"].values()))

    def test_request_caps_resume_cursors_then_restart_after_exhaustion(self):
        calls = []
        def request(url, params):
            calls.append((url, dict(params)))
            if "kalshi" in url:
                return 200, {"events": [kalshi_event()], "cursor": "next-k" if not params.get("cursor") else ""}
            return 200, {"events": [polymarket_event()], "next_cursor": "next-p" if not params.get("after_cursor") else ""}
        catalog = MarketCatalog(self.store, request)
        first = catalog.refresh(1)
        self.assertTrue(first["exchanges"]["kalshi"]["capped"])
        self.assertFalse(first["exchanges"]["kalshi"]["complete"])
        self.assertNotIn("cursor", calls[0][1])
        second = catalog.refresh(1)
        self.assertEqual(calls[2][1]["cursor"], "next-k")
        self.assertEqual(calls[3][1]["after_cursor"], "next-p")
        self.assertTrue(second["exchanges"]["kalshi"]["complete"])
        catalog.refresh(1)
        self.assertNotIn("cursor", calls[4][1])
        self.assertEqual(catalog.summary()["markets"], 2)
        self.assertEqual(len(calls), 6)

    def test_short_pages_with_continuation_are_not_mistaken_for_exhaustion(self):
        calls = []
        def request(url, params):
            calls.append((url, params))
            key = "cursor" if "kalshi" in url else "next_cursor"
            event = kalshi_event() if "kalshi" in url else polymarket_event()
            return 200, {"events": [event], key: "repeated"}
        result = MarketCatalog(self.store, request).refresh(2)
        self.assertEqual(len(calls), 4)
        self.assertIn("repeated cursor", result["exchanges"]["kalshi"]["warning"])
        self.assertFalse(result["exchanges"]["kalshi"]["complete"])

    def test_missing_tokens_retained_without_guessing_and_closed_children_labelled(self):
        def request(url, params):
            if "kalshi" in url:
                return 200, {"events": [kalshi_event(status="settled")], "cursor": ""}
            return 200, {"events": [polymarket_event(clobTokenIds='["only-one"]', closed=True)], "next_cursor": ""}
        catalog = MarketCatalog(self.store, request)
        result = catalog.refresh(1)
        self.assertEqual(result["markets"], 2)
        self.assertEqual(result["exchanges"]["polymarket_intl"]["missing_instruments"], 2)
        for item in catalog.markets():
            if item["exchange"] == "polymarket_intl":
                self.assertEqual(item["status"], "closed")
                self.assertEqual(item["instruments"], {})
        self.assertEqual(result["exchanges"]["kalshi"]["status_counts"]["closed"], 1)
        self.assertEqual(result["exchanges"]["polymarket_intl"]["status_counts"]["closed"], 1)

    def test_failed_scan_preserves_existing_metadata_and_reports_error(self):
        def initial(url, params):
            return 200, {"events": [kalshi_event()] if "kalshi" in url else [], "cursor": "", "next_cursor": ""}
        catalog = MarketCatalog(self.store, initial)
        catalog.refresh(1)
        catalog.requester = lambda url, params: (503, None)
        result = catalog.refresh(1)
        self.assertEqual(result["markets"], 1)
        self.assertFalse(result["exchanges"]["kalshi"]["complete"])
        self.assertIn("503", result["exchanges"]["kalshi"]["warning"])

    def test_metadata_survives_reopen_and_native_category_evidence_kept(self):
        event = kalshi_event(category="Climate and Weather")
        catalog = MarketCatalog(self.store, lambda url, params: (200, {"events": [event] if "kalshi" in url else [], "cursor": ""}))
        catalog.refresh(1)
        reopened = MarketCatalog(ResearchStore(self.store.path))
        item = reopened.markets()[0]
        row, assets = reopened.lookup(item["id"])
        self.assertEqual(item["category"], "Weather")
        self.assertEqual(row["native_category"], "Climate and Weather")
        self.assertEqual(row["category_source"], "native_event_category")
        self.assertEqual(row["kalshi_rules"].strip(), "Official GDP release")
        self.assertEqual(assets, {("kalshi", "KXGDP-26-2", "yes"): "KXGDP-26-2"})

    def test_limits_and_concurrent_scans_do_not_spend_requests(self):
        calls = []
        catalog = MarketCatalog(self.store, lambda *args: calls.append(args))
        for invalid in (0, -1, 21, "2", True):
            with self.assertRaises(ValueError):
                catalog.refresh(invalid)
        catalog._refresh_lock.acquire()
        try:
            with self.assertRaisesRegex(ValueError, "already running"):
                catalog.refresh(1)
        finally:
            catalog._refresh_lock.release()
        self.assertEqual(calls, [])

    def test_cached_response_preserves_actual_metadata_receive_time(self):
        stamp = "2026-10-20T12:00:00+00:00"
        catalog = MarketCatalog(self.store, lambda url, params: (200, {"events": [kalshi_event()] if "kalshi" in url else [], "cursor": ""}, stamp))
        catalog.refresh(1)
        choice = catalog.markets()[0]
        row, _ = catalog.lookup(choice["id"])
        self.assertEqual(row["kalshi_metadata_time"], "2026-10-20T12:00:00.000000+00:00")
        self.assertEqual(row["discovered_at"], row["kalshi_metadata_time"])


if __name__ == "__main__":
    unittest.main()
