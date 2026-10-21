import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import canonical as C
from market_catalog import MarketCatalog
from research_store import ResearchStore
from runtime import MarketRuntime
from terminal_markets import TerminalMarkets
from test_runtime import write_runtime_snapshot


class TerminalMarketsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.templates = write_runtime_snapshot(self.root)
        self.store = ResearchStore(self.root / 'data/eventlens.sqlite3')
        self.runtime = MarketRuntime(self.root, collector=lambda *a, **k: ([], []))
        self.catalog = MarketCatalog(self.store)
        rows, assets = self.runtime.research_rows()
        self.assets = assets
        self.events = {
            'kalshi': [{'event_ticker': 'native-event', 'category': 'Sports', 'markets': [
                {'ticker': r['kalshi_market_id'], 'title': 'Native winner ' + r['canonical_outcome'], 'status': 'active'} for r in rows]}],
            'polymarket_intl': [{'id': 'native-event', 'tags': [{'slug': 'sports'}], 'markets': [
                {'id': '101', 'question': 'Native winner', 'outcomes': '["Beta esports","Alpha esports"]',
                 'clobTokenIds': '["200","100"]', 'active': True}]}]}
        self.load_catalog()
        self.terminal = TerminalMarkets(self.catalog, self.store, self.runtime)

    def load_catalog(self):
        self.catalog.requester = lambda url, params: (200, {'events': self.events['kalshi' if 'kalshi' in url else 'polymarket_intl'], 'cursor': '', 'next_cursor': ''})
        self.catalog.refresh(1)

    def alter_runtime(self, change):
        rows, assets = self.runtime.research_rows()
        for row in rows:
            change(row)
        self.terminal.runtime = SimpleNamespace(research_rows=lambda: (copy.deepcopy(rows), assets),
            settings=lambda: {'min_liquidity_usd': '100'})

    def test_exact_native_ids_merge_catalog_into_one_row_per_validated_outcome(self):
        result = self.terminal.page()
        self.assertEqual((result['total'], result['counts']['matched']), (2, 2))
        for row in result['markets']:
            self.assertEqual(row['match_status'], 'matched')
            legs = {leg['exchange']: leg for leg in row['legs']}
            expected = '100' if row['outcome'] == 'Alpha Academy' else '200'
            self.assertEqual(legs['polymarket_intl']['instrument_id'], expected)
            self.assertEqual(row['category'], 'Esports')
            self.assertEqual(row['net_cents'], '1.00')
            self.assertEqual(row['pnl'], '1.00')
            self.assertTrue(row['alert'] and row['net_crossed'])
        self.assertEqual(len(self.terminal.page(view='alerts')['markets']), 2)

    def test_unmatched_outcome_stays_single_without_fee_or_profit_claims(self):
        self.events['polymarket_intl'].append({'id': 'rates', 'tags': [{'slug': 'economics'}], 'markets': [
            {'id': 'rates-market', 'question': 'Fed cuts in December?', 'outcomes': '["Yes"]', 'clobTokenIds': '["900"]', 'active': True}]})
        self.load_catalog()
        row = self.terminal.page(category='Economics')['markets'][0]
        self.assertEqual(row['match_status'], 'single')
        self.assertEqual(len(row['legs']), 1)
        self.assertIsNone(row['pnl'])
        self.assertIsNone(row['net_cents'])
        self.assertFalse(row['alert'])
        self.assertEqual(row['legs'][0]['research_pair'], row['id'])

    def test_exact_question_candidates_are_review_links_and_never_alerts(self):
        title, expiry = 'Will policy pass by December 31, 2026?', '2026-12-31T23:59:00Z'
        self.events['kalshi'].append({'event_ticker': 'policy', 'category': 'Politics', 'markets': [
            {'ticker': 'KXPOLICY-YES', 'title': title, 'close_time': expiry, 'status': 'active'}]})
        self.events['polymarket_intl'].append({'id': 'policy', 'tags': [{'slug': 'politics'}], 'markets': [
            {'id': 'policy-pm', 'question': title, 'endDate': expiry, 'outcomes': '["Yes"]', 'clobTokenIds': '["999"]', 'active': True}]})
        self.load_catalog()
        for choice in self.catalog.markets():
            if choice['category'] != 'Politics': continue
            row, assets = self.catalog.lookup(choice['id']);v = choice['exchange']
            row.update({v + '_bid': '.9' if v == 'kalshi' else '.3', v + '_ask': '.95' if v == 'kalshi' else '.4',
                v + '_bid_size': '1000', v + '_ask_size': '1000', v + '_quote_time': datetime.now(timezone.utc).isoformat()})
            self.store.record([row], assets=assets, select=False)
        rows = self.terminal.page(category='Politics')['markets']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['match_status'], 'review')
        self.assertEqual(len(rows[0]['legs']), 2)
        self.assertFalse(rows[0]['crossed'] or rows[0]['net_crossed'] or rows[0]['alert'])
        self.assertIsNone(rows[0]['comparison_id'])
        self.assertEqual(self.terminal.page(category='Politics', view='alerts')['total'], 0)

    def test_related_candidate_matching_keeps_comparators_and_expiry_times_distinct(self):
        expiry = '2026-12-31T23:59:00Z'
        self.events['kalshi'].append({'event_ticker': 'threshold', 'category': 'Economics', 'markets': [
            {'ticker': 'threshold-yes', 'title': 'Will GDP be > 2%?', 'close_time': expiry, 'status': 'active'}]})
        self.events['polymarket_intl'].append({'id': 'threshold', 'tags': [{'slug': 'economics'}], 'markets': [
            {'id': 'threshold-pm', 'question': 'Will GDP be < 2%?', 'endDate': expiry, 'outcomes': '["Yes"]', 'clobTokenIds': '["999"]', 'active': True}]})
        self.load_catalog()
        self.assertEqual(self.terminal.page(category='Economics', view='review')['total'], 0)
        self.assertEqual(self.terminal.page(category='Economics', view='single')['total'], 2)
        market = self.events['polymarket_intl'][-1]['markets'][0]
        market.update(question='Will GDP be > 2%?', endDate='2026-12-31T12:00:00Z')
        self.load_catalog()
        self.assertEqual(self.terminal.page(category='Economics', view='review')['total'], 0)

    def test_stale_skewed_invalid_review_and_thin_quotes_do_not_generate_alerts(self):
        cases = {
            'stale': lambda r: r.update(kalshi_quote_time=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()),
            'skew': lambda r: r.update(kalshi_quote_time=(datetime.now(timezone.utc)-timedelta(seconds=10)).isoformat()),
            'local_cross': lambda r: r.update(kalshi_ask='.4'),
            'review': lambda r: r.update(comparison_status='rules_unknown'),
            'thin': lambda r: r.update(kalshi_bid_size='2', polymarket_intl_ask_size='2'),
            'closed': lambda r: r.update(kalshi_market_status='closed')}
        for case, change in cases.items():
            with self.subTest(case=case):
                self.alter_runtime(change)
                self.assertEqual(self.terminal.page(view='alerts')['total'], 0)
        self.alter_runtime(cases['stale'])
        self.assertTrue(all(row['top_liquidity'] is None for row in self.terminal.page()['markets']))

    def test_fees_negative_cross_remains_a_gross_alert_without_positive_net_signal(self):
        self.alter_runtime(lambda r: r.update(kalshi_fee_metadata=json.dumps({'taker_fee': {'model':'kalshi_quadratic', 'multiplier':'1'}}), kalshi_fee_schedule=json.dumps({'model':'kalshi_quadratic', 'multiplier':'1'})))
        rows = self.terminal.page(view='alerts')['markets']
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['crossed'] and not row['net_crossed'] and float(row['pnl']) < 0 for row in rows))
        self.assertTrue(all('fees absorb' in row['alert_reason'] for row in rows))

    def test_paging_filters_strict_size_watchlist_and_input_validation(self):
        all_rows = self.terminal.page()['markets']
        self.assertEqual(self.terminal.page(limit=1, offset=1)['total'], 2)
        self.assertEqual(len(self.terminal.page(limit=1, offset=1)['markets']), 1)
        self.assertEqual(self.terminal.page(category='Economics')['total'], 0)
        self.assertEqual(self.terminal.page(view='single')['total'], 0)
        self.assertEqual(self.terminal.page(strict=True)['total'], 0)
        self.assertEqual(self.terminal.page(quoted=True)['total'], 2)
        self.assertEqual(self.terminal.page(min_liquidity='1000')['total'], 0)
        self.assertEqual(self.terminal.page(size='10')['markets'][0]['quantity'], '10')
        self.assertEqual(self.terminal.page(view='watch', watchlist=[all_rows[0]['comparison_key']])['total'], 1)
        for params in ({'view':'fake'},{'sort':'fake'},{'direction':'bad'},{'limit':101},{'offset':-1},{'min_liquidity':'NaN'},{'strict':'true'},{'size':'999'},{'watchlist':'not a list'}):
            with self.subTest(params=params), self.assertRaises(ValueError): self.terminal.page(**params)

    def test_latest_exact_token_quotes_can_update_single_rows_without_matching_them(self):
        self.terminal.runtime = None
        choice = next(c for c in self.catalog.markets() if c['exchange']=='polymarket_intl' and c['instruments']['polymarket_intl']=='100')
        row, assets = self.catalog.lookup(choice['id'])
        row.update(polymarket_intl_bid='.15', polymarket_intl_ask='.17', polymarket_intl_bid_size='1000',
                   polymarket_intl_ask_size='1000', polymarket_intl_quote_time=datetime.now(timezone.utc).isoformat())
        self.store.record([row], assets=assets, select=False)
        rows = self.terminal.page(quoted=True)['markets']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['legs'][0]['bid'], '.15')
        self.assertEqual(rows[0]['match_status'], 'single')
        self.assertFalse(rows[0]['alert'])

    def test_unresolved_ambiguous_and_conflicting_links_remain_separate_venues(self):
        cases = [('fixture_review', 'fixture is unresolved: published start times differ'),
                 ('market_review', 'multiple market candidates on polymarket_intl'),
                 ('rules_differ', 'normal-result settlement rules differ: draw payout'),
                 ('rules_unknown', 'missing DLS policy; fixture is unresolved')]
        for status, reason in cases:
            with self.subTest(status=status):
                self.alter_runtime(lambda r: r.update(mapping_status='review', comparison_status=status, comparison_reason=reason))
                page = self.terminal.page()
                self.assertEqual(page['counts'].get('review', 0), 0)
                self.assertEqual(page['total'], 4)
                self.assertTrue(all(row['match_status'] == 'single' and len(row['legs']) == 1 for row in page['markets']))
                self.assertTrue(all(row['link_reason'] == reason for row in page['markets']))
                self.assertTrue(all(row['pnl'] is None and not row['alert'] for row in page['markets']))
                # Catalog duplicates are consumed by exact native identities.
                identities = [(leg['exchange'], leg['instrument_id']) for row in page['markets'] for leg in row['legs']]
                self.assertEqual(len(identities), len(set(identities)))

    def test_confirmed_fixture_with_missing_rule_evidence_is_still_reviewable(self):
        self.alter_runtime(lambda r: r.update(mapping_status='review', comparison_status='rules_unknown',
            comparison_reason='normal-result settlement rules are incomplete: policy:dls'))
        page = self.terminal.page(view='review')
        self.assertEqual(page['total'], 2)
        self.assertTrue(all(len(row['legs']) == 2 and not row['alert'] and row['net_cents'] is None for row in page['markets']))

    def test_sports_filter_and_counts_use_explicit_runtime_and_native_metadata(self):
        self.events['kalshi'].extend([
            {'event_ticker': 'basketball', 'category': 'Sports', 'sport': 'basketball', 'markets': [
                {'ticker': 'basketball-yes', 'title': 'Native proposition', 'status': 'active'}]},
            {'event_ticker': 'ambiguous', 'category': 'Sports', 'markets': [
                {'ticker': 'generic-yes', 'title': 'NBA NFL World Cup winner?', 'status': 'active'}]}])
        self.load_catalog()
        page = self.terminal.page()
        counts = {(item['category'], item['sport']): item['markets'] for item in page['sports']}
        self.assertEqual(counts, {('Esports', 'cs2'): 2, ('Sports', 'basketball'): 1, ('Sports', 'unknown'): 1})
        self.assertEqual(self.terminal.page(category='Sports', sport='basketball')['total'], 1)
        self.assertEqual(self.terminal.page(category='Sports', sport='cs2')['total'], 0)
        self.assertEqual(self.terminal.page(sport='unknown')['markets'][0]['title'], 'NBA NFL World Cup winner?')
        self.assertEqual(self.terminal.page(sport='cs2')['markets'][0]['sport_label'], 'Counter-Strike 2')
        with self.assertRaises(ValueError):
            self.terminal.page(sport='made-up-sport')

    def test_duplicate_pairs_are_deduplicated_and_conflicting_native_links_are_not_accepted(self):
        rows, assets = self.runtime.research_rows()
        rows.append(copy.deepcopy(rows[0]))
        runtime = SimpleNamespace(research_rows=lambda: (copy.deepcopy(rows), assets),
            settings=lambda: {'min_liquidity_usd': '100'})
        self.terminal.runtime = runtime
        self.assertEqual(self.terminal.page()['total'], 2)
        conflicting = rows[-1]
        old_key = ('kalshi', conflicting['kalshi_market_id'], C.norm_team(conflicting['canonical_outcome']))
        conflicting['kalshi_market_id'] = 'another-native-market'
        assets[('kalshi', conflicting['kalshi_market_id'], old_key[2])] = 'another-native-market'
        page = self.terminal.page()
        self.assertEqual(page['counts']['matched'], 1)
        self.assertEqual(page['counts']['single'], 3)
        self.assertTrue(all(row['match_status'] != 'matched' or row['outcome'] != rows[0]['canonical_outcome'] for row in page['markets']))
        self.assertTrue(all(not row['alert'] for row in page['markets'] if row['match_status'] == 'single'))

if __name__ == '__main__': unittest.main()
