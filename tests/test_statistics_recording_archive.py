import hashlib
import io
import json
import unittest
import zipfile
from urllib.parse import urlencode
import test_card_statistics as C
import test_statistics_feature65 as F
from bot.statistics_build_detail import query
from bot.statistics_recording_archive import archive
from bot.statistics_recording_names import catalog,safe_filename


class RecordingArchiveTests(unittest.TestCase):
    setUp=F.PopulationAndDetailTests.setUp
    evidence=F.PopulationAndDetailTests.evidence
    get=F.PopulationAndDetailTests.get

    def test_zip_is_original_bytes_named_per_match_and_private(self):
        self.evidence();result=query(self.store._db,self.replays,C.NOW,'甲',self.rows[0]['digest'])
        group=next(g for g in result['match_groups']if len(g['games'])==2)
        a,b=group['games'];self.assertEqual(a['observed_match_no'],b['observed_match_no'])
        self.assertEqual((a['observed_game_no'],b['observed_game_no']),(1,2))
        body,name,count=archive(self.store._db,self.replays,C.NOW,'甲',self.rows[0]['digest'],match=group['id'],clock=lambda:C.NOW)
        self.assertEqual(count,2);self.assertTrue(name.endswith('.zip'))
        with zipfile.ZipFile(io.BytesIO(body))as package:
            self.assertEqual(len(package.namelist()),2)
            self.assertTrue(all(n.endswith('.yrp')and'当前#'in n for n in package.namelist()))
            self.assertEqual({hashlib.sha256(package.read(n)).hexdigest()for n in package.namelist()},{a['digest'],b['digest']})
        path='/statistics/recording-zip?'+urlencode(dict(player='甲',digest=self.rows[0]['digest'],match=group['id']))
        self.assertEqual(self.portal.route(path,{'Host':'127.0.0.1'},'127.0.0.1')[0],403)
        response=self.get(path);self.assertEqual(response[0],200);self.assertEqual(response[1],'application/zip')

    def test_ordinal_is_not_renumbered_by_scope_or_exclusion(self):
        self.evidence();before=catalog(self.store._db,'甲')
        self.store.set_fact_exclusion(self.rows[0]['digest'],'甲',reason='test')
        self.assertEqual(before,catalog(self.store._db,'甲'))
        result=query(self.store._db,self.replays,C.NOW,'甲',self.rows[3]['digest'])
        self.assertTrue(all(r['digest']!=self.rows[0]['digest']for r in result['matches']))
        for row in result['matches']:self.assertEqual(row['observed_match_no'],before[row['digest']]['observed_match_no'])

    def test_expiry_and_unknown_group_and_filename_safety(self):
        self.evidence()
        self.assertRaises(ValueError,archive,self.store._db,self.replays,C.NOW,'甲',match='not-a-group',clock=lambda:C.NOW)
        self.assertRaises(ValueError,archive,self.store._db,self.replays,C.NOW,'甲',clock=lambda:C.NOW+14*86400)
        name=safe_filename('10/27 1 1 a\\b:*?.')
        self.assertTrue(name.endswith('.yrp'))
        self.assertFalse(any(c in name for c in '/\\:*?'))

    def test_missing_source_never_in_zip_or_renewed(self):
        self.evidence();before=self.store._db.execute('SELECT SUM(expires_at) FROM stat_games').fetchone()[0]
        (self.replays/self.rows[0]['rel_path']).unlink()
        body,_,_=archive(self.store._db,self.replays,C.NOW,'甲',self.rows[3]['digest'],clock=lambda:C.NOW)
        with zipfile.ZipFile(io.BytesIO(body))as package:
            self.assertNotIn(self.rows[0]['digest'],{hashlib.sha256(package.read(n)).hexdigest()for n in package.namelist()})
        self.assertEqual(before,self.store._db.execute('SELECT SUM(expires_at) FROM stat_games').fetchone()[0])

    def test_match_crossing_midnight_uses_first_observed_day(self):
        from datetime import datetime
        from bot.statistics_recording_names import CST
        self.evidence()
        stamps=[datetime(2026,10,27,23,59,tzinfo=CST).timestamp(),datetime(2026,10,28,0,5,tzinfo=CST).timestamp()]
        for index,stamp in zip((0,3),stamps):
            self.store._db.execute('UPDATE stat_games SET received_at=? WHERE digest=?',(stamp,self.rows[index]['digest']))
        self.store._db.commit();names=catalog(self.store._db,'甲')
        first,second=[names[self.rows[index]['digest']]for index in (0,3)]
        self.assertEqual(first['observed_date'],'2026-10-27')
        self.assertEqual(first['observed_date'],second['observed_date'])
        self.assertEqual(first['observed_match_no'],second['observed_match_no'])

    def test_changed_original_bytes_fail_closed(self):
        self.evidence()
        for row in self.rows:(self.replays/row['rel_path']).write_bytes(b'changed')
        self.assertRaises(ValueError,archive,self.store._db,self.replays,C.NOW,'甲',clock=lambda:C.NOW)

    def test_recording_title_and_opponent_filter_use_same_reliable_first_game(self):
        from bot.statistics_recording_filters import query as recordings
        self.evidence();db=self.store._db
        old=db.execute('SELECT build FROM stat_views WHERE digest=? AND player=?',(self.rows[3]['digest'],'乙')).fetchone()[0]
        clone=dict(db.execute('SELECT * FROM stat_builds WHERE fingerprint=?',(old,)).fetchone())
        clone.update(fingerprint='post-opponent-unknown',deck_type=None)
        columns=list(clone);db.execute('INSERT INTO stat_builds('+','.join(columns)+') VALUES('+','.join('?'for _ in columns)+')',[clone[k]for k in columns])
        db.execute('UPDATE stat_views SET build=? WHERE digest=? AND player=?',(clone['fingerprint'],self.rows[3]['digest'],'乙'));db.commit()
        r=recordings(db,self.replays,C.NOW,{'opponent':['对手B']})
        second=next(r for r in r['samples']if r['digest']==self.rows[3]['digest']and r['player']=='甲')
        self.assertEqual(second['opponent_deck_type'],'对手B')
        self.assertIn('对手B',second['recording_title'])
