"""Cached, transport-independent platform taker-fee and BBO edge estimates.

Compile a market's fee metadata once, outside the quote update path. All money
and quantities stay Decimal until explicit JSON/report serialization. Missing
metadata is unknown, never an assumed free market. See docs/FEES.md for sources
and the single-price fill/account-rounding assumptions behind these estimates.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Optional

import liquidity as L

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")
MICRO = Decimal("0.000001")
PM_PRECISION = Decimal("0.00001")
KALSHI_TYPES = frozenset({"quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"})


def _decimal(value: Any, *, price: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError("missing or invalid number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid number") from exc
    if not number.is_finite() or number < ZERO or (price and number > ONE):
        raise ValueError("price must be 0..1 and quantity finite and nonnegative")
    return number


def _text(value: Optional[Decimal]) -> Optional[str]:
    return format(value, "f") if value is not None else None


def _mapping(value: Any) -> Mapping:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class FeeSchedule:
    model: str = "unknown"
    rate: Decimal = ZERO
    exponent: Decimal = ONE
    multiplier: Decimal = ONE
    # Unknown account type uses the coarser non-direct member precision.
    balance_precision: Decimal = CENT
    status: str = "unknown"
    reason: str = "missing_fee_metadata"
    source: str = ""
    checked_at: str = ""

    @property
    def known(self) -> bool:
        return self.status == "known"

    def to_dict(self) -> dict:
        return {"model": self.model, "rate": _text(self.rate), "exponent": _text(self.exponent),
                "multiplier": _text(self.multiplier), "balance_precision": _text(self.balance_precision),
                "status": self.status, "reason": self.reason, "source": self.source,
                "checked_at": self.checked_at}


def compile_schedule(venue: str, extra: Any = None) -> FeeSchedule:
    """Validate normalized ``taker_fee`` metadata, or the venue's raw fields.

    Accepted raw metadata: Kalshi ``fee_type`` / ``fee_multiplier`` (including
    ``_kalshi_series``); Polymarket ``feesEnabled`` / ``feeSchedule``. Legacy
    base_fee/takerBaseFee fields alone cannot identify the active fee curve.
    Unknown and unsupported metadata return an unknown schedule without raising.
    """
    if isinstance(extra, FeeSchedule):
        return extra
    raw = _mapping(extra)
    normalized = _mapping(raw.get("taker_fee"))
    if not normalized and "model" in raw:
        normalized = raw
    source = str(normalized.get("source", raw.get("fee_source", "")) or "")
    checked = str(normalized.get("checked_at", raw.get("fee_checked_at", "")) or "")
    unknown = lambda reason: FeeSchedule(reason=reason, source=source, checked_at=checked)
    if venue not in {"kalshi", "polymarket_intl"}:
        return unknown("unsupported_venue")
    if not raw:
        return unknown("missing_fee_metadata")
    try:
        if normalized:
            if (normalized.get("status") in {"unknown", "unavailable", "stale"}
                    or normalized.get("model") == "unknown"):
                return unknown(str(normalized.get("reason", "unknown_fee_metadata")))
            model = normalized.get("model")
            data = normalized
        elif venue == "kalshi":
            data = _mapping(raw.get("_kalshi_series")) or raw
            model = "kalshi_quadratic" if data.get("fee_type") in KALSHI_TYPES else "unknown"
        elif venue == "polymarket_intl":
            if raw.get("feesEnabled", raw.get("fees_enabled")) is False:
                model, data = "zero", raw
            else:
                data = _mapping(raw.get("feeSchedule", raw.get("fee_schedule")))
                model = "polymarket_curve" if data else "unknown"
        else:
            return unknown("unsupported_venue")
        common = {"status": "known", "reason": "", "source": source, "checked_at": checked}
        if model == "zero":
            return FeeSchedule(model="zero", **common)
        if model == "kalshi_quadratic" and venue == "kalshi":
            multiplier = _decimal(data.get("multiplier", data.get("fee_multiplier")))
            precision = _decimal(data.get("balance_precision", raw.get("fee_balance_precision", CENT)))
            if precision not in {CENT, Decimal("0.0001")}:
                return unknown("unsupported_balance_precision")
            return FeeSchedule(model=model, rate=Decimal("0.07"), multiplier=multiplier,
                               balance_precision=precision, **common)
        if model == "polymarket_curve" and venue == "polymarket_intl":
            rate, exponent = _decimal(data.get("rate")), _decimal(data.get("exponent"))
            # Decimal supports the documented fractional exponents as well.
            if exponent > 16 or rate > ONE:
                return unknown("unsupported_fee_curve")
            return FeeSchedule(model=model, rate=rate, exponent=exponent, **common)
        return unknown("unsupported_fee_model")
    except ValueError:
        return unknown("invalid_fee_metadata")


@dataclass(frozen=True)
class FeeEstimate:
    status: str
    reason: str = ""
    amount: Optional[Decimal] = None
    model_fee: Optional[Decimal] = None
    trade_fee: Optional[Decimal] = None
    rounding_fee: Optional[Decimal] = None

    def to_dict(self) -> dict:
        return {"status": self.status, "reason": self.reason, "amount": _text(self.amount),
                "model_fee": _text(self.model_fee), "trade_fee": _text(self.trade_fee),
                "rounding_fee": _text(self.rounding_fee)}


def estimate_taker_fee(schedule: FeeSchedule, price: Any, quantity: Any = ONE,
                       *, side: str = "buy") -> FeeEstimate:
    """One order at one price, without a pre-existing rounding accumulator.

    Kalshi aligns signed cash flow as documented (sell and buy can differ for
    fractional cost); Polymarket uses its explicit curve and 5dp fee precision.
    This excludes account-specific broker/builder charges and rebate programs.
    """
    if side not in {"buy", "sell"}:
        return FeeEstimate("unavailable", "invalid_side")
    try:
        p, q = _decimal(price, price=True), _decimal(quantity)
    except ValueError:
        return FeeEstimate("unavailable", "invalid_price_or_quantity")
    if not schedule.known:
        return FeeEstimate("unavailable", schedule.reason)
    if q == ZERO or schedule.model == "zero":
        return FeeEstimate("estimated", amount=ZERO, model_fee=ZERO, trade_fee=ZERO, rounding_fee=ZERO)
    try:
        if schedule.model == "kalshi_quadratic":
            model = q * schedule.rate * schedule.multiplier * p * (ONE - p)
            trade = model.quantize(MICRO, rounding=ROUND_CEILING)
            revenue = q * p * (ONE if side == "sell" else -ONE)
            aligned = ((revenue - trade) / schedule.balance_precision).to_integral_value(
                rounding=ROUND_FLOOR) * schedule.balance_precision
            rounding = revenue - trade - aligned
            return FeeEstimate("estimated", amount=trade + rounding, model_fee=model,
                               trade_fee=trade, rounding_fee=rounding)
        if schedule.model == "polymarket_curve":
            model = q * schedule.rate * ((p * (ONE - p)) ** schedule.exponent)
            # The docs state amounts below the smallest charged fee round to zero.
            amount = ZERO if model < PM_PRECISION else model.quantize(PM_PRECISION, rounding=ROUND_HALF_UP)
            return FeeEstimate("estimated", amount=amount, model_fee=model,
                               trade_fee=amount, rounding_fee=amount - model)
    except (InvalidOperation, OverflowError):
        return FeeEstimate("unavailable", "invalid_fee_curve")
    return FeeEstimate("unavailable", "unsupported_fee_model")


@dataclass(frozen=True)
class TakerEdge:
    status: str
    reason: str = ""
    quantity: Decimal = ONE
    gross_gap: Optional[Decimal] = None
    sell_fee: Optional[Decimal] = None
    buy_fee: Optional[Decimal] = None
    total_fees: Optional[Decimal] = None
    net_gap: Optional[Decimal] = None
    net_total: Optional[Decimal] = None
    top_size: Optional[Decimal] = None
    top_quantity: Optional[Decimal] = None
    top_net_total: Optional[Decimal] = None
    top_net_gap: Optional[Decimal] = None

    @property
    def net_positive(self) -> bool:
        return self.net_total is not None and self.net_total > ZERO

    def to_dict(self) -> dict:
        result = {key: _text(getattr(self, key)) for key in (
            "quantity", "gross_gap", "sell_fee", "buy_fee", "total_fees", "net_gap", "net_total",
            "top_size", "top_quantity", "top_net_total", "top_net_gap")}
        return {**result, "status": self.status, "reason": self.reason,
                "net_positive": self.net_positive, "depth_confirmed": self.top_size is not None,
                "within_depth": None if self.top_size is None else self.quantity <= self.top_size}


def taker_edge(sell_schedule: FeeSchedule, sell_price: Any, buy_schedule: FeeSchedule,
               buy_price: Any, quantity: Any = ONE, *, sell_size: Any = None,
               buy_size: Any = None) -> TakerEdge:
    """Pure Decimal edge calculation suitable for an indexed quote update path.

    Top capacity is the smaller displayed BBO quantity, floored to whole paired
    contracts for the size estimate. It does not assert inventory, minimum-order
    validation, freshness, executable matching rules, or simultaneous execution.
    """
    try:
        bid, ask, q = _decimal(sell_price, price=True), _decimal(buy_price, price=True), _decimal(quantity)
        if q <= ZERO:
            raise ValueError("quantity must be positive")
    except ValueError:
        return TakerEdge("unavailable", "invalid_price_or_quantity")
    size = None
    if sell_size not in (None, "") and buy_size not in (None, ""):
        try:
            size = min(_decimal(sell_size), _decimal(buy_size))
        except ValueError:
            pass
    base = {"quantity": q, "gross_gap": bid - ask, "top_size": size,
            "top_quantity": size.to_integral_value(rounding=ROUND_FLOOR) if size is not None else None}
    sell = estimate_taker_fee(sell_schedule, bid, q, side="sell")
    buy = estimate_taker_fee(buy_schedule, ask, q, side="buy")
    if sell.amount is None or buy.amount is None:
        reasons = [f"{side}:{fee.reason}" for side, fee in (("sell", sell), ("buy", buy)) if fee.amount is None]
        return TakerEdge("unavailable", ";".join(reasons), sell_fee=sell.amount, buy_fee=buy.amount, **base)
    fees = sell.amount + buy.amount
    net = q * (bid - ask) - fees
    top_net = top_gap = None
    top_quantity = base["top_quantity"]
    if top_quantity is not None and top_quantity > ZERO:
        top_sell = estimate_taker_fee(sell_schedule, bid, top_quantity, side="sell")
        top_buy = estimate_taker_fee(buy_schedule, ask, top_quantity, side="buy")
        if top_sell.amount is not None and top_buy.amount is not None:
            top_net = top_quantity * (bid - ask) - top_sell.amount - top_buy.amount
            top_gap = top_net / top_quantity
    return TakerEdge("estimated", sell_fee=sell.amount, buy_fee=buy.amount,
                     total_fees=fees, net_gap=net / q, net_total=net,
                     top_net_total=top_net, top_net_gap=top_gap, **base)


def estimate_cross(sell_venue: str, sell_price: Any, sell_extra: Any,
                   buy_venue: str, buy_price: Any, buy_extra: Any,
                   quantity: Any = ONE, *, sell_size: Any = None, buy_size: Any = None) -> dict:
    """Report/diagnostic wrapper; compile and serialize away from the hot path."""
    return taker_edge(compile_schedule(sell_venue, sell_extra), sell_price,
                      compile_schedule(buy_venue, buy_extra), buy_price, quantity,
                      sell_size=sell_size, buy_size=buy_size).to_dict()


def calculate_taker_cross(row: Mapping, buy_venue: str, sell_venue: str,
                          quantity: Any = ONE) -> dict:
    """Read flat terminal/CSV fields; fee schedules may be dicts or JSON strings."""
    return estimate_cross(
        sell_venue, row.get(f"{sell_venue}_bid"),
        row.get(f"{sell_venue}_fee_schedule") or row.get(f"{sell_venue}_fee_metadata"),
        buy_venue, row.get(f"{buy_venue}_ask"),
        row.get(f"{buy_venue}_fee_schedule") or row.get(f"{buy_venue}_fee_metadata"), quantity,
        sell_size=row.get(f"{sell_venue}_bid_size"), buy_size=row.get(f"{buy_venue}_ask_size"))


@dataclass(frozen=True)
class SizedTakerEdge:
    """Screening estimate at a requested size, bounded by observed BBO depth.

    Capital is an explicitly labeled fully collateralized paired-position
    estimate: buy cost + (1 - sell bid) collateral + both taker fees. It does not
    assume that selling inventory is free capital, nor validate shorting or a
    complementary NO order. Inventory and venue minimums remain separate checks.
    """
    edge: TakerEdge
    liquidity: L.RouteLiquidity
    desired_quantity: Optional[Decimal] = None
    sized_quantity: Optional[Decimal] = None
    quantity_capped: bool = False
    cap_reasons: tuple[str, ...] = ()
    buy_cost: Optional[Decimal] = None
    sell_proceeds: Optional[Decimal] = None
    collateral_estimate: Optional[Decimal] = None
    capital_required: Optional[Decimal] = None
    minimum_order_status: str = "unknown"
    minimum_order_reason: str = "minimum_order_metadata_not_provided"

    @property
    def actionable_estimate(self) -> bool:
        return (self.sized_quantity is not None and self.sized_quantity > ZERO
                and self.edge.net_positive and self.liquidity.sufficient
                and self.minimum_order_status != "below_minimum")

    def to_dict(self) -> dict:
        edge = self.edge.to_dict()
        q = self.sized_quantity
        gross = q * self.edge.gross_gap if q is not None and self.edge.gross_gap is not None else None
        break_even = self.edge.total_fees / q if q and self.edge.total_fees is not None else None
        roi = (self.edge.net_total / self.capital_required
               if self.capital_required and self.edge.net_total is not None else None)
        return {**edge,
                **{name: _text(getattr(self, name)) for name in (
                    "desired_quantity", "sized_quantity", "buy_cost", "sell_proceeds",
                    "collateral_estimate", "capital_required")},
                "quantity_capped": self.quantity_capped, "cap_reasons": list(self.cap_reasons),
                "gross_total": _text(gross), "break_even_gap": _text(break_even),
                "fees_per_contract": _text(break_even),
                "net_per_contract_cents": _text(self.edge.net_gap * 100) if self.edge.net_gap is not None else None,
                "return_on_capital": _text(roi), "return_on_capital_percent": _text(roi * 100) if roi is not None else None,
                "capital_basis": "fully_collateralized_pair_estimate",
                "liquidity_status": self.liquidity.status, "liquidity_reason": self.liquidity.reason,
                "paired_top_quantity": _text(self.liquidity.paired_quantity),
                "paired_top_notional_usd": _text(self.liquidity.limiting_notional_usd),
                "sell_top_notional_usd": _text(self.liquidity.sell_notional_usd),
                "buy_top_notional_usd": _text(self.liquidity.buy_notional_usd),
                "min_top_notional_usd": _text(self.liquidity.min_top_notional_usd),
                "minimum_order_status": self.minimum_order_status,
                "minimum_order_reason": self.minimum_order_reason,
                "actionable_estimate": self.actionable_estimate,
                "execution_validated": False}


def sized_taker_edge(sell_schedule: FeeSchedule, sell_price: Any,
                     buy_schedule: FeeSchedule, buy_price: Any, *,
                     desired_contracts: Any = None, sell_size: Any = None, buy_size: Any = None,
                     policy: Optional[L.LiquidityPolicy] = None, max_capital_usd: Any = None,
                     sell_min_order_size: Any = None, buy_min_order_size: Any = None,
                     sell_min_order_notional_usd: Any = None,
                     buy_min_order_notional_usd: Any = None) -> SizedTakerEdge:
    """Calculate order-level fees after whole-contract depth and budget sizing.

    ``desired_contracts='top'`` uses confirmed paired top capacity. Unknown depth
    leaves size and size-dependent PnL unknown. Known venue minimums gate the
    estimate; missing constraints are reported as unknown and never invented.
    Compile ``policy`` and fee schedules once for an incremental update engine.
    """
    policy = policy if policy is not None else L.LiquidityPolicy()
    route = policy.assess_route(sell_price, buy_price, sell_size, buy_size)
    bid, ask = L.number(sell_price, price=True), L.number(buy_price, price=True)
    requested = policy.desired_contracts if desired_contracts is None else desired_contracts
    use_top = requested == "top"
    desired = route.paired_quantity if use_top else L.number(requested)
    gap = bid - ask if bid is not None and ask is not None else None

    def unavailable(reason: str, *, sized: Optional[Decimal] = None,
                    capped: bool = False, caps: tuple[str, ...] = ()) -> SizedTakerEdge:
        return SizedTakerEdge(TakerEdge("unavailable", reason, quantity=sized or desired or ONE,
                                      gross_gap=gap, top_size=route.paired_quantity,
                                      top_quantity=route.paired_quantity), route,
                              desired_quantity=desired, sized_quantity=sized,
                              quantity_capped=capped, cap_reasons=caps)

    if bid is None or ask is None:
        return unavailable("invalid_price_or_quantity")
    if desired is None and not use_top or desired is not None and desired <= ZERO:
        return unavailable("invalid_desired_quantity")
    if route.paired_quantity is None:
        return unavailable("unconfirmed_top_depth")
    if route.paired_quantity <= ZERO:
        return unavailable("insufficient_whole_contract_depth", sized=ZERO)
    q = min(desired, route.paired_quantity).to_integral_value(rounding=ROUND_FLOOR)
    caps = []
    if q < desired:
        caps.append("top_depth" if route.paired_quantity < desired else "whole_contracts")
    if q <= ZERO:
        return unavailable("insufficient_whole_contract_quantity", sized=ZERO, capped=True,
                           caps=tuple(caps))
    budget_raw = policy.max_capital_usd if max_capital_usd is None else max_capital_usd
    budget = None if budget_raw is None else L.number(budget_raw)
    if budget_raw is not None and (budget is None or budget <= ZERO):
        return unavailable("invalid_capital_budget")

    def at_size(quantity: Decimal) -> tuple[TakerEdge, Optional[Decimal]]:
        estimate = taker_edge(sell_schedule, bid, buy_schedule, ask, quantity)
        capital = (quantity * (ask + ONE - bid) + estimate.total_fees
                   if estimate.total_fees is not None else None)
        return estimate, capital

    if budget is not None:
        _, needed = at_size(q)
        if needed is None:
            return unavailable("unknown_fees_for_budget")
        if needed > budget:
            # Whole-contract cash requirements are monotone, including account
            # rounding. Binary search keeps budget sizing logarithmic in depth.
            lower, upper = 0, int(q)
            while lower < upper:
                midpoint = (lower + upper + 1) // 2
                _, needed = at_size(Decimal(midpoint))
                if needed <= budget:
                    lower = midpoint
                else:
                    upper = midpoint - 1
            q = Decimal(lower)
            caps.append("capital_budget")
            if q <= ZERO:
                return unavailable("budget_below_one_contract", sized=ZERO, capped=True,
                                   caps=tuple(caps))
    edge = taker_edge(sell_schedule, bid, buy_schedule, ask, q,
                      sell_size=sell_size, buy_size=buy_size)
    buy_cost, proceeds, collateral = q * ask, q * bid, q * (ONE - bid)
    capital = buy_cost + collateral + edge.total_fees if edge.total_fees is not None else None
    constraints = (("sell", "size", sell_min_order_size, q),
                   ("buy", "size", buy_min_order_size, q),
                   ("sell", "notional", sell_min_order_notional_usd, proceeds),
                   ("buy", "notional", buy_min_order_notional_usd, buy_cost))
    known = []
    invalid = []
    below = []
    for side, kind, raw, actual in constraints:
        if raw is None or raw == "":
            continue
        value = L.number(raw)
        if value is None:
            invalid.append(f"{side}_{kind}")
        else:
            known.append((side, kind))
            if actual < value:
                below.append(f"{side}_{kind}")
    minimum_status = "below_minimum" if below else "unknown" if not known or invalid else "known_minimums_met"
    minimum_reason = ("below:" + ",".join(below) if below else
                      "invalid_metadata:" + ",".join(invalid) if invalid else
                      "minimum_order_metadata_not_provided" if not known else
                      "provided_minimums_met_other_constraints_unverified")
    return SizedTakerEdge(edge, route, desired, q, bool(caps), tuple(caps), buy_cost,
                          proceeds, collateral, capital, minimum_status, minimum_reason)


def estimate_sized_cross(sell_venue: str, sell_price: Any, sell_extra: Any,
                         buy_venue: str, buy_price: Any, buy_extra: Any, *,
                         desired_contracts: Any = None, sell_size: Any = None, buy_size: Any = None,
                         policy: Optional[L.LiquidityPolicy] = None,
                         min_top_notional_usd: Any = "100", max_capital_usd: Any = None,
                         sell_min_order_size: Any = None, buy_min_order_size: Any = None,
                         sell_min_order_notional_usd: Any = None,
                         buy_min_order_notional_usd: Any = None) -> dict:
    """Serialization wrapper for report and API callers, away from quote ingestion."""
    policy = policy if policy is not None else L.LiquidityPolicy(min_top_notional_usd=min_top_notional_usd)
    return sized_taker_edge(
        compile_schedule(sell_venue, sell_extra), sell_price,
        compile_schedule(buy_venue, buy_extra), buy_price, desired_contracts=desired_contracts,
        sell_size=sell_size, buy_size=buy_size, policy=policy, max_capital_usd=max_capital_usd,
        sell_min_order_size=sell_min_order_size, buy_min_order_size=buy_min_order_size,
        sell_min_order_notional_usd=sell_min_order_notional_usd,
        buy_min_order_notional_usd=buy_min_order_notional_usd).to_dict()


def calculate_sized_taker_cross(row: Mapping, buy_venue: str, sell_venue: str, *,
                                desired_contracts: Any = None,
                                min_top_notional_usd: Any = "100",
                                max_capital_usd: Any = None,
                                policy: Optional[L.LiquidityPolicy] = None) -> dict:
    """Read report/CSV fields with exact Decimal sizing and separate fee totals."""
    return estimate_sized_cross(
        sell_venue, row.get(f"{sell_venue}_bid"),
        row.get(f"{sell_venue}_fee_schedule") or row.get(f"{sell_venue}_fee_metadata"),
        buy_venue, row.get(f"{buy_venue}_ask"),
        row.get(f"{buy_venue}_fee_schedule") or row.get(f"{buy_venue}_fee_metadata"),
        desired_contracts=desired_contracts, min_top_notional_usd=min_top_notional_usd,
        max_capital_usd=max_capital_usd, policy=policy,
        sell_size=row.get(f"{sell_venue}_bid_size"), buy_size=row.get(f"{buy_venue}_ask_size"),
        sell_min_order_size=row.get(f"{sell_venue}_minimum_order_size"),
        buy_min_order_size=row.get(f"{buy_venue}_minimum_order_size"),
        sell_min_order_notional_usd=row.get(f"{sell_venue}_minimum_order_notional_usd"),
        buy_min_order_notional_usd=row.get(f"{buy_venue}_minimum_order_notional_usd"))
