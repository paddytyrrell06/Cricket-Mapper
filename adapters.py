"""Discovery scopes grouped by terminal domain; shared collect/parse contract.

New domains register their own identity/settlement parsers here rather than
teaching the UI or quote transport about a particular sport or event class.
"""
import niches
import sports

ADAPTERS = dict(sports.ADAPTERS)
ADAPTERS["esports"] = sports.SportAdapter(
    "esports", "Esports", ("kalshi", "polymarket_intl"),
    niches.collect_esports, niches.parse_esports)
DOMAINS = {"sports": tuple(sports.ADAPTERS), "esports": ("esports",),
           "finance": (), "tech": ()}


def domain_for_scope(scope: str) -> str:
    return next((domain for domain, scopes in DOMAINS.items() if scope in scopes), "other")
