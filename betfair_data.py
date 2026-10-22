"""Bounded, read-only Betfair Exchange snapshots in native odds/stake units.

Only the four public market-data methods below can reach the betting API. Auth
requests are kept separate, errors never include response bodies or credentials,
and importing/constructing this adapter never performs network I/O.
"""
from __future__ import annotations

import copy
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


READ_METHODS = frozenset({"listEventTypes", "listCompetitions", "listMarketCatalogue", "listMarketBook"})
PROJECTIONS = ["EVENT", "EVENT_TYPE", "COMPETITION", "MARKET_START_TIME",
               "RUNNER_DESCRIPTION", "MARKET_DESCRIPTION"]


def _utc(value=None):
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _date(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return str(number.normalize()) if number.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def _name(value):
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    app_key: str = field(default="", repr=False)
    session_token: str = field(default="", repr=False)
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    cert_file: str = field(default="", repr=False)
    key_file: str = field(default="", repr=False)
    region: str = "nz"
    currency: str = ""
    leagues: tuple[str, ...] = ("NBA",)
    lookahead_hours: int = 72
    catalogue_interval: int = 900
    data_mode: str = "unknown"
    replay_file: str = ""

    @classmethod
    def from_env(cls, environ=None, root=None):
        env = os.environ if environ is None else environ
        def integer(key, default, lower, upper):
            try:
                return min(upper, max(lower, int(env.get(key, default))))
            except (TypeError, ValueError):
                return default
        def path(key):
            value = env.get(key, "").strip()
            return str(Path(root, value) if root and value and not Path(value).is_absolute() else value)
        return cls(
            enabled=str(env.get("BETFAIR_ENABLED", "1" if env.get("BETFAIR_REPLAY_FILE") else "0")).lower() in {"1", "true", "yes"},
            app_key=env.get("BETFAIR_APP_KEY", "").strip(),
            session_token=env.get("BETFAIR_SESSION_TOKEN", "").strip(),
            username=env.get("BETFAIR_USERNAME", ""), password=env.get("BETFAIR_PASSWORD", ""),
            cert_file=path("BETFAIR_CERT_FILE"), key_file=path("BETFAIR_KEY_FILE"),
            region=env.get("BETFAIR_REGION", "nz").strip().lower(),
            currency=env.get("BETFAIR_CURRENCY", "").strip().upper(),
            leagues=tuple(x.strip() for x in env.get("BETFAIR_LEAGUES", "NBA").split(",") if x.strip()),
            lookahead_hours=integer("BETFAIR_LOOKAHEAD_HOURS", 72, 1, 168),
            catalogue_interval=integer("BETFAIR_CATALOGUE_INTERVAL", 900, 60, 86400),
            data_mode=env.get("BETFAIR_DATA_MODE", "unknown").strip().lower(),
            replay_file=path("BETFAIR_REPLAY_FILE"))


class BetfairError(RuntimeError):
    """Sanitized error safe for the terminal and persisted diagnostics."""


class BetfairData:
    def __init__(self, config=None, transport=None, *, clock=None, monotonic_ns=None):
        self.config = config or Config.from_env()
        self.transport = transport or self._transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic_ns = monotonic_ns or time.monotonic_ns
        self._token = self.config.session_token
        self._owns_session = False
        self._keepalive_at = None
        self._catalogue = []
        self._inplay_ids = set()
        self._discovered_at = None
        self._coverage = {}
        self._warnings = []
        self._last = None

    def status(self):
        c = self.config
        mode = "synthetic" if c.replay_file else c.data_mode if c.data_mode in {"live", "delayed"} else "unknown"
        def status(value, message):
            return {"status": value, "state": {"disabled": "not_configured", "synthetic": "replay"}.get(value, value),
                    "mode": mode, "message": message}
        if not c.enabled:
            return status("disabled", "Enable Betfair and configure credentials; see docs/BETFAIR.md.")
        if c.replay_file:
            return status("synthetic", "Explicit synthetic replay; these are not live market prices.")
        if c.region not in {"au", "nz", "global"}:
            return status("disabled", "BETFAIR_REGION must be au, nz or global.")
        if not c.leagues:
            return status("disabled", "BETFAIR_LEAGUES requires an explicit basketball competition allowlist.")
        if c.currency and not re.fullmatch(r"[A-Z]{3}", c.currency):
            return status("disabled", "BETFAIR_CURRENCY must be a three-letter account currency, or empty if unknown.")
        certificate = bool(c.username and c.password and c.cert_file and c.key_file)
        if not c.app_key or not (self._token or certificate):
            return status("disabled", "Betfair needs APP_KEY and SESSION_TOKEN, or username/password plus certificate/key; see docs/BETFAIR.md.")
        return status("ready", "Read-only REST market data; real-time/delayed mode is reported by each market book.")

    def _transport(self, url, payload, headers, *, certificate=False):
        context = ssl.create_default_context()
        if certificate:
            context.load_cert_chain(self.config.cert_file, self.config.key_file)
        data = (urllib.parse.urlencode(payload).encode() if certificate else json.dumps(payload).encode())
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        # Do not allow redirects to forward auth headers to another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
        with opener.open(request, timeout=8) as response:
            return json.loads(response.read(8_000_001).decode())

    def _request(self, url, payload, *, certificate=False):
        headers = {"X-Application": self.config.app_key, "Accept": "application/json",
                   "Content-Type": "application/x-www-form-urlencoded" if certificate else "application/json"}
        if self._token:
            headers["X-Authentication"] = self._token
        try:
            return self.transport(url, payload, headers, certificate=certificate)
        except Exception:
            raise BetfairError("Betfair request failed; check credentials, certificate, connectivity and API access.") from None

    def _identity(self, operation):
        tld = ".com" if self.config.region == "global" else ".com.au"
        cert = operation == "certlogin"
        host = "identitysso-cert" if cert else "identitysso"
        return f"https://{host}.betfair{tld}/api/{operation}"

    def _authenticate(self):
        if not self._token:
            result = self._request(self._identity("certlogin"),
                                   {"username": self.config.username, "password": self.config.password}, certificate=True)
            if not isinstance(result, dict) or result.get("loginStatus") != "SUCCESS" or not result.get("sessionToken"):
                raise BetfairError("Betfair certificate login failed; check account setup and credentials.")
            self._token, self._owns_session = result["sessionToken"], True
            self._keepalive_at = self.clock()
        elif self._keepalive_at is None:
            self._keepalive_at = self.clock()
        elif (self.clock() - self._keepalive_at).total_seconds() >= 900:
            result = self._request(self._identity("keepAlive"), {})
            if not isinstance(result, dict) or result.get("status") != "SUCCESS":
                if self._owns_session:
                    self._token = ""
                raise BetfairError("Betfair session expired; refresh the session token or certificate login.")
            self._keepalive_at = self.clock()

    def _call(self, method, params):
        if method not in READ_METHODS:
            raise BetfairError("Unsupported operation: this adapter permits read-only market data only.")
        if self.status()["status"] != "ready":
            raise BetfairError("Betfair is not configured for live market data.")
        self._authenticate()
        tld = ".com.au" if self.config.region == "nz" else ".com"
        result = self._request(f"https://api.betfair{tld}/exchange/betting/rest/v1.0/{method}/", params)
        if not isinstance(result, list):
            # Betfair REST faults carry APINGException inside detail. Inspect
            # only the error code; never expose the potentially sensitive body.
            detail = result.get("detail", {}) if isinstance(result, dict) else {}
            fault = detail.get("APINGException", {}) if isinstance(detail, dict) else {}
            if isinstance(fault, dict) and fault.get("errorCode") in {"INVALID_SESSION_INFORMATION", "NO_SESSION"}:
                if self._owns_session:
                    self._token, self._owns_session = "", False
                raise BetfairError("Betfair session expired; refresh the session token or certificate login.")
            raise BetfairError(f"Betfair {method} returned an API error; no quotes were refreshed.")
        return result

    def discover(self, now=None):
        now = now or self.clock()
        end = now + timedelta(hours=self.config.lookahead_hours)
        if self._discovered_at and 0 <= (now - self._discovered_at).total_seconds() < self.config.catalogue_interval:
            # Catalogue metadata can be cached; a pre-match polling universe
            # cannot. Prune starts on every cycle without another API request.
            upcoming = [m for m in self._catalogue if m["market_id"] not in self._inplay_ids and
                        (start := _date(m.get("start_time"))) is not None and now <= start <= end]
            self._coverage["markets_in_scope"] = len(upcoming)
            return copy.deepcopy(upcoming)
        base = {"marketStartTime": {"from": _utc(now), "to": _utc(end)}, "inPlayOnly": False}
        sports = self._call("listEventTypes", {"filter": base, "locale": "en"})
        basketball = [x["eventType"] for x in sports if isinstance(x, dict) and
                      isinstance(x.get("eventType"), dict) and _name(x["eventType"].get("name")) == "basketball"]
        self._warnings = []
        self._coverage = {"scope": "Pre-match basketball full-game winner", "leagues": list(self.config.leagues),
                          "from": _utc(now), "to": _utc(end), "catalogue_requests": 0,
                          "catalogue_cap": 200, "capped_windows": 0, "partial": False,
                          "catalogue_rows_returned": 0, "excluded_rows": 0}
        markets = {}
        if len(basketball) != 1:
            self._warnings.append("No unique Basketball event type was returned for the selected time window.")
        else:
            sport = basketball[0]
            base["eventTypeIds"] = [str(sport["id"])]
            competitions = self._call("listCompetitions", {"filter": base, "locale": "en"})
            selected = [x["competition"] for x in competitions if isinstance(x, dict) and
                        isinstance(x.get("competition"), dict) and
                        _name(x["competition"].get("name")) in {_name(y) for y in self.config.leagues}]
            self._coverage["competitions_returned"] = len(competitions)
            self._coverage["competitions_selected"] = len(selected)
            found = {_name(x.get("name")) for x in selected}
            missing = [x for x in self.config.leagues if _name(x) not in found]
            if missing:
                self._warnings.append("No listed competition for: " + ", ".join(missing))
            if selected:
                base.update(competitionIds=[str(x["id"]) for x in selected], marketTypeCodes=["MATCH_ODDS"])
                cursor = now
                while cursor < end:
                    stop = min(end, cursor + timedelta(hours=24))
                    query = dict(base, marketStartTime={"from": _utc(cursor), "to": _utc(stop)})
                    response = self._call("listMarketCatalogue", {"filter": query, "marketProjection": PROJECTIONS,
                                                               "sort": "FIRST_TO_START", "maxResults": "200", "locale": "en"})
                    self._coverage["catalogue_requests"] += 1
                    self._coverage["catalogue_rows_returned"] += len(response)
                    if len(response) >= 200:
                        self._coverage["capped_windows"] += 1
                        self._coverage["partial"] = True
                    for raw in response:
                        if not isinstance(raw, dict):
                            self._coverage["excluded_rows"] += 1
                            continue
                        market = self._normalise(raw)
                        start = _date(market["start_time"])
                        if (market["market_id"] and market["market_type"] == "MATCH_ODDS" and start and
                            now <= start <= end and market["competition"].get("id") in base["competitionIds"]):
                            markets[market["market_id"]] = market
                        else:
                            self._coverage["excluded_rows"] += 1
                    cursor = stop
        if self._coverage["partial"]:
            self._warnings.append("Catalogue reached the 200-market request cap; coverage is partial in the reported windows.")
        self._catalogue = list(markets.values())
        self._discovered_at = now
        self._coverage["markets_discovered"] = len(markets)
        upcoming = [m for m in self._catalogue if m["market_id"] not in self._inplay_ids]
        self._coverage["markets_in_scope"] = len(upcoming)
        return copy.deepcopy(upcoming)

    def _normalise(self, raw):
        description = raw.get("description") if isinstance(raw.get("description"), dict) else {}
        event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
        competition = raw.get("competition") if isinstance(raw.get("competition"), dict) else {}
        competition = {"id": str(competition.get("id", "")), "name": str(competition.get("name", ""))}
        rules = "\n\n".join(str(description[x]) for x in ("rules", "clarifications") if description.get(x))
        return {"market_id": str(raw.get("marketId", "")), "market_name": str(raw.get("marketName", "")),
                "event_id": str(event.get("id", "")), "event_name": str(event.get("name", "")),
                "event_type": raw.get("eventType") or {}, "competition": competition,
                "sport": "basketball", "league": competition["name"],
                "start_time": raw.get("marketStartTime") or event.get("openDate") or "",
                "market_type": str(description.get("marketType", "")), "rules": rules,
                "description": description,
                "commission": {"market_base_rate": _decimal(description.get("marketBaseRate")),
                               "discount_allowed": description.get("discountAllowed"), "effective_rate": None},
                "source_url": "https://www.betfair.com/exchange/plus/basketball/market/" + str(raw.get("marketId", "")),
                "currency": self.config.currency or None, "book_available": False,
                "market_status": None, "inplay": None, "bet_delay": None, "is_market_data_delayed": None,
                "market_version": None, "observed_at": None, "received_monotonic_ns": None,
                "data_mode": "unknown",
                "source_publish_time": None,
                "runners": [{"selection_id": str(r["selectionId"]), "runner_name": str(r.get("runnerName", "")),
                             "handicap": _decimal(r.get("handicap", 0)), "sort_priority": r.get("sortPriority"),
                             "status": None, "back": [], "lay": [], "observed_at": None,
                             "received_monotonic_ns": None, "source_publish_time": None}
                            for r in raw.get("runners", []) if isinstance(r, dict) and r.get("selectionId") is not None]}

    @staticmethod
    def _levels(values, *, reverse):
        # A malformed/empty level never becomes an apparent executable price.
        levels = []
        for value in values if isinstance(values, list) else []:
            if not isinstance(value, dict):
                continue
            odds, size = _decimal(value.get("price")), _decimal(value.get("size"))
            if odds is not None and size is not None and Decimal("1.01") <= Decimal(odds) <= 1000 and Decimal(size) > 0:
                levels.append({"odds": odds, "size": size})
        return sorted(levels, key=lambda x: Decimal(x["odds"]), reverse=reverse)[:3]

    def books(self, markets):
        result = copy.deepcopy(markets)
        # Callers may pass an earlier snapshot. Never retain its prices if a new
        # response omits a market or runner, or falsely advance the old clocks.
        for market in result:
            market.update(book_available=False, market_status=None, inplay=None, bet_delay=None,
                          is_market_data_delayed=None, data_mode="unknown", observed_at=None,
                          received_monotonic_ns=None, market_version=None)
            for runner in market["runners"]:
                runner.update(status=None, back=[], lay=[], observed_at=None, received_monotonic_ns=None)
        by_id = {m["market_id"]: m for m in result}
        ids = list(by_id)
        for start in range(0, len(ids), 20):
            response = self._call("listMarketBook", {"marketIds": ids[start:start + 20], "locale": "en",
                "priceProjection": {"priceData": ["EX_BEST_OFFERS"], "exBestOffersOverrides": {"bestPricesDepth": 3},
                                    "virtualise": False}})
            observed, monotonic = _utc(self.clock()), self.monotonic_ns()
            for raw in response:
                if not isinstance(raw, dict) or str(raw.get("marketId", "")) not in by_id:
                    continue
                market = by_id[str(raw["marketId"])]
                if raw.get("inplay") is True:
                    # A game can start between catalogue and book responses.
                    # Keep that observed state visible, then stop future polls.
                    self._inplay_ids.add(market["market_id"])
                market.update(book_available=True, market_status=raw.get("status"), inplay=raw.get("inplay"),
                    bet_delay=raw.get("betDelay"), is_market_data_delayed=raw.get("isMarketDataDelayed"),
                    market_version=raw.get("version"), observed_at=observed, received_monotonic_ns=monotonic,
                    total_matched=_decimal(raw.get("totalMatched")), total_available=_decimal(raw.get("totalAvailable")))
                market["data_mode"] = ("delayed" if raw.get("isMarketDataDelayed") is True or self.config.data_mode == "delayed"
                                       else "live" if raw.get("isMarketDataDelayed") is False and self.config.data_mode == "live"
                                       else "unknown")
                runners = {(str(r.get("selectionId")), _decimal(r.get("handicap", 0))): r
                           for r in raw.get("runners", []) if isinstance(r, dict)}
                for runner in market["runners"]:
                    book = runners.get((runner["selection_id"], runner["handicap"]))
                    if book is None:
                        continue
                    ex = book.get("ex") if isinstance(book.get("ex"), dict) else {}
                    runner.update(status=book.get("status"), back=self._levels(ex.get("availableToBack"), reverse=True),
                                  lay=self._levels(ex.get("availableToLay"), reverse=False),
                                  observed_at=observed, received_monotonic_ns=monotonic)
        return result

    def refresh(self, now=None):
        state = self.status()
        result = dict(state, source="betfair_rest", observed_at=None, markets=[], coverage={}, warnings=[])
        if state["status"] == "disabled":
            return result
        try:
            if self.config.replay_file:
                raw = json.loads(Path(self.config.replay_file).read_text())
                if raw.get("synthetic") is not True or not isinstance(raw.get("catalogue"), list) or not isinstance(raw.get("books"), list):
                    raise BetfairError("Replay must explicitly declare synthetic=true and contain catalogue/books arrays.")
                # Replay uses the same normalisation and never rewrites fixture time as current time.
                observed = _date(raw.get("observed_at"))
                if observed is None:
                    raise BetfairError("Synthetic replay requires its original observed_at timestamp.")
                adapter = BetfairData(Config(enabled=True, app_key="synthetic", session_token="synthetic", currency=raw.get("currency", "")),
                                     transport=lambda *args, **kwargs: raw["books"], clock=lambda: observed,
                                     monotonic_ns=lambda: 0)
                result.update(status="synthetic", state="replay", source="synthetic_replay", mode="synthetic", observed_at=_utc(observed),
                              markets=adapter.books([adapter._normalise(m) for m in raw["catalogue"]]),
                              coverage={"markets_discovered": len(raw["catalogue"]), "partial": False},
                              warnings=["Synthetic fixture prices; excluded from live observations and arbitrage alerts."])
                for market in result["markets"]:
                    market["data_mode"] = "synthetic"
            else:
                markets = self.books(self.discover(now))
                result.update(status="ok", state="ok", markets=markets, coverage=copy.deepcopy(self._coverage), warnings=list(self._warnings),
                              observed_at=max((m["observed_at"] for m in markets if m.get("observed_at")), default=None))
                modes = {m["data_mode"] for m in markets if m.get("book_available")}
                result["mode"] = next(iter(modes)) if len(modes) == 1 else "unknown"
                omitted = sum(not m["book_available"] for m in markets)
                if omitted:
                    result["warnings"].append(f"{omitted} discovered markets had no book response; no prices were fabricated.")
                inplay = sum(m.get("inplay") is True for m in markets)
                if inplay:
                    result["warnings"].append(f"{inplay} markets advanced in-play after discovery; their state is marked and future pre-match polling stops.")
        except BetfairError as exc:
            result.update(status="error", state="error", message=str(exc), coverage=copy.deepcopy(self._coverage))
        except Exception:
            result.update(status="error", state="error", message="Betfair response or replay could not be decoded; no prices were refreshed.")
        self._last = copy.deepcopy(result)
        return result

    def close(self):
        # A caller-provided token can be shared by other tools; do not revoke it.
        if self._owns_session and self._token:
            try:
                self._request(self._identity("logout"), {})
            finally:
                self._token, self._owns_session = "", False
