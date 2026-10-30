import os
import unittest
import zipfile
from collections import Counter
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from bot.match_history import build_match_groups, recover_legacy_sessions
from bot.watch_core import ReplayWriter, Spectator, STOC_REPLAY, render_ydk
from test_bot import temporary_service, link_token, room
from test_deck_links import save_game, pack_replay, deck_url, decode_url


def register(service, session, rows):
    service.store.record_session(session, [(index, row['digest']) for index, row in enumerate(rows, 1)])


def query_side(service, player='甲'):
    token = link_token(service.handle_text('10001', '录像 ' + player))
    body = service.share.page(token, 'dir')[2]
    return Counter(decode_url(deck_url(body))[1][2]), body


class MatchHistoryTests(unittest.TestCase):
    def setUp(self):
        patcher = patch('bot.share.arena_points', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_side_uses_first_game_baseline_and_later_games_like_watch(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            first = save_game(service, '甲', [1, 1, 2], [10])
            second = save_game(service, '甲', [1, 4, 4], [10, 11])
            register(service, 'match-one', [first, second])
            side, body = query_side(service)
            self.assertEqual(side, Counter([4, 4, 11]))
            self.assertEqual(decode_url(deck_url(body))[1][:2], [[1, 1, 2], [10]])
            self.assertEqual(body.count(b'<article class="match">'), 1)
            self.assertIn('第 1 局'.encode(), body)
            self.assertIn('第 2 局'.encode(), body)

    def test_history_supplements_only_identical_main_multiset_and_never_sums_duplicates(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            one = save_game(service, '甲', [2, 1, 1], [10])
            two = save_game(service, '甲', [1, 4, 4], [10, 11])
            register(service, 'old-same-main', [one, two])
            service.clock.now += 10
            bad1 = save_game(service, '甲', [9, 9], [10])
            bad2 = save_game(service, '甲', [8, 9], [10])
            register(service, 'different-main', [bad1, bad2])
            service.clock.now += 10
            new1 = save_game(service, '甲', [1, 1, 2], [10])
            new2 = save_game(service, '甲', [1, 4, 5], [10])
            register(service, 'latest-same-main', [new1, new2])
            side, body = query_side(service)
            self.assertEqual(side, Counter([4, 4, 5, 11]))
            self.assertNotIn(8, side)
            self.assertIn('完全相同的 1 场'.encode(), body)

    def test_same_names_in_distinct_matches_are_not_merged_and_snapshot_cannot_see_later_games(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            first = save_game(service, '甲', [1, 1, 2], [10])
            register(service, 'first-match', [first])
            token = link_token(service.handle_text('10001', '录像 甲'))
            second = save_game(service, '甲', [1, 4, 4], [10])
            register(service, 'first-match', [first, second])
            self.assertEqual(decode_url(deck_url(service.share.page(token, 'dir')[2]))[1][2], [])
            service.clock.now += 10
            third = save_game(service, '甲', [9, 9], [20])
            register(service, 'separate-match', [third])
            side, body = query_side(service)
            self.assertEqual(side, Counter())
            self.assertEqual(body.count(b'<article class="match">'), 2)

    def test_legacy_watch_ydk_recovers_only_an_unambiguous_matching_batch(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 乙 全部 静默')
            foreign = save_game(service, '乙', [8, 8], [30])
            service.handle_text('10001', '订阅 甲 全部 静默')
            first = save_game(service, '甲', [1, 1, 2], [10])
            second = save_game(service, '甲', [1, 4, 4], [10, 11])
            ydk = service.data_dir / 'decks/12-00 甲 测试.ydk'
            ydk.write_text(render_ydk([1, 1, 2], [10], [4, 4, 11]), encoding='utf-8')
            os.utime(ydk, (service.clock.now + 30, service.clock.now + 30))
            self.assertEqual(recover_legacy_sessions(service.store, service.replay_root, ydk.parent), 1)
            self.assertEqual(set(service.store.session_map('甲')), {first['id'], second['id']})
            self.assertEqual(service.store.session_map('乙'), {})
            self.assertEqual(query_side(service)[0], Counter([4, 4, 11]))
            self.assertEqual(recover_legacy_sessions(service.store, service.replay_root, ydk.parent), 0)

    def test_unverified_legacy_ydk_cannot_inject_side(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            save_game(service, '甲', [1, 1, 2], [10])
            ydk = service.data_dir / 'decks/12-00 甲 测试.ydk'
            ydk.write_text(render_ydk([1, 1, 2], [10], [9999]), encoding='utf-8')
            os.utime(ydk, (service.clock.now + 30, service.clock.now + 30))
            self.assertEqual(recover_legacy_sessions(service.store, service.replay_root, ydk.parent), 0)
            self.assertEqual(query_side(service)[0], Counter())

    def test_web_matchup_and_download_names_contain_both_archetypes(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            replay = save_game(service, '甲', [1, 1, 2], [10])
            token = link_token(service.handle_text('10001', '录像 甲'))
            def title(main, extra, cache):
                return '耀圣' if 1 in main else '影依'
            with patch('bot.deck_links.observed_title', side_effect=title):
                body = service.share.page(token, 'dir')[2]
                self.assertIn('甲 · 耀圣 vs 对手 · 影依'.encode(), body)
                filename = service.share.download_name(replay)
                self.assertIn('【耀圣】', filename)
                self.assertIn('【影依】', filename)
                _, _, packed = service.share.page(token, 'zip')
                with zipfile.ZipFile(BytesIO(packed)) as archive:
                    self.assertEqual(archive.namelist(), [filename])

    def test_spectator_records_real_session_and_deduplicates_reconnect_payloads(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            writer = ReplayWriter(service.replay_root, ['甲'])
            writer.on_saved = service.on_replay_saved
            writer.on_session = service.store.record_session
            spectator = Spectator(service.watch_config(), room('test-room', ['甲', '对手'])[0], writer)
            first = pack_replay(['甲', '对手'], [([1, 1, 2], [10]), ([999999], [])])
            second = pack_replay(['甲', '对手'], [([1, 4, 4], [10]), ([999999], [])])
            spectator.handle(STOC_REPLAY, first)
            spectator.handle(STOC_REPLAY, second)
            spectator.handle(STOC_REPLAY, second)
            self.assertEqual(len(spectator.observed_games), 2)
            self.assertEqual(len({row['session_id'] for row in service.store.session_map('甲').values()}), 1)
            self.assertEqual(query_side(service)[0], Counter([4, 4]))

    def test_history_cannot_cross_users_subscription_time_or_the_seven_day_window(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            first = save_game(service, '甲', [2, 1, 1], [10])
            second = save_game(service, '甲', [1, 4, 4], [10])
            register(service, 'historical', [first, second])
            service.clock.now += 10
            service.handle_text('10002', '订阅 甲 全部 静默')
            latest = save_game(service, '甲', [1, 1, 2], [10])
            register(service, 'latest', [latest])
            token = link_token(service.handle_text('10002', '录像 甲'))
            self.assertEqual(decode_url(deck_url(service.share.page(token, 'dir')[2]))[1][2], [])
            self.assertEqual(query_side(service)[0], Counter([4, 4]))
            service.clock.now += 7 * 86400 - 5
            self.assertEqual(query_side(service)[0], Counter())

    def test_session_grouping_survives_an_unparseable_deck(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            writer = ReplayWriter(service.replay_root, ['甲'])
            writer.on_saved = service.on_replay_saved
            writer.on_session = service.store.record_session
            spectator = Spectator(service.watch_config(), room('test-room', ['甲', '对手'])[0], writer)
            spectator.handle(STOC_REPLAY, b'unparsed-game-one')
            spectator.handle(STOC_REPLAY, b'unparsed-game-two')
            mapping = service.store.session_map('甲')
            self.assertEqual(len(mapping), 2)
            self.assertEqual({entry['game_no'] for entry in mapping.values()}, {1, 2})
            self.assertEqual(len({entry['session_id'] for entry in mapping.values()}), 1)


if __name__ == '__main__':
    unittest.main()
