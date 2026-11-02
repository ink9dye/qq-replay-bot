# -*- coding: utf-8 -*-
"""代理只放行卡组命名/组合编辑这两个写接口，并限制请求体大小。"""
import unittest
from types import SimpleNamespace

from bot import statistics_proxy


class FakeStore:
    def get_stats_link(self, token):
        return dict(expires_at=999999999.0, revoked_at=None)


class FakePortal:
    def __init__(self):
        self.calls = []

    def _session(self, expires):
        return 'test-session'

    def post(self, path, headers, client, body):
        self.calls.append((path, body))
        return 200, 'application/json', b'{"ok":true}', {}


def app():
    return SimpleNamespace(store=FakeStore(), clock=lambda: 1000.0, statistics_portal=FakePortal())


class ProxyPostTests(unittest.TestCase):
    def test_deck_naming_and_groups_are_allowed(self):
        instance = app()
        status, _kind, _body = statistics_proxy.post(
            instance, '/stats/t/statistics/api/deck-naming',
            ['stats', 't', 'statistics', 'api', 'deck-naming'], b'{}')
        self.assertEqual(status, 200)
        status, _kind, _body = statistics_proxy.post(
            instance, '/stats/t/statistics/api/deck-naming/groups',
            ['stats', 't', 'statistics', 'api', 'deck-naming', 'groups'], b'{}')
        self.assertEqual(status, 200)

    def test_other_post_paths_are_blocked(self):
        status, _kind, body = statistics_proxy.post(
            instance := app(), '/stats/t/statistics/api/card-search',
            ['stats', 't', 'statistics', 'api', 'card-search'], b'{}')
        self.assertEqual(status, 403)
        self.assertEqual(instance.statistics_portal.calls, [])

    def test_groups_body_limit_is_larger_but_bounded(self):
        instance = app()
        status, _kind, _body = statistics_proxy.post(
            instance, '/stats/t/statistics/api/deck-naming/groups',
            ['stats', 't', 'statistics', 'api', 'deck-naming', 'groups'], b'x' * 16385)
        self.assertEqual(status, 400)
        self.assertEqual(instance.statistics_portal.calls, [])

    def test_expired_link_is_rejected(self):
        instance = app()
        instance.store = SimpleNamespace(get_stats_link=lambda token: dict(
            expires_at=1.0, revoked_at=None))
        status, _kind, _body = statistics_proxy.post(
            instance, '/stats/t/statistics/api/deck-naming',
            ['stats', 't', 'statistics', 'api', 'deck-naming'], b'{}')
        self.assertEqual(status, 410)


if __name__ == '__main__':
    unittest.main()
