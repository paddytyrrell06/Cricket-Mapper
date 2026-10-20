# Platform taker fees and edge estimates

Primary sources checked **20 October 2026**. Metadata for each market takes
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

`net_total = quantity × (sell_bid - buy_ask) - sell_fee - buy_fee`.
`net_gap` is that total divided by quantity, in dollars per paired contract.
Gross cross flags remain independent of net positivity. Unknown fees on either
leg make net edge unavailable, while preserving the gross quote difference.

The default is an indicative one-contract estimate. The top-size estimate uses
the smaller displayed sell-bid / buy-ask quantity, floors to whole paired
contracts, and recalculates fees for that order size. It does not multiply a
rounded one-contract fee by size. Missing depth stays unknown. These quantities
do not validate venue minimum orders, inventory, shorting capability, fees from
a broker or builder, slippage, financing, transfer costs, settlement alignment,
or the feasibility of filling both venues together. The terminal separately
checks mapping, settlement and quote freshness.

`compile_schedule(venue, extra)` validates metadata once. Normalized metadata is
stored in `Market.extra["taker_fee"]`, with `source` and `checked_at` retained:

```python
{"model": "kalshi_quadratic", "multiplier": "1", "balance_precision": "0.01"}
{"model": "polymarket_curve", "rate": "0.05", "exponent": 1}
{"model": "zero"}  # only from explicitly verified fee-free metadata
{"model": "unknown", "reason": "refresh_failed"}
```

`taker_edge` consumes immutable compiled schedules and Decimal prices/quantities.
The streaming engine caches schedules per instrument and recalculates only
affected comparisons. `update_fee_schedule` applies separately refreshed
metadata; pass an unknown schedule when refresh fails. Fee discovery, network
requests, JSON encoding, and report generation stay outside quote updates.
