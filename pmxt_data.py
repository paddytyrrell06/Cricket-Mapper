"""Optional, read-only public books through the local PMXT SDK sidecar."""
from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from threading import Lock
from urllib.parse import urlparse


class PmxtUnavailable(RuntimeError):
    """The optional SDK or its local service is unavailable."""


def _iso(value):
    if value is None or value == "" or isinstance(value, bool):
        return ""
    try:
        if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
            parsed = datetime.fromtimestamp(float(value) / 1000, timezone.utc)
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else ""
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


def _get(record, name, default=None):
    return record.get(name, default) if isinstance(record, dict) else getattr(record, name, default)


def _number(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and not isinstance(value, bool) else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _levels(levels, reverse=False):
    totals = {}
    for level in levels or []:
        price, size = _number(_get(level, "price")), _number(_get(level, "size"))
        if price is not None and size is not None and 0 <= price <= 1 and size > 0:
            totals[price] = totals.get(price, Decimal(0)) + size
    return [{"price": format(price, "f"), "size": format(totals[price], "f")}
            for price in sorted(totals, reverse=reverse)]


class PmxtData:
    """No hosted routing, account credentials, sockets, or order methods."""

    def __init__(self, base_url="http://localhost:3847", *, auto_start_server=True):
        parsed = urlparse(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment):
            raise ValueError("PMXT data adapter requires a loopback HTTP sidecar URL")
        self.base_url, self.auto_start_server = base_url.rstrip("/"), auto_start_server
        self._clients, self._lock = {}, Lock()

    def _client(self, venue):
        name = {"polymarket": "Polymarket", "polymarket_intl": "Polymarket", "kalshi": "Kalshi"}.get(venue)
        if not name:
            raise ValueError("PMXT supports only polymarket_intl and kalshi here")
        if os.environ.get("PMXT_API_KEY"):
            raise PmxtUnavailable("Unset PMXT_API_KEY for this local-only data adapter")
        with self._lock:
            if name not in self._clients:
                try:
                    sdk = importlib.import_module("pmxt")
                    self._clients[name] = getattr(sdk, name)(
                        base_url=self.base_url, auto_start_server=self.auto_start_server)
                except ImportError as exc:
                    raise PmxtUnavailable("Install requirements-pmxt.txt with Python 3.10+; see docs/PMXT.md") from exc
                except Exception as exc:
                    raise PmxtUnavailable("PMXT local sidecar could not start; see docs/PMXT.md") from exc
            return self._clients[name]

    def fetch_book(self, venue, instrument_id):
        """One SDK book call using an exact venue outcome ID; never a market UUID."""
        if not isinstance(instrument_id, str) or not instrument_id.strip() or len(instrument_id) > 256:
            raise ValueError("An exact nonempty native outcome ID is required")
        try:
            book = self._client(venue).fetch_order_book(instrument_id)
        except Exception as exc:
            if venue == "kalshi" and "authentication" in str(exc).lower():
                raise PmxtUnavailable("PMXT Kalshi books currently require venue authentication; use the native public REST source") from exc
            raise
        observed = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        metadata = _get(book, "source_metadata") or {}
        # PMXT may synthesize its timestamp; never label that value as exchange time.
        result = {"venue": "polymarket_intl" if venue == "polymarket" else venue,
                  "instrument_id": instrument_id, "source": "pmxt_local", "observed_at": observed,
                  "exchange_time": _iso(metadata.get("exchange_timestamp")),
                  "provider_time": _iso(_get(book, "timestamp")) or _iso(_get(book, "datetime")),
                  "bids": _levels(_get(book, "bids"), True), "asks": _levels(_get(book, "asks")),
                  "source_metadata": metadata}
        for target, names in {"minimum_order_size": ("min_order_size", "minimum_order_size"),
                              "minimum_order_notional_usd": ("minimum_order_notional_usd",)}.items():
            for name in names:
                value = _number(metadata.get(name))
                if value is not None and value > 0:
                    result[target] = format(value, "f")
                    break
        return result

    def fetch_candles(self, venue, instrument_id, *, resolution="1h", limit=24):
        """Bounded price history; candles never establish executable book depth."""
        if venue not in {"polymarket", "polymarket_intl"}:
            raise ValueError("This optional candle helper supports Polymarket only")
        if not isinstance(instrument_id, str) or not instrument_id.strip() or len(instrument_id) > 256:
            raise ValueError("An exact native outcome ID is required")
        if resolution not in {"1h", "1d"} or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("Use 1h/1d resolution and a candle limit between 1 and 100")
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=limit * (24 if resolution == "1d" else 1))
        records = self._client(venue).fetch_ohlcv(
            instrument_id, resolution=resolution, limit=limit, start=start, end=end)
        candles = []
        for record in list(records)[-limit:]:
            candle = {"time": _iso(_get(record, "timestamp"))}
            for name in ("open", "high", "low", "close", "volume"):
                value = _number(_get(record, name))
                if name == "volume" and value == 0:
                    value = None  # PMXT substitutes zero when native price history omits volume.
                candle[name] = format(value, "f") if value is not None else None
            candles.append(candle)
        return {"venue": "polymarket_intl", "instrument_id": instrument_id,
                "source": "pmxt_local", "data_type": "price_candles", "executable": False,
                "resolution": resolution, "observed_at": datetime.now(timezone.utc).isoformat(),
                "candles": candles}

    def fetch_markets(self, venue, query, *, limit=3):
        """One bounded SDK result page; discovery suggestions require normal settlement review."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 3:
            raise ValueError("Discovery limit must be between 1 and 3")
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("A bounded nonempty discovery query is required")
        records = self._client(venue).fetch_markets(query=query, limit=limit)
        return [{"venue": venue, "source": "pmxt_local", "status": "review",
                 "raw": dataclasses.asdict(m) if dataclasses.is_dataclass(m) else m}
                for m in list(records)[:limit]]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read at most two explicitly selected PMXT books")
    parser.add_argument("venue", choices=("kalshi", "polymarket_intl"))
    parser.add_argument("instrument_ids", nargs="+", help="Exact native outcome IDs (maximum two)")
    args = parser.parse_args()
    if len(args.instrument_ids) > 2:
        parser.error("At most two native outcome IDs are allowed")
    provider = PmxtData()
    print(json.dumps([provider.fetch_book(args.venue, identity) for identity in args.instrument_ids], indent=2))
