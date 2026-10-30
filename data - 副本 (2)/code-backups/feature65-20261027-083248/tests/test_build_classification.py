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

    def test_rule_match_and_multiple_series_do_not_duplicate_views(self):
        result=self.store.refresh_classifications(self.reference)
        views=self.views()
        self.assertEqual(len(views),2)
        self.assertEqual(views[0]['deck_type'],'甲类型')
        self.assertEqual(views[0]['series'],['系列A','系列B'])
        self.assertEqual(views[0]['series_status'],'known')
        self.assertIsNone(views[1]['deck_type'])
        self.assertEqual(views[1]['series_status'],'unknown')
        second=self.store.refresh_classifications(self.reference)
        self.assertEqual(second['changed'],0)
        self.assertEqual(second['revision'],result['revision'])

    def test_manual_correction_wins_over_future_rules_and_import_does_not_renew(self):
        build=self.views()[0]['build']
        expires=self.store.fact_games(self.replays,NOW)[0]['expires_at']
        self.store.classify_build(build,'手动类型',['系列X','系列Y'],'manual1')
        self.store.refresh_classifications(self.reference)
        self.store.import_replay_facts(self.row,self.path,None,NOW)
        self.assertEqual(self.views()[0]['deck_type'],'手动类型')
        self.assertEqual(self.views()[0]['series'],['系列X','系列Y'])
        self.assertEqual(self.store.fact_games(self.replays,NOW)[0]['expires_at'],expires)

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

    def test_ambiguous_reference_stays_unknown_and_bad_rules_do_not_erase_labels(self):
        original=json.loads(self.reference.read_text())
        original['decks'].append(dict(label='另一个类型',main=[1,1,2],extra=[9]))
        self.reference.write_text(json.dumps(original))
        self.store.refresh_classifications(self.reference)
        self.assertIn(self.views()[0]['deck_type'],['甲类型','另一个类型'])
        self.store.classify_build(self.views()[0]['build'],'明确手动',['系列A'],'manual')
        self.reference.write_text('null')
        with self.assertRaises(ValueError):self.store.refresh_classifications(self.reference)
        self.assertEqual(self.views()[0]['deck_type'],'明确手动')

    def test_dropout_and_expiration_still_apply_after_classification(self):
        self.store.refresh_classifications(self.reference)
        self.store.replace_top100(T.roster(['乙']),NOW+1)
        self.assertEqual([v['player'] for v in self.views()],['乙'])
        self.store.replace_top100(T.roster(['甲','乙']),NOW+2)
        self.assertEqual(len(self.views()),2)
        from retention_policy import RETENTION_SECONDS
        self.assertEqual(self.views(NOW+RETENTION_SECONDS),[])
        self.store.purge_expired_facts(NOW+RETENTION_SECONDS)
        self.assertEqual(self.store._db.execute('SELECT COUNT(*) FROM stat_build_series').fetchone()[0],0)

    def test_explicit_rule_extension_can_complete_series_for_same_reference(self):
        override=self.root/'rules.json'
        override.write_text(json.dumps(dict(references=[dict(label='甲类型',main=[1,1,2],extra=[9],series=['补全系列'])])))
        self.store.refresh_classifications(self.reference,override)
        self.assertEqual(self.views()[0]['series'],['补全系列'])

    def test_unique_exact_reference_wins_nearby_label_but_not_another_exact(self):
        # One added card yields >.9 similarity, below the .08 fuzzy margin.
        original=json.loads(self.reference.read_text())
        original['decks'].append(dict(label='近似另一类',main=[1,1,2,3],extra=[9]))
        self.reference.write_text(json.dumps(original))
        self.store.refresh_classifications(self.reference)
        self.assertEqual(self.views()[0]['deck_type'],'甲类型')
        original['decks'].append(dict(label='冲突的完全相同卡表',main=[1,1,2],extra=[9]))
        self.reference.write_text(json.dumps(original))
        self.store.refresh_classifications(self.reference)
        self.assertIn(self.views()[0]['deck_type'],['甲类型','冲突的完全相同卡表'])

    def test_identical_reference_name_choice_is_stable_and_order_independent(self):
        from bot.build_classification import normalize_references
        from collections import Counter
        refs=[dict(label=name,cards=Counter([1,1,2]),series=[]) for name in ('甲','乙')]
        first=normalize_references(refs,{})[0]['label']
        self.assertIn(first,['甲','乙'])
        self.assertEqual(first,normalize_references(list(reversed(refs)),{})[0]['label'])
