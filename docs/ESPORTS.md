# Esports coverage

Esports is the first separate category beyond physical sports. CS2 and League
of Legends use the existing fixture, market, quote, fee, and subscription
models. Finance and technology require their own identity and settlement
adapters before they can supply comparable markets.

The public catalogs and representative contracts were checked on 21 October
2026, New Zealand time:

| Game | Kalshi full-match series | Polymarket Gamma series |
| --- | --- | --- |
| Counter-Strike 2 | `KXCS2GAME` | `10310` |
| League of Legends | `KXLOLGAME` | `10311` |

Sources: [Kalshi sports catalog](https://external-api.kalshi.com/trade-api/v2/series?category=Sports),
[Gamma sports metadata](https://gamma-api.polymarket.com/sports),
[Kalshi CS2 events](https://external-api.kalshi.com/trade-api/v2/events?series_ticker=KXCS2GAME&status=open&with_nested_markets=true&with_milestones=true&limit=200),
[Kalshi LoL events](https://external-api.kalshi.com/trade-api/v2/events?series_ticker=KXLOLGAME&status=open&with_nested_markets=true&with_milestones=true&limit=200),
[Gamma CS2 events](https://gamma-api.polymarket.com/events?series_id=10310&closed=false&limit=100),
[Gamma LoL events](https://gamma-api.polymarket.com/events?series_id=10311&closed=false&limit=100).

`niches.collect_esports()` discovers both games with the existing bounded
three-worker discovery pool. Pass `competitions=["cs2"]` to inspect one game.
Pagination follows every native event, including historical events with only
unresolved map props. An event marked open can contain a closed match-winner
contract; the market itself must also be open. Only the top-level moneyline
with its original event slug is retained from Gamma. Kalshi map series,
outrights, map handicaps, totals, and round winners are excluded.

Exact published team names and aliases establish identity. Gamma's CS2 event
team records currently use the legacy league value `csgo`; that value is
accepted exclusively for the verified CS2 series. Academy designations,
sponsor names, and roster variations are retained. The adapter does not infer
that a similar team name belongs to the same organization. Games have separate
fixture keys (`sport="esports"`, format `CS2` or `LOL`). Conflicting scheduled
starts remain in review.

The [Kalshi ESPORTSWIN contract](https://assets.kalshi.com/contract_terms/ESPORTSWIN.pdf)
distinguishes match, map, and round scopes. It uses the official result for the
specified scope, including counted replays and wins awarded after play begins.
When there are exactly two participants and no Tie strike, its tie formula
produces a half-dollar payout. Pre-start forfeits, cancellation, or excessive
postponement can produce a variable fair-price settlement. Gamma's sampled
match contracts instead specify 50–50 for pre-start forfeits and cancellation.
Current Gamma contracts use a published 14-calendar-day rescheduling deadline;
some older contracts use seven days. These policies and original evidence are
retained separately.

A normal-completion match therefore remains conditional. It does not establish
equivalent cancellation, postponement, roster, replay, or overtime treatment.
Unstated terms stay unknown; full-contract comparison remains reviewable.

Read-only validation of the first corpus produced 33 accepted cross-venue
fixtures and 66 outcome links; 15 candidate fixtures required review. These
counts are an observed snapshot, not guaranteed ongoing coverage or executable
opportunities. All 326 parsed outcome markets resolved to native subscription
instruments, including correct Gamma token order. Cold discovery completed in
approximately four seconds; this is independent of quote latency.

New categories should follow the same boundary: discover identities and rules
on the cold path, select a useful liquid universe, and retain native instrument
IDs for incremental quote updates. A category label alone does not authorize
cross-venue equivalence or order execution.
