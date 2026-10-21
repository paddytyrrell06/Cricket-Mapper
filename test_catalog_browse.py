"""Catalog browsing is local metadata plus exact, previously recorded quotes."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import canonical as C
from market_catalog import MarketCatalog
from research import ResearchService
from research_store import ResearchStore


class CatalogBrowseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = ResearchStore(self.root / "data/eventlens.sqlite3")
        kalshi = {"event_ticker": "KXGDP-26", "category": "Economics", "title": "GDP growth",
                  "markets": [{"ticker": "KXGDP-26-2", "title": "GDP above 2%?", "status": "active",
                               "volume_fp": "7.00", "close_time": "2026-12-01T12:00:00Z"}]}
        polymarket = {"id": "btc-event", "title": "Bitcoin milestone", "tags": [{"slug": "crypto"}],
                      "markets": [{"id": "btc-market", "question": "Bitcoin over threshold?",
                                   "outcomes": '["No","Yes"]', "clobTokenIds": '["exact-no","exact-yes"]',
                                   "outcomePrices": '["0.40","0.60"]', "active": True, "closed": True,
                                   "volume": "100.00", "endDate": "2026-12-31T23:59:00Z"}]}
        self.requester = Mock(side_effect=lambda url, params: (
            200, {"events": [kalshi] if "kalshi" in url else [polymarket], "cursor": "", "next_cursor": ""},
            "2026-10-20T12:00:00Z"))
        self.catalog = MarketCatalog(self.store, self.requester)
        self.catalog.refresh(1)
        self.requester.reset_mock()

    def record(self, identifier, *, instrument=None, venue=None, observed_at="2026-10-21T12:00:00Z",
               available_at=None, bid="0.39", ask="0.43", source="recorded_test_book", market_id=None):
        row, assets = self.catalog.lookup(identifier)
        row = copy.deepcopy(row)
        original_venue = row["research_exchange"]
        venue = venue or original_venue
        original_market = row[original_venue + "_market_id"]
        original_instrument = next(iter(assets.values()))
        if venue != original_venue:
            for key in list(row):
                if key.startswith(original_venue + "_"):
                    del row[key]
            row["research_exchange"] = venue
        row[venue + "_market_id"] = market_id or original_market
        row.update({venue + "_bid": bid, venue + "_ask": ask,
                    venue + "_bid_size": "12", venue + "_ask_size": "15",
                    venue + "_quote_time": observed_at, venue + "_quote_status": "ok",
                    venue + "_quote_source": source})
        assets = {(venue, row[venue + "_market_id"], C.norm_team(row["canonical_outcome"])):
                  instrument or original_instrument}
        self.store.record([row], assets=assets, available_at=available_at or observed_at, select=False)

    def choices(self):
        return {(row["exchange"], row["outcome"]): row for row in self.catalog.page()["markets"]}

    def test_pagination_sorting_and_filters_do_not_fetch_or_create_quote_history(self):
        ascending = self.catalog.page(sort="name", direction="asc")
        self.assertEqual(ascending["total"], 3)
        self.assertEqual(ascending["coverage"]["markets"], 2)
        names = [row["name"] for row in ascending["markets"]]
        self.assertEqual(names, sorted(names, key=str.casefold))
        descending = self.catalog.page(sort="name", direction="desc")["markets"]
        self.assertEqual([row["title"] for row in descending],
                         sorted((row["title"] for row in ascending["markets"]), key=str.casefold, reverse=True))
        self.assertEqual({row["id"] for row in descending}, {row["id"] for row in ascending["markets"]})
        pages = [self.catalog.page(limit=1, offset=offset) for offset in range(4)]
        self.assertEqual([page["total"] for page in pages], [3] * 4)
        self.assertEqual([page["markets"][0]["id"] for page in pages[:3]],
                         [row["id"] for row in ascending["markets"]])
        self.assertEqual(pages[3]["markets"], [])
        self.assertEqual(self.catalog.page(category="Economics")["total"], 1)
        self.assertEqual(self.catalog.page(exchange="polymarket_intl")["total"], 2)
        self.assertEqual(self.catalog.page(status="closed")["total"], 2)
        self.assertEqual(self.catalog.page(status="active")["total"], 1)
        self.assertEqual(self.catalog.page(search="BITCOIN", category="Crypto", exchange="polymarket_intl", status="closed")["total"], 2)
        self.assertEqual(self.catalog.page(search="no matching title")["total"], 0)
        volumes = self.catalog.page(sort="volume", direction="desc")["markets"]
        self.assertEqual([row["exchange"] for row in volumes], ["polymarket_intl", "polymarket_intl", "kalshi"])
        self.requester.assert_not_called()
        self.assertEqual(self.store.datasets(), [])

    def test_provider_outcome_prices_are_not_executable_quotes(self):
        required = {"id", "name", "category", "exchange", "status", "market_id", "outcome", "instrument_id",
                    "volume", "volume_unit", "expiry", "bid", "ask", "bid_size", "ask_size", "observed_at",
                    "quote_status", "source", "quote_age_seconds"}
        choices = self.choices()
        self.assertEqual(choices[("polymarket_intl", "No")]["instrument_id"], "exact-no")
        self.assertEqual(choices[("polymarket_intl", "Yes")]["instrument_id"], "exact-yes")
        for row in choices.values():
            self.assertTrue(required.issubset(row))
            for field in ("bid", "ask", "bid_size", "ask_size", "observed_at", "quote_age_seconds"):
                self.assertIsNone(row[field], (field, row))
            self.assertIn(row["quote_status"], (None, "not_collected"))
        self.requester.assert_not_called()

    def test_saved_quotes_join_exact_exchange_and_instrument_not_display_outcome_or_market(self):
        choices = self.choices()
        no, yes = choices[("polymarket_intl", "No")], choices[("polymarket_intl", "Yes")]
        self.record(no["id"], bid="0.39", ask="0.43")
        self.record(yes["id"], bid="0.59", ask="0.63")
        # A newer record for the same display outcome/market but another token
        # must not be associated with the currently mapped executable token.
        self.record(yes["id"], instrument="wrong-token", observed_at="2026-10-21T12:01:00Z", bid="0.01", ask="0.02")
        # Token strings alone are insufficient across exchanges.
        self.record(no["id"], venue="kalshi", instrument="exact-no", observed_at="2026-10-21T12:02:00Z", bid="0.89", ask="0.90")
        choices = self.choices()
        self.assertEqual(float(choices[("polymarket_intl", "No")]["bid"]), .39)
        self.assertEqual(float(choices[("polymarket_intl", "Yes")]["ask"]), .63)
        self.assertIsNone(choices[("kalshi", "Yes")]["bid"])
        self.requester.assert_not_called()

    def test_newest_actual_observation_wins_over_late_archive_import_and_keeps_provenance(self):
        no = self.choices()[("polymarket_intl", "No")]
        # The exact instrument can be recorded under a previous catalog/display
        # identity: quote association must use the token, not that old identity.
        self.record(no["id"], market_id="previous-display-market", bid="0.39", ask="0.43", source="new_native_book")
        self.record(no["id"], observed_at="2026-10-20T12:00:00Z", available_at="2026-10-22T12:00:00Z",
                    bid="0.19", ask="0.23", source="late_archive")
        quote = self.choices()[("polymarket_intl", "No")]
        self.assertEqual((float(quote["bid"]), float(quote["ask"])), (.39, .43))
        self.assertEqual((float(quote["bid_size"]), float(quote["ask_size"])), (12, 15))
        self.assertEqual(quote["source"], "new_native_book")
        self.assertTrue(quote["observed_at"].startswith("2026-10-21T12:00:00"))
        self.assertIsInstance(quote["quote_age_seconds"], (int, float))
        self.requester.assert_not_called()

    def test_query_allowlists_and_page_bounds_reject_invalid_inputs(self):
        for arguments in ({"sort": "name; DROP TABLE research_catalog"}, {"sort": "unsupported"},
                          {"direction": "desc; DELETE FROM observations"}, {"direction": "sideways"},
                          {"exchange": "kalshi' OR 1=1 --"}, {"exchange": "other"},
                          *({"limit": value} for value in (0, -1, 101, True, "10")),
                          *({"offset": value} for value in (-1, 1000001, True, "0"))):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.catalog.page(**arguments)
        self.assertEqual(self.catalog.page(limit=100, offset=1000000)["markets"], [])
        self.assertEqual(self.catalog.page(search="%' OR 1=1 --")["total"], 0)
        self.assertEqual(self.catalog.summary()["markets"], 2)
        self.requester.assert_not_called()

    def test_catalog_api_maps_query_parameters_and_stays_read_only(self):
        service = ResearchService(self.root, self.store)
        service.catalog = self.catalog
        query = {"search": ["GDP"], "category": ["Economics"], "exchange": ["kalshi"],
                 "status": ["active"], "limit": ["1"], "offset": ["0"], "sort": ["name"], "direction": ["desc"]}
        response = service.get("/api/research/catalog", query)
        self.assertEqual(response["total"], 1)
        self.assertEqual(response["markets"][0]["market_id"], "KXGDP-26-2")
        self.assertEqual(service.get("/api/research/catalog", {"offset": ["1"], "exchange": ["kalshi"]})["markets"], [])
        for invalid in ({"limit": ["101"]}, {"offset": ["-1"]}, {"sort": ["SQL injection"]}):
            with self.subTest(query=invalid), self.assertRaises(ValueError):
                service.get("/api/research/catalog", invalid)
        self.requester.assert_not_called()
        self.assertEqual(self.store.datasets(), [])


if __name__ == "__main__":
    unittest.main()
