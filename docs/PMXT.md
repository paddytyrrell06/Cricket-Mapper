# Optional PMXT research data

The regular terminal remains a Python 3.9+ standard-library application. `pmxt_data.PmxtData` adds selected public Polymarket order books and bounded price history through a local PMXT sidecar. It exposes no account, wallet, signing or order methods. It does not run catalog scans on startup or replace the existing venue collectors.

## Install and run

The optional SDK is pinned to the verified [PyPI PMXT 2.54.0 release](https://pypi.org/project/pmxt/2.54.0/). Its current `urllib3>=2.7` dependency requires **Python 3.10+**, despite PMXT declaring Python 3.8+. The base terminal does not acquire this requirement. Self-hosting also needs Node.js on `PATH`; the Python wheel bundles the sidecar JavaScript.

```bash
python3.14 -m venv .venv-pmxt
.venv-pmxt/bin/python -m pip install -r requirements-pmxt.txt
.venv-pmxt/bin/python server.py --no-open
```

Any installed Python 3.10+ can create this environment. On the verified workstation the interpreter is `/opt/homebrew/bin/python3.14`. The SDK starts and reuses a local process and writes its own lock/log files under `~/.pmxt`. No global npm installation is needed for the verified wheel. The isolated environment used about 39 MB.

The adapter explicitly supplies a loopback HTTP URL, ignoring `PMXT_BASE_URL`. It rejects `PMXT_API_KEY` rather than allowing the SDK to inherit a hosted key. Leave that variable unset in this process. No wallet address, private key, Kalshi API key or hosted PMXT key was used for the successful Polymarket checks.

## Provider contract

```python
from pmxt_data import PmxtData

data = PmxtData()  # SDK import and sidecar startup wait until the first call.
book = data.fetch_book("polymarket_intl", exact_native_outcome_token_id)
history = data.fetch_candles("polymarket_intl", exact_native_outcome_token_id,
                             resolution="1h", limit=24)
```

`fetch_book` performs one SDK `fetch_order_book` call with the selected outcome ID. Polymarket requires the exact CLOB **outcome token ID**, not Gamma's market number, a condition ID or a hosted catalog UUID. The result contains `venue`, `instrument_id`, `source="pmxt_local"`, `observed_at`, `provider_time`, `exchange_time`, `source_metadata`, `bids` and `asks`. Each level has decimal-string `price` and `size`. Duplicate prices aggregate; bids sort descending and asks ascending. Invalid or zero-depth levels are excluded, and empty sides remain empty. Known minimum order fields are included only when the SDK preserves venue evidence. SDK float conversion has already occurred before this adapter receives levels; it cannot recover precision discarded upstream.

`observed_at` is recorded after the fetch. `provider_time` preserves PMXT's snapshot timestamp. Current PMXT normalizers can substitute their own clock, and the Polymarket wrapper removes the original timestamp from metadata. Consequently the adapter leaves `exchange_time` empty unless explicitly sourced exchange timestamp metadata exists. A provider timestamp is not proof of exchange freshness. Keep all clocks separately when recording research observations.

`fetch_candles` allows 1–100 hourly or daily candles from a bounded time window and one SDK call. Results are labeled `data_type="price_candles"` and `executable=False`. They describe sampled historical prices, not historical bids, asks, spread or depth; the newest bucket may be incomplete. PMXT builds OHLC buckets from Polymarket price samples. Its zero-volume fallback becomes `null` here because native price history does not establish zero traded volume. These candles cannot validate an executable backtest or support an after-fee arbitrage claim.

`fetch_markets(venue, query, limit=3)` returns at most three discovery suggestions marked `status="review"`, retaining the complete SDK record under `raw` (including its source metadata). A bounded result count does not guarantee a bounded number of upstream requests inside the SDK. No suggestion establishes identical settlement; the normal identity, rules, fees and outcome-mapping checks still apply.

A small CLI accepts at most two exact outcome IDs and performs at most two book method calls:

```bash
.venv-pmxt/bin/python pmxt_data.py polymarket_intl EXACT_OUTCOME_TOKEN_ID SECOND_EXACT_OUTCOME_TOKEN_ID
```

The SDK may retry a failed transport; method-call limits are not promises about the number of wire requests. Offline adapter tests require no installed SDK:

```bash
python3 -m unittest test_pmxt_data -v
```

## Verified behavior and limits

On 21 October 2026, a bounded comparison fetched two selected saved Polymarket outcomes through the installed SDK, then one native public `/books` batch for those same IDs. Chicago Bears was 0.60 bid / 0.61 ask, with 36,464.59 / 88,463.59 shares; Tampa Bay Rays was 0.40 / 0.41, with 97,911.52 / 71,202.72 shares. Both top levels, sizes, timestamps and the five-share minimum matched the native response in that sample. This establishes adapter operation for those observations; books can change between requests.

One three-hour Polymarket history request also succeeded and produced three hourly sampled-price buckets. No archive, hosted, catalog-discovery or trading calls were made during validation.

**Kalshi is currently limited by the SDK.** PMXT 2.54.0 `fetch_order_book` rejected the selected Kalshi market with an authentication error. The same market's native public `/markets/{ticker}/orderbook` endpoint returned its book without credentials. The adapter reports this limitation and does not ask for trading credentials or silently relabel native data as PMXT. Continue using the terminal's existing native public Kalshi source.

PMXT also offers Polymarket `watch_order_book` and batched `fetch_order_books`. They are not connected by this adapter. Public Polymarket market-data sockets do not require wallet credentials; Kalshi sockets require native API authentication.

### Why the optional socket recorder is deferred

Inspection of the installed 2.54.0 SDK found that `watch_order_book` requires the separately installed `websocket-client` package, waits up to 30 seconds without a public per-call cancellation argument, and uses a private Python transport lifecycle. The public exchange `close()` and `unwatch_order_book()` methods make HTTP requests to the sidecar; they do not call the Python socket transport's `close()` method. A reliable stop button would therefore need version-specific private SDK handling.

The sidecar can also silently satisfy a Polymarket watch with a real REST snapshot when the upstream socket is quiet. It does not preserve whether each returned book came from a socket event or that fallback. Labeling those results as verified socket updates would misstate their provenance. No socket package or recorder was installed or activated during this inspection.

A future recorder should have its own bounded session for at most two explicitly selected, audited outcome IDs, full snapshot replacement, separate provider/observation times, interruptible stop/unsubscribe, capped exponential reconnect backoff, and explicit stale/disconnected state. Transport pings and unchanged cached books must not advance quote freshness. Existing selected REST observations remain usable without those unresolved socket lifecycle guarantees.

The SDK's historical order-book `since`/`until` queries are a separate hosted PMXT Archive feature and require a hosted key. They are not enabled here. Public historical price candles are not a substitute for that archive. See the [PMXT order-book documentation](https://pmxt.dev/docs/api-reference/fetch-order-book) and [self-hosting documentation](https://pmxt.dev/docs/guides/self-hosted).

Hosted Router matching is likewise separate: it needs an explicitly supplied hosted key and may consume credits. No Router calls are made or configured. Future `fetch_matched_market_clusters` integration should request one bounded result with strict `relation="identity"`, retain confidence and reasoning as review evidence, and never bypass the terminal's settlement validation.
