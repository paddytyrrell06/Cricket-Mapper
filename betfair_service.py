"""Persist and present native Betfair prices without entering binary-price maths."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import presentation as P
from betfair_data import BetfairData, Config
from betfair_mapping import map_markets


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def runner_key(market_id, runner):
    return str(market_id), str(runner.get("selection_id")), str(runner.get("handicap"))


class BetfairService:
    def __init__(self, root, store, runtime=None, provider=None, *, clock=None):
        self.root, self.store, self.runtime = Path(root), store, runtime
        self.provider = provider or BetfairData(Config.from_env(root=self.root))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock, self._refresh_lock = threading.RLock(), threading.Lock()
        self._raw_signature, self._raw = None, []
        self._synthetic = self.provider.status().get("mode") == "synthetic"
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS betfair_markets(
                    market_id TEXT PRIMARY KEY,payload TEXT NOT NULL,book_signature TEXT);
                CREATE TABLE IF NOT EXISTS betfair_books(
                    id INTEGER PRIMARY KEY,market_id TEXT NOT NULL,observed_at TEXT NOT NULL,
                    mode TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS betfair_book_market ON betfair_books(market_id,id);
                CREATE TABLE IF NOT EXISTS betfair_mappings(
                    mapping_key TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS betfair_mapping_versions(
                    mapping_key TEXT NOT NULL,fingerprint TEXT NOT NULL,observed_at TEXT NOT NULL,
                    payload TEXT NOT NULL,PRIMARY KEY(mapping_key,fingerprint));
                CREATE TABLE IF NOT EXISTS betfair_state(id INTEGER PRIMARY KEY,payload TEXT NOT NULL);
            """)
            saved = [json.loads(row[0]) for row in db.execute("SELECT payload FROM betfair_markets ORDER BY market_id")]
            self._markets = [market for market in saved if (market.get("data_mode") == "synthetic") == self._synthetic]
            ids = {market["market_id"] for market in self._markets}
            self._mappings = [item for row in db.execute("SELECT payload FROM betfair_mappings ORDER BY mapping_key")
                              if (item := json.loads(row[0]))["betfair_market_id"] in ids]
            state = db.execute("SELECT payload FROM betfair_state WHERE id=1").fetchone()
            saved_state = json.loads(state[0]) if state else {}
            self._state = saved_state if (saved_state.get("mode") == "synthetic") == self._synthetic else {}
            if self.provider.status().get("state") == "ready":
                self._state.update(state="ready", status="ready")

    def _native_polymarket(self):
        path = self.root / "output/data/raw_basketball_polymarket_intl.json"
        try:
            signature = path.stat().st_mtime_ns
            if signature != self._raw_signature:
                self._raw = json.loads(path.read_text())
                self._raw_signature = signature
        except (OSError, ValueError):
            self._raw, self._raw_signature = [], None
        return self._raw

    def status(self):
        with self._lock:
            configuration = self.provider.status()
            enabled = configuration.get("state") != "not_configured"
            state = {**configuration, **self._state} if enabled else {**self._state, **configuration}
            counts = Counter(mapping["status"] for mapping in self._mappings)
            state.update(market_count=len(self._markets), runner_count=sum(len(m.get("runners", [])) for m in self._markets),
                         mapping_counts=dict(counts), last_refresh=self._state.get("last_refresh"),
                         note=state.get("message") or "Native odds and stake sizes; fixture links do not establish a hedge.",
                         history_note="Changed REST books only; original receipt clocks retained. No interpolation or fill inference.")
            return copy.deepcopy(state)

    @staticmethod
    def _book(market):
        return {key: copy.deepcopy(market.get(key)) for key in (
            "market_id", "market_status", "market_version", "inplay", "bet_delay", "is_market_data_delayed",
            "data_mode", "currency", "book_available", "observed_at", "received_monotonic_ns", "source_publish_time", "runners")}

    @staticmethod
    def _signature(book):
        value = copy.deepcopy(book)
        for item in [value, *value.get("runners", [])]:
            for field in ("observed_at", "received_monotonic_ns", "source_publish_time"):
                item.pop(field, None)
        return hashlib.sha256(packed(value).encode()).hexdigest()

    def refresh(self):
        if not self._refresh_lock.acquire(blocking=False):
            return self.status()
        try:
            if self.provider.status().get("state") == "not_configured":
                return self.status()
            result = self.provider.refresh()
            now = self.clock().isoformat()
            state = {key: copy.deepcopy(result[key]) for key in
                     ("state", "status", "mode", "message", "source", "coverage", "warnings") if key in result}
            state["last_refresh"] = now
            if result.get("state") == "error":
                state["error"] = result.get("message") or "Betfair collection failed; saved quote times retained."
                with self._lock:
                    self._state.update(state)
                return self.status()
            markets = result.get("markets", [])
            rows, assets = self.runtime.research_rows() if self.runtime else ([], {})
            mappings = map_markets(markets, rows, assets, self._native_polymarket())
            state.update(error=None, last_success=now)
            with self.store.connect() as db:
                previous = {row["market_id"]: row["book_signature"] for row in db.execute("SELECT market_id,book_signature FROM betfair_markets")}
                db.execute("DELETE FROM betfair_markets")
                for market in markets:
                    book = self._book(market)
                    signature = self._signature(book)
                    db.execute("INSERT INTO betfair_markets VALUES(?,?,?)", (market["market_id"], packed(market), signature))
                    stamp = P._timestamp(book.get("observed_at"))
                    if book.get("book_available") and stamp and previous.get(market["market_id"]) != signature:
                        db.execute("INSERT INTO betfair_books(market_id,observed_at,mode,payload) VALUES(?,?,?,?)",
                                   (market["market_id"], book["observed_at"], market.get("data_mode", result.get("mode", "unknown")), packed(book)))
                db.execute("DELETE FROM betfair_mappings")
                for mapping in mappings:
                    key = packed([mapping["betfair_market_id"], mapping["selection_id"], str(mapping["handicap"])])
                    payload = packed(mapping)
                    db.execute("INSERT INTO betfair_mappings VALUES(?,?)", (key, payload))
                    db.execute("INSERT OR IGNORE INTO betfair_mapping_versions VALUES(?,?,?,?)",
                               (key, mapping["fingerprint"], now, payload))
                db.execute("INSERT OR REPLACE INTO betfair_state VALUES(1,?)", (packed(state),))
            with self._lock:
                self._markets, self._mappings, self._state = copy.deepcopy(markets), mappings, state
        except Exception:
            with self._lock:
                self._state.update(state="error", error="Betfair collection or mapping failed; saved quote times retained.",
                                   last_refresh=self.clock().isoformat())
        finally:
            self._refresh_lock.release()
        return self.status()

    def snapshot(self):
        with self._lock:
            return {"status": self.status(), "markets": copy.deepcopy(self._markets), "mappings": copy.deepcopy(self._mappings)}

    def history(self, market_id, limit=250):
        if not isinstance(market_id, str) or len(market_id) > 100 or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("Invalid native Betfair history selection")
        with self.store.connect() as db:
            mode_clause = "mode='synthetic'" if self._synthetic else "mode!='synthetic'"
            records = db.execute("SELECT id,observed_at,mode,payload FROM betfair_books WHERE market_id=? AND " + mode_clause + " ORDER BY id DESC LIMIT ?",
                                 (market_id, limit)).fetchall()
        return {"books": [{"id": row["id"], "mode": row["mode"], **json.loads(row["payload"])} for row in reversed(records)],
                "dataset": "synthetic" if self._synthetic else "exchange",
                "note": "Native changed REST books; stake currency is not assumed to be USD. Synthetic observations stay labelled."}

    def decorate(self, rows):
        """Attach only exact asset links; never modify the binary fee/alert route."""
        snapshot = self.snapshot()
        mappings = {(str(m["betfair_market_id"]), str(m["selection_id"]), str(m["handicap"])): m for m in snapshot["mappings"]}
        linked = defaultdict(list)
        books = []
        for market in snapshot["markets"]:
            for runner in market.get("runners", []):
                key = runner_key(market["market_id"], runner)
                book = {**{k: v for k, v in market.items() if k != "runners"}, **runner,
                        "mode": market.get("data_mode", snapshot["status"].get("mode", "unknown")),
                        "mapping": mappings.get(key, {"status": "unmatched", "settlement_status": "unverified"})}
                book["mapping"]["arbitrage_eligible"] = False
                books.append(book)
                if book["mapping"]["status"] == "fixture_linked" and book["mapping"].get("pm_asset_id"):
                    linked[str(book["mapping"]["pm_asset_id"])].append(book)
        used = set()
        for row in rows:
            pm = next((leg for leg in row["legs"] if leg["exchange"] == "polymarket_intl"), None)
            candidates = linked.get(str(pm.get("instrument_id")), []) if pm else []
            if len(candidates) == 1:
                book = candidates[0]
                key = runner_key(book["market_id"], book)
                if key not in used:
                    row["betfair"] = book
                    row["venues"] = [*row["venues"], "betfair"]
                    stamp = P._timestamp(book.get("observed_at"))
                    row["quoted"] = row["quoted"] or bool(stamp and 0 <= (self.clock() - stamp).total_seconds() <= 30
                                                            and (book.get("back") or book.get("lay")))
                    used.add(key)
        now = self.clock()
        for book in books:
            key = runner_key(book["market_id"], book)
            if key in used:
                continue
            if book["mapping"].get("pm_asset_id") and len(linked[str(book["mapping"]["pm_asset_id"])]) > 1:
                book["mapping"].update(status="ambiguous", reason="Multiple Betfair outcomes share this Polymarket asset; no unique attachment")
            stamp = P._timestamp(book.get("observed_at"))
            fresh = bool(stamp and 0 <= (now - stamp).total_seconds() <= 30)
            identity = "betfair:" + ":".join(key)
            leg = {"id": identity, "exchange": "betfair", "instrument_id": ":".join(key),
                   "market_id": book["market_id"], "outcome": book["runner_name"], "status": book.get("market_status", "unknown"),
                   "bid": None, "ask": None, "bid_size": None, "ask_size": None, "observed_at": book.get("observed_at"),
                   "fresh": fresh, "quoted": bool(book.get("back") or book.get("lay")), "top_liquidity": None,
                   "expiry": book.get("start_time", ""), "sport": "basketball"}
            row = {"id": identity, "title": book.get("event_name") or book.get("market_name") or book["market_id"],
                   "outcome": book["runner_name"], "category": "Sports", "sport": "basketball", "sport_label": "Basketball",
                   "venues": ["betfair"], "legs": [leg], "match_status": "single", "comparison_id": None,
                   "comparison_key": None, "comparison_status": "unmatched", "conditional": False,
                   "crossed": False, "net_crossed": False, "alert": False, "gross_cents": None, "fees_cents": None,
                   "net_cents": None, "pnl": None, "top_liquidity": None, "expiry": book.get("start_time", ""),
                   "quoted": fresh and leg["quoted"], "quote_expiry": 0, "competition": (book.get("competition") or {}).get("name"),
                   "alert_reason": "Native Betfair odds; hedge calculations are outside this data milestone.", "betfair": book}
            rows.append(row)
        return rows

    def close(self):
        self.provider.close()
