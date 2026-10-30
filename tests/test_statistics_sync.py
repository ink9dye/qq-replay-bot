import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import watch
from test_bot import temporary_service
from test_deck_links import pack_replay


class StatisticsSyncTests(unittest.TestCase):
    def observe(self, service):
        writer=watch.ReplayWriter(service.replay_root,['甲'])
        writer.on_saved=service.on_replay_saved
        writer.on_session=service.store.record_session
        s=watch.Spectator({},watch.RoomInfo('one','one','one',['甲','乙'],True),writer)
        s.handle(watch.STOC_DUEL_START,b'')
        s.handle(watch.STOC_GAME_MSG,bytes([4,0x10])+bytes(17))
        s.handle(watch.STOC_GAME_MSG,bytes([40,1]))
        blob=pack_replay(['甲','乙'],[([1,2],[9]),([3],[8])])
        s.handle(watch.STOC_REPLAY,blob)
        row=service.store.replays_for_player('甲')[0]
        return s,row,service.replay_root/row['rel_path']

    def test_late_result_then_conflict_resynchronizes_without_subscription_or_qq(self):
        with temporary_service() as service,patch('watch.arena_rank_number',return_value=None), \
                patch('bot.service.arena_rank_number',return_value=None),patch('watch.deck_title_for',return_value='测试'):
            s,row,path=self.observe(service)
            self.assertEqual(service.sync_replay_facts(),1)
            before=service.store.fact_details(row['digest'],service.replay_root,service.clock())
            self.assertEqual(before['views'][0]['outcome'],'unknown')
            self.assertEqual(service.sync_replay_facts(),0)
            s.handle(watch.STOC_GAME_MSG,bytes([5,1,0]))
            self.assertEqual(service.sync_replay_facts(),1)
            after=service.store.fact_details(row['digest'],service.replay_root,service.clock())
            self.assertEqual([v['outcome'] for v in after['views']],['loss','win'])
            self.assertEqual([v['first_player'] for v in after['views']],[0,1])
            s.handle(watch.STOC_GAME_MSG,bytes([5,0,0]))
            service.sync_replay_facts()
            revoked=service.store.fact_details(row['digest'],service.replay_root,service.clock())
            self.assertTrue(all(v['outcome']=='unknown' for v in revoked['views']))
            self.assertEqual(service.store.list_subscriptions(),[])
            self.assertEqual(service.store.pending_notifications(),[])

    def test_corrupt_or_wrong_cached_report_falls_back_to_actual_build_unknown_usage(self):
        with temporary_service() as service,patch('watch.arena_rank_number',return_value=None), \
                patch('bot.service.arena_rank_number',return_value=None),patch('watch.deck_title_for',return_value='测试'):
            s,row,path=self.observe(service)
            wrong=dict(version=4,digest=row['digest'],players=[dict(name='甲',main=[99],extra=[]),dict(name='乙',main=[3],extra=[8])],
                       winner=0,result_source='native_replay',playback='completed')
            Path(str(path)+'.analysis.json').write_text(json.dumps(wrong))
            service.sync_replay_facts()
            details=service.store.fact_details(row['digest'],service.replay_root,service.clock())
            self.assertEqual(details['views'][0]['outcome'],'unknown')
            self.assertEqual({c['code'] for c in details['cards'] if c['player']=='甲'},{1,2,9})
            self.assertTrue(all(c['used'] is None for c in details['cards']))

    def test_expired_facts_are_purged_even_when_physical_replay_is_locked(self):
        from retention_policy import RETENTION_SECONDS
        with temporary_service() as service,patch('watch.arena_rank_number',return_value=None), \
                patch('bot.service.arena_rank_number',return_value=None),patch('watch.deck_title_for',return_value='测试'):
            _,row,path=self.observe(service)
            service.sync_replay_facts()
            service.clock.now += RETENTION_SECONDS
            original_unlink=Path.unlink
            def locked(item,*args,**kwargs):
                if item == path:raise PermissionError('test lock')
                return original_unlink(item,*args,**kwargs)
            with patch.object(Path,'unlink',locked):
                service.cleanup_old_files()
            self.assertTrue(path.exists())
            self.assertIsNone(service.store.fact_details(row['digest'],service.replay_root,service.clock()))
            self.assertEqual(service.store._db.execute('SELECT COUNT(*) FROM stat_card_facts').fetchone()[0],0)
