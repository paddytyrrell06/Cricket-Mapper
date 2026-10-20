# Market data and expansion

The current terminal is a read-only snapshot scanner for cricket, seven selected soccer competitions, and NBA, NFL, NHL, and MLB game winners on Kalshi and Polymarket International. Soccer covers regulation time plus stoppage time; advancement and extra-time outcomes are not yet in scope. It does not place orders or claim that a displayed price difference guarantees a profit.

## Rules before prices

An event identity and a contract identity are separate. Sport, participants, date, competition, start-time evidence and market type establish candidate fixtures. A payout profile then records the selected outcome's payments and policies for the relevant result states. A three-way cricket contract that loses on a draw is different from a two-way contract paying 0.50 on a draw. Postponement deadlines, abandonment, forfeits, overtime and administrative voids also matter.

Core completed-game payouts and sport-specific result rules must be evidenced and match before a row can enter the comparable-cross scan. Cancellation, postponement, abandonment, and other exceptional event states are reported as conditional caveats; they do not make normal-result payouts appear different. Strict full-contract comparison is available in the terminal when exceptional-state equivalence is required. Unverified core rules and real completed-game differences remain visible for review. Raw rules, extracted evidence, missing states, caveats and quote provenance remain in the saved data and row details. Equal unknown rules are never treated as equivalent.

The cross tab is a shortlist of comparable, fresh cross-venue quotes before fees and execution costs. It is not an execution engine: available quantity, fee schedules, cash/collateral requirements, cancellation exposure and simultaneous execution must be checked before treating a cross as an arbitrage.

## Current collection

Sport adapters separate discovery and parsing from the shared fixture matcher, rule checks, quote collector, audit and terminal. Basketball, football, hockey and baseball discovery uses each league's game-series endpoint; identities require published venue metadata and that league's exact team registry rather than fuzzy abbreviations. Soccer discovery is scoped to the English Premier League, LaLiga, Bundesliga, Serie A, Ligue 1, UEFA Champions League and MLS; each participant must match the event's published competition-scoped name/alias metadata. Raw records, linked Kalshi game milestones and authoritative team metadata are saved for replay.

Discovery is slower than price collection and should remain a separate, periodic task. Selected Kalshi contracts are refreshed using the documented comma-separated `tickers` filter. Polymarket books are requested in batches for exact outcome-token IDs. The code uses bounded request concurrency and retains missing sides as missing. [Kalshi markets API](https://docs.kalshi.com/api-reference/market/get-markets), [Polymarket order books](https://docs.polymarket.com/market-data/prices-order-books).

Quote observation time is recorded independently of the exchange book timestamp and market metadata update time. Replay never refreshes a saved quote. Freshness and cross-venue skew limits prevent stale snapshots from entering the eligible scan.

## WebSocket route

Polymarket's public market socket is `wss://ws-subscriptions-clob.polymarket.com/ws/market`. Subscribe by outcome asset ID, maintain the application heartbeat, and consume book/top-of-book changes rather than last trades. Sports score messages are a different feed and cannot substitute for executable quotes. [Official real-time documentation](https://docs.polymarket.com/market-data/realtime-data).

Kalshi's current production socket is `wss://external-api-ws.kalshi.com/trade-api/ws/v2`. Its handshake requires signed API-key authentication even for public market-data channels. The browser must never receive the private key. Subscribe only to the selected tickers; seed state from a snapshot before applying deltas. [Kalshi connection guide](https://docs.kalshi.com/getting_started/quick_start_websockets), [order-book messages](https://docs.kalshi.com/websockets/orderbook-updates).

The next service should own both connections, keep a cache keyed by venue and contract/outcome ID, and publish changes to the local terminal. On a disconnect, sequence discontinuity or invalid book, invalidate that cache entry and obtain a new snapshot before restoring cross eligibility. Preserve positive depth, decimal prices, exchange time, observation time and subscription generation. Recompute only rows touched by an update; rescan discovery separately for new/resolved markets and changed rules.

Streaming is not enabled in this release. A static HTML report cannot securely own Kalshi credentials or recover a server-side order book. No credentials were needed or requested for the present public REST scanner. A future authenticated service should honor the account's actual limits, back off on throttling, and avoid assuming that metadata `updated_time` represents a quote update. [Kalshi rate limits](https://docs.kalshi.com/getting_started/rate_limits), [Polymarket rate limits](https://docs.polymarket.com/api-reference/rate-limits).
