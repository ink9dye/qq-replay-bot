import os
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote

from bot.service import BotService, ManualClock
from bot.share import attachment_header
from bot.store import Store
from bot.watch_core import ReplayWriter, _rank_cache
from test_bot import link_token, sample_config, temporary_service


class ReplayDeliveryTests(unittest.TestCase):
    def test_both_games_saved_after_subscription_are_visible_even_if_first_started_earlier(self):
        with temporary_service() as service:
            epoch = 1_800_000_000
            service.clock.now = epoch + 1000
            service.handle_text('10001', '订阅 甲 前100 推送')
            _rank_cache['甲'], _rank_cache['乙'] = 12, 88
            writer = ReplayWriter(service.replay_root, ['甲'])
            writer.on_saved = service.on_replay_saved
            service.clock.now = epoch + 2000
            writer.save(b'game-1', ['甲', '乙'], [datetime.fromtimestamp(epoch + 900)])
            writer.save(b'game-2', ['甲', '乙'], [datetime.fromtimestamp(epoch + 1500)])
            rows = service.visible_replays('10001', '甲')
            self.assertEqual(len(rows), 2)
            self.assertEqual([row['recorded_at'] for row in rows], [epoch + 900, epoch + 1500])
            self.assertTrue(all(row['saved_at'] == epoch + 2000 for row in rows))
            self.assertEqual(len(service.store.pending_notifications()), 2)
            token = link_token(service.handle_text('10001', '录像 甲'))
            self.assertEqual(len(service.store.get_link(token)['replay_ids']), 2)

    def test_legacy_schema_is_migrated_using_file_save_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = sample_config(root)
            data = root / 'data'
            data.mkdir(parents=True, exist_ok=True)
            replay = root / 'replays/甲/旧名称.yrp'
            replay.parent.mkdir(parents=True)
            replay.write_bytes(b'old-game')
            os.utime(replay, (2000, 2000))
            db = sqlite3.connect(str(data / 'bot.sqlite3'))
            db.execute('CREATE TABLE replays (id INTEGER PRIMARY KEY, digest TEXT, focus_player TEXT, rel_path TEXT, players_json TEXT, ranks_json TEXT, recorded_at REAL, UNIQUE(digest,focus_player))')
            db.execute("INSERT INTO replays VALUES (1, 'old', '甲', '甲/旧名称.yrp', '[\"甲\",\"乙\"]', '{\"乙\":88}', 900)")
            db.commit()
            db.close()
            store = Store(data / 'bot.sqlite3')
            store.upsert_subscription('10001', '甲', 100, True, 1000)
            store.insert_link('old-token-for-snapshot-12345', '10001', '甲', 1100, 19100, [], 1000)
            store.close()
            service = BotService(config, root, clock=ManualClock(3000))
            try:
                rows = service.visible_replays('10001', '甲')
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['recorded_at'], 900)
                self.assertEqual(rows[0]['saved_at'], 2000)
                old = service.store.get_link('old-token-for-snapshot-12345')
                self.assertEqual(old['replay_ids'], [])
                self.assertEqual(old['expires_at'], 19100)
            finally:
                service.store.close()

    def test_download_headers_preserve_watch_name_and_extensions(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            _rank_cache['甲'], _rank_cache['乙'] = 12, 88
            writer = ReplayWriter(service.replay_root, ['甲'])
            writer.on_saved = service.on_replay_saved
            writer.save(b'replay-file-body', ['甲', '乙'])
            service.share_server.start()
            token = link_token(service.handle_text('10001', '录像 甲'))
            row = service.visible_replays('10001', '甲')[0]
            base = f'http://127.0.0.1:{service.share.bind_port}/s/{token}'
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'{base}/file/{row["id"]}', timeout=2) as response:
                disposition = response.headers['Content-Disposition']
                self.assertIn('.yrp"', disposition)
                actual_name = unquote(disposition.split("filename*=UTF-8''", 1)[1])
                self.assertEqual(actual_name, Path(row['rel_path']).name)
                self.assertEqual(response.read(), b'replay-file-body')
            with opener.open(base + '/zip', timeout=2) as response:
                self.assertEqual(response.headers['Content-Type'], 'application/zip')
                disposition = response.headers['Content-Disposition']
                self.assertTrue(unquote(disposition.split("filename*=UTF-8''", 1)[1]).endswith('.zip'))
                with zipfile.ZipFile(BytesIO(response.read())) as archive:
                    self.assertEqual(archive.namelist(), [Path(row['rel_path']).name])
            service.clock.now += 5 * 3600
            with self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(f'{base}/file/{row["id"]}', timeout=2)
            self.assertEqual(caught.exception.code, 410)
            self.assertIsNone(caught.exception.headers.get('Content-Disposition'))

    def test_download_header_does_not_allow_control_character_injection(self):
        header = attachment_header('甲"\r\nInjected: yes.yrp', 'replay.yrp')
        self.assertNotIn('\r', header)
        self.assertNotIn('\n', header)
        self.assertIn("filename*=UTF-8''", header)

    def test_existing_replays_are_indexed_from_directory(self):
        from test_deck_links import pack_replay
        _rank_cache['雨夜泪痕'] = 1
        _rank_cache['对手'] = 2
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = sample_config(root)
            data = Path(config['data_dir'])
            data.mkdir(parents=True, exist_ok=True)
            store = Store(data / 'bot.sqlite3')
            store.upsert_subscription('10001', '雨夜泪痕', None, True, 1000)
            store.close()
            replay_root = Path(config['replay_dir'])
            (replay_root / '雨夜泪痕').mkdir(parents=True, exist_ok=True)
            blob = pack_replay(['雨夜泪痕', '对手'], [([1, 1, 2], [10]), ([999999], [])])
            (replay_root / '雨夜泪痕' / 'test.yrp').write_bytes(blob)
            service = BotService(config, root)
            try:
                rows = service.visible_replays('10001', '雨夜泪痕')
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['rel_path'], str(Path('雨夜泪痕') / 'test.yrp'))
            finally:
                service.store.close()

    def test_query_scans_directory_and_indexes_on_demand(self):
        from test_deck_links import pack_replay
        _rank_cache['甲'] = 1
        _rank_cache['对手'] = 2
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            replay_root = service.replay_root
            (replay_root / '甲').mkdir(parents=True, exist_ok=True)
            blob = pack_replay(['甲', '对手'], [([1, 1, 2], [10]), ([999999], [])])
            target = replay_root / '甲' / 'test.yrp'
            target.write_bytes(blob)
            stamp = 1_700_000_000 - 100
            os.utime(target, (stamp, stamp))
            self.assertEqual(service.visible_replays('10001', '甲'), [])
            service.handle_text('10001', '录像 甲')
            rows = service.visible_replays('10001', '甲')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['rel_path'], str(Path('甲') / 'test.yrp'))


if __name__ == '__main__':
    unittest.main()
