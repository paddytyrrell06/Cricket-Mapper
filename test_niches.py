import copy
import json
import unittest
from unittest.mock import patch

import matching as M
import niches as N
import quotes as Q
import settlement as S


K_RULES = (
    "The following market refers to the full match. "
    "If the relevant match is postponed and not started within 48 hours of its originally scheduled start time, "
    "the market will resolve to the fair market price. "
    "If the relevant match is cancelled before play begins, the market will resolve to the fair market price. "
    "If the relevant match is forfeited before any play occurs, the market will resolve to the fair market price. "
    "If the relevant match begins and is subsequently forfeited, the market will resolve according to the "
    "official result published by the governing body or tournament organizer."
)
P_RULES = (
    'This market will resolve to "FaZe" if FaZe win the match against MOUZ. '
    'This market will resolve to "MOUZ" if MOUZ win the match against FaZe. '
    "If the match is canceled (not played at all), ends in a tie, or is delayed beyond 7 days from the scheduled "
    "date without a winner determined, this market will resolve to 50-50. "
    "If the match begins but is not completed, and one team wins due to the opponent's forfeiture, "
    "disqualification, or walkover, this market will resolve to the team who wins. "
    "If the match ends in a forfeit, disqualification, or walkover (team withdraws before the start and the "
    "other wins automatically), this market will resolve to 50-50."
)


def esports_raw(league="cs2"):
    details = N.ESPORTS_COMPETITIONS[league]
    event = details["series"] + "-26OCT211400FAZEMOUZ"
    slug = league + "-faze-mouz-2026-10-21"
    raw = {"kalshi": [], "polymarket_intl": []}
    for code, name in (("FAZE", "FaZe"), ("MOUZ", "MOUZ")):
        raw["kalshi"].append({
            "ticker": event + "-" + code, "event_ticker": event,
            "market_type": "binary", "yes_sub_title": name,
            "title": name + " wins", "status": "active",
            "occurrence_datetime": "2026-10-21T18:00:00Z",
            "rules_primary": f"If {name} wins the match then the market resolves to Yes.",
            "rules_secondary": K_RULES, "_native_league": league,
            "_esports_competition": league, "_contract_terms_url": N.ESPORTS_TERMS,
            "_settlement_documents": [dict(N.ESPORTS_TIE_DOCUMENT)],
        })
    raw["polymarket_intl"].append({
        "id": "match-winner", "slug": slug, "_event_slug": slug,
        "_event_id": "event-1", "question": "FaZe vs. MOUZ (BO3)",
        "sportsMarketType": "moneyline", "active": True, "closed": False,
        "outcomes": '["MOUZ", "FaZe"]', "clobTokenIds": '["200", "100"]',
        "gameStartTime": "2026-10-21T18:00:00Z", "_native_league": league,
        "_esports_competition": league,
        "_event_teams": [
            {"name": "FaZe", "abbreviation": "faze", "league": "csgo" if league == "cs2" else league},
            {"name": "MOUZ", "abbreviation": "mouz", "league": "csgo" if league == "cs2" else league}],
        "description": P_RULES, "resolutionSource": "https://hltv.org",
    })
    return raw


class EsportsParserTests(unittest.TestCase):
    def test_full_series_share_canonical_identity_and_original_outcome_order(self):
        fixtures, markets, warnings = N.parse_esports(esports_raw())
        self.assertEqual(warnings, [])
        self.assertEqual(len(fixtures), 2)
        self.assertEqual(fixtures[0].key, fixtures[1].key)
        self.assertEqual(fixtures[0].key.sport, "esports")
        self.assertEqual(fixtures[0].key.fmt, "CS2")
        self.assertEqual([m.outcome for m in markets if m.venue == "polymarket_intl"], ["MOUZ", "FaZe"])
        accepted, review, _ = M.match_fixtures(fixtures)
        linked, needs_review, _ = M.match_markets(accepted, markets)
        self.assertEqual(review, [])
        self.assertEqual(needs_review, [])
        self.assertEqual(len(linked), 2)
        self.assertEqual(linked[0].contingency_status, "different")
        self.assertEqual(linked[0].comparison_basis, "normal_completion")

    def test_published_csgo_metadata_is_accepted_only_in_cs2(self):
        raw = esports_raw("lol")
        raw["polymarket_intl"][0]["_event_teams"][0]["league"] = "csgo"
        fixtures, _, warnings = N.parse_esports(raw)
        self.assertEqual([f.venue for f in fixtures], ["kalshi"])
        self.assertTrue(any("another game" in warning for warning in warnings))

    def test_websocket_manifest_uses_native_token_ids_in_published_outcome_order(self):
        raw = esports_raw()
        _, markets, _ = N.parse_esports(raw)
        manifest = Q.stream_instruments(markets, raw)
        subscriptions = {row["outcome"]: row["instrument_id"] for row in manifest
                         if row["venue"] == "polymarket_intl"}
        self.assertEqual(subscriptions, {"faze": "100", "mouz": "200"})

    def test_map_winner_never_enters_full_match_pipeline(self):
        raw = esports_raw()
        raw["polymarket_intl"][0]["slug"] += "-game1"
        raw["polymarket_intl"][0]["sportsMarketType"] = "child_moneyline"
        for row in raw["kalshi"]:
            row["rules_secondary"] = row["rules_secondary"].replace("the full match", "Map 1")
        fixtures, markets, warnings = N.parse_esports(raw)
        self.assertEqual(fixtures, [])
        self.assertEqual(markets, [])
        self.assertEqual(len(warnings), 2)

    def test_unknown_names_do_not_gain_fuzzy_aliases(self):
        raw = esports_raw()
        raw["kalshi"][0]["yes_sub_title"] = "FaZe Academy"
        fixtures, _, _ = N.parse_esports(raw)
        accepted, review, _ = M.match_fixtures(fixtures)
        self.assertEqual(accepted, [])
        self.assertTrue(review)
        self.assertNotEqual(fixtures[0].key, fixtures[1].key)

    def test_outcomes_must_agree_with_both_exact_published_names(self):
        raw = esports_raw()
        raw["polymarket_intl"][0]["outcomes"] = '["MOUZ", "FaZe Academy"]'
        fixtures, _, warnings = N.parse_esports(raw)
        self.assertEqual([f.venue for f in fixtures], ["kalshi"])
        self.assertTrue(any("outcomes disagree" in warning for warning in warnings))

    def test_games_keep_distinct_keys_even_for_same_teams_and_dates(self):
        cs2 = N.parse_esports(esports_raw())[0]
        lol = N.parse_esports(esports_raw("lol"))[0]
        self.assertNotEqual(cs2[0].key, lol[0].key)

    def test_closed_records_and_ambiguous_team_codes_are_excluded(self):
        raw = esports_raw()
        raw["polymarket_intl"][0]["_event_teams"].append(
            copy.deepcopy(raw["polymarket_intl"][0]["_event_teams"][0]))
        raw["kalshi"][0]["status"] = "finalized"
        fixtures, markets, warnings = N.parse_esports(raw)
        self.assertEqual(fixtures, [])
        self.assertEqual(markets, [])
        self.assertTrue(warnings)

    def test_exact_published_alias_can_resolve_a_venue_name(self):
        raw = esports_raw()
        raw["polymarket_intl"][0]["_event_teams"][0]["alias"] = "FaZe Clan"
        raw["kalshi"][0]["yes_sub_title"] = "FaZe Clan"
        fixtures, markets, _ = N.parse_esports(raw)
        self.assertEqual(fixtures[0].key, fixtures[1].key)
        self.assertEqual(markets[0].outcome, "FaZe")


class EsportsCollectionTests(unittest.TestCase):
    def test_lol_milestone_preserves_published_game_and_normalizes_lookup_league(self):
        raw = esports_raw("lol")
        event = raw["kalshi"][0]["event_ticker"]
        raw["_lol_milestones"] = [{
            "id": "native-1", "type": "esports_match", "start_date": "2026-10-21T18:00:00Z",
            "primary_event_tickers": [event], "related_event_tickers": [event],
            "details": {"main_game_event_ticker": event, "game": "League of Legends", "league": "League of Legends"},
        }]
        with patch("sports._collect_competitions", return_value=(raw, {}, [])) as collect:
            collected, _, _ = N.collect_esports(["lol"])
        self.assertEqual(set(collect.call_args.args[0]), {"lol"})
        milestone = collected["kalshi"][0]["_event_milestone"]
        self.assertEqual(milestone["published_details"]["league"], "League of Legends")
        self.assertEqual(milestone["details"]["league"], "lol")

    def test_unknown_competition_fails_before_network_access(self):
        with self.assertRaisesRegex(ValueError, "Unknown esports"):
            N.collect_esports(["invented"])


class EsportsSettlementTests(unittest.TestCase):
    def profiles(self):
        _, markets, _ = N.parse_esports(esports_raw())
        return markets[0].extra["settlement"], markets[2].extra["settlement"]

    def test_normal_winner_tie_match_but_prestart_forfeit_cannot_be_called_risk_free(self):
        kalshi, gamma = self.profiles()
        self.assertEqual(kalshi["payouts"]["tie"], "0.5")
        self.assertEqual(gamma["payouts"]["tie"], "0.5")
        self.assertEqual(kalshi["payouts"]["forfeit_before_start_win"], "fair_market_price")
        self.assertEqual(gamma["payouts"]["forfeit_before_start_win"], "0.5")
        self.assertEqual(kalshi["payouts"]["forfeit_after_start_win"], "1")
        self.assertEqual(gamma["payouts"]["forfeit_after_start_win"], "1")
        self.assertEqual(S.compare_profiles(kalshi, gamma)["status"], "compatible")
        self.assertEqual(S.compare_profiles(kalshi, gamma, strict=True)["status"], "incompatible")
        self.assertIsNone(kalshi["policies"]["overtime"])

    def test_tie_formula_needs_verified_document_and_two_participant_scope(self):
        raw = esports_raw()["kalshi"][0]
        for altered in ({**raw, "_esports_two_participant_contract": False},
                        {**raw, "_esports_two_participant_contract": True, "_settlement_documents": []}):
            profile = S.profile_market("kalshi", altered, sport="esports", outcome="FaZe")
            self.assertEqual(profile["core_status"], "unknown")
            self.assertIsNone(profile["payouts"]["tie"])

    def test_map_language_cannot_establish_full_series_period(self):
        raw = esports_raw()["polymarket_intl"][0]
        raw["description"] = raw["description"].replace("win the match against", "win Map 1 against")
        profile = S.profile_market("polymarket_intl", raw, sport="esports", outcome="FaZe")
        self.assertIsNone(profile["policies"]["period"])
        self.assertEqual(profile["core_status"], "unknown")

    def test_current_fourteen_day_deadline_remains_separate_from_older_seven_days(self):
        raw = esports_raw()["polymarket_intl"][0]
        raw["description"] = P_RULES.replace(
            ", or is delayed beyond 7 days from the scheduled date without a winner determined", "")
        raw["description"] += (
            " If the match is postponed, it must be rescheduled to a start time on or before November 4 "
            "(14 calendar days after the originally scheduled start). The rescheduled start time must be on "
            "or before the deadline. Otherwise, this market resolves 50-50.")
        profile = S.profile_market("polymarket_intl", raw, sport="esports", outcome="FaZe")
        self.assertEqual(profile["policies"]["postponement"],
                         "half_if_rescheduled_start_after_published_14_day_deadline")


if __name__ == "__main__":
    unittest.main()
