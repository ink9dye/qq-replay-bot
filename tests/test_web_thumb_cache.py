import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from bot import statistics_local_cards as cards, statistics_proxy as proxy
from bot.statistics_portal import StatisticsPortal


class WebThumbCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.portal=StatisticsPortal(self.root/'bot.sqlite3',self.root,lambda:1000,'x'*43)
        self.headers={'Host':'127.0.0.1','Cookie':'qqstats_owner='+self.portal._session(1100)}

    def test_disk_hit_never_schedules_network_and_has_image_cache_header(self):
        folder=cards.thumb_dir(self.root);folder.mkdir()
        (folder/'123.jpg').write_bytes(b'cached jpeg')
        with patch.object(cards,'_schedule_thumb',side_effect=AssertionError('disk hit')):
            result=self.portal.route('/statistics/card-thumb/123',self.headers,'127.0.0.1')
        self.assertEqual(result[:3],(200,'image/jpeg',b'cached jpeg'))
        self.assertEqual(result[3]['Cache-Control'],'private, max-age=86400')

    def test_miss_redirects_immediately_and_queues_only_small_image(self):
        with patch.object(cards,'_schedule_thumb') as queue:
            result=self.portal.route('/statistics/card-thumb/123',self.headers,'127.0.0.1')
        self.assertEqual(result[0],302)
        self.assertEqual(result[3]['Location'],cards.THUMB_REMOTE % 123)
        self.assertIn('no-store',result[3]['Cache-Control'])
        self.assertEqual(len(queue.call_args.args),3)

    def test_invalid_codes_and_unauthenticated_requests_do_not_warm_cache(self):
        with patch.object(cards,'_schedule_thumb',side_effect=AssertionError('must not queue')):
            for value in ('0','4294967296','abc'):
                self.assertEqual(self.portal.route('/statistics/card-thumb/'+value,self.headers,'127.0.0.1')[0],400)
            self.assertEqual(self.portal.route('/statistics/card-thumb/123',{'Host':'127.0.0.1'},'127.0.0.1')[0],403)

    def test_pending_queue_is_bounded(self):
        with patch.object(cards,'_thumb_pending',set(range(cards.THUMB_PENDING_MAX))), \
                patch.object(cards._thumb_pool,'submit',side_effect=AssertionError('queue full')):
            cards._schedule_thumb(self.root,999999,cards.thumb_dir(self.root))
            self.assertEqual(len(cards._thumb_pending),cards.THUMB_PENDING_MAX)

    def app(self):
        return SimpleNamespace(store=SimpleNamespace(get_stats_link=lambda token:dict(expires_at=2000,revoked_at=None)),clock=lambda:1000,statistics_portal=self.portal)

    def test_share_proxy_preserves_thumbnail_redirect_and_cache_hits(self):
        parts=['stats','t','statistics','card-thumb','123']
        with patch.object(proxy,'_fetch',return_value=(302,{'Content-Type':'text/plain','Location':cards.THUMB_REMOTE % 123},b'')):
            result=proxy.route(self.app(),'/stats/t/statistics/card-thumb/123',parts)
        self.assertEqual(result[3]['Location'],cards.THUMB_REMOTE % 123)
        with patch.object(proxy,'_fetch',return_value=(200,{'Content-Type':'image/webp'},b'image')):
            result=proxy.route(self.app(),'/stats/t/statistics/card-thumb/123',parts)
        self.assertEqual(result[3]['Cache-Control'],'private, max-age=86400')

    def test_fact_responses_cannot_inherit_image_cache_control(self):
        with patch.object(proxy,'_fetch',return_value=(200,{'Content-Type':'application/json','Cache-Control':'public, max-age=86400'},b'{}')):
            result=proxy.route(self.app(),'/stats/t/statistics/api/roster',['stats','t','statistics','api','roster'])
        self.assertEqual(len(result),3)

    def test_card_image_miss_redirects_to_the_right_source(self):
        # 缺本地图时不再 404：≥1e8 直接 302 到 super-pre，普通卡回 momobako。
        with patch.object(cards,'picture',return_value=None):
            result=self.portal.route('/statistics/card-image/101307074',self.headers,'127.0.0.1')
            self.assertEqual(result[0],302)
            self.assertTrue(result[3]['Location'].startswith('https://cdntx.moecube.com/ygopro-super-pre/data/pics/'))
            result=self.portal.route('/statistics/card-image/12345678',self.headers,'127.0.0.1')
            self.assertEqual(result[0],302)
            self.assertTrue(result[3]['Location'].startswith('https://cdn.233.momobako.com/ygopro/pics/'))

    def test_card_image_hit_serves_local_bytes(self):
        image=self.root/'x.jpg'; image.write_bytes(b'jpeg-bytes')
        with patch.object(cards,'picture',return_value=(image,'image/jpeg')):
            result=self.portal.route('/statistics/card-image/123',self.headers,'127.0.0.1')
        self.assertEqual(result[:3],(200,'image/jpeg',b'jpeg-bytes'))
        self.assertEqual(result[3]['Cache-Control'],'public, max-age=86400')

    def test_share_proxy_preserves_card_image_redirect(self):
        target='https://cdntx.moecube.com/ygopro-super-pre/data/pics/101307074.jpg'
        with patch.object(proxy,'_fetch',return_value=(302,{'Content-Type':'text/plain','Location':target},b'')):
            result=proxy.route(self.app(),'/stats/t/statistics/card-image/101307074',
                               ['stats','t','statistics','card-image','101307074'])
        self.assertEqual(result[3]['Location'],target)

    def test_share_proxy_drops_untrusted_image_redirect(self):
        with patch.object(proxy,'_fetch',return_value=(302,{'Content-Type':'text/plain','Location':'https://evil.example/x.jpg'},b'')):
            result=proxy.route(self.app(),'/stats/t/statistics/card-image/1',
                               ['stats','t','statistics','card-image','1'])
        self.assertEqual(len(result),3)

    def test_thumbnail_redirects_are_not_followed_by_server_with_owner_cookie(self):
        handler=proxy._NoImageRedirect()
        self.assertIsNone(handler.redirect_request(None,None,302,'found',{},'https://cdn.example/image'))
