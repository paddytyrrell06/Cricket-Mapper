import copy
import unittest
from unittest.mock import patch

import matching as M
import quotes as Q
import settlement
import sports as S


def kalshi_event(day="20"):
    ticker = f"KXNBAGAME-26OCT{day}BOSDET"
    markets = []
    for code, name in (("BOS", "Boston"), ("DET", "Detroit")):
        markets.append({
            "ticker": ticker + "-" + code, "event_ticker": ticker,
            "market_type": "binary", "yes_sub_title": name,
            "title": name + " wins", "status": "active",
            "occurrence_datetime": f"2026-10-{day}T23:00:00Z",
            "rules_primary": f"If {name} wins the basketball game, the market resolves to Yes.",
            "rules_secondary": "If postponed but begins within 48 hours, remain open. Otherwise resolve to a fair price.",
        })
    return {"event_ticker": ticker, "series_ticker": "KXNBAGAME",
            "title": "Boston vs Detroit", "markets": markets}


def gamma_event(day="20"):
    slug = f"nba-bos-det-2026-10-{day}"
    market = {
        "id": "winner-" + day, "slug": slug, "question": "Celtics vs. Pistons",
        "sportsMarketType": "moneyline", "active": True, "closed": False,
        "outcomes": '["Pistons", "Celtics"]', "clobTokenIds": '["200", "100"]',
        "gameStartTime": f"2026-10-{day}T23:00:00Z",
        "description": "If Celtics win, resolve Celtics. If Pistons win, resolve Pistons. "
                       "If postponed, remain open until the game is completed. "
                       "If canceled entirely, resolve 50-50. Final score including any overtime periods.",
        "resolutionSource": "https://www.nba.com/",
    }
    return {"id": "event-" + day, "slug": slug, "title": "Celtics vs. Pistons",
            "startTime": f"2026-10-{day}T23:00:00Z", "active": True, "closed": False,
            "teams": [{"name": "Celtics", "abbreviation": "bos", "league": "nba"},
                      {"name": "Pistons", "abbreviation": "det", "league": "nba"}],
            "markets": [market]}


def flatten_samples():
    event = kalshi_event()
    kalshi = []
    for market in event["markets"]:
        record = dict(market, _event_title=event["title"], _series_title="NBA Game",
                      _settlement_documents=[S.NBA_DOCUMENT], _contract_terms_url=S.NBA_TERMS)
        kalshi.append(record)
    event = gamma_event()
    gamma = [dict(event["markets"][0], _event_slug=event["slug"], _event_id=event["id"],
                  _event_teams=event["teams"], _event_start_time=event["startTime"])]
    return {"kalshi": kalshi, "polymarket_intl": gamma, "_teams": event["teams"]}


class NbaTeamRegistryTests(unittest.TestCase):
    def test_exact_league_code_and_published_alias_are_all_required(self):
        registry = S.TeamRegistry()
        self.assertEqual(registry.resolve("Boston", "bos"), "Boston Celtics")
        self.assertEqual(registry.resolve("Celtics", "BOS"), "Boston Celtics")
        self.assertEqual(registry.resolve("Los Angeles C", "LAC"), "Los Angeles Clippers")
        for name, code, league in (("Celtic", "BOS", "nba"),
                                   ("Los Angeles", "LAC", "nba"),
                                   ("Boston", "DET", "nba"),
                                   ("Boston", "BOS", "wnba")):
            self.assertEqual(registry.resolve(name, code, league=league), "")

    def test_native_alias_requires_recognized_nba_identity_and_never_accepts_empty(self):
        registry = S.TeamRegistry([
            {"name": "Celtics", "abbreviation": "bos", "league": "nba", "alias": "Boston Cs"},
            {"name": "Unrelated", "abbreviation": "det", "league": "nba", "alias": "Fake team"},
            {"name": "Pistons", "abbreviation": "det", "league": "nba"},
        ])
        self.assertEqual(registry.resolve("Boston Cs", "bos"), "Boston Celtics")
        self.assertEqual(registry.resolve("Fake team", "det"), "")
        self.assertEqual(registry.resolve("", "det"), "")


class NbaParserTests(unittest.TestCase):
    def test_native_winners_normalize_exactly_but_settlement_difference_stays_review(self):
        raw = flatten_samples()
        fixtures, markets, warnings = S.parse_basketball(raw)
        self.assertEqual(warnings, [])
        self.assertEqual(len(fixtures), 2)
        self.assertEqual(len(markets), 4)
        self.assertTrue(all(f.key.sport == "basketball" and f.key.fmt == "NBA" for f in fixtures))
        self.assertEqual(fixtures[0].key, fixtures[1].key)
        accepted, review, unmatched = M.match_fixtures(fixtures)
        self.assertEqual((len(accepted), len(review), len(unmatched)), (1, 0, 0))
        market_accepted, market_review, _ = M.match_markets(accepted, markets)
        self.assertEqual(market_accepted, [])
        self.assertEqual(len(market_review), 2)
        self.assertTrue(all("settlement rules differ" in match.reason for match in market_review))
        for name in ("Boston Celtics", "Detroit Pistons"):
            profiles = [m.extra["settlement"] for m in markets if m.outcome == name]
            self.assertEqual(settlement.compare_profiles(*profiles)["status"], "incompatible")

    def test_native_time_disagreement_is_not_silently_aligned(self):
        raw = flatten_samples()
        for record in raw["kalshi"]:
            record["occurrence_datetime"] = "2026-10-21T02:00:00Z"
        fixtures, _, warnings = S.parse_basketball(raw)
        self.assertEqual(warnings, [])
        self.assertEqual({f.key.date for f in fixtures}, {"2026-10-20", "2026-10-21"})
        accepted, review, unmatched = M.match_fixtures(fixtures)
        self.assertEqual((len(accepted), len(review), len(unmatched)), (0, 1, 0))
        self.assertIn("published start times differ", review[0].reason)
        self.assertIn("published dates differ", review[0].reason)

    def test_explicit_rules_start_takes_priority_and_retains_native_conflict(self):
        raw = flatten_samples()
        for record in raw["kalshi"]:
            record["rules_primary"] += " Originally scheduled for Oct 20, 2026 at 7:00 PM EDT."
            record["occurrence_datetime"] = "2026-10-21T02:00:00Z"
        fixtures, _, _ = S.parse_basketball(raw)
        kalshi = next(f for f in fixtures if f.venue == "kalshi")
        self.assertEqual((kalshi.key.date, kalshi.start_time), ("2026-10-20", "23:00"))
        self.assertIn("rules start conflicts", kalshi.note)

    def test_naive_native_game_time_uses_valid_timezone_aware_event_start(self):
        raw = flatten_samples()
        record = raw["polymarket_intl"][0]
        record["gameStartTime"] = "2026-10-20 19:00:00"
        record["_event_start_time"] = "2026-10-20T19:00:00Z"
        fixtures, markets, warnings = S.parse_basketball(raw)
        self.assertEqual(warnings, [])
        self.assertEqual((len(fixtures), len(markets)), (2, 4))
        fixture = next(item for item in fixtures if item.venue == "polymarket_intl")
        self.assertEqual((fixture.key.date, fixture.start_time), ("2026-10-20", "19:00"))
        self.assertEqual(fixture.extra["start_time_basis"], "native_event_start_timestamp_to_utc")
        self.assertIn("lacks a timezone", fixture.note)

    def test_outcome_token_mapping_preserves_native_order_and_published_names(self):
        raw = flatten_samples()
        _, markets, _ = S.parse_basketball(raw)
        outcomes = {m.outcome: m for m in markets if m.venue == "polymarket_intl"}
        self.assertEqual(Q._intl_asset(outcomes["Boston Celtics"], raw["polymarket_intl"][0]), "100")
        self.assertEqual(Q._intl_asset(outcomes["Detroit Pistons"], raw["polymarket_intl"][0]), "200")
        self.assertEqual(outcomes["Boston Celtics"].raw_outcome, "Celtics")
        self.assertTrue(outcomes["Boston Celtics"].extra["resolution_evidence"]["description"])

    def test_wrong_league_unknown_team_and_yes_no_prop_cannot_become_game_winners(self):
        for mutate in (
            lambda row: row["_event_teams"][0].update(league="wnba"),
            lambda row: row["_event_teams"][0].update(name="Celtic"),
            lambda row: row.update(outcomes='["Yes","No"]'),
            lambda row: row.update(sportsMarketType="player_points"),
        ):
            raw = flatten_samples()
            raw["kalshi"] = []
            mutate(raw["polymarket_intl"][0])
            fixtures, markets, warnings = S.parse_basketball(raw)
            self.assertEqual(fixtures, [])
            self.assertEqual(markets, [])
            self.assertTrue(any(w.startswith("COVERAGE_INCOMPLETE") for w in warnings))

    def test_closed_records_are_excluded_and_missing_start_is_incomplete(self):
        raw = flatten_samples()
        for row in raw["kalshi"]:
            row["status"] = "settled"
        raw["polymarket_intl"][0]["closed"] = True
        self.assertEqual(S.parse_basketball(raw), ([], [], []))
        raw = flatten_samples()
        row = raw["polymarket_intl"][0]
        row.pop("gameStartTime")
        row.pop("_event_start_time")
        fixtures, markets, warnings = S.parse_basketball(raw)
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(len(markets), 2)
        self.assertTrue(any("no scheduled UTC start" in warning for warning in warnings))


class NbaCollectorTests(unittest.TestCase):
    def responder(self, *, repeat_cursor=False, gamma_failed=False):
        def get(url, params=None, **kwargs):
            params = params or {}
            if url.endswith("/series/KXNBAGAME"):
                return 200, {"series": {"contract_terms_url": S.NBA_TERMS}}
            if url == S.K.KALSHI + "/events":
                if not params.get("cursor"):
                    return 200, {"events": [kalshi_event()], "cursor": "k-next"}
                return 200, {"events": [kalshi_event("20" if repeat_cursor else "21")],
                             "cursor": "k-next" if repeat_cursor else ""}
            if url.endswith("/teams"):
                return 200, gamma_event()["teams"]
            if url.endswith("/events/keyset"):
                if gamma_failed:
                    return 503, None
                if not params.get("after_cursor"):
                    event = gamma_event()
                    prop = dict(event["markets"][0], id="prop", slug=event["slug"] + "-player-points",
                                sportsMarketType="player_points")
                    event["markets"].append(prop)
                    future = {"id": "future", "slug": "nba-title-winner", "markets": []}
                    return 200, {"events": [event, future], "next_cursor": "p-next"}
                return 200, {"events": [gamma_event("21")], "next_cursor": None}
            raise AssertionError(url)
        return get

    def test_full_native_pagination_keeps_only_game_winners_and_snapshot_metadata(self):
        with patch.object(S.K.http, "get", side_effect=self.responder()) as get:
            raw, coverage, warnings = S.collect_basketball()
        self.assertEqual(warnings, [])
        self.assertTrue(all(item["complete"] for item in coverage.values()))
        self.assertEqual((len(raw["kalshi"]), len(raw["polymarket_intl"])), (4, 2))
        self.assertEqual(len(raw["_teams"]), 2)
        self.assertEqual(raw["_kalshi_series"][0]["contract_terms_url"], S.NBA_TERMS)
        self.assertTrue(any(call.args[1].get("cursor") == "k-next"
                            for call in get.call_args_list if len(call.args) > 1))
        self.assertTrue(any(call.args[1].get("after_cursor") == "p-next"
                            for call in get.call_args_list if len(call.args) > 1))
        fixtures, markets, warnings = S.parse_basketball(raw)
        self.assertEqual((len(fixtures), len(markets), warnings), (4, 8, []))

    def test_repeated_cursor_keeps_partial_records_but_never_claims_complete(self):
        with patch.object(S.K.http, "get", side_effect=self.responder(repeat_cursor=True)):
            raw, coverage, warnings = S.collect_basketball()
        self.assertEqual(len(raw["kalshi"]), 2)
        self.assertFalse(coverage["kalshi"]["complete"])
        self.assertTrue(coverage["polymarket_intl"]["complete"])
        self.assertTrue(any("pagination made no progress" in warning for warning in warnings))

    def test_failed_second_venue_preserves_first_venue_and_marks_only_failed_coverage(self):
        with patch.object(S.K.http, "get", side_effect=self.responder(gamma_failed=True)):
            raw, coverage, warnings = S.collect_basketball()
        self.assertEqual(len(raw["kalshi"]), 4)
        self.assertEqual(raw["polymarket_intl"], [])
        self.assertTrue(coverage["kalshi"]["complete"])
        self.assertFalse(coverage["polymarket_intl"]["complete"])
        self.assertTrue(warnings)

    def test_registry_exposes_two_venues_and_distinct_sport_adapters(self):
        self.assertEqual(set(S.ADAPTERS), {"cricket", "basketball"})
        self.assertEqual(S.ADAPTERS["basketball"].label, "Basketball (NBA)")
        self.assertTrue(all(adapter.venues == ("kalshi", "polymarket_intl")
                            for adapter in S.ADAPTERS.values()))

    def test_cricket_registry_requests_winner_scope_before_parsing_props(self):
        with patch.object(S.C, "parse_kalshi", return_value=([], [], [])) as kalshi, patch.object(
                S.C, "parse_polymarket", return_value=([], [], [])) as gamma:
            S.ADAPTERS["cricket"].parse({"kalshi": [], "polymarket_intl": []})
        kalshi.assert_called_once_with([], market_types={S.C.MATCH_WINNER})
        gamma.assert_called_once_with([], "polymarket_intl", market_types={S.C.MATCH_WINNER})


if __name__ == "__main__":
    unittest.main()
