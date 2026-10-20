# Saved coverage and opportunity audit

Historical saved snapshot, not live opportunities. No API requests. Snapshot freshness uses the end of the recorded check second; report limits 300s/30s, runtime limits 30s/5s. Review reasons and route exclusions use different units and are not additive. Only configured scopes and returned records can be audited.

Quote observation checkpoint: 2026-10-20T22:01:28.999999+00:00

## Pipeline counts

| Stage / unit | Count |
| --- | ---: |
| outcome_rows | 1117 |
| contract_accepted_rows | 407 |
| fresh_comparable_rows | 403 |
| both_directions_evaluated_rows | 403 |
| possible_directional_routes | 2234 |
| fresh_comparable_routes | 806 |
| known_sized_fee_routes | 798 |
| depth_bound_routes | 280 |
| known_fees_and_depth_bound_routes | 280 |
| known_fees_depth_and_minimum_routes | 280 |
| positive_before_bounds_routes | 1 |
| positive_after_filters_routes | 0 |
| fixture_links_accepted | 198 |
| fixture_links_review | 315 |
| market_links_review | 722 |
| unmatched_fixtures | 681 |
| unmatched_markets | 1645 |
| collected_source_records | 4397 |

Normalized source objects: {'fixtures': 1696, 'markets': 3885}

Collection responses complete: 18/18
Parser exclusions (unique saved warning records): {'unverified_team_identity': 252, 'other': 2}
Coverage-flag attribution anomalies (other venue's parser warning): ['soccer:polymarket_intl', 'basketball:polymarket_intl', 'football:polymarket_intl']

## Depth classification

- measured: 1100
- missing_size: 0
- unpriced_or_invalid_book: 17
- no_estimate_rows: 714
- no_estimate_but_measured: 697

No estimate is not evidence of missing depth. Review, skew, stale quotes, absent prices and missing quantities are distinct.

## Directional exclusions (first failing gate)

- contract:fixture_review: 1336
- contract:market_review: 16
- contract:rules_differ: 8
- contract:rules_unknown: 60
- liquidity:below_bound: 518
- net:not_positive: 280
- quote:skewed: 8
- size:insufficient_whole_contract_depth: 8

Both inventory trading directions evaluated: {'buy_polymarket_intl_sell_kalshi': 403, 'buy_kalshi_sell_polymarket_intl': 403}
Runtime 30s age / 5s skew at the SAME saved checkpoint: {'outcome_rows': 1117, 'contract_accepted_rows': 407, 'fresh_comparable_rows': 228, 'both_directions_evaluated_rows': 228, 'possible_directional_routes': 2234, 'fresh_comparable_routes': 456, 'known_sized_fee_routes': 449, 'depth_bound_routes': 135, 'known_fees_and_depth_bound_routes': 135, 'known_fees_depth_and_minimum_routes': 135, 'positive_before_bounds_routes': 1, 'positive_after_filters_routes': 0}

## Matching review causes (fixture links)

- published start times differ by more than 30 minutes: 229
- probable team identity variant: both team names are similar; published dates differ by 1 day; published start times differ by more than 30 minutes (weaker-team similarity 1.00): 46
- published start times differ by more than 30 minutes; published rules start conflicts with linked game milestone: 5
- published rules start conflicts with linked game milestone: 4
- format mismatch ['FIRSTCLASS', 'TEST']; competition mismatch ['ranji trophy', 'test']: 3
- probable team identity variant: same venue-published team codes; published dates differ by 1 day (weaker-team similarity 0.96): 3
- format mismatch ['', 'LISTA']; competition mismatch ['icc cricket world cup challenge league a', 'international']; format not established: 2
- probable team identity variant: both team names are similar; published dates differ by 1 day; published start times differ by more than 30 minutes; published rules start conflicts with linked game milestone (weaker-team similarity 1.00): 2

## Quote/API warnings

Saved quote sources: {'kalshi': {'Kalshi refreshed market yes_bid_dollars/yes_ask_dollars': 1075, 'Kalshi market no longer open': 2, 'Kalshi market snapshot yes_bid_dollars/yes_ask_dollars': 40}, 'polymarket_intl': {'https://clob.polymarket.com/books': 1117}}

- QUOTES kalshi: refresh unavailable (HTTP 429); discovery snapshot retained
- QUOTES ('kalshi', 'KXCS2GAME-26OCT201500LNQUIN-LN', 'los ninos'): market is no longer open
- QUOTES ('kalshi', 'KXCS2GAME-26OCT201500LNQUIN-QUIN', 'quintessencia'): market is no longer open

## 20 closest near-misses

Runtime limits (30s age, 5s cross-venue skew). Closest nonpositive net per contract, one best route per outcome row; fresh comparable quotes, known sized fees, $100 paired top bound and provided order minimums retained.
All fee and edge columns below are cents per contract; PnL and liquidity are dollars. Conditional settlement caveats and unknown execution constraints remain in CSV/JSON.

| Market / outcome | Buy → sell | Qty | Gross ¢ | Fees ¢ | Net ¢ | Net $ | Paired top $ |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Denver Nuggets vs Oklahoma City Thunder / oklahoma city thunder | kalshi → polymarket_intl | 100 | 0 | 1.38 | -1.38 | -1.38 | 219.73 |
| Virtus.pro vs XMETA / virtus pro | kalshi → polymarket_intl | 100 | -1.3 | 0.2996 | -1.5996 | -1.5996 | 192.433 |
| Golden State Warriors vs Los Angeles Lakers / golden state warriors | polymarket_intl → kalshi | 100 | 0 | 1.6 | -1.6 | -1.6 | 133.7 |
| Brooklyn Nets vs Charlotte Hornets / charlotte hornets | kalshi → polymarket_intl | 100 | 0 | 1.71 | -1.71 | -1.71 | 2047.08 |
| Chicago Bulls vs Toronto Raptors / toronto raptors | kalshi → polymarket_intl | 100 | -1 | 1.21 | -2.21 | -2.21 | 5602.52 |
| Dallas Mavericks vs Houston Rockets / houston rockets | kalshi → polymarket_intl | 100 | -1 | 1.24 | -2.24 | -2.24 | 171.76 |
| HAVU vs Inner Circle Academy / havu | kalshi → polymarket_intl | 100 | 0 | 2.312 | -2.312 | -2.312 | 116.74 |
| FaZe vs JiJieHao / faze | polymarket_intl → kalshi | 100 | 0 | 2.4795 | -2.4795 | -2.4795 | 105.08 |
| Brighton & Hove Albion FC vs Liverpool FC / brighton hove albion fc | kalshi → polymarket_intl | 100 | 0 | 2.5695 | -2.5695 | -2.5695 | 15782.72 |
| Golden State Warriors vs Los Angeles Lakers / los angeles lakers | kalshi → polymarket_intl | 100 | -1 | 1.58 | -2.58 | -2.58 | 464.75 |
| Los Angeles Clippers vs Sacramento Kings / los angeles clippers | kalshi → polymarket_intl | 100 | -1 | 1.62 | -2.62 | -2.62 | 119.7 |
| Los Angeles Clippers vs Sacramento Kings / sacramento kings | kalshi → polymarket_intl | 100 | -1 | 1.64 | -2.64 | -2.64 | 178.56 |
| New York Knicks vs Philadelphia 76ers / new york knicks | kalshi → polymarket_intl | 100 | -1 | 1.65 | -2.65 | -2.65 | 29702.73 |
| New York Knicks vs Philadelphia 76ers / philadelphia 76ers | polymarket_intl → kalshi | 100 | -1 | 1.65 | -2.65 | -2.65 | 18503.34 |
| Phoenix Suns vs Portland Trail Blazers / portland trail blazers | kalshi → polymarket_intl | 100 | -1 | 1.67 | -2.67 | -2.67 | 352.2 |
| Phoenix Suns vs Portland Trail Blazers / phoenix suns | polymarket_intl → kalshi | 100 | -1 | 1.67 | -2.67 | -2.67 | 228.93 |
| Indiana Pacers vs New Orleans Pelicans / indiana pacers | kalshi → polymarket_intl | 100 | -1 | 1.7 | -2.7 | -2.7 | 6454.24 |
| Indiana Pacers vs New Orleans Pelicans / new orleans pelicans | kalshi → polymarket_intl | 100 | -1 | 1.71 | -2.71 | -2.71 | 12034.32 |
| Atlanta Hawks vs Orlando Magic / orlando magic | kalshi → polymarket_intl | 100 | -1 | 1.71 | -2.71 | -2.71 | 7500.63 |
| Brooklyn Nets vs Charlotte Hornets / brooklyn nets | kalshi → polymarket_intl | 100 | -1 | 1.71 | -2.71 | -2.71 | 3583.59 |

Positive estimates (including blocked): 1

- Virtus.pro vs XMETA / xmeta: net $0.0357, paired top $0.462, liquidity:below_bound, below_minimum

Prices use native YES/outcome-token books; inventory-sale routes are not a proof that a short or a separate NO-token order can be executed. NO-token/basket coverage is not inferred from an opposing team's YES when a draw, tie or cancellation payout can differ. Fees and whole-contract sizing reuse fees.py and liquidity.py. Nothing is sent to venues.
