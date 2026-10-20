# Native sports coverage

The terminal discovers full-game or full-match winners from the public venue
catalogs. Kalshi series tickers and Polymarket Gamma series IDs below were
checked against [Kalshi's Sports catalog](https://external-api.kalshi.com/trade-api/v2/series?category=Sports)
and [Gamma's sports metadata](https://gamma-api.polymarket.com/sports) on
20 October 2026. A listed feed can be empty outside its season. Listings do not
establish equal settlement terms or executable liquidity.

| Sport | Competition | Kalshi series | Gamma series |
| --- | --- | --- | --- |
| Soccer | Premier League | KXEPLGAME | 10188 |
| Soccer | LaLiga | KXLALIGAGAME | 10193 |
| Soccer | Bundesliga | KXBUNDESLIGAGAME | 10194 |
| Soccer | Serie A | KXSERIEAGAME | 10203 |
| Soccer | Ligue 1 | KXLIGUE1GAME | 10195 |
| Soccer | Champions League | KXUCLGAME | 10204 |
| Soccer | MLS | KXMLSGAME | 10189 |
| Soccer | Europa League | KXUELGAME | 10209 |
| Soccer | Conference League | KXUECLGAME | 10437 |
| Soccer | EFL Championship | KXEFLCHAMPIONSHIPGAME | 10355 |
| Soccer | Eredivisie | KXEREDIVISIEGAME | 10286 |
| Soccer | Primeira Liga | KXLIGAPORTUGALGAME | 10330 |
| Soccer | Scottish Premiership | KXSCOTTISHPREMGAME | 10674 |
| Soccer | Liga MX | KXLIGAMXGAME | 10290 |
| Soccer | 2. Bundesliga | KXBUNDESLIGA2GAME | 10670 |
| Soccer | A-League Men | KXALEAGUEGAME | 10438 |
| Soccer | USL Championship | KXUSLGAME | 12318 |
| Basketball | NBA | KXNBAGAME | 10345 |
| Basketball | WNBA | KXWNBAGAME | 10105 |
| Basketball | Men's college basketball | KXNCAAMBGAME | 10470 |
| Basketball | Women's college basketball | KXNCAAWBGAME | 10471 |
| American football | NFL | KXNFLGAME | 12185 |
| American football | College football | KXNCAAFGAME | 12756 |
| Hockey | NHL | KXNHLGAME | 10346 |
| Baseball | MLB | KXMLBGAME | 3 |
| Tennis | ATP singles | KXATPMATCH | 10365 |
| Tennis | WTA singles | KXWTAMATCH | 10366 |
| MMA | UFC | KXUFCFIGHT | 38 |

Cricket retains its dedicated native collectors and competition catalog in
`canonical.py` and `collectors.py`.

Discovery uses at most three worker threads per selected sport, shared across
its leagues and venues. Soccer selection is available through
`sports._collect_soccer(competitions=["epl", "uel", "col"])`. Pagination follows
all native records, including pages containing only props, and reports failures
separately by league and venue. A completed scan is not a live price feed.

Event and market IDs, token IDs, original outcome order, native team metadata,
published start-time basis, and resolution rules are retained for the quote and
future subscription paths. Discovery can run less frequently than quotes.

Participant matching accepts exact league-scoped names and published aliases.
The WNBA registry additionally records the native code aliases verified against
both venues' published teams. College identity uses the published school name,
so shared mascots such as “Huskies” cannot merge different schools. Unverified or
ambiguous aliases remain excluded with a coverage warning; no fuzzy identity
guessing is used. Tennis and MMA use complete participant names and distinct
tour/league keys.

Soccer preserves the native regulation/full-match scope and requires compatible
evidenced result periods across venues. Tennis preserves different walkover and retirement provisions, and
MMA requires evidenced draw and no-contest payouts. The default comparison is
conditional on normal completion; strict settlement comparison still requires
all contingency terms to agree. Sports expansion does not enable order routing.
