# Research discovery coverage

The audited comparison pipeline uses league-specific discovery, strict fixture/outcome matching and settlement validation. Research previously inherited those comparison rows; regular historical recording also selects at most 25 liquid, validated pairs. These choices explain the sports concentration and absence of unmatched markets from research.

`market_catalog.MarketCatalog` now discovers public catalogs independently. It uses native [Kalshi events](https://docs.kalshi.com/api-reference/events/get-events) with nested markets and [Polymarket Gamma event keyset pagination](https://docs.polymarket.com/market-data/discover-markets). These endpoints give explicit continuation cursors, category/tag metadata and native outcome IDs. The existing PMXT adapter only exposes a small discovery suggestion interface; native discovery offers a clearer, bounded pagination contract here. PMXT remains the selected Polymarket book/history provider.

## Verified sample — 21 October 2026

One public page per exchange returned **3,060 distinct native contracts / 4,318 outcome records**. These are cached discovery counts, not counts of matched, liquid or actively quoted markets.

| Category | Kalshi contracts | Polymarket contracts |
| --- | ---: | ---: |
| Politics | 237 | 889 |
| Economics | 808 | 14 |
| Sports | 442 | 89 |
| Geopolitics | 0 | 155 |
| Technology | 137 | 6 |
| Entertainment | 113 | 4 |
| Crypto | 0 | 82 |
| Finance | 42 | 10 |
| Weather | 21 | 0 |
| Other | 0 | 9 |
| Health | 2 | 0 |
| **Total** | **1,802** | **1,258** |

Kalshi returned 200 events; Polymarket silently returned 100 despite a requested limit of 200. Both returned continuation cursors. The collector follows cursor presence, rather than assuming a short page means exhaustion. The initial sample is explicitly incomplete.

Two selected non-sports books were also fetched and saved through the real application: Kalshi's Taiwan travel-warning contract through its native public source, and a Polymarket presidential-election outcome through PMXT. Exact outcome IDs persisted, the unmatched status remained unchanged, and no counterpart observation was fabricated. The sampled Polymarket book was one-sided; the missing ask remained unavailable.

## Operation and limits

- Use **More data tools → Refresh market catalog** in Research. Each scan attempts at most six pages per exchange and saves its continuation cursor. Subsequent scans resume; completed traversal starts over on the next scan. The shared HTTP transport may retry failed logical page calls. Page load and local queries make no discovery requests.
- Categories come from native categories/tags, with a small presentation mapping; unknown metadata remains Other. Native evidence and full metadata are retained. Category classification never excludes a market.
- The coverage table counts distinct native market IDs, separately from outcome records. It reports refresh time, pagination limits and status counts. Catalog records are cached, may become stale and are not removed merely because a bounded scan did not see them.
- Open parent events can contain closed children. The sample includes 200 explicitly closed contracts and 679 with unknown status. These remain available for metadata/history research; explicit closed status disables live-book capture in the UI.
- Six Polymarket outcomes lacked reliable exact token IDs. They remain discoverable; live-book controls stay disabled. Outcome probabilities are never substituted for bids or asks.
- Kalshi discovery exposes the YES side of each binary contract, clearly labelled. Polymarket preserves all returned outcome labels and exact token mappings. No new equivalence claims or cross-exchange matches are created.
- Discovery does not apply liquidity filters or create quote frames. The separate capped quote worker selects liquidity candidates; settlement checks and alert liquidity gates remain enforced. Selected independent outcomes can use the existing read/save-book and sampled-price-history tools.
- This is open-parent discovery, not a complete historical archive. No WebSockets, strategies, backtests, trading or new dependencies were added.

The terminal now merges validated comparisons and independent catalog outcomes into one **Markets** index, using exact venue/outcome IDs to suppress duplicate legs. Exact stated question/outcome/expiry candidates can share a **Review** row but cannot generate alerts. **Alerts** requires a validated fresh cross, measured route liquidity, known size and venue minimum gates; gross crosses and positive results after both fees are labelled separately. Thresholds are recomputed at request time and expired quotes withdraw in the browser.

Catalog quote collection is separate from unrestricted discovery: a maximum of 100 outcomes per cycle includes up to 20 rotating probes with 60-second backoff. Provider aggregate liquidity is only a selection hint; actual displayed depth still controls paired eligibility. Some native Kalshi bulk BBOs have no quantities: prices are shown but missing depth remains unknown and cannot establish executable liquidity. The catalog remains a bounded discovery sample, not a complete exchange mirror.
