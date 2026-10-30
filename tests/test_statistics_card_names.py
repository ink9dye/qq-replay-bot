import json,tempfile,unittest
from pathlib import Path
from bot.statistics_card_names import index,search
from bot.statistics_portal import StatisticsPortal
import test_card_statistics as C


class CardNamesTests(unittest.TestCase):
    def test_cid_key_never_replaces_passcode_even_when_keys_collide(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'cache').mkdir()
            (root/'cache/cards.json').write_text(json.dumps({'4007':dict(id=89631139,cid=4007,cn_name='青眼白龙'),
                '89631139':dict(id=123,cid=89631139,cn_name='冲突资料键'),
                '6980':dict(id=24508238,cid=6980,cn_name='D.D.乌鸦'),
                'bad':dict(id=None,cid=11,cn_name='无效'),
                'true':dict(id=True,cn_name='布尔值')},ensure_ascii=False),encoding='utf8')
            self.assertEqual(search(root,'89631139'),[dict(code=89631139,name='青眼白龙')])
            self.assertEqual(search(root,'D.D.乌鸦'),[dict(code=24508238,name='D.D.乌鸦')])
            self.assertEqual(index(root)['123']['cn_name'],'冲突资料键')
            self.assertNotIn('4007',index(root));self.assertNotIn('11',index(root))
            self.assertEqual(search(root,'99999999')[0]['code'],99999999)

    def test_public_cache_refresh_and_legacy_passcode_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'cache').mkdir();path=root/'cache/cards.json'
            path.write_text(json.dumps({'1':dict(cn_name='旧名')}),encoding='utf8')
            self.assertEqual(search(root,'旧名')[0]['code'],1)
            path.write_text(json.dumps({'4007':dict(id=89631139,cid=4007,cn_name='青眼白龙新资料')}),encoding='utf8')
            self.assertEqual(search(root,'青眼白龙')[0]['code'],89631139)
            self.assertNotIn('1',index(root))

    def test_real_structure_used_by_build_and_historical_source_lookup(self):
        C.CardStatisticsTests.setUp(self)
        cache=self.root/'cache';cache.mkdir()
        (cache/'cards.json').write_text(json.dumps({'4007':dict(id=1,cid=4007,cn_name='正确密码一'),
            '1':dict(id=222,cid=1,cn_name='不能串到资料键一')}),encoding='utf8')
        portal=StatisticsPortal(self.store.path,self.replays,lambda:C.NOW,'x'*43);headers={'Host':'127.0.0.1'}
        headers['Cookie']=portal.route('/statistics/access/'+'x'*43,headers,'127.0.0.1')[3]['Set-Cookie'].split(';',1)[0]
        from urllib.parse import urlencode
        paths=['/statistics/api/build?'+urlencode(dict(player='甲')),
               '/statistics/api/sample?'+urlencode(dict(player='甲',digest=self.rows[0]['digest']))]
        for path in paths:
            status,_,body,_=portal.route(path,headers,'127.0.0.1');self.assertEqual(status,200)
            card=next(c for c in json.loads(body)['cards'] if c['code']==1)
            self.assertEqual(card['name'],'正确密码一')
