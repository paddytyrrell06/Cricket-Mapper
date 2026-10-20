# Market data and expansion

The terminal continuously collects public REST quotes from Kalshi and Polymarket International. `python3 server.py` keeps the audited universe in memory, serves the browser UI and schedules refreshes. Physical sports and CS2/LoL esports have separate categories; Finance and Tech remain disabled pending identity and payout adapters. Venue WebSocket connections and order execution are not enabled.

## Rules before prices

An event identity and a contract identity are separate. Sport, participants, date, competition, start-time evidence and market type establish candidate fixtures. A payout profile then records the selected outcome's payments and policies for the relevant result states. A three-way cricket contract that loses on a draw is different from a two-way contract paying 0.50 on a draw. Postponement deadlines, abandonment, forfeits, overtime and administrative voids also matter.

Core completed-game payouts and sport-specific result rules must be evidenced and match before a row can enter the comparable-cross scan. Cancellation, postponement, abandonment, and other exceptional event states are reported as conditional caveats; they do not make normal-result payouts appear different. Strict full-contract comparison is available in the terminal when exceptional-state equivalence is required. Unverified core rules and real completed-game differences remain visible for review. Raw rules, extracted evidence, missing states, caveats and quote provenance remain in the saved data and row details. Equal unknown rules are never treated as equivalent.

Gross crosses lists comparable, fresh cross-venue price differences. After fees + additionally requires a positive estimate after both platform taker fees at the selected actual size, using per-market fee parameters. The default requested size is 100 contracts, capped to whole paired top depth. Gross ¢, Fees ¢ and Net ¢ are per contract; Est. PnL $ is total dollars at the displayed quantity. Details separates both order fees and the assumed capital. See [fee sources, liquidity bounds and worked examples](FEES.md). Inventory, collateral, venue constraints and simultaneous execution still require validation.

## Local browser service

Run `python3 server.py` and leave it running. It opens `http://127.0.0.1:8765/CricketMatchingReport.html` and loads `market_map.csv`, native instrument IDs, discovery records and fee context once into `runtime.MarketRuntime`. The service requests quotes immediately and every **15 seconds**. Manual browser reload and **Refresh prices** request the same work. Repeated requests coalesce into one background job, and the existing table stays visible while it runs. Errors appear in the service status; stale quotes lose eligibility.

Normal runtime refreshes update market objects and row records in memory. They do not run the scanner subprocess, rebuild HTML, reparse discovery or archive snapshots. A new full CLI publication is detected through the snapshot metadata fingerprint and reloads the universe. If an audited universe cannot initialize, the compatibility refresh path can still invoke the saved-discovery report builder.

Use `--interval=30` to change the refresh interval or `--interval=0` for manual refresh. Nonzero intervals must be between 5 and 3,600 seconds. Requests arriving while a refresh runs are coalesced, so the interval does not promise a fixed venue delivery cadence. `--port=8766` selects another port; `--no-open` skips opening the tab. Ctrl+C stops the service.

The service binds only to loopback. It serves the terminal assets and fixed data/control endpoints, rather than browser-supplied commands or file paths:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/status` | Refresh progress, errors, runtime mode and revision |
| `GET /api/markets?revision=<revision>` | Changed compact rows/freshness updates for the previous revision; full records after missed revisions or discovery changes; no repeated records for the current revision |
| `GET /api/market/<id>` | Fetch full settlement evidence/details on demand |
| `POST /api/refresh` | Request quote collection; repeated requests share one job |
| `POST /api/settings` | Apply the liquidity threshold and optional watchlist-only universe |

The browser polls status and patches changed records without reloading the page. Unchanged price/depth/fee rows receive small freshness updates; a client that misses revisions gets a complete frame. Details stay outside the regular row payload and are fetched only when opened. A completed refresh publishes one coherent frame; an exception retains the previous frame. The API's `as_of` reflects actual quote observation time; `checked_at` separately records the refresh check. Per-row eligibility still depends on that row's quote timestamps. Service settings are process-wide: watchlist-only mode and the threshold affect every tab connected to the same service.

HTML/JavaScript remains appropriate for the browser terminal. Python owns market identity, fee calculations, quote state and future exchange connections. The standalone `file://` HTML remains an export; it cannot launch Python on reload. WebSocket adapters can later replace REST ingestion and publish engine changes through a browser socket/event stream. That transport change does not require replacing the browser UI or rebuilding HTML on ticks.

## Selecting a useful universe

`LiquidityPolicy` defaults to a $100 minimum paired top notional on **each selected leg**. For `q = floor(min(sell_bid_size, buy_ask_size))`, the route must satisfy both `q × sell_bid >= 100` and `q × buy_ask >= 100`. The UI displays their minimum as Top liq $. This threshold concerns available top capacity, separately from the requested trade's size. Volume, open interest and a venue's headline market liquidity do not establish paired executable depth.

The runtime narrows repeated quote work using current explicit CLOB aggregate liquidity or current BBO depth. A low aggregate can avoid a book request; a BBO suppresses an instrument only when **both** its bid and ask notionals are below the bound, so one large side is retained for potential routes. Aggregate and book timestamps remain independent. Unknown, invalid, future or stale data is probed. Thin markets are probed again after **60 seconds**; they are not permanently removed. The UI hides unknown depth by default and offers an explicit toggle to inspect it. Missing depth never becomes a zero-size or zero-fee actionable estimate.

Star useful rows and open Watchlist to narrow local quote requests to those instruments. Leaving Watchlist restores the broader monitored universe. The browser's liquidity control also updates service policy; league/search/category filters otherwise organize the displayed rows. Display settings do not discover additional markets.

## Current collection

The adapter registry separates discovery and parsing from the shared fixture matcher, rule checks, quote collector, audit and terminal. Basketball, football, hockey, baseball, tennis and MMA use verified league-specific game series and participant evidence. Soccer includes 17 competitions in `sports.SOCCER_COMPETITIONS`; `--soccer-leagues=epl,ucl,uel` narrows discovery. CS2 and LoL use full-match series and exact published participant identities; maps, rounds and props remain excluded. See [sports coverage](SPORTS.md) and [esports scope and settlement](ESPORTS.md). Raw records, native milestones and participant metadata are saved for replay.

Discovery is slower than price collection and stays outside the incremental quote path. Three independent discovery scopes run concurrently; the shared discovery HTTP client limits each origin to four simultaneous requests, including nested league jobs. Pagination within each feed remains ordered. A failed sport does not discard successful sports. Selected Kalshi contracts are refreshed using the documented comma-separated `tickers` filter. Polymarket books are requested in batches for exact outcome-token IDs. Both venue refreshes overlap to reduce wall time and timestamp skew, with four batch workers per venue. Missing sides remain missing. [Kalshi markets API](https://docs.kalshi.com/api-reference/market/get-markets), [Polymarket order books](https://docs.polymarket.com/market-data/prices-order-books).

Quote observation time is recorded independently of the exchange book timestamp and market metadata update time. Replay never refreshes a saved quote. Freshness and cross-venue skew limits prevent stale snapshots from entering the eligible scan.

For a REST-only refresh during development, run `python3 run.py --snapshot-dir=output/data --refresh-quotes --no-open`. It reloads and rechecks saved discovery, then fetches matched quotes concurrently from both venues. Discovery keeps its original timestamp; new games/rule changes still require a periodic full scan. Plain `--snapshot-dir` remains offline. This command rebuilds an audited HTML snapshot and archives evidence, so it is not the future per-tick service loop.

A standalone HTML export checks its sibling `CricketMatchingReport.version.js` every two seconds and reloads when a successful CLI publication changes the version, preserving view settings in session storage. Runtime mode follows API records instead of repeatedly rebuilding the report. Failed builds do not publish a marker. `--no-open` controls whether the Python process requests a browser window. The quote-check time remains separate from discovery time.

## Incremental quote path

The report build writes three additional files under `output/data`:

- `stream_manifest.json`: deduplicated subscription IDs and mappings back to venue, market and normalized outcome. Kalshi IDs are contract tickers; Polymarket IDs are exact selected outcome tokens. No fuzzy mapping happens on a quote update.
- `live_state.json`: the audited matches seeded into the incremental engine. It records quote states, comparison states and freshness policy. This is a diagnostic snapshot, not a connected service.
- `latency.json`: measured discovery, parsing/matching, quote refresh and audit/render wall times for that run. These are REST scanner timings, not network WebSocket latency or execution guarantees.

`LiveQuoteEngine.from_matches(...)` builds the instrument-to-comparison index, evaluates settlement compatibility and compiles fee schedules once. Its immutable liquidity policy is also compiled outside ingestion. `seed(markets)` imports REST quotes without refreshing their times. Each `apply(QuoteUpdate(...))` atomically replaces one normalized BBO and recalculates only comparisons containing that instrument, including order-size fees and top-notional bounds. Decimal values preserve price precision. Unknown depth remains distinct from positive capacity. No disk writes, HTML generation, HTTP calls, metadata parsing, discovery or fixture matching occur in `apply`.

The three current freshness policies serve different collection modes:

| Mode | Maximum quote age | Maximum cross-venue skew |
| --- | ---: | ---: |
| Persistent REST runtime | 30 seconds | 5 seconds |
| Exported report | 300 seconds | 30 seconds |
| Incremental engine defaults | 5 seconds | 1 second |

Callers can configure the incremental engine explicitly. Seeded scanner quotes may already be too old for its default eligibility. Expiration must run on a timer even during quiet markets. A WebSocket adapter must feed complete validated BBO replacements into the engine; raw deltas need a venue-specific book assembler first. Freshness withdrawal removes previously estimated fee signals as well as gross crosses. Thin/unknown liquidity retains diagnostic prices while screened monitor/actionable flags remain false.

Use `engine.invalidate(affected_keys)` when an adapter detects a channel gap or corrupt book. Empty delta/heartbeat messages cannot refresh quote age. After `disconnect(venue)`, start a new generation with `begin_session(venue)` and seed fresh snapshots before deltas. A fresh full snapshot can restore an invalidated, unchanged book at the same source sequence; duplicate ordinary updates do not refresh its lifetime.

The current service and future socket path share the same identity boundary:

```text
periodic discovery + rule checks -> audited universe + native instrument IDs
                                                   |
current: batched REST -> persistent market runtime -> revised row API -> browser
                                                   |
future: venue sockets -> book assemblers -> indexed quote engine
                                                   |
                              affected comparisons -> browser / future strategy
```

Keep a single owner for feed ordering and serialize updates through the engine. Coalesce UI notifications separately from the quote evaluation loop, so a slow browser cannot block ingestion. Refresh discovery periodically and rebuild/swap the index when markets open, resolve or change rules. The current REST clients use standard-library urllib; persistent pooled HTTP connections and measured venue-specific pacing remain useful future REST improvements. Faster discovery does not substitute for streaming prices.

Measure the local path independently of REST/network time:

```bash
python3 benchmark_streaming.py --pairs=1000 --updates=20000
```

The benchmark reports median/p95/p99 update time and throughput for synthetic paired instruments. It does not measure venue delivery, browser delivery, order placement, or guaranteed production throughput.

## WebSocket route

Polymarket's public market socket is `wss://ws-subscriptions-clob.polymarket.com/ws/market`. Subscribe by outcome asset ID, maintain the application heartbeat, and consume book/top-of-book changes rather than last trades. Sports score messages are a different feed and cannot substitute for executable quotes. [Official real-time documentation](https://docs.polymarket.com/market-data/realtime-data).

Kalshi's current production socket is `wss://external-api-ws.kalshi.com/trade-api/ws/v2`. Its handshake requires signed API-key authentication even for public market-data channels. The browser must never receive the private key. Subscribe only to the selected tickers; seed state from a snapshot before applying deltas. [Kalshi connection guide](https://docs.kalshi.com/getting_started/quick_start_websockets), [order-book messages](https://docs.kalshi.com/websockets/orderbook-updates).

The Python service should own both connections and use `LiveQuoteEngine` as the cache/evaluation boundary. On a disconnect, sequence discontinuity or invalid book, invalidate affected quotes and obtain a new snapshot before restoring eligibility. `begin_session(venue)` creates a new generation; updates from old connections must be rejected. Kalshi sequence continuity belongs to the subscription/channel, so validate it before dispatching per-instrument updates. Preserve positive depth, decimal prices, exchange time, observation time and subscription generation. Rescan discovery separately for new/resolved markets and changed rules.

Venue streaming is not enabled in this release. The browser is the display and control surface; the backend will own credentials and book recovery. No credentials were needed or requested for the public REST scanner. A future authenticated service should honor the account's actual limits, back off on throttling, and avoid assuming metadata `updated_time` represents a quote update. Order execution, balances, inventory reservation, slippage limits, partial-fill recovery and account-specific charges require their own strategy/execution boundary. [Kalshi rate limits](https://docs.kalshi.com/getting_started/rate_limits), [Polymarket rate limits](https://docs.polymarket.com/api-reference/rate-limits).
