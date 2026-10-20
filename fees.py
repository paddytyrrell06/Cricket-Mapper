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
