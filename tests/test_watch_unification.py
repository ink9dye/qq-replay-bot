import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import watch
from bot import watch_core
from bot.watch_core import ReplayWriter, Spectator, STOC_REPLAY, _rank_cache, select_rooms
from test_bot import temporary_service, link_token, room
from test_deck_links import pack_replay, deck_url, decode_url

ROOT=Path(__file__).resolve().parent.parent


@contextmanager
def backend_globals():
    fields=['YGOPRO_DIR','DATA_DIR','CACHE_DIR','CONFIG_PATH','SESSION_PATH','REFERENCE_DECKS_PATH',
            '_CARD_NAMES','_REFERENCE_DECKS']
    old={name:getattr(watch,name) for name in fields}
    try:
        yield
    finally:
        for name,value in old.items():
            setattr(watch,name,value)


def observe(service, player, opponent, rank, main):
    _rank_cache[player]=12
    _rank_cache[opponent]=rank
    writer=ReplayWriter(service.replay_root,[player])
    writer.on_saved=service.on_replay_saved
    writer.on_session=service.store.record_session
    spectator=Spectator(service.watch_config(),room('room-'+opponent,[player,opponent])[0],writer)
    blob=pack_replay([player,opponent],[(main,[10]),([999999],[])])
    spectator.handle(STOC_REPLAY,blob)
    return service.store.replays_for_player(player)[-1]


class UnifiedWatchTests(unittest.TestCase):
    def test_bot_import_is_same_module_as_independent_backend(self):
        self.assertIs(watch_core,watch)
        self.assertIs(watch_core.ReplayWriter,watch.ReplayWriter)
        self.assertIs(watch_core.sided_decks,watch.sided_decks)
        self.assertEqual(Path(watch.__file__).resolve(),ROOT/'watch.py')
        self.assertFalse(hasattr(watch,'_watch_module'))

    def test_reference_classification_is_bundled_next_to_new_watch(self):
        with backend_globals():
            watch._REFERENCE_DECKS=None
            self.assertEqual(watch.REFERENCE_DECKS_PATH,ROOT/'reference_decks.json')
            refs=watch.reference_decks()
            self.assertTrue(refs)
            first=refs[0]
            self.assertEqual(watch.deck_title_for(first['main'],first['extra'],[]),first['label'])

    def test_standalone_check_config_does_not_start_qq_or_connect(self):
        with tempfile.TemporaryDirectory() as tmp, backend_globals():
            root=Path(tmp)
            cfg=root/'independent.json'
            cfg.write_text(json.dumps({'ygopro_dir':str(root/'game'),'data_dir':str(root/'data'),
                                       'replay_dir':str(root/'replays'),'deck_dir':str(root/'decks'),
                                       'account':'test-private-account','password':'test-private-password',
                                       'watch_names':['雨雨雨1']}),encoding='utf-8')
            with patch.object(sys,'argv',['watch.py','--config',str(cfg),'--check-config']), \
                    patch('watch.asyncio.run') as runner:
                watch.main()
            runner.assert_not_called()

    def test_standalone_main_really_runs_same_serve_and_observes_all_targets(self):
        with tempfile.TemporaryDirectory() as tmp, backend_globals():
            root=Path(tmp)
            cfg=root/'independent.json'
            cfg.write_text(json.dumps({'ygopro_dir':str(root/'game'),'data_dir':str(root/'data'),
                                       'watch_names':['甲'],'watch_vs_top':[{'name':'乙','top':100}]}),encoding='utf-8')
            fake=AsyncMock()
            with patch.object(sys,'argv',['watch.py','--config',str(cfg),'--name','丙']),patch('watch.serve',fake):
                watch.main()
            fake.assert_awaited_once()
            # --name沿用原watch行为，替代配置的无条件名单，排名名单中的玩家仍会追踪。
            self.assertEqual(set(fake.call_args.args[1]),{'乙','丙'})
            self.assertEqual(fake.call_args.args[2],[])

    def test_backend_independent_from_old_location_and_current_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=subprocess.run([sys.executable,str(ROOT/'watch.py'),'--self-test'],cwd=tmp,
                                  stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',timeout=15)
            self.assertEqual(result.returncode,0,result.stdout)
            self.assertIn('self-test ok',result.stdout)

    def test_all_players_are_observed_but_rank_only_filters_recording_links_and_pushes(self):
        with temporary_service() as service,patch('bot.share.arena_points',return_value=None):
            service.handle_text('10001','订阅 雨雨雨1 前10 推送')
            names,rules=service.current_rules()
            self.assertEqual(names,['雨雨雨1'])
            self.assertEqual(rules,[])
            _rank_cache['普通对手']=500
            picked,_=select_rooms(room('ordinary',['雨雨雨1','普通对手']),names,'exact',True,rules)
            self.assertEqual(len(picked),1)
            high=observe(service,'雨雨雨1','前十对手',5,[1,1,2])
            service.clock.now+=10
            low=observe(service,'雨雨雨1','普通对手',500,[3,4,4])
            self.assertEqual([r['id'] for r in service.visible_replays('10001','雨雨雨1')],[high['id']])
            self.assertEqual([r['replay_id'] for r in service.store.pending_notifications()],[high['id']])
            token=link_token(service.handle_text('10001','录像 雨雨雨1'))
            snapshot=service.store.get_link(token)
            self.assertEqual(snapshot['replay_ids'],[high['id']])
            self.assertEqual(set(snapshot['deck_replay_ids']),{high['id'],low['id']})
            body=service.share.page(token,'dir')[2]
            self.assertEqual(decode_url(deck_url(body))[1][0],[3,4,4])
            self.assertEqual(service.share.page(token,'file',str(low['id']))[0],404)
            self.assertEqual(service.share.page(token,'file',str(high['id']))[0],200)
            service.clock.now+=10
            observe(service,'雨雨雨1','另一对手',700,[6,6,7])
            old_body=service.share.page(token,'dir')[2]
            self.assertEqual(decode_url(deck_url(old_body))[1][0],[3,4,4])
            new=link_token(service.handle_text('10001','录像 雨雨雨1'))
            self.assertEqual(decode_url(deck_url(service.share.page(new,'dir')[2]))[1][0],[6,6,7])

    def test_default_recording_paths_belong_to_new_project_data(self):
        from test_bot import sample_config
        from bot.service import BotService
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cfg=sample_config(root)
            cfg.pop('replay_dir',None);cfg.pop('deck_dir',None)
            service=BotService(cfg,root)
            try:
                self.assertEqual(service.replay_root,root/'data/replays')
                self.assertEqual(service.deck_dir,root/'data/decks')
            finally:
                service.store.close()


if __name__=='__main__':
    unittest.main()
