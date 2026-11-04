import json,unittest
import test_card_statistics as C
import test_top100 as T
from bot.statistics_review import query,apply,Conflict
from bot.statistics_portal import StatisticsPortal


class ReviewTests(unittest.TestCase):
    def setUp(self):
        C.CardStatisticsTests.setUp(self)
        self.store.replace_top100(T.roster(['甲','乙']),C.NOW)

    def test_retired_manual_review_cannot_write_metadata(self):
        before=[tuple(r) for r in self.store._db.execute('SELECT * FROM stat_builds ORDER BY fingerprint')]
        with self.assertRaisesRegex(ValueError, 'manual_naming_removed'):
            apply(self.store._db,self.replays,C.NOW,'build','signature','人工复核类型')
        self.assertEqual([tuple(r) for r in self.store._db.execute('SELECT * FROM stat_builds ORDER BY fingerprint')],before)

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
