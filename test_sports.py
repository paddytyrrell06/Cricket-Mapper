import copy
import unittest
from unittest.mock import patch
import json
from pathlib import Path

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


def configured_sports_raw(sport, *, include_kalshi=True, include_polymarket=True):
    config = json.loads((Path(S.__file__).parent / "config" /
                         S.SPORT_CONFIGS[sport]["config"]).read_text())
    teams = config["teams"][:2]
    left, right = teams
    league = config["league"]
    series = S.SPORT_CONFIGS[sport]["series"]
    date = "2026-10-20"
    start = "2026-10-20T23:00:00Z"
    codes = (left["code"], right["code"])
    names = (left["canonical"], right["canonical"])
    slug = f"{league}-{codes[0].lower()}-{codes[1].lower()}-{date}"
    raw = {"kalshi": [], "polymarket_intl": [],
           "_teams": [{"name": name, "abbreviation": code.lower(), "league": league}
                      for code, name in zip(codes, names)]}
    if include_kalshi:
        event = f"{series}-26OCT20{codes[0]}{codes[1]}"
        for code, name in zip(codes, names):
            ticker = f"{event}-{code}"
            raw["kalshi"].append({
                "ticker": ticker, "event_ticker": event,
                "market_type": "binary", "yes_sub_title": name,
                "title": f"{name} wins", "status": "active",
                "occurrence_datetime": start,
                "rules_primary": f"If {name} wins the {league.upper()} game, the market resolves Yes.",
                "rules_secondary": "If postponed but begins within 48 hours, remain open. Otherwise resolve to a fair price.",
            })
    if include_polymarket:
        outcomes = [teams[0]["aliases"][0], teams[1]["aliases"][0]]
        market = {
            "id": "winner-" + sport, "slug": slug,
            "question": " vs. ".join(outcomes),
            "sportsMarketType": "moneyline", "active": True, "closed": False,
            "outcomes": json.dumps(outcomes), "clobTokenIds": '["200", "100"]',
            "gameStartTime": start,
            "description": f"If {outcomes[0]} win, resolve {outcomes[0]}. If {outcomes[1]} win, resolve {outcomes[1]}. Overtime is included if played. The result includes any overtime periods and shootouts and extra innings.",
            "resolutionSource": config["source"],
        }
        raw["polymarket_intl"].append({
            **market, "_event_slug": slug, "_event_id": "event-" + sport,
            "_event_teams": raw["_teams"], "_event_start_time": start,
            "_native_league": league,
        })
    return raw


def configured_soccer_raw():
    competition = "epl"
    date = "2026-10-23"
    start = "2026-10-23T19:00:00Z"
    series = S.SOCCER_COMPETITIONS[competition]["series"]
    event = f"{series}-26OCT23IPSNFO"
    kalshi_rules = (
        "If Ipswich Town wins the Ipswich Town vs Nottingham Forest professional EPL soccer game "
        "originally scheduled for Oct 23, 2026 after 90 minutes plus stoppage time "
        "(does not include extra time or penalties), then the market resolves to Yes."
    )
    secondary = (
        'If the game ends in a tie, the market called "Tie" resolves to Yes. '
        "If the game is cancelled or rescheduled to over 48 hours away, the market will resolve to a fair price."
    )
    kalshi = []
    for code, name, title in (
        ("IPS", "Ipswich Town", "Ipswich Town wins"),
        ("NFO", "Nottingham Forest", "Nottingham Forest wins"),
        ("TIE", "Tie", "Tie is the result"),
    ):
        kalshi.append({
            "ticker": f"{event}-{code}", "event_ticker": event,
            "market_type": "binary", "yes_sub_title": name, "title": title,
            "status": "active", "occurrence_datetime": start,
            "rules_primary": kalshi_rules, "rules_secondary": secondary,
            "_queried_series": series, "_series_title": "English Premier League Game",
            "_soccer_competition": competition,
        })
    teams = [
        {"name": "Ipswich Town FC", "abbreviation": "ips", "league": competition,
         "alias": "Ipswich"},
        {"name": "Nottingham Forest FC", "abbreviation": "not", "league": competition,
         "alias": "Forest"},
    ]
    descriptions = {
        "ips": ("If Ipswich Town FC wins, this market will resolve to \"Yes\". "
                "Otherwise, this market will resolve to \"No\". "
                "This market refers only to the outcome within the first 90 minutes of regular play plus stoppage time."),
        "draw": ("If the game ends in a draw, this market will resolve to \"Yes\". "
                 "Otherwise, this market will resolve to \"No\". "
                 "This market refers only to the outcome within the first 90 minutes of regular play plus stoppage time."),
        "not": ("If Nottingham Forest FC wins, this market will resolve to \"Yes\". "
                "Otherwise, this market will resolve to \"No\". "
                "This market refers only to the outcome within the first 90 minutes of regular play plus stoppage time."),
    }
    gamma = []
    for suffix in ("ips", "draw", "not"):
        market_slug = f"epl-ips-not-{date}-{suffix}"
        gamma.append({
            "id": f"pm-{suffix}", "slug": market_slug,
            "_event_slug": f"epl-ips-not-{date}", "_event_id": "epl-event",
            "_event_teams": teams, "_event_start_time": start,
            "_native_league": competition, "_soccer_competition": competition,
            "gameStartTime": start, "sportsMarketType": "moneyline",
            "outcomes": '["Yes", "No"]', "clobTokenIds": '["100", "200"]',
            "question": descriptions[suffix].split(".")[0],
            "description": descriptions[suffix], "active": True, "closed": False,
        })
    return {"kalshi": kalshi, "polymarket_intl": gamma}


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


def native_match_raw(league="atp"):
    details = S.NATIVE_COMPETITIONS[league]
    pairs = {
        "atp": (("atmane", "Terence Atmane", "T. Atmane"), ("hijikata", "Rinky Hijikata", "R. Hijikata")),
        "wta": (("joint", "Maya Joint", "M. Joint"), ("sonmez", "Zeynep Sonmez", "Z. Sonmez")),
        "ufc": (("jasluc", "Jasmin Lucindo", ""), ("niccal", "Nicolle Caliari", "")),
        "cfb": (("nill", "Huskies", "Northern Illinois"), ("iowa", "Hawkeyes", "Iowa")),
    }
    players = pairs[league]
    teams = [{"abbreviation": code, "name": name, "alias": alias, "league": league}
             for code, name, alias in players]
    names = [alias if details.get("school_identity") else name for _, name, alias in players]
    slug = f"{league}-{players[0][0]}-{players[1][0]}-2026-10-20"
    start = "2026-10-20T18:00:00Z"
    event = details["series"] + "-26OCT20TEST"
    if details["sport"] == "tennis":
        description = f"If {names[0]} advances against {names[1]}, resolve to {names[0]}. If {names[1]} advances against {names[0]}, resolve to {names[1]}. "
        description += "If the match ends in a walkover, this market will resolve to 50-50. If the match begins but is not completed, and one player advances due to the opponent's retirement, default, or disqualification, this market will resolve to the player who advances."
        secondary = "If a walkover occurs (all before the match starts), resolve to a fair price. If postponed, remain open within two weeks."
    elif details["sport"] == "mma":
        description = "The market resolves to the officially declared winner. If the fight is declared a draw or technical draw, ruled a No Contest, not scored, canceled, this market will resolve 50-50."
        secondary = "If the fight ends in a draw/no contest, both markets will resolve to $0.50. If postponed but begins within 48 hours, remain open; otherwise resolve to a fair price."
    else:
        description = "The market resolves to the winner including any overtime periods. If the game ends in a tie, resolve 50-50."
        secondary = "If postponed but begins within 48 hours, remain open. Otherwise resolve to a fair price."
    kalshi = [{"ticker": f"{event}-{index}", "event_ticker": event,
               "market_type": "binary", "yes_sub_title": name, "title": name + " wins",
               "status": "active", "occurrence_datetime": start,
               "rules_primary": f"If {name} wins the full match, the market resolves Yes.",
               "rules_secondary": secondary, "_native_competition": league,
               "_settlement_documents": [S.KALSHI_DOCUMENTS[details["sport"]]]
                   if details["sport"] in S.KALSHI_DOCUMENTS else []}
              for index, name in enumerate(names)]
    gamma = {"id": "native-winner", "slug": slug, "_event_slug": slug,
             "_event_id": "native-event", "_event_teams": teams,
             "_event_start_time": start, "_native_competition": league,
             "sportsMarketType": "moneyline", "outcomes": json.dumps(names),
             "clobTokenIds": '["200","100"]', "gameStartTime": start,
             "description": description, "active": True, "closed": False}
    return {"kalshi": kalshi, "polymarket_intl": [gamma]}


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
    def test_native_winners_allow_normal_result_with_contingency_difference(self):
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
        self.assertEqual(len(market_accepted), 2)
        self.assertEqual(market_review, [])
        self.assertTrue(all(match.contingency_status == "different" for match in market_accepted))
        self.assertTrue(all("conditional" in match.reason for match in market_accepted))
        for name in ("Boston Celtics", "Detroit Pistons"):
            profiles = [m.extra["settlement"] for m in markets if m.outcome == name]
            comparison = settlement.compare_profiles(*profiles)
            self.assertEqual(comparison["status"], "compatible")
            self.assertEqual(comparison["contingency_status"], "different")

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
        self.assertEqual(fixture.note, "")

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
            if url == S.K.GAMMA + "/events":
                if gamma_failed:
                    return 503, None
                if not params.get("offset"):
                    event = gamma_event()
                    prop = dict(event["markets"][0], id="prop", slug=event["slug"] + "-player-points",
                                sportsMarketType="player_points")
                    event["markets"].append(prop)
                    future = {"id": "future", "slug": "nba-title-winner", "markets": []}
                    unrelated = [
                        {"id": f"future-{index}", "slug": f"nba-award-{index}", "markets": []}
                        for index in range(98)
                    ]
                    return 200, [event, future, *unrelated]
                return 200, [gamma_event("21")]
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
        self.assertTrue(any(call.args[1].get("offset") == 100
                            for call in get.call_args_list if len(call.args) > 1))
        fixtures, markets, warnings = S.parse_basketball(raw)
        self.assertEqual((len(fixtures), len(markets), warnings), (4, 8, []))

    def test_verified_series_collection_attaches_official_rules_for_each_sport(self):
        for sport in ("basketball", "football", "hockey", "baseball"):
            with self.subTest(sport=sport):
                series = S.SPORT_CONFIGS[sport]["series"]
                league = S.SPORT_CONFIGS[sport]["league"]
                event_id = f"{series}-26OCT20BOSDET"

                def respond(url, params=None, **kwargs):
                    if url.endswith("/series/" + series):
                        return 200, {"series": {"title": league.upper() + " Game",
                                                 "contract_terms_url": S.KALSHI_DOCUMENTS[sport]["url"]}}
                    if url == S.K.KALSHI + "/events":
                        return 200, {"events": [{
                            "event_ticker": event_id, "series_ticker": series,
                            "title": "Boston vs Detroit", "markets": [{
                                "ticker": event_id + "-BOS", "market_type": "binary",
                                "yes_sub_title": "Boston", "status": "active",
                                "occurrence_datetime": "2026-10-20T23:00:00Z",
                            }],
                        }]}
                    raise AssertionError(url)

                with patch.object(S.K.http, "get", side_effect=respond):
                    records, _, _, _, _ = S._collect_kalshi(
                        series=series, league=league,
                        document=S.KALSHI_DOCUMENTS[sport],
                    )
                self.assertTrue(records)
                self.assertTrue(all(record.get("_settlement_documents") for record in records))
                self.assertEqual(records[0]["_settlement_documents"][0]["url"],
                                 S.KALSHI_DOCUMENTS[sport]["url"])

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
        self.assertEqual(set(S.ADAPTERS), {"cricket", "soccer", "basketball", "football", "hockey", "baseball", "tennis", "mma"})
        self.assertEqual(S.ADAPTERS["basketball"].label, "Basketball")
        self.assertTrue(all(adapter.venues == ("kalshi", "polymarket_intl")
                            for adapter in S.ADAPTERS.values()))

    def test_new_team_sport_adapters_parse_only_exact_league_winners(self):
        for sport in ("football", "hockey", "baseball"):
            with self.subTest(sport=sport):
                raw = configured_sports_raw(sport)
                fixtures, markets, warnings = S.ADAPTERS[sport].parse(raw)
                self.assertEqual(warnings, [])
                self.assertEqual(len(fixtures), 2)
                self.assertEqual(len(markets), 4)
                self.assertEqual(fixtures[0].key.sport, sport)
                accepted, review, unmatched = M.match_fixtures(fixtures)
                self.assertEqual((len(accepted), len(review), len(unmatched)), (1, 0, 0))
                market_accepted, market_review, _ = M.match_markets(accepted, markets)
                self.assertEqual(len(market_accepted) + len(market_review), 2)

    def test_new_team_sport_parser_rejects_cross_league_team_metadata(self):
        raw = configured_sports_raw("football", include_kalshi=False)
        raw["polymarket_intl"][0]["_event_teams"][0]["league"] = "nba"
        fixtures, markets, warnings = S.ADAPTERS["football"].parse(raw)
        self.assertEqual(fixtures, [])
        self.assertEqual(markets, [])
        self.assertTrue(any("unresolved" in warning for warning in warnings))

    def test_soccer_gamma_collection_keeps_home_away_and_draw_but_not_props(self):
        slug = "epl-ips-not-2026-10-23"
        teams = [
            {"name": "Ipswich Town FC", "abbreviation": "ips", "league": "epl"},
            {"name": "Nottingham Forest FC", "abbreviation": "not", "league": "epl"},
        ]
        event = {
            "id": "event-epl", "slug": slug, "title": "Ipswich Town FC vs Nottingham Forest FC",
            "startTime": "2026-10-23T19:00:00Z", "teams": teams,
            "markets": [
                {"id": "home", "slug": slug + "-ips", "sportsMarketType": "moneyline",
                 "outcomes": '["Yes", "No"]', "active": True},
                {"id": "draw", "slug": slug + "-draw", "sportsMarketType": "moneyline",
                 "outcomes": '["Yes", "No"]', "active": True},
                {"id": "away", "slug": slug + "-not", "sportsMarketType": "moneyline",
                 "outcomes": '["Yes", "No"]', "active": True},
                {"id": "half", "slug": slug + "-halftime-result-ips", "sportsMarketType": "moneyline",
                 "outcomes": '["Yes", "No"]', "active": True},
            ],
        }
        with patch.object(S.K.http, "get", return_value=(200, [event])) as get:
            records, coverage, warnings = S._collect_gamma_series(
                league="epl", series_id=S.SOCCER_COMPETITIONS["epl"]["series_id"])
        self.assertTrue(coverage["complete"])
        self.assertEqual(warnings, [])
        self.assertEqual({record["id"] for record in records}, {"home", "draw", "away"})
        self.assertTrue(all(record["_event_slug"] == slug for record in records))
        self.assertEqual(get.call_args.args[1]["series_id"], 10188)

    def test_soccer_parser_maps_two_team_and_draw_markets_exactly(self):
        raw = configured_soccer_raw()
        fixtures, markets, warnings = S.ADAPTERS["soccer"].parse(raw)
        self.assertEqual(warnings, [])
        self.assertEqual(len(fixtures), 2)
        self.assertEqual(len(markets), 6)
        self.assertEqual({fixture.key.sport for fixture in fixtures}, {"soccer"})
        self.assertEqual({fixture.key.fmt for fixture in fixtures}, {"SOCCER"})
        pm_targets = {market.outcome for market in markets
                      if market.venue == "polymarket_intl"}
        self.assertEqual(pm_targets,
                         {"Ipswich Town FC", "Nottingham Forest FC", "draw"})
        self.assertTrue(all(market.extra.get("binary_target_market")
                            for market in markets if market.venue == "polymarket_intl"))
        accepted_fixtures, fixture_review, _ = M.match_fixtures(fixtures)
        self.assertEqual((len(accepted_fixtures), len(fixture_review)), (1, 0))
        accepted, review, unmatched = M.match_markets(accepted_fixtures, markets)
        self.assertEqual((len(accepted), len(review), len(unmatched)), (3, 0, 0))
        self.assertTrue(all(market.comparison_basis == "normal_completion"
                            for market in accepted))
        self.assertTrue(all(market.contingency_status != "aligned" for market in accepted))

    def test_soccer_parser_rejects_unknown_competition_team_and_scope(self):
        for mutate in (
            lambda raw: raw["polymarket_intl"][0]["_event_teams"][0].update(league="lal"),
            lambda raw: raw["polymarket_intl"][0].update(slug="epl-ips-not-2026-10-23-first-half-result-ips"),
            lambda raw: raw["polymarket_intl"][0].update(outcomes='["Over", "Under"]'),
        ):
            raw = configured_soccer_raw()
            raw["kalshi"] = []
            raw["polymarket_intl"] = raw["polymarket_intl"][:1]
            mutate(raw)
            fixtures, markets, warnings = S.ADAPTERS["soccer"].parse(raw)
            self.assertEqual(fixtures, [])
            self.assertEqual(markets, [])
            self.assertTrue(any(w.startswith("COVERAGE_INCOMPLETE") for w in warnings))

    def test_cricket_registry_requests_winner_scope_before_parsing_props(self):
        with patch.object(S.C, "parse_kalshi", return_value=([], [], [])) as kalshi, patch.object(
                S.C, "parse_polymarket", return_value=([], [], [])) as gamma:
            S.ADAPTERS["cricket"].parse({"kalshi": [], "polymarket_intl": []})
        kalshi.assert_called_once_with([], market_types={S.C.MATCH_WINNER})
        gamma.assert_called_once_with([], "polymarket_intl", market_types={S.C.MATCH_WINNER})

    def test_new_native_sports_preserve_exact_identities_tokens_and_contingencies(self):
        for league in ("atp", "wta", "ufc", "cfb"):
            with self.subTest(league=league):
                raw = native_match_raw(league)
                sport = S.NATIVE_COMPETITIONS[league]["sport"]
                fixtures, markets, warnings = S.ADAPTERS[sport].parse(raw)
                self.assertEqual(warnings, [])
                self.assertEqual((len(fixtures), len(markets)), (2, 4))
                self.assertEqual({fixture.key.fmt for fixture in fixtures}, {league.upper()})
                accepted, review, _ = M.match_fixtures(fixtures)
                self.assertEqual((len(accepted), len(review)), (1, 0))
                matched, review, _ = M.match_markets(accepted, markets)
                self.assertEqual((len(matched), len(review)), (2, 0))
                polymarket = [market for market in markets if market.venue == "polymarket_intl"]
                self.assertEqual(Q._intl_asset(polymarket[0], raw["polymarket_intl"][0]), "200")
                self.assertEqual(Q._intl_asset(polymarket[1], raw["polymarket_intl"][0]), "100")
                if sport == "tennis":
                    self.assertTrue(all(market.contingency_status == "different" for market in matched))

    def test_native_winner_rejects_wrong_league_metadata_and_period_props(self):
        for mutate in (
            lambda row: row["_event_teams"][0].update(league="wta"),
            lambda row: row.update(slug=row["slug"] + "-set-1"),
            lambda row: row.update(sportsMarketType="tennis_first_set_winner"),
            lambda row: row.update(outcomes='["Yes","No"]'),
        ):
            raw = native_match_raw()
            raw["kalshi"] = []
            mutate(raw["polymarket_intl"][0])
            fixtures, markets, warnings = S.ADAPTERS["tennis"].parse(raw)
            self.assertEqual((fixtures, markets), ([], []))
            self.assertTrue(any("COVERAGE_INCOMPLETE" in note for note in warnings))

    def test_college_mascot_cannot_establish_cross_venue_school_identity(self):
        raw = native_match_raw("cfb")
        raw["kalshi"][0].update(
            yes_sub_title="Huskies", title="Washington Huskies at Iowa",
            rules_primary="If Washington wins the full match, the market resolves Yes.")
        fixtures, markets, warnings = S.ADAPTERS["football"].parse(raw)
        self.assertEqual({fixture.venue for fixture in fixtures}, {"polymarket_intl"})
        self.assertEqual({market.venue for market in markets}, {"polymarket_intl"})
        self.assertTrue(any("unverified exact school identities" in note for note in warnings))

    def test_wnba_registry_handles_verified_native_code_aliases_and_gender(self):
        config = json.loads((Path(S.__file__).parent / "config" / "wnba_teams.json").read_text())
        registry = S.TeamRegistry(config=config)
        self.assertEqual(registry.resolve("Golden State", "GS"), "Golden State Valkyries")
        self.assertEqual(registry.resolve("Las Vegas", "LV"), "Las Vegas Aces")
        self.assertEqual(registry.resolve("Golden State", "GS", league="nba"), "")
        details = S.NATIVE_COMPETITIONS["wnba"]
        with patch.dict(S.SPORT_CONFIGS, {"wnba": dict(details, league="wnba")}):
            raw = configured_sports_raw("wnba")
        fixtures, markets, warnings = S.ADAPTERS["basketball"].parse(raw)
        self.assertEqual(warnings, [])
        self.assertEqual((len(fixtures), len(markets)), (2, 4))
        self.assertEqual({fixture.key.gender for fixture in fixtures}, {"W"})
        self.assertEqual({market.fixture_key.fmt for market in markets}, {"WNBA"})

    def test_prop_only_event_page_does_not_end_winner_discovery(self):
        props = [{"id": str(index), "slug": f"atp-a-b-2026-10-20-prop-{index}"}
                 for index in range(100)]
        raw = native_match_raw()
        record = raw["polymarket_intl"][0]
        event = {"id": "game", "slug": record["slug"], "teams": record["_event_teams"],
                 "startTime": record["gameStartTime"], "markets": [record]}
        with patch.object(S.K.http, "get", side_effect=[(200, props), (200, [event])]) as get:
            records, coverage, warnings = S._collect_gamma_series(league="atp", series_id=10365)
        self.assertEqual(len(records), 1)
        self.assertTrue(coverage["complete"])
        self.assertEqual(warnings, [])
        self.assertEqual(get.call_args.args[1]["offset"], 100)

    def test_selected_soccer_discovery_is_bounded_and_preserves_one_venue_failure(self):
        def kalshi(**kwargs):
            raise RuntimeError("temporary Kalshi outage")
        def gamma(**kwargs):
            return [], S._coverage(True, "series", 0, []), []
        with patch.object(S, "_collect_kalshi", side_effect=kalshi), patch.object(
                S, "_collect_gamma_series", side_effect=gamma), patch.object(
                S, "ThreadPoolExecutor", wraps=S.ThreadPoolExecutor) as pool:
            raw, coverage, warnings = S._collect_soccer(["uel", "col"])
        pool.assert_called_once_with(max_workers=3)
        self.assertEqual(raw["kalshi"], [])
        self.assertFalse(coverage["kalshi"]["complete"])
        self.assertTrue(coverage["polymarket_intl"]["complete"])
        self.assertEqual(set(coverage["polymarket_intl"]["competitions"]), {"uel", "col"})
        self.assertEqual(len(warnings), 2)
        with self.assertRaises(ValueError):
            S._collect_soccer(["unverified-league"])


if __name__ == "__main__":
    unittest.main()
