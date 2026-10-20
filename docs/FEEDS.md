# Market data and expansion

The terminal collects public REST snapshots from Kalshi and Polymarket International. `python3 server.py` serves the browser UI locally and accepts quote refresh requests. Sport adapters define the available competitions in `sports.py`. Soccer covers regulation time plus stoppage time; advancement and extra-time outcomes are outside that scope. `streaming.py` provides the incremental in-memory quote engine. Venue WebSocket connections and order execution are not enabled.

## Rules before prices

An event identity and a contract identity are separate. Sport, participants, date, competition, start-time evidence and market type establish candidate fixtures. A payout profile then records the selected outcome's payments and policies for the relevant result states. A three-way cricket contract that loses on a draw is different from a two-way contract paying 0.50 on a draw. Postponement deadlines, abandonment, forfeits, overtime and administrative voids also matter.

Core completed-game payouts and sport-specific result rules must be evidenced and match before a row can enter the comparable-cross scan. Cancellation, postponement, abandonment, and other exceptional event states are reported as conditional caveats; they do not make normal-result payouts appear different. Strict full-contract comparison is available in the terminal when exceptional-state equivalence is required. Unverified core rules and real completed-game differences remain visible for review. Raw rules, extracted evidence, missing states, caveats and quote provenance remain in the saved data and row details. Equal unknown rules are never treated as equivalent.

Gross crosses lists comparable, fresh cross-venue price differences. Net + additionally requires a positive estimate after both platform taker fees, using current per-market fee parameters. The default estimate is one contract per leg; top-level quantities and repriced whole-contract capacity appear in details. See [fee sources and rounding assumptions](FEES.md). Minimum order sizes, inventory/collateral and simultaneous execution remain separate from this indicative screen.

## Local browser service

Run `python3 server.py` and leave it running. It opens `http://127.0.0.1:8765/CricketMatchingReport.html`; manual browser reload and **Refresh prices** request a saved-discovery quote scan. The table stays visible while that job runs, then follows the audited publication. Repeated requests coalesce into one job; publication-triggered reloads are marked so they cannot start a refresh loop. A failed job displays its error and retains the previous audited table. `--port=8766` selects a different port and `--no-open` skips opening the tab.

The service binds only to loopback, serves only the report/version assets, and accepts a fixed refresh action rather than executable browser-supplied commands. A shared OS build lock prevents an independent CLI run and the service from modifying the same staging directory at once. Public exchange requests still run in the existing scanner subprocess. Stop the service with Ctrl+C.

HTML/JavaScript remains the browser frontend. The local service supplies the missing backend boundary; the standalone `file://` report remains an export. For continuous market data, replace the on-demand scanner job with persistent exchange connections, feed the in-memory quote engine, and publish changed rows to the browser over a socket or event stream. Price ticks should not rebuild HTML, rerun discovery, or archive a whole snapshot. This HTTP controller is the first service step; its current price collection is REST on demand.

## Current collection

Sport adapters separate discovery and parsing from the shared fixture matcher, rule checks, quote collector, audit and terminal. Basketball, football, hockey, baseball, tennis and MMA discovery uses verified league-specific game series; identities require exact registries or published league participant metadata. Soccer discovery includes 17 verified competitions in `sports.SOCCER_COMPETITIONS`; each participant must match the event's published competition-scoped name/alias metadata. `--soccer-leagues=epl,ucl,uel` limits live discovery when a smaller universe is desired. Raw records, linked Kalshi game milestones and participant metadata are saved for replay.

Discovery is slower than price collection and stays outside the incremental quote path. Three independent discovery scopes run concurrently; the shared discovery HTTP client limits each origin to four simultaneous requests, including nested league jobs. Pagination within each feed remains ordered. A failed sport does not discard successful sports. Selected Kalshi contracts are refreshed using the documented comma-separated `tickers` filter. Polymarket books are requested in batches for exact outcome-token IDs. Both venue refreshes overlap to reduce wall time and timestamp skew, with four batch workers per venue. Missing sides remain missing. [Kalshi markets API](https://docs.kalshi.com/api-reference/market/get-markets), [Polymarket order books](https://docs.polymarket.com/market-data/prices-order-books).

Quote observation time is recorded independently of the exchange book timestamp and market metadata update time. Replay never refreshes a saved quote. Freshness and cross-venue skew limits prevent stale snapshots from entering the eligible scan.

For a REST-only refresh during development, run `python3 run.py --snapshot-dir=output/data --refresh-quotes --no-open`. It reloads and rechecks saved discovery, then fetches matched quotes concurrently from both venues. Discovery keeps its original timestamp; new games/rule changes still require a periodic full scan. Plain `--snapshot-dir` remains offline. This command rebuilds an audited HTML snapshot and archives evidence, so it is not the future per-tick service loop.

The open HTML checks its sibling `CricketMatchingReport.version.js` every two seconds. It reloads only when an audited publication changes that version, preserving view settings in browser session storage. A small script request works with a directly opened `file://` report; it avoids browser restrictions on fetching local JSON. Failed builds do not publish a new marker. `--no-open` controls whether the Python process requests a browser window; an already open updated report continues following publications. The displayed quote-check time is separate from discovery time. Continuous quote ingestion still belongs to the future feed service.

## Incremental quote path

The report build writes three additional files under `output/data`:

- `stream_manifest.json`: deduplicated subscription IDs and mappings back to venue, market and normalized outcome. Kalshi IDs are contract tickers; Polymarket IDs are exact selected outcome tokens. No fuzzy mapping happens on a quote update.
- `live_state.json`: the audited matches seeded into the incremental engine. It records quote states, comparison states and freshness policy. This is a diagnostic snapshot, not a connected service.
- `latency.json`: measured discovery, parsing/matching, quote refresh and audit/render wall times for that run. These are REST scanner timings, not network WebSocket latency or execution guarantees.

`LiveQuoteEngine.from_matches(...)` builds the instrument-to-comparison index once, evaluates settlement compatibility and compiles fee schedules once. `seed(markets)` imports REST quotes without refreshing their times. Each `apply(QuoteUpdate(...))` atomically replaces one normalized BBO and recomputes only comparisons containing that instrument, including fees and top-depth estimates. Decimal values preserve price precision. Best-level sizes are saved where the venue supplies them; unknown depth is distinct from positive executable depth. No disk writes, HTML generation, HTTP calls, discovery or fixture matching occur in `apply`.

The live engine uses stricter freshness defaults than the static report: five seconds of quote age and one second of cross-venue skew. The snapshot report retains its existing five-minute/30-second policy. Callers can configure the engine policy explicitly. Seeded scanner quotes may already be too old for live eligibility; this is expected. Expiration must run on a service timer even during quiet markets. A WebSocket transport must feed complete validated BBO replacements into the engine; raw order-book deltas require a venue-specific book assembler first.

Use `engine.invalidate(affected_keys)` when an adapter detects a channel gap or corrupt book. Empty delta/heartbeat messages cannot refresh quote age. After `disconnect(venue)`, start a new generation with `begin_session(venue)` and seed fresh snapshots before deltas. A fresh full snapshot can restore an invalidated, unchanged book at the same source sequence; duplicate ordinary updates do not refresh its lifetime.

The intended service data flow is:

```text
periodic discovery + rule checks -> audited matches + subscription manifest
                                                   |
venue sockets -> book assemblers -> QuoteUpdate -> indexed quote engine
                                                   |
                                      affected comparison changes
                                                   |
                                       local UI / future strategy
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

The service should own both connections and use `LiveQuoteEngine` as the cache/evaluation boundary. On a disconnect, sequence discontinuity or invalid book, invalidate the affected cache and obtain a new snapshot before restoring eligibility. `begin_session(venue)` creates a new generation; updates from old connections must be rejected. Kalshi sequence continuity belongs to the subscription/channel, so validate it before dispatching per-instrument updates. Preserve positive depth, decimal prices, exchange time, observation time and subscription generation. Rescan discovery separately for new/resolved markets and changed rules.

Streaming is not enabled in this release. A static HTML report cannot securely own Kalshi credentials or recover a server-side order book. No credentials were needed or requested for the present public REST scanner. A future authenticated service should honor the account's actual limits, back off on throttling, and avoid assuming that metadata `updated_time` represents a quote update. [Kalshi rate limits](https://docs.kalshi.com/getting_started/rate_limits), [Polymarket rate limits](https://docs.polymarket.com/api-reference/rate-limits).
