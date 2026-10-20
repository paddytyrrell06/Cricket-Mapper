# Sports Market Terminal

A comparison terminal and streaming-ready quote infrastructure for **Kalshi and Polymarket International**. Coverage includes cricket, 17 soccer competitions, NBA/WNBA/college basketball, NFL/college football, NHL, MLB, ATP/WTA tennis and UFC. Polymarket US is excluded from the active scanner. The application currently collects public REST snapshots; sockets and order execution are not connected.

## Run

```bash
python3 run.py
```

Python 3.9+ and the standard library are sufficient. No API keys or package installation are required for the public REST snapshot scanner. Successful runs open the report in your default browser. `--no-open` skips that browser-opening step. To select one sport:

```bash
python3 run.py --sports=cricket --no-open
python3 run.py --sports=soccer --no-open
python3 run.py --sports=basketball --no-open
python3 run.py --sports=football,hockey,baseball --no-open
python3 run.py --sports=tennis,mma --no-open
python3 run.py --sports=soccer --soccer-leagues=epl,ucl,uel --no-open
```

The generated terminal remains at `output/CricketMatchingReport.html` for compatibility. Its black/amber palette, dense 25px rows, league column and tabular prices support scanning many outcomes. Sport tabs show bid/ask quotes with a compact rule status; the Crosses tab collates comparable, fresh cross-venue quotes. Details shows the same normalized bid/ask as the row, with status, observation time and original venue links. Settlement payouts at resolution are separately labeled; they never populate missing bid/ask prices.

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

Keep the report open: it checks a tiny local version file every two seconds and reloads when a successful run publishes new data. Your sport tab, search, filters and pagination are preserved. The header shows when quotes were checked, with the discovery timestamp retained separately. Use `--no-open` on subsequent runs to update the existing tab without requesting another browser window. An older tab opened before this feature needs one manual reload to enable it. Prices change when a collection run completes.

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
