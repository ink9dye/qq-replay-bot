import copy
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from bot.store import Store
from retention_policy import RETENTION_SECONDS
from test_deck_links import pack_replay

NOW = 1800000000.0


class StatisticsFactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'db.sqlite3')
        self.addCleanup(self.store.close)
        self.replays = self.root / 'replays'
        self.replays.mkdir()

    def source(self, focus='甲', stamp=NOW-100, code=1):
        blob = pack_replay(['甲', '乙'], [([code, code, 2], [9]), ([3, 4], [8])])
        digest = hashlib.sha256(blob).hexdigest()
        path = self.replays / focus / (digest + '.yrp')
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(blob)
        os.utime(path, (stamp, stamp))
        row, _ = self.store.add_replay(digest, focus, str(path.relative_to(self.replays)),
                                      ['甲', '乙'], {}, NOW-200, saved_at=stamp)
        return row, path

    def report(self, row, complete=True):
        return dict(version=4, digest=row['digest'], playback='completed' if complete else 'failed',
            winner=0, result_source='live_server', first_player=1,
            players=[dict(name='甲', main=[1, 1, 2], extra=[9], used_cards=[dict(code=1,events=5),dict(code=9,events=1)],
                extra_appearances=[dict(code=9,appearance_count=0)], draw_record_complete=complete,
                initial_draws_accounted=complete,
                drawn_cards=[dict(code=1,draw_count=2,initial_count=1,used_after_draw=True)]),
                dict(name='乙', main=[3,4], extra=[8], used_cards=[], extra_appearances=[],
                draw_record_complete=complete, initial_draws_accounted=complete, drawn_cards=[])])

    def details(self, row, now=NOW):
        return self.store.fact_details(row['digest'], self.replays, now)

    def card(self, row, code, player='甲'):
        return next(c for c in self.details(row)['cards'] if c['player']==player and c['code']==code)

    def test_duplicate_files_and_reimports_keep_one_game_two_views_and_one_card_unit(self):
        row, path = self.source()
        report = self.report(row)
        for _ in range(3):
            self.store.import_replay_facts(row, path, report, NOW)
        other, other_path = self.source('乙', NOW-20)
        self.store.import_replay_facts(other, other_path, report, NOW)
        self.assertEqual(len(self.store.fact_games(self.replays, NOW)), 1)
        self.assertEqual(len(self.details(row)['views']), 2)
        self.assertEqual(self.card(row,1)['copies'],2)
        self.assertEqual(self.card(row,1)['use_count'],5)
        self.assertEqual(self.store._db.execute('SELECT COUNT(*) FROM stat_files').fetchone()[0],2)

    def test_partial_absence_is_unknown_but_positive_draw_and_use_are_known(self):
        row, path = self.source()
        self.store.import_replay_facts(row,path,self.report(row,False),NOW)
        self.assertEqual(self.card(row,1)['drawn'],1)
        self.assertEqual(self.card(row,1)['used'],1)
        self.assertIsNone(self.card(row,2)['drawn'])
        self.assertIsNone(self.card(row,2)['used'])
        self.assertIsNone(self.card(row,1)['initial_count'])
        self.assertIsNone(self.card(row,9)['appeared'])

    def test_full_absence_and_negated_extra_summon_are_zero_not_appearance(self):
        row,path=self.source()
        report=self.report(row)
        # 卡2用过却没有抽到（例如检索所得），两类事实不能混为一类。
        report['players'][0]['used_cards'].append(dict(code=2,events=1))
        self.store.import_replay_facts(row,path,report,NOW)
        self.assertEqual((self.card(row,2)['used'],self.card(row,2)['drawn']),(1,0))
        self.assertEqual((self.card(row,9)['used'],self.card(row,9)['appeared']),(1,0))
        self.assertEqual(self.card(row,9)['partition'],'extra')
        self.assertIsNone(self.card(row,9)['drawn'])

    def test_later_partial_or_base_import_does_not_erase_complete_facts(self):
        row,path=self.source()
        self.store.import_replay_facts(row,path,self.report(row),NOW)
        partial=self.report(row,False)
        partial['players'][0]['drawn_cards']=[]
        partial['players'][0]['used_cards']=[]
        self.store.import_replay_facts(row,path,partial,NOW)
        self.store.import_replay_facts(row,path,None,NOW)
        self.assertEqual((self.card(row,1)['used'],self.card(row,1)['draw_count']),(1,2))
        self.assertEqual(self.details(row)['views'][0]['draw_complete'],1)

    def test_result_and_actual_order_are_player_perspectives(self):
        row,path=self.source()
        self.store.import_replay_facts(row,path,self.report(row),NOW)
        views=self.details(row)['views']
        self.assertEqual([(v['outcome'],v['first_player']) for v in views],[('win',0),('loss',1)])

    def test_conflicting_direct_result_stays_unknown_and_older_parser_cannot_replace(self):
        row,path=self.source()
        report=self.report(row)
        self.store.import_replay_facts(row,path,report,NOW)
        conflict=copy.deepcopy(report);conflict['winner']=1
        self.store.import_replay_facts(row,path,conflict,NOW)
        self.store.import_replay_facts(row,path,report,NOW)
        self.assertTrue(all(v['outcome']=='unknown' for v in self.details(row)['views']))
        old=copy.deepcopy(report);old['version']=3
        self.store.import_replay_facts(row,path,old,NOW)
        self.assertTrue(all(v['result_source']=='conflict' for v in self.details(row)['views']))

    def test_fourteen_day_boundary_reanalysis_and_classification_never_renew(self):
        received=NOW-RETENTION_SECONDS+1
        row,path=self.source(stamp=received)
        self.store.import_replay_facts(row,path,self.report(row),NOW)
        before=self.store.fact_games(self.replays,NOW)[0]
        os.utime(path,(NOW,NOW))
        other,other_path=self.source('乙',NOW)
        self.store.import_replay_facts(other,other_path,self.report(row),NOW)
        self.store._db.execute("UPDATE stat_builds SET deck_type='新分类',classification_version='v2'")
        self.store._db.execute("INSERT INTO stat_exclusions VALUES(?,?,?,?)",(row['digest'],'甲','default','验收排除'))
        self.store._db.commit()
        self.assertEqual(self.store.fact_games(self.replays,NOW)[0]['expires_at'],before['expires_at'])
        self.assertIsNone(self.details(row,NOW+1))
        self.assertEqual(self.store.purge_expired_facts(NOW+1),1)
        for table in ('stat_games','stat_views','stat_card_facts','stat_builds','stat_build_cards','stat_files','stat_evidence','stat_exclusions'):
            self.assertEqual(self.store._db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0,table)
        self.assertFalse(self.store.import_replay_facts(other,other_path,self.report(row),NOW+1))

    def test_delete_one_copy_keeps_facts_last_copy_cascades(self):
        row,path=self.source(); other,other_path=self.source('乙')
        self.store.import_replay_facts(row,path,self.report(row),NOW)
        self.store.import_replay_facts(other,other_path,self.report(row),NOW)
        self.store.delete_replay(row['id'])
        self.assertIsNotNone(self.details(other))
        self.store.delete_replay(other['id'])
        self.assertEqual(self.store._db.execute('SELECT COUNT(*) FROM stat_card_facts').fetchone()[0],0)
        self.assertEqual(self.store._db.execute('SELECT COUNT(*) FROM stat_builds').fetchone()[0],0)

    def test_expiry_does_not_delete_another_live_game_sharing_build(self):
        row,path=self.source(stamp=NOW-RETENTION_SECONDS+1)
        other,other_path=self.source('甲',NOW-10,code=5)
        self.store.import_replay_facts(row,path,None,NOW)
        self.store.import_replay_facts(other,other_path,None,NOW)
        self.store.purge_expired_facts(NOW+1)
        self.assertEqual(len(self.store.fact_games(self.replays,NOW+1)),1)
        self.assertIsNotNone(self.details(other,NOW+1))
        # 乙的构筑相同，仍被另一小局引用，不能误删。
        build=self.details(other,NOW+1)['views'][1]['build']
        self.assertIsNotNone(self.store._db.execute('SELECT * FROM stat_builds WHERE fingerprint=?',(build,)).fetchone())

    def test_missing_file_and_invalid_report_are_not_valid_samples(self):
        row,path=self.source()
        report=self.report(row);report['players'][0]['main']=[999]
        with self.assertRaises(ValueError):self.store.import_replay_facts(row,path,report,NOW)
        self.assertEqual(self.store.fact_games(self.replays,NOW),[])
        self.store.import_replay_facts(row,path,None,NOW)
        path.unlink()
        self.assertEqual(self.store.fact_games(self.replays,NOW),[])

    def test_side_unknown_and_unseen_hidden_main_slot_are_not_complete_zero(self):
        row,path=self.source(code=0)
        self.store.import_replay_facts(row,path,None,NOW)
        build=self.details(row)['views'][0]['build']
        b=self.store._db.execute('SELECT * FROM stat_builds WHERE fingerprint=?',(build,)).fetchone()
        self.assertEqual((b['main_complete'],b['side_complete']),(0,0))
        self.assertFalse(any(c['partition']=='side' for c in self.details(row)['cards']))

    def test_missing_event_arrays_do_not_make_negative_usage_facts(self):
        row,path=self.source()
        report=self.report(row)
        del report['players'][0]['used_cards']
        del report['players'][0]['extra_appearances']
        self.store.import_replay_facts(row,path,report,NOW)
        self.assertIsNone(self.card(row,2)['used'])
        self.assertEqual(self.details(row)['views'][0]['usage_complete'],0)
