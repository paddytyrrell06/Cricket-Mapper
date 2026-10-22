import copy
import json
import unittest

from betfair_mapping import map_markets
from test_sports import gamma_event


def betfair_market():
    return {"market_id": "1.234", "market_name": "Match Odds", "event_id": "bf-event",
            "event_type": {"id": "7522", "name": "Basketball"}, "sport": "basketball",
            "league": "NBA", "competition": {"id": "nba-id", "name": "NBA"},
            "start_time": "2026-10-20T23:00:00Z", "market_type": "MATCH_ODDS",
            "rules": "The winner of the full game wins. Overtime is included.",
            "runners": [{"selection_id": "11", "runner_name": "Boston Celtics", "handicap": "0"},
                        {"selection_id": "22", "runner_name": "Detroit Pistons", "handicap": "0"}]}


def raw_market(day="20"):
    event = gamma_event(day)
    return {**event["markets"][0], "conditionId": "0x-condition-" + day,
            "_event_slug": event["slug"], "_event_id": event["id"],
            "_event_start_time": event["startTime"], "_event_teams": event["teams"]}


def runtime_candidates():
    rows, assets = [], {}
    for team, token in (("Boston Celtics", "100"), ("Detroit Pistons", "200")):
        rows.append({"sport": "basketball", "competition": "NBA", "fixture_date": "2026-10-20",
            "start_time_utc": "23:00", "canonical_fixture_id": "nba-game-20", "team_a": "Boston Celtics",
            "team_b": "Detroit Pistons", "canonical_market_type": "match_winner", "canonical_outcome": team.lower(),
            "polymarket_intl_status": "Available", "polymarket_intl_market_id": "winner-20",
            "polymarket_intl_event_id": "event-20", "polymarket_intl_settlement_payouts": '{"win":"1","loss":"0"}',
            "polymarket_intl_settlement_policies": '{"period":"full_match","overtime":"included"}',
            "polymarket_intl_settlement_evidence": '[{"field":"description","text":"Full game winner including overtime"}]'})
        assets[("polymarket_intl", "winner-20", team.lower())] = token
    return rows, assets


class BetfairMappingTests(unittest.TestCase):
    def test_native_unmatched_polymarket_games_use_exact_reversed_tokens_and_condition_ids(self):
        result = map_markets([betfair_market()], [], {}, [raw_market()])
        self.assertEqual([item["pm_asset_id"] for item in result], ["100", "200"])
        self.assertEqual({item["pm_condition_id"] for item in result}, {"0x-condition-20"})
        self.assertEqual({item["state"] for item in result}, {"fixture_linked"})
        self.assertEqual({item["normal_result_status"] for item in result}, {"aligned"})
        self.assertEqual({item["settlement_status"] for item in result}, {"unverified"})
        self.assertTrue(all(not item["arbitrage_eligible"] for item in result))

    def test_existing_runtime_rows_need_no_kalshi_pair_and_no_yes_no_inference(self):
        rows, assets = runtime_candidates()
        result = map_markets([betfair_market()], rows[:1], assets)
        self.assertEqual(result[0]["state"], "fixture_linked")
        self.assertIsNone(result[0]["pm_condition_id"])
        self.assertEqual(result[1]["state"], "unmatched")
        self.assertIsNone(result[1]["pm_asset_id"])
        self.assertIn("no complementary token inferred", result[1]["reason"])

    def test_exact_league_scoped_approved_aliases_and_codes(self):
        market = betfair_market()
        for names in (("Celtics", "Pistons"), ("BOS", "DET"), ("Boston", "Detroit")):
            for runner, name in zip(market["runners"], names):
                runner["runner_name"] = name
            result = map_markets([market], [], raw_markets=[raw_market()])
            self.assertEqual([item["pm_asset_id"] for item in result], ["100", "200"])
        market["runners"][0]["runner_name"] = "Boston Celtic"
        self.assertEqual({item["state"] for item in map_markets([market], [], raw_markets=[raw_market()])}, {"rejected"})

    def test_wrong_league_unknown_competition_half_market_and_handicap_are_rejected(self):
        for change in (
                lambda market: market.update(league="WNBA"),
                lambda market: market.update(competition={"name": "NBA Summer League"}),
                lambda market: market.update(market_type="HALF_TIME"),
                lambda market: market.update(market_name="First Half Match Odds"),
                lambda market: market["runners"][0].update(handicap="1")):
            market = betfair_market()
            change(market)
            self.assertEqual({item["state"] for item in map_markets([market], [], raw_markets=[raw_market()])}, {"rejected"})

    def test_utc_tolerance_is_bounded_and_no_date_only_or_naive_time_matches(self):
        market = betfair_market()
        market["start_time"] = "2026-10-21T01:15:00+02:00"
        self.assertEqual({item["state"] for item in map_markets([market], [], raw_markets=[raw_market()])}, {"fixture_linked"})
        market["start_time"] = "2026-10-20T23:15:01Z"
        self.assertEqual({item["state"] for item in map_markets([market], [], raw_markets=[raw_market()])}, {"unmatched"})
        for invalid in ("2026-10-20T23:00:00", "2026-10-20", "invalid"):
            market["start_time"] = invalid
            self.assertEqual({item["state"] for item in map_markets([market], [], raw_markets=[raw_market()])}, {"rejected"})
        with self.assertRaises(ValueError):
            map_markets([], [], max_start_delta_seconds=3600)

    def test_missing_rules_never_count_as_evidence_and_explicit_overtime_conflict_is_rejected(self):
        market = betfair_market()
        market["rules"] = ""
        result = map_markets([market], [], raw_markets=[raw_market()])
        self.assertEqual({item["state"] for item in result}, {"fixture_linked"})
        self.assertEqual({item["normal_result_status"] for item in result}, {"unknown"})
        market["rules"] = "The winner wins. Regulation time only; excluding overtime."
        result = map_markets([market], [], raw_markets=[raw_market()])
        self.assertEqual({item["state"] for item in result}, {"rejected"})
        self.assertEqual({item["settlement_status"] for item in result}, {"incompatible"})

    def test_rematches_and_multiple_close_fixtures_never_use_nearest_candidate_guess(self):
        other = raw_market()
        other.update(id="other-game", gameStartTime="2026-10-20T23:10:00Z", _event_start_time="2026-10-20T23:10:00Z")
        result = map_markets([betfair_market()], [], raw_markets=[raw_market(), other])
        self.assertEqual({item["state"] for item in result}, {"ambiguous"})
        self.assertTrue(all(item["pm_asset_id"] is None for item in result))
        result = map_markets([betfair_market()], [], raw_markets=[raw_market(), raw_market("21")])
        self.assertEqual({item["state"] for item in result}, {"fixture_linked"})

    def test_duplicate_native_selection_ids_and_competing_betfair_events_are_not_linked(self):
        market = betfair_market()
        market["runners"][1]["selection_id"] = "11"
        result = map_markets([market], [], raw_markets=[raw_market()])
        self.assertEqual(len(result), 2)
        self.assertEqual({item["state"] for item in result}, {"rejected"})
        other = betfair_market()
        other.update(market_id="1.other", event_id="another-event")
        result = map_markets([betfair_market(), other], [], raw_markets=[raw_market()])
        self.assertEqual({item["state"] for item in result}, {"ambiguous"})
        unique = map_markets([betfair_market()], [], raw_markets=[raw_market()])
        self.assertNotEqual(result[0]["fingerprint"], unique[0]["fingerprint"])

    def test_conflicting_native_times_tokens_and_metadata_are_not_overridden_by_runtime(self):
        rows, assets = runtime_candidates()
        native = raw_market()
        native["_event_start_time"] = "2026-10-21T05:00:00Z"
        self.assertEqual({item["state"] for item in map_markets([betfair_market()], rows, assets, [native])}, {"unmatched"})
        native = raw_market()
        native["clobTokenIds"] = '["100","100"]'
        self.assertEqual({item["state"] for item in map_markets([betfair_market()], rows, assets, [native])}, {"unmatched"})
        native = raw_market()
        changed = copy.deepcopy(native)
        changed["description"] += " Different settlement rule."
        self.assertEqual({item["state"] for item in map_markets([betfair_market()], rows, assets, [native, changed])}, {"unmatched"})

    def test_changed_rules_and_start_change_fingerprint_but_book_updates_do_not(self):
        market = betfair_market()
        original = map_markets([market], [], raw_markets=[raw_market()])[0]
        market["runners"][0].update(back=[{"odds": "2.0", "size": "90"}], observed_at="later")
        updated = map_markets([market], [], raw_markets=[raw_market()])[0]
        self.assertEqual(original["fingerprint"], updated["fingerprint"])
        self.assertEqual(original["mapping_id"], updated["mapping_id"])
        market["rules"] += " Cancellation rules changed."
        self.assertNotEqual(original["fingerprint"], map_markets([market], [], raw_markets=[raw_market()])[0]["fingerprint"])
        native = raw_market()
        native["description"] += " New cancellation clause."
        self.assertNotEqual(original["fingerprint"], map_markets([betfair_market()], [], raw_markets=[native])[0]["fingerprint"])
        market["start_time"] = "2026-10-20T23:01:00Z"
        self.assertNotEqual(updated["fingerprint"], map_markets([market], [], raw_markets=[raw_market()])[0]["fingerprint"])
        json.dumps(original)  # Mapping is directly persistable without custom encoders.


if __name__ == "__main__":
    unittest.main()
