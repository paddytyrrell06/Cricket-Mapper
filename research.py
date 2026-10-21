"""Local research data API, sharing the terminal's loopback HTTP service."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import canonical as C
import presentation as P
import quotes as Q
from research_store import pair_key, market_name


class ResearchService:
    def __init__(self, root, store, runtime=None):
        self.root, self.store, self.runtime = Path(root), store, runtime
        self._provider = None
        from market_catalog import MarketCatalog
        self.catalog = MarketCatalog(store)
        from terminal_markets import TerminalMarkets
        self.terminal = TerminalMarkets(self.catalog, store, runtime)

    def get(self, path, query):
        if path == "/api/research/universe":
            rows, assets = self.runtime.research_rows() if self.runtime else ([], {})
            return {"markets": [{"id": pair_key(row), "name": market_name(row),
                "sport": row.get("sport"), "comparison_status": row.get("comparison_status"), "fixture_date": row.get("fixture_date"),
                "instruments": {venue: assets.get((venue, row.get(venue + "_market_id"), C.norm_team(row["canonical_outcome"]))) for venue in ("kalshi", "polymarket_intl")}}
                for row in rows] + self.catalog.markets()}
        if path == "/api/research/terminal":
            args = {key: query[key][0] for key in ("search", "category", "sport", "exchange", "status", "view", "sort", "direction", "min_liquidity", "size") if key in query}
            args.update({key: int(query[key][0]) for key in ("limit", "offset") if key in query})
            for key in ("strict", "quoted"):
                if key in query:
                    if query[key][0] not in ("true", "false"):
                        raise ValueError("Invalid boolean filter")
                    args[key] = query[key][0] == "true"
            if "watchlist" in query:
                args["watchlist"] = json.loads(query["watchlist"][0])
            return self.terminal.page(**args)
        if path == "/api/research/catalog":
            args = {key: query[key][0] for key in ("search", "category", "exchange", "status", "sort", "direction") if key in query}
            args.update({key: int(query[key][0]) for key in ("limit", "offset") if key in query})
            return self.catalog.page(**args)
        if path == "/api/research/catalog-market":
            found = self.catalog.lookup(query.get("pair", [""])[0])
            if found is None:
                raise ValueError("Unknown catalog outcome")
            return {"row": found[0], "source": "catalog_metadata", "has_quote_history": False}
        if path == "/api/research/coverage":
            return self.catalog.summary()
        if path == "/api/research/datasets":
            return {"datasets": self.store.datasets(),
                    "recording": "Up to 25 validated liquid outcome pairs per refresh; no fabricated depth or candles-as-quotes."}
        if path == "/api/research/history":
            args = {key: query[key][0] for key in ("market_id", "exchange", "pair", "start", "end") if key in query}
            args.update({key: int(query[key][0]) for key in ("every_seconds", "limit") if key in query})
            return {"observations": self.store.history(query.get("dataset", [""])[0], **args),
                    "sampling": "last observed per bucket; original timestamps retained; no interpolation"}
        if path == "/api/research/candles":
            return {"candles": self.store.candle_history(query.get("instrument", [""])[0]),
                    "data_type": "price_candles", "executable": False}
        if path == "/api/research/snapshot":
            dataset = query.get("dataset", [""])[0]
            row = self.store.snapshot(dataset, query.get("pair", [""])[0], int(query.get("frame", ["0"])[0]))
            return {"row": row, "dataset": self.store.info(dataset)}
        if path in {"/api/research/series", "/api/research/pairs"}:
            dataset = query.get("dataset", [""])[0]
            info = self.store.info(dataset)
            if path.endswith("pairs"):
                return {"pairs": self.store.pairs(dataset), "dataset": info}
            wanted = query.get("pair", [None])[0]
            if wanted is None:
                pairs = self.store.pairs(dataset)
                wanted = pairs[0]["id"] if pairs else ""
            args = {key: query[key][0] for key in ("start", "end", "exchange") if key in query}
            args.update({key: int(query[key][0]) for key in ("every_seconds",) if key in query})
            observations = self.store.history(info["id"], pair=wanted, **args)
            points = {}
            stale, missing_exchange_time = 0, 0
            for row in observations:
                stamp = P._timestamp(row["observed_at"])
                age = (P._timestamp(row["available_at"]) - stamp).total_seconds() if stamp else None
                stale += age is None or age > 30 or age < 0
                missing_exchange_time += not bool(row["exchange_time"])
                point = points.setdefault(row["frame_id"], {"time": row["available_at"], "frame_id": row["frame_id"]})
                for field in ("bid", "ask", "bid_size", "ask_size", "volume", "observed_at", "source", "book_kind"):
                    point[row["exchange"] + "_" + field] = row[field]
            return {"dataset": info, "points": list(points.values()), "observations": observations,
                    "diagnostics": {"stale_or_unknown_quote_observations": stale, "missing_exchange_timestamps": missing_exchange_time},
                    "note": "Actual bid/ask samples; gaps remain gaps. No executable prices inferred between observations."}
        return None

    def export(self, dataset):
        rows = self.store.history(dataset, limit=20000)
        output = io.StringIO()
        columns = ["kind", "frame_id", "pair_key", "exchange", "event_id", "market_id", "outcome", "instrument_id",
                   "available_at", "observed_at", "exchange_time", "provider_time", "ingested_at", "bid", "ask",
                   "bid_size", "ask_size", "volume", "volume_time", "market_status", "metadata_time", "quote_status", "source", "book_kind"]
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in columns})
        return output.getvalue().encode()

    def post(self, path, body):
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        if path == "/api/research/discover":
            return self.catalog.refresh()
        if path == "/api/research/import":
            paths = [p / "data" if (p / "data").is_dir() else p
                     for p in sorted((self.root / ".cricket_matching_history").glob("*")) if p.is_dir()][-20:]
            paths.append(self.root / "output/data")
            imported = 0
            for directory in paths:
                if (directory / "market_map.csv").exists() and self.store.import_snapshot(directory) is not None:
                    imported += 1
            return {"datasets": self.store.datasets(), "source_snapshots_considered": imported}
        if path in {"/api/research/book", "/api/research/candles"}:
            venue = body.get("venue", "polymarket_intl")
            if venue not in {"kalshi", "polymarket_intl"}:
                raise ValueError("Unsupported venue")
            rows, assets = self.runtime.research_rows() if self.runtime else ([], {})
            row = next((row for row in rows if pair_key(row) == body.get("pair")), None)
            if row is None:
                found = self.catalog.lookup(body.get("pair"))
                if found is None:
                    raise ValueError("Select a market in the saved research catalog")
                row, assets = found
            instrument = assets.get((venue, row.get(venue + "_market_id"), C.norm_team(row["canonical_outcome"])))
            if not instrument:
                raise ValueError("Exact outcome instrument unavailable")
            if venue == "polymarket_intl" and self._provider is None:
                from pmxt_data import PmxtData
                self._provider = PmxtData()
            if path.endswith("candles"):
                if venue != "polymarket_intl":
                    raise ValueError("Public candle history currently supports Polymarket only")
                data = self._provider.fetch_candles(venue, instrument, limit=24)
                self.store.save_candles(data)
                return data
            book = Q.fetch_public_kalshi_book(instrument) if venue == "kalshi" else self._provider.fetch_book(venue, instrument)
            market = C.Market(venue, "research", None, row["canonical_market_type"], row["canonical_outcome"])
            Q._book_quote(market, {"bids": book["bids"], "asks": book["asks"], "timestamp": book["exchange_time"]},
                          book["observed_at"], instrument, [])
            market.quote_source = book["source"] + " book " + instrument
            market.extra["quote_provider_time"] = book.get("provider_time")
            market.extra["quote_provider_metadata"] = book.get("source_metadata") or {}
            # The periodic collector retains ten levels. Explicit research reads
            # retain every level actually returned by the selected provider.
            market.extra["quote_order_book"] = {"bids": book["bids"], "asks": book["asks"]}
            values = P._market_dict(market, venue)
            for field in ("bid", "ask", "bid_size", "ask_size", "quote_time", "quote_status", "quote_source", "quote_details"):
                row[venue + "_" + field] = values[field]
            self.store.record([row], available_at=book["observed_at"], dataset_id="selected_books",
                kind="recorded_order_books", name="Selected live books · PMXT Polymarket / native Kalshi", assets=assets, select=False)
            return book
        return None
