"""New query contracts: authenticated HTTP, real store, private portal and Top20."""
import copy
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, urlencode

from bot.query_auth import QueryAuth
from bot.query_core import (PortalBusy, PortalClient, PortalUnavailable, QueryCore,
                            QueryRequestError, query_options)
from bot.statistics_portal import StatisticsPortal
from bot.store import Store
from bot.watch_api import WatchApiServer
from bot.watch_jobs import RemoteJobs
from bot import card_winrate


def request(port, path, token=None, body=None, method='GET'):
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path),
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method=method)
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read()), dict(response.headers)


class ExtensionHTTPTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.store = Store(self.directory / 'stats.db')
        self.addCleanup(self.store.close)
        with self.store._db:
            self.store._db.execute('DROP TABLE stat_views')
            self.store._db.execute('CREATE TABLE stat_views(player TEXT)')
            self.store._db.execute("INSERT INTO stat_views VALUES ('久  远')")
        self.now = [100.0]
        self.portal = StatisticsPortal(self.store.path, self.directory / 'replays',
                                       lambda: self.now[0], 'x' * 43)
        portal = self.portal

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                status, kind, body, extra = portal.route(self.path, self.headers, '127.0.0.1')
                self.send_response(status)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                for key, value in extra.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

        web = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        self.addCleanup(web.server_close)
        self.addCleanup(web.shutdown)
        secret_path = self.directory / 'statistics-owner.key'
        secret_path.write_text('x' * 43, encoding='ascii')
        client = PortalClient('http://127.0.0.1:%d' % web.server_port,
                              secret_path=secret_path, clock=lambda: self.now[0])
        self.core = QueryCore(client, store=self.store, clock=lambda: self.now[0])
        self.auth = QueryAuth(self.directory / 'auth.db')
        self.tokens = []
        for identity in ('12345', '23456'):
            provision = self.auth.provision(identity)
            self.tokens.append(self.auth.login(identity, provision['code'])['token'])
        self.api = WatchApiServer(RemoteJobs(), self.directory / 'replays', 'watch-test',
                                  bind=('127.0.0.1', 0), query_auth=self.auth,
                                  query_core=self.core, admin_token='admin-test',
                                  heavy_wait=0, log=lambda *args: None)
        self.api.start()
        self.addCleanup(self.api.stop)
        self.port = self.api._httpd.server_address[1]

    def call(self, path, **kwargs):
        return request(self.port, path, self.tokens[0], **kwargs)

    def test_binding_real_shared_store_idempotence_and_identity(self):
        self.assertEqual(self.call('/query-api/binding')[1]['data'], {'player': None})
        path = '/query-api/binding'
        data = self.call(path, body={'player': ' 久 远 '}, method='POST')[1]['data']
        self.assertEqual(data, dict(player='久  远', known=True, changed=True))
        self.assertEqual(self.store.get_binding('12345'), '久  远')
        self.assertFalse(self.call(path, body={'player': '久 远'}, method='POST')[1]['data']['changed'])
        self.assertIsNone(request(self.port, path, self.tokens[1])[1]['data']['player'])
        self.assertEqual(self.call(path, body={'player': '未收录'}, method='POST')[1]['data']['known'], False)
        self.assertEqual(self.call(path, body={'player': '他人', 'identity': '23456'}, method='POST')[0], 400)
        self.assertIsNone(self.store.get_binding('23456'))
        self.assertTrue(self.call(path + '/unbind', method='POST')[1]['data']['removed'])
        self.assertFalse(self.call(path + '/unbind', method='POST')[1]['data']['removed'])

    def test_binding_validation_and_shared_write_limit(self):
        for value in (None, 12, [], 'a/b', 'a\\b', 'x\ny', 'x' * 101):
            self.assertEqual(self.call('/query-api/binding', body={'player': value}, method='POST')[0], 400)
        for _ in range(5):
            self.assertEqual(self.call('/query-api/binding', body={'player': 'a'}, method='POST')[0], 200)
            self.assertEqual(self.call('/query-api/binding/unbind', method='POST')[0], 200)
        status, data, headers = self.call('/query-api/binding/unbind', method='POST')
        self.assertEqual((status, data['error']), (429, 'busy'))
        self.assertIn('Retry-After', headers)
        self.now[0] += 61
        self.assertEqual(self.call('/query-api/binding/unbind', method='POST')[0], 200)

    def test_all_new_paths_require_query_access_even_when_gate_busy(self):
        paths = ['/query-api/binding', '/query-api/binding/unbind',
                 '/query-api/player/a/side-plan']
        paths += ['/query-api/card/1/' + kind for kind in ('usage', 'flow', 'winrate')]
        for path in paths:
            method = 'POST' if path.endswith('unbind') else 'GET'
            for token in (None, 'watch-test', 'admin-test'):
                self.assertEqual(request(self.port, path, token, method=method)[0], 401)
        gate = self.api._httpd.query_heavy_gate
        permits = self.api.query_heavy_gate
        for _ in range(permits):
            gate.acquire()
        try:
            path = '/query-api/card/1/winrate'
            self.assertEqual(request(self.port, path)[0], 401)
            status, data, headers = self.call(path)
            self.assertEqual((status, data['error']), (429, 'busy'))
            self.assertIn('Retry-After', headers)
        finally:
            for _ in range(permits):
                gate.release()

    def test_side_parameters_encoding_empty_and_missing_build(self):
        name = '久  远+%'
        path = '/query-api/player/' + quote(name, safe='') + '/side-plan'
        with patch('bot.statistics_side_recent.query', return_value={
                'opponents': [], 'generated_at': 12}) as query:
            status, payload, _ = self.call(path + '?min_side_games=9&opponent_range=0')
            self.assertEqual(status, 200)
            self.assertEqual(payload['data']['generated_at'], 12)
            self.assertEqual(query.call_args.args[3], name)
            self.assertEqual(query.call_args.kwargs['min_side_games'], 9)
            self.assertIsNone(query.call_args.kwargs['opponent_range'])
        with patch('bot.statistics_side_recent.query', return_value=None):
            status, data, _ = self.call(path + '?digest=' + 'a' * 64)
            self.assertEqual((status, data['error']), (404, 'no_build'))
        for suffix in ('?top=0', '?recent=11', '?min_side_games=true', '?opponent_range=1',
                       '?digest=bad', '?top=2&top=3'):
            self.assertEqual(self.call(path + suffix)[0], 400)
        for name in ('a/b', 'a\\b', 'a\nb'):
            self.assertEqual(self.call('/query-api/player/' + quote(name, safe='') + '/side-plan')[0], 400)

    def test_usage_trims_only_records_and_cache_keeps_generation_time(self):
        original = dict(code=1, name='卡', covered=3, current=3,
                        main_total=7, records=[{'player': str(i)} for i in range(3)])
        with patch('bot.card_usage_pie.collect', return_value=original) as collect:
            status, first, _ = self.call('/query-api/card/1/usage?limit=1')
            self.assertEqual(status, 200)
            self.assertEqual(len(first['data']['records']), 1)
            self.assertTrue(first['data']['truncated'])
            self.assertEqual(first['data']['records_total'], 3)
            self.now[0] += 10
            second = self.call('/query-api/card/1/usage?limit=1')[1]
            self.assertEqual(first, second)
            self.assertEqual(collect.call_count, 1)
            full = self.call('/query-api/card/1/usage?limit=100')[1]['data']
            self.assertFalse(full['truncated'])
            self.assertEqual(full['main_total'], first['data']['main_total'])
        self.assertEqual(len(original['records']), 3)

    def test_flow_full_lists_no_record_dump_and_null_denominator(self):
        item = dict(deck='A', players=1, **{key: dict(known=0, used=0, rate=0)
                                          for key in ('main', 'side', 'total')})
        original = dict(code=1, items=[item], bars={'main': []},
                        small_groups=[copy.deepcopy(item)], records=[{'player': 'secret'}], shown=[])
        with patch('bot.card_flow.collect_flow', return_value=original):
            result = self.call('/query-api/card/1/flow')[1]['data']
        self.assertNotIn('records', result)
        self.assertNotIn('shown', result)
        self.assertIsNone(result['items'][0]['main']['rate'])
        self.assertEqual(original['items'][0]['main']['rate'], 0)
        self.assertEqual(self.call('/query-api/card/1/flow?limit=20')[0], 400)

    def test_winrate_default20_explicit5_and_error_mapping(self):
        def payload(*args, **kwargs):
            return dict(code=kwargs['code'], generated_at=10,
                        matchups=dict(limit=kwargs['limit'], rows=[]), order={})
        with patch('bot.card_winrate.collect', side_effect=payload) as collect:
            data = self.call('/query-api/card/1/winrate')[1]['data']
            self.assertEqual(data['matchups']['limit'], 20)
            self.assertEqual(self.call('/query-api/card/1/winrate?limit=5')[1]['data']['matchups']['limit'], 5)
            self.assertEqual(collect.call_count, 2)
        for suffix in ('?limit=0', '?limit=21', '?limit=1.5', '?limit=5&limit=6'):
            self.assertEqual(self.call('/query-api/card/1/winrate' + suffix)[0], 400)
        with patch('bot.card_usage_pie.collect', side_effect=KeyError('card_not_found')):
            self.assertEqual(self.call('/query-api/card/9/usage')[0], 404)
        with patch('bot.card_usage_pie.collect', side_effect=OSError('catalog unavailable')):
            self.assertEqual(self.call('/query-api/card/9/usage')[0], 503)
        with patch.object(self.core.portal, 'get_json', side_effect=PortalBusy(3)):
            status, data, headers = self.call('/query-api/card/10/usage')
            self.assertEqual((status, data['error']), (429, 'busy'))
            self.assertEqual(headers['Retry-After'], '3')
        for code in ('0', '-1', '4294967296', 'abc'):
            self.assertEqual(self.call('/query-api/card/' + code + '/usage')[0], 400)

    def test_internal_routes_need_owner_locality_and_unforwarded_header(self):
        path = '/statistics/api/query-card/usage?code=1&limit=20'
        headers = {'Host': '127.0.0.1', 'Cookie': 'qqstats_owner=' + self.portal._session(200)}
        self.assertEqual(self.portal.route(path, headers, '127.0.0.1')[0], 403)
        headers['X-Query-Internal'] = '1'
        self.assertEqual(self.portal.route(path, headers, '192.0.2.1')[0], 403)
        headers['X-Forwarded-For'] = '192.0.2.1'
        self.assertEqual(self.portal.route(path, headers, '127.0.0.1')[0], 403)


class ExactCardAndTop20Tests(unittest.TestCase):
    def test_real_usage_and_flow_keep_coverage_for_known_unused_card(self):
        from bot.card_usage_pie import collect
        from bot.card_flow import collect_flow
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'stats.db'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE stat_build_cards(build TEXT,code INTEGER,partition TEXT,copies INTEGER)')
            db.commit()
            db.close()
            view = dict(player='a', build='build', digest='d', recorded_at=1,
                        observed_game_no=1, observed_identity_confirmed=True)
            population = ({'views': [view]}, {'a': view},
                          [dict(player='a', rank=1, deck_type='A')])
            names = {'1': dict(id=1, cn_name='未使用卡')}
            with patch('bot.statistics_dashboard.population', return_value=population), \
                 patch('bot.card_usage_pie._card_index', return_value=names):
                usage = collect(path, tmp, 1, code=1)
                flow = collect_flow(path, tmp, 1, code=1)
            self.assertEqual((usage['covered'], usage['main_total']), (1, 0))
            self.assertEqual(usage['main_dist'][0], 1)
            self.assertEqual((flow['covered'], flow['total_used']), (1, 0))
            self.assertEqual(flow['bars']['total'], [])

    def test_exact_code_uses_verified_identity_never_name_search(self):
        from bot.card_usage_pie import resolve_code
        names = {'1': dict(id=1, cn_name='同名卡'), '3': dict(id=3, cn_name='同名卡')}
        with patch('bot.card_usage_pie._card_index', return_value=names), \
             patch('bot.card_identity.identity', side_effect=lambda db, code:
                   dict(canonical_card=1 if code == 2 else code,
                        codes=[1, 2] if code in (1, 2) else [code])):
            self.assertEqual(resolve_code('.', 2, None), (1, '同名卡'))
            self.assertEqual(resolve_code('.', 3, None), (3, '同名卡'))
            with self.assertRaisesRegex(KeyError, 'card_not_found'):
                resolve_code('.', 9, None)
        with patch('bot.card_usage_pie._card_index', return_value={}):
            with self.assertRaises(OSError):
                resolve_code('.', 1, None)

    def test_real_compute_top20_fallback_and_default5_share_aggregates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'stats.db'
            with sqlite3.connect(path) as db:
                db.executescript("CREATE TABLE stat_current_top100(player TEXT);"
                                 "INSERT INTO stat_current_top100 VALUES('a'),('b');"
                                 "CREATE TABLE stat_build_cards(build TEXT, code INTEGER, partition TEXT,copies INTEGER);"
                                 "INSERT INTO stat_build_cards VALUES('build',1,'main',3);")
            db.close()
            selected, facts, opponents = [], {}, {}
            for group in range(23):
                for game in range(10 if group < 7 else 6 if group < 22 else 4):
                    digest = '%d-%d' % (group, game)
                    selected.append(dict(digest=digest, player='a', build='build',
                                         first_player=game % 2, outcome='win' if game % 3 else 'draw'))
                    facts[(digest, 'a')] = dict(used=1)
                    opponents[digest] = [dict(player='b', deck_type='D%02d' % group)]
            with patch('bot.card_winrate._basis', return_value=selected), \
                 patch('bot.card_identity.identity', return_value=dict(codes=[1], canonical_card=1)), \
                 patch('bot.card_identity.facts', return_value=facts), \
                 patch('bot.card_statistics._opponent_rows', return_value=opponents):
                default = card_winrate._compute(path, tmp, 0, 1)
                top20 = card_winrate._compute(path, tmp, 0, 1, limit=20)
                self.assertEqual(len(default['matchups']['rows']), 5)
                self.assertEqual(len(top20['matchups']['rows']), 20)
                self.assertEqual(default['matchups']['base'], top20['matchups']['base'])
                self.assertEqual(default['order'], top20['order'])
                self.assertEqual(sum(row['low_sample'] for row in top20['matchups']['rows']), 13)
                with patch('bot.card_identity.identity', return_value=dict(codes=[99], canonical_card=99)):
                    empty = card_winrate._compute(path, tmp, 0, 99, limit=20)
                self.assertEqual(empty['matchups']['rows'], [])
                self.assertIsNone(empty['matchups']['base']['rate'])

    def test_collect_cache_separates_limits_and_preserves_timestamp(self):
        with patch.dict(card_winrate._CACHE, {}, clear=True), \
             patch('bot.card_winrate.resolve_card', return_value=(1, 'a')), \
             patch('bot.card_winrate._compute', side_effect=lambda *args, **kwargs:
                   dict(matchups={'limit': kwargs['limit']})) as compute:
            small = card_winrate.collect('x.db', '.', 10, 'a')
            large = card_winrate.collect('x.db', '.', 20, 'a', limit=20)
            cached = card_winrate.collect('x.db', '.', 30, 'a', limit=20)
            self.assertEqual(compute.call_count, 2)
            self.assertEqual(small['matchups']['limit'], 5)
            self.assertEqual(large['matchups']['limit'], 20)
            self.assertEqual(cached['generated_at'], 20)


if __name__ == '__main__':
    unittest.main()
