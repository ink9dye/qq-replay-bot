import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch

from bot.watch_api import WatchApiServer
from bot.watch_jobs import RemoteJobs
from bot.store import Store
from bot.statistics_import import load_report
from test_deck_links import pack_replay


class ReplayEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'bot.sqlite3')
        self.addCleanup(self.store.close)
        self.api = WatchApiServer(RemoteJobs(), self.root/'replays', token='test')
        self.blob = pack_replay(['甲','乙'], [([1,2],[9]),([3],[8])])
        self.digest = hashlib.sha256(self.blob).hexdigest()
        self.e = dict(digest=self.digest,players=['甲','乙'],game_no=1,
                      session_id='verified-match',captured_from_start=True)

    def upload_e(self, **updates):
        return self.api.save_upload('job','sample.yrp.evidence.json',
            json.dumps(dict(self.e,**updates)).encode(),['甲','乙'],time.time())

    def upload_replay(self):
        result=self.api.save_upload('job','sample.yrp',self.blob,['甲','乙'],time.time())
        self.path=Path(result['path'])
        self.row,_=self.store.add_replay(self.digest,'甲',self.path.relative_to(self.api.replay_root).as_posix(),
                                       ['甲','乙'],{},time.time(),saved_at=time.time())
        self.store.import_replay_facts(self.row,self.path,load_report(self.path))
        return result

    def outcomes(self):
        return [r['outcome'] for r in self.store._db.execute('select outcome from stat_views order by player_index')]

    def test_evidence_first_records_identity_without_win(self):
        self.upload_e()
        self.upload_replay()
        self.assertEqual(self.outcomes(),['unknown','unknown'])
        row=self.store._db.execute('select payload_json from stat_evidence').fetchone()
        self.assertEqual(json.loads(row[0])['capture']['session_id'],'verified-match')
        with sqlite3.connect(self.root/'watch-upload-sessions.sqlite3') as db:
            self.assertEqual(json.loads(db.execute('select capture from uploads').fetchone()[0])['game_no'],1)

    def test_late_win_updates_existing_facts_and_preserves_expiry(self):
        self.upload_e()
        self.upload_replay()
        before=self.store._db.execute('select expires_at from stat_games').fetchone()[0]
        result=self.upload_e(winner=1,raw='050101')
        self.assertEqual(result['evidence_status'],'facts_updated')
        self.assertEqual(self.outcomes(),['loss','win'])
        self.assertEqual(self.store._db.execute('select expires_at from stat_games').fetchone()[0],before)
        self.assertEqual(len(list(self.path.parent.glob('*.evidence*.json'))),1)

    def test_replay_first_accepts_win_with_none_association(self):
        self.upload_replay()
        self.upload_e(winner=0,association='none')
        self.assertEqual(self.outcomes(),['win','loss'])

    def test_same_replay_retry_keeps_one_file(self):
        result=self.upload_replay()
        retry=self.api.save_upload('job','sample.yrp',self.blob,['甲','乙'],time.time())
        self.assertEqual(result['path'],retry['path'])
        self.assertEqual(len(list(self.path.parent.glob('*.yrp'))),1)

    def test_duplicate_evidence_does_not_increment_fact_revision(self):
        self.upload_replay()
        self.upload_e(winner=1)
        before=self.store._db.execute('select revision from stat_fact_revisions').fetchone()[0]
        self.assertEqual(self.upload_e(winner=1)['evidence_status'],'unchanged')
        self.assertEqual(self.store._db.execute('select revision from stat_fact_revisions').fetchone()[0],before)

    def test_incomplete_retry_does_not_erase_win(self):
        self.upload_replay()
        self.upload_e(winner=1)
        self.upload_e()
        self.assertEqual(json.loads(Path(str(self.path)+'.evidence.json').read_text(encoding='utf-8'))['winner'],1)

    def test_conflicting_win_is_rejected_and_previous_preserved(self):
        self.upload_replay()
        self.upload_e(winner=1)
        with self.assertRaisesRegex(ValueError,'conflict_winner'):self.upload_e(winner=0)
        self.assertEqual(self.outcomes(),['loss','win'])

    def test_wrong_digest_cannot_overwrite_sidecar(self):
        self.upload_replay()
        with self.assertRaisesRegex(ValueError,'digest_mismatch'):self.upload_e(digest='a'*64)
        self.assertFalse(Path(str(self.path)+'.evidence.json').exists())

    def test_reversed_players_cannot_overwrite_sidecar(self):
        self.upload_replay()
        with self.assertRaisesRegex(ValueError,'player_order'):self.upload_e(players=['乙','甲'])

    def test_invalid_json_shape_and_bool_winner_rejected(self):
        for body in [b'[]',b'{',json.dumps(dict(self.e,winner=True)).encode()]:
            with self.assertRaises(ValueError):self.api.save_upload('job','sample.yrp.evidence.json',body,['甲','乙'],time.time())

    def test_valid_win_without_match_identity_remains_usable(self):
        self.e={k:v for k,v in self.e.items() if k not in ('game_no','session_id','captured_from_start')}
        self.upload_replay();self.upload_e(winner=2)
        self.assertEqual(self.outcomes(),['draw','draw'])

