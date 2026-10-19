# This file fetches the current cricket market data from Kalshi, Polymarket International, and Polymarket US while recording whether each venue was collected completely.

from __future__ import annotations

import json
import sys
import time
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

import universe as U

TIMEOUT = 15
RETRIES = 2
BACKOFF = 1.0
PAGE_GUARD = 400

SMOKE = False
CLOSED = False


def say(m: str) -> None:
    print(m, file=sys.stderr, flush=True)


GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
PMUS = "https://gateway.polymarket.us"

KALSHI = "https://external-api.kalshi.com/trade-api/v2"


@dataclass
class Row:
    venue: str
    market_id: str
    title: str
    group_title: str = ""
    ticker_or_slug: str = ""
    outcome: str = ""
    close_time: str = ""
    verdict: str = ""
    evidence: str = ""
    extra: dict = field(default_factory=dict)


class Http:
    def __init__(self) -> None:
        self.log: list[dict] = []
        self.dns_failed_hosts: set[str] = set()

    def get(self, url: str, params: Optional[dict] = None, note: str = "",
            quiet: bool = False) -> tuple[Optional[int], Any]:
        full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        host = urllib.parse.urlparse(url).hostname or ""
        if host in self.dns_failed_hosts:
            self.log.append({"url": full, "status": None, "note": note,
                             "error": "host DNS previously failed"})
            return None, None
        last = None
        for attempt in range(RETRIES):
            t0 = time.time()
            if not quiet:
                say(f"      GET {full[:112]}")
            req = urllib.request.Request(full, headers={
                "User-Agent": "cricket-exchange-matching/1.0",
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            })
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    raw = r.read()
                    secs = round(time.time() - t0, 2)
                    self.log.append({"url": full, "status": r.status,
                                     "note": note, "len": len(raw),
                                     "secs": secs})
                    if not quiet:
                        say(f"        -> {r.status} {len(raw)}b {secs}s")
                    try:
                        return r.status, json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        return r.status, None
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read()[:200].decode("utf-8", "replace")
                except Exception:
                    pass
                self.log.append({"url": full, "status": e.code, "note": note,
                                 "error": detail})
                say(f"        -> HTTP {e.code} {detail[:100]}")
                if e.code in {408, 425, 429}:
                    last = f"HTTP {e.code}"
                    retry_after = 0.0
                    try:
                        retry_after = float(e.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        retry_after = 0.0
                    if retry_after > 0:
                        time.sleep(min(retry_after, 10.0))
                    continue
                if 400 <= e.code < 500:
                    return e.code, None
                last = f"HTTP {e.code}"
            except urllib.error.URLError as e:
                last = repr(e)
                say(f"        -> fail {last[:120]}")
                reason = getattr(e, "reason", None)
                if isinstance(reason, socket.gaierror) or "name resolution" in last.lower():
                    if host:
                        self.dns_failed_hosts.add(host)
                    break
            except Exception as e:
                last = repr(e)
                say(f"        -> fail {last[:120]}")
            time.sleep(BACKOFF * (attempt + 1))
        self.log.append({"url": full, "status": None, "note": note,
                         "error": last})
        return None, None


http = Http()


COLLECTION_META: dict[str, dict[str, Any]] = {}


def _set_collection_meta(venue: str, *, complete: bool, method: str,
                         reason: str = "", records: Optional[int] = None) -> None:
    COLLECTION_META[venue] = {
        "complete": bool(complete),
        "method": method,
        "reason": reason,
        "records": records,
    }


def _maybe_json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def classify(*fields: Optional[str], sport: Optional[str] = None,
             tags: Optional[list[str]] = None,
             series: Optional[str] = None) -> dict:
    return U.cricket_evidence(*fields, native_sport=sport,
                              native_tags=tags, series_ticker=series)


def _record_explicitly_closed(record: dict) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("closed") is True or record.get("archived") is True:
        return True
    if record.get("active") is False:
        return True
    status = str(record.get("status") or record.get("state") or "").strip().lower()
    return status in {
        "closed", "settled", "resolved", "finalized", "finalised",
        "cancelled", "canceled", "archived", "expired",
    }


def _gamma_cricket_events() -> tuple[bool, bool, list[dict]]:
    out: list[dict] = []
    after_cursor: Optional[str] = None
    seen_cursors: set[str] = set()
    seen_event_ids: set[str] = set()

    for page_no in range(1, PAGE_GUARD + 1):
        params: dict[str, Any] = {
            "tag_slug": "cricket",
            "closed": "true" if CLOSED else "false",
            "limit": 100,
        }
        if after_cursor:
            params["after_cursor"] = after_cursor

        status, body = http.get(
            f"{GAMMA}/events/keyset", params, note="gamma cricket events",
            quiet=page_no > 1)
        if status != 200:
            say(f"    tagged cricket endpoint failed with status {status}")
            return False, False, out
        if not isinstance(body, dict):
            say("    tagged cricket endpoint returned an unexpected payload")
            return False, False, out

        items = body.get("data") or body.get("events") or []
        if not isinstance(items, list):
            say("    tagged cricket endpoint returned non-list event data")
            return False, False, out

        added = 0
        for event in items:
            event_id = str((event or {}).get("id") or (event or {}).get("slug") or "")
            if event_id and event_id in seen_event_ids:
                continue
            if event_id:
                seen_event_ids.add(event_id)
            out.append(event)
            added += 1
        say(f"    cricket events page {page_no}: +{added}, total {len(out)}")

        next_cursor = body.get("next_cursor")
        if SMOKE:
            return True, False, out
        if not items or not next_cursor:
            return True, True, out
        if added == 0:
            say("    tagged endpoint page made no progress; coverage is incomplete")
            return True, False, out
        next_cursor = str(next_cursor)
        if next_cursor in seen_cursors:
            say("    repeated next_cursor; coverage is incomplete")
            return True, False, out
        seen_cursors.add(next_cursor)
        after_cursor = next_cursor

    say("    page guard reached; coverage is incomplete")
    return True, False, out


def _gamma_public_search_fallback() -> list[dict]:
    raw: list[dict] = []
    seen: set[str] = set()
    terms = (U.POLYMARKET_SEARCH_QUERIES[:3] if SMOKE
             else U.POLYMARKET_SEARCH_QUERIES)

    for term in terms:
        status, body = http.get(
            f"{GAMMA}/public-search",
            {"q": term, "limit_per_type": 100},
            note=f"search {term}",
            quiet=True,
        )
        if status != 200 or not isinstance(body, dict):
            continue

        bucket: list[dict] = []
        bucket.extend(body.get("markets") or [])
        for event in body.get("events") or []:
            for market in event.get("markets") or []:
                copied = dict(market)
                copied.setdefault("_event_title", event.get("title", ""))
                copied.setdefault("_event_slug", event.get("slug", ""))
                copied["_discovery_source"] = "public_search"
                bucket.append(copied)

        for market in bucket:
            market_id = str(market.get("id") or market.get("slug") or "")
            if market_id and market_id not in seen:
                seen.add(market_id)
                raw.append(market)

    say(f"    fallback collected {len(raw)} unique markets")
    return raw



def _clob_cricket_markets_fallback() -> tuple[bool, bool, list[dict]]:
    out: list[dict] = []
    next_cursor: Optional[str] = None
    seen_cursors: set[str] = set()
    seen_ids: set[str] = set()

    for page_no in range(1, PAGE_GUARD + 1):
        params = {"next_cursor": next_cursor} if next_cursor else None
        status, body = http.get(
            f"{CLOB}/markets", params, note="clob market discovery",
            quiet=page_no > 1)
        if status != 200 or not isinstance(body, dict):
            return False, False, out

        page = body.get("data") or body.get("markets") or []
        if not isinstance(page, list):
            return False, False, out

        added = 0
        for original in page:
            if not isinstance(original, dict) or _record_explicitly_closed(original):
                continue
            question = str(original.get("question") or "")
            slug = str(original.get("market_slug") or original.get("slug") or "")
            description = str(original.get("description") or "")
            tags = original.get("tags") or []
            if not isinstance(tags, list):
                tags = [str(tags)]
            verdict = classify(question, slug, description, tags=tags)
            if verdict["verdict"] not in ("cricket", "review"):
                continue

            market_id = str(
                original.get("condition_id") or original.get("id") or slug)
            if not market_id or market_id in seen_ids:
                continue
            seen_ids.add(market_id)

            tokens = original.get("tokens") or []
            outcomes = [
                str(token.get("outcome"))
                for token in tokens
                if isinstance(token, dict) and token.get("outcome") is not None
            ]
            out.append({
                "id": market_id,
                "conditionId": original.get("condition_id") or "",
                "question": question,
                "slug": slug,
                "description": description,
                "endDate": original.get("end_date_iso") or "",
                "active": original.get("active"),
                "closed": original.get("closed"),
                "archived": original.get("archived"),
                "outcomes": outcomes,
                "_event_title": question,
                "_event_slug": slug,
                "_event_id": market_id,
                "_event_start_time": original.get("game_start_time") or "",
                "_native_tags": tags,
                "_discovery_source": "clob_markets",
            })
            added += 1

        say(f"    CLOB page {page_no}: +{added} cricket markets, "
            f"total {len(out)}")

        cursor = body.get("next_cursor")
        if SMOKE:
            return True, False, out
        if not cursor or str(cursor) == "LTE=":
            return True, True, out
        cursor = str(cursor)
        if cursor in seen_cursors:
            say("    CLOB repeated next_cursor; coverage is incomplete")
            return True, False, out
        if not page:
            say("    CLOB returned an empty non-terminal page; coverage is incomplete")
            return True, False, out
        seen_cursors.add(cursor)
        next_cursor = cursor

    say("    CLOB page guard reached; coverage is incomplete")
    return True, False, out

def _flatten_polymarket_events(raw: list[dict], native_tagged: bool) -> list[dict]:
    flat: list[dict] = []
    seen: set[str] = set()

    for item in raw:
        nested = item.get("markets")
        if isinstance(nested, list):
            for original in nested:
                if not isinstance(original, dict) or _record_explicitly_closed(original):
                    continue
                market = dict(original)
                market.setdefault("_event_title", item.get("title", ""))
                market.setdefault("_event_slug", item.get("slug", ""))
                market.setdefault("_event_id", str(item.get("id", "")))
                market.setdefault("_event_start_time",
                                  item.get("startTime") or item.get("startDate") or "")
                market.setdefault("_event_date", item.get("eventDate") or "")
                market.setdefault("_series_slug", item.get("seriesSlug") or "")
                market["_discovery_source"] = (
                    "native_tag" if native_tagged else
                    market.get("_discovery_source", "public_search")
                )
                if native_tagged:
                    market["_native_tags"] = ["cricket"]
                market_id = str(market.get("id") or market.get("slug") or "")
                if market_id and market_id not in seen:
                    seen.add(market_id)
                    flat.append(market)
        else:
            if not isinstance(item, dict) or _record_explicitly_closed(item):
                continue
            market = dict(item)
            if native_tagged:
                market["_native_tags"] = ["cricket"]
                market["_discovery_source"] = "native_tag"
            market_id = str(market.get("id") or market.get("slug") or "")
            if market_id and market_id not in seen:
                seen.add(market_id)
                flat.append(market)

    return flat


def fetch_polymarket_intl() -> tuple[list[dict], list[Row], list[Row]]:
    tagged_ok, pagination_complete, raw = _gamma_cricket_events()
    native_tagged = tagged_ok

    if not tagged_ok:
        say("    tagged endpoint failed; trying the public CLOB market feed")
        clob_ok, clob_complete, clob_raw = _clob_cricket_markets_fallback()
        if clob_ok:
            raw = clob_raw
            native_tagged = False
            _set_collection_meta(
                "polymarket_intl", complete=clob_complete,
                method="clob_markets_fallback",
                reason="" if clob_complete else "CLOB pagination did not complete")
        else:
            say("    CLOB fallback failed; trying degraded Gamma public-search")
            raw = _gamma_public_search_fallback()
            native_tagged = False
            _set_collection_meta(
                "polymarket_intl", complete=False,
                method="public_search_fallback",
                reason="native cricket-tag and CLOB market endpoints failed")
    else:
        if not raw:
            say("    tagged endpoint returned zero cricket events; "
                "not running broad keyword fallback")
        _set_collection_meta(
            "polymarket_intl", complete=pagination_complete,
            method="events_keyset_tag_slug_cricket",
            reason="" if pagination_complete else "pagination did not complete")

    flat = _flatten_polymarket_events(raw, native_tagged=native_tagged)
    say(f"    flattened to {len(flat)} unique markets")

    kept: list[Row] = []
    dropped: list[Row] = []
    for m in flat:
        title = m.get("question") or m.get("title") or ""
        slug = m.get("slug") or ""
        group = m.get("_event_title", "") or m.get("_event_slug", "")
        tags = m.get("_native_tags") or []
        r = classify(title, slug, group, tags=tags)

        outcomes = _maybe_json(m.get("outcomes")) or []
        base = dict(
            venue="polymarket_intl",
            market_id=str(m.get("id", "")),
            title=title,
            group_title=group,
            ticker_or_slug=slug,
            close_time=str(m.get("endDate", "")),
            verdict=r["verdict"],
            evidence="; ".join(r["evidence"]),
        )

        rows: list[Row] = []
        if isinstance(outcomes, list) and outcomes:
            for name in outcomes:
                rows.append(Row(
                    **base,
                    outcome=str(name),
                    extra={"discovery_source": m.get("_discovery_source", "")},
                ))
        else:
            rows.append(Row(
                **base,
                extra={"discovery_source": m.get("_discovery_source", "")},
            ))

        target = kept if r["verdict"] in ("cricket", "review") else dropped
        target.extend(rows)

    COLLECTION_META.setdefault("polymarket_intl", {})["records"] = len(flat)
    return flat, kept, dropped


def _flatten_pmus_events(events: list[dict], source: str) -> list[dict]:
    raw: list[dict] = []
    seen: set[str] = set()
    for ev in events:
        if not isinstance(ev, dict) or _record_explicitly_closed(ev):
            continue
        event_teams = ev.get("teams") or ev.get("participants") or []
        for original in (ev.get("markets") or []):
            if not isinstance(original, dict) or _record_explicitly_closed(original):
                continue
            market = dict(original)
            mid = str(market.get("id") or market.get("slug") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            market["_event_ticker"] = ev.get("ticker", "")
            market["_event_slug"] = ev.get("slug", "")
            market["_event_title"] = ev.get("title", "")
            market["_event_description"] = ev.get("description", "")
            market["_event_id"] = str(ev.get("id", ""))
            market["_series_slug"] = ev.get("seriesSlug", "")
            market["_start_time"] = (ev.get("startTime") or
                                     ev.get("eventDate") or
                                     ev.get("startDate") or "")
            market["_event_date"] = ev.get("eventDate") or ""
            market["_event_teams"] = event_teams
            market["_event_status"] = ev.get("status") or ev.get("state") or ""
            market["_native_sport"] = "cricket" if source == "native_sport" else ""
            market["_discovery_source"] = source
            raw.append(market)
    return raw

def _pmus_native_cricket_events() -> tuple[bool, bool, list[dict]]:
    events: list[dict] = []
    offset = 0
    limit = 100
    seen_ids: set[str] = set()
    seen_pages: set[tuple[str, ...]] = set()
    for page_no in range(1, PAGE_GUARD + 1):
        status, body = http.get(
            f"{PMUS}/v2/sports/cricket/events",
            {"limit": limit, "offset": offset, "type": "sport",
             "section": "general"},
            note="pmus native cricket sport", quiet=page_no > 1)
        if status != 200:
            return False, False, events
        if not isinstance(body, dict):
            return False, False, events
        page = body.get("events") or []
        if not isinstance(page, list):
            return False, False, events

        page_ids = tuple(str((ev or {}).get("id") or (ev or {}).get("slug") or "")
                         for ev in page)
        if page and page_ids in seen_pages:
            say("    pmus pagination repeated a page; coverage incomplete")
            return True, False, events
        seen_pages.add(page_ids)

        added = 0
        for ev in page:
            eid = str((ev or {}).get("id") or (ev or {}).get("slug") or "")
            if eid and eid not in seen_ids:
                seen_ids.add(eid)
                events.append(ev)
                added += 1
        say(f"    pmus cricket page {page_no}: +{added}, total {len(events)}")
        if SMOKE:
            return True, False, events
        if len(page) < limit:
            return True, True, events
        if page and added == 0:
            say("    pmus pagination made no progress; coverage incomplete")
            return True, False, events
        offset += limit
    return True, False, events

def _pmus_search_fallback() -> list[dict]:
    events: list[dict] = []
    seen: set[str] = set()
    terms = ["cricket"] if SMOKE else [
        "cricket", "t20 cricket", "odi cricket", "test cricket",
        "the hundred cricket"]
    for term in terms:
        status, body = http.get(
            f"{PMUS}/v1/search", {"query": term, "limit": 100},
            note=f"pmus search fallback {term}")
        if status != 200 or not isinstance(body, dict):
            continue
        page = body.get("events") or body.get("data") or []
        if not isinstance(page, list):
            continue
        for ev in page:
            eid = str((ev or {}).get("id") or (ev or {}).get("slug") or "")
            if eid and eid not in seen:
                seen.add(eid)
                events.append(ev)
    return events


def fetch_polymarket_us() -> tuple[list[dict], list[Row], list[Row]]:
    native_ok, pagination_complete, events = _pmus_native_cricket_events()
    if native_ok:
        raw = _flatten_pmus_events(events, "native_sport")
        _set_collection_meta(
            "polymarket_us", complete=pagination_complete,
            method="v2_sports_cricket_events",
            reason="" if pagination_complete else "pagination did not complete")
    else:
        say("    native cricket sport endpoint failed; using degraded search")
        events = _pmus_search_fallback()
        raw = _flatten_pmus_events(events, "search_fallback")
        _set_collection_meta(
            "polymarket_us", complete=False, method="search_fallback",
            reason="native /v2/sports/cricket/events failed")

    kept: list[Row] = []
    dropped: list[Row] = []
    for m in raw:
        title = m.get("question") or m.get("_event_title") or ""
        ident = str(m.get("slug") or m.get("id") or "")
        if m.get("_native_sport") == "cricket":
            result = {"verdict": "cricket",
                      "evidence": ["native Polymarket US cricket sport endpoint"]}
        else:
            result = classify(title, ident, m.get("_event_ticker"),
                              m.get("_series_slug"), m.get("description"))
        row = Row(venue="polymarket_us", market_id=str(m.get("id", "")),
                  title=title, group_title=m.get("_event_title", ""),
                  ticker_or_slug=ident, close_time=str(m.get("endDate", "")),
                  verdict=result["verdict"],
                  evidence="; ".join(result["evidence"]))
        (kept if result["verdict"] in ("cricket", "review") else dropped).append(row)
    COLLECTION_META.setdefault("polymarket_us", {})["records"] = len(raw)
    return raw, kept, dropped


KALSHI_SERIES_BLOCKLIST = {
    "KXSCOTTISHPREMGAME",
    "KXBBLGAME",
}


def _collect_kx_tickers(node: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(node, str):
        v = node.upper().strip()
        if v.startswith("KX") and v.replace("_", "").isalnum():
            out.add(v)
    elif isinstance(node, list):
        for item in node:
            out |= _collect_kx_tickers(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in {"ticker", "series_ticker", "seriesticker"}:
                out |= _collect_kx_tickers(value)
            else:
                out |= _collect_kx_tickers(value)
    return out


def _find_cricket_filter_node(filters: Any) -> Any:
    if not isinstance(filters, dict):
        return None
    for key, value in filters.items():
        if str(key).strip().lower() == "cricket":
            return value
        if isinstance(value, dict):
            label = " ".join(str(value.get(k) or "") for k in
                             ("name", "label", "sport", "slug")).lower()
            if "cricket" in label:
                return value
    return None


def _kalshi_cricket_series() -> tuple[list[str], bool, dict[str, str], set[str]]:
    known = set(U.KNOWN_KALSHI_CRICKET_SERIES) - KALSHI_SERIES_BLOCKLIST
    found = set(known)
    native_found: set[str] = set()
    titles: dict[str, str] = {}

    filters_ok = False
    status, body = http.get(f"{KALSHI}/search/filters_by_sport",
                            note="kalshi filters by sport")
    if status == 200 and isinstance(body, dict):
        filters = body.get("filters_by_sports") or {}
        filters_ok = _find_cricket_filter_node(filters) is not None

    native_series_ok = False
    status, body = http.get(
        f"{KALSHI}/series", {"category": "Sports", "tags": "Cricket"},
        note="kalshi native cricket series")
    if status == 200 and isinstance(body, dict):
        series_items = body.get("series") or []
        if isinstance(series_items, list):
            native_series_ok = True
            for series in series_items:
                if not isinstance(series, dict):
                    continue
                ticker = (series.get("ticker") or
                          series.get("series_ticker") or "").upper()
                if not ticker or ticker in KALSHI_SERIES_BLOCKLIST:
                    continue
                native_found.add(ticker)
                titles[ticker] = str(series.get("title") or "")
            found |= native_found


    if not native_series_ok:
        status, body = http.get(f"{KALSHI}/series", {"category": "Sports"},
                                note="kalshi sports series fallback")
        if status == 200 and isinstance(body, dict):
            for series in (body.get("series") or []):
                if not isinstance(series, dict):
                    continue
                ticker = (series.get("ticker") or
                          series.get("series_ticker") or "").upper()
                if not ticker or ticker in KALSHI_SERIES_BLOCKLIST:
                    continue
                titles[ticker] = str(series.get("title") or "")
                result = classify(ticker, series.get("title"),
                                  series.get("category"),
                                  " ".join(series.get("tags") or []),
                                  series=ticker)
                if result["verdict"] == "cricket":
                    found.add(ticker)


    complete = filters_ok and native_series_ok
    trusted = native_found - KALSHI_SERIES_BLOCKLIST
    return sorted(found), complete, titles, trusted


def fetch_kalshi() -> tuple[list[dict], list[Row], list[Row]]:
    raw: list[dict] = []
    tickers, native_complete, series_titles, trusted_series = _kalshi_cricket_series()
    say(f"    {len(tickers)} candidate cricket series")
    if SMOKE:
        tickers = tickers[:3]
        native_complete = False

    failed_series: list[str] = []
    untrusted_series_with_markets: list[str] = []
    seen_global_markets: set[str] = set()
    for ticker in tickers:
        cursor = None
        seen_cursors: set[str] = set()
        seen_series_markets: set[str] = set()
        got = 0
        for _ in range(PAGE_GUARD):
            params: dict[str, Any] = {
                "limit": 1000, "series_ticker": ticker,
                "status": "settled" if CLOSED else "open"}
            if cursor:
                params["cursor"] = cursor
            status, body = http.get(f"{KALSHI}/markets", params,
                                    note=f"kalshi {ticker}", quiet=True)
            if status != 200 or not isinstance(body, dict):
                failed_series.append(ticker)
                break
            page = body.get("markets") or []
            if not isinstance(page, list):
                failed_series.append(ticker)
                break
            added = 0
            for original in page:
                market = dict(original)
                market_id = str(market.get("ticker") or market.get("id") or "")
                if not market_id or market_id in seen_series_markets:
                    continue
                seen_series_markets.add(market_id)
                if market_id in seen_global_markets:
                    failed_series.append(ticker)
                    continue
                seen_global_markets.add(market_id)
                market["_queried_series"] = ticker
                market["_series_title"] = series_titles.get(ticker, "")
                market["_native_cricket_series"] = ticker in trusted_series
                raw.append(market)
                got += 1
                added += 1
            next_cursor = body.get("cursor")
            if not next_cursor or not page:
                break
            if added == 0:
                failed_series.append(ticker)
                break
            next_cursor = str(next_cursor)
            if next_cursor in seen_cursors:
                failed_series.append(ticker)
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            failed_series.append(ticker)
        if got:
            say(f"      {ticker}: {got}")
            if ticker not in trusted_series:
                untrusted_series_with_markets.append(ticker)

    complete = native_complete and not failed_series and not untrusted_series_with_markets
    reason_parts = []
    if not native_complete:
        reason_parts.append("native cricket sport filter did not yield a complete series set")
    if failed_series:
        reason_parts.append(f"{len(set(failed_series))} series requests failed or truncated")
    if untrusted_series_with_markets:
        reason_parts.append(
            "open markets appeared in series absent from the native Cricket tag: "
            + ", ".join(sorted(set(untrusted_series_with_markets))))
    _set_collection_meta(
        "kalshi", complete=complete, method="sports_filter_plus_series",
        reason="; ".join(reason_parts), records=len(raw))

    kept: list[Row] = []
    dropped: list[Row] = []
    for market in raw:
        ticker = market.get("ticker") or ""
        series = (market.get("_queried_series") or
                  market.get("series_ticker") or "").upper()
        if market.get("_native_cricket_series"):
            result = {"verdict": "cricket",
                      "evidence": ["native/verified Kalshi cricket series"]}
        else:
            result = classify(market.get("title"), ticker,
                              market.get("event_ticker"),
                              market.get("yes_sub_title"), series=series)
        row = Row(venue="kalshi", market_id=ticker,
                  title=market.get("title", ""),
                  group_title=market.get("event_ticker", ""),
                  ticker_or_slug=ticker,
                  outcome=market.get("yes_sub_title") or ticker.rsplit("-", 1)[-1],
                  close_time=str(market.get("close_time", "")),
                  verdict=result["verdict"],
                  evidence="; ".join(result["evidence"]),
                  extra={"series_ticker": series})
        (kept if result["verdict"] in ("cricket", "review") else dropped).append(row)
    return raw, kept, dropped
