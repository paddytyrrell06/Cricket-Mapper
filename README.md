# EventLens

A local prediction-market data terminal and research dataset for Kalshi and Polymarket International. Open **Data & research** in the terminal, or visit `http://127.0.0.1:8765/research`, to query recorded prices, inspect selected live books, chart historical observations and export data. This iteration adds data tools; it includes no strategies, backtesting or trading.

The Research workspace has a searchable market explorer, category/exchange filters, quote statistics, interactive price charts (hover, scroll to zoom, reset), local-time date ranges and quick ranges. **Export selection** downloads the current market/time/exchange/sampling selection; **More data tools → Export whole dataset** retains the dataset-wide export. Order books, historical price samples, observations and metadata have separate panels. The compact terminal now has one **Markets** workspace plus **Alerts**. Validated equivalent outcomes share a row with both venues; independent outcomes remain single venue. Exact question/outcome/expiry candidates are linked as **Review**, with settlement validation still required. Category navigation, link-status views, fresh-quote filtering, liquidity, size, watchlists and per-venue Research links all operate on the unified index. Missing quotes remain unavailable and stale quotes cannot remain in Alerts.

The existing REST worker also records at most 100 additional catalog outcomes per cycle, reserving up to 20 rotating probes for unknown/thin depth. Measured liquidity or provider liquidity metadata prioritises the remainder; volume only ranks probes within each venue. Runtime instruments are deduplicated, explicit closed/inactive contracts are skipped, and probes back off for at least 60 seconds. Native batching avoids per-market fee requests. These single-venue quote observations never create an arbitrage signal. A small indexed latest-quote table avoids rescanning historical observations for each page.

Research discovery is independent of matching: **More data tools → Refresh market catalog** discovers native Kalshi and Polymarket event catalogs, retaining unmatched outcomes without a liquidity threshold. The **Discovery coverage** table counts distinct native contracts by category and exchange, separately from outcome records and monitored comparisons. Each scan is bounded to six pages per exchange and resumes saved continuation cursors; nothing starts a catalog scan on page load. Provider categories/tags supply category labels; unknown categories remain Other. Open events may contain closed child contracts, so status and discovery time remain visible. Discovery metadata does not create quote history. A separate capped worker chooses liquidity candidates for quote collection. See [coverage scope and verification](docs/COVERAGE.md).

For Python access to the cached catalog (no venue requests):

```python
from market_catalog import MarketCatalog
from research_store import ResearchStore

catalog = MarketCatalog(ResearchStore("data/eventlens.sqlite3"))
print(catalog.summary())
markets = catalog.markets()
# Explicit bounded discovery: catalog.refresh(max_pages=1)
# catalog.lookup(markets[0]["id"]) returns metadata plus exact native asset IDs.
```

All persistent research data and the independent catalog live in `data/eventlens.sqlite3`. Older full CLI scan archives are in `.cricket_matching_history`; the current generated report and snapshot are in `output`. The database and archives are excluded from Git and need a separate backup if you want one. The service continues collecting after you close the browser. Ctrl+C stops collection without deleting data; `run.py` is a one-off scan. There is currently no automatic retention or disk quota: stop the service when unused and monitor disk space before leaving it running continuously. No history is deleted automatically.

The service records up to 25 liquid, validated outcome pairs per normal refresh in `data/eventlens.sqlite3`. This database survives report regeneration and application restarts and is excluded from Git. Selected markets can also be recorded every five seconds from the research page while it is open. Missing depth, exchange clocks and stale observations remain explicit.

To use selected Polymarket books and hourly price samples through the verified local PMXT SDK, start with `.venv-pmxt/bin/python server.py`. The optional environment is installed on this workstation; setup and the SDK's limitations are in [docs/PMXT.md](docs/PMXT.md). The ordinary `python3 server.py` command keeps native collection and historical research working without that dependency or a subscription. Kalshi books use its native public endpoint because the current PMXT implementation demands authentication.

Import the last 20 local snapshots without venue requests:

```bash
python3 research_store.py --import-history
```

Python research access uses actual observations, with optional time, exchange, market and sampling filters:

```python
from research_store import ResearchStore

store = ResearchStore("data/eventlens.sqlite3")
print(store.datasets())
quotes = store.history("recorded", exchange="kalshi", every_seconds=60, limit=1000)
# Add market_id=..., pair=..., start="2026-10-21T00:00:00Z", end=... as needed.
# Sampling returns the last actual observation per bucket; it never fills gaps.
row_with_rules_and_depth = store.snapshot("recorded", quotes[-1]["pair_key"])
```

Regular Polymarket recording retains up to ten returned price levels per side; Kalshi bulk collection supplies BBO depth. Explicit live-book reads retain every level returned by PMXT or the selected native endpoint. Imported snapshots only contain the data actually saved at that time. Public hourly candles are sampled-price buckets, stored separately from quotes, with no historical spread/depth or execution claim. Volume and market status carry their metadata observation times; absent values and unresolved settlements remain unknown. Full trade-print history, a resolution collector and verified WebSocket recording are future work. Hosted PMXT matching/order-book archive access requires separate credentials and was not invoked.

A comparison terminal and quote infrastructure for **Kalshi and Polymarket International**. Sports and Esports have separate category tabs. Sports includes cricket, 17 soccer competitions, NBA/WNBA/college basketball, NFL/college football, NHL, MLB, ATP/WTA tennis and UFC. Esports starts with Counter-Strike 2 and League of Legends full-match winners. The unified Markets workspace includes all discovered categories; cross-exchange settlement validation remains strongest for the existing sports/esports adapters. Polymarket US is excluded from the active scanner. Continuous collection currently uses public REST APIs; venue sockets and order execution are not connected.

## Run

Betfair's first read-only integration adds pre-match NBA BACK/LAY prices and
conservative fixture mappings alongside the existing markets. It is disabled
until configured and uses native odds/stake units. See [Betfair setup, scope and
validation](docs/BETFAIR.md); it adds no Betfair profit or execution calculation.

Start the local browser terminal:

```bash
python3 server.py
```

Keep this process running and use `http://127.0.0.1:8765/CricketMatchingReport.html`. It loads the audited market universe into memory and requests fresh quotes at startup and every **15 seconds**. Browser reload and **Refresh prices** also request a refresh. Quotes update the existing table through the local API; normal service refreshes do not rebuild HTML, rerun discovery or archive full snapshots. Repeated requests share one job. Stop the service with Ctrl+C. For first-time setup, `python3 run.py` performs discovery, then opens this same HTTP workspace and starts its server if needed.

Use `python3 server.py --interval=30` for a slower REST cycle, `--interval=0` for manual refresh, `--port=8766` for another port, or `--no-open` to leave browser opening to you. The interval is a scheduling target, not a quote-delivery guarantee; a running refresh is never duplicated.

Python 3.9+ and the standard library are sufficient. No API keys or package installation are required for the public REST scanner and local HTTP service. `python3 run.py` performs a full sports discovery scan and opens the full terminal, including the independent catalog's non-sports categories. An existing server for this publication is reused; otherwise the command keeps running as the local server until Ctrl+C. `--no-open` only publishes the audited export and does not open or start a server. To select one sport:

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

This intentionally removes apparent opportunities caused by different contracts. For example, current NBA venue rules have different cancellation/postponement treatment even where both markets include overtime. In the unified terminal, unresolved fixtures, ambiguous outcomes and known payout conflicts remain separate venue markets. **Needs check** identifies unambiguous candidates with a specific remaining evidence gap; these cannot produce alerts. Detailed audit exports retain all candidate decisions, and newly generated market reviews preserve the underlying fixture conflict.

Prices use exact decimals. A gross cross requires a bid strictly above a distinct venue's ask, compatible evidenced settlement and sufficiently recent observations. The local REST service permits **30 seconds of age / 5 seconds of cross-venue skew**. Exported reports use **300 / 30 seconds**; the incremental engine's default is **5 / 1 seconds**. The browser withdraws eligible estimates as quotes expire. Equal prices and an internally crossed book alone are not cross-venue candidates. Positive net estimates include both platform taker fees and displayed top depth; they do not establish execution.

## Sources and expansion

Soccer includes EPL, LaLiga, Bundesliga, Serie A, Ligue 1, Champions League, MLS, Europa League, Conference League, EFL Championship, Eredivisie, Primeira Liga, Scottish Premiership, Liga MX, 2. Bundesliga, A-League Men and USL Championship. Use `--soccer-leagues` with codes from `sports.SOCCER_COMPETITIONS` to narrow live discovery. NBA/WNBA, men's/women's college basketball, NFL/college football, NHL, MLB, ATP/WTA and UFC use league-specific game series and exact registries or published participant metadata. Cricket retains the project's competition and team normalization. Props, futures, unresolved identities and conflicting start times stay outside accepted comparisons. A configured league can have no open games; availability depends on both venues.

The first Esports adapters cover CS2 and LoL. They retain game-specific identity, exact outcome-token mappings and full-match settlement evidence; maps, rounds, props and outrights remain outside scope. See [docs/ESPORTS.md](docs/ESPORTS.md). Shared adapters, settlement profiles, matching, quote collection and audit provide the expansion points for future domains. Finance, Technology, Politics and other independently discovered categories are already accessible in the HTTP terminal; validated cross-exchange comparisons in those domains still need their own identity and payout models.

Discovery overlaps independent scopes with bounded requests per origin. Both venues' quote refreshes then run concurrently. Kalshi contracts refresh in batches; Polymarket quotes come from exact outcome-token order books. Last trades and display probabilities are never substituted for bids or asks. Observation time, exchange book time and available best-level sizes are recorded separately. Missing or failed quotes stay unavailable without erasing a fixture comparison.

Refresh prices using saved discovery to skip the slower catalog scan:

```bash
python3 run.py --snapshot-dir=output/data --refresh-quotes
```

This still reparses and audits the saved universe, fetches current matched quotes, rebuilds the report and archives evidence. It retains the discovery timestamp and cannot discover games opened since that snapshot; run a full scan periodically. The persistent local service uses the same known identities for repeated quote refreshes without these report-build steps. Omit `--refresh-quotes` for a strictly offline replay.

Directly opened `file://` reports remain sports snapshot exports, so they only contain Sports and Esports. The full catalog and Research database require the HTTP terminal. Normal live `run.py` invocations now open that terminal. Explicit offline replays (`--snapshot-dir` without `--refresh-quotes`) continue to open the snapshot file and make no live requests; add `--no-open` to suppress opening it. Snapshot files check a tiny local version marker every two seconds and reload after successful publications, preserving view settings. Their browser reload cannot launch Python. The header shows when quotes were checked, with discovery time retained separately.

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

Audit the saved coverage and closest near-misses without any API requests:

```bash
python3 coverage_audit.py
```

The report at `output/coverage_audit/report.md` includes pipeline counts, exclusion reasons, both trading directions and 20 distinct near-miss outcomes under the existing runtime freshness, liquidity and known minimum-order gates. `near_misses.csv`, `routes.csv` and `summary.json` provide exact fee, size and depth diagnostics. It evaluates the saved quote checkpoint, so results are historical, not current trading signals. Observed depth is reported separately from review and quote-timing eligibility; no filters or contract checks are relaxed.
