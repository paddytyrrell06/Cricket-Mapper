# Sports Market Terminal

A comparison terminal and streaming-ready quote infrastructure for **Kalshi and Polymarket International**. Coverage includes cricket, 17 soccer competitions, NBA/WNBA/college basketball, NFL/college football, NHL, MLB, ATP/WTA tennis and UFC. Polymarket US is excluded from the active scanner. The application currently collects public REST snapshots; sockets and order execution are not connected.

## Run

Start the local browser terminal:

```bash
python3 server.py
```

Keep this process running and use `http://127.0.0.1:8765/CricketMatchingReport.html`. Browser reload and the **Refresh prices** button request a new quote scan; the existing table remains visible until an audited refresh is published. View settings are preserved and repeated requests share one in-progress job. Automatic report reloads do not trigger a refresh loop. Stop the local service with Ctrl+C. A first-time install needs one `python3 run.py` discovery scan before starting the service.

Python 3.9+ and the standard library are sufficient. No API keys or package installation are required for the public REST scanner and local HTTP service. The original `python3 run.py` command still performs a full discovery scan and opens the report. `--no-open` skips that browser-opening step. To select one sport:

```bash
python3 run.py --sports=cricket --no-open
python3 run.py --sports=soccer --no-open
python3 run.py --sports=basketball --no-open
python3 run.py --sports=football,hockey,baseball --no-open
python3 run.py --sports=tennis,mma --no-open
python3 run.py --sports=soccer --soccer-leagues=epl,ucl,uel --no-open
```

The generated terminal remains at `output/CricketMatchingReport.html` for compatibility. Its black/amber palette, dense rows, league column and tabular prices support scanning many outcomes. Bid/ask quantities appear beside the corresponding prices in contracts/shares. The Net + tab shows estimated positive edges after taker fees on both legs; Gross crosses retains price-only differences. Unknown fees stay unavailable, and an internally crossed venue book cannot produce an eligible route. Details separates market prices, best-level quantities, fee calculations, and settlement payouts.

Net edge is `sell bid - buy ask - sell taker fee - buy taker fee`. The main estimate uses one contract per leg; details show each fee, the smaller available top-level quantity, and a recalculation for whole paired contracts at those prices. Fees are rounded for the order size rather than multiplying rounded one-contract fees. Live refreshes fetch current Kalshi series overrides and Polymarket market fee schedules. Kalshi uses conservative cent balance precision until the account type is known. These are indicative platform-fee estimates; minimum order sizes and available inventory are not validated. See [fee sources and assumptions](docs/FEES.md).

## Settlement compatibility

Matching teams and dates is insufficient. A team contract paying zero on a draw is different from one paying 0.50. Soccer compares team-win and draw payouts within 90 minutes plus stoppage time; extra-time/penalty advancement is intentionally excluded. The scanner also compares cricket draw, tie-breaks and DLS; NFL tied games and overtime; NHL overtime and shootouts; MLB extra innings; and NBA overtime. Cancellation, postponement, abandonment and other exceptional-state differences appear as conditional caveats, rather than making otherwise matching normal-result terms look different. Missing normal-result evidence or genuinely different completed-game payouts remain review rows and cannot produce an eligible cross. Discretionary fair-price or refund language cannot establish a fixed equivalent payoff.

This intentionally removes apparent opportunities caused by different contracts. For example, current NBA venue rules have different cancellation/postponement treatment even where both markets include overtime. A review row still shows both prices; it is not presented as an arbitrage.

Prices use exact decimals. A cross requires a bid strictly above a distinct venue's ask, compatible known settlement, observations no older than five minutes, and at most 30 seconds of quote-time skew. Equal prices and a locally crossed book alone are not cross-venue candidates. The browser expires eligible crosses as their observation window passes. Displayed differences exclude fees, available execution depth and execution risk.

## Sources and expansion

Soccer includes EPL, LaLiga, Bundesliga, Serie A, Ligue 1, Champions League, MLS, Europa League, Conference League, EFL Championship, Eredivisie, Primeira Liga, Scottish Premiership, Liga MX, 2. Bundesliga, A-League Men and USL Championship. Use `--soccer-leagues` with codes from `sports.SOCCER_COMPETITIONS` to narrow live discovery. NBA/WNBA, men's/women's college basketball, NFL/college football, NHL, MLB, ATP/WTA and UFC use league-specific game series and exact registries or published participant metadata. Cricket retains the project's competition and team normalization. Props, futures, unresolved identities and conflicting start times stay outside accepted comparisons. A configured league can have no open games; availability depends on both venues.

The sport adapters, shared settlement model, matching, quote collection and audit provide the expansion points for additional sports. The supported scope is currently match winners, not every market type on either exchange.

Discovery overlaps independent scopes with bounded requests per origin. Both venues' quote refreshes then run concurrently. Kalshi contracts refresh in batches; Polymarket quotes come from exact outcome-token order books. Last trades and display probabilities are never substituted for bids or asks. Observation time, exchange book time and available best-level sizes are recorded separately. Missing or failed quotes stay unavailable without erasing a fixture comparison.

Refresh prices using saved discovery to skip the slower catalog scan:

```bash
python3 run.py --snapshot-dir=output/data --refresh-quotes
```

This still reparses and audits the saved universe, and fetches current matched quotes. It retains the discovery timestamp and cannot discover games opened since that snapshot; run a full scan periodically. Omit `--refresh-quotes` for a strictly offline replay.

Directly opened `file://` reports remain snapshot exports. They check a tiny local version file every two seconds and reload when a successful CLI run publishes new data, preserving view settings. Their browser reload cannot launch Python; use the local HTTP terminal for browser-triggered collection. The header shows when quotes were checked, with discovery time retained separately.

`streaming.LiveQuoteEngine` compiles audited matches into an instrument index and recomputes only affected comparisons on a quote update. It supports atomic BBO replacements, freshness expiry, sequence-gap invalidation, reconnect generations and snapshot recovery. Its live defaults are five seconds of quote age and one second of skew. Discovery, settlement parsing, disk writes and HTML rendering stay outside that update path. `output/data/stream_manifest.json` maps exact feed IDs to outcomes; `live_state.json` records the seeded engine; `latency.json` records scanner phase times.

Benchmark local quote processing independently of network latency:

```bash
python3 benchmark_streaming.py --pairs=1000 --updates=20000
```

The verified competition catalog is in [docs/SPORTS.md](docs/SPORTS.md). WebSocket integration, authentication requirements, cache recovery and service boundaries are documented in [docs/FEEDS.md](docs/FEEDS.md). The generated HTML is a snapshot. A future service must own sockets and publish engine changes to the browser; unknown depth, fees and contingency differences still need to be handled before implementing execution.

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
