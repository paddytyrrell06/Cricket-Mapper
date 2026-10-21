"""Bounded public REST quote recording for independently discovered markets.

Discovery remains unrestricted. This worker spends its smaller quote budget on
liquidity candidates plus rotating probes, and never establishes equivalence or
creates opportunities. The normal server scheduler owns its lifetime.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime, timezone

import canonical as C
import presentation as P
import quotes as Q
from liquidity import number
from research_store import utc


class CatalogQuoteMonitor:
    probe_seconds = 60
    measured_seconds = 60

    def __init__(self, catalog, store, runtime=None, collector=None, *,
                 max_instruments=100, probe_budget=20, min_notional=100,
                 clock=None, timer=None):
        if (isinstance(max_instruments, bool) or not isinstance(max_instruments, int)
                or not 1 <= max_instruments <= 100):
            raise ValueError("Catalog quote budget must be 1–100 instruments")
        if (isinstance(probe_budget, bool) or not isinstance(probe_budget, int)
                or not 0 <= probe_budget <= max_instruments):
            raise ValueError("Probe budget must fit the catalog quote budget")
        threshold = number(min_notional)
        if threshold is None:
            raise ValueError("A nonnegative finite liquidity bound is required")
        self.catalog, self.store, self.runtime = catalog, store, runtime
        self.collector = collector or Q.enrich_quotes
        self.max_instruments, self.probe_budget, self.min_notional = max_instruments, probe_budget, threshold
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.timer = timer or time.monotonic
        self._refresh_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._attempts = {}
        self._stats = {"monitored_count": 0, "liquid_count": 0, "unknown_probe_count": 0,
                       "filtered_count": 0, "last_refresh": None, "error": None,
                       "max_instruments": max_instruments, "probe_budget": probe_budget,
                       "min_notional_usd": str(threshold)}

    def status(self):
        with self._state_lock:
            return copy.deepcopy(self._stats)

    def _measurements(self, now):
        """Read the indexed latest observations, preserving their actual age."""
        observations = self.store.latest_quotes().values()
        latest = {}
        for observation in observations:
            stamp = P._timestamp(observation["observed_at"])
            if stamp is None or not 0 <= (now - stamp).total_seconds() < self.measured_seconds:
                continue
            key = observation["exchange"], observation["instrument_id"]
            if key not in latest or stamp >= latest[key][0]:
                latest[key] = stamp, dict(observation)
        return {key: value[1] for key, value in latest.items()}

    def _candidate_rows(self, now):
        with self.store.connect() as db:
            entries = db.execute("SELECT id,exchange,market_id,outcome,instrument_id,row_json "
                                 "FROM research_catalog ORDER BY id").fetchall()
        covered = set()
        if self.runtime is not None:
            _, assets = self.runtime.research_rows()
            covered = {(key[0], str(asset)) for key, asset in assets.items() if asset}
        measured, seen, result, skipped = self._measurements(now), set(), [], 0
        for entry in entries:
            venue, asset = entry["exchange"], str(entry["instrument_id"] or "")
            key = venue, asset
            row = json.loads(entry["row_json"])
            status = str(row.get(venue + "_market_status") or "").lower()
            if (venue not in ("kalshi", "polymarket_intl") or not asset
                    or (venue == "polymarket_intl" and not asset.isdigit())
                    or status not in ("open", "active") or key in covered or key in seen):
                skipped += 1
                continue
            seen.add(key)
            raw = dict(row.get("raw_metadata", {}).get("market") or {})
            market = C.Market(venue, row["canonical_fixture_id"], None,
                row["canonical_market_type"], row["canonical_outcome"],
                market_id=entry["market_id"], event_id=row.get(venue + "_event_id", ""),
                title=row.get("research_title", ""), raw_outcome=entry["outcome"])
            if venue == "polymarket_intl" and Q._intl_asset(market, raw) != asset:
                # Never reinterpret YES/NO indices or position IDs to get a book.
                skipped += 1
                continue
            observation = measured.get(key)
            top = None
            if observation:
                sides = []
                for side in ("bid", "ask"):
                    price, size = number(observation[side], price=True), number(observation[side + "_size"])
                    if price is not None and size is not None:
                        sides.append(price * size)
                top = max(sides) if sides else None
            # A measured thin book overrides older aggregate discovery hints.
            aggregate = next((value for field in ("liquidityClob", "liquidity_clob", "liquidity")
                              if (value := number(raw.get(field))) is not None), None)
            liquid = top >= self.min_notional if top is not None else (
                aggregate is not None and aggregate >= self.min_notional)
            # Volume only prioritises bounded unknown probes, never qualifies a
            # market for the liquid budget. Venue units are kept separate.
            volume = number(row.get(venue + "_volume")) or number(0)
            result.append({"id": entry["id"], "key": key, "row": row, "raw": raw,
                           "market": market, "liquid": liquid, "volume": volume,
                           "notional": top if top is not None else aggregate,
                           "attempted": self._attempts.get(key, float("-inf"))})
        return result, skipped

    @staticmethod
    def _interleave(entries):
        """Rank volume within each venue; their volume units differ."""
        groups = {venue: sorted((entry for entry in entries if entry["key"][0] == venue),
                                key=lambda entry: (entry["attempted"], -entry["volume"], entry["id"]))
                  for venue in ("kalshi", "polymarket_intl")}
        result = []
        for index in range(max((len(group) for group in groups.values()), default=0)):
            for group in groups.values():
                if index < len(group):
                    result.append(group[index])
        return result

    def refresh(self):
        # A button and scheduler may coincide; a second request shares the
        # current status rather than queueing another batch of venue calls.
        if not self._refresh_lock.acquire(blocking=False):
            return self.status()
        started = self.timer()
        try:
            now = self.clock()
            candidates, skipped = self._candidate_rows(now)
            liquid = self._interleave([item for item in candidates if item["liquid"]])
            probes = self._interleave([item for item in candidates if not item["liquid"]
                                      and started - item["attempted"] >= self.probe_seconds])
            selected_probes = probes[:self.probe_budget]
            selected = liquid[:self.max_instruments - len(selected_probes)] + selected_probes
            self._attempts.update({item["key"]: started for item in selected})
            markets, raw, assets = [], {"kalshi": [], "polymarket_intl": []}, {}
            for item in selected:
                market, record = item["market"], dict(item["raw"])
                # The shared collector retains discovery prices on failed
                # native requests. These records carry metadata only, so no
                # old discovery BBO can become a newly recorded observation.
                if market.venue == "kalshi":
                    for field in ("yes_bid_dollars", "yes_ask_dollars", "yes_bid_size_fp", "yes_ask_size_fp"):
                        record.pop(field, None)
                record["_collected_at_utc"] = item["row"].get(market.venue + "_metadata_time") or ""
                markets.append(market)
                raw[market.venue].append(record)
                assets[(market.venue, str(market.market_id), C.norm_team(market.outcome))] = item["key"][1]
            warnings = []
            if markets:
                _, warnings = self.collector(markets, raw, live=True, refresh_fees=False)
            recorded = []
            for item, market in zip(selected, markets):
                stamp = P._timestamp(market.quote_time)
                # Shared collection is synchronous. Only quotes observed in
                # this request can replace the last genuinely recorded book.
                if (stamp is None or stamp < now or "snapshot" in market.quote_source.lower()):
                    continue
                row, venue = dict(item["row"]), market.venue
                # Raw discovery metadata already lives in the catalog. Keep
                # rule/expiry fields in frames without recopying full records.
                row.pop("raw_metadata", None)
                row.update({venue + "_" + field: value for field, value in P._market_dict(market, venue).items()})
                details = market.extra
                for target, origin in (("market_status", "quote_market_status"), ("volume", "quote_volume"),
                                       ("volume_time", "quote_metadata_time"), ("metadata_time", "quote_metadata_time")):
                    if details.get(origin) is not None:
                        row[venue + "_" + target] = details[origin]
                recorded.append(row)
            frame_id = self.store.record(recorded, available_at=utc(self.clock()),
                                         dataset_id="catalog_quotes", name="Catalog REST quotes",
                                         kind="recorded_quotes", assets=assets, select=False,
                                         limit=self.max_instruments) if recorded else None
            stats = {"monitored_count": len(selected), "recorded_count": len(recorded),
                     "liquid_count": len(selected) - len(selected_probes),
                     "liquid_candidate_count": len(liquid), "unknown_probe_count": len(selected_probes),
                     "filtered_count": skipped + len(candidates) - len(selected),
                     "catalog_candidate_count": len(candidates), "last_refresh": utc(self.clock()),
                     "last_frame_id": frame_id, "refresh_ms": round((self.timer() - started) * 1000, 3),
                     "warnings": list(warnings)[:20], "error": None,
                     "note": "Liquidity candidates use measured depth or provider aggregate metadata; volume only ranks probes. This is not executable pair liquidity."}
            with self._state_lock:
                self._stats.update(stats)
        except Exception:
            # Never expose URLs/credentials from a transport exception, and do
            # not stop the existing terminal or its primary quote worker.
            with self._state_lock:
                self._stats.update(last_refresh=utc(self.clock()), error="Catalog quote refresh failed; saved data retained")
        finally:
            self._refresh_lock.release()
        return self.status()
