"""One read-only market index: validated links, rule reviews and single venues."""
from __future__ import annotations

import json
import re
import threading
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import canonical as C
import fees as F
import presentation as P
from research_store import pair_key

VENUES = ("kalshi", "polymarket_intl")
CLOSED = {"closed", "settled", "resolved", "finalized", "archived", "inactive", "cancelled", "canceled"}


def number(value):
    try:
        n = Decimal(str(value))
        return n if n.is_finite() and not isinstance(value, bool) else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def cents(value):
    n = number(value)
    return str(n * 100) if n is not None else None


def normal_question(value):
    return re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


class TerminalMarkets:
    def __init__(self, catalog, store, runtime=None, monitor=None):
        self.catalog, self.store, self.runtime, self.monitor = catalog, store, runtime, monitor
        self._cache_lock = threading.Lock()
        self._catalog_signature = None
        self._entries, self._coverage = [], {}

    def _catalog(self):
        with self.store.connect() as db:
            signature = tuple(tuple(row) for row in db.execute("SELECT exchange,data_json FROM research_catalog_state ORDER BY exchange"))
            # Also supports an initially empty catalog before the first scan.
            count = db.execute("SELECT COUNT(*) FROM research_catalog").fetchone()[0]
        signature = signature, count
        with self._cache_lock:
            if signature != self._catalog_signature:
                self._entries, self._coverage = self.catalog.terminal_entries(), self.catalog.summary()
                self._catalog_signature = signature
            return self._entries, self._coverage

    @staticmethod
    def _leg(entry, quotes, now):
        leg = dict(entry)
        leg["research_pair"] = entry.get("research_pair") or entry["id"]
        leg["volume_unit"] = "contracts" if entry["exchange"] == "kalshi" else "USD"
        quote = quotes.get((entry["exchange"], entry.get("instrument_id")), {})
        for key in ("bid", "ask", "bid_size", "ask_size", "observed_at", "quote_status", "source"):
            leg[key] = quote.get(key, entry.get(key))
        stamp = P._timestamp(leg.get("observed_at"))
        leg["quote_age_seconds"] = (now - stamp).total_seconds() if stamp else None
        leg["fresh"] = leg["quote_age_seconds"] is not None and 0 <= leg["quote_age_seconds"] <= 30
        bid, ask = number(leg.get("bid")), number(leg.get("ask"))
        leg["invalid_book"] = bid is not None and ask is not None and bid > ask
        leg["quoted"] = (bid is not None or ask is not None) and not leg["invalid_book"]
        values = [number(leg.get(p)) * number(leg.get(q)) for p, q in (("bid", "bid_size"), ("ask", "ask_size"))
                  if number(leg.get(p)) is not None and number(leg.get(q)) is not None]
        leg["top_liquidity"] = str(min(values)) if len(values) == 2 and leg["fresh"] and not leg["invalid_book"] else None
        return leg

    @staticmethod
    def _base(identity, title, outcome, category, legs, status):
        depth = [number(leg.get("top_liquidity")) for leg in legs if number(leg.get("top_liquidity")) is not None]
        expiry = min((str(leg.get("expiry")) for leg in legs if leg.get("expiry")), default="")
        return {"id": identity, "title": title, "outcome": outcome, "category": category,
                "venues": [leg["exchange"] for leg in legs], "legs": legs, "match_status": status,
                "comparison_id": None, "comparison_key": None, "comparison_status": "unmatched",
                "conditional": False, "crossed": False, "net_crossed": False, "alert": False,
                "gross_cents": None, "fees_cents": None, "net_cents": None, "pnl": None,
                "top_liquidity": str(max(depth)) if depth else None, "expiry": expiry,
                "quoted": any(leg["quoted"] and leg["fresh"] for leg in legs), "quote_expiry": 0,
                "alert_reason": "Single exchange" if status == "single" else "Settlement review required"}

    def _rows(self, now, size, strict):
        entries, coverage = self._catalog()
        quotes = self.store.latest_quotes()
        by_asset = {(entry["exchange"], entry["instrument_id"]): entry for entry in entries if entry.get("instrument_id")}
        consumed, result = set(), []
        rows, assets = self.runtime.research_rows() if self.runtime else ([], {})
        policy_min = self.runtime.settings().get("min_liquidity_usd", "100") if self.runtime else "100"
        alert_min = max(Decimal("100"), number(policy_min) or Decimal("100"))
        for index, template in enumerate(rows):
            row, legs = dict(template), []
            for venue in VENUES:
                market_id = row.get(venue + "_market_id")
                if not market_id or row.get(venue + "_status") != "Available":
                    continue
                instrument = assets.get((venue, market_id, C.norm_team(row["canonical_outcome"])))
                native = by_asset.get((venue, instrument), {}) if instrument else {}
                entry = {**native, "id": pair_key(row), "exchange": venue, "market_id": market_id,
                         "instrument_id": instrument, "outcome": row["canonical_outcome"],
                         "research_pair": pair_key(row), "research_dataset": "recorded",
                         "status": row.get(venue + "_market_status") or native.get("status", "unknown"),
                         "volume": row.get(venue + "_volume"), "expiry": native.get("expiry") or row.get("fixture_date"),
                         **{field: row.get(venue + "_" + old) for field, old in
                            (("bid", "bid"), ("ask", "ask"), ("bid_size", "bid_size"), ("ask_size", "ask_size"),
                             ("observed_at", "quote_time"), ("quote_status", "quote_status"), ("source", "quote_source"))}}
                observed = P._timestamp(entry["observed_at"])
                recorded = quotes.get((venue, instrument), {})
                recorded_at = P._timestamp(recorded.get("observed_at"))
                selected = {} if observed and (recorded_at is None or observed > recorded_at) else quotes
                leg = self._leg(entry, selected, now)
                legs.append(leg)
                if instrument:
                    consumed.add((venue, instrument))
                for field, value in (("bid", leg["bid"]), ("ask", leg["ask"]), ("bid_size", leg["bid_size"]),
                                     ("ask_size", leg["ask_size"]), ("quote_time", leg["observed_at"])):
                    row[venue + "_" + field] = value
            if not legs:
                continue
            equivalent = (len(legs) == 2 and all(leg.get("instrument_id") for leg in legs)
                          and row.get("mapping_status") == "accepted" and row.get("comparison_status") == "compatible")
            kind = "matched" if equivalent else "review" if len(legs) > 1 else "single"
            domain = row.get("domain") or ("esports" if row.get("sport") in {"esports", "cs2", "lol", "dota2", "valorant"} else "sports")
            category = "Esports" if domain == "esports" else "Sports"
            item = self._base(pair_key(row), row.get("team_a", "") + " vs " + row.get("team_b", ""),
                              row["canonical_outcome"], category, legs, kind)
            item.update(comparison_id=index, comparison_key=P._json([domain, row.get("sport"),
                        row.get("canonical_fixture_id"), row.get("canonical_market_type"), row["canonical_outcome"]]),
                        comparison_status=row.get("comparison_status"), conditional=row.get("contingency_status") != "aligned",
                        link_reason=row.get("comparison_reason"), competition=row.get("competition"),
                        strict_equivalent=row.get("strict_comparison_status") == "compatible")
            if equivalent:
                item["top_liquidity"] = None
                row.update(P._eligible_quotes(row, list(VENUES), now, max_age_seconds=30, max_skew_seconds=5))
                routes = json.loads(row.get("eligible_routes") or "[]")
                estimates = [{**F.calculate_sized_taker_cross(row, route["buy"], route["sell"],
                                desired_contracts=size, min_top_notional_usd=str(alert_min)),
                              "buy_venue": route["buy"], "sell_venue": route["sell"]} for route in routes]
                if estimates:
                    positive = [e for e in estimates if number(e.get("net_total")) is not None and number(e["net_total"]) > 0]
                    gross_crosses = [e for e in estimates if number(e.get("gross_gap")) is not None and number(e["gross_gap"]) > 0]
                    estimate = max(positive or gross_crosses or estimates, key=lambda e: (number(e.get("net_total")) is not None,
                        number(e.get("net_total")) if number(e.get("net_total")) is not None else Decimal("-1000000"),
                        number(e.get("gross_gap")) or Decimal("-1")))
                    item.update(gross_cents=cents(estimate.get("gross_gap")), fees_cents=cents(estimate.get("fees_per_contract")),
                        net_cents=cents(estimate.get("net_gap")), pnl=estimate.get("net_total"),
                        top_liquidity=estimate.get("paired_top_notional_usd"), quantity=estimate.get("sized_quantity"),
                        estimate=estimate, quote_check=row.get("quote_check"))
                    strict_ok = not strict or row.get("strict_comparison_status") == "compatible"
                    valid_route = (estimate.get("liquidity_status") == "sufficient"
                                   and estimate.get("minimum_order_status") != "below_minimum"
                                   and number(estimate.get("sized_quantity")) is not None
                                   and not any(str(leg.get("status")).lower() in CLOSED for leg in legs))
                    # No opportunity can be created from an unvalidated link,
                    # stale quotes, unknown depth or a crossed local venue book.
                    item["crossed"] = bool(number(estimate.get("gross_gap")) is not None and number(estimate["gross_gap"]) > 0 and strict_ok)
                    item["alert"] = bool(item["crossed"] and valid_route)
                    item["net_crossed"] = bool(item["alert"] and number(estimate.get("net_total")) is not None and number(estimate["net_total"]) > 0)
                    item["alert_reason"] = ("After fees +" if item["net_crossed"] else ("Gross cross; fees unavailable" if number(estimate.get("net_total")) is None else "Gross cross; fees absorb edge") if item["alert"]
                        else estimate.get("reason") or row.get("quote_check"))
                    stamps = [P._timestamp(leg.get("observed_at")) for leg in legs]
                    item["quote_expiry"] = int((min(stamps).timestamp() + 30) * 1000) if all(stamps) else 0
                else:
                    item["alert_reason"] = row.get("quote_check") or "Quotes unavailable"
                if all(leg["fresh"] for leg in legs):
                    item["top_liquidity"] = item["top_liquidity"] or P._observed_depth(row, list(VENUES))["notional"]
            result.append(item)
        singles = []
        for entry in entries:
            if entry.get("instrument_id") and (entry["exchange"], entry["instrument_id"]) in consumed:
                continue
            leg = self._leg(entry, quotes, now)
            leg["research_dataset"] = "catalog_quotes"
            singles.append(self._base(entry["id"], entry["title"], entry["outcome"], entry["category"], [leg], "single"))
        # Exact stated question, native outcome and expiry are useful research
        # links, but cannot establish equivalent settlement rules on their own.
        candidates = defaultdict(list)
        for item in singles:
            leg = item["legs"][0]
            if item["expiry"] and leg.get("instrument_id"):
                candidates[(normal_question(item["title"]), C.norm_team(item["outcome"]), item["expiry"][:10])].append(item)
        joined = set()
        for group in candidates.values():
            if len(group) != 2 or {g["legs"][0]["exchange"] for g in group} != set(VENUES):
                continue
            left, right = group
            item = self._base("related:" + left["id"] + ":" + right["id"], left["title"], left["outcome"],
                              left["category"], left["legs"] + right["legs"], "review")
            item.update(comparison_status="rules_unknown", link_reason="Same stated question, outcome and expiry; settlement not validated")
            result.append(item)
            joined.update(g["id"] for g in group)
        result.extend(item for item in singles if item["id"] not in joined)
        return result, coverage

    def page(self, *, search="", category="", exchange="", status="", view="all", limit=100, offset=0,
             sort="liquidity", direction="desc", min_liquidity=0, size="100", strict=False, quoted=False, watchlist=None):
        if sort not in {"name", "category", "liquidity", "edge", "expiry", "gross", "pnl"} or direction not in {"asc", "desc"}:
            raise ValueError("Unsupported terminal sort")
        if view not in {"all", "matched", "single", "review", "alerts", "watch"} or exchange and exchange not in VENUES or status not in {"", "active", "closed", "unknown"}:
            raise ValueError("Unsupported market view")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100 or not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 1000000:
            raise ValueError("Invalid terminal page")
        if any(not isinstance(x, str) or len(x) > 200 for x in (search, category, exchange, status)) or size not in {"1", "10", "100", "1000", "top"}:
            raise ValueError("Invalid terminal filter")
        threshold = number(min_liquidity)
        if threshold is None or threshold < 0 or not isinstance(strict, bool) or not isinstance(quoted, bool):
            raise ValueError("Invalid liquidity or quote filter")
        if watchlist is not None and (not isinstance(watchlist, list) or len(watchlist) > 10000 or any(not isinstance(k, str) or len(k) > 1000 for k in watchlist)):
            raise ValueError("Invalid watchlist")
        now = datetime.now(timezone.utc)
        rows, coverage = self._rows(now, size, strict)
        counts = Counter(item["match_status"] for item in rows)
        counts.update(all=len(rows), alerts=sum(item["alert"] for item in rows), net_alerts=sum(item["net_crossed"] for item in rows), quoted=sum(item["quoted"] for item in rows))
        categories = Counter(item["category"] for item in rows)
        wanted = set(watchlist or [])
        def keep(item):
            if search.strip() and search.casefold().strip() not in " ".join([item["title"], item["outcome"], item["category"],
                    *[str(leg.get("market_id") or "") for leg in item["legs"]]]).casefold(): return False
            if category and item["category"] != category or exchange and exchange not in item["venues"]: return False
            if view in {"matched", "single", "review"} and item["match_status"] != view: return False
            if view == "alerts" and not item["alert"] or view == "watch" and item["comparison_key"] not in wanted: return False
            if strict and (item["match_status"] != "matched" or not item.get("strict_equivalent")) or quoted and not item["quoted"]: return False
            if threshold > 0 and (number(item["top_liquidity"]) is None or number(item["top_liquidity"]) < threshold): return False
            statuses = [str(leg.get("status") or "unknown").lower() for leg in item["legs"]]
            if status == "active" and not any(s in {"active", "open"} for s in statuses): return False
            if status == "closed" and not all(s in CLOSED for s in statuses): return False
            if status == "unknown" and not any(s not in {"active", "open", *CLOSED} for s in statuses): return False
            return True
        selected = [item for item in rows if keep(item)]
        key = {"name": "title", "category": "category", "liquidity": "top_liquidity", "edge": "net_cents", "gross": "gross_cents", "pnl": "pnl", "expiry": "expiry"}[sort]
        if sort in {"liquidity", "edge", "gross", "pnl"}:
            known = [item for item in selected if number(item[key]) is not None]
            unknown = [item for item in selected if number(item[key]) is None]
            known.sort(key=lambda item: number(item[key]), reverse=direction == "desc")
            selected = known + unknown
        else:
            selected.sort(key=lambda item: str(item[key] or "").casefold(), reverse=direction == "desc")
        monitoring = self.monitor.status() if self.monitor else {"monitored_count": 0, "state": "not_started"}
        return {"markets": selected[offset:offset + limit], "total": len(selected), "counts": dict(counts),
                "categories": [{"category": name, "markets": count} for name, count in sorted(categories.items())],
                "coverage": coverage, "monitoring": monitoring, "as_of": now.isoformat(), "size": size}
