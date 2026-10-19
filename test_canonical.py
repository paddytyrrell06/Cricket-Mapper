import unittest

import canonical as C


def kalshi_pair(series, blob, teams, *, competition="", date="26OCT211330"):
    event = f"{series}-{date}{blob}"
    return [{
        "ticker": f"{event}-{code}",
        "event_ticker": event,
        "_queried_series": series,
        "_series_title": "Cricket Match",
        "_native_cricket_series": True,
        "yes_sub_title": name,
        "title": f"{name} wins",
        "rules_primary": (
            f"If {name} wins the {teams[0][1]} vs {teams[1][1]} men's "
            f"professional {competition} cricket match originally scheduled for "
            "Oct 21, 2026 at 1:30 PM EDT, then the market resolves to Yes."
        ),
    } for code, name in teams]


def pm_market(league, a, b, title, *, suffix="", outcomes=None):
    slug = f"cric{league}-{a}-{b}-2026-10-18"
    return {
        "slug": slug + (f"-{suffix}" if suffix else ""),
        "_event_title": title,
        "_event_start_time": "2026-10-18T08:00:00Z",
        "outcomes": outcomes if outcomes is not None else ["Yes", "No"],
    }


class KalshiParsingTests(unittest.TestCase):
    def test_long_event_codes_keep_both_names_and_outcomes(self):
        raw = kalshi_pair("KXT20MATCH", "MORRAPNJSMCAV", [
            ("MORRAP", "Morrisville Raptors"),
            ("NJSMCAV", "New Jersey Somerset Cavaliers"),
        ])
        fixtures, markets, warnings = C.parse_kalshi(raw)
        self.assertEqual(warnings, [])
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(fixtures[0].key.teams, {
            "morrisville raptors", "new jersey somerset cavaliers"})
        self.assertEqual({m.outcome for m in markets}, {
            "Morrisville Raptors", "New Jersey Somerset Cavaliers"})

    def test_hyphenated_outcome_uses_published_event_boundary(self):
        raw = kalshi_pair("KXT20MATCH", "ART-BALMEX", [
            ("ART-B", "Artech - B"), ("ALMEX", "Al Mulla Exchange"),
        ])
        fixtures, markets, warnings = C.parse_kalshi(raw)
        self.assertEqual(warnings, [])
        self.assertEqual(fixtures[0].extra["team_codes"], ["ALMEX", "ART-B"])
        self.assertEqual(fixtures[0].key.teams, {"artech b", "al mulla exchange"})
        self.assertEqual({m.extra["outcome_code"] for m in markets}, {"ART-B", "ALMEX"})

    def test_hyphens_in_both_team_codes_preserve_womens_identity(self):
        raw = kalshi_pair("KXWT20MATCH", "SS-WST-W", [
            ("SS-W", "Sydney Sixers Women"), ("ST-W", "Sydney Thunder Women"),
        ])
        fixtures, markets, warnings = C.parse_kalshi(raw)
        self.assertEqual(warnings, [])
        self.assertEqual(fixtures[0].key.gender, "W")
        self.assertEqual(fixtures[0].key.teams, {
            "sydney sixers women", "sydney thunder women"})
        self.assertEqual(len(markets), 2)

    def test_legacy_tickers_without_event_metadata_still_parse(self):
        raw = kalshi_pair("KXT20MATCH", "MORRAPNJSMCAV", [
            ("MORRAP", "Morrisville Raptors"),
            ("NJSMCAV", "New Jersey Somerset Cavaliers"),
        ])
        for market in raw:
            market.pop("event_ticker")
        fixtures, markets, warnings = C.parse_kalshi(raw)
        self.assertEqual(warnings, [])
        self.assertEqual(len(fixtures[0].key.teams), 2)
        self.assertEqual(len(markets), 2)

    def test_conflicting_event_boundary_stays_incomplete(self):
        raw = kalshi_pair("KXT20MATCH", "ART-BALMEX", [
            ("ART-B", "Artech - B"), ("ALMEX", "Al Mulla Exchange"),
        ])
        raw[0]["event_ticker"] = "KXT20MATCH-26OCT211330WRONG"
        fixtures, markets, warnings = C.parse_kalshi(raw[:1])
        self.assertEqual(fixtures, [])
        self.assertEqual(markets, [])
        self.assertTrue(any(w.startswith("COVERAGE_INCOMPLETE") for w in warnings))

    def test_sheffield_shield_is_a_first_class_match(self):
        raw = kalshi_pair("KXSSHIELDMATCH", "VICTAS", [
            ("VIC", "Victoria"), ("TAS", "Tasmania"),
        ], competition="Sheffield Shield")
        fixtures, markets, warnings = C.parse_kalshi(raw)
        self.assertEqual(warnings, [])
        self.assertEqual(fixtures[0].key.fmt, "FIRSTCLASS")
        self.assertEqual(fixtures[0].competition, "Sheffield Shield")
        self.assertEqual({m.market_type for m in markets}, {C.MATCH_WINNER})

    def test_draw_first_with_unequal_codes_keeps_one_complete_fixture_key(self):
        raw = kalshi_pair("KXTESTMATCH", "AUSSA", [
            ("AUS", "Australia"), ("SA", "South Africa"),
        ], competition="Test")
        draw = {**raw[0], "ticker": raw[0]["event_ticker"] + "-TIE",
                "yes_sub_title": "Draw/Tie", "title": "Draw/Tie wins"}
        fixtures, markets, warnings = C.parse_kalshi([draw, *raw])
        self.assertEqual(warnings, [])
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(fixtures[0].key.teams, {"australia", "south africa"})
        self.assertEqual(fixtures[0].note, "")
        self.assertEqual({m.fixture_key for m in markets}, {fixtures[0].key})
        self.assertEqual({m.outcome for m in markets}, {"Australia", "South Africa", "draw"})

    def test_odi_series_uses_its_published_competition(self):
        raw = kalshi_pair("KXODIMATCH", "ENGPAK", [
            ("ENG", "England"), ("PAK", "Pakistan"),
        ], competition="England and Sri Lanka Tour of Pakistan ODIs")
        fixtures, _, warnings = C.parse_kalshi(raw)
        self.assertEqual(warnings, [])
        self.assertEqual(fixtures[0].key.fmt, "LISTA")
        self.assertEqual(fixtures[0].competition,
                         "England and Sri Lanka Tour of Pakistan ODIs")

    def test_missing_odi_competition_does_not_invent_domestic_league(self):
        raw = kalshi_pair("KXODIMATCH", "ENGPAK", [
            ("ENG", "England"), ("PAK", "Pakistan"),
        ])
        for market in raw:
            market.pop("rules_primary")
        fixtures, _, _ = C.parse_kalshi(raw)
        self.assertEqual(fixtures[0].competition, "")

    def test_resolution_rules_and_profile_are_retained_on_each_market(self):
        raw = kalshi_pair("KXT20MATCH", "ENGPAK", [("ENG", "England"), ("PAK", "Pakistan")])
        raw[0]["rules_secondary"] = "If no result, the market resolves to a fair price."
        _, markets, _ = C.parse_kalshi(raw)
        self.assertEqual(markets[0].extra["resolution_evidence"]["rules_primary"], raw[0]["rules_primary"])
        self.assertEqual(markets[0].extra["resolution_evidence"]["rules_secondary"], raw[0]["rules_secondary"])
        self.assertEqual(markets[0].extra["settlement"]["status"], "unknown")


class PolymarketParsingTests(unittest.TestCase):
    def test_three_way_winner_slugs_resolve_team_and_draw_targets(self):
        raw = [pm_market("int", "zaf", "aus", "Test Match: South Africa vs Australia",
                         suffix=suffix) for suffix in ("zaf", "aus", "draw")]
        fixtures, markets, warnings = C.parse_polymarket(raw, "polymarket_intl")
        self.assertEqual(warnings, [])
        self.assertEqual(fixtures[0].key.teams, {"south africa", "australia"})
        self.assertEqual({m.market_type for m in markets}, {C.MATCH_WINNER})
        self.assertEqual({m.outcome for m in markets}, {"South Africa", "Australia", "draw"})
        self.assertTrue(all(m.extra["binary_target_market"] for m in markets))

    def test_known_first_class_leagues_have_complete_metadata(self):
        for league, title, a, b, competition in [
            ("shield", "Sheffield Shield: Western Australia vs New South Wales Blues",
             "wea", "blu", "Sheffield Shield"),
            ("ranji", "Ranji Trophy: Karnataka vs Saurashtra", "kar", "sau", "Ranji Trophy"),
        ]:
            with self.subTest(league=league):
                fixtures, markets, warnings = C.parse_polymarket([
                    pm_market(league, a, b, title, suffix=a),
                    pm_market(league, a, b, title, suffix=b),
                ], "polymarket_intl")
                self.assertEqual(warnings, [])
                self.assertEqual(fixtures[0].competition, competition)
                self.assertEqual(fixtures[0].key.fmt, "FIRSTCLASS")
                self.assertEqual({m.market_type for m in markets}, {C.MATCH_WINNER})
                self.assertEqual({C.norm_team(m.outcome) for m in markets}, fixtures[0].key.teams)

    def test_tour_titles_use_plural_format_labels(self):
        for title, fmt in [
            ("New Zealand Women Tour of Australia T20s: Australia Women vs New Zealand Women", "T20"),
            ("England Tour of Pakistan ODIs: Pakistan Women vs England Women", "LISTA"),
        ]:
            with self.subTest(fmt=fmt):
                fixtures, _, warnings = C.parse_polymarket([
                    pm_market("int", "a", "b", title,
                              outcomes=C._teams_from_title(title)),
                ], "polymarket_intl")
                self.assertEqual(warnings, [])
                self.assertEqual(fixtures[0].key.fmt, fmt)
                self.assertEqual(fixtures[0].key.gender, "W")
                self.assertTrue(all("women" in name for name in fixtures[0].key.teams))

    def test_two_outcome_match_keeps_both_published_sides(self):
        fixtures, markets, warnings = C.parse_polymarket([
            pm_market("ipl", "rcb", "mi", "Royal Challengers Bengaluru vs Mumbai Indians",
                      outcomes=["Royal Challengers Bengaluru", "Mumbai Indians"]),
        ], "polymarket_intl")
        self.assertEqual(warnings, [])
        self.assertEqual(len(markets), 2)
        self.assertEqual({C.norm_team(m.outcome) for m in markets}, fixtures[0].key.teams)

    def test_unrecognized_suffix_is_not_guessed_as_a_winner(self):
        _, markets, warnings = C.parse_polymarket([
            pm_market("int", "zaf", "aus", "Test Match: South Africa vs Australia",
                      suffix="match-to-day-3"),
        ], "polymarket_intl")
        self.assertEqual({m.market_type for m in markets}, {C.UNKNOWN})
        self.assertTrue(all("unmapped suffix" in m.note for m in markets))
        self.assertTrue(any(w.startswith("COVERAGE_INCOMPLETE") for w in warnings))

    def test_unrecognized_league_still_marks_coverage_incomplete(self):
        _, _, warnings = C.parse_polymarket([
            pm_market("unmapped", "a", "b", "Unknown T20: Team A vs Team B"),
        ], "polymarket_intl")
        self.assertTrue(any(w.startswith("COVERAGE_INCOMPLETE") for w in warnings))

    def test_match_winner_scope_skips_known_props_but_keeps_unknown_semantics_visible(self):
        raw = [pm_market("int", "zaf", "aus", "Test Match: South Africa vs Australia",
                         suffix=suffix) for suffix in ("zaf", "aus", "draw", "match-to-day-3", "toss-winner")]
        _, markets, warnings = C.parse_polymarket(raw, "polymarket_intl", market_types={C.MATCH_WINNER})
        self.assertEqual(len(markets), 3)
        self.assertEqual({market.market_type for market in markets}, {C.MATCH_WINNER})
        self.assertFalse(any(w.startswith("COVERAGE_INCOMPLETE") for w in warnings))
        self.assertTrue(any("2 markets outside" in w for w in warnings))
        raw.append(pm_market("int", "zaf", "aus", "Test Match: South Africa vs Australia",
                             suffix="undocumented-winner-contract"))
        _, markets, warnings = C.parse_polymarket(raw, "polymarket_intl", market_types={C.MATCH_WINNER})
        self.assertTrue(any(w.startswith("COVERAGE_INCOMPLETE") for w in warnings))

    def test_fixture_sport_is_part_of_identity_with_cricket_backward_compatibility(self):
        cricket = C.FixtureKey("2026-10-20", frozenset({"a", "b"}), "M", "T20")
        basketball = C.FixtureKey("2026-10-20", frozenset({"a", "b"}), "M", "T20", sport="basketball")
        self.assertEqual(cricket.sport, "cricket")
        self.assertNotEqual(cricket, basketball)
        self.assertTrue(basketball.pretty().startswith("basketball|"))


if __name__ == "__main__":
    unittest.main()
