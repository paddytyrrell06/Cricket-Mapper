# Betfair sports data: first milestone

EventLens can collect native Betfair Exchange **pre-match basketball winner**
markets and display their BACK/LAY odds and stake depth alongside conservative
fixture mappings. The initial competition is NBA, with a 72-hour look-ahead.
These links identify comparable sporting fixtures; they do not establish a
guaranteed hedge or calculate Betfair arbitrage profits.

This milestone uses REST snapshots in the existing terminal refresh cycle.
Streaming, execution, wallets, positions, payoff engines and paper trading are
outside its scope. Betfair data is kept in native decimal odds and stake units;
it is not passed through the binary-share fee/P&L calculator.

## Configure real data

The adapter uses Python's standard library. No additional package or PMXT hosted
subscription is required. Betfair still requires eligible Exchange API access,
an application key and a valid session or certificate login. Activation/data
charges and usage permissions depend on the account and access arrangement.

1. Copy `.env.betfair.example` to `.env.betfair`, which is ignored by Git.
2. Set `BETFAIR_ENABLED=1` and your `BETFAIR_APP_KEY`.
3. Provide either `BETFAIR_SESSION_TOKEN`, **or** all four certificate-login
   settings: `BETFAIR_USERNAME`, `BETFAIR_PASSWORD`, `BETFAIR_CERT_FILE` and
   `BETFAIR_KEY_FILE`. An existing token takes precedence. The certificate and
   key must be PEM files registered to your Betfair account; follow Betfair's
   [certificate setup and login instructions](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687915/Non-Interactive+bot+login).
4. Set the region for your account: `nz` uses `.com.au` betting and identity
   endpoints; `au` uses the global betting API and Australian identity;
   `global` uses `.com` for both. Endpoint choices follow the official
   [Betting API](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687158/Betting+API)
   and [session-management](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687869)
   documentation.
5. Set `BETFAIR_CURRENCY` to your account's currency if known, e.g. `AUD`, `NZD`
   or `GBP`. Missing currency stays unknown; EventLens neither assumes USD nor
   converts stake sizes into prediction-market shares.
6. Set `BETFAIR_DATA_MODE` to `delayed` or `live` only if you know your key's data
   access. The default is `unknown`. A delayed API response overrides a live
   configuration. A live label requires both explicit live configuration and
   `isMarketDataDelayed=false` from the API. REST receipt time is shown; API
   publication time remains unknown because these REST responses do not supply
   it.

Load the settings into the shell before starting EventLens:

```bash
set -a
source .env.betfair
set +a
python3 server.py
```

Use normal shell quoting for passwords containing spaces or special characters.
The application does not automatically execute or load `.env.betfair`. Restart
an already running terminal server after changing its environment. When keys are
missing or Betfair is disabled, the adapter reports setup status and makes no
requests. Certificate sessions are kept alive during active collection and
logged out on shutdown; externally supplied tokens are kept alive but never
revoked by EventLens.

## What is collected

- `listEventTypes` resolves Basketball; `listCompetitions` selects exact named
  competitions from `BETFAIR_LEAGUES` (default `NBA`). Similar league names are
  not silently accepted. No popular-market ranking or liquidity cutoff is used
  for this bounded discovery universe.
- `listMarketCatalogue` retrieves native market/event/competition IDs, start
  time, runners and handicaps, description/rule text, and available market base
  commission metadata. An effective account commission rate remains unknown.
  Catalogue refresh defaults to 900 seconds. `BETFAIR_LOOKAHEAD_HOURS` accepts
  1–168; discovery queries one-day windows and reports partial coverage if any
  reaches its 200-market cap.
- `listMarketBook` requests three levels of `EX_BEST_OFFERS` without virtual
  prices, at most 20 market IDs per request. This remains below the official
  [market-data request weighting limit](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687478).
  It requests no account orders or positions. Native market/runner status,
  in-play state, bet delay, delayed-data flag, version and receipt clocks are
  retained.
- Omitted market/runner books have no quote; failed requests do not turn an old
  book into a fresh observation. BACK odds sort descending and LAY odds
  ascending. Size is available **stake**, not payout, liability, volume or shares.

The small adapter deliberately uses a four-method read-only allowlist instead
of adding `betfairlightweight` and its broader client surface. Its current
[market-data methods](https://github.com/betcode-org/betfair/blob/master/betfairlightweight/endpoints/betting.py)
were inspected against the official API. This keeps this stage dependency-free
and leaves transport replaceable if streaming later justifies a maintained SDK.

## Offline synthetic validation

The handwritten `fixtures/betfair/nba_synthetic.json` is test data, **not** an
observed Betfair listing or an arbitrage opportunity. It is used only when
explicitly selected, without credentials or network calls:

```bash
BETFAIR_ENABLED=1 BETFAIR_REPLAY_FILE="$PWD/fixtures/betfair/nba_synthetic.json" python3 server.py --port=8766 --no-open
```

Replay prices retain their fixture timestamps, carry a synthetic label and must
remain separate from live research records and alerts. Clearing the replay
setting returns to the configured real-data source; synthetic data is never an
automatic fallback when credentials or requests fail. The standard server still
runs its existing Kalshi/Polymarket collectors; only the Betfair source is
offline in this example. Unit tests exercise the complete replay with all
network transports disabled.

## Terminal, mapping and storage

The compact Markets table adds a Betfair BACK/LAY column when Betfair data is
available. **Exchange → Betfair** selects native runners and existing outcome
rows with an exact Betfair fixture attachment. Unmatched runners remain visible.
The Betfair details button shows native IDs, three price levels, stake currency,
source receipt times, mapping evidence and the corresponding Polymarket token.
**Betfair · setup** opens connection guidance when credentials are absent.
Data & research includes the same connection status and mapping counts.

NBA mappings reuse the existing team registry, exact outcome-token handling and
settlement evidence. A unique fixture within a 15-minute UTC tolerance can be
linked for research, but this does not establish payoff equivalence. Conflicting
overtime rules reject the link; incomplete exceptional-state, commission and
void/refund evidence stays unverified. Multiple counterparts remain ambiguous.
Rules and material metadata are fingerprinted, with mapping versions retained.
The mapper also inspects the saved native NBA Polymarket catalogue independently
of Kalshi matching. No complementary YES/NO token is inferred.

Native Betfair catalogues, changed books and mapping versions are stored in
dedicated `betfair_*` tables in `data/eventlens.sqlite3`. They do not enter the
binary-share `observations` table or its fee/P&L calculations. Repeated unchanged
books update the latest actual REST receipt time without duplicating historical
price levels. Failed requests retain original quote times. Synthetic books are
labelled and excluded from the default exchange history and startup cache.

The local read-only APIs are `/api/research/betfair` and
`/api/research/betfair/history?market_id=1.EXAMPLE&limit=250` (up to 1,000 changed
books). Browser reads make no Betfair API calls; the existing background worker
owns collection. These are research REST snapshots, not a trading clock.

Focused tests:

```bash
python3 -m unittest test_betfair_data test_betfair_mapping test_betfair_service
```

The tests cover auth boundaries, endpoint selection, request budgets, omitted
books, exact runner/handicap keys, native BACK/LAY sides, delayed/unknown modes,
secrets in error messages and explicit replay clocks. Live authentication,
actual NBA availability, account currency/commission and real settlement-rule
coverage need validation with the configured account. An absence of returned
markets is reported as coverage information, not replaced with sample prices.
