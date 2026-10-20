"""Persistent REST quote worker, separate from discovery and HTML publication.

Load an audited universe once, keep its exact outcome/token identities in memory,
and update the browser's row records. No discovery, HTML writes or snapshot
archives occur in a normal refresh. Venue WebSocket adapters can later feed the
same indexed markets instead of this batched REST transport.
"""
from __future__ import annotations

import csv
import copy
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import canonical as C
import presentation as P
import quotes as Q
from liquidity import LiquidityPolicy

VENUES = ["kalshi", "polymarket_intl"]


class MarketRuntime:
    max_age_seconds = 30
    max_skew_seconds = 5
    thin_probe_seconds = 60

    def __init__(self, root: Path, *, collector=None):
        self.root = Path(root).resolve()
        self.collector = collector or Q.enrich_quotes
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._rows = []
        self._records = []
        self._record_signatures = []
        self._delta = None
        self._market_by_key = {}
        self._raw = {}
        self._fee_context = {}
        self._probe_times = {}
        self._fingerprint = None
        self._revision = 0
        self._as_of = None
        self._watchlist = set()
        self._watch_only = False
        self._policy = LiquidityPolicy()
        self._stats = {"monitored_count": 0, "filtered_count": 0, "warnings": []}
        self._load()

    def _load(self):
        data = self.root / "output" / "data"
        fingerprint = (data / "snapshot_meta.json").stat().st_mtime_ns
        with (data / "market_map.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        manifest = json.loads((data / "stream_manifest.json").read_text())
        assets = {(item["venue"], str(item["market_id"]), C.norm_team(item["outcome"])):
                  item["instrument_id"] for item in manifest.get("instruments", [])}
        raw, context = {venue: [] for venue in VENUES}, {}
        for path in data.glob("raw_*.json"):
            if path.stem.endswith("kalshi") or path.stem.endswith("polymarket_intl"):
                venue = "polymarket_intl" if path.stem.endswith("polymarket_intl") else "kalshi"
                raw[venue].extend(json.loads(path.read_text()))
            elif path.stem.endswith("kalshi_series"):
                context[path.stem] = json.loads(path.read_text())
        raw_index = {(venue, str(item.get("ticker") if venue == "kalshi" else item.get("id"))): item
                     for venue, items in raw.items() for item in items if isinstance(item, dict)}
        markets = {}
        for row in rows:
            for venue in VENUES:
                identity = row.get(venue + "_market_id", "")
                if not identity or row.get(venue + "_status") != "Available":
                    continue
                key = venue, identity, C.norm_team(row["canonical_outcome"])
                if key in markets:
                    continue
                original = raw_index.get((venue, identity), {})
                extra = json.loads(row.get(venue + "_fee_metadata") or "{}")
                extra["settlement"] = {
                    "sport": row.get("sport", "cricket"),
                    "target_kind": "draw" if key[2] in {"draw", "tie"} else "team",
                    "payouts": json.loads(row.get(venue + "_settlement_payouts") or "{}"),
                    "policies": json.loads(row.get(venue + "_settlement_policies") or "{}"),
                    "evidence": json.loads(row.get(venue + "_settlement_evidence") or "[]"),
                    "label": row.get(venue + "_settlement_label", ""),
                }
                for side in ("bid", "ask"):
                    extra["quote_" + side + "_size"] = row.get(venue + "_" + side + "_size") or None
                for field in ("minimum_order_size", "minimum_order_notional_usd"):
                    extra[field] = row.get(venue + "_" + field) or None
                raw_outcome = row["canonical_outcome"]
                if venue == "polymarket_intl":
                    outcomes = Q._json_list(original.get("outcomes"))
                    token_ids = Q._json_list(original.get("positionIds") if original.get("version") == "v2"
                                             else original.get("clobTokenIds"))
                    asset = assets.get(key)
                    indices = [i for i, value in enumerate(token_ids) if str(value) == asset]
                    if len(indices) == 1 and len(outcomes) == len(token_ids):
                        raw_outcome = str(outcomes[indices[0]])
                        extra["binary_target_market"] = C.norm_team(raw_outcome) == "yes"
                market = C.Market(venue, row["canonical_fixture_id"], None, row["canonical_market_type"],
                                  row["canonical_outcome"], market_id=identity,
                                  event_id=row.get(venue + "_event_id", ""),
                                  slug=row.get(venue + "_slug", ""), title=row.get(venue + "_title", ""),
                                  url=row.get(venue + "_url", ""), raw_outcome=raw_outcome, extra=extra,
                                  bid=row.get(venue + "_bid") or None, ask=row.get(venue + "_ask") or None,
                                  quote_time=row.get(venue + "_quote_time", ""),
                                  quote_source=row.get(venue + "_quote_source", ""),
                                  quote_status=row.get(venue + "_quote_status", "unavailable"))
                markets[key] = market
        if (data / "snapshot_meta.json").stat().st_mtime_ns != fingerprint:
            raise ValueError("Discovery changed during loading; retry the new publication")
        with self._lock:
            self._publish(datetime.now(timezone.utc), markets=markets, template_rows=rows)
            self._raw, self._raw_index, self._fee_context = raw, raw_index, context
            self._fingerprint = fingerprint
            self._probe_times.clear()
            self._watchlist.intersection_update(record["key"] for record in self._records)
            # Row IDs may change when discovery adds/removes markets.
            self._delta = None

    @staticmethod
    def _record_signature(record):
        # Quote timestamps change every observation. Send small heartbeats when
        # prices, depth, fee estimates and eligibility are otherwise unchanged.
        stable = {key: value for key, value in record.items() if key != "expiry"}
        stable["html"] = re.sub(r'data-cross-expiry="\d+"', 'data-cross-expiry=""', stable["html"])
        return json.dumps(stable, sort_keys=True, separators=(",", ":"))

    def _publish(self, as_of, *, markets=None, template_rows=None):
        markets = self._market_by_key if markets is None else markets
        updated = []
        for template in self._rows if template_rows is None else template_rows:
            row = dict(template)
            for venue in VENUES:
                key = venue, row.get(venue + "_market_id", ""), C.norm_team(row["canonical_outcome"])
                market = markets.get(key)
                if market:
                    row.update({venue + "_" + name: value for name, value in P._market_dict(market, venue).items()})
            row.update(P._quote_summary(row, VENUES))
            row.update(P._eligible_quotes(row, VENUES, as_of, max_age_seconds=self.max_age_seconds,
                                          max_skew_seconds=self.max_skew_seconds))
            updated.append(row)
        records = P.build_terminal_records(updated, VENUES, include_details=False,
                                            max_age_seconds=self.max_age_seconds)
        signatures = [self._record_signature(record) for record in records]
        changed, heartbeats = [], []
        for index, record in enumerate(records):
            if index >= len(self._record_signatures) or signatures[index] != self._record_signatures[index]:
                changed.append(record)
            else:
                heartbeats.append({key: record[key] for key in
                                   ("id", "expiry", "crossed", "netCrossed", "edge")})
        # Publish one complete frame only after quote parsing and calculations
        # succeed; an exception cannot leak a partially updated book to readers.
        self._rows, self._records, self._market_by_key = updated, records, markets
        self._record_signatures = signatures
        self._delta = {"records": changed, "heartbeats": heartbeats}
        self._revision += 1
        stamps = [P._timestamp(market.quote_time) for market in markets.values()]
        known = [stamp for stamp in stamps if stamp is not None]
        self._as_of = max(known).isoformat() if known else None

    def refresh(self):
        with self._refresh_lock:
            metadata = self.root / "output" / "data" / "snapshot_meta.json"
            if metadata.stat().st_mtime_ns != self._fingerprint:
                self._load()
            with self._lock:
                policy, watchlist, watch_only = self._policy, set(self._watchlist), self._watch_only
                instrument_rows = {}
                for row, record in zip(self._rows, self._records):
                    for venue in VENUES:
                        key = venue, row.get(venue + "_market_id", ""), C.norm_team(row["canonical_outcome"])
                        instrument_rows.setdefault(key, set()).add(record["key"])
            selected, selected_keys, filtered = [], [], 0
            now = datetime.now(timezone.utc)
            tick = time.monotonic()
            for key, market in self._market_by_key.items():
                if watch_only and not (instrument_rows.get(key, set()) & watchlist):
                    filtered += 1
                    continue
                # Keep aggregate metadata, but never fill current missing BBO
                # depth from an older discovery response.
                raw = {name: value for name, value in self._raw_index.get(
                    (market.venue, str(market.market_id)), {}).items() if name in
                    {"liquidityClob", "liquidity_clob", "liquidity_observed_at", "_collected_at_utc"}}
                raw.update({name: value for name, value in market.extra.items() if name in
                            {"liquidityClob", "liquidity_observed_at", "_collected_at_utc"}})
                decision = policy.discovery_decision(
                    market.venue, raw, bid=market.bid, ask=market.ask,
                    bid_size=market.extra.get("quote_bid_size"), ask_size=market.extra.get("quote_ask_size"),
                    bbo_observed_at=market.quote_time,
                    now=now, max_age_seconds=self.thin_probe_seconds)
                if decision.skip_quotes and tick - self._probe_times.get(key, float("-inf")) < self.thin_probe_seconds:
                    filtered += 1
                    continue
                selected.append(copy.deepcopy(market))
                selected_keys.append(key)
            started = time.perf_counter()
            _, warnings = self.collector(selected, self._raw, live=True, fee_context=self._fee_context,
                                          refresh_fees=True)
            with self._lock:
                candidate = {**self._market_by_key, **dict(zip(selected_keys, selected))}
                self._publish(datetime.now(timezone.utc), markets=candidate)
                self._probe_times.update({key: tick for key in selected_keys})
                self._stats = {"monitored_count": len(selected), "filtered_count": filtered,
                               "warnings": warnings[:20],
                               "checked_at": datetime.now(timezone.utc).isoformat(),
                               "refresh_ms": round((time.perf_counter() - started) * 1000, 3)}

    def settings(self):
        with self._lock:
            return {"min_liquidity_usd": str(self._policy.min_top_notional_usd),
                    "watch_only": self._watch_only, "watchlist": sorted(self._watchlist)}

    @property
    def revision(self):
        with self._lock:
            return self._revision

    def configure(self, values):
        if not isinstance(values, dict) or set(values) - {"min_liquidity_usd", "watch_only", "watchlist"}:
            raise ValueError("Unknown scanner setting")
        with self._lock:
            policy = LiquidityPolicy(values.get("min_liquidity_usd", self._policy.min_top_notional_usd))
            watch_only = values.get("watch_only", self._watch_only)
            watchlist = values.get("watchlist", list(self._watchlist))
            if (not isinstance(watch_only, bool) or not isinstance(watchlist, list) or len(watchlist) > 10000 or
                    any(not isinstance(key, str) or len(key) > 1000 for key in watchlist)):
                raise ValueError("Invalid watchlist")
            allowed = {record["key"] for record in self._records}
            self._policy, self._watch_only, self._watchlist = policy, watch_only, set(watchlist) & allowed
            return self.settings()

    def snapshot(self, revision=None):
        with self._lock:
            try:
                previous = int(revision)
            except (ValueError, TypeError):
                previous = None
            partial = previous == self._revision - 1 and self._delta is not None
            records = None if previous == self._revision else (
                self._delta["records"] if partial else self._records)
            return {"mode": "runtime_rest", "revision": self._revision, "as_of": self._as_of,
                    "records": records, "partial": partial,
                    "heartbeats": self._delta["heartbeats"] if partial else [],
                    "settings": self.settings(), "policy": self._policy.to_dict(), **self._stats}

    def detail(self, index):
        with self._lock:
            row = dict(self._rows[index])
            revision = self._revision
        return {"revision": revision, "details": P.terminal_record_details(
            row, VENUES, max_age_seconds=self.max_age_seconds, max_skew_seconds=self.max_skew_seconds)}
