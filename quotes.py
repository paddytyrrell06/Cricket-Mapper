"""Read outcome-specific quotes without changing collection or matching results.

Sources:
https://docs.kalshi.com/api-reference/market/get-market
https://docs.polymarket.com/market-data/prices-order-books
https://docs.polymarket.us/api-reference/markets/get-market-bbo
https://docs.polymarket.us/concepts/orders

Prices are dollar probabilities, never last trades or display probabilities.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import canonical as C

CLOB = "https://clob.polymarket.com"
PMUS = "https://gateway.polymarket.us"
KALSHI = "https://external-api.kalshi.com/trade-api/v2"
TIMEOUT = 8
RETRIES = 2
WORKERS = 4
BOOK_BATCH_SIZE = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_time(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        text = str(value)
        if text.isdigit():
            stamp = int(text)
            parsed = datetime.fromtimestamp(stamp / (1000 if stamp > 10**11 else 1), timezone.utc)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return ""
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


def _number(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _price(value: Any) -> Optional[Decimal]:
    result = _number(value)
    return result if result is not None and 0 <= result <= 1 else None


def _text(value: Optional[Decimal]) -> Optional[str]:
    return ("0" if value == 0 else str(value.normalize())) if value is not None else None


def _json_list(value: Any) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _key(market: C.Market) -> tuple[str, str, str]:
    return market.venue, str(market.market_id), C.norm_team(market.outcome)


def _set_quote(market: C.Market, bid: Optional[Decimal], ask: Optional[Decimal],
               stamp: str, source: str, *, empty: bool = False) -> None:
    market.bid, market.ask = _text(bid), _text(ask)
    market.quote_time, market.quote_source = stamp, source
    market.quote_status = ("available" if bid is not None and ask is not None else
                           "one-sided" if bid is not None or ask is not None else
                           "empty" if empty else "unavailable")


def _side_price(value: Any, size: Any, label: str, warnings: list[str]
                ) -> Optional[Decimal]:
    if isinstance(value, dict) and size is None:
        size = value.get("size")
    if size is not None:
        quantity = _number(size)
        if quantity is None:
            warnings.append(f"QUOTES {label}: invalid quote size")
            return None
        if quantity <= 0:
            return None
    if isinstance(value, dict):
        if value.get("currency") not in (None, "", "USD", "USDC"):
            warnings.append(f"QUOTES {label}: unsupported quote currency")
            return None
        value = value.get("value")
    if value is None or value == "":
        return None
    result = _price(value)
    if result is None:
        warnings.append(f"QUOTES {label}: invalid price outside finite 0..1")
    return result


def _raw_quote_time(record: dict, collected_at: str) -> str:
    # updatedAt/updated_time are market metadata times, not quote timestamps.
    return _iso_time(record.get("_collected_at_utc")) or _iso_time(collected_at)


def _intl_asset(market: C.Market, record: dict) -> str:
    outcomes = _json_list(record.get("outcomes"))
    ids = _json_list(record.get("positionIds") if record.get("version") == "v2"
                     else record.get("clobTokenIds"))
    if len(outcomes) != len(ids) or not outcomes:
        return ""
    if market.extra.get("binary_target_market"):
        wanted = {"yes"}
    else:
        wanted = {C.norm_team(market.raw_outcome or market.outcome)}
    matches = [i for i, name in enumerate(outcomes) if C.norm_team(str(name)) in wanted]
    if len(matches) != 1:
        return ""
    asset = str(ids[matches[0]])
    return asset if asset.isdigit() else ""


def _us_long(market: C.Market, record: dict) -> Optional[bool]:
    names = {C.norm_team(market.raw_outcome), C.norm_team(market.outcome)} - {""}
    matched = []
    for side in record.get("marketSides") or []:
        if not isinstance(side, dict) or not isinstance(side.get("long"), bool):
            continue
        name = C._published_team_name(side)
        canonical, _ = C.canonical_team_name(
            name, C._pmus_competition(record), market.fixture_key.date if market.fixture_key else "")
        if names & {C.norm_team(name), C.norm_team(canonical)}:
            matched.append(side["long"])
    return matched[0] if len(matched) == 1 else None


def _request_json(method: str, url: str, payload: Any = None) -> tuple[int, Any, str]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    for attempt in range(RETRIES):
        request = urllib.request.Request(url, data=data, method=method, headers={
            "User-Agent": "cricket-exchange-matching/1.0",
            "Accept": "application/json", "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        })
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.status, json.loads(response.read().decode("utf-8")), _now()
        except urllib.error.HTTPError as exc:
            if exc.code not in {408, 425, 429} and exc.code < 500:
                return exc.code, None, _now()
            if attempt + 1 == RETRIES:
                return exc.code, None, _now()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            if attempt + 1 == RETRIES:
                return 0, None, _now()
        time.sleep(0.25)
    return 0, None, _now()


def _run_requests(jobs: list[tuple]) -> list[tuple]:
    results = []
    if not jobs:
        return results
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        pending = {pool.submit(_request_json, method, url, payload): key
                   for key, method, url, payload in jobs}
        for future in as_completed(pending):
            key = pending[future]
            try:
                status, body, observed = future.result()
                results.append((key, status, body, observed))
            except Exception:
                # Price availability must never prevent fixture publication.
                results.append((key, 0, None, _now()))
    return results


def _live_books(assets: set[str], us_slugs: set[str]
                ) -> tuple[dict, dict, list[str]]:
    jobs, books, us_quotes, warnings = [], {}, {}, []
    ordered = sorted(assets)
    for offset in range(0, len(ordered), BOOK_BATCH_SIZE):
        batch = tuple(ordered[offset:offset + BOOK_BATCH_SIZE])
        jobs.append((("intl", batch), "POST", CLOB + "/books",
                     [{"token_id": asset} for asset in batch]))
    for slug in sorted(us_slugs):
        jobs.append((("us", slug), "GET",
                     f"{PMUS}/v1/markets/{urllib.parse.quote(slug, safe='')}/bbo", None))
    fallback = []
    for (venue, key), status, body, observed in _run_requests(jobs):
        if venue == "intl":
            if status == 200 and isinstance(body, list):
                for book in body:
                    if isinstance(book, dict) and str(book.get("asset_id")) in key:
                        books[str(book["asset_id"])] = (book, observed)
            elif status in {400, 404}:
                # A retired token may reject the entire batch. Isolate it.
                fallback.extend((("book", asset), "GET", CLOB + "/book?" +
                                  urllib.parse.urlencode({"token_id": asset}), None)
                                 for asset in key)
            else:
                warnings.append(f"QUOTES polymarket_intl: CLOB batch unavailable (HTTP {status or 'network error'})")
        elif status == 200 and isinstance(body, dict) and isinstance(body.get("marketData"), dict):
            data = body["marketData"]
            if data.get("marketSlug") in (None, "", key):
                us_quotes[key] = (data, observed)
            else:
                warnings.append(f"QUOTES polymarket_us: BBO instrument mismatch for {key}; event snapshot fallback")
        else:
            warnings.append(f"QUOTES polymarket_us: BBO unavailable for {key} (HTTP {status or 'network error'}); event snapshot fallback")
    for (_, asset), status, book, observed in _run_requests(fallback):
        if status == 200 and isinstance(book, dict) and str(book.get("asset_id")) == asset:
            books[asset] = (book, observed)
    return books, us_quotes, warnings


def _book_quote(market: C.Market, book: dict, observed: str,
                asset: str, warnings: list[str]) -> None:
    prices = {}
    invalid = False
    for side in ("bids", "asks"):
        levels = book.get(side)
        if not isinstance(levels, list):
            warnings.append(f"QUOTES polymarket_intl {asset}: malformed order book")
            return
        values = []
        for level in levels:
            if not isinstance(level, dict):
                invalid = True
                continue
            size = _number(level.get("size"))
            price = _price(level.get("price"))
            if size is None or price is None:
                invalid = True
            elif size > 0:
                values.append(price)
        prices[side] = (max(values) if side == "bids" else min(values)) if values else None
    if invalid:
        warnings.append(f"QUOTES polymarket_intl {asset}: invalid order-book level ignored")
    market.extra["quote_exchange_time"] = _iso_time(book.get("timestamp"))
    _set_quote(market, prices["bids"], prices["asks"], observed,
               f"{CLOB}/books token_id={asset}", empty=not invalid)


def _live_kalshi(tickers: set[str]) -> tuple[dict[str, dict], list[str]]:
    """Refresh selected contracts after discovery instead of using its older BBO."""
    jobs = []
    ordered = sorted(tickers)
    for offset in range(0, len(ordered), 40):
        batch = tuple(ordered[offset:offset + 40])
        url = KALSHI + "/markets?" + urllib.parse.urlencode({
            "tickers": ",".join(batch), "limit": len(batch),
        })
        jobs.append((batch, "GET", url, None))
    refreshed, warnings = {}, []
    for batch, status, body, observed in _run_requests(jobs):
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("markets"), list):
            warnings.append(f"QUOTES kalshi: refresh unavailable (HTTP {status}); discovery snapshot retained")
            continue
        for record in body["markets"]:
            if isinstance(record, dict) and record.get("ticker") in batch:
                refreshed[record["ticker"]] = {**record, "_collected_at_utc": observed}
    return refreshed, warnings


def enrich_quotes(markets: list[C.Market], raw: dict[str, list[dict]], *, live: bool,
                  saved_quotes: Optional[list[dict]] = None, collected_at: str = ""
                  ) -> tuple[list[dict], list[str]]:
    """Mutate matched markets and return replayable quote observations plus warnings.

    Replay never makes requests. Legacy international snapshots without outcome
    books remain unavailable: market display prices cannot substitute for bids.
    """
    warnings: list[str] = []
    records = {(venue, str(record.get("ticker") if venue == "kalshi" else record.get("id"))): record
               for venue, rows in raw.items() for record in rows if isinstance(record, dict)}
    saved, duplicate_saved = {}, set()
    for entry in saved_quotes or []:
        if not isinstance(entry, dict):
            continue
        key = (str(entry.get("venue") or ""), str(entry.get("market_id") or ""),
               C.norm_team(entry.get("outcome") or ""))
        if key in saved and saved[key] != entry:
            duplicate_saved.add(key)
        saved[key] = entry
    assets, us_slugs = {}, set()
    for market in markets:
        _set_quote(market, None, None, "", "")
        record = records.get((market.venue, str(market.market_id)), {})
        if live and market.venue == "polymarket_intl":
            assets[_key(market)] = _intl_asset(market, record)
        if live and market.venue == "polymarket_us" and record.get("slug"):
            us_slugs.add(str(record["slug"]))
    fresh_kalshi, kalshi_warnings = (_live_kalshi({market.market_id for market in markets
                                                if market.venue == "kalshi"})
                                    if live else ({}, []))
    warnings.extend(kalshi_warnings)
    books, us_quotes, network_warnings = (_live_books(set(assets.values()) - {""}, us_slugs)
                                         if live else ({}, {}, []))
    warnings.extend(network_warnings)
    entries = []
    for market in markets:
        key = _key(market)
        record = records.get((market.venue, str(market.market_id)), {})
        if live and market.venue == "kalshi":
            record = fresh_kalshi.get(market.market_id, record)
        stamp = _raw_quote_time(record, collected_at)
        if not live and key in saved and key not in duplicate_saved:
            entry = saved[key]
            bid = _side_price(entry.get("bid"), None, str(key), warnings)
            ask = _side_price(entry.get("ask"), None, str(key), warnings)
            _set_quote(market, bid, ask, _iso_time(entry.get("quote_time")),
                       str(entry.get("quote_source") or "saved quote snapshot"),
                       empty=entry.get("quote_status") == "empty")
            market.extra["quote_exchange_time"] = _iso_time(entry.get("exchange_time"))
        elif not live and key in duplicate_saved:
            warnings.append(f"QUOTES {key}: conflicting saved quote entries")
        elif market.venue == "kalshi" and record:
            if record.get("status") not in (None, "", "open", "active"):
                warnings.append(f"QUOTES {key}: market is no longer open")
                entries.append({"venue": market.venue, "market_id": str(market.market_id),
                                "outcome": market.outcome, "bid": None, "ask": None,
                                "quote_time": stamp, "quote_status": "unavailable",
                                "quote_source": "Kalshi market no longer open"})
                market.quote_time, market.quote_source = stamp, "Kalshi market no longer open"
                continue
            warning_count = len(warnings)
            bid = _side_price(record.get("yes_bid_dollars"), record.get("yes_bid_size_fp"), str(key), warnings)
            ask = _side_price(record.get("yes_ask_dollars"), record.get("yes_ask_size_fp"), str(key), warnings)
            _set_quote(market, bid, ask, stamp,
                       ("Kalshi refreshed market yes_bid_dollars/yes_ask_dollars"
                        if market.market_id in fresh_kalshi else
                        "Kalshi market snapshot yes_bid_dollars/yes_ask_dollars"),
                       empty=len(warnings) == warning_count and
                       ("yes_bid_dollars" in record or "yes_ask_dollars" in record))
        elif market.venue == "polymarket_us" and record:
            is_long = _us_long(market, record)
            slug = str(record.get("slug") or "")
            if is_long is None:
                warnings.append(f"QUOTES {key}: cannot identify long/short outcome")
            else:
                live_data = us_quotes.get(slug)
                if live_data:
                    data, stamp = live_data
                    bid_raw, ask_raw = data.get("bestBid"), data.get("bestAsk")
                    bid_size = data.get("bidShares") if data.get("bidShares") is not None else data.get("bidDepth")
                    ask_size = data.get("askShares") if data.get("askShares") is not None else data.get("askDepth")
                    source = f"{PMUS}/v1/markets/{slug}/bbo"
                else:
                    data = record
                    bid_raw, ask_raw = data.get("bestBidQuote"), data.get("bestAskQuote")
                    bid_size, ask_size = None, None
                    source = "Polymarket US event snapshot bestBidQuote/bestAskQuote"
                warning_count = len(warnings)
                bid = _side_price(bid_raw, bid_size, str(key), warnings)
                ask = _side_price(ask_raw, ask_size, str(key), warnings)
                if not is_long:
                    bid, ask = (Decimal(1) - ask if ask is not None else None,
                                Decimal(1) - bid if bid is not None else None)
                _set_quote(market, bid, ask, stamp, source + (" long" if is_long else " short complement"),
                           empty=len(warnings) == warning_count and
                           (bid_raw is not None or ask_raw is not None))
        elif market.venue == "polymarket_intl":
            asset = assets.get(key, "")
            if asset and asset in books:
                book, observed = books[asset]
                _book_quote(market, book, observed, asset, warnings)
            elif live:
                warnings.append(f"QUOTES {key}: outcome order book unavailable")
        entries.append({"venue": market.venue, "market_id": str(market.market_id),
                        "outcome": market.outcome, "bid": market.bid, "ask": market.ask,
                        "quote_time": market.quote_time, "quote_status": market.quote_status,
                        "quote_source": market.quote_source,
                        "exchange_time": market.extra.get("quote_exchange_time", "")})
    return entries, list(dict.fromkeys(warnings))
