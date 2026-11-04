import json
import tempfile
import unittest
from pathlib import Path

from bot.store import Store
import test_statistics_facts as F
import test_top100 as T

NOW=F.NOW


class BuildClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.replays=self.root/'replays';self.replays.mkdir()
        self.store=Store(self.root/'db.sqlite3');self.addCleanup(self.store.close)
        self.row,self.path=F.StatisticsFactsTests.source(self)
        self.store.import_replay_facts(self.row,self.path,None,NOW)
        self.store.replace_top100(T.roster(['甲','乙']),NOW)
        self.reference=self.root/'reference.json'
        self.reference.write_text(json.dumps(dict(decks=[dict(label='甲类型',main=[1,1,2],extra=[9],series=['系列A','系列B'])])),encoding='utf-8')

    def views(self,now=NOW):return self.store.current_top100_views(self.replays,now)['views']



    def test_unknown_to_known_reclassifies_all_same_build_only(self):
        other,other_path=F.StatisticsFactsTests.source(self,code=5)
        self.store.import_replay_facts(other,other_path,None,NOW)
        build=next(v['build'] for v in self.views() if v['digest']==self.row['digest'] and v['player']=='甲')
        self.store.classify_build(build,'新识别',['系列A'],'manual1')
        mine=[v for v in self.views() if v['player']=='甲']
        self.assertEqual(len(mine),2)
        self.assertEqual({v['deck_type'] for v in mine},{None,'新识别'})
        counts=self.store._db.execute('SELECT COUNT(*) FROM stat_card_facts').fetchone()[0]
        self.store.classify_build(build,'修正名称',['系列B'],'manual2')
        self.assertEqual(self.store._db.execute('SELECT COUNT(*) FROM stat_card_facts').fetchone()[0],counts)
        self.assertEqual(next(v['series'] for v in self.views() if v['build']==build),['系列B'])


    def test_dropout_and_expiration_still_apply_after_classification(self):
        self.store.classify_build(self.views()[0]["build"], "存储标签", ["系列A"], "fixture")
        self.store.replace_top100(T.roster(['乙']),NOW+1)
        self.assertEqual([v['player'] for v in self.views()],['乙'])
        self.store.replace_top100(T.roster(['甲','乙']),NOW+2)
        self.assertEqual(len(self.views()),2)
        from retention_policy import RETENTION_SECONDS
        self.assertEqual(self.views(NOW+RETENTION_SECONDS),[])
        self.store.purge_expired_facts(NOW+RETENTION_SECONDS)
        self.assertEqual(self.store._db.execute('SELECT COUNT(*) FROM stat_build_series').fetchone()[0],0)






