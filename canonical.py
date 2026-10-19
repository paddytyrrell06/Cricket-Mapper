# This file turns venue-specific cricket events and markets into consistent fixtures, teams, market types, outcomes, times, and public event links.

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional


MATCH_WINNER = "match_winner"
TOSS_WINNER = "toss_winner"
MATCH_COMPLETED = "match_completed"
TOSS_MATCH_DOUBLE = "toss_match_double"
TEAM_TOTAL = "team_total"
TOTAL_SIXES = "total_sixes"
TOTAL_FOURS = "total_fours"
MOST_SIXES = "most_sixes"
TEAM_TOP_BATTER = "team_top_batter"
OUTRIGHT = "outright"
UNKNOWN = "unknown"


KALSHI_SERIES: dict[str, tuple[str, str, str, str]] = {
    "KXHUNDREDMATCH":    ("M", "HUNDRED", "The Hundred", MATCH_WINNER),
    "KXWHUNDREDMATCH":   ("W", "HUNDRED", "The Hundred", MATCH_WINNER),
    "KXODIMATCH":        ("M", "LISTA", "One-Day Cup", MATCH_WINNER),
    "KXWODIMATCH":       ("W", "LISTA", "One-Day Cup", MATCH_WINNER),
    "KXTESTMATCH":       ("M", "TEST", "Test", MATCH_WINNER),
    "KXWTESTMATCH":      ("W", "TEST", "Test", MATCH_WINNER),
    "KXT20MATCH":        ("M", "T20", "", MATCH_WINNER),
    "KXWT20MATCH":       ("W", "T20", "", MATCH_WINNER),
    "KXT20CANADAMATCH":  ("M", "T20", "Global T20 Canada", MATCH_WINNER),
    "KXLPLMATCH":        ("M", "T20", "Lanka Premier League", MATCH_WINNER),
    "KXIPLGAME":         ("M", "T20", "Indian Premier League", MATCH_WINNER),
    "KXCOUNTYCHAMPMATCH": ("M", "FIRSTCLASS", "County Championship",
                           MATCH_WINNER),
    "KXWT20TEAMTOTAL":   ("W", "T20", "", TEAM_TOTAL),
    "KXIPLTEAMTOTAL":    ("M", "T20", "Indian Premier League", TEAM_TOTAL),
    "KXIPLSIX":          ("M", "T20", "Indian Premier League", TOTAL_SIXES),
    "KXIPLFOUR":         ("M", "T20", "Indian Premier League", TOTAL_FOURS),

    "KXIPL":             ("M", "T20", "Indian Premier League", OUTRIGHT),
    "KXCPL":             ("M", "T20", "Caribbean Premier League", OUTRIGHT),
    "KXCPLMATCH":        ("M", "T20", "Caribbean Premier League", MATCH_WINNER),
    "KXLPL":             ("M", "T20", "Lanka Premier League", OUTRIGHT),
}


KALSHI_NOT_CRICKET = {
    "KXSCOTTISHPREMGAME",
    "KXBBLGAME",
}


PM_LEAGUES: dict[str, tuple[str, str, str]] = {
    "hundred":    ("M", "HUNDRED", "The Hundred"),
    "hundredw":   ("W", "HUNDRED", "The Hundred"),
    "odc":        ("M", "LISTA", "One-Day Cup"),
    "odcl2w":     ("W", "LISTA", "One-Day Cup"),
    "t20lpl":     ("M", "T20", "Lanka Premier League"),
    "ipl":        ("M", "T20", "Indian Premier League"),
    "psl":        ("M", "T20", "Pakistan Super League"),
    "cpl":        ("M", "T20", "Caribbean Premier League"),
    "bbl":        ("M", "T20", "Big Bash League"),
    "sa20":       ("M", "T20", "SA20"),
    "mlc":        ("M", "T20", "Major League Cricket"),
    "t20blast":   ("M", "T20", "T20 Blast"),
    "t20blastw":  ("W", "T20", "T20 Blast"),
    "int":        ("M", "", "International"),
    "nt":         ("M", "T20", "National T20"),
    "ecseng":     ("M", "T10", "ECS England"),
    "ecsch":      ("M", "T10", "ECS Switzerland"),
    "ecsbg":      ("M", "T10", "ECS Bulgaria"),
    "kerala":     ("M", "T20", "Kerala Cricket League"),
    "mukono":     ("M", "T20", "Mukono"),
    "jclt10":     ("M", "T10", "Japan Cricket League"),
    "jcl":        ("M", "T20", "Japan Cricket League"),
    "lcl":        ("M", "T20", "Legends League Cricket"),
    "gsl":        ("M", "T20", "Global Super League"),
    "maharani":   ("W", "T20", "Maharani Trophy"),
    "tbcl":       ("M", "T10", "Thunderbolt T10 League"),
    "coppat10":   ("M", "T10", "Coppa T10"),
    "germant10":  ("M", "T10", "German T10"),
    "thunderbolt": ("M", "T10", "Thunderbolt T10 League"),
    "pakt20cup":  ("M", "T20", "Pakistan T20 Cup"),
    "nt20c":      ("M", "T20", "National T20 Cup"),
    "interprov":  ("M", "LISTA", "Inter-Provincial"),
    "shpageeza":  ("M", "T20", "Shpageeza"),
    "telangana":  ("M", "T20", "Telangana"),
    "falcons":    ("M", "T20", "Falcons T20"),
    "kpl":        ("M", "T20", "Kashmir Premier League"),
    "ppl":        ("M", "T20", "Pondicherry Premier League"),
    "nsk":        ("M", "T20", "NSK T20"),
    "apl":        ("M", "T20", "Assam Premier League"),
    "ilt20":      ("M", "T20", "ILT20"),
    "wncl":       ("W", "LISTA", "Womens National Cricket League"),
    "ss":         ("M", "T20", "Super Smash"),
    "sm":         ("M", "T20", "Super Smash"),
    "maharaja":   ("M", "T20", "Maharaja Trophy"),
}


PM_SUFFIX: dict[str, str] = {
    "": MATCH_WINNER,
    "toss-winner": TOSS_WINNER,
    "completed-match": MATCH_COMPLETED,
    "most-sixes": MOST_SIXES,
    "total-sixes": TOTAL_SIXES,
    "total-fours": TOTAL_FOURS,
    "team-total": TEAM_TOTAL,
    "top-batter": TEAM_TOP_BATTER,
    "team-top-batter": TEAM_TOP_BATTER,
}

PM_SLUG = re.compile(
    r"^cr(?:ic)?(?P<league>[a-z0-9]+?)-"
    r"(?P<a>[a-z0-9]+)-(?P<b>[a-z0-9]+)-"
    r"(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:-(?P<suffix>.+))?$"
)

KALSHI_TICKER = re.compile(
    r"^(?P<series>KX[A-Z0-9]+?)-"
    r"(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})"
    r"(?P<hhmm>\d{4})?"
    r"(?P<teams>[A-Z0-9]{4,12})"
    r"-(?P<outcome>[A-Z0-9.]+)$"
)

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


def _iso_utc_parts(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        return "", ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "", ""
    if parsed.tzinfo is None:
        return "", ""
    utc = parsed.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%d"), utc.strftime("%H:%M")


def _url_slug(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().replace("’", "").replace("'", "")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def _kalshi_market_url(market: dict, series: str, ticker: str) -> str:
    event_ticker = str(market.get("event_ticker") or "").strip()
    series_title = str(market.get("_series_title") or "").strip()
    series_slug = _url_slug(series)
    title_slug = _url_slug(series_title)
    event_slug = _url_slug(event_ticker)
    if series_slug and title_slug and event_slug:
        return (f"https://kalshi.com/markets/{series_slug}/"
                f"{title_slug}/{event_slug}")
    return f"https://external-api.kalshi.com/trade-api/v2/markets/{ticker}"

def norm_team(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    s = re.sub(r"\b(the|cricket|club|cc)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_competition_name(value: str) -> str:
    s = unicodedata.normalize("NFKD", str(value or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\b(metro bank|vitality|betway|tata|dream11)\b", " ", s)
    s = re.sub(r"\b(men|women|mens|womens)\b", " ", s)
    s = re.sub(r"\b(?:19|20)\d{2}\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    aliases = {
        "t20 global super league": "global super league",
        "metro bank one day cup": "one day cup",
    }
    return aliases.get(s, s)


def _load_team_aliases() -> list[dict]:
    path = Path(__file__).resolve().parent / "config" / "team_aliases.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot load trusted team aliases from {path}: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError("team_aliases.json must contain a list")
    return data


TRUSTED_TEAM_ALIASES = _load_team_aliases()


def canonical_team_name(name: str, competition: str, date: str
                        ) -> tuple[str, Optional[dict]]:
    raw = str(name or "").strip()
    normal = norm_team(raw)
    competition_key = _norm_competition_name(competition)
    for rule in TRUSTED_TEAM_ALIASES:
        if _norm_competition_name(rule.get("competition", "")) != competition_key:
            continue
        valid_from = str(rule.get("valid_from") or "")
        valid_to = str(rule.get("valid_to") or "")
        if valid_from and date and date < valid_from:
            continue
        if valid_to and date and date > valid_to:
            continue
        aliases = {norm_team(x) for x in rule.get("aliases", [])}
        if normal not in aliases:
            continue
        canonical = str(rule.get("canonical") or raw).strip()
        if norm_team(canonical) == normal:
            return canonical, None
        return canonical, {
            "rule_id": str(rule.get("id") or ""),
            "published_name": raw,
            "canonical_name": canonical,
            "competition": competition,
            "date": date,
            "evidence": str(rule.get("evidence") or ""),
        }
    return raw, None


def canonicalise_team_pair(names: list[str], competition: str, date: str
                           ) -> tuple[list[str], list[dict]]:
    canonical: list[str] = []
    applications: list[dict] = []
    for name in names:
        resolved, applied = canonical_team_name(name, competition, date)
        canonical.append(resolved)
        if applied:
            applications.append(applied)
    return canonical, applications


@dataclass(frozen=True)
class FixtureKey:
    date: str
    teams: frozenset
    gender: str
    fmt: str

    def pretty(self) -> str:
        a, b = sorted(self.teams) if len(self.teams) == 2 else \
            (sorted(self.teams) + ["?"])[:2]
        return f"{self.date}|{a} v {b}|{self.gender}|{self.fmt}"


@dataclass
class Fixture:
    key: FixtureKey
    venue: str
    venue_fixture_id: str
    competition: str = ""
    start_time: str = ""
    team_a: str = ""
    team_b: str = ""
    note: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Market:
    venue: str
    venue_fixture_id: str
    fixture_key: Optional[FixtureKey]
    market_type: str
    outcome: str
    event_id: str = ""
    market_id: str = ""
    slug: str = ""
    title: str = ""
    url: str = ""
    raw_outcome: str = ""
    note: str = ""
    extra: dict = field(default_factory=dict)


def _j(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


_VS_TITLE = re.compile(
    r"(?P<a>[^:]+?)\s+vs\.?\s+(?P<b>.+?)(?:\s+-\s+|\s+match:|$)",
    re.I,
)


def _teams_from_title(value: str) -> list[str]:
    text = str(value or "")


    if ":" in text:
        tail = text.rsplit(":", 1)[-1].strip()
        if re.search(r"\s+vs\.?\s+", tail, re.I):
            text = tail
    match = _VS_TITLE.search(text)
    if not match:
        return []
    a = match.group("a").strip()
    b = match.group("b").strip()


    suffix = re.compile(r"\s+(?:men(?:'s)?|women(?:'s)?)\s+cricket$", re.I)
    a = suffix.sub("", a).strip()
    b = suffix.sub("", b).strip()
    if ":" in a:
        a = a.rsplit(":", 1)[-1].strip()
    return [a, b]


def _gender_format_from_text(*parts: str) -> tuple[str, str]:
    blob = " ".join(str(p or "") for p in parts).lower()
    gender = "W" if re.search(r"\bwomen(?:'s|s)?\b|\bfemale\b|\bladies\b", blob) else "M"
    if re.search(r"\btest\b|first[- ]class|county championship", blob):
        fmt = "TEST" if re.search(r"\btest\b", blob) else "FIRSTCLASS"
    elif re.search(r"\bthe hundred\b|\bhundred\b", blob):
        fmt = "HUNDRED"
    elif re.search(r"\bt10\b", blob):
        fmt = "T10"
    elif re.search(r"\bt20\b|twenty[ -]?20|\bipl\b|\blpl\b|\bpsl\b|\bbbl\b|\bsa20\b|\bcpl\b", blob):
        fmt = "T20"
    elif re.search(r"\bodi\b|one[- ]day|list[ -]?a", blob):
        fmt = "LISTA"
    else:
        fmt = ""
    return gender, fmt


def _infer_kalshi_meta(series: str, title: str, series_title: str
                       ) -> tuple[str, str, str, str]:
    gender, fmt = _gender_format_from_text(series, title, series_title)
    upper = series.upper()
    if upper.startswith("KXW") or "WOMEN" in upper:
        gender = "W"
    if "TEST" in upper:
        fmt = "TEST"
    elif "COUNTYCHAMP" in upper:
        fmt = "FIRSTCLASS"
    elif "ODI" in upper:
        fmt = "LISTA"
    elif "HUNDRED" in upper:
        fmt = "HUNDRED"
    elif "T10" in upper:
        fmt = "T10"
    elif any(x in upper for x in ("T20", "IPL", "LPL", "PSL", "BBL", "CPL")):
        fmt = "T20"

    if "TEAMTOTAL" in upper:
        market_type = TEAM_TOTAL
    elif upper.endswith("SIX") or "SIXES" in upper:
        market_type = TOTAL_SIXES
    elif upper.endswith("FOUR") or "FOURS" in upper:
        market_type = TOTAL_FOURS
    elif "MATCH" in upper or "GAME" in upper:
        market_type = MATCH_WINNER
    else:
        market_type = UNKNOWN
    return gender, fmt, series_title or series, market_type


def _event_scope(m: dict, fallback: str = "") -> str:
    return str(m.get("event_ticker") or m.get("_event_id") or fallback)


_KALSHI_RULE_START = re.compile(
    r"scheduled for\s+(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),\s+"
    r"(?P<year>\d{4})\s+at\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})\s+"
    r"(?P<ampm>AM|PM)\s+(?P<zone>EDT|EST)", re.I)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> int:
    first = datetime(year, month, 1)
    return 1 + ((weekday - first.weekday()) % 7) + 7 * (n - 1)


def _valid_us_eastern_label(local: datetime, label: str) -> bool:
    if local.year < 2007 or label not in {"EDT", "EST"}:
        return False
    march_day = _nth_weekday(local.year, 3, 6, 2)
    november_day = _nth_weekday(local.year, 11, 6, 1)
    start = datetime(local.year, 3, march_day, 2, 0)
    end = datetime(local.year, 11, november_day, 2, 0)
    if local.date() == end.date() and local.hour == 1:
        return True
    expected = "EDT" if start <= local < end else "EST"
    return label == expected


def _kalshi_start_utc_parts(market: dict) -> tuple[str, str, str, str]:
    text = str(market.get("rules_primary") or "")
    match = _KALSHI_RULE_START.search(text)
    if not match:
        return "", "", "unavailable", "scheduled start not found in rules_primary"
    try:
        local_naive = datetime.strptime(
            f"{match.group('mon')} {match.group('day')} {match.group('year')} "
            f"{match.group('hour')}:{match.group('minute')} {match.group('ampm')}",
            "%b %d %Y %I:%M %p",
        )
    except (ValueError, KeyError):
        return "", "", "unavailable", "scheduled start could not be parsed"
    published_zone = match.group("zone").upper()
    if not _valid_us_eastern_label(local_naive, published_zone):
        other = "EST" if published_zone == "EDT" else "EDT"
        return ("", "", "unavailable",
                f"published timezone {published_zone} conflicts with expected {other}")
    offset_hours = -4 if published_zone == "EDT" else -5
    local = local_naive.replace(
        tzinfo=timezone(timedelta(hours=offset_hours), name=published_zone))
    utc = local.astimezone(timezone.utc)
    return (utc.strftime("%Y-%m-%d"), utc.strftime("%H:%M"),
            f"rules_primary_{published_zone}_to_utc", "")


def parse_kalshi(markets: list[dict]) -> tuple[list[Fixture], list[Market],
                                               list[str]]:
    warnings: list[str] = []


    code_name: dict[tuple[str, str], set[str]] = defaultdict(set)
    for market in markets:
        match = KALSHI_TICKER.match(market.get("ticker") or "")
        if not match:
            continue
        scope = _event_scope(market, match.group("teams"))
        subtitle = str(market.get("yes_sub_title") or "").strip()
        if subtitle:
            code_name[(scope, match.group("outcome"))].add(subtitle)
        title_teams = _teams_from_title(str(market.get("title") or ""))
        codes = _split_codes(match.group("teams"), match.group("outcome"))
        if len(title_teams) == 2 and len(codes) == 2 and subtitle:
            other_names = [n for n in title_teams if norm_team(n) != norm_team(subtitle)]
            other_codes = [c for c in codes if c != match.group("outcome")]
            if len(other_names) == 1 and len(other_codes) == 1:
                code_name[(scope, other_codes[0])].add(other_names[0])

    fixtures: dict[str, Fixture] = {}
    out: list[Market] = []

    for market in markets:
        ticker = market.get("ticker") or ""
        series = (market.get("_queried_series") or market.get("series_ticker") or
                  ticker.split("-")[0]).upper()
        if series in KALSHI_NOT_CRICKET:
            continue

        meta = KALSHI_SERIES.get(series)
        inferred = False
        if meta:
            gender, fmt, competition, market_type = meta
        else:
            inferred = True
            gender, fmt, competition, market_type = _infer_kalshi_meta(
                series, str(market.get("title") or ""),
                str(market.get("_series_title") or ""))
            native_series = bool(market.get("_native_cricket_series"))
            inference_complete = bool(
                gender and fmt and competition and market_type != UNKNOWN)
            if not native_series or not inference_complete:
                warnings.append(
                    f"COVERAGE_INCOMPLETE kalshi: cannot safely classify "
                    f"unmapped series {series}")
            else:
                warnings.append(
                    f"INFO kalshi: safely inferred native cricket series {series}")

        if market_type == OUTRIGHT:
            out.append(Market(
                venue="kalshi", venue_fixture_id="", fixture_key=None,
                market_type=OUTRIGHT, outcome=market.get("yes_sub_title", ""),
                event_id=market.get("event_ticker", ""), market_id=ticker,
                slug=ticker, title=market.get("title", ""),
                url=_kalshi_market_url(market, series, ticker),
                raw_outcome=market.get("yes_sub_title", ""),
                note="tournament outright, no fixture"))
            continue

        match = KALSHI_TICKER.match(ticker)
        if not match:
            warnings.append(f"COVERAGE_INCOMPLETE kalshi: unparsed ticker {ticker}")
            continue
        month = MONTHS.get(match.group("mon"), 0)
        if not month:
            warnings.append(f"COVERAGE_INCOMPLETE kalshi: bad month in {ticker}")
            continue
        ticker_date = f"20{match.group('yy')}-{month:02d}-{match.group('dd')}"
        hhmm = match.group("hhmm") or ""
        utc_date, utc_time, time_basis, time_error = _kalshi_start_utc_parts(market)
        date = utc_date or ticker_date
        blob = match.group("teams")
        outcome_code = match.group("outcome")
        scope = _event_scope(market, blob)

        names: list[str] = []
        notes: list[str] = []
        codes = _split_codes(blob, outcome_code)
        for code in codes:
            found = code_name.get((scope, code), set())
            if len(found) == 1:
                names.append(next(iter(found)))
            elif len(found) > 1:
                names.append(sorted(found)[0])
                notes.append(f"event code {code} resolves to multiple names")
            else:
                names.append(code)
                notes.append(f"unresolved event team code {code}")
        if inferred:
            notes.append(f"unmapped series metadata inferred from {series}")
        if time_error and str(market.get("rules_primary") or "").strip():
            notes.append(time_error)
            warnings.append(f"COVERAGE_INCOMPLETE kalshi: {time_error} for {ticker}")
        if not fmt:
            notes.append("format not established")
        if market_type == UNKNOWN:
            notes.append("market type not established")
        note = "; ".join(dict.fromkeys(notes))

        canonical_names, alias_applications = canonicalise_team_pair(
            names, competition, date)
        fixture_key = FixtureKey(
            date, frozenset(norm_team(name) for name in canonical_names), gender, fmt)
        fixture_id = f"kalshi:{series}:{date}:{blob}"
        display = sorted(names)
        fixtures.setdefault(fixture_id, Fixture(
            key=fixture_key, venue="kalshi", venue_fixture_id=fixture_id,
            competition=competition,
            start_time=utc_time,
            team_a=display[0] if display else "",
            team_b=display[1] if len(display) > 1 else "", note=note,
            extra={"team_codes": sorted(codes), "event_ticker": scope,
                   "series": series,
                   "ticker_local_date": ticker_date,
                   "ticker_local_time": (f"{hhmm[:2]}:{hhmm[2:]}" if hhmm else ""),
                   "start_time_basis": time_basis,
                   "trusted_aliases": alias_applications}))

        subtitle = str(market.get("yes_sub_title") or "")
        resolved = code_name.get((scope, outcome_code), set())
        canonical_outcome = (next(iter(resolved)) if len(resolved) == 1
                             else (subtitle or outcome_code))
        if outcome_code in ("TIE", "DRAW"):
            canonical_outcome = "draw"
        elif canonical_outcome:
            canonical_outcome, _ = canonical_team_name(
                canonical_outcome, competition, date)

        out.append(Market(
            venue="kalshi", venue_fixture_id=fixture_id,
            fixture_key=fixture_key, market_type=market_type,
            outcome=canonical_outcome,
            event_id=market.get("event_ticker", ""), market_id=ticker,
            slug=ticker, title=market.get("title", ""),
            url=_kalshi_market_url(market, series, ticker),
            raw_outcome=subtitle or outcome_code, note=note,
            extra={"series": series,
                   "series_page": f"https://kalshi.com/markets/{series.lower()}",
                   "outcome_code": outcome_code}))
    return list(fixtures.values()), out, warnings


def _split_codes(blob: str, outcome: str) -> list[str]:
    if outcome and outcome not in ("TIE", "DRAW") and len(blob) > len(outcome):
        if blob.endswith(outcome):
            return [blob[:-len(outcome)], outcome]
        if blob.startswith(outcome):
            return [outcome, blob[len(outcome):]]
    if len(blob) % 2 == 0:
        h = len(blob) // 2
        return [blob[:h], blob[h:]]
    return [blob]


def _pm_fixture_tuple(match: re.Match) -> tuple[str, str, str, str]:
    return (match.group("league"), match.group("a"),
            match.group("b"), match.group("date"))


def _pm_target_type(suffix: str) -> tuple[str, str]:
    if suffix in PM_SUFFIX:
        return PM_SUFFIX[suffix], ""
    for prefix, market_type in (
        ("toss-match-double-", TOSS_MATCH_DOUBLE),
        ("most-sixes-", MOST_SIXES),
        ("team-top-batter-", TEAM_TOP_BATTER),
        ("top-batter-", TEAM_TOP_BATTER),
        ("team-total-", TEAM_TOTAL),
        ("total-sixes-", TOTAL_SIXES),
        ("total-fours-", TOTAL_FOURS),
    ):
        if suffix.startswith(prefix):
            return market_type, suffix[len(prefix):]
    return UNKNOWN, ""


def _pm_competition_meta(league: str, title: str) -> tuple[str, str, str, bool]:
    mapped = PM_LEAGUES.get(league)
    inferred = mapped is None
    if mapped:
        gender, fmt, competition = mapped
    else:
        gender, fmt = _gender_format_from_text(league, title)
        competition = league
    text_gender, text_fmt = _gender_format_from_text(title, league)
    if text_gender == "W":
        gender = "W"
    if not fmt:
        fmt = text_fmt
    return gender, fmt, competition, inferred

def parse_polymarket(markets: list[dict], venue: str
                     ) -> tuple[list[Fixture], list[Market], list[str]]:
    warnings: list[str] = []

    code_name: dict[tuple[tuple[str, str, str, str], str], set[str]] = defaultdict(set)
    for market in markets:
        match = PM_SLUG.match(market.get("slug") or "")
        if not match:
            continue
        fixture = _pm_fixture_tuple(match)
        outcomes = _j(market.get("outcomes")) or []
        if not match.group("suffix") and isinstance(outcomes, list) and len(outcomes) == 2:
            code_name[(fixture, match.group("a"))].add(str(outcomes[0]))
            code_name[(fixture, match.group("b"))].add(str(outcomes[1]))
        title_teams = _teams_from_title(
            str(market.get("_event_title") or market.get("question") or ""))
        if len(title_teams) == 2:
            code_name[(fixture, match.group("a"))].add(title_teams[0])
            code_name[(fixture, match.group("b"))].add(title_teams[1])

    fixtures: dict[str, Fixture] = {}
    out: list[Market] = []
    for market in markets:
        slug = market.get("slug") or ""
        match = PM_SLUG.match(slug)
        if not match:
            warnings.append(f"COVERAGE_INCOMPLETE {venue}: unparsed slug {slug[:60]}")
            continue

        league = match.group("league")
        event_title = str(market.get("_event_title") or market.get("question") or "")
        gender, fmt, competition, inferred_league = _pm_competition_meta(
            league, event_title)
        if inferred_league:
            warnings.append(f"COVERAGE_INCOMPLETE {venue}: inferred unmapped league {league}")

        suffix = match.group("suffix") or ""
        market_type, target_code = _pm_target_type(suffix)
        notes: list[str] = []
        if market_type == UNKNOWN:
            notes.append(f"unmapped suffix {suffix}")
        if not fmt:
            notes.append("format not established")

        fixture_tuple = _pm_fixture_tuple(match)
        names: list[str] = []
        for code in (match.group("a"), match.group("b")):
            found = code_name.get((fixture_tuple, code), set())
            if len(found) == 1:
                names.append(next(iter(found)))
            elif len(found) > 1:
                names.append(sorted(found)[0])
                notes.append(f"fixture code {code} resolves to multiple names")
            else:
                names.append(code)
                notes.append(f"unresolved fixture team code {code}")
        note = "; ".join(dict.fromkeys(notes))

        slug_date = match.group("date")
        start_raw = str(market.get("_event_start_time") or "")
        start_date_utc, start_time = _iso_utc_parts(start_raw)
        date = start_date_utc or slug_date
        canonical_names, alias_applications = canonicalise_team_pair(
            names, competition, date)
        fixture_key = FixtureKey(
            date, frozenset(norm_team(name) for name in canonical_names), gender, fmt)
        fixture_id = (f"{venue}:{league}:{date}:"
                      f"{match.group('a')}-{match.group('b')}")
        fixtures.setdefault(fixture_id, Fixture(
            key=fixture_key, venue=venue, venue_fixture_id=fixture_id,
            competition=competition, start_time=start_time,
            team_a=names[0], team_b=names[1], note=note,
            extra={"team_codes": sorted([match.group("a"), match.group("b")]),
                   "league": league,
                   "event_slug": market.get("_event_slug") or "",
                   "slug_date": slug_date,
                   "start_time_basis": ("event_start_timestamp_to_utc"
                                        if start_date_utc else "unavailable"),
                   "trusted_aliases": alias_applications}))

        outcomes = _j(market.get("outcomes")) or []
        url = f"https://polymarket.com/event/{market.get('_event_slug') or slug}"
        common = dict(
            venue=venue, venue_fixture_id=fixture_id, fixture_key=fixture_key,
            market_type=market_type,
            event_id=str(market.get("_event_id") or market.get("_event_slug") or ""),
            market_id=str(market.get("id", "")), slug=slug,
            title=market.get("question", ""), url=url, note=note)


        if target_code:
            if target_code in ("draw", "tie"):
                target = "draw"
            else:
                found = code_name.get((fixture_tuple, target_code), set())
                target = next(iter(found)) if len(found) == 1 else target_code
                target, _ = canonical_team_name(target, competition, date)
            out.append(Market(**common, outcome=target, raw_outcome=target_code,
                              extra={"binary_target_market": True}))
            continue

        if not isinstance(outcomes, list) or not outcomes:
            out.append(Market(**common, outcome="", raw_outcome=""))
            continue
        for outcome_name in outcomes:
            canonical = str(outcome_name)
            if canonical.strip().lower() in ("draw", "tie", "no winner"):
                canonical = "draw"
            else:
                canonical, _ = canonical_team_name(canonical, competition, date)
            out.append(Market(**common, outcome=canonical,
                              raw_outcome=str(outcome_name)))
    return list(fixtures.values()), out, warnings


PMUS_SLUG = re.compile(
    r"^(?:aec-)?(?P<league>[a-z0-9]+)-"
    r"(?P<a>[a-z0-9]+)-(?P<b>[a-z0-9]+)-"
    r"(?P<date>\d{4}-\d{2}-\d{2})$"
)

PMUS_TITLE = re.compile(r"^\s*(?P<a>.+?)\s+vs\.?\s+(?P<b>.+?)\s*$", re.I)


def _pmus_gender_format(*text: str) -> tuple[str, str]:
    blob = " ".join(t for t in text if t).lower()
    gender = "W" if re.search(r"\bwomen('s)?\b|\bwomens\b", blob) else "M"
    if re.search(r"\btest\b", blob):
        fmt = "TEST"
    elif re.search(r"\bthe hundred\b|\bhundred\b", blob):
        fmt = "HUNDRED"
    elif re.search(r"\bt10\b", blob):
        fmt = "T10"
    elif re.search(r"\bt20\b|\btwenty20\b", blob):
        fmt = "T20"
    elif re.search(r"\bodi\b|one[- ]day", blob):
        fmt = "LISTA"
    else:
        fmt = ""
    return gender, fmt


_PMUS_CRICKET = re.compile(
    r"\bcricket\b|\bt20\b|\bodi\b|\btest match\b|\bwickets?\b|"
    r"\bovers?\b|\binnings\b|\bthe hundred\b", re.I)
_PMUS_NOT_CRICKET = re.compile(
    r"\bitf\b|\batp\b|\bwta\b|tennis|\buwcl\b|champions league|"
    r"\bnfl\b|\bnba\b|\bnhl\b|\bmlb\b|soccer|football", re.I)


def _pmus_is_cricket(m: dict) -> bool:
    blob = " ".join(str(m.get(k) or "") for k in
                    ("question", "description", "_event_title",
                     "_series_slug", "_event_ticker", "slug"))
    if m.get("_native_sport") == "cricket":
        return True
    if _PMUS_NOT_CRICKET.search(blob):
        return False
    return bool(_PMUS_CRICKET.search(blob))


def _published_team_name(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    nested = value.get("team") or {}
    if isinstance(nested, dict):
        name = nested.get("name") or nested.get("safeName") or nested.get("displayName")
        if name:
            return str(name).strip()
    name = (value.get("name") or value.get("safeName") or
            value.get("displayName") or value.get("description"))
    return str(name).strip() if name else ""


def _pmus_team_names(market: dict, event_title: str) -> list[str]:
    names: list[str] = []

    def add(value: Any) -> None:
        name = _published_team_name(value)
        if name and norm_team(name) not in {norm_team(n) for n in names}:
            names.append(name)


    for side in (market.get("marketSides") or []):
        add(side)
    if len(names) >= 2:
        return names[:2]


    for team in (market.get("_event_teams") or []):
        add(team)
    if len(names) >= 2:
        return names[:2]

    title_match = PMUS_TITLE.match(event_title or "")
    if title_match:
        return [title_match.group("a").strip(), title_match.group("b").strip()]
    outcomes = _j(market.get("outcomes")) or []
    if isinstance(outcomes, list):
        team_outcomes = [str(x) for x in outcomes
                         if str(x).strip().lower() not in ("yes", "no", "draw", "tie")]
        if len(team_outcomes) >= 2:
            return team_outcomes[:2]
    return []


def _pmus_market_type(market: dict) -> str:
    raw = " ".join(str(market.get(key) or "") for key in
                   ("sportsMarketType", "sportsMarketTypeV2", "marketType",
                    "question", "title", "_event_title")).lower()
    if ("moneyline" in raw or "match_winner" in raw or
            re.search(r"\bwho (?:will|wins?)\b.*\bwin|\bgame winner\b", raw) or
            raw.strip() in {"winner", "game winner"}):
        return MATCH_WINNER
    if "toss" in raw and "double" in raw:
        return TOSS_MATCH_DOUBLE
    if "toss" in raw:
        return TOSS_WINNER
    if "team_total" in raw or "team total" in raw:
        return TEAM_TOTAL
    if "most_sixes" in raw or "most sixes" in raw:
        return MOST_SIXES
    return UNKNOWN


def _pmus_competition(market: dict) -> str:
    for tag in (market.get("tags") or []):
        if not isinstance(tag, dict):
            continue
        league = tag.get("league") or {}
        if isinstance(league, dict) and league.get("name"):
            return str(league["name"])
    slug = str(market.get("_series_slug") or "").lower()
    if "one-day-cup" in slug or "one-day cup" in slug:
        return "One-Day Cup"
    if "hundred" in slug:
        return "The Hundred"
    if "global-super-league" in slug:
        return "Global Super League"
    if "lanka-premier-league" in slug or re.search(r"(^|-)lpl($|-)", slug):
        return "Lanka Premier League"
    if re.search(r"(^|-)(t20i|odi|test)(-|$)", slug):
        return "International"
    return slug


def parse_polymarket_us(markets: list[dict]
                        ) -> tuple[list[Fixture], list[Market], list[str]]:
    warnings: list[str] = []
    fixtures: dict[str, Fixture] = {}
    out: list[Market] = []
    venue = "polymarket_us"
    skipped = 0

    for market in markets:
        if not _pmus_is_cricket(market):
            skipped += 1
            continue
        slug = str(market.get("slug") or "")
        ticker = str(market.get("_event_ticker") or "")
        slug_match = PMUS_SLUG.match(slug) or PMUS_SLUG.match(ticker)
        event_title = str(market.get("_event_title") or market.get("title") or "")
        event_desc = str(market.get("_event_description") or "")
        description = str(market.get("description") or "")
        question = str(market.get("question") or "")
        series_slug = str(market.get("_series_slug") or "")

        gender, fmt = _pmus_gender_format(
            event_desc, description, question, series_slug,
            str(market.get("sportsMarketType") or ""),
            slug_match.group("league") if slug_match else "")
        notes: list[str] = []
        if not fmt:
            notes.append("format not stated in venue fields")
            warnings.append(f"COVERAGE_INCOMPLETE {venue}: unknown format for {slug[:50]}")

        names = _pmus_team_names(market, event_title)
        if len(names) != 2:
            if slug_match:
                names = [slug_match.group("a"), slug_match.group("b")]
                notes.append("team names unavailable; slug codes used")
            else:
                warnings.append(f"COVERAGE_INCOMPLETE {venue}: no two teams for {slug[:60]}")
                continue

        start_raw = str(market.get("_start_time") or market.get("gameStartTime") or "")
        start_date_utc, start_time_utc = _iso_utc_parts(start_raw)
        event_date = start_date_utc or str(market.get("_event_date") or "")[:10]
        if not event_date and slug_match:
            event_date = slug_match.group("date")
        if not event_date:
            warnings.append(f"COVERAGE_INCOMPLETE {venue}: no event date for {slug[:60]}")
            continue

        market_type = _pmus_market_type(market)
        if market_type == UNKNOWN:
            notes.append("unmapped sports market type")
            warnings.append(
                f"COVERAGE_INCOMPLETE {venue}: unmapped sports market type for {slug[:50]}")
        note = "; ".join(dict.fromkeys(notes))
        competition = _pmus_competition(market)
        canonical_names, alias_applications = canonicalise_team_pair(
            names, competition, event_date)
        fixture_key = FixtureKey(
            event_date, frozenset(norm_team(name) for name in canonical_names), gender, fmt)
        event_id = str(market.get("_event_id") or ticker or
                       (slug_match.group(0) if slug_match else slug))
        fixture_id = f"{venue}:{event_id}"
        fixtures.setdefault(fixture_id, Fixture(
            key=fixture_key, venue=venue, venue_fixture_id=fixture_id,
            competition=competition,
            start_time=start_time_utc,
            team_a=names[0], team_b=names[1], note=note,
            extra={"team_codes": ([slug_match.group("a"), slug_match.group("b")]
                                  if slug_match else []),
                   "event_ticker": ticker,
                   "start_time_basis": ("event_start_timestamp_to_utc"
                                        if start_date_utc else "unavailable"),
                   "trusted_aliases": alias_applications}))

        url = f"https://polymarket.us/event/{market.get('_event_slug') or slug}"
        common = dict(
            venue=venue, venue_fixture_id=fixture_id, fixture_key=fixture_key,
            market_type=market_type, event_id=event_id,
            market_id=str(market.get("id", "")), slug=slug,
            title=question or event_title, url=url, note=note)
        if market_type == MATCH_WINNER:
            published_outcomes: list[str] = []
            for side in (market.get("marketSides") or []):
                name = _published_team_name(side)
                if not name and isinstance(side, dict):
                    name = str(side.get("description") or "").strip()
                if not name:
                    continue
                lowered = name.lower()
                canonical_name = "draw" if lowered in {"draw", "tie", "no winner"} else name
                if canonical_name != "draw":
                    canonical_name, _ = canonical_team_name(
                        canonical_name, competition, event_date)
                if C_norm := norm_team(canonical_name):
                    if C_norm not in {norm_team(x) for x in published_outcomes}:
                        published_outcomes.append(canonical_name)
            existing_keys = {norm_team(x) for x in published_outcomes}
            for canonical_name in canonical_names:
                if norm_team(canonical_name) not in existing_keys:
                    published_outcomes.append(canonical_name)
                    existing_keys.add(norm_team(canonical_name))
            team_keys = {norm_team(x) for x in canonical_names}
            outcome_keys = {norm_team(x) for x in published_outcomes}
            if not team_keys.issubset(outcome_keys):
                warnings.append(
                    f"COVERAGE_INCOMPLETE {venue}: match-winner sides omit a fixture team for {slug[:50]}")
            for name in published_outcomes:
                out.append(Market(**common, outcome=name, raw_outcome=name))
        else:
            outcomes = _j(market.get("outcomes")) or []
            if isinstance(outcomes, list) and outcomes:
                for outcome in outcomes:
                    out.append(Market(**common, outcome=str(outcome),
                                      raw_outcome=str(outcome)))
            else:
                out.append(Market(**common, outcome="", raw_outcome=""))
    if skipped:
        warnings.append(f"{venue}: {skipped} non-cricket fallback-search markets skipped")
    return list(fixtures.values()), out, warnings
