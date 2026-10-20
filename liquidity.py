"""Shared, transport-independent liquidity screening for market-data work.

Displayed top-of-book dollars, total market liquidity, traded volume and open
interest are different measurements. Only the first gates a paired route. A
fresh, explicitly reported low aggregate CLOB liquidity value may conservatively
avoid a book request; missing and stale observations always get a probe.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any, Optional

ZERO = Decimal("0")
ONE = Decimal("1")


def number(value: Any, *, price: bool = False) -> Optional[Decimal]:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not result.is_finite() or result < ZERO or (price and result > ONE):
        return None
    return result


def _text(value: Optional[Decimal]) -> Optional[str]:
    return format(value, "f") if value is not None else None


def _stamp(value: Any) -> Optional[datetime]:
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class RouteLiquidity:
    status: str
    reason: str
    min_top_notional_usd: Decimal
    paired_quantity: Optional[Decimal] = None
    sell_notional_usd: Optional[Decimal] = None
    buy_notional_usd: Optional[Decimal] = None
    limiting_notional_usd: Optional[Decimal] = None

    @property
    def sufficient(self) -> bool:
        return self.status == "sufficient"

    def to_dict(self) -> dict:
        return {"status": self.status, "reason": self.reason,
                **{field: _text(getattr(self, field)) for field in (
                    "min_top_notional_usd", "paired_quantity", "sell_notional_usd",
                    "buy_notional_usd", "limiting_notional_usd")}}


@dataclass(frozen=True)
class DiscoveryDecision:
    skip_quotes: bool = False
    reason: str = "unknown_liquidity_probe_required"
    verified_notional_usd: Optional[Decimal] = None
    source: str = ""

    def to_dict(self) -> dict:
        return {"skip_quotes": self.skip_quotes, "reason": self.reason,
                "verified_notional_usd": _text(self.verified_notional_usd), "source": self.source}


@dataclass(frozen=True)
class LiquidityPolicy:
    min_top_notional_usd: Decimal = Decimal("100")
    desired_contracts: Decimal = Decimal("100")
    max_capital_usd: Optional[Decimal] = None

    def __post_init__(self):
        for name in ("min_top_notional_usd", "desired_contracts", "max_capital_usd"):
            raw = getattr(self, name)
            if name == "max_capital_usd" and raw is None:
                continue
            value = number(raw)
            if value is None or (name != "min_top_notional_usd" and value <= ZERO):
                raise ValueError(f"{name} must be a finite {'nonnegative' if name == 'min_top_notional_usd' else 'positive'} number")
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict:
        return {name: _text(getattr(self, name)) for name in (
            "min_top_notional_usd", "desired_contracts", "max_capital_usd")}

    def assess_route(self, sell_price: Any, buy_price: Any, sell_size: Any,
                     buy_size: Any) -> RouteLiquidity:
        """Require the smaller displayed paired size to clear $ bounds on both legs.

        Whole contracts are used because a route containing Kalshi cannot assume
        fractional-contract support. No orderbook walk or volume substitution.
        """
        bid, ask = number(sell_price, price=True), number(buy_price, price=True)
        sizes = number(sell_size), number(buy_size)
        if bid is None or ask is None:
            return RouteLiquidity("unknown", "missing_or_invalid_price", self.min_top_notional_usd)
        if any(size is None for size in sizes):
            return RouteLiquidity("unknown", "missing_or_invalid_top_depth", self.min_top_notional_usd)
        quantity = min(sizes).to_integral_value(rounding=ROUND_FLOOR)
        sell_notional, buy_notional = quantity * bid, quantity * ask
        limiting = min(sell_notional, buy_notional)
        sufficient = quantity > ZERO and limiting >= self.min_top_notional_usd
        return RouteLiquidity("sufficient" if sufficient else "insufficient",
                              "meets_top_notional_bound" if sufficient else "below_top_notional_bound",
                              self.min_top_notional_usd, quantity, sell_notional, buy_notional, limiting)

    def discovery_decision(self, venue: str, extra: Any = None, *, bid: Any = None,
                           ask: Any = None, bid_size: Any = None, ask_size: Any = None,
                           observed_at: Any = None, bbo_observed_at: Any = None,
                           now: Optional[datetime] = None,
                           max_age_seconds: float = 300) -> DiscoveryDecision:
        """Prune only fresh explicit insufficient measurements, otherwise probe.

        Without BBO arguments, ``observed_at`` can timestamp a fresh discovery
        response. With BBO arguments it timestamps that book; aggregate liquidity
        then needs its own ``liquidity_observed_at`` / ``_collected_at_utc``.
        ``bbo_observed_at`` explicitly overrides only the book's timestamp.
        Saved low-liquidity observations are reprobed after the age limit.
        """
        if self.min_top_notional_usd == ZERO:
            return DiscoveryDecision(reason="liquidity_filter_disabled")
        raw = extra if isinstance(extra, Mapping) else {}
        current = _stamp(now or datetime.now(timezone.utc))
        max_age = number(max_age_seconds)
        if current is None or max_age is None or max_age <= ZERO:
            return DiscoveryDecision(reason="unknown_liquidity_time_probe_required")
        explicit_bbo = any(value is not None for value in (bid, ask, bid_size, ask_size))
        aggregate_stamp = raw.get("liquidity_observed_at", raw.get("_collected_at_utc"))
        if aggregate_stamp is None and not explicit_bbo:
            aggregate_stamp = observed_at
        book_stamp = bbo_observed_at if bbo_observed_at is not None else observed_at
        if book_stamp is None:
            book_stamp = raw.get("quote_time", raw.get("_collected_at_utc"))
            if book_stamp is None and venue == "kalshi":
                book_stamp = raw.get("liquidity_observed_at")

        def fresh(value: Any) -> bool:
            stamp = _stamp(value)
            return stamp is not None and ZERO <= (current - stamp).total_seconds() < max_age

        aggregate_probe = None
        if venue == "polymarket_intl":
            # The generic `liquidity`, volume and open-interest fields are not a
            # verified CLOB liquidity measurement and never cause exclusion.
            value = number(raw.get("liquidityClob", raw.get("liquidity_clob")))
            if value is not None and fresh(aggregate_stamp):
                if value < self.min_top_notional_usd:
                    return DiscoveryDecision(True, "below_discovery_clob_bound", value,
                                             "polymarket_liquidity_clob")
                aggregate_probe = DiscoveryDecision(False, "probe_top_depth", value,
                                                     "polymarket_liquidity_clob")
        if venue == "kalshi":
            bid = raw.get("yes_bid_dollars") if bid is None else bid
            ask = raw.get("yes_ask_dollars") if ask is None else ask
            bid_size = raw.get("yes_bid_size_fp") if bid_size is None else bid_size
            ask_size = raw.get("yes_ask_size_fp") if ask_size is None else ask_size
        if venue in {"kalshi", "polymarket_intl", "polymarket_us"} and fresh(book_stamp):
            values = number(bid, price=True), number(ask, price=True), number(bid_size), number(ask_size)
            if all(value is not None for value in values) and values[0] <= values[1]:
                # Either side may take part in a cross. Both must be small to
                # avoid this instrument's expensive depth/fee requests.
                maximum = max(values[0] * values[2], values[1] * values[3])
                low = maximum < self.min_top_notional_usd
                return DiscoveryDecision(low, "below_discovery_top_bound" if low else "probe_paired_depth",
                                         maximum, f"{venue}_current_bbo")
        if aggregate_probe is not None:
            return aggregate_probe
        if _stamp(aggregate_stamp) is not None or _stamp(book_stamp) is not None:
            if not fresh(aggregate_stamp) and not fresh(book_stamp):
                return DiscoveryDecision(reason="stale_liquidity_probe_required")
        elif aggregate_stamp is None and book_stamp is None:
            return DiscoveryDecision(reason="unknown_liquidity_time_probe_required")
        return DiscoveryDecision()
