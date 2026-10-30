import unittest
from unittest.mock import patch
from test_bot import temporary_service
from test_deck_links import pack_replay
from bot.watch_core import ReplayWriter


class StandaloneIndexTests(unittest.TestCase):
    def test_unsubscribed_standalone_replay_is_indexed_without_granting_access(self):
        with temporary_service() as service, patch('bot.service.arena_rank_number',return_value=None):
            folder=service.replay_root/'甲'; folder.mkdir()
            path=folder/'game.yrp'
            path.write_bytes(pack_replay(['甲','乙'],[([1],[]),([2],[])]))
            self.assertEqual(service.index_existing_replays(),1)
            self.assertEqual(len(service.store.replays_for_player('甲')),1)
            self.assertEqual(service.visible_replays('10001','甲'), [])
            self.assertEqual(service.store.list_subscriptions(),[])

    def test_folder_not_matching_replay_players_and_corrupt_files_stay_unindexed(self):
        with temporary_service() as service:
            folder=service.replay_root/'不是当局玩家'; folder.mkdir()
            path=folder/'game.yrp'
            path.write_bytes(pack_replay(['甲','乙'],[([1],[]),([2],[])]))
            self.assertEqual(service._focus_player(path),'')
            path.write_bytes(b'not a replay')
            self.assertEqual(service._focus_player(path),'')

    def test_new_standalone_save_indexes_but_does_not_enqueue_qq_notification(self):
        with temporary_service() as service, patch('watch.arena_rank_number',return_value=None), \
             patch('bot.service.arena_rank_number',return_value=None), patch('watch.deck_title_for',return_value='测试'):
            writer=ReplayWriter(service.replay_root,['甲'])
            writer.on_saved=service.on_replay_saved
            writer.save(pack_replay(['甲','乙'],[([1],[]),([2],[])]),['甲','乙'])
            self.assertEqual(len(service.store.replays_for_player('甲')),1)
            self.assertEqual(service.store.pending_notifications(),[])
            self.assertEqual(service.store.list_subscriptions(),[])
