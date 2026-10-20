import tempfile
import unittest
import csv
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import presentation as P
import canonical as C
import matching as M
import settlement as S


VENUES = ["kalshi", "polymarket_intl", "polymarket_us"]


def known_profile(sport="cricket", draw="0"):
    states = S.NBA_STATES if sport == "basketball" else S.CRICKET_STATES
    policies = S.NBA_POLICIES if sport == "basketball" else S.CRICKET_POLICIES
    payouts = {state: "0" for state in states}
    payouts.update(win="1", loss="0")
    if "draw" in payouts:
        payouts["draw"] = draw
    policy_values = {
        "period": "full_match",
        "overtime": "included",
        "shootout": "included",
        "extra_innings": "included",
        "tiebreakers": "included_if_played",
        "dls": "official_result",
        "postponement": "wait_until_completed",
    }
    return S.make_profile(sport=sport, target_kind="team", payouts=payouts,
                          policies={policy: policy_values[policy] for policy in policies},
                          evidence=[{"field": "test", "text": "Explicit synthetic settlement rules"}])


class WideRowsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SimpleNamespace(
            canonical_fixture_id="2026-10-19:W:T20:a-b",
            key=SimpleNamespace(date="2026-10-19", gender="W", fmt="T20"),
            start_time="09:00", competition="Example cup", team_a="A", team_b="B",
        )
        self.match = SimpleNamespace(
            fixture=self.fixture, market_type="match_winner", outcome="a",
            venue_1="kalshi", venue_2="polymarket_intl",
            m1=self.market("kalshi"), m2=self.market("polymarket_intl"),
        )
        self.coverage = dict.fromkeys(VENUES, True)

    @staticmethod
    def market(venue):
        return SimpleNamespace(
            event_id=f"{venue}-event", market_id=f"{venue}-market",
            venue_fixture_id=f"{venue}-fixture",
            slug=f"{venue}-slug", title="A to win", url=f"https://{venue}.example/event",
            extra={"settlement": known_profile()},
        )

    def test_complete_collection_without_candidate_can_assert_absence(self):
        rows = P.build_wide_rows([self.match], [], self.coverage, VENUES)
        self.assertEqual(rows[0]["polymarket_us_status"], "Not currently open")

    def test_fixture_review_prevents_absence_without_matching_market_outcome(self):
        fixture_review = SimpleNamespace(
            canonical_fixture_id=self.fixture.canonical_fixture_id,
            by_venue={venue: SimpleNamespace(venue_fixture_id=f"{venue}-fixture")
                      for venue in ("kalshi", "polymarket_us")},
        )
        rows = P.build_wide_rows(
            [self.match], [], self.coverage, VENUES, [fixture_review])
        self.assertEqual(rows[0]["polymarket_us_status"], "Needs review")
        self.assertEqual(rows[0]["kalshi_status"], "Available")
        self.assertEqual(rows[0]["polymarket_intl_status"], "Available")
        self.assertEqual(rows[0]["venues_available"], "2")

    def test_review_for_different_fixture_does_not_change_absence(self):
        fixture_review = SimpleNamespace(
            canonical_fixture_id="other-fixture",
            by_venue={venue: SimpleNamespace(venue_fixture_id=f"other-{venue}-fixture")
                      for venue in ("kalshi", "polymarket_us")},
        )
        rows = P.build_wide_rows(
            [self.match], [], self.coverage, VENUES, [fixture_review])
        self.assertEqual(rows[0]["polymarket_us_status"], "Not currently open")

    def test_review_between_two_accepted_fixtures_marks_both_canonical_groups(self):
        fixtures = []
        for venue, fixture_id, day, opponent in [
            ("kalshi", "k", "22", "Zambia"),
            ("polymarket_intl", "i", "21", "Gambia"),
            ("polymarket_us", "u1", "22", "Zambia"),
            ("polymarket_us", "u2", "21", "Gambia"),
        ]:
            fixtures.append(C.Fixture(
                C.FixtureKey(f"2026-10-{day}", frozenset({"nigeria", opponent.lower()}),
                             "M", "T20"),
                venue, fixture_id, competition="International", start_time="08:00",
                team_a="Nigeria", team_b=opponent,
            ))
        markets = [C.Market(
            source.venue, source.venue_fixture_id, source.key, C.MATCH_WINNER, outcome,
            market_id=f"{source.venue_fixture_id}:{outcome}",
            event_id=source.venue_fixture_id, title=f"{outcome} wins",
            extra={"settlement": known_profile()},
        ) for source in fixtures for outcome in (source.team_a, source.team_b)]
        accepted, review, _ = M.match_fixtures(fixtures)
        market_accepted, market_review, _ = M.match_markets(accepted + review, markets)

        rows = P.build_wide_rows(
            market_accepted, market_review, self.coverage, VENUES, fixture_review=review)

        self.assertEqual(len(accepted), 2)
        self.assertTrue(review)
        self.assertEqual(len(rows), 4)
        for row in rows:
            with self.subTest(date=row["fixture_date"], outcome=row["canonical_outcome"]):
                if row["fixture_date"] == "2026-10-22":
                    self.assertEqual(row["kalshi_status"], "Available")
                    self.assertEqual(row["polymarket_intl_status"], "Needs review")
                    self.assertEqual(row["polymarket_us_event_id"], "u1")
                else:
                    self.assertEqual(row["kalshi_status"], "Needs review")
                    self.assertEqual(row["polymarket_intl_status"], "Available")
                    self.assertEqual(row["polymarket_us_event_id"], "u2")
                self.assertEqual(row["polymarket_us_status"], "Available")
                self.assertEqual(row["venues_available"], "2")


class ReportModeTests(unittest.TestCase):
    source_time = datetime(2026, 10, 18, 5, 0, tzinfo=timezone.utc)
    replay_time = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)

    def render(self, quality):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            with patch("presentation.datetime", wraps=datetime) as clock:
                clock.now.return_value = self.replay_time
                P.write_html(
                    path, [], VENUES, dict.fromkeys(VENUES, True),
                    self.source_time, quality,
                )
            return path.read_text(encoding="utf-8")

    def test_live_report_uses_collection_timestamp_for_freshness(self):
        document = self.render({"Run type": "LIVE"})
        self.assertIn("LIVE API REPORT", document)
        self.assertIn("Live collection completed", document)
        self.assertIn(f"new Date('{self.source_time.isoformat()}')", document)

    def test_replay_keeps_source_timestamp_separate_from_generation_age(self):
        document = self.render({"Run type": "REPLAY"})
        self.assertIn("SNAPSHOT REPLAY", document)
        self.assertIn("Market Terminal · Snapshot Replay", document)
        self.assertIn("Snapshot collected", document)
        self.assertIn("18 Oct 2026, 05:00:00 UTC", document)
        self.assertIn(f"new Date('{self.replay_time.isoformat()}')", document)
        self.assertIn("const ageLabel='Replay generated'", document)
        self.assertIn("Venue availability reflects the saved collection.", document)
        self.assertNotIn("LIVE API REPORT", document)
        self.assertNotIn("Live collection completed", document)
        self.assertNotIn("regenerated from live venue APIs", document)

    def test_replay_without_source_time_does_not_claim_collection_timestamp(self):
        document = self.render({
            "Run type": "REPLAY", "Source collection": "Unknown",
        })
        self.assertIn("Source collection time unavailable", document)
        self.assertNotIn("18 Oct 2026, 05:00:00 UTC", document)
        self.assertIn(f"new Date('{self.replay_time.isoformat()}')", document)

    def test_refreshed_live_report_ages_quotes_without_relabeling_discovery(self):
        document = self.render({"Run type": "LIVE", "Quotes checked at": self.replay_time.isoformat()})
        self.assertIn("Quotes checked 19 Oct 2026, 08:00:00 UTC", document)
        self.assertIn(f"new Date('{self.replay_time.isoformat()}')", document)
        self.assertIn('title="Source discovery completed 18 Oct 2026, 05:00:00 UTC"', document)

    def test_replay_ignores_live_quote_age_metadata(self):
        document = self.render({"Run type": "REPLAY", "Quotes checked at": self.replay_time.isoformat()})
        self.assertIn("Snapshot collected 18 Oct 2026, 05:00:00 UTC", document)
        self.assertIn("const ageLabel='Replay generated'", document)
        self.assertNotIn("Quotes checked", document)

    def test_invalid_quote_check_time_preserves_collection_age(self):
        for stamp in ("not-a-date", "2026-10-19T08:00:00"):
            with self.subTest(stamp=stamp):
                document = self.render({"Run type": "LIVE", "Quotes checked at": stamp})
                self.assertIn("Live collection completed", document)
                self.assertIn(f"new Date('{self.source_time.isoformat()}')", document)


class ReportRefreshTests(unittest.TestCase):
    source_time = datetime(2026, 10, 18, 5, 0, tzinfo=timezone.utc)

    def render(self, path):
        P.write_html(path, [], VENUES, dict.fromkeys(VENUES, True), self.source_time)
        return path.read_text(encoding="utf-8"), path.with_suffix(".version.js").read_text(encoding="utf-8")

    def test_every_publication_has_a_matching_fresh_version_even_with_same_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            previous = None
            for _ in range(2):
                document, marker = self.render(path)
                version = re.search(r'const reportVersion="([0-9a-f]{32})"', document).group(1)
                self.assertEqual(marker, f'window.marketTerminalReportVersion("{version}");\n')
                self.assertNotEqual(version, previous)
                previous = version
            self.assertEqual({entry.name for entry in Path(directory).iterdir()}, {"report.html", "report.version.js"})

    def test_failed_html_publication_leaves_previous_report_and_marker_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            previous_document, previous_marker = self.render(path)
            replace = Path.replace

            def fail_html_replace(source, destination):
                if destination == path:
                    raise OSError("Synthetic publication failure")
                return replace(source, destination)

            with patch.object(Path, "replace", fail_html_replace):
                with self.assertRaisesRegex(OSError, "publication failure"):
                    self.render(path)
            self.assertEqual(path.read_text(encoding="utf-8"), previous_document)
            self.assertEqual(path.with_suffix(".version.js").read_text(encoding="utf-8"), previous_marker)
            self.assertEqual(len(list(Path(directory).iterdir())), 2)

    def test_version_filename_cannot_inject_html_into_the_refresh_script(self):
        with tempfile.TemporaryDirectory() as directory:
            document, _ = self.render(Path(directory) / 'report<script>#?.html')
            refresh = re.search(r'<script id="reportRefresh">(.*?)</script>', document, re.S).group(1)
            self.assertIn(r'report\u003cscript>#?.version.js', refresh)
            self.assertNotIn("<script>", refresh)
            self.assertIn("encodeURIComponent(reportVersionFile)", refresh)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to exercise browser JavaScript")
    def test_browser_refresh_preserves_view_and_recovers_from_publication_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            document, marker = self.render(Path(directory) / 'report<svg>#?.html')
        refresh = re.search(r'<script id="reportRefresh">(.*?)</script>', document, re.S).group(1)
        harness = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
function browser(saved,storageBlocked=false){
    const storage=new Map(),timers=[],timeouts=new Map(),scripts=[],events={},sortDirection={};
    const key='market-terminal-view:file:///tmp/Market%20Terminal/report.html';
    if(saved!==undefined)storage.set(key,typeof saved==='string'?saved:JSON.stringify(saved));
    let reloads=0,removed=0,timeoutId=0;
    const context=vm.createContext({
        URL,location:{protocol:'file:',host:'',pathname:'/tmp/Market%20Terminal/report.html',
            href:'file:///tmp/Market%20Terminal/report.html',reload(){reloads++;}},
        window:{addEventListener(name,fn){events[name]=fn;}},
        sessionStorage:{getItem(k){if(storageBlocked)throw Error('Storage blocked');return storage.get(k)||null;},
            setItem(k,v){if(storageBlocked)throw Error('Storage blocked');storage.set(k,v);}},
        activeTab:'all',page:0,direction:1,search:{value:''},reviewToggle:{checked:true},strictToggle:{checked:false},
        sortBy:{value:'date'},pageSize:{value:'100'},tabs:[{dataset:{tab:'all'}},{dataset:{tab:'soccer'}}],
        document:{getElementById(){return sortDirection;},createElement(){return {remove(){removed++;}};},
            head:{appendChild(script){scripts.push(script);}}},
        selectTab(tab){context.activeTab=tab.dataset.tab;context.page=0;},
        apply(){context.page=Math.min(context.page,4);},
        setInterval(fn,ms){timers.push({fn,ms});return timers.length;},
        setTimeout(fn,ms){timeouts.set(++timeoutId,{fn,ms});return timeoutId;},
        clearTimeout(id){timeouts.delete(id);},
    });
    vm.runInContext(REFRESH,context);
    return {context,scripts,events,storage,timeouts,sortDirection,poll:timers.find(t=>t.ms===2000).fn,
        state:()=>JSON.parse(storage.get(key)),reloads:()=>reloads,removed:()=>removed};
}
const view={tab:'soccer',search:'Arsenal',reviews:false,strict:true,sort:'edge',direction:-1,pageSize:'25',page:3};
const b=browser(view),c=b.context;
assert.equal(c.activeTab,'soccer');assert.equal(c.search.value,'Arsenal');assert.equal(c.page,3);
assert.equal(c.reviewToggle.checked,false);assert.equal(c.strictToggle.checked,true);
assert.equal(c.sortBy.value,'edge');assert.equal(c.direction,-1);assert.equal(c.pageSize.value,'25');
assert.equal(b.sortDirection.textContent,'↓');
b.poll();assert.equal(b.scripts.length,1);
assert.equal(new URL(b.scripts[0].src).pathname,'/tmp/Market%20Terminal/report%3Csvg%3E%23%3F.version.js');
assert.ok(new URL(b.scripts[0].src).searchParams.has('check'));
b.poll();assert.equal(b.scripts.length,1); // Only one version check can be in flight.
vm.runInContext(MARKER,c);assert.equal(b.reloads(),0); // Same published report.
b.scripts[0].onload();assert.equal(b.removed(),1);assert.equal(b.timeouts.size,0);
b.poll();assert.equal(b.scripts.length,2);assert.notEqual(b.scripts[0].src,b.scripts[1].src);
b.scripts[1].onerror();assert.equal(b.reloads(),0);assert.equal(c.search.value,'Arsenal');
b.poll();assert.equal(b.scripts.length,3);
[...b.timeouts.values()][0].fn();assert.equal(b.timeouts.size,0); // Stalled loads also recover.
b.poll();assert.equal(b.scripts.length,4);
c.search.value='Liverpool';c.page=2;
c.window.marketTerminalReportVersion('next-publication');assert.equal(b.reloads(),1);
assert.deepEqual(b.state(),{...view,search:'Liverpool',page:2});
assert.equal(Object.hasOwn(b.state(),'records'),false);
c.window.marketTerminalReportVersion('next-publication');b.poll();assert.equal(b.reloads(),1);assert.equal(b.scripts.length,4);
const restored=browser(b.state());assert.equal(restored.context.search.value,'Liverpool');assert.equal(restored.context.page,2);
const removedTab=browser({...view,tab:'removed',page:999});assert.equal(removedTab.context.activeTab,'all');assert.equal(removedTab.context.page,4);
const malformed=browser('invalid-json');assert.equal(malformed.context.activeTab,'all');assert.equal(malformed.context.search.value,'');
const blocked=browser(undefined,true);blocked.poll();blocked.context.window.marketTerminalReportVersion('new');assert.equal(blocked.reloads(),1);
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", "const REFRESH=" + json.dumps(refresh) + ";const MARKER=" + json.dumps(marker) + ";\n" + harness],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ServiceRefreshTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required to exercise browser JavaScript")
    def test_http_reload_refreshes_once_and_publication_reload_does_not_loop(self):
        harness = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function browser({protocol='http:',navigation='reload',storage=new Map(),blocked=false}={}){
    const requests=[],timers=[],events={},button={hidden:true,disabled:false,addEventListener(name,fn){events[name]=fn;}},label={hidden:true};
    let views=0,checks=0;
    const context=vm.createContext({location:{protocol,pathname:'/report.html'},window:{},
        document:{getElementById(id){return id==='refreshPrices'?button:label;}},
        performance:{getEntriesByType(){return [{type:navigation}];}},
        sessionStorage:{getItem(key){if(blocked)throw Error('Blocked');return storage.get(key)||null;},
            setItem(key,value){if(blocked)throw Error('Blocked');storage.set(key,value);},
            removeItem(key){if(blocked)throw Error('Blocked');storage.delete(key);}},
        saveView(){views++;},checkForNewReport(){checks++;},
        setInterval(fn,ms){timers.push({fn,ms});},
        async fetch(url,options={}){requests.push({url,options});return {ok:true,async json(){return {
            state:url==='/api/refresh'?'running':'idle',running:url==='/api/refresh',revision:0,error:null};}};},
    });
    vm.runInContext(SERVICE,context);
    return {context,requests,timers,events,button,label,storage,views:()=>views,checks:()=>checks,
        posts:()=>requests.filter(request=>request.url==='/api/refresh')};
}
(async()=>{
    const b=browser();await settle();assert.equal(b.posts().length,1);assert.equal(b.button.hidden,false);
    assert.equal(b.posts()[0].options.method,'POST');
    assert.equal(b.posts()[0].options.headers['X-Terminal-Request'],'refresh');
    assert.equal(b.posts()[0].options.body,'{}');assert.ok(b.views()>0);
    b.context.window.marketTerminalBeforeReportReload();
    const publication=browser({storage:b.storage});await settle();assert.equal(publication.posts().length,0);
    const manual=browser({storage:b.storage});await settle();assert.equal(manual.posts().length,1);
    const initial=browser({navigation:'navigate'});await settle();assert.equal(initial.posts().length,0);
    initial.events.click();await settle();assert.equal(initial.posts().length,1);
    const file=browser({protocol:'file:'});await settle();assert.equal(file.requests.length,0);assert.equal(file.button.hidden,true);
    const blocked=browser({blocked:true});await settle();assert.equal(blocked.posts().length,0);
    blocked.events.click();await settle();assert.equal(blocked.posts().length,1);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", "const SERVICE=" + json.dumps(P._terminal_service_script()) + ";\n" + harness],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class QuotePresentationTests(unittest.TestCase):
    as_of = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)

    def setUp(self):
        fixture = SimpleNamespace(
            canonical_fixture_id="2026-10-19:M:T20:a-b",
            key=SimpleNamespace(date="2026-10-19", gender="M", fmt="T20"),
            start_time="09:00", competition="Example cup", team_a="A", team_b="B",
        )
        self.left = WideRowsTests.market("kalshi")
        self.right = WideRowsTests.market("polymarket_intl")
        self.match = SimpleNamespace(
            fixture=fixture, market_type="match_winner", outcome="a",
            venue_1="kalshi", venue_2="polymarket_intl", m1=self.left, m2=self.right,
        )
        self.coverage = dict.fromkeys(VENUES, True)
        self.left.quote_time = self.right.quote_time = self.as_of.isoformat()

    def row(self):
        return P.build_wide_rows([self.match], [], self.coverage, VENUES, as_of=self.as_of)[0]

    def test_across_venue_cross_uses_exact_decimal_gap(self):
        self.left.bid, self.left.ask = "0.65", "0.70"
        self.right.bid, self.right.ask = "0.60", "0.62"
        row = self.row()
        self.assertEqual(row["best_bid"], "0.65")
        self.assertEqual(row["best_ask"], "0.62")
        self.assertEqual(row["best_bid_venue"], "kalshi")
        self.assertEqual(row["best_ask_venue"], "polymarket_intl")
        self.assertEqual(row["best_spread"], "-0.03")
        self.assertEqual(row["cross_gap"], "0.03")
        self.assertEqual(row["cross_kind"], "across venues")
        self.assertEqual(row["crossed"], "true")
        self.assertEqual(row["kalshi_crossed"], "false")

    def known_fees(self):
        self.left.extra.update(fee_type="quadratic", fee_multiplier="1")
        self.right.extra.update(feesEnabled=False)

    def test_net_edge_deducts_taker_fees_on_both_legs_and_exports_sizes(self):
        self.known_fees()
        self.left.bid, self.left.ask = "0.65", "0.70"
        self.right.bid, self.right.ask = "0.60", "0.62"
        self.left.extra.update(quote_bid_size="12.5", quote_ask_size="40")
        self.right.extra.update(quote_bid_size="19", quote_ask_size="8.7")
        row = self.row()
        self.assertEqual(row["cross_gap"], "0.03")
        self.assertEqual(row["net_gap"], "0.01")
        self.assertEqual(row["net_sell_fee"], "0.02")
        self.assertEqual(row["net_buy_fee"], "0")
        self.assertEqual(row["net_crossed"], "true")
        self.assertEqual(row["net_quantity"], "1")
        self.assertEqual(row["net_top_size"], "8.7")
        self.assertEqual(row["net_top_quantity"], "8")
        self.assertEqual(row["kalshi_bid_size"], "12.5")
        self.assertEqual(row["polymarket_intl_ask_size"], "8.7")
        details = P._details(row, VENUES)
        self.assertIn("Taker edge · 1 contract per leg", details)
        self.assertIn("Sell taker fee</dt><dd>2¢", details)
        self.assertIn("Net / contract</dt><dd>1¢", details)
        self.assertIn("×12.5 ct", details)
        self.assertIn("below venue minimum order sizes", details)
        self.assertIn("×12.5", P._venue_cell(row, "kalshi"))

    def test_gross_cross_can_be_negative_after_fees(self):
        self.known_fees()
        self.left.bid, self.right.ask = "0.65", "0.64"
        row = self.row()
        self.assertEqual(row["crossed"], "true")
        self.assertEqual(row["net_cross_eligible"], "true")
        self.assertEqual(row["net_crossed"], "false")
        self.assertEqual(row["net_gap"], "-0.01")
        self.assertIn('class="edge-negative"', P._net_edge_cell(row, 0))
        self.assertNotIn('class="edge-positive"', P._net_edge_cell(row, 0))

    def test_unknown_fees_keep_gross_diagnostics_without_inventing_net(self):
        self.left.bid, self.right.ask = "0.65", "0.60"
        row = self.row()
        self.assertEqual(row["crossed"], "true")
        self.assertEqual(row["net_crossed"], "false")
        self.assertEqual(row["net_cross_eligible"], "false")
        self.assertEqual(row["net_gap"], "")
        self.assertEqual(row["net_fee_status"], "unavailable")
        self.assertIn("Fee ?", P._net_edge_cell(row, 0))

    def test_sizes_require_a_corresponding_price_and_valid_quantity(self):
        self.left.bid = "0.65"
        self.left.extra.update(quote_bid_size="0", quote_ask_size="12")
        row = self.row()
        self.assertEqual(row["kalshi_bid_size"], "0")
        self.assertEqual(row["kalshi_ask_size"], "")
        for invalid in ("NaN", "-1", True, "invalid"):
            self.left.extra["quote_bid_size"] = invalid
            self.assertEqual(self.row()["kalshi_bid_size"], "")

    def test_net_estimates_require_the_same_settlement_and_freshness_checks(self):
        self.known_fees()
        self.left.bid, self.right.ask = "0.65", "0.60"
        self.left.quote_time = (self.as_of - timedelta(minutes=6)).isoformat()
        stale = self.row()
        self.assertEqual(stale["net_crossed"], "false")
        self.assertEqual(stale["net_gap"], "")
        self.left.quote_time = self.as_of.isoformat()
        self.right.extra["settlement"] = known_profile(draw="0.5")
        incompatible = self.row()
        self.assertEqual(incompatible["net_crossed"], "false")
        self.assertEqual(incompatible["net_gap"], "")

    def test_locked_market_is_not_crossed(self):
        self.left.bid, self.left.ask = "0.5", "0.5"
        self.right.bid, self.right.ask = "0.49", "0.51"
        row = self.row()
        self.assertEqual(row["crossed"], "false")
        self.assertEqual(row["kalshi_crossed"], "false")
        self.assertEqual(row["best_spread"], "0")
        self.assertEqual(row["cross_gap"], "")
        self.assertEqual(row["cross_kind"], "")

    def test_local_cross_and_across_cross_are_flagged_separately(self):
        self.left.bid, self.left.ask = "0.65", "0.60"
        self.right.bid, self.right.ask = "0.61", "0.62"
        row = self.row()
        self.assertEqual(row["kalshi_crossed"], "true")
        self.assertEqual(row["polymarket_intl_crossed"], "false")
        self.assertEqual(row["observed_cross_kind"], "within venue and across venues")
        self.assertEqual(row["observed_cross_gap"], "0.05")
        self.assertEqual(row["quote_check"], "invalid_book")
        self.assertEqual(row["cross_eligible"], "false")
        self.assertEqual(row["cross_kind"], "")
        self.assertEqual(row["cross_gap"], "")

    def test_locally_crossed_book_cannot_emit_a_positive_net_signal(self):
        self.known_fees()
        self.left.bid, self.left.ask = "0.80", "0.60"
        self.right.bid, self.right.ask = "0.65", "0.70"
        row = self.row()
        self.assertEqual(row["observed_crossed"], "true")
        self.assertEqual(row["quote_check"], "invalid_book")
        self.assertEqual(row["crossed"], "false")
        self.assertEqual(row["net_crossed"], "false")
        self.assertEqual(row["net_gap"], "")
        self.assertIn("Invalid book", P._net_edge_cell(row, 0))
        self.right.quote_time = (self.as_of - timedelta(minutes=6)).isoformat()
        self.assertEqual(self.row()["quote_check"], "invalid_book")

    def test_invalid_third_book_does_not_block_two_healthy_venues(self):
        self.known_fees()
        self.left.bid, self.left.ask = "0.70", "0.75"
        self.right.bid, self.right.ask = "0.61", "0.62"
        row = self.row()
        row.update(polymarket_us_bid="0.80", polymarket_us_ask="0.60",
                   polymarket_us_quote_time=self.as_of.isoformat())
        eligibility = P._eligible_quotes(row, VENUES, self.as_of)
        self.assertEqual(eligibility["quote_check"], "fresh")
        self.assertEqual(eligibility["crossed"], "true")
        self.assertEqual(eligibility["eligible_bid_venue"], "kalshi")
        self.assertEqual(eligibility["eligible_ask_venue"], "polymarket_intl")
        self.assertEqual(eligibility["cross_gap"], "0.08")
        self.assertEqual(eligibility["net_gap"], "0.06")

    def test_local_cross_without_another_quoted_venue(self):
        self.left.bid, self.left.ask = "0.3", "0.2"
        row = self.row()
        self.assertEqual(row["observed_cross_kind"], "within venue")
        self.assertEqual(row["observed_cross_gap"], "0.1")
        self.assertEqual(row["crossed"], "false")
        self.assertEqual(row["cross_gap"], "")

    def test_one_sided_books_can_cross_without_inventing_missing_sides(self):
        self.left.bid = "0.3"
        self.right.ask = "0.2"
        row = self.row()
        self.assertEqual(row["kalshi_ask"], "")
        self.assertEqual(row["polymarket_intl_bid"], "")
        self.assertEqual(row["kalshi_quote_status"], "one-sided")
        self.assertEqual(row["polymarket_intl_quote_status"], "one-sided")
        self.assertEqual(row["cross_kind"], "across venues")
        self.assertEqual(row["cross_gap"], "0.1")

    def test_missing_quote_sides_do_not_become_zero(self):
        self.left.bid = "0.3"
        row = self.row()
        self.assertEqual(row["best_bid"], "0.3")
        for field in ("best_ask", "best_spread", "cross_gap", "cross_kind"):
            self.assertEqual(row[field], "")
        self.assertEqual(row["crossed"], "false")
        self.assertEqual(row["polymarket_intl_quote_status"], "unavailable")
        self.assertEqual(row["polymarket_us_bid"], "")

    def test_nonfinite_invalid_and_out_of_range_quotes_are_excluded(self):
        for value in ("NaN", "Infinity", "garbage", "-0.01", "1.01", True):
            with self.subTest(value=value):
                self.left.bid, self.left.ask = value, "0.2"
                row = self.row()
                self.assertEqual(row["kalshi_bid"], "")
                self.assertEqual(row["kalshi_quote_status"], "one-sided")
                self.assertEqual(row["best_bid"], "")
                self.assertEqual(row["crossed"], "false")

    def test_zero_and_one_remain_real_quote_values(self):
        self.left.bid, self.left.ask = "0", "1"
        row = self.row()
        self.assertEqual(row["best_bid"], "0")
        self.assertEqual(row["best_ask"], "1")
        self.assertEqual(row["best_spread"], "1")
        self.assertEqual(P._cents(row["best_bid"]), "0¢")
        self.assertEqual(P._cents(row["best_ask"]), "100¢")

    def test_csv_retains_probability_quotes_and_quote_provenance(self):
        self.left.bid, self.left.ask = "0.1234", "0.6"
        self.left.quote_time = "2026-10-19T10:00:00Z"
        self.left.quote_source = "saved order book"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quotes.csv"
            P.write_wide_csv(path, [self.row()], VENUES)
            with path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
        self.assertEqual(row["kalshi_bid"], "0.1234")
        self.assertEqual(row["kalshi_quote_time"], self.left.quote_time)
        self.assertEqual(row["kalshi_quote_source"], self.left.quote_source)
        self.assertEqual(row["crossed"], "false")

    def test_dashboard_renders_cents_cross_filter_and_honest_quote_language(self):
        self.left.bid, self.left.ask = "0.65", "0.7"
        self.right.ask = "0.62"
        self.left.quote_source = "<unsafe>"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            P.write_html(path, [self.row()], VENUES, self.coverage,
                         datetime(2026, 10, 19, tzinfo=timezone.utc), {"Run type": "LIVE"})
            document = path.read_text(encoding="utf-8")
        self.assertIn("65¢", document)
        self.assertIn("62¢", document)
        self.assertIn('data-crossed="true"', document)
        self.assertIn('id="tab-crosses"', document)
        self.assertIn('id="tab-cricket"', document)
        self.assertIn('id="tab-basketball"', document)
        self.assertIn('id="detailsDialog"', document)
        self.assertIn("activeTab==='crosses'", document)
        self.assertIn("not a guarantee of profit", document)
        self.assertIn("check venue settlement rules", document)
        self.assertNotIn('class="cards"', document)
        self.assertIn("\\u0026lt;unsafe\\u0026gt;", document)
        self.assertNotIn("<unsafe>", document)

    @staticmethod
    def detail_quotes(details):
        return details.split(f'aria-label="Observed bid and ask">', 1)[1].split('<dl>', 1)[0]

    def test_details_quotes_use_normalized_prices_and_label_settlement_separately(self):
        self.left.bid, self.left.ask = "0.6525", "0.70"
        # Raw diagnostic values must never override the selected outcome quote.
        self.left.extra.update(raw_quote_bid="0", raw_quote_ask="1")
        details = P._details(self.row(), VENUES)
        quotes = self.detail_quotes(details)
        self.assertIn('class="bid" data-side="bid">65.25¢</strong>', quotes)
        self.assertIn('class="ask" data-side="ask">70¢</strong>', quotes)
        self.assertIn('class="quote-status">available</span>', quotes)
        self.assertNotIn(">0¢<", quotes)
        self.assertNotIn(">100¢<", quotes)
        self.assertIn('<details><summary>Settlement payouts at resolution</summary>', details)
        self.assertIn('<caption>Payout per $1 contract at resolution</caption>', details)
        self.assertIn('<td>Win</td><td>100¢</td>', details)
        self.assertIn('<td>Loss</td><td>0¢</td>', details)

    def test_details_keep_missing_side_unavailable_and_display_real_zero(self):
        for bid, ask, expected_bid, expected_ask, status in (
            (None, None, "—", "—", "unavailable"),
            ("0", None, "0¢", "—", "one-sided"),
            (None, "1", "—", "100¢", "one-sided"),
            ("0", "1", "0¢", "100¢", "available"),
        ):
            with self.subTest(bid=bid, ask=ask):
                self.left.bid, self.left.ask = bid, ask
                quotes = self.detail_quotes(P._details(self.row(), VENUES))
                self.assertIn(f'class="bid" data-side="bid">{expected_bid}</strong>', quotes)
                self.assertIn(f'class="ask" data-side="ask">{expected_ask}</strong>', quotes)
                self.assertIn(f'class="quote-status">{status}</span>', quotes)

    def test_popup_payload_and_table_quotes_agree_for_each_venue(self):
        self.crossing_quotes()
        self.left.market_id = 'market"<unsafe>'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.html"
            P.write_html(path, [self.row()], VENUES, self.coverage, self.as_of)
            document = path.read_text()
        payload = document.split('id="marketData">', 1)[1].split('</script>', 1)[0]
        record = json.loads(payload)[0]
        for venue, bid, ask in (("kalshi", "65¢", "70¢"), ("polymarket_intl", "60¢", "62¢")):
            section = record["details"].split(f'data-venue="{venue}"', 1)[1].split('<dl>', 1)[0]
            self.assertIn(f'data-side="bid">{bid}</strong>', section)
            self.assertIn(f'data-side="ask">{ask}</strong>', section)
            self.assertIn(f'data-side="bid">{bid}</strong>', record["html"])
            self.assertIn(f'data-side="ask">{ask}</strong>', record["html"])
        self.assertIn('data-market-id="market&quot;&lt;unsafe&gt;"', record["html"])
        self.assertIn('class="competition-cell" title="Example cup">Example cup</td>', record["html"])
        self.assertIn('class="competition-head">League</th>', document)

    def crossing_quotes(self):
        self.left.bid, self.left.ask = "0.65", "0.70"
        self.right.bid, self.right.ask = "0.60", "0.62"

    def test_half_dollar_draw_vs_zero_draw_is_never_a_cross(self):
        self.crossing_quotes()
        self.left.extra["settlement"] = known_profile(draw="0.5")
        self.right.extra["settlement"] = known_profile(draw="0")
        row = self.row()
        self.assertEqual(row["observed_crossed"], "true")
        self.assertEqual(row["comparison_status"], "rules_differ")
        self.assertIn("draw", row["comparison_reason"])
        self.assertEqual(row["cross_eligible"], "false")
        self.assertEqual(row["crossed"], "false")
        self.assertEqual(row["cross_gap"], "")
        self.assertIn("Draw: 50¢ / 0¢", P._edge_cell(row, 0))

    def test_missing_settlement_evidence_never_permits_cross(self):
        self.crossing_quotes()
        self.right.extra["settlement"] = {}
        row = self.row()
        self.assertEqual(row["comparison_status"], "rules_unknown")
        self.assertEqual(row["observed_crossed"], "true")
        self.assertEqual(row["crossed"], "false")

    def test_review_rows_remain_visible_with_raw_quotes_and_ineligible_cross(self):
        self.crossing_quotes()
        self.left.extra["settlement"] = known_profile(draw="0.5")
        self.match.reason = "Settlement draw payout differs"
        rows = P.build_wide_rows([], [self.match], self.coverage, VENUES,
                                 include_review=True, as_of=self.as_of)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mapping_status"], "review")
        self.assertEqual(rows[0]["comparison_status"], "rules_differ")
        self.assertEqual(rows[0]["kalshi_bid"], "0.65")
        self.assertEqual(rows[0]["polymarket_intl_ask"], "0.62")
        self.assertEqual(rows[0]["crossed"], "false")
        self.assertEqual(P.build_wide_rows([], [self.match], self.coverage, VENUES), [])

    def test_unresolved_fixture_reviews_with_equal_outcomes_are_visible(self):
        self.crossing_quotes()
        self.match.fixture.status = "review"
        self.match.reason = "Published start times differ"
        rows = P.build_wide_rows([], [self.match], self.coverage, VENUES,
                                 include_review=True, as_of=self.as_of)
        self.assertEqual(rows[0]["comparison_status"], "fixture_review")
        self.assertIn("Published start times differ", rows[0]["comparison_reason"])
        self.assertEqual(rows[0]["crossed"], "false")

    def test_stale_quotes_do_not_make_eligible_cross(self):
        self.crossing_quotes()
        old_time = self.as_of - timedelta(seconds=P.QUOTE_MAX_AGE_SECONDS + 1)
        self.left.quote_time = self.right.quote_time = old_time.isoformat()
        row = self.row()
        self.assertEqual(row["quote_check"], "stale")
        self.assertEqual(row["observed_crossed"], "true")
        self.assertEqual(row["crossed"], "false")

    def test_missing_and_naive_quote_times_are_unknown(self):
        self.crossing_quotes()
        for value in ("", "not-a-date", "2026-10-19T10:00:00"):
            with self.subTest(value=value):
                self.left.quote_time = value
                row = self.row()
                self.assertEqual(row["quote_check"], "unknown")
                self.assertEqual(row["crossed"], "false")

    def test_future_quote_observation_is_not_eligible(self):
        self.crossing_quotes()
        self.left.quote_time = (self.as_of + timedelta(seconds=1)).isoformat()
        row = self.row()
        self.assertEqual(row["quote_check"], "future")
        self.assertEqual(row["crossed"], "false")

    def test_asynchronous_quotes_beyond_skew_limit_are_not_eligible(self):
        self.crossing_quotes()
        self.left.quote_time = (self.as_of - timedelta(seconds=P.QUOTE_MAX_SKEW_SECONDS + 1)).isoformat()
        row = self.row()
        self.assertEqual(row["quote_check"], "skewed")
        self.assertEqual(row["crossed"], "false")

    def test_freshness_and_skew_boundaries_are_inclusive(self):
        self.crossing_quotes()
        self.left.quote_time = (self.as_of - timedelta(seconds=P.QUOTE_MAX_AGE_SECONDS)).isoformat()
        self.right.quote_time = (self.as_of - timedelta(seconds=P.QUOTE_MAX_AGE_SECONDS - P.QUOTE_MAX_SKEW_SECONDS)).isoformat()
        row = self.row()
        self.assertEqual(row["quote_check"], "fresh")
        self.assertEqual(row["crossed"], "true")

    def test_sports_with_same_fixture_id_remain_separate(self):
        basketball_match = deepcopy(self.match)
        basketball_match.fixture.key.sport = "basketball"
        for market in (basketball_match.m1, basketball_match.m2):
            market.extra["settlement"] = known_profile(sport="basketball")
        rows = P.build_wide_rows([self.match, basketball_match], [], self.coverage, VENUES)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["sport"] for row in rows}, {"cricket", "basketball"})

    def test_absence_requires_complete_collection_for_its_own_sport(self):
        self.match.fixture.key.sport = "basketball"
        rows = P.build_wide_rows([self.match], [], self.coverage, VENUES,
                                 sport_coverage={"basketball": {
                                     "kalshi": True, "polymarket_intl": True, "polymarket_us": False,
                                 }})
        self.assertEqual(rows[0]["polymarket_us_status"], "Coverage incomplete")
        self.assertEqual(rows[0]["venues_checked"], "2")

    def test_two_venue_terminal_omits_polymarket_us_cells_and_coverage(self):
        active = ["kalshi", "polymarket_intl"]
        rows = P.build_wide_rows([self.match], [], self.coverage, active, as_of=self.as_of)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.html"
            P.write_html(path, rows, active, self.coverage, self.as_of)
            document = path.read_text()
        self.assertNotIn("Polymarket US", document)
        self.assertIn('role="tablist"', document)
        self.assertIn('role="dialog"', document.replace('<dialog ', '<dialog role="dialog" '))

    def test_multiple_review_market_candidates_do_not_choose_a_quote(self):
        self.crossing_quotes()
        alternative = deepcopy(self.match)
        alternative.m1.market_id = "another-kalshi-market"
        alternative.m1.bid = "0.90"
        rows = P.build_wide_rows([], [self.match, alternative], self.coverage, VENUES,
                                 include_review=True, as_of=self.as_of)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["comparison_status"], "market_review")
        self.assertEqual(rows[0]["kalshi_bid"], "")
        self.assertEqual(rows[0]["kalshi_ask"], "")
        self.assertEqual(rows[0]["crossed"], "false")
        self.assertIn("another-kalshi-market", rows[0]["kalshi_alternatives"])


if __name__ == "__main__":
    unittest.main()
