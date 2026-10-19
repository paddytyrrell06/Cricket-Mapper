# This file holds the shared cricket terms, competitions, team aliases, series identifiers, and conservative classification helpers used during market discovery.

from __future__ import annotations

from typing import Optional

import re
import unicodedata


RECON_REVIEW = [
    "Polymarket International discovery is incomplete: /public-search is capped "
    "and the code does not paginate its page parameter. Primary discovery should "
    "use /events/keyset with tag_slug=cricket, return each next_cursor as "
    "after_cursor, and use public-search only if that native request fails.",

    "Polymarket International should also call /teams and retain name, league, "
    "abbreviation and alias. Still dynamically extract all participants from "
    "events because /teams may not cover every obscure/local side.",

    "Polymarket US uses the wrong host. Public reads belong on "
    "https://gateway.polymarket.us, not https://api.polymarket.us. The current "
    "candidate list therefore produces a false zero.",

    "Kalshi should use the official public base "
    "https://external-api.kalshi.com/trade-api/v2. Discover cricket using "
    "/search/filters_by_sport and exact series/event metadata, not substring "
    "matching across every sports ticker.",

    "cricket_score() has severe substring false positives. Examples: 'odi' can "
    "match inside unrelated words; short strings such as cpl/mlc/list a can also "
    "collide. Use normalized token/phrase boundaries.",

    "COUNTRY_TERMS currently qualifies a market whenever a cricket country is "
    "present and no NOISE_TERM is found. That admits politics and other unrelated "
    "markets. A country must never be sufficient cricket evidence by itself.",

    "Kalshi output confirms the false-positive problem: 384 of 519 retained rows "
    "were already labelled noisy, including MLB first-innings markets.",

    "Polymarket ask extraction is wrong for named outcomes. The code assigns "
    "bestAsk only when outcome == 'Yes', but cricket markets commonly expose team "
    "names as outcomes. Parse clobTokenIds alongside outcomes and query the CLOB "
    "BBO/book/price for each outcome token.",

    "Kalshi prices are coming back None. Support current fixed-point/dollar fields "
    "such as yes_ask_dollars and yes_bid_dollars, with legacy yes_ask/yes_bid only "
    "as fallback.",

    "Preserving full raw JSON, request logs, timestamps, retries and cursor "
    "pagination is good and should remain.",
]


API_ENDPOINTS = {
    "polymarket_intl": {
        "cricket_tag": "https://gamma-api.polymarket.com/tags/slug/cricket",
        "teams": "https://gamma-api.polymarket.com/teams",
        "cricket_events_keyset": (
            "https://gamma-api.polymarket.com/events/keyset"
            "?tag_slug=cricket&closed=false&limit=1"
        ),
        "markets_keyset": "https://gamma-api.polymarket.com/markets/keyset",
        "public_search_fallback": "https://gamma-api.polymarket.com/public-search",
        "clob_book": "https://clob.polymarket.com/book",
        "clob_price": "https://clob.polymarket.com/price",
    },
    "polymarket_us": {
        "base": "https://gateway.polymarket.us",
        "markets": "https://gateway.polymarket.us/v1/markets",
        "events": "https://gateway.polymarket.us/v1/events",
        "series": "https://gateway.polymarket.us/v1/series",
        "search": "https://gateway.polymarket.us/v1/search",
        "market_bbo_template": "https://gateway.polymarket.us/v1/markets/{slug}/bbo",
        "market_book_template": "https://gateway.polymarket.us/v1/markets/{slug}/book",
    },
    "kalshi": {
        "base": "https://external-api.kalshi.com/trade-api/v2",
        "sports_filters": "https://external-api.kalshi.com/trade-api/v2/search/filters_by_sport",
        "category_tags": "https://external-api.kalshi.com/trade-api/v2/search/tags_by_categories",
        "series": "https://external-api.kalshi.com/trade-api/v2/series",
        "events": "https://external-api.kalshi.com/trade-api/v2/events",
        "markets": "https://external-api.kalshi.com/trade-api/v2/markets",
    },
}


POLYMARKET_SEARCH_QUERIES = [
    "cricket",
    "cricket match",
    "cricket series",
    "cricket tournament",
    "men's cricket",
    "women's cricket",
    "under 19 cricket",
    "u19 cricket",
    "test cricket",
    "test match",
    "test series",
    "world test championship",
    "ashes cricket",
    "odi cricket",
    "one day cricket",
    "one-day cricket",
    "odi series",
    "t20 cricket",
    "t20i cricket",
    "twenty20 cricket",
    "t20 series",
    "t10 cricket",
    "hundred cricket",
    "hundred-ball cricket",
    "cricket world cup",
    "t20 world cup",
    "women's cricket world cup",
    "women's t20 world cup",
    "u19 world cup cricket",
    "champions trophy cricket",
    "world cup qualifier cricket",
    "cricket league 2",
    "cricket challenge league",
    "asia cup cricket",
    "ipl cricket",
    "indian premier league",
    "wpl cricket",
    "women's premier league cricket",
    "psl cricket",
    "pakistan super league",
    "bbl cricket",
    "big bash cricket",
    "wbbl cricket",
    "sa20 cricket",
    "cpl cricket",
    "caribbean premier league cricket",
    "ilt20 cricket",
    "international league t20",
    "mlc cricket",
    "major league cricket",
    "the hundred cricket",
    "t20 blast cricket",
    "county championship cricket",
    "one day cup cricket",
    "ranji trophy cricket",
    "super smash cricket",
    "lanka premier league",
    "bangladesh premier league cricket",
    "global t20 canada cricket",
    "european cricket series",
    "european cricket league",
    "toss winner cricket",
    "completed match cricket",
    "most sixes cricket",
    "team total cricket",
]


CRICKET_FORMAT_TERMS = [
    "test",
    "test match",
    "test series",
    "first-class",
    "first class",
    "four-day",
    "4-day",
    "odi",
    "one day international",
    "one-day international",
    "one day",
    "one-day",
    "list a",
    "t20",
    "t20i",
    "twenty20",
    "twenty 20",
    "twenty-20",
    "t10",
    "hundred",
    "hundred-ball",
    "100-ball",
    "sixty",
    "6ixty",
    "six-a-side",
    "sixes",
    "tri-series",
    "tri series",
    "quadrangular",
    "tour match",
    "warm-up",
    "warmup",
    "qualifier",
    "regional qualifier",
    "sub-regional qualifier",
]


CRICKET_STRONG_PHRASES = [
    "cricket",
    "test match",
    "test series",
    "world test championship",
    "one day international",
    "one-day international",
    "odi series",
    "t20 international",
    "t20i",
    "twenty20",
    "t20 world cup",
    "cricket world cup",
    "champions trophy",
    "wicket",
    "wickets",
    "powerplay",
    "super over",
    "toss winner",
    "coin toss",
    "top batsman",
    "top bowler",
    "most sixes",
    "most fours",
]


CRICKET_WEAK_TOKENS = [
    "odi",
    "t20",
    "t10",
    "ashes",
    "ipl",
    "wpl",
    "psl",
    "bbl",
    "wbbl",
    "cpl",
    "wcpl",
    "mlc",
    "ilt20",
    "sa20",
    "ecs",
    "ecl",
    "ecc",
    "wtc",
]


NON_CRICKET_NOISE_TERMS = [
    "election",
    "president",
    "prime minister",
    "parliament",
    "inflation",
    "cpi",
    "gdp",
    "interest rate",
    "rate decision",
    "bitcoin",
    "ethereum",
    "crypto price",
    "nba",
    "nfl",
    "nhl",
    "mlb",
    "baseball",
    "soccer",
    "football",
    "rugby",
    "netball",
    "tennis",
    "ufc",
    "mma",
    "formula 1",
    "f1 race",
    "olympic",
]


OBSERVED_POLYMARKET_COMPETITIONS_2026_07_31 = ['Bangladesh T20 Series', 'Big Bash League', 'Cricket', 'Cricket World Cup', 'ECS England', 'ICC Champions Trophy Final', 'ICC Cricket World Cup League Two', 'IPL Finals', 'India ODI Series', 'India T20 Series', 'International ODI', 'Kuwait Kerala Premier League T20', 'Maharani Trophy KSCA T20', 'Major League Cricket', 'Mukono Super Smash T20', 'New Zealand ODI Series', 'ODI Series Australia vs India, Women', 'ODI Series Bangladesh vs Australia', 'ODI Series Bangladesh vs New Zealand', 'ODI Series England vs India', 'ODI Series India vs Afghanistan', 'ODI Series Ireland vs West Indies, Women', 'ODI Series Pakistan vs Zimbabwe, Women', 'ODI Series Sri Lanka vs Pakistan, Women', 'ODI Series West Indies vs Sri Lanka', 'ODI Series West Indies vs. New Zealand', 'ODI Series Zimbabwe vs Bangladesh', 'ODi Series South Africa vs Pakistan, Women', 'Pakistan ODI Series', 'Ranji Trophy', 'SA20', 'Shpageeza Cricket League', 'T20 Asia Pacific Champions Trophy', 'T20 Blast', 'T20 Blast, Women', 'T20 Series England vs India', 'T20 World Cup', 'U19 World Cup', "Women's Cricket World Cup Final Stage", "Women's Cricket World Cup Group Stage", "Women's T20 World Cup Qualifier", 'World Test Championship']


CRICKET_COMPETITIONS = {
    "international_global": [
        "International",
        "Test Series",
        "ODI Series",
        "T20 Series",
        "T20I Series",
        "T20 Tri-Series",
        "ODI Tri-Series",
        "The Ashes",
        "Women's Ashes",
        "Border-Gavaskar Trophy",
        "World Test Championship",
        "ICC World Test Championship",
        "ICC Cricket World Cup",
        "Cricket World Cup",
        "ICC Women's Cricket World Cup",
        "Women's Cricket World Cup",
        "ICC Men's T20 World Cup",
        "T20 World Cup",
        "ICC Women's T20 World Cup",
        "Women's T20 World Cup",
        "ICC Champions Trophy",
        "Champions Trophy",
        "ICC Cricket World Cup League 2",
        "ICC Cricket World Cup League Two",
        "ICC CWC League 2",
        "ICC CWC Challenge League",
        "ICC Cricket World Cup Qualifier",
        "ICC Women's Cricket World Cup Qualifier",
        "ICC T20 World Cup Qualifier",
        "ICC Women's T20 World Cup Qualifier",
        "T20 World Cup Global Qualifier",
        "T20 World Cup Regional Qualifier",
        "T20 World Cup Sub-Regional Qualifier",
        "T20 World Cup Europe Qualifier",
        "T20 World Cup Africa Qualifier",
        "T20 World Cup Asia Qualifier",
        "T20 World Cup Americas Qualifier",
        "T20 World Cup East Asia Pacific Qualifier",
        "T20 World Cup EAP Qualifier",
        "Women's T20 World Cup Europe Qualifier",
        "Women's T20 World Cup Africa Qualifier",
        "Women's T20 World Cup Asia Qualifier",
        "Women's T20 World Cup Americas Qualifier",
        "Women's T20 World Cup EAP Qualifier",
        "ICC Women's Championship",
        "Under-19 Cricket World Cup",
        "U19 Cricket World Cup",
        "ICC Under-19 Men's Cricket World Cup",
        "ICC Under-19 Women's T20 World Cup",
        "U19 Women's T20 World Cup",
        "Hong Kong Sixes",
        "Commonwealth Games Cricket",
        "African Games Cricket",
        "Pacific Games Cricket",
    ],
    "regional_international": [
        "Asia Cup",
        "ACC Asia Cup",
        "Women's Asia Cup",
        "ACC Women's Asia Cup",
        "ACC Premier Cup",
        "ACC Emerging Teams Asia Cup",
        "Emerging Teams Asia Cup",
        "T20 Asia Pacific Champions Trophy",
        "Gulf Cricket T20 Championship",
        "Gulf T20I Championship",
        "Nordic T20 Cup",
        "Balkan Cup",
        "Continental Cup",
        "Valletta Cup",
        "Central Europe Cup",
        "East Asia Cup",
        "South American Cricket Championship",
        "Africa T20 Cup",
    ],
    "india": [
        "Indian Premier League",
        "IPL",
        "IPL Finals",
        "Women's Premier League",
        "WPL",
        "Ranji Trophy",
        "Vijay Hazare Trophy",
        "Syed Mushtaq Ali Trophy",
        "SMAT",
        "Duleep Trophy",
        "Deodhar Trophy",
        "Irani Cup",
        "Tamil Nadu Premier League",
        "TNPL",
        "Maharaja Trophy KSCA T20",
        "Maharaja T20 Trophy",
        "Maharani Trophy KSCA T20",
        "Karnataka Premier League",
        "KSCA T20",
        "Bengal Pro T20 League",
        "UP T20 League",
        "Uttar Pradesh T20 League",
        "Delhi Premier League",
        "Rajasthan Premier League",
        "Andhra Premier League",
        "Saurashtra Premier League",
        "Sher-e-Punjab T20 Cup",
        "Kerala Cricket League",
        "Pondicherry T10",
        "Pondicherry T20",
        "Mumbai T20 League",
        "Baroda T20",
        "Assam Premier Club Championship",
        "Bihar Cricket League",
    ],
    "pakistan": [
        "Pakistan Super League",
        "PSL",
        "Quaid-e-Azam Trophy",
        "Pakistan Cup",
        "National T20 Cup",
        "Champions T20 Cup",
        "Pakistan Champions Cup",
        "Kashmir Premier League",
    ],
    "australia": [
        "Big Bash League",
        "BBL",
        "Women's Big Bash League",
        "WBBL",
        "Sheffield Shield",
        "Australia One-Day Cup",
        "Marsh One-Day Cup",
        "Women's National Cricket League",
        "WNCL",
        "Top End T20 Series",
        "T20 Max",
    ],
    "england_wales": [
        "County Championship",
        "County Championship Division One",
        "County Championship Division Two",
        "T20 Blast",
        "Vitality Blast",
        "T20 Blast Women",
        "T20 Blast, Women",
        "The Hundred",
        "The Hundred Men",
        "The Hundred Women",
        "The Hundred (W)",
        "One Day Cup",
        "Metro Bank One Day Cup",
        "Women's One-Day Cup",
        "One Day Cup Women",
        "Rachael Heyhoe Flint Trophy",
        "Charlotte Edwards Cup",
        "Women's County Championship",
        "National Counties Championship",
        "National Counties Trophy",
    ],
    "new_zealand": [
        "Super Smash",
        "Women's Super Smash",
        "Plunket Shield",
        "Ford Trophy",
        "Hallyburton Johnstone Shield",
        "HBJ Shield",
    ],
    "south_africa": [
        "SA20",
        "CSA T20 Challenge",
        "CSA T20 Women",
        "CSA One-Day Cup",
        "Momentum One Day Cup",
        "CSA 4-Day Series",
        "Sunfoil Series",
        "Provincial T20 Cup",
        "Women's Professional League",
    ],
    "west_indies_caribbean": [
        "Caribbean Premier League",
        "CPL",
        "Women's Caribbean Premier League",
        "WCPL",
        "Regional Super50",
        "Super50 Cup",
        "West Indies Championship",
        "The 6ixty",
        "6ixty",
    ],
    "bangladesh": [
        "Bangladesh Premier League",
        "BPL",
        "Dhaka Premier League",
        "DPL",
        "Bangladesh Cricket League",
        "BCL",
        "National Cricket League",
        "NCL",
    ],
    "sri_lanka": [
        "Lanka Premier League",
        "LPL",
        "T20 Lanka Premier League",
        "National Super League",
        "Major Clubs T20 Tournament",
        "Major League Tournament",
        "Lanka T10 Super League",
        "Lanka T10",
    ],
    "afghanistan": [
        "Shpageeza Cricket League",
        "Ghazi Amanullah Khan Regional One Day Tournament",
        "Ahmad Shah Abdali 4-day Tournament",
    ],
    "zimbabwe": [
        "Zim Afro T10",
        "Zimbabwe Domestic Twenty20",
        "Pro50 Championship",
        "Logan Cup",
    ],
    "uae_gulf": [
        "International League T20",
        "ILT20",
        "Abu Dhabi T10",
        "Emirates D10",
        "Emirates D20",
        "Emirates D50",
        "Bukhatir T10",
        "Bukhatir T20",
        "Sharjah Ramadan T10",
        "Sharjah Ramadan T20",
        "Kuwait T10",
        "Kuwait T20",
        "Kuwait Kerala Premier League",
        "Kuwait Kerala Premier League T20",
        "Oman D10",
        "Oman D20",
        "Qatar T10",
    ],
    "united_states_canada": [
        "Major League Cricket",
        "MLC",
        "Minor League Cricket",
        "MiLC",
        "Global T20 Canada",
        "GT20 Canada",
        "US Masters T10",
        "National Cricket League USA",
    ],
    "nepal": [
        "Nepal Premier League",
        "NPL",
        "Everest Premier League",
        "Dhangadhi Premier League",
    ],
    "ireland_scotland_netherlands": [
        "Inter-Provincial Championship",
        "Inter-Provincial Cup",
        "Inter-Provincial Trophy",
        "Topklasse",
        "Dutch T20 Cup",
        "Dutch One Day Cup",
        "Scottish Premiership",
        "Scottish T20",
    ],
    "hong_kong_asia_pacific": [
        "Hong Kong Premier League",
        "Hong Kong T20 Premier League",
        "Hong Kong T20 Premier League Women",
        "HK T20 PLW",
        "Japan Cricket League",
        "Japan Premier League",
        "JCL T10",
        "Singapore National Cricket League",
        "Malaysia T20",
    ],
    "european_club_associate": [
        "European Cricket Series",
        "ECS",
        "ECS T10",
        "ECS England",
        "European Cricket League",
        "ECL",
        "ECL T10",
        "European Cricket Championship",
        "ECC",
        "European Cricket Network",
        "ECN",
        "German Super League",
        "European T10",
    ],
    "africa_local_associate": [
        "Mukono Super Smash",
        "Mukono Super Smash T20",
        "Rwanda T20 League",
        "Uganda T20 League",
        "Nigeria T20",
        "Kenya T20",
    ],
    "legends_exhibition": [
        "Legends League Cricket",
        "LLC",
        "World Championship of Legends",
        "WCL",
        "International Masters League",
        "Road Safety World Series",
        "Global Legends League",
        "Legends Cricket Trophy",
        "Celebrity Cricket League",
    ],
    "other_short_format": [
        "Global Super League",
        "Max60",
        "Sixty",
        "T10 League",
    ],
}


ALL_COMPETITION_NAMES = sorted({
    name
    for group in CRICKET_COMPETITIONS.values()
    for name in group
} | set(OBSERVED_POLYMARKET_COMPETITIONS_2026_07_31))


CRICKET_MARKET_TYPES = [
    "match winner",
    "moneyline",
    "winner",
    "to win",
    "champion",
    "tournament winner",
    "series winner",
    "series result",
    "series score",
    "draw",
    "tie",
    "no result",
    "completed match",
    "match completed",
    "match abandoned",
    "toss",
    "coin toss",
    "toss winner",
    "toss match double",
    "team top batter",
    "top batsman",
    "highest individual score",
    "top bowler",
    "most wickets",
    "most sixes",
    "most fours",
    "innings total",
    "total runs",
    "over/under",
    "runs line",
    "wickets line",
    "run handicap",
    "spread",
    "winning margin",
    "method of dismissal",
    "first innings",
    "first innings winner",
    "first over",
    "first 5 overs",
    "first 6 overs",
    "first 10 overs",
    "first 15 overs",
    "powerplay",
    "highest opening partnership",
    "player runs",
    "player wickets",
    "player sixes",
    "player fours",
    "player of the match",
    "man of the match",
    "orange cap",
    "purple cap",
    "to qualify",
    "to reach final",
    "to reach playoffs",
    "top group",
    "bottom group",
]


MATCH_SEPARATORS = [
    " vs ",
    " vs. ",
    " v ",
    " versus ",
    " @ ",
    " at ",
    " - ",
    " – ",
    " — ",
]


ICC_MEMBERS_BY_REGION = {
    "africa": [
        "Botswana", "Cameroon", "Côte d’Ivoire", "Eswatini", "Gambia",
        "Ghana", "Kenya", "Lesotho", "Malawi", "Mali", "Mozambique",
        "Namibia", "Nigeria", "Rwanda", "Saint Helena", "Seychelles",
        "Sierra Leone", "South Africa", "Tanzania", "Uganda", "Zimbabwe",
    ],
    "americas": [
        "Argentina", "Bahamas", "Belize", "Bermuda", "Brazil", "Canada",
        "Cayman Islands", "Chile", "Costa Rica", "Falkland Islands",
        "Mexico", "Panama", "Peru", "Suriname", "Turks and Caicos Islands",
        "USA", "West Indies",
    ],
    "asia": [
        "Afghanistan", "Bahrain", "Bangladesh", "Bhutan", "Cambodia",
        "China", "Hong Kong, China", "India", "Iran", "Kuwait", "Malaysia",
        "Maldives", "Mongolia", "Myanmar", "Nepal", "Oman", "Pakistan",
        "Qatar", "Saudi Arabia", "Singapore", "Sri Lanka", "Tajikistan",
        "Thailand", "UAE", "Uzbekistan",
    ],
    "east_asia_pacific": [
        "Australia", "Cook Islands", "Fiji", "Indonesia", "Japan",
        "New Zealand", "Papua New Guinea", "Philippines", "Samoa",
        "South Korea", "Vanuatu",
    ],
    "europe": [
        "Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus",
        "Czech Republic", "Denmark", "England & Wales", "Estonia",
        "Finland", "France", "Germany", "Gibraltar", "Greece", "Guernsey",
        "Hungary", "Ireland", "Isle of Man", "Israel", "Italy", "Jersey",
        "Luxembourg", "Malta", "Netherlands", "Norway", "Portugal",
        "Romania", "Scotland", "Serbia", "Slovenia", "Spain", "Sweden",
        "Switzerland", "Turkey",
    ],
}


ALL_ICC_MEMBERS = sorted({
    team
    for region in ICC_MEMBERS_BY_REGION.values()
    for team in region
})


INTERNATIONAL_TEAM_ALIASES = {
    "Afghanistan": ["AFG"],
    "Australia": ["AUS", "Australia Men", "Australia Women", "AUS-W"],
    "Bangladesh": ["BAN", "BANGLADESH", "BD", "BGD"],
    "Canada": ["CAN"],
    "England": ["ENG", "England & Wales", "England Men", "England Women", "ENG-W"],
    "Hong Kong, China": ["Hong Kong", "HK", "HKG"],
    "India": ["IND", "India Men", "India Women", "IND-W"],
    "Ireland": ["IRE", "IRL"],
    "Italy": ["ITA"],
    "Namibia": ["NAM"],
    "Nepal": ["NEP"],
    "Netherlands": ["NED", "Netherlands Men", "Netherlands Women"],
    "New Zealand": ["NZ", "NZL", "Black Caps", "White Ferns", "NZ-W"],
    "Oman": ["OMA", "OMN"],
    "Pakistan": ["PAK", "Pakistan Men", "Pakistan Women", "PAK-W"],
    "Papua New Guinea": ["PNG", "New Guinea"],
    "Scotland": ["SCO"],
    "Singapore": ["SGP", "SIN"],
    "South Africa": ["SA", "RSA", "ZA", "Proteas", "South Africa Women", "SA-W"],
    "Sri Lanka": ["SL", "SRI", "Sri Lanka Men", "Sri Lanka Women", "SL-W"],
    "Thailand": ["THA"],
    "Uganda": ["UGA"],
    "United Arab Emirates": ["UAE", "Emirates"],
    "USA": ["United States", "United States of America", "US", "USA Cricket"],
    "West Indies": ["WI", "Windies", "West Indies Men", "West Indies Women", "WI-W"],
    "Zimbabwe": ["ZIM"],
}


TEAM_LEVEL_SUFFIXES = [
    "Women",
    "Women's",
    "W",
    "Men",
    "Men's",
    "Under-19",
    "Under 19",
    "U19",
    "Under-23",
    "Under 23",
    "U23",
    "A",
    "Emerging",
    "Academy",
    "XI",
    "2nd XI",
]


OBSERVED_POLYMARKET_TEAMS_2026_07_31 = ['Adelaide Strikers', 'Afghanistan', 'Afghanistan Under-19s', 'Apex CC', 'Arabian Eagles', 'Australia', 'Australia Under-19s', 'Band-e-Amir Dragons', 'Bangalore', 'Bangladesh', 'Bihar', 'Blue Giants', 'Boost Defenders', 'Brisbane Heat', 'Bussana', 'Canada', 'Chennai', 'Chennai Super Kings', 'Comilla Warriors (ENG)', 'Delhi', 'Delhi Capitals', 'Derbyshire', "Durban's Super Giants", 'Durham', 'East Londoners', 'England', 'England Under-19s', 'Essex', 'Fearless Lions of West Ham', 'Glamorgan', 'Gloucestershire', 'Gujarat', 'Gujarat Titans', 'Gulbarga Mystics Women', 'Hampshire', 'Himachal Pradesh', 'Hobart Hurricanes', 'Hornchurch', 'Hubli Tigers Women', 'Hyderabad', 'India', 'India Under-19s', 'Indonesia', 'Ireland', 'Ishaq11 Kasaragod', 'Italy', 'Jammu & Kashmir', 'Japan Under-19s', 'Joburg Super Kings', 'Kalagi', 'Kalyani Bengaluru Blasters Women', 'Kent', 'Kochi Lions', 'Kolkata', 'Kolkata Knight Riders', 'Lancashire', 'Leicestershire', 'Los Angeles Knight Riders', 'Lucknow', 'Lucknow Super Giants', 'MI Cape Town', 'Manipur', 'Markhor London', 'Melbourne Renegades', 'Melbourne Stars', 'Mi New York', 'Middlesex', 'Mis-e-Ainak Knights', 'Mpata', 'Mumbai', 'Mumbai Indians', 'Mysore Warriors Women', 'Namibia', 'Nepal', 'Netherlands', 'New Guinea', 'New Zealand', 'Northamptonshire', 'Nottinghamshire', 'Ntunda', 'Oman', 'Paarl Royals', 'Pakistan', 'Pakistan Under-19s', 'Palakad Patriots', 'Papua New Guinea', 'Perth Scorchers', 'Pretoria Capitals', 'Puducherry', 'Punjab', 'Punjab Kings', 'Rainham', 'Rajasthan', 'Rajasthan Royals', 'Rambros Kasargod', 'Royal Challengers Bangalore', 'San Francisco Unicorns', 'Scotland', 'Seattle Orcas', 'Sharks Cricket Academy', 'Singapore', 'Somerset', 'South Africa', 'South Africa Under-19s', 'Speen Ghar Tigers', 'Sri Lanka', 'Sri Lanka Under-19s', 'Sunrisers Eastern Cape', 'Sunrisers Hyderabad', 'Surrey', 'Sussex', 'Sydney Sixers', 'Sydney Thunder', 'Texas Super Kings', 'Thailand', 'USA', 'Uganda', 'United Arab Emirates', 'United States', 'Upminster', 'Uzbekistan', 'Warwickshire', 'Washington Freedom', 'West Indies', 'Worcestershire', 'Yorkshire', 'Zimbabwe']


TEAM_ALIASES = {
    **INTERNATIONAL_TEAM_ALIASES,
    "Chennai Super Kings": ["CSK", "Chennai"],
    "Delhi Capitals": ["DC", "Delhi"],
    "Gujarat Titans": ["GT", "Gujarat"],
    "Kolkata Knight Riders": ["KKR", "Kolkata"],
    "Lucknow Super Giants": ["LSG", "Lucknow"],
    "Mumbai Indians": ["MI", "Mumbai"],
    "Punjab Kings": ["PBKS", "Punjab", "Kings XI Punjab", "KXIP"],
    "Rajasthan Royals": ["RR", "Rajasthan"],
    "Royal Challengers Bengaluru": [
        "RCB", "Royal Challengers Bangalore", "Bangalore", "Bengaluru"
    ],
    "Sunrisers Hyderabad": ["SRH", "Hyderabad"],
    "Adelaide Strikers": ["ADS", "STR"],
    "Brisbane Heat": ["BRH", "HEA"],
    "Hobart Hurricanes": ["HBH", "HUR"],
    "Melbourne Renegades": ["MLR", "REN"],
    "Melbourne Stars": ["MLS", "STA"],
    "Perth Scorchers": ["PRS", "SCO"],
    "Sydney Sixers": ["SYS", "SIX"],
    "Sydney Thunder": ["SYT", "THU"],
    "MI Cape Town": ["MICT"],
    "Joburg Super Kings": ["JSK"],
    "Paarl Royals": ["PR"],
    "Pretoria Capitals": ["PC"],
    "Sunrisers Eastern Cape": ["SEC"],
    "Durban's Super Giants": ["DSG", "Durban Super Giants"],
    "Los Angeles Knight Riders": ["LAKR"],
    "MI New York": ["MINY", "Mi New York"],
    "San Francisco Unicorns": ["SFU"],
    "Seattle Orcas": ["SEO"],
    "Texas Super Kings": ["TSK"],
    "Washington Freedom": ["WF"],
}


KNOWN_KALSHI_CRICKET_SERIES = {
    "KXCRICKET",
    "KXCRICKETODIMATCH",
    "KXCRICKETT20IMATCH",
    "KXCRICKETTESTMATCH",
    "KXCRICKETWOMENODIMATCH",
    "KXCRICKETWOMENT20IMATCH",
    "KXCRICKETWOMENTESTMATCH",
    "KXTESTMATCH",
    "KXWTESTMATCH",
    "KXODIMATCH",
    "KXWODIMATCH",
    "KXT20MATCH",
    "KXWT20MATCH",
    "KXT20WC",
    "KXT20WORLDCUP",
    "KXWT20WORLDCUP",
    "KXT20CANADA",
    "KXT20CANADAMATCH",
    "KXIPL",
    "KXIPLGAME",
    "KXIPLFINAL",
    "KXIPLFINALS",
    "KXIPLPLAYOFF",
    "KXIPLFIRST6",
    "KXIPLFIRST10",
    "KXIPLFIRST15",
    "KXIPLTOTAL",
    "KXIPLTEAMTOTAL",
    "KXIPLSIX",
    "KXIPLFOUR",
    "KXIPLORANGECAP",
    "KXIPLPURPLECAP",
    "KXPSL",
    "KXPSLGAME",

    "KXCPL",
    "KXCPLMATCH",
    "KXWPLGAME",
    "KXHUNDREDMATCH",
    "KXWHUNDREDMATCH",
    "KXCOUNTYCHAMPMATCH",
    "KXASIACUPCRICKET",
    "KXT20TOTAL",
    "KXT20TEAMTOTAL",
    "KXWT20TEAMTOTAL",
    "KXT20FIRST6",
    "KXT20FIRST10",
    "KXT20FIRST15",
    "KXT20SIX",
    "KXT20SIXES",
    "KXT20FOUR",
}


KNOWN_KALSHI_CRICKET_SERIES_PREFIXES = sorted(KNOWN_KALSHI_CRICKET_SERIES)


def normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def has_phrase(blob: str, phrase: str) -> bool:
    b = normalize_text(blob)
    p = normalize_text(phrase)
    return bool(p and re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", b))


_AMBIGUOUS_COMPETITION = {
    "bbl", "bcl", "bpl", "cpl", "dpl", "ecc", "ecl", "ecn", "ecs", "llc",
    "lpl", "mlc", "ipl", "psl", "wpl", "t10", "t20", "6ixty", "max60",
    "cricket",
}

SPECIFIC_COMPETITIONS = [
    c for c in ALL_COMPETITION_NAMES
    if normalize_text(c) not in _AMBIGUOUS_COMPETITION
]
AMBIGUOUS_COMPETITIONS = [
    c for c in ALL_COMPETITION_NAMES
    if normalize_text(c) in _AMBIGUOUS_COMPETITION
]


def cricket_evidence(
    *fields: Optional[str],
    native_sport: Optional[str] = None,
    native_tags: Optional[list[str]] = None,
    series_ticker: Optional[str] = None,
) -> dict:
    blob = " ".join(str(x) for x in fields if x)
    norm = normalize_text(blob)
    ev: list[str] = []

    native_blob = " ".join([native_sport or ""] + list(native_tags or []))
    native = has_phrase(native_blob, "cricket")
    if native:
        ev.append("native_sport=cricket")

    ticker = (series_ticker or "").upper().strip()


    series = ticker in KNOWN_KALSHI_CRICKET_SERIES
    if series:
        ev.append(f"known_series={ticker}")

    strong = [p for p in CRICKET_STRONG_PHRASES if has_phrase(norm, p)]
    if strong:
        ev.append(f"strong={strong[:3]}")

    specific = [c for c in SPECIFIC_COMPETITIONS if has_phrase(norm, c)]
    if specific:
        ev.append(f"competition={specific[:3]}")

    ambiguous = [c for c in AMBIGUOUS_COMPETITIONS if has_phrase(norm, c)]
    weak = [t for t in CRICKET_WEAK_TOKENS if has_phrase(norm, t)]
    market = [m for m in CRICKET_MARKET_TYPES if has_phrase(norm, m)]
    if ambiguous:
        ev.append(f"ambiguous_competition={ambiguous[:3]}")
    if weak:
        ev.append(f"weak={weak[:3]}")
    if market:
        ev.append(f"market_wording={market[:3]}")

    noise = [n for n in NON_CRICKET_NOISE_TERMS if has_phrase(norm, n)]
    if noise:
        ev.append(f"NOISE={noise[:3]}")


    if native or series:
        verdict = "cricket"
    elif noise:


        verdict = "not_cricket"
    elif strong or specific:
        verdict = "cricket"
    elif (ambiguous or weak) and market:
        verdict = "review"
    else:


        verdict = "not_cricket"

    return {"verdict": verdict, "evidence": ev, "normalized": norm}


def looks_like_cricket(
    *fields: Optional[str],
    native_sport: Optional[str] = None,
    native_tags: Optional[list[str]] = None,
    series_ticker: Optional[str] = None,
    include_review: bool = True,
) -> bool:
    r = cricket_evidence(*fields, native_sport=native_sport,
                         native_tags=native_tags, series_ticker=series_ticker)
    return r["verdict"] == "cricket" or (
        include_review and r["verdict"] == "review")


def flatten_aliases() -> tuple[dict[str, str], dict[str, list[str]]]:
    out: dict[str, str] = {}
    collisions: dict[str, list[str]] = {}
    for canonical, aliases in TEAM_ALIASES.items():
        for alias in [canonical, *aliases]:
            key = normalize_text(alias)
            if not key:
                continue
            if key in out and out[key] != canonical:
                collisions.setdefault(key, [out[key]]).append(canonical)
            else:
                out[key] = canonical
    for key in collisions:
        out.pop(key, None)
    return out, collisions


REQUIRED_RAW_FIELDS = [
    "venue",
    "retrieved_at_utc",
    "endpoint",
    "request_params",
    "raw_event_json",
    "raw_market_json",
]

REQUIRED_NORMALIZED_FIELDS = [
    "venue",
    "event_id",
    "market_id",
    "series_id_or_ticker",
    "title",
    "subtitle",
    "description",
    "slug_or_ticker",
    "native_sport",
    "native_competition",
    "native_league",
    "game_id",
    "scheduled_start_utc",
    "close_time_utc",
    "team_1_raw",
    "team_2_raw",
    "outcome_raw",
    "market_type_raw",
    "line",
    "best_bid",
    "best_ask",
    "status",
    "volume",
]

DISCOVERY_ORDER = [
    "Use native tags and venue filter metadata.",
    "Resolve the Polymarket cricket tag and all venue competitions/series.",
    "Paginate every active/open event and market under those identifiers.",
    "Fetch every participant/team embedded in those events.",
    "Fetch venue team metadata endpoints and merge aliases.",
    "Run keyword searches only as a recall backstop.",
    "Persist raw JSON before filtering.",
    "Flag uncertain rows for review; never silently discard them.",
]


DETECTOR_CASES = [

    ("West Indies vs Pakistan match: Pakistan wins", "KXTESTMATCH", "cricket"),
    ("Major League Cricket: Mi New York vs Seattle Orcas", None, "cricket"),
    ("ECS England: Sharks Cricket Academy vs Upminster", None, "cricket"),
    ("Guwahati Royals vs Jorhat Stallions men's cricket match",
     "KXT20MATCH", "cricket"),
    ("Will St. Lucia Kings win the 2026 Caribbean Premier League championship?",
     "KXCPL", "cricket"),

    ("US Test scores in Math", "KXUSTESTSMATH", "not_cricket"),
    ("Bundesliga Basketball Game", "KXBBLGAME", "not_cricket"),
    ("Will Detroit be the Detroit vs A's first 3 innings winner?",
     "KXMLBF3", "not_cricket"),
    ("Acme Holdings LLC merger closes before 2027", None, "not_cricket"),
    ("Cocoa Monthly", "KXCOCOAMON", "not_cricket"),
    ("AI turing test", "KXAITURING", "not_cricket"),
]


def self_test(verbose: bool = True) -> bool:
    ok = True
    for text, ticker, expected in DETECTOR_CASES:
        got = cricket_evidence(text, series_ticker=ticker)["verdict"]
        passed = got == expected
        ok &= passed
        if verbose and not passed:
            print(f"  FAIL  want={expected:12} got={got:12} {text[:58]}")
    tagged = cricket_evidence(
        "Will Team A win?", native_tags=["cricket"]
    )["verdict"]
    if tagged != "cricket":
        ok = False
        if verbose:
            print(f"  FAIL  native cricket tag got={tagged}")

    aliases, collisions = flatten_aliases()
    if verbose:
        print(f"  aliases resolvable: {len(aliases)}")
        if collisions:
            print(f"  UNRESOLVABLE COLLISIONS ({len(collisions)}):")
            for k, v in collisions.items():
                print(f"    {k!r} -> {v}")
        print("  detector:", "PASS" if ok else "FAIL")
    return ok


def probe_endpoints(timeout: int = 12) -> None:
    import json as _json
    import urllib.error
    import urllib.request

    extra = {"kalshi_known_good": {
        "markets": "https://api.elections.kalshi.com/trade-api/v2/markets"}}
    for venue, urls in {**API_ENDPOINTS, **extra}.items():
        print(f"\n[{venue}]")
        for name, url in urls.items():
            if "{" in url:
                print(f"  {name:24} SKIP (template)")
                continue
            req = urllib.request.Request(
                url + ("?limit=1" if "?" not in url else ""),
                headers={"User-Agent": "cricket-recon/0.4",
                         "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = r.read(400)
                    print(f"  {name:24} {r.status} {len(body)}b "
                          f"{body[:60]!r}")
            except urllib.error.HTTPError as e:
                print(f"  {name:24} HTTP {e.code}")
            except Exception as e:
                print(f"  {name:24} FAIL {e!r}"[:90])


if __name__ == "__main__":
    import sys
    if "--probe" in sys.argv:
        probe_endpoints()
    else:
        print("self-test:")
        sys.exit(0 if self_test() else 1)
