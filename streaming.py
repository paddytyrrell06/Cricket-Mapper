"""Transport-neutral incremental quote cache for a future market-data service.

Discovery, matching, rule extraction, network I/O and JSON rendering stay outside
the update path. Feed adapters publish outcome-specific BBOs, not last trades.
An adapter must assemble a complete venue message before calling ``apply`` and
validate any channel-wide sequence numbers before projecting them to instruments.
``previous_sequence`` is the previous sequence *for this instrument*; this avoids
mistaking interleaved Kalshi subscription messages for a gap in one contract.

No WebSocket connections or order execution are implemented here. Unknown depth
remains unknown, and a quote cross is a pre-fee price signal, not an execution.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Optional

import canonical as C
import settlement as S

InstrumentKey = tuple[str, str, str]
_UNCHANGED = object()


def instrument_key(market: C.Market) -> InstrumentKey:
    """Use the same outcome identity as the replayable REST quote snapshots."""
    return market.venue, str(market.market_id), C.norm_team(market.outcome)


def _decimal(value: Any, *, price: bool = False) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a price or quantity")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid decimal") from exc
    if not number.is_finite() or number < 0 or (price and number > 1):
        raise ValueError("price must be finite 0..1; quantity must be nonnegative")
    return number


def _stamp(value: Any) -> Optional[datetime]:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _text(value: Optional[Decimal]) -> Optional[str]:
    return format(value.normalize(), "f") if value is not None else None


@dataclass(frozen=True)
class QuoteUpdate:
    key: InstrumentKey
    generation: int = 0
    snapshot: bool = True
    # Omitted snapshot sides are empty; omitted delta sides retain their values.
    bid: Any = _UNCHANGED
    ask: Any = _UNCHANGED
    bid_size: Any = _UNCHANGED
    ask_size: Any = _UNCHANGED
    sequence: Optional[int] = None
    previous_sequence: Optional[int] = None
    observed_at: Any = None
    exchange_time: str = ""
    source: str = ""


@dataclass(frozen=True)
class QuoteState:
    key: InstrumentKey
    generation: int
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    bid_size: Optional[Decimal] = None
    ask_size: Optional[Decimal] = None
    observed_at: str = ""
    exchange_time: str = ""
    source: str = ""
    sequence: Optional[int] = None
    valid: bool = False
    requires_snapshot: bool = True
    status: str = "awaiting_snapshot"
    revision: int = 0
    # Internal timing uses a monotonic clock after the initial UTC age check.
    observation_ns: int = 0
    expires_ns: int = 0

    def to_dict(self) -> dict:
        return {"venue": self.key[0], "market_id": self.key[1], "outcome": self.key[2],
                "generation": self.generation, "bid": _text(self.bid), "ask": _text(self.ask),
                "bid_size": _text(self.bid_size), "ask_size": _text(self.ask_size),
                "quote_time": self.observed_at, "exchange_time": self.exchange_time,
                "source": self.source, "sequence": self.sequence, "valid": self.valid,
                "requires_snapshot": self.requires_snapshot, "status": self.status}


@dataclass(frozen=True)
class Comparison:
    comparison_id: str
    left: InstrumentKey
    right: InstrumentKey
    compatible: bool
    comparison_basis: str
    contingency_status: str
    caveats: tuple[str, ...]


@dataclass(frozen=True)
class CrossState:
    comparison_id: str
    eligible: bool
    crossed: bool
    reason: str
    bid_key: Optional[InstrumentKey] = None
    ask_key: Optional[InstrumentKey] = None
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    gap: Optional[Decimal] = None
    depth_confirmed: bool = False
    available_size: Optional[Decimal] = None
    comparison_basis: str = ""
    contingency_status: str = "unknown"
    caveats: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"comparison_id": self.comparison_id, "eligible": self.eligible,
                "crossed": self.crossed, "reason": self.reason,
                "bid_key": list(self.bid_key) if self.bid_key else None,
                "ask_key": list(self.ask_key) if self.ask_key else None,
                "bid": _text(self.bid), "ask": _text(self.ask), "gap": _text(self.gap),
                "depth_confirmed": self.depth_confirmed, "available_size": _text(self.available_size),
                "comparison_basis": self.comparison_basis,
                "contingency_status": self.contingency_status, "caveats": list(self.caveats)}


@dataclass(frozen=True)
class UpdateResult:
    applied: bool
    reason: str
    comparisons: tuple[CrossState, ...] = ()


class _Deadlines:
    """Indexed min-heap: one entry per instrument, no global compaction pauses."""

    def __init__(self):
        self.heap: list[tuple[int, InstrumentKey]] = []
        self.positions: dict[InstrumentKey, int] = {}

    def _swap(self, left: int, right: int) -> None:
        self.heap[left], self.heap[right] = self.heap[right], self.heap[left]
        self.positions[self.heap[left][1]] = left
        self.positions[self.heap[right][1]] = right

    def _up(self, position: int) -> int:
        while position and self.heap[position] < self.heap[(position - 1) // 2]:
            parent = (position - 1) // 2
            self._swap(position, parent)
            position = parent
        return position

    def _down(self, position: int) -> None:
        length = len(self.heap)
        while position * 2 + 1 < length:
            child = position * 2 + 1
            if child + 1 < length and self.heap[child + 1] < self.heap[child]:
                child += 1
            if self.heap[position] <= self.heap[child]:
                break
            self._swap(position, child)
            position = child

    def set(self, key: InstrumentKey, deadline: int) -> None:
        position = self.positions.get(key)
        if position is None:
            position = len(self.heap)
            self.positions[key] = position
            self.heap.append((deadline, key))
        else:
            self.heap[position] = deadline, key
        self._down(self._up(position))

    def discard(self, key: InstrumentKey) -> None:
        position = self.positions.get(key)
        if position is None:
            return
        last = len(self.heap) - 1
        if position != last:
            self._swap(position, last)
        self.heap.pop()
        del self.positions[key]
        if position < len(self.heap):
            self._down(self._up(position))

    def pop_due(self, now_ns: int) -> Optional[InstrumentKey]:
        if not self.heap or self.heap[0][0] > now_ns:
            return None
        key = self.heap[0][1]
        self.discard(key)
        return key


class LiveQuoteEngine:
    """Indexed quote state: O(log instruments + affected pairs) per update.

    All public methods are guarded by one lock. This supports a single event-loop
    owner and also prevents readers seeing half-applied BBOs from feed threads.
    Call ``expire`` on a service timer even when no market messages arrive. It
    uses an indexed deadline heap, so idle instruments do not require a full
    market scan and active instruments cannot accumulate old deadline entries.
    """

    def __init__(self, comparisons: Iterable[Comparison], *, max_age_seconds: float = 5,
                 max_skew_seconds: float = 1, clock_ns: Callable[[], int] = time.monotonic_ns,
                 wall_clock: Optional[Callable[[], datetime]] = None):
        if (not math.isfinite(max_age_seconds) or max_age_seconds <= 0 or
                not math.isfinite(max_skew_seconds) or max_skew_seconds < 0):
            raise ValueError("age must be positive and skew nonnegative")
        self.max_age_seconds = max_age_seconds
        self.max_skew_seconds = max_skew_seconds
        self._age_ns = int(max_age_seconds * 1_000_000_000)
        self._skew_ns = int(max_skew_seconds * 1_000_000_000)
        self._clock_ns, self._wall_clock = clock_ns, wall_clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._comparisons: dict[str, Comparison] = {}
        self._index: dict[InstrumentKey, set[str]] = {}
        self._venues: dict[str, set[InstrumentKey]] = {}
        self._generations: dict[str, int] = {}
        self._connected: dict[str, bool] = {}
        self._quotes: dict[InstrumentKey, QuoteState] = {}
        self._crosses: dict[str, CrossState] = {}
        self._deadlines = _Deadlines()
        for comparison in comparisons:
            if comparison.comparison_id in self._comparisons:
                raise ValueError("duplicate comparison_id")
            if comparison.left[0] == comparison.right[0]:
                raise ValueError("comparisons must use distinct venues")
            for key in (comparison.left, comparison.right):
                if len(key) != 3 or not all(key):
                    raise ValueError("instrument key requires venue, contract ID and outcome")
                self._index.setdefault(key, set()).add(comparison.comparison_id)
                self._venues.setdefault(key[0], set()).add(key)
                self._generations[key[0]], self._connected[key[0]] = 0, True
                self._quotes[key] = QuoteState(key=key, generation=0)
            self._comparisons[comparison.comparison_id] = comparison
        self._refresh(set(self._comparisons))

    @classmethod
    def from_matches(cls, matches: Iterable[Any], *, strict_settlement: bool = False,
                     **kwargs: Any) -> "LiveQuoteEngine":
        """Compile matching and settlement checks once, away from the hot path."""
        comparisons = []
        seen = set()
        for match in matches:
            left, right = instrument_key(match.m1), instrument_key(match.m2)
            identity = (match.fixture.canonical_fixture_id, match.market_type,
                        C.norm_team(match.outcome), *sorted((left, right)))
            if identity in seen:
                continue
            seen.add(identity)
            rules = S.compare_profiles(match.m1.extra.get("settlement"),
                                       match.m2.extra.get("settlement"), strict=strict_settlement)
            compatible = (match.status == "accepted" and match.fixture.status == "accepted"
                          and rules["status"] == "compatible")
            # Stable identifiers survive discovery order changes and process restarts.
            identifier = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:24]
            comparisons.append(Comparison(identifier, left, right, compatible,
                                          rules["comparison_basis"], rules["contingency_status"],
                                          tuple(rules["caveats"])))
        return cls(comparisons, **kwargs)

    @property
    def keys(self) -> tuple[InstrumentKey, ...]:
        return tuple(sorted(self._index))

    def generation(self, venue: str) -> int:
        with self._lock:
            return self._generations[venue]

    def quote(self, key: InstrumentKey) -> QuoteState:
        with self._lock:
            self._expire(self._clock_ns())
            return self._quotes[key]

    def _invalidate(self, key: InstrumentKey, reason: str) -> None:
        self._deadlines.discard(key)
        old = self._quotes[key]
        self._quotes[key] = replace(old, bid=None, ask=None, bid_size=None, ask_size=None,
                                    valid=False, requires_snapshot=True, status=reason,
                                    revision=old.revision + 1)

    def _refresh(self, comparison_ids: set[str]) -> tuple[CrossState, ...]:
        result = []
        for identifier in sorted(comparison_ids):
            comparison = self._comparisons[identifier]
            left, right = self._quotes[comparison.left], self._quotes[comparison.right]
            common = {"comparison_id": identifier, "comparison_basis": comparison.comparison_basis,
                      "contingency_status": comparison.contingency_status, "caveats": comparison.caveats}
            reason = "fresh"
            if not comparison.compatible:
                reason = "rules_or_mapping_unverified"
            elif not left.valid or not right.valid:
                reason = next(q.status for q in (left, right) if not q.valid)
            elif abs(left.observation_ns - right.observation_ns) > self._skew_ns:
                reason = "skewed"
            candidates = []
            if reason == "fresh":
                for sell, buy in ((left, right), (right, left)):
                    if sell.bid is not None and buy.ask is not None:
                        candidates.append((sell.bid - buy.ask, sell, buy))
                if not candidates:
                    reason = "unpriced"
            if reason != "fresh":
                state = CrossState(eligible=False, crossed=False, reason=reason, **common)
            else:
                gap, sell, buy = max(candidates, key=lambda item: item[0])
                sized = sell.bid_size is not None and buy.ask_size is not None
                state = CrossState(eligible=True, crossed=gap > 0, reason="fresh",
                                   bid_key=sell.key, ask_key=buy.key, bid=sell.bid, ask=buy.ask, gap=gap,
                                   depth_confirmed=sized,
                                   available_size=min(sell.bid_size, buy.ask_size) if sized else None, **common)
            self._crosses[identifier] = state
            result.append(state)
        return tuple(result)

    def _expire(self, now_ns: int) -> set[str]:
        affected = set()
        while True:
            key = self._deadlines.pop_due(now_ns)
            if key is None:
                break
            self._invalidate(key, "stale")
            affected.update(self._index[key])
        if affected:
            self._refresh(affected)
        return affected

    def expire(self) -> UpdateResult:
        """Withdraw expired signals without waiting for another feed message."""
        with self._lock:
            affected = self._expire(self._clock_ns())
            return UpdateResult(bool(affected), "expired" if affected else "unchanged",
                                tuple(self._crosses[key] for key in sorted(affected)))

    def disconnect(self, venue: str) -> UpdateResult:
        with self._lock:
            self._connected[venue] = False
            affected = set()
            for key in self._venues[venue]:
                self._invalidate(key, "disconnected")
                affected.update(self._index[key])
            return UpdateResult(True, "disconnected", self._refresh(affected))

    def invalidate(self, keys: Iterable[InstrumentKey], reason: str = "resync_required") -> UpdateResult:
        """Withdraw a channel's instruments after an adapter detects a gap."""
        with self._lock:
            selected = set(keys)
            if not selected.issubset(self._quotes):
                raise KeyError("unknown instrument in invalidation")
            affected = set()
            for key in selected:
                self._invalidate(key, reason)
                affected.update(self._index[key])
            return UpdateResult(bool(selected), reason, self._refresh(affected))

    def begin_session(self, venue: str) -> int:
        """Invalidate old books and return the token required by new callbacks."""
        with self._lock:
            self._generations[venue] += 1
            self._connected[venue] = True
            affected = set()
            for key in self._venues[venue]:
                self._deadlines.discard(key)
                self._quotes[key] = QuoteState(key=key, generation=self._generations[venue],
                                               revision=self._quotes[key].revision + 1)
                affected.update(self._index[key])
            self._refresh(affected)
            return self._generations[venue]

    @staticmethod
    def _side(price: Any, size: Any, old_price: Optional[Decimal], old_size: Optional[Decimal],
              snapshot: bool) -> tuple[Optional[Decimal], Optional[Decimal]]:
        value = (None if snapshot else old_price) if price is _UNCHANGED else _decimal(price, price=True)
        quantity = ((None if snapshot or price is not _UNCHANGED else old_size)
                    if size is _UNCHANGED else _decimal(size))
        if quantity == 0:
            return None, None
        if value is None:
            if quantity is not None:
                raise ValueError("positive quantity without price")
            return None, None
        return value, quantity

    def _apply(self, update: QuoteUpdate, now_ns: int) -> tuple[bool, str, set[str]]:
        key = update.key
        if key not in self._index:
            return False, "unknown_instrument", set()
        current = self._quotes[key]
        if update.generation != self._generations[key[0]]:
            return False, "old_generation", set()
        if not self._connected[key[0]]:
            return False, "disconnected", set()
        if not update.snapshot and current.requires_snapshot:
            return False, "snapshot_required", set()
        sequence = update.sequence
        if (update.previous_sequence is not None and
                (isinstance(update.previous_sequence, bool) or not isinstance(update.previous_sequence, int)
                 or update.previous_sequence < 0)):
            self._invalidate(key, "invalid_sequence")
            return False, "invalid_sequence", set(self._index[key])
        if sequence is not None and (isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0):
            self._invalidate(key, "invalid_sequence")
            return False, "invalid_sequence", set(self._index[key])
        if current.sequence is not None:
            if sequence is None:
                self._invalidate(key, "missing_sequence")
                return False, "missing_sequence", set(self._index[key])
            if (sequence < current.sequence or
                    (sequence == current.sequence and not (update.snapshot and current.requires_snapshot))):
                return False, "out_of_order", set()
        if (not update.snapshot and update.previous_sequence is not None
                and update.previous_sequence != current.sequence):
            self._invalidate(key, "sequence_gap")
            return False, "sequence_gap", set(self._index[key])
        if not update.snapshot and all(value is _UNCHANGED for value in
                                       (update.bid, update.ask, update.bid_size, update.ask_size)):
            # A heartbeat or an update outside the BBO must not extend quote age.
            # Retain sequence linkage when a channel adapter forwards such a message.
            if sequence is not None:
                self._quotes[key] = replace(current, sequence=sequence)
            return sequence is not None, "sequence_only" if sequence is not None else "no_quote_change", set()
        now = self._wall_clock()
        observed = _stamp(update.observed_at) if update.observed_at is not None else now
        age = (now - observed).total_seconds() if observed is not None else None
        reason = ("unknown_time" if age is None else "future" if age < 0 else
                  "stale" if age >= self.max_age_seconds else "")
        if not reason:
            old_stamp = _stamp(current.observed_at)
            if old_stamp is not None and observed < old_stamp:
                return False, "out_of_order", set()
        if reason:
            self._invalidate(key, reason)
            return False, reason, set(self._index[key])
        try:
            bid, bid_size = self._side(update.bid, update.bid_size, current.bid, current.bid_size, update.snapshot)
            ask, ask_size = self._side(update.ask, update.ask_size, current.ask, current.ask_size, update.snapshot)
            if bid is not None and ask is not None and bid > ask:
                raise ValueError("crossed local book requires resynchronization")
        except ValueError:
            self._invalidate(key, "invalid_book")
            return False, "invalid_book", set(self._index[key])
        observation_ns = now_ns - int(age * 1_000_000_000)
        state = QuoteState(key=key, generation=update.generation, bid=bid, ask=ask,
                           bid_size=bid_size, ask_size=ask_size,
                           observed_at=observed.isoformat(), exchange_time=update.exchange_time,
                           source=update.source or current.source, sequence=sequence,
                           valid=True, requires_snapshot=False,
                           status="available" if bid is not None and ask is not None else
                                  "one-sided" if bid is not None or ask is not None else "empty",
                           revision=current.revision + 1, observation_ns=observation_ns,
                           expires_ns=observation_ns + self._age_ns)
        self._quotes[key] = state
        self._deadlines.set(key, state.expires_ns)
        return True, "applied", set(self._index[key])

    def apply(self, update: QuoteUpdate) -> UpdateResult:
        """Atomically replace a BBO or patch its sides, then evaluate touched pairs."""
        with self._lock:
            now_ns = self._clock_ns()
            affected = self._expire(now_ns)
            applied, reason, changed = self._apply(update, now_ns)
            affected.update(changed)
            return UpdateResult(applied, reason, self._refresh(affected))

    def seed(self, markets: Iterable[C.Market]) -> UpdateResult:
        """Load REST/replay observations atomically, retaining their original times.

        Depth is optional and is never inferred from a price. Do not call this
        over an already-running sequenced connection; seed a newly built engine.
        """
        with self._lock:
            now_ns = self._clock_ns()
            affected = self._expire(now_ns)
            count = 0
            for market in markets:
                key = instrument_key(market)
                if key not in self._index:
                    continue
                extra = market.extra or {}
                applied, _, changed = self._apply(QuoteUpdate(
                    key, generation=self._generations[key[0]], bid=market.bid, ask=market.ask,
                    bid_size=extra.get("quote_bid_size", extra.get("bid_size")),
                    ask_size=extra.get("quote_ask_size", extra.get("ask_size")),
                    observed_at=market.quote_time or "", exchange_time=extra.get("quote_exchange_time", ""),
                    source=market.quote_source), now_ns)
                count += int(applied)
                affected.update(changed)
            return UpdateResult(bool(count), "seeded", self._refresh(affected))

    def snapshot(self) -> dict:
        """JSON-safe diagnostic state; full serialization stays off the hot path."""
        with self._lock:
            self._expire(self._clock_ns())
            as_of = self._wall_clock().isoformat()
            quotes, crosses = tuple(self._quotes.values()), tuple(self._crosses.values())
        # Immutable states let a publisher sort/format without blocking ingestion.
        return {"as_of": as_of, "transport": "not_connected",
                "max_age_seconds": self.max_age_seconds, "max_skew_seconds": self.max_skew_seconds,
                "instrument_count": len(quotes), "comparison_count": len(crosses),
                "quotes": [quote.to_dict() for quote in sorted(quotes, key=lambda q: q.key)],
                "comparisons": [cross.to_dict() for cross in sorted(crosses, key=lambda c: c.comparison_id)]}
