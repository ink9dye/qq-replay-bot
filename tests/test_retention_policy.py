import os
import unittest
from pathlib import Path
from retention_policy import RETENTION_SECONDS, next_cleanup_delay
from bot.replay_window import REPLAY_WINDOW_SECONDS, replay_in_window
from bot.watch_core import RETENTION_SECONDS as WATCH_RETENTION_SECONDS
from test_bot import temporary_service
from test_help_and_window import save_replay


class RetentionPolicyTests(unittest.TestCase):
    def test_all_entry_points_share_fourteen_days(self):
        self.assertEqual(RETENTION_SECONDS, 14 * 86400)
        self.assertEqual(REPLAY_WINDOW_SECONDS, RETENTION_SECONDS)
        self.assertEqual(WATCH_RETENTION_SECONDS, RETENTION_SECONDS)

    def test_eight_day_and_last_minute_samples_remain_until_exact_boundary(self):
        row = {'saved_at': 100.0, 'recorded_at': 1.0}
        for age in (8 * 86400, 13 * 86400 + 23 * 3600 + 59 * 60, RETENTION_SECONDS - .001):
            self.assertTrue(replay_in_window(row, 100 + age))
        self.assertFalse(replay_in_window(row, 100 + RETENTION_SECONDS))
        self.assertFalse(replay_in_window(row, 99))

    def test_original_saved_time_controls_cleanup_and_sidecars_at_exact_boundary(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            row = save_replay(service, '甲', b'expiry-test')
            path = service.replay_root / row['rel_path']
            metadata = [Path(str(path) + '.' + kind + '.json') for kind in ('analysis', 'evidence')]
            for p in metadata:
                p.write_text('{}', encoding='utf-8')
            original = row['saved_at']
            service.clock.now = original + RETENTION_SECONDS - 1
            os.utime(path, (service.clock.now, service.clock.now))
            self.assertEqual(next_cleanup_delay(service.clock.now, service.replay_root,
                             service.deck_dir, service.store.all_replays()), 1)
            self.assertEqual(service.cleanup_old_files(), 0)
            self.assertTrue(path.exists())
            service.clock.now += 1
            self.assertEqual(service.cleanup_old_files(), 1)
            self.assertIsNone(service.store.get_replay(row['id']))
            self.assertFalse(path.exists())
            self.assertTrue(all(not p.exists() for p in metadata))

    def test_duplicate_import_does_not_renew_saved_time(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            row = save_replay(service, '甲', b'same-digest')
            service.clock.now += 8 * 86400
            existing, created = service.store.add_replay(row['digest'], row['focus_player'],
                row['rel_path'], row['players'], row['ranks'], service.clock.now,
                saved_at=service.clock.now)
            self.assertFalse(created)
            self.assertEqual(existing['saved_at'], row['saved_at'])
            service.clock.now = row['saved_at'] + RETENTION_SECONDS
            service.cleanup_old_files()
            self.assertIsNone(service.store.get_replay(row['id']))

    def test_scheduler_includes_decks_and_rechecks_new_files(self):
        with temporary_service() as service:
            now = service.clock.now
            self.assertEqual(next_cleanup_delay(now, service.replay_root, service.deck_dir), 60)
            path = service.deck_dir / 'near-expiry.ydk'
            path.write_text('#main\n1', encoding='utf-8')
            stamp = now - RETENTION_SECONDS + 4
            os.utime(path, (stamp, stamp))
            self.assertEqual(next_cleanup_delay(now, service.replay_root, service.deck_dir), 4)
            service.clock.now += 4
            self.assertEqual(service.cleanup_old_files(), 1)
            self.assertFalse(path.exists())
