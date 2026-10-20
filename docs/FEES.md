# Platform taker fees and edge estimates

Primary sources checked **21 October 2026**. Metadata for each market takes
precedence over category examples. This module estimates the exchange's platform
taker fees on both legs; it does not send orders.

## Kalshi

The published general taker model is `0.07 × multiplier × quantity × p × (1-p)`.
The series API supplies `fee_type` and `fee_multiplier`; a multiplier of zero is
valid. Unsupported fee types and missing multipliers remain unknown. An MLB or
other series override must come from its metadata rather than a universal rate.
Sources: [official fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf),
[Get Series](https://docs.kalshi.com/api-reference/market/get-series).

The API's rounding specification rounds the model trade fee upward to $0.000001,
then floors signed revenue minus that fee to the account's balance precision.
Direct members use $0.0001; non-direct members use $0.01. Buy revenue is negative
and sell revenue is positive, so fractional notional can produce different fees.
Our default is the coarser $0.01 precision because the terminal has no account
type. Set `balance_precision: "0.0001"` only for a known direct member. Estimates
use one fill at the BBO and no existing rounding accumulator; exchange rebates
across several fills can change the final amount.
Source: [Fee Rounding](https://docs.kalshi.com/getting_started/fee_rounding).

## Polymarket International

Read each market's `feesEnabled` and `feeSchedule.rate` / `feeSchedule.exponent`.
Explicitly disabled fees are known zero fees; missing metadata is unknown.
Legacy `takerBaseFee`, `taker_base_fee`, and `/fee-rate` values alone do not
identify the active curve, and are not substituted for its parameters.
Sources: [Market Details](https://docs.polymarket.com/market-data/market-details),
[CLOB market info](https://docs.polymarket.com/api-reference/markets/get-clob-market-info).

The curve is `quantity × rate × (p × (1-p)) ** exponent`. The current published
sports example has rate `0.05` and exponent `1`, giving $1.25 for 100 shares at
$0.50. Fee precision is five decimal places; documented subminimum fees below
$0.00001 are zero. For the estimate we use nearest rounding with half-up ties;
the public precision text does not specify the tie rule. Current SDK buy
budgeting adds the cash fee to purchase cost. These calculations follow that
cash model, not legacy share-deduction formulas.
Sources: [Trading Fees](https://docs.polymarket.com/trading/fees),
[official Python SDK fee calculation](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/fees.py),
[official SDK fee examples](https://github.com/Polymarket/py-clob-client-v2/blob/main/tests/test_fee_calculations.py).

## Calculation and integration

Let `q` be the actual paired order quantity, `b` the selected sell bid and `a`
the distinct venue's buy ask:

```text
gross_gap = b - a                       dollars per paired contract
gross_total = q × gross_gap             dollars for the whole pair of orders
total_fees = sell_fee(q, b) + buy_fee(q, a)
net_total = gross_total - total_fees     estimated total dollars after taker fees
net_gap = net_total / q                 dollars per paired contract
break_even_gap = total_fees / q         gross spread required to cover these fees
```

The table displays `gross_gap`, `total_fees / q` and `net_gap` in **cents per
contract**, and `net_total` in **total dollars**. A 2¢ net edge on 100 filled
paired contracts corresponds to $2 estimated net, rather than 2¢ total profit.
Gross cross flags remain independent of net positivity. Missing fees on either
leg make net PnL unavailable while retaining the gross quote difference.

The UI and liquidity policy default to **100 requested contracts**. The UI also
offers 1, 10, 1,000 and Top depth. Actual quantity is capped to the smaller
displayed sell-bid/buy-ask quantity and floored to whole contracts. Both order
fees are recalculated at that actual size. Multiplying a rounded one-contract
fee by 100 can overstate the larger order's cost. Unknown depth leaves actual
quantity and sized PnL unavailable; it never becomes zero-cost capacity.

Illustrations using 100 contracts, Kalshi's general multiplier 1 with cent
balance precision, and Polymarket's sports rate 0.05 / exponent 1:

| Sell Kalshi bid / buy Polymarket ask | Gross | Kalshi fee | Polymarket fee | Estimated net |
| --- | ---: | ---: | ---: | ---: |
| 52¢ / 50¢ | $2.00 | $1.75 | $1.25 | **−$1.00** |
| 53¢ / 50¢ | $3.00 | $1.75 | $1.25 | **$0.00** |
| 12¢ / 10¢ | $2.00 | $0.74 | $0.45 | **+$0.81** |

These are fee-model examples, not current opportunities or a universal spread
rule. In the final row, at least 1,000 displayed paired contracts at those
prices are needed to clear the terminal's default $100 top-notional bound on
the 10¢ leg, even if the desired trade itself is 100 contracts. All examples
assume sufficient depth, matching payouts and the order/account assumptions
above. Market-specific fees can change the result.

## Liquidity, order bounds and capital

The default shared policy requires **$100 of paired top-of-book notional on each
selected leg**. It first computes `top_q = floor(min(sell_bid_size,
buy_ask_size))`, then tests both `top_q × sell_bid` and `top_q × buy_ask` against
the bound. `paired_top_notional_usd` is their minimum. This tests available top
capacity, separately from the requested trade size. Volume, open interest and
total market liquidity cannot substitute for paired depth.

Fresh explicit low CLOB aggregate liquidity or a book whose two current sides
are both below the bound may avoid unnecessary quote requests. Both sides must
be small before screening out an instrument: one large side can still take
part in a cross. Aggregate and book timestamps are validated independently;
missing, stale, future or invalid measurements get a probe. The runtime
reprobes thin markets after 60 seconds so liquidity changes can restore them.
Unknown depth is hidden by the UI's default liquidity filter and can be shown
explicitly. It cannot create sized profit or an actionable estimate.

The capital estimate is explicitly **fully collateralized paired positions**:

```text
buy_cost = q × buy_ask
collateral_estimate = q × (1 - sell_bid)
capital_required = buy_cost + collateral_estimate + total_fees
return_on_capital = net_total / capital_required
```

This does not assume free existing inventory, prove a short is available, or
validate an executable complementary NO order. It is a labeled economic
capital estimate. Optional budget sizing uses this basis, includes both fees,
and finds the largest whole quantity within the budget and displayed depth.
Known minimum order quantity/notional constraints are checked **after** sizing;
missing constraints remain unknown. `actionable_estimate` is a screening flag,
and `execution_validated` remains false.

Polymarket order books supply `min_order_size` in shares; it is retained as
`minimum_order_size`. Gamma market details document `orderMinSize` as a USDC
minimum, retained separately as `minimum_order_notional_usd`. These distinct
constraints are not silently interchanged. The UI marks a known violation
**MIN** and excludes it from After fees +, while details retains the diagnostic
profit calculation. Sources: [Place Orders](https://docs.polymarket.com/trading/place-orders),
[Market Details](https://docs.polymarket.com/market-data/market-details).

These estimates exclude broker/builder charges, rebates not explicitly modeled,
slippage beyond the best level, financing and transfer costs. Inventory,
shorting, account collateral, venue order constraints and simultaneous fills
still require validation before execution. The terminal separately checks
mapping, settlement and quote freshness; conditional contingency differences
remain visible. No orders are sent.

## Developer integration

`compile_schedule(venue, extra)` validates metadata once. Normalized metadata is
stored in `Market.extra["taker_fee"]`, with `source` and `checked_at` retained:

```python
{"model": "kalshi_quadratic", "multiplier": "1", "balance_precision": "0.01"}
{"model": "polymarket_curve", "rate": "0.05", "exponent": 1}
{"model": "zero"}  # only from explicitly verified fee-free metadata
{"model": "unknown", "reason": "refresh_failed"}
```

`taker_edge` retains its one-contract default for diagnostic compatibility.
`sized_taker_edge` consumes compiled schedules and a compiled `LiquidityPolicy`
for actual sizing; `calculate_sized_taker_cross` reads report/CSV fields and
serializes the result. `desired_contracts="top"` selects available whole paired
top depth. `max_capital_usd` optionally caps the capital estimate. The browser
uses exact Python-computed preset estimates, keeping fee-model rounding out of
JavaScript rendering.

The streaming engine caches schedules and policy outside quote ingestion, then
recalculates only affected comparisons. Gross and fee-adjusted diagnostics stay
visible for thin markets, while `monitor_eligible` / `actionable_estimate` gate
screened signals. `update_fee_schedule` applies separately refreshed metadata;
pass an unknown schedule when refresh fails. `update_liquidity_policy` performs
an explicit configuration re-evaluation outside ingestion. Fee discovery,
network requests, JSON encoding and report generation stay outside updates.
