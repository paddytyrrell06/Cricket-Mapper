# Sports Market Terminal

A read-only comparison terminal for **Kalshi and Polymarket International**, currently covering cricket match winners and NBA full-game winners. Polymarket US is excluded from the active scanner.

## Run

```bash
python3 run.py
```

Python 3.9+ and the standard library are sufficient. No API keys or package installation are required for the public REST snapshot scanner. Add `--no-open` to leave the browser closed. To select one sport:

```bash
python3 run.py --sports=cricket --no-open
python3 run.py --sports=basketball --no-open
```

The generated terminal remains at `output/CricketMatchingReport.html` for compatibility. Its Cricket and Basketball tabs show bid/ask quotes with a compact rule status. All crosses collates only comparable, fresh cross-venue quotes. Details opens the original venue links, payouts, policy differences, supporting rule text, observation times and diagnostics.

## Settlement compatibility

Matching teams and dates is insufficient. A team contract paying zero on a draw is different from one paying 0.50. The scanner compares evidenced payout states and policies, including draw, tie-breaks, no result, abandonment, forfeits, postponement deadlines and overtime. Unknown or differing rules remain review rows and cannot produce an eligible cross. Discretionary fair-price or refund language cannot establish a fixed equivalent payoff.

This intentionally removes apparent opportunities caused by different contracts. For example, current NBA venue rules have different cancellation/postponement treatment even where both markets include overtime. A review row still shows both prices; it is not presented as an arbitrage.

Prices use exact decimals. A cross requires a bid strictly above a distinct venue's ask, compatible known settlement, observations no older than five minutes, and at most 30 seconds of quote-time skew. Equal prices and a locally crossed book alone are not cross-venue candidates. The browser expires eligible crosses as their observation window passes. Displayed differences exclude fees, available execution depth and execution risk.

## Sources and expansion

Cricket reuses the project's competition and team-normalization logic. NBA discovery uses native game-winner endpoints, published team metadata and a league-scoped registry of all 30 teams. Props, futures, unresolved team identities and conflicting start times are kept out of accepted comparisons. Sport identifiers form part of fixture identity, so similar names cannot link across sports.

The sport adapters, shared settlement model, matching, quote collection and audit provide the expansion points for additional sports. The supported scope is currently match winners, not every market type on either exchange.

Selected Kalshi quotes are refreshed in batches after discovery. Polymarket quotes come from exact outcome-token order books. Last trades and display probabilities are never substituted for bids or asks. Observation time and exchange book time are recorded separately. Missing or failed quotes stay unavailable without erasing a fixture comparison.

WebSocket options, authentication requirements, cache recovery, rate limits and the next service architecture are documented in [docs/FEEDS.md](docs/FEEDS.md). Streaming and order execution are not enabled in this version.

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
