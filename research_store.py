"""Local, append-only research observations. No network or trading operations."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import presentation as P
import canonical as C

VENUES = ["kalshi", "polymarket_intl"]


def utc(value=None):
    stamp = P._timestamp(value) if value is not None else datetime.now(timezone.utc)
    if stamp is None:
        raise ValueError("A timezone-aware timestamp is required")
    return stamp.isoformat(timespec="microseconds")


def pair_key(row):
    return json.dumps([row.get("sport"), row["canonical_fixture_id"],
                       row.get("canonical_market_type"), row["canonical_outcome"]], separators=(",", ":"))


def market_name(row):
    title = row.get("research_title") or row.get("team_a", "") + " vs " + row.get("team_b", "")
    return title + " / " + row["canonical_outcome"]


class ResearchStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS frames(id INTEGER PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id),
                  available_at TEXT NOT NULL, ingested_at TEXT NOT NULL, digest TEXT NOT NULL, rows_blob BLOB NOT NULL,
                  UNIQUE(dataset_id,digest));
                CREATE INDEX IF NOT EXISTS frame_time ON frames(dataset_id,available_at,id);
                CREATE TABLE IF NOT EXISTS observations(frame_id INTEGER NOT NULL REFERENCES frames(id),
                  pair_key TEXT NOT NULL, exchange TEXT NOT NULL, event_id TEXT, market_id TEXT, outcome TEXT,
                  instrument_id TEXT, observed_at TEXT, exchange_time TEXT, bid TEXT, ask TEXT, bid_size TEXT,
                  ask_size TEXT, volume TEXT, market_status TEXT, quote_status TEXT, source TEXT, book_kind TEXT NOT NULL,
                  provider_time TEXT, volume_time TEXT, metadata_time TEXT,
                  PRIMARY KEY(frame_id,pair_key,exchange));
                CREATE INDEX IF NOT EXISTS observation_market ON observations(exchange,market_id,outcome,frame_id);
                CREATE INDEX IF NOT EXISTS observation_pair ON observations(pair_key,frame_id);
                CREATE TABLE IF NOT EXISTS candles(exchange TEXT NOT NULL, instrument_id TEXT NOT NULL,
                  resolution TEXT NOT NULL, time TEXT NOT NULL, observed_at TEXT NOT NULL, source TEXT NOT NULL,
                  open TEXT, high TEXT, low TEXT, close TEXT, volume TEXT,
                  PRIMARY KEY(exchange,instrument_id,resolution,time));
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(observations)")}
            for column in ("provider_time", "volume_time", "metadata_time"):
                if column not in columns:
                    db.execute("ALTER TABLE observations ADD COLUMN " + column + " TEXT")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def record(self, rows, *, available_at=None, dataset_id="recorded", kind="recorded_quotes",
               name=None, assets=None, limit=25, select=True):
        selected = []
        for row in rows:
            if select:
                observed = P._observed_depth(row, VENUES)["notional"]
                if (row.get("mapping_status") != "accepted" or row.get("comparison_status") != "compatible"
                        or observed is None or P.Decimal(observed) < 100):
                    continue
            selected.append(dict(row))
        if select:
            selected.sort(key=lambda row: (-(P.Decimal(row.get("net_gap") or "-1000")), pair_key(row)))
        selected = selected[:limit]
        if not selected:
            return None
        payload = json.dumps(selected, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        available, ingested = utc(available_at), utc()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO datasets VALUES(?,?,?)", (dataset_id, name or dataset_id, kind))
            if db.execute("SELECT kind FROM datasets WHERE id=?", (dataset_id,)).fetchone()[0] != kind:
                raise ValueError("Cannot mix different dataset provenance kinds")
            existing = db.execute("SELECT id FROM frames WHERE dataset_id=? AND digest=?", (dataset_id, digest)).fetchone()
            if existing:
                for row in selected:
                    for venue in VENUES:
                        asset = (assets or {}).get((venue, row.get(venue + "_market_id"), C.norm_team(row["canonical_outcome"])))
                        if asset:
                            db.execute("UPDATE observations SET instrument_id=? WHERE frame_id=? AND pair_key=? AND exchange=? AND instrument_id IS NULL",
                                       (asset, existing[0], pair_key(row), venue))
                return existing[0]
            frame_id = db.execute("INSERT INTO frames(dataset_id,available_at,ingested_at,digest,rows_blob) VALUES(?,?,?,?,?)",
                (dataset_id, available, ingested, digest, zlib.compress(payload.encode()))).lastrowid
            for row in selected:
                for venue in VENUES:
                    # Independently discovered markets have no counterpart.
                    if not row.get(venue + "_market_id"):
                        continue
                    details = json.loads(row.get(venue + "_quote_details") or "{}")
                    book = details.get("quote_order_book")
                    db.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        frame_id, pair_key(row), venue, row.get(venue + "_event_id"), row.get(venue + "_market_id"),
                        row["canonical_outcome"], (assets or {}).get((venue, row.get(venue + "_market_id"), C.norm_team(row["canonical_outcome"]))),
                        row.get(venue + "_quote_time"), details.get("quote_exchange_time"),
                        row.get(venue + "_bid"), row.get(venue + "_ask"), row.get(venue + "_bid_size"),
                        row.get(venue + "_ask_size"), row.get(venue + "_volume"), row.get(venue + "_market_status"), row.get(venue + "_quote_status"),
                        row.get(venue + "_quote_source"), "recorded_levels" if book else "recorded_bbo",
                        details.get("quote_provider_time"), row.get(venue + "_volume_time"), row.get(venue + "_metadata_time")))
        return frame_id

    def datasets(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT d.*,COUNT(f.id) frames,MIN(f.available_at) first_at,
                MAX(f.available_at) last_at FROM datasets d LEFT JOIN frames f ON f.dataset_id=d.id GROUP BY d.id ORDER BY d.id""")]

    def load(self, dataset_id, *, end_frame_id=None):
        with self.connect() as db:
            dataset = db.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
            if dataset is None:
                raise ValueError("Unknown dataset")
            entries = db.execute("SELECT id,available_at,ingested_at,digest,rows_blob FROM frames WHERE dataset_id=? AND id<=? ORDER BY available_at,id",
                                 (dataset_id, end_frame_id or 2**63-1)).fetchall()
        if len(entries) > 10000:
            raise ValueError("Dataset exceeds this lab's 10,000-frame limit; select an earlier end frame")
        frames, row_count = [], 0
        digest = hashlib.sha256()
        for entry in entries:
            rows = json.loads(zlib.decompress(entry["rows_blob"]))
            row_count += len(rows)
            if row_count > 100000:
                raise ValueError("Dataset exceeds this lab's 100,000-row limit")
            frames.append({"id": entry["id"], "available_at": entry["available_at"], "ingested_at": entry["ingested_at"], "rows": rows})
            digest.update((str(entry["id"]) + entry["available_at"] + entry["digest"]).encode())
        return frames, {**dict(dataset), "hash": digest.hexdigest(), "end_frame_id": max((f["id"] for f in frames), default=0)}

    def info(self, dataset_id):
        with self.connect() as db:
            dataset = db.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
            if dataset is None:
                raise ValueError("Unknown dataset")
            entries = db.execute("SELECT id,available_at,digest FROM frames WHERE dataset_id=? ORDER BY available_at,id", (dataset_id,)).fetchall()
        digest = hashlib.sha256()
        for row in entries:
            digest.update((str(row["id"]) + row["available_at"] + row["digest"]).encode())
        return {**dict(dataset), "hash": digest.hexdigest(), "frames": len(entries),
                "end_frame_id": max((row["id"] for row in entries), default=0)}

    def snapshot(self, dataset_id, pair, end_frame_id=None):
        with self.connect() as db:
            frame = db.execute("SELECT f.rows_blob FROM frames f JOIN observations o ON o.frame_id=f.id "
                "WHERE f.dataset_id=? AND o.pair_key=? AND f.id<=? ORDER BY f.available_at DESC,f.id DESC LIMIT 1",
                (dataset_id, pair, end_frame_id or 2**63-1)).fetchone()
        if frame is None:
            raise ValueError("No recorded snapshot for this market")
        return next(row for row in json.loads(zlib.decompress(frame[0])) if pair_key(row) == pair)

    def pairs(self, dataset_id):
        with self.connect() as db:
            groups = db.execute("SELECT o.pair_key,COUNT(DISTINCT f.id) frames FROM observations o "
                "JOIN frames f ON f.id=o.frame_id WHERE f.dataset_id=? GROUP BY o.pair_key ORDER BY frames DESC LIMIT 2000",
                (dataset_id,)).fetchall()
        result = []
        for group in groups:
            row = self.snapshot(dataset_id, group["pair_key"])
            result.append({"id": group["pair_key"], "frames": group["frames"],
                           "name": market_name(row)})
        return result

    def history(self, dataset_id, *, market_id=None, exchange=None, pair=None, start=None, end=None,
                every_seconds=0, limit=10000):
        """Actual observations, optionally sampled last-per-bucket; no interpolation."""
        if exchange is not None and exchange not in VENUES:
            raise ValueError("Unsupported exchange")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20000:
            raise ValueError("Observation limit must be between 1 and 20,000")
        if not isinstance(every_seconds, int) or isinstance(every_seconds, bool) or not 0 <= every_seconds <= 86400:
            raise ValueError("Sampling interval must be 0 to 86,400 seconds")
        clauses, params = ["f.dataset_id=?"], [dataset_id]
        for column, value in (("o.market_id", market_id), ("o.exchange", exchange), ("o.pair_key", pair),
                              ("f.available_at>=", utc(start) if start else None),
                              ("f.available_at<=", utc(end) if end else None)):
            if value is not None:
                clauses.append(column + ("?" if column.endswith((">=", "<=")) else "=?"))
                params.append(value)
        if start and end and utc(start) > utc(end):
            raise ValueError("Start must not be after end")
        with self.connect() as db:
            records = [dict(row) for row in db.execute("SELECT o.*,f.available_at,f.ingested_at,d.kind FROM observations o "
                "JOIN frames f ON f.id=o.frame_id JOIN datasets d ON d.id=f.dataset_id WHERE " + " AND ".join(clauses)
                + " ORDER BY f.available_at,f.id,o.exchange LIMIT 20001", params)]
        if len(records) > 20000:
            raise ValueError("Narrow the query time range; more than 20,000 observations")
        if every_seconds:
            buckets = {}
            for row in records:
                bucket = int(P._timestamp(row["available_at"]).timestamp()) // every_seconds
                buckets[(row["exchange"], row["market_id"], row["outcome"], row["pair_key"], bucket)] = row
            records = sorted(buckets.values(), key=lambda row: (row["available_at"], row["frame_id"], row["exchange"]))
        return records[-limit:]

    def save_candles(self, data):
        with self.connect() as db:
            for candle in data["candles"]:
                db.execute("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                    data["venue"], data["instrument_id"], data["resolution"], candle["time"], data["observed_at"],
                    data["source"], candle["open"], candle["high"], candle["low"], candle["close"], candle["volume"]))

    def candle_history(self, instrument_id, resolution="1h"):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM candles WHERE instrument_id=? AND resolution=? ORDER BY time LIMIT 1000",
                                                  (instrument_id, resolution))]

    def import_snapshot(self, directory, *, dataset_id="saved_snapshots"):
        directory = Path(directory)
        meta = json.loads((directory / "snapshot_meta.json").read_text())
        # Offline replay's generation time is not a new market observation.
        if meta.get("quote_mode") == "saved_replay":
            return None
        checkpoint = P._timestamp(meta.get("quotes_checked_at_utc"))
        if checkpoint is None:
            return None
        checkpoint = checkpoint.replace(microsecond=999999)
        with (directory / "market_map.csv").open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        manifest_path = directory / "stream_manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        assets = {(item["venue"], str(item["market_id"]), C.norm_team(item["outcome"])): item["instrument_id"]
                  for item in manifest.get("instruments", [])}
        return self.record(rows, available_at=checkpoint.isoformat(), dataset_id=dataset_id,
                           kind="saved_quote_snapshots", assets=assets, name="Saved real quote snapshots · sparse, not tick history")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/eventlens.sqlite3"))
    parser.add_argument("--import-history", action="store_true")
    args = parser.parse_args()
    store = ResearchStore(args.db)
    if args.import_history:
        sources = [path / "data" if (path / "data").is_dir() else path
                   for path in sorted(Path(".cricket_matching_history").glob("*")) if path.is_dir()][-20:]
        sources.append(Path("output/data"))
        for source in sources:
            if (source / "market_map.csv").exists():
                store.import_snapshot(source)
    print(json.dumps(store.datasets(), indent=2))


if __name__ == "__main__":
    main()
