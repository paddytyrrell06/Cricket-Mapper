"""Synthetic, offline tests of the read-only Betfair boundary and native prices."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from betfair_data import BetfairData, BetfairError, Config, PROJECTIONS


FIXTURE = Path(__file__).parent / "fixtures" / "betfair" / "nba_synthetic.json"


class BetfairDataTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text())
        self.now = datetime(2026, 10, 22, tzinfo=timezone.utc)
        self.calls = []
        self.config = Config(enabled=True, app_key="test-app-secret", session_token="test-session-secret", currency="AUD")

    def transport(self, url, payload, headers, *, certificate=False):
        self.calls.append((url, copy.deepcopy(payload), dict(headers), certificate))
        operation = url.rstrip("/").split("/")[-1]
        if operation == "listEventTypes":
            return [{"eventType": {"id": "7522", "name": "Basketball"}, "marketCount": 2},
                    {"eventType": {"id": "1", "name": "Soccer"}, "marketCount": 1000}]
        if operation == "listCompetitions":
            return [{"competition": {"id": "10547864", "name": "NBA"}},
                    {"competition": {"id": "2", "name": "NBA Preseason"}}]
        if operation == "listMarketCatalogue":
            return copy.deepcopy(self.fixture["catalogue"])
        if operation == "listMarketBook":
            return copy.deepcopy(self.fixture["books"])
        if operation == "certlogin":
            return {"loginStatus": "SUCCESS", "sessionToken": "test-owned-session"}
        if operation in {"keepAlive", "logout"}:
            return {"status": "SUCCESS"}
        self.fail("Unexpected API operation: " + operation)

    def provider(self, **kwargs):
        return BetfairData(kwargs.pop("config", self.config), kwargs.pop("transport", self.transport),
                           clock=lambda: self.now, monotonic_ns=lambda: 123456789, **kwargs)

    def test_missing_disabled_or_invalid_credentials_do_not_make_requests(self):
        for config in [Config(), Config(enabled=True), replace(self.config, region="invalid"),
                       replace(self.config, leagues=()), replace(self.config, currency="not-currency")]:
            provider = self.provider(config=config)
            self.assertEqual(provider.status()["state"], "not_configured")
            self.assertEqual(provider.refresh()["markets"], [])
        self.assertEqual(self.calls, [])

    def test_configuration_bounded_secret_repr_and_explicit_replay(self):
        config = Config.from_env({"BETFAIR_ENABLED": "true", "BETFAIR_APP_KEY": "hidden-app",
                                  "BETFAIR_PASSWORD": "hidden-pass", "BETFAIR_SESSION_TOKEN": "hidden-session",
                                  "BETFAIR_LOOKAHEAD_HOURS": "10000", "BETFAIR_CATALOGUE_INTERVAL": "bad",
                                  "BETFAIR_LEAGUES": "NBA, WNBA", "BETFAIR_CERT_FILE": "private/cert.crt"}, root="/tmp")
        self.assertEqual(config.lookahead_hours, 168)
        self.assertEqual(config.catalogue_interval, 900)
        self.assertEqual(config.leagues, ("NBA", "WNBA"))
        self.assertEqual(config.cert_file, "/tmp/private/cert.crt")
        self.assertNotIn("hidden", repr(config))
        self.assertTrue(Config.from_env({"BETFAIR_REPLAY_FILE": str(FIXTURE)}).enabled)
        self.assertFalse(Config.from_env({}).enabled)

    def test_only_market_data_allowlist_can_reach_betting_endpoint(self):
        provider = self.provider()
        for method in ("placeOrders", "cancelOrders", "replaceOrders", "updateOrders", "getAccountFunds", "listCurrentOrders"):
            with self.assertRaisesRegex(BetfairError, "read-only"):
                provider._call(method, {})
        self.assertEqual(self.calls, [])

    def test_discovery_uses_competitions_not_popularity_and_caches_catalogue(self):
        provider = self.provider()
        result = provider.refresh()
        self.assertEqual(result["state"], "ok")
        self.assertEqual(len(result["markets"]), 1)
        self.assertEqual(result["coverage"]["catalogue_requests"], 3)
        self.assertFalse(result["coverage"]["partial"])
        catalogue = [payload for url, payload, _, _ in self.calls if "listMarketCatalogue" in url]
        self.assertEqual(len(catalogue), 3)
        self.assertTrue(all(p["filter"]["competitionIds"] == ["10547864"] for p in catalogue))
        self.assertTrue(all(p["filter"]["marketTypeCodes"] == ["MATCH_ODDS"] for p in catalogue))
        self.assertTrue(all(p["filter"]["inPlayOnly"] is False for p in catalogue))
        self.assertTrue(all(p["sort"] == "FIRST_TO_START" and p["maxResults"] == "200" for p in catalogue))
        self.assertTrue(all(p["marketProjection"] == PROJECTIONS for p in catalogue))
        self.now += timedelta(seconds=15)
        provider.refresh()
        self.assertEqual(len(self.calls), 7)  # 2 discovery + 3 daily windows + 2 books.
        self.assertTrue(self.calls[-1][0].endswith("listMarketBook/"))
        self.now += timedelta(seconds=900)
        provider.refresh()
        self.assertEqual(sum("listEventTypes" in call[0] for call in self.calls), 2)

    def test_no_matching_competition_does_not_fall_back_to_other_leagues(self):
        provider = self.provider(config=replace(self.config, leagues=("WNBA",)))
        result = provider.refresh()
        self.assertEqual(result["markets"], [])
        self.assertEqual(result["coverage"]["competitions_selected"], 0)
        self.assertIn("WNBA", result["warnings"][0])
        self.assertEqual(len(self.calls), 2)

    def test_cached_catalogue_prunes_started_games_without_discovery_or_book_requests(self):
        self.fixture["catalogue"][0]["marketStartTime"] = "2026-10-22T00:01:00Z"
        provider = self.provider()
        self.assertEqual(len(provider.refresh()["markets"]), 1)
        count = len(self.calls)
        self.now += timedelta(seconds=61)  # Still inside the 900-second catalogue TTL.
        result = provider.refresh()
        self.assertEqual(result["markets"], [])
        self.assertEqual(result["coverage"]["markets_in_scope"], 0)
        self.assertEqual(len(self.calls), count)

    def test_book_inplay_race_visible_and_no_longer_polled_from_cached_catalogue(self):
        self.fixture["books"][0]["inplay"] = True
        provider = self.provider()
        result = provider.refresh()
        self.assertTrue(result["markets"][0]["inplay"])
        self.assertTrue(any("advanced in-play" in warning for warning in result["warnings"]))
        count = len(self.calls)
        self.now += timedelta(seconds=15)
        self.assertEqual(provider.refresh()["markets"], [])
        self.assertEqual(len(self.calls), count)

    def test_catalogue_cap_reported_and_requests_remain_bounded(self):
        def transport(url, payload, headers, **kwargs):
            result = self.transport(url, payload, headers, **kwargs)
            return result * 200 if "listMarketCatalogue" in url else result
        result = self.provider(transport=transport).refresh()
        self.assertTrue(result["coverage"]["partial"])
        self.assertEqual(result["coverage"]["capped_windows"], 3)
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(len(result["markets"]), 1)  # Overlapping daily responses deduplicate by native ID.

    def test_native_prices_stakes_rules_and_actual_receipt_clocks(self):
        market = self.provider().refresh()["markets"][0]
        self.assertEqual(market["currency"], "AUD")
        self.assertEqual(market["commission"]["market_base_rate"], "6")
        self.assertIsNone(market["commission"]["effective_rate"])
        self.assertIn("including overtime", market["rules"])
        self.assertTrue(market["is_market_data_delayed"])
        self.assertEqual(market["data_mode"], "delayed")
        self.assertEqual(market["observed_at"], "2026-10-22T00:00:00.000+00:00")
        self.assertEqual(market["received_monotonic_ns"], 123456789)
        self.assertIsNone(market["source_publish_time"])
        runner = market["runners"][0]
        self.assertEqual(runner["back"][0], {"odds": "2.2", "size": "1E+2"})
        self.assertEqual(runner["lay"][0], {"odds": "2.22", "size": "8E+1"})
        self.assertNotIn("bid", runner)
        self.assertNotIn("ask", runner)
        self.assertNotIn("pnl", runner)
        request = self.calls[-1][1]
        self.assertFalse(request["priceProjection"]["virtualise"])
        self.assertEqual(request["priceProjection"]["exBestOffersOverrides"]["bestPricesDepth"], 3)
        self.assertNotIn("orderProjection", request)
        self.assertNotIn("matchProjection", request)

    def test_books_batch_twenty_and_match_exact_selection_and_handicap(self):
        provider = self.provider()
        source = provider._normalise(self.fixture["catalogue"][0])
        source["runners"].append(dict(source["runners"][0], handicap="1"))
        markets = []
        for i in range(41):
            market = copy.deepcopy(source)
            market["market_id"] = "1." + str(i)
            markets.append(market)
        def transport(url, payload, headers, **kwargs):
            self.calls.append((url, payload, headers, kwargs))
            return [dict(copy.deepcopy(self.fixture["books"][0]), marketId=identity) for identity in payload["marketIds"]]
        provider.transport = transport
        result = provider.books(markets)
        self.assertEqual([len(c[1]["marketIds"]) for c in self.calls], [20, 20, 1])
        self.assertTrue(all(m["runners"][0]["back"] for m in result))
        self.assertTrue(all(not m["runners"][2]["back"] for m in result))

    def test_invalid_levels_are_filtered_and_sides_sorted_in_native_directions(self):
        values = [{"price": 2.2, "size": 1}, {"price": 2.3, "size": 2}, {"price": 2.1, "size": 3},
                  {"price": "NaN", "size": 100}, {"price": 1, "size": 10}, {"price": 3, "size": -1},
                  {"price": 2.5, "size": 0}, {"price": True, "size": 5}, None]
        self.assertEqual([x["odds"] for x in BetfairData._levels(values, reverse=True)], ["2.3", "2.2", "2.1"])
        self.assertEqual([x["odds"] for x in BetfairData._levels(values, reverse=False)], ["2.1", "2.2", "2.3"])

    def test_omitted_market_or_runner_does_not_keep_prior_prices(self):
        provider = self.provider()
        old = provider.refresh()["markets"]
        self.now += timedelta(seconds=15)
        provider.transport = lambda *args, **kwargs: []
        missing = provider.books(old)[0]
        self.assertFalse(missing["book_available"])
        self.assertIsNone(missing["observed_at"])
        self.assertFalse(missing["runners"][0]["back"])
        provider.transport = lambda *args, **kwargs: [dict(self.fixture["books"][0], runners=[])]
        empty = provider.books(old)[0]
        self.assertTrue(empty["book_available"])
        self.assertFalse(empty["runners"][0]["back"])
        self.assertIsNone(empty["runners"][0]["observed_at"])

    def test_failures_are_sanitized_and_old_quotes_never_returned_as_new(self):
        provider = self.provider()
        provider.refresh()
        def fail(*args, **kwargs):
            raise RuntimeError("test-app-secret test-session-secret untrusted server body")
        provider.transport = fail
        result = provider.refresh()
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["markets"], [])
        self.assertIsNone(result["observed_at"])
        self.assertNotIn("secret", json.dumps(result))
        provider.transport = lambda *args, **kwargs: {"error": "test-session-secret"}
        self.assertNotIn("secret", json.dumps(provider.refresh()))

    def test_certificate_login_uses_au_identity_and_only_owned_session_logs_out(self):
        config = replace(self.config, session_token="", username="test-name", password="test-pass",
                         cert_file="test.crt", key_file="test.key")
        provider = self.provider(config=config)
        provider.refresh()
        login = self.calls[0]
        self.assertEqual(login[0], "https://identitysso-cert.betfair.com.au/api/certlogin")
        self.assertTrue(login[3])
        self.assertEqual(login[1], {"username": "test-name", "password": "test-pass"})
        self.assertEqual(self.calls[1][2]["X-Authentication"], "test-owned-session")
        self.assertIn("api.betfair.com.au", self.calls[1][0])
        provider.close()
        self.assertTrue(self.calls[-1][0].endswith("/logout"))
        count = len(self.calls)
        provider.close()
        self.assertEqual(len(self.calls), count)

    def test_external_session_keepalive_and_regional_endpoints(self):
        provider = self.provider(config=replace(self.config, region="au"))
        provider.refresh()
        self.assertTrue(all("api.betfair.com/" in c[0] for c in self.calls))
        count = len(self.calls)
        provider.close()
        self.assertEqual(len(self.calls), count)
        self.now += timedelta(seconds=901)
        provider.refresh()
        self.assertTrue(any(c[0] == "https://identitysso.betfair.com.au/api/keepAlive" for c in self.calls))
        provider = self.provider(config=replace(self.config, region="global"))
        self.assertEqual(provider._identity("certlogin"), "https://identitysso-cert.betfair.com/api/certlogin")

    def test_expired_owned_session_can_reauthenticate_without_replaying_books(self):
        provider = self.provider(config=replace(self.config, session_token="", username="u", password="p",
                                               cert_file="test.crt", key_file="test.key"))
        provider.refresh()
        def expired(url, payload, headers, **kwargs):
            return {"detail": {"APINGException": {"errorCode": "INVALID_SESSION_INFORMATION"}}}
        provider.transport = expired
        result = provider.refresh()
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["markets"], [])
        self.assertIn("expired", result["message"])
        provider.transport = self.transport
        self.assertEqual(provider.refresh()["state"], "ok")
        self.assertEqual(sum("certlogin" in c[0] for c in self.calls), 2)

    def test_unknown_key_mode_not_promoted_to_live_and_unknown_currency_retained(self):
        self.fixture["books"][0]["isMarketDataDelayed"] = False
        market = self.provider(config=replace(self.config, currency="")).refresh()["markets"][0]
        self.assertIsNone(market["currency"])
        self.assertEqual(market["data_mode"], "unknown")
        market = self.provider(config=replace(self.config, data_mode="live")).refresh()["markets"][0]
        self.assertEqual(market["data_mode"], "live")
        self.fixture["books"][0]["isMarketDataDelayed"] = True
        self.assertEqual(self.provider(config=replace(self.config, data_mode="live")).refresh()["mode"], "delayed")

    def test_replay_explicit_synthetic_same_normalizers_and_original_clock(self):
        provider = self.provider(config=Config(enabled=True, replay_file=str(FIXTURE)))
        result = provider.refresh()
        self.assertEqual(result["state"], "replay")
        self.assertEqual(result["mode"], "synthetic")
        self.assertEqual(result["source"], "synthetic_replay")
        self.assertTrue(all(m["data_mode"] == "synthetic" for m in result["markets"]))
        self.assertEqual(result["markets"][0]["runners"][0]["back"][0]["odds"], "2.2")
        self.now += timedelta(days=7)
        self.assertEqual(provider.refresh()["observed_at"], result["observed_at"])
        self.assertEqual(self.calls, [])
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "not-explicit.json"
            filename.write_text(json.dumps(dict(self.fixture, synthetic=False)))
            provider = self.provider(config=Config(enabled=True, replay_file=str(filename)))
            self.assertEqual(provider.refresh()["state"], "error")


if __name__ == "__main__":
    unittest.main()
