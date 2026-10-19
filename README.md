# Cricket Exchange Matching

This project matches live cricket match-winner outcomes across Kalshi, Polymarket International, and Polymarket US.

## Run

Open this folder in VS Code or Terminal and run:

```bash
python3 run.py
```

No API keys, package installation, or virtual environment are required. It uses Python 3.9 or newer and the standard library only. Polymarket International is collected from its public Gamma API, with the official public CLOB market feed as a fallback if Gamma cannot be reached.

Each run collects the three venues, publishes only confirmed mappings, audits the accepted result, creates a fresh report, and normally opens it automatically:

```text
output/CricketMatchingReport.html
```

The report shows its collection time and the coverage status for each venue. If a venue is incomplete or a possible match needs review, that is shown explicitly and no absence or mapping is forced. A genuinely unsafe accepted result blocks publication, while any previous valid report remains untouched.

Venue links open the shared event page; the mapped team outcome is identified by its row and venue market ID.

Each confirmed venue outcome includes its current bid and ask, displayed in cents per $1 payout. The report highlights the best bid and ask and flags a crossed outcome when a bid is strictly above an ask, either within one venue or across venues. Equal prices are not crossed. Use the crossed-only filter to inspect those rows. Crosses compare captured quotes before fees; prices can move between venue requests.

Quotes come from Kalshi's dollar bid/ask fields, Polymarket International's outcome-token order books, and Polymarket US's public BBO. Opposite outcomes use the opposite side of the book (`bid = 1 - other ask`, `ask = 1 - other bid`) only when the published outcome mapping establishes the relationship. Missing sides stay missing. Last trades and outcome display prices are never substituted for bid/ask quotes.

`output/data/market_map.csv` includes venue prices and crossed flags in probability units from 0 to 1. `output/data/quotes.json` preserves the normalized quotes, sources, and observation times for audited replay. A quote request failure leaves that price unavailable and keeps the confirmed fixture mapping intact.

Use `python3 run.py --no-open` to generate the report without opening a browser.

If a run is blocked, its specific failures appear in the terminal and in `.cricket_matching_build/data/audit_report.txt`. After fixing a parser or matching issue, replay those saved records without fetching again:

```bash
python3 run.py --no-open --snapshot-dir=.cricket_matching_build/data
```

Replay reports retain the original collection time and identify themselves as saved data. Missing or invalid snapshot files cannot count as complete venue coverage.

Publication requires complete collection and parsing for at least two venues and all critical audit checks to pass. Unsupported market types keep the affected venue's coverage incomplete. A draw outcome offered by only one venue stays in the unmatched data; it does not block valid mappings for the teams.

Run the offline regression tests with `python3 -m unittest discover -v`.


The report compares currently open markets. If a venue completed successfully but a fixture is no longer present there, the cell is shown as `Not currently open`; this does not make venue coverage incomplete.
# Cricket-Mapper
# Cricket-Mapper
