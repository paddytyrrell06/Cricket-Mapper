"""Bounded, independent public market discovery for research, without quote polling.

Native references: docs.kalshi.com/api-reference/events/get-events and
docs.polymarket.com/market-data/discover-markets (Gamma keyset pagination).
"""
from __future__ import annotations

import json
from collections import defaultdict
from functools import lru_cache
from threading import Lock

import canonical as C
import collectors
from research_store import pair_key, utc


VENUES = ("kalshi", "polymarket_intl")
PAGE_SIZE = 200
MAX_PAGES = 20

# Category labels are a presentation taxonomy over explicit provider metadata,
# not title inference or a discovery gate. Unknown labels remain Other.
CATEGORY_LABELS = {
    "sports": "Sports", "esports": "Esports", "politics": "Politics",
    "elections": "Politics", "us-election": "Politics", "economics": "Economics",
    "economy": "Economics", "finance": "Finance", "financials": "Finance",
    "business": "Finance", "companies": "Finance", "crypto": "Crypto",
    "cryptocurrency": "Crypto", "geopolitics": "Geopolitics", "world": "Geopolitics",
    "weather": "Weather", "climate-and-weather": "Weather", "climate": "Weather",
    "entertainment": "Entertainment", "pop-culture": "Entertainment", "culture": "Entertainment",
    "technology": "Technology", "tech": "Technology", "science-and-technology": "Technology",
    "science": "Science", "health": "Health",
}
CATEGORY_PRIORITY = ("Esports", "Sports", "Weather", "Crypto", "Geopolitics",
                     "Economics", "Finance", "Technology", "Science", "Health", "Entertainment", "Politics")

SPORT_LABELS = {
    "soccer": "Football (soccer)", "football": "American football", "basketball": "Basketball",
    "baseball": "Baseball", "hockey": "Ice hockey", "cricket": "Cricket", "tennis": "Tennis",
    "mma": "MMA", "boxing": "Boxing", "golf": "Golf", "rugby": "Rugby", "motorsport": "Motorsport",
    "cs2": "Counter-Strike 2", "lol": "League of Legends", "dota2": "Dota 2", "valorant": "Valorant",
    "esports": "Esports (unspecified)", "unknown": "Unspecified sport",
}
SPORT_ALIASES = {
    "soccer": "soccer", "association-football": "soccer", "american-football": "football",
    "nfl": "football", "ncaaf": "football", "college-football": "football", "football": "football",
    "basketball": "basketball", "nba": "basketball", "wnba": "basketball", "ncaab": "basketball",
    "baseball": "baseball", "mlb": "baseball", "hockey": "hockey", "ice-hockey": "hockey", "nhl": "hockey",
    "cricket": "cricket", "tennis": "tennis", "atp": "tennis", "wta": "tennis", "mma": "mma", "ufc": "mma",
    "boxing": "boxing", "golf": "golf", "rugby": "rugby", "formula-1": "motorsport", "f1": "motorsport",
    "motorsport": "motorsport", "cs2": "cs2", "counter-strike-2": "cs2", "counter-strike": "cs2",
    "lol": "lol", "league-of-legends": "lol", "dota-2": "dota2", "dota2": "dota2", "valorant": "valorant",
}


def _sport_name(value):
    return SPORT_ALIASES.get(str(value or "").strip().casefold().replace(" ", "-"), "")


@lru_cache(maxsize=1)
def _sport_series():
    # Reuse the exact native series already supported by our collectors. This
    # is metadata classification, never inference from a contract's title.
    from sports import SPORT_CONFIGS, SOCCER_COMPETITIONS, NATIVE_COMPETITIONS
    from niches import ESPORTS_COMPETITIONS
    from universe import KNOWN_KALSHI_CRICKET_SERIES
    result = {item["series"]: sport for sport, item in SPORT_CONFIGS.items()}
    result.update({item["series"]: "soccer" for item in SOCCER_COMPETITIONS.values()})
    result.update({item["series"]: item["sport"] for item in NATIVE_COMPETITIONS.values()})
    result.update({item["series"]: sport for sport, item in ESPORTS_COMPETITIONS.items()})
    result.update({series: "cricket" for series in KNOWN_KALSHI_CRICKET_SERIES})
    return result


def native_sport(event, market, category):
    """Classify only explicit native sport, league, tags or a known series."""
    if category not in {"Sports", "Esports"}:
        return ""
    for source in (market, event):
        for key in ("sport", "sport_name", "league"):
            value = source.get(key)
            if isinstance(value, dict):
                value = value.get("slug") or value.get("name")
            if sport := _sport_name(value):
                return sport
    sports = {_sport_name(tag.get("slug") or tag.get("label")) for source in (event, market)
              for tag in (source.get("tags") if isinstance(source.get("tags"), list) else []) if isinstance(tag, dict)} - {""}
    if len(sports) == 1:
        return sports.pop()
    if not sports and (sport := _sport_series().get(str(event.get("series_ticker") or ""))):
        return sport
    return "unknown"


def _category(event, venue):
    native = str(event.get("category") or "")
    tags = event.get("tags") if isinstance(event.get("tags"), list) else []
    labels = [native] if native else [str(t.get("slug") or t.get("label") or "")
                                        for t in tags if isinstance(t, dict)]
    categories = {CATEGORY_LABELS.get(label.strip().lower().replace(" ", "-")) for label in labels}
    category = next((label for label in CATEGORY_PRIORITY if label in categories), "Other")
    return category, ("native_event_category" if native else "native_event_tags"), tags


def _list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _outcomes(market, venue):
    if venue == "kalshi":
        return [("Yes", str(market.get("ticker") or ""))]
    labels = market.get("outcomes")
    # Current SDK-normalized objects may expose exact token IDs by named side;
    # position IDs are not interchangeable with CLOB tokens.
    if isinstance(labels, dict):
        return [(str(label).capitalize(), str(value.get("tokenId") or value.get("token_id") or ""))
                for label, value in labels.items() if isinstance(value, dict)]
    labels, tokens = _list(labels), _list(market.get("clobTokenIds"))
    if not labels:
        return [("Outcome unavailable", "")]
    # A length mismatch makes index mapping uncertain; retain discovery but do
    # not guess an instrument or use outcomePrices as executable quotes.
    trusted = (len(labels) == len(tokens) and len(set(map(str, tokens))) == len(tokens)
               and len({C.norm_team(label) for label in labels}) == len(labels))
    return [(str(label), str(tokens[i] or "") if trusted else "") for i, label in enumerate(labels)]


class MarketCatalog:
    def __init__(self, store, requester=None):
        self.store = store
        self.requester = requester or (lambda url, params: collectors.http.get(url, params, quiet=True))
        self._refresh_lock = Lock()
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS research_catalog(
                    id TEXT PRIMARY KEY, exchange TEXT NOT NULL, market_id TEXT NOT NULL,
                    category TEXT NOT NULL, outcome TEXT NOT NULL, instrument_id TEXT,
                    discovered_at TEXT NOT NULL, row_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS catalog_exchange ON research_catalog(exchange,market_id);
                CREATE INDEX IF NOT EXISTS observation_instrument ON observations(exchange,instrument_id);
                CREATE UNIQUE INDEX IF NOT EXISTS catalog_outcome ON research_catalog(exchange,market_id,outcome);
                CREATE TABLE IF NOT EXISTS research_catalog_state(exchange TEXT PRIMARY KEY,data_json TEXT NOT NULL);
            """)

    def _state(self, venue):
        with self.store.connect() as db:
            row = db.execute("SELECT data_json FROM research_catalog_state WHERE exchange=?", (venue,)).fetchone()
        return json.loads(row[0]) if row else {}

    def _normalise(self, event, market, venue, observed_at):
        event_id = str(event.get("event_ticker" if venue == "kalshi" else "id") or "")
        market_id = str(market.get("ticker" if venue == "kalshi" else "id") or "")
        if not event_id or not market_id:
            return []
        category, category_source, tags = _category(event, venue)
        sport = native_sport(event, market, category)
        status = str(market.get("status") or ("closed" if market.get("closed") is True
                      else "active" if market.get("active") is True else "unknown")).lower()
        title = str(market.get("title" if venue == "kalshi" else "question") or event.get("title") or market_id)
        # Categorical Kalshi strikes can share a question; retain the named
        # YES proposition so candidates/thresholds remain distinguishable.
        subtitle = str(market.get("yes_sub_title") or "").strip() if venue == "kalshi" else ""
        if subtitle and subtitle.casefold() not in title.casefold():
            title += " · " + subtitle
        expiry = market.get("close_time" if venue == "kalshi" else "endDate") or ""
        volume = market.get("volume_fp" if venue == "kalshi" else "volume")
        rules = "\n".join(str(market.get(key) or "") for key in ("rules_primary", "rules_secondary")) if venue == "kalshi" else str(market.get("description") or event.get("description") or "")
        raw_event = {key: value for key, value in event.items() if key != "markets"}
        result = []
        for outcome, instrument in _outcomes(market, venue):
            row = {
                "sport": "catalog", "canonical_fixture_id": "catalog:" + venue + ":" + event_id,
                "canonical_market_type": market_id, "canonical_outcome": outcome,
                "research_title": title, "category": category, "category_source": category_source,
                "native_category": event.get("category"), "native_tags": tags,
                "sport_code": sport, "sport_label": SPORT_LABELS.get(sport, ""),
                "mapping_status": "unmatched", "comparison_status": "unmatched",
                "fixture_date": str(expiry)[:10], "discovery_source": "native_public_catalog",
                "discovered_at": observed_at, "research_exchange": venue,
                venue + "_market_id": market_id, venue + "_event_id": event_id,
                venue + "_market_status": status, venue + "_metadata_time": observed_at,
                venue + "_volume": str(volume) if volume is not None else None,
                venue + "_volume_time": observed_at if volume is not None else None,
                venue + "_rules": rules, venue + "_expiry": str(expiry),
                venue + "_quote_status": "not_collected",
                "raw_metadata": {"event": raw_event, "market": market},
            }
            result.append((pair_key(row), venue, market_id, category, outcome, instrument or None,
                           observed_at, json.dumps(row, separators=(",", ":"))))
        return result

    def refresh(self, max_pages=6):
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= MAX_PAGES:
            raise ValueError("Catalog budget must be 1–20 pages per exchange")
        if not self._refresh_lock.acquire(blocking=False):
            raise ValueError("Catalog discovery is already running")
        try:
            for venue in VENUES:
                self._refresh_venue(venue, max_pages)
            return self.summary()
        finally:
            self._refresh_lock.release()

    def _refresh_venue(self, venue, budget):
        previous = self._state(venue)
        cursor = previous.get("next_cursor") or ""
        state = {"last_refresh": utc(), "pages": 0, "requests": 0, "events": 0,
                 "returned_markets": 0, "missing_instruments": 0, "malformed_records": 0,
                 "complete": False, "capped": False, "has_more": bool(cursor),
                 "next_cursor": cursor, "warning": "", "scope": "open_events_with_nested_contracts",
                 "request_count_note": "Logical page calls; shared HTTP transport may retry a failed request"}
        seen_cursors = {cursor} if cursor else set()
        for _ in range(budget):
            params = {"limit": PAGE_SIZE}
            if venue == "kalshi":
                url = collectors.KALSHI + "/events"
                params.update(status="open", with_nested_markets="true")
                if cursor:
                    params["cursor"] = cursor
            else:
                url = collectors.GAMMA + "/events/keyset"
                params.update(closed="false")
                if cursor:
                    params["after_cursor"] = cursor
            state["requests"] += 1
            try:
                response = self.requester(url, params)
                status, body = response[:2]
                # Cached verified responses can retain their actual receive
                # time instead of pretending a local re-import is fresh data.
                response_time = response[2] if len(response) == 3 else None
            except Exception:
                status, body, response_time = None, None, None
            if status != 200 or not isinstance(body, dict) or not isinstance(body.get("events"), list):
                state["warning"] = "Public discovery response failed or malformed" + (" (HTTP " + str(status) + ")" if status else "")
                # A provider may expire cursors. Reset only after this explicit
                # failed continuation; do not spend extra requests on retries.
                if cursor and status in (400, 404, 410, 422):
                    state["next_cursor"] = ""
                    state["warning"] += "; continuation reset for the next scan"
                break
            events = body["events"]
            state["pages"] += 1
            state["events"] += len(events)
            normalised = []
            observed_at = utc(response_time)
            for event in events:
                if not isinstance(event, dict) or not isinstance(event.get("markets"), list):
                    state["malformed_records"] += 1
                    continue
                for market in event["markets"]:
                    state["returned_markets"] += 1
                    if not isinstance(market, dict):
                        state["malformed_records"] += 1
                        continue
                    rows = self._normalise(event, market, venue, observed_at)
                    state["malformed_records"] += not bool(rows)
                    state["missing_instruments"] += sum(not row[5] for row in rows)
                    normalised.extend(rows)
            cursor = str(body.get("cursor" if venue == "kalshi" else "next_cursor") or "")
            state["next_cursor"], state["has_more"] = cursor, bool(cursor)
            if cursor and (cursor in seen_cursors or not events):
                state["warning"] = "Pagination stopped: repeated cursor or empty page with continuation"
                state["next_cursor"] = ""
            elif not cursor:
                state["complete"] = True
            with self.store.connect() as db:
                db.executemany("INSERT OR REPLACE INTO research_catalog VALUES(?,?,?,?,?,?,?,?)", normalised)
                db.execute("INSERT OR REPLACE INTO research_catalog_state VALUES(?,?)", (venue, json.dumps(state)))
            if state["complete"] or state["warning"]:
                break
            seen_cursors.add(cursor)
        if state["has_more"] and not state["warning"]:
            state["capped"] = True
            state["warning"] = "Request budget reached; next discovery resumes the saved cursor"
        if state["malformed_records"]:
            state["complete"] = False
            state["warning"] += ("; " if state["warning"] else "") + "Some provider records lack required metadata; see malformed_records"
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO research_catalog_state VALUES(?,?)", (venue, json.dumps(state)))

    def markets(self):
        with self.store.connect() as db:
            rows = db.execute("""SELECT id,exchange,market_id,category,outcome,instrument_id,discovered_at,
                json_extract(row_json,'$.research_title') title,
                json_extract(row_json,'$.fixture_date') fixture_date,
                COALESCE(json_extract(row_json,'$.kalshi_market_status'),
                         json_extract(row_json,'$.polymarket_intl_market_status')) market_status
                FROM research_catalog ORDER BY category,exchange,market_id,outcome""").fetchall()
        result = []
        for item in rows:
            result.append({"id": item["id"], "name": item["title"] + " / " + item["outcome"],
                           "sport": "catalog", "category": item["category"], "exchange": item["exchange"],
                           "comparison_status": "unmatched", "fixture_date": item["fixture_date"],
                           "status": item["market_status"], "market_status": item["market_status"],
                           "discovered_at": item["discovered_at"],
                           "instruments": {item["exchange"]: item["instrument_id"]} if item["instrument_id"] else {}})
        return result

    def page(self, *, search="", category="", exchange="", status="", limit=100,
             offset=0, sort="name", direction="asc"):
        """A bounded local catalog page, with only genuinely recorded quotes."""
        order_columns = {"name": "title COLLATE NOCASE", "category": "category COLLATE NOCASE", "exchange": "exchange",
                         "status": "status", "volume": "CAST(volume AS REAL)", "expiry": "expiry"}
        if sort not in order_columns or direction not in ("asc", "desc"):
            raise ValueError("Unsupported catalog sort")
        if (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
                or isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 1000000):
            raise ValueError("Use 1–100 rows and a nonnegative offset up to 1,000,000")
        for value in (search, category, exchange, status):
            if not isinstance(value, str) or len(value) > 200:
                raise ValueError("Invalid catalog filter")
        if exchange and exchange not in VENUES:
            raise ValueError("Unsupported exchange")
        active = ("active", "open")
        closed = ("closed", "settled", "resolved", "finalized", "archived", "inactive", "cancelled", "canceled")
        if status and status not in (*active, *closed, "unknown"):
            raise ValueError("Unsupported catalog status")
        # Project metadata only; outcome probabilities never become bid/ask.
        projection = """SELECT id,exchange,market_id,category,outcome,instrument_id,discovered_at,
            json_extract(row_json,'$.research_title') title,
            COALESCE(json_extract(row_json,'$.kalshi_market_status'),
                     json_extract(row_json,'$.polymarket_intl_market_status'),'unknown') status,
            COALESCE(json_extract(row_json,'$.kalshi_volume'),json_extract(row_json,'$.polymarket_intl_volume')) volume,
            COALESCE(json_extract(row_json,'$.kalshi_expiry'),json_extract(row_json,'$.polymarket_intl_expiry')) expiry
            FROM research_catalog"""
        clauses, values = [], []
        if search.strip():
            clauses.append("(instr(lower(title),lower(?))>0 OR instr(lower(outcome),lower(?))>0 OR instr(lower(market_id),lower(?))>0)")
            values.extend([search.strip()] * 3)
        for field, value in (("category", category), ("exchange", exchange)):
            if value:
                clauses.append(field + "=?")
                values.append(value)
        if status:
            group = active if status == "active" else closed if status == "closed" else (status,)
            if status == "unknown":
                clauses.append("lower(status) NOT IN (" + ",".join("?" for _ in (*active, *closed)) + ")")
                values.extend((*active, *closed))
            else:
                clauses.append("lower(status) IN (" + ",".join("?" for _ in group) + ")")
                values.extend(group)
        filtered = " FROM (" + projection + ") catalog" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        with self.store.connect() as db:
            total = db.execute("SELECT COUNT(*)" + filtered, values).fetchone()[0]
            entries = [dict(row) for row in db.execute("SELECT *" + filtered + " ORDER BY " + order_columns[sort]
                + " " + direction.upper() + ",id ASC LIMIT ? OFFSET ?", [*values, limit, offset])]
        quotes = self.store.latest_quotes((row["exchange"], row["instrument_id"]) for row in entries if row["instrument_id"])
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        for row in entries:
            row["name"] = row["title"] + " / " + row["outcome"]
            row["volume_unit"] = "contracts" if row["exchange"] == "kalshi" else "USD"
            quote = quotes.get((row["exchange"], row["instrument_id"]), {})
            for key in ("bid", "ask", "bid_size", "ask_size", "observed_at", "quote_status", "source"):
                row[key] = quote.get(key)
            row["quote_age_seconds"] = None
            try:
                stamp = datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
                if stamp.tzinfo:
                    row["quote_age_seconds"] = round((now - stamp).total_seconds(), 3)
            except (AttributeError, ValueError, TypeError, OverflowError):
                pass
        return {"markets": entries, "total": total, "coverage": self.summary()}

    def terminal_entries(self):
        """Lightweight local metadata for assembling one unified market index."""
        with self.store.connect() as db:
            entries = db.execute("""SELECT id,exchange,market_id,category,outcome,instrument_id,discovered_at,
                json_extract(row_json,'$.research_title') title,
                COALESCE(json_extract(row_json,'$.kalshi_market_status'),json_extract(row_json,'$.polymarket_intl_market_status'),'unknown') status,
                COALESCE(json_extract(row_json,'$.kalshi_volume'),json_extract(row_json,'$.polymarket_intl_volume')) volume,
                COALESCE(json_extract(row_json,'$.kalshi_expiry'),json_extract(row_json,'$.polymarket_intl_expiry')) expiry,
                json_extract(row_json,'$.sport_code') sport,
                json_extract(row_json,'$.raw_metadata.event.tags') sport_tags,
                COALESCE(json_extract(row_json,'$.raw_metadata.event.sport'),json_extract(row_json,'$.raw_metadata.event.sport_name')) event_sport,
                COALESCE(json_extract(row_json,'$.raw_metadata.market.sport'),json_extract(row_json,'$.raw_metadata.market.sport_name')) market_sport,
                json_extract(row_json,'$.raw_metadata.event.league') event_league,
                json_extract(row_json,'$.raw_metadata.market.league') market_league,
                json_extract(row_json,'$.raw_metadata.event.series_ticker') sport_series
                FROM research_catalog ORDER BY id""").fetchall()
        result = []
        for row in entries:
            item = dict(row)
            # Old saved catalogs gain sport navigation without rediscovery or
            # a database migration. Read only the required metadata fields.
            metadata = {key: item.pop(key) for key in ("sport_tags", "event_sport", "market_sport", "event_league", "market_league", "sport_series")}
            def unpack(value):
                if isinstance(value, str) and value.startswith(("{", "[")):
                    try:
                        return json.loads(value)
                    except ValueError:
                        pass
                return value
            item["sport"] = item["sport"] or native_sport(
                {"sport": unpack(metadata["event_sport"]), "league": unpack(metadata["event_league"]),
                 "tags": unpack(metadata["sport_tags"]), "series_ticker": metadata["sport_series"]},
                {"sport": unpack(metadata["market_sport"]), "league": unpack(metadata["market_league"])}, item["category"])
            item["sport_label"] = SPORT_LABELS.get(item["sport"], "")
            result.append(item)
        return result

    def lookup(self, identifier):
        with self.store.connect() as db:
            item = db.execute("SELECT * FROM research_catalog WHERE id=?", (identifier,)).fetchone()
        if item is None:
            return None
        row = json.loads(item["row_json"])
        assets = {(item["exchange"], item["market_id"], C.norm_team(item["outcome"])): item["instrument_id"]} if item["instrument_id"] else {}
        return row, assets

    def summary(self):
        with self.store.connect() as db:
            groups = db.execute("SELECT exchange,category,COUNT(DISTINCT market_id) markets,COUNT(*) outcomes FROM research_catalog GROUP BY exchange,category").fetchall()
            statuses = db.execute("""SELECT exchange,
                CASE lower(COALESCE(json_extract(row_json,'$.kalshi_market_status'),json_extract(row_json,'$.polymarket_intl_market_status'),'unknown'))
                  WHEN 'active' THEN 'active' WHEN 'open' THEN 'active'
                  WHEN 'closed' THEN 'closed' WHEN 'settled' THEN 'closed'
                  WHEN 'finalized' THEN 'closed' WHEN 'resolved' THEN 'closed'
                  ELSE 'unknown' END status, COUNT(DISTINCT market_id) count
                FROM research_catalog GROUP BY exchange,status""").fetchall()
        exchanges, categories = {}, defaultdict(lambda: {"markets": 0, "outcomes": 0, "exchanges": {}})
        for venue in VENUES:
            state = self._state(venue)
            # Keep provider cursors internal; only expose whether more data is available.
            state.pop("next_cursor", None)
            exchanges[venue] = {"markets": 0, "outcomes": 0, "categories": {}, "complete": False,
                                "status_counts": {"active": 0, "closed": 0, "unknown": 0},
                                "warning": "Not yet discovered", **state}
        for item in groups:
            venue, category, markets, outcomes = tuple(item)
            exchanges[venue]["markets"] += markets
            exchanges[venue]["outcomes"] += outcomes
            exchanges[venue]["categories"][category] = markets
            categories[category]["markets"] += markets
            categories[category]["outcomes"] += outcomes
            categories[category]["exchanges"][venue] = markets
        for item in statuses:
            exchanges[item["exchange"]]["status_counts"][item["status"]] = item["count"]
        return {"markets": sum(v["markets"] for v in exchanges.values()),
                "outcomes": sum(v["outcomes"] for v in exchanges.values()), "exchanges": exchanges,
                "categories": [{"category": key, **value} for key, value in sorted(categories.items())],
                "note": "Cached open-event metadata, including closed child contracts; not a matched or monitored universe. Capped scans resume. Cached absence does not imply delisting; metadata can become stale."}
