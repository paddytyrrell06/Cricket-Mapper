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


The report compares currently open markets. If a venue completed successfully but a fixture is no longer present there, the cell is shown as `Not currently open`; this does not make venue coverage incomplete.
# Cricket-Mapper
# Cricket-Mapper
