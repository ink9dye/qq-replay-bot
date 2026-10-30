import json,unittest
import test_card_statistics as C
import test_top100 as T
from bot.statistics_review import query,apply,Conflict
from bot.statistics_portal import StatisticsPortal


class ReviewTests(unittest.TestCase):
    def setUp(self):
        C.CardStatisticsTests.setUp(self)
        self.store.replace_top100(T.roster(['甲','乙']),C.NOW)

    def test_queue_is_latest_per_person_and_manual_type_links_unexpired_views(self):
        full=query(self.store._db,self.replays,C.NOW,True)
        item=next(i for i in full['items'] if any(p['player']=='甲' for p in i['people']))
        before=tuple(self.store._db.execute('SELECT COUNT(*),SUM(expires_at) FROM stat_games').fetchone())
        self.store._db.execute('BEGIN IMMEDIATE')
        result=apply(self.store._db,self.replays,C.NOW,item['build'],item['signature'],'人工复核类型');self.store._db.commit()
        self.assertGreater(result['affected_unexpired_views'],1)
        rows=self.store._db.execute('SELECT deck_type,classification_source FROM stat_builds WHERE fingerprint=?',(item['build'],)).fetchone()
        self.assertEqual(tuple(rows),('人工复核类型','manual'))
        self.assertEqual(tuple(self.store._db.execute('SELECT COUNT(*),SUM(expires_at) FROM stat_games').fetchone()),before)
        self.assertEqual(sum(len(i['people']) for i in query(self.store._db,self.replays,C.NOW,True)['items']),2)

    def test_changed_classification_rejected_without_overwriting_other_editor(self):
        item=query(self.store._db,self.replays,C.NOW,True)['items'][0]
        self.store.classify_build(item['build'],'另一编辑者',['保护标签'],'manual')
        with self.assertRaises(Conflict):apply(self.store._db,self.replays,C.NOW,item['build'],item['signature'],'过期表单')
        self.assertEqual(self.store._db.execute('SELECT deck_type FROM stat_builds WHERE fingerprint=?',(item['build'],)).fetchone()[0],'另一编辑者')

    def test_dropout_expiry_and_private_origin_boundary(self):
        item=query(self.store._db,self.replays,C.NOW,True)['items'][0]
        self.assertEqual(query(self.store._db,self.replays,C.NOW+14*86400,True)['items'],[])
        portal=StatisticsPortal(self.store.path,self.replays,lambda:C.NOW,'x'*43)
        headers={'Host':'127.0.0.1','Origin':'https://wrong.example','Content-Type':'application/json'}
        headers['Cookie']=portal.route('/statistics/access/'+'x'*43,{'Host':'127.0.0.1'},'127.0.0.1')[3]['Set-Cookie'].split(';',1)[0]
        body=json.dumps(dict(build=item['build'],signature=item['signature'],deck_type='类型')).encode()
        self.assertEqual(portal.post('/statistics/api/classification',headers,'127.0.0.1',body)[0],403)
        headers['Origin']='http://127.0.0.1'
        self.assertEqual(portal.post('/statistics/api/classification',headers,'127.0.0.1',body)[0],403)
        self.assertEqual(portal.post('/statistics/api/classification',headers,'127.0.0.1',body)[0],403)
