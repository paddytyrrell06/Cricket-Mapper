# Prediction Market Terminal

A comparison terminal and quote infrastructure for **Kalshi and Polymarket International**. Sports and Esports have separate category tabs. Sports includes cricket, 17 soccer competitions, NBA/WNBA/college basketball, NFL/college football, NHL, MLB, ATP/WTA tennis and UFC. Esports starts with Counter-Strike 2 and League of Legends full-match winners. Finance and Tech are visible future categories with discovery disabled. Polymarket US is excluded from the active scanner. Continuous collection currently uses public REST APIs; venue sockets and order execution are not connected.

## Run

Start the local browser terminal:

```bash
python3 server.py
```

Keep this process running and use `http://127.0.0.1:8765/CricketMatchingReport.html`. It loads the audited market universe into memory and requests fresh quotes at startup and every **15 seconds**. Browser reload and **Refresh prices** also request a refresh. Quotes update the existing table through the local API; normal service refreshes do not rebuild HTML, rerun discovery or archive full snapshots. Repeated requests share one job. Stop the service with Ctrl+C. A first-time install needs one `python3 run.py` discovery scan before starting it.

Use `python3 server.py --interval=30` for a slower REST cycle, `--interval=0` for manual refresh, `--port=8766` for another port, or `--no-open` to leave browser opening to you. The interval is a scheduling target, not a quote-delivery guarantee; a running refresh is never duplicated.

Python 3.9+ and the standard library are sufficient. No API keys or package installation are required for the public REST scanner and local HTTP service. The original `python3 run.py` command still performs a full discovery scan and opens the report. `--no-open` skips that browser-opening step. To select one sport:

```bash
python3 run.py --sports=cricket --no-open
python3 run.py --sports=soccer --no-open
python3 run.py --sports=basketball --no-open
python3 run.py --sports=football,hockey,baseball --no-open
python3 run.py --sports=tennis,mma --no-open
python3 run.py --sports=soccer --soccer-leagues=epl,ucl,uel --no-open
python3 run.py --sports=esports --no-open
```

The generated terminal remains at `output/CricketMatchingReport.html` for compatibility. The black/amber table shows bid/ask quantities beside their prices, compact event names, sortable headers, league filters and a star watchlist. Click an event or its detail arrow for full titles, venue links, settlement evidence and fee calculations. **Extra columns** restores secondary fields. `/` focuses search; `e`, `d` and `n` select edge, date and name sorting.

## Fees, size and liquidity

The main table separates **Gross ¢**, **Fees ¢**, **Net ¢** and **Est. PnL $**. The first three are per paired contract; PnL is total estimated dollars at the actual displayed quantity. **After fees +** shows positive fee-adjusted estimates; **Gross crosses** shows eligible price differences before fees. Unknown fee schedules do not become zero fees, and a locally crossed venue book cannot produce an eligible route.

The size selector defaults to **100 contracts**, with 1, 10, 1,000 and Top depth presets. Actual size is capped to the smaller displayed sell-bid/buy-ask quantity and floored to whole contracts. Both taker fees are recalculated and rounded for that whole order. Details shows `gross dollars − buy taker fee − sell taker fee = estimated net dollars`, plus the gross spread needed to cover those fees. Missing depth leaves sized PnL unavailable.

The default liquidity bound is **$100 of paired top-of-book notional on each selected leg**. For paired capacity `q = floor(min(sell_bid_size, buy_ask_size))`, Top liq is `min(q × sell_bid, q × buy_ask)`. This measures available top depth, not traded volume, open interest or total market liquidity. A book with $10 on the limiting leg stays below the bound even if the other venue is deep. Change **Min top liquidity $** to adjust the bound; **Unknown depth** explicitly includes records whose depth could not be established.

In the local service, the liquidity setting also narrows quote work. Fresh verified low CLOB liquidity or low BBO depth can avoid repeated expensive requests; thin markets are probed again after 60 seconds. Missing or stale measurements get a probe. Opening the **Watchlist** view narrows service quote requests to starred rows; returning to another view resumes the broader universe. These service settings are shared by browser tabs connected to that process.

A 2¢ or 3¢ gross spread does not establish profit. For 100 contracts with Kalshi's general multiplier 1 and Polymarket's sports curve, selling at 52¢ and buying at 50¢ gives **$2 gross − $3 taker fees = −$1**; 53¢/50¢ gives **$3 − $3 = $0**. At 12¢/10¢, the same 2¢ gross spread gives **$2 − $1.19 = $0.81** because the fee curves shrink near the extremes. These examples assume sufficient displayed depth and the documented rounding model. Actual per-market fees decide the result; there is no universal profitable spread threshold. See [fee sources, worked examples and assumptions](docs/FEES.md).

Estimated capital assumes fully collateralized paired positions: `q × (buy_ask + 1 − sell_bid) + both taker fees`. It does not assume that selling existing inventory has zero capital cost. Known venue minimum violations display **MIN** and are excluded from After fees +; missing constraints remain unknown. Available inventory, collateral mechanics, slippage, simultaneous fills and exceptional settlement still need execution validation. The terminal submits no trades.

## Settlement compatibility

Matching teams and dates is insufficient. A team contract paying zero on a draw is different from one paying 0.50. Soccer compares team-win and draw payouts within 90 minutes plus stoppage time; extra-time/penalty advancement is intentionally excluded. The scanner also compares cricket draw, tie-breaks and DLS; NFL tied games and overtime; NHL overtime and shootouts; MLB extra innings; and NBA overtime. Cancellation, postponement, abandonment and other exceptional-state differences appear as conditional caveats, rather than making otherwise matching normal-result terms look different. Missing normal-result evidence or genuinely different completed-game payouts remain review rows and cannot produce an eligible cross. Discretionary fair-price or refund language cannot establish a fixed equivalent payoff.

This intentionally removes apparent opportunities caused by different contracts. For example, current NBA venue rules have different cancellation/postponement treatment even where both markets include overtime. A review row still shows both prices; it is not presented as an arbitrage.

Prices use exact decimals. A gross cross requires a bid strictly above a distinct venue's ask, compatible evidenced settlement and sufficiently recent observations. The local REST service permits **30 seconds of age / 5 seconds of cross-venue skew**. Exported reports use **300 / 30 seconds**; the incremental engine's default is **5 / 1 seconds**. The browser withdraws eligible estimates as quotes expire. Equal prices and an internally crossed book alone are not cross-venue candidates. Positive net estimates include both platform taker fees and displayed top depth; they do not establish execution.

## Sources and expansion

Soccer includes EPL, LaLiga, Bundesliga, Serie A, Ligue 1, Champions League, MLS, Europa League, Conference League, EFL Championship, Eredivisie, Primeira Liga, Scottish Premiership, Liga MX, 2. Bundesliga, A-League Men and USL Championship. Use `--soccer-leagues` with codes from `sports.SOCCER_COMPETITIONS` to narrow live discovery. NBA/WNBA, men's/women's college basketball, NFL/college football, NHL, MLB, ATP/WTA and UFC use league-specific game series and exact registries or published participant metadata. Cricket retains the project's competition and team normalization. Props, futures, unresolved identities and conflicting start times stay outside accepted comparisons. A configured league can have no open games; availability depends on both venues.

The first Esports adapters cover CS2 and LoL. They retain game-specific identity, exact outcome-token mappings and full-match settlement evidence; maps, rounds, props and outrights remain outside scope. See [docs/ESPORTS.md](docs/ESPORTS.md). Shared adapters, settlement profiles, matching, quote collection and audit provide the expansion points for future domains. Finance and Tech need their own identity and payout models before their tabs can become active.

Discovery overlaps independent scopes with bounded requests per origin. Both venues' quote refreshes then run concurrently. Kalshi contracts refresh in batches; Polymarket quotes come from exact outcome-token order books. Last trades and display probabilities are never substituted for bids or asks. Observation time, exchange book time and available best-level sizes are recorded separately. Missing or failed quotes stay unavailable without erasing a fixture comparison.

Refresh prices using saved discovery to skip the slower catalog scan:

```bash
python3 run.py --snapshot-dir=output/data --refresh-quotes
```

This still reparses and audits the saved universe, fetches current matched quotes, rebuilds the report and archives evidence. It retains the discovery timestamp and cannot discover games opened since that snapshot; run a full scan periodically. The persistent local service uses the same known identities for repeated quote refreshes without these report-build steps. Omit `--refresh-quotes` for a strictly offline replay.

Directly opened `file://` reports remain snapshot exports. They check a tiny local version file every two seconds and reload when a successful CLI run publishes new data, preserving view settings. Their browser reload cannot launch Python; use the local HTTP terminal for browser-triggered collection. The header shows when quotes were checked, with discovery time retained separately.

HTML/JavaScript remains appropriate for the browser interface. `runtime.MarketRuntime` now owns persistent REST market state and serves changed row records and small freshness updates through `/api/markets`; detailed evidence is fetched on demand through `/api/market/<id>`. `streaming.LiveQuoteEngine` provides the future socket ingestion boundary: it compiles audited matches, fee schedules and liquidity policy into an instrument index and recalculates only affected comparisons. It supports atomic BBO replacements, freshness expiry, sequence-gap invalidation, reconnect generations and snapshot recovery. Its default is five seconds of quote age and one second of skew. Discovery, settlement parsing, disk writes and HTML rendering stay outside that update path. `output/data/stream_manifest.json` maps exact feed IDs to outcomes; `live_state.json` records the seeded engine; `latency.json` records scanner phase times.

Benchmark local quote processing independently of network latency:

```bash
python3 benchmark_streaming.py --pairs=1000 --updates=20000
```

The verified competition catalogs are in [docs/SPORTS.md](docs/SPORTS.md) and [docs/ESPORTS.md](docs/ESPORTS.md). Local service endpoints, WebSocket integration, authentication requirements and recovery are documented in [docs/FEEDS.md](docs/FEEDS.md). The Python service will own exchange sockets and publish engine changes to the browser. Venue WebSocket adapters and order execution remain future work.

## Audit and replay

A complete sport scope requires successful collection and parsing on both active venues. An incomplete sport does not make another sport's completed coverage disappear; absence claims always use that sport's own coverage. Critical correctness failures preserve the previous report. Review findings remain visible without forcing equivalence.

Saved files include raw venue records, authoritative NBA metadata, normalized `quotes.json`, `market_map.csv`, `coverage_status.csv`, `review_queue.csv`, `run_report.txt` and `audit_report.txt` under `output/data`. Successful runs also archive the snapshot in `.cricket_matching_history`.

Replay saved inputs without network access:

```bash
python3 run.py --no-open --snapshot-dir=output/data
```

Replay retains original quote times and is labelled as saved data. Missing or invalid files cannot inherit complete coverage. The failed build can also be replayed from `.cricket_matching_build/data`.

Run offline regression tests with:

```bash
python3 -m unittest discover -v
```
