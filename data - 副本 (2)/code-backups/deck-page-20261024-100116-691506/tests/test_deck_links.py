import base64
import html
import re
import struct
import unittest
from urllib.parse import parse_qs, urlsplit

from bot.deck_links import ourygo_url
from bot.help import help_cards
from bot.replay_window import REPLAY_WINDOW_SECONDS
from bot.watch_core import REPLAY_MAGIC_YRP2, ReplayWriter
from test_bot import link_token, temporary_service


def pack_replay(names, decks):
    body = b''.join(name.encode('utf-16le')[:40].ljust(40, b'\0') for name in names)
    body += struct.pack('<iiii', 8000, 5, 1, 0)
    for main, extra in decks:
        for cards in (main, extra):
            body += struct.pack('<I', len(cards))
            if cards:
                body += struct.pack(f'<{len(cards)}I', *reversed(cards))
    header = struct.pack('<IIIIII', REPLAY_MAGIC_YRP2, 0x1362, 0x10, 0, len(body), 0)
    return header.ljust(80, b'\0') + body


def save_game(service, player, main, extra, opponent='对手'):
    writer = ReplayWriter(service.replay_root, [player])
    writer.on_saved = service.on_replay_saved
    writer.save(pack_replay([player, opponent], [(main, extra), ([999999], [])]), [player, opponent])
    return service.store.replays_for_player(player)[-1]


def deck_url(page):
    matches = re.findall(r'href="(http://deck\.ourygo\.top\?[^"]+)"', page.decode())
    return html.unescape(matches[0]) if matches else None


def decode_url(url):
    query = parse_qs(urlsplit(url).query)
    code = query['d'][0]
    data = base64.urlsafe_b64decode(code + '=' * (-len(code) % 4))
    bits = ''.join(f'{byte:08b}' for byte in data)
    counts = [int(bits[:8], 2), int(bits[8:12], 2), int(bits[12:16], 2)]
    position = 16
    sections = []
    for total in counts:
        cards = []
        for _ in range(total):
            amount = int(bits[position:position + 2], 2)
            card = int(bits[position + 2:position + 29], 2)
            cards += [card] * amount
            position += 29
        sections.append(cards)
    return query, sections


class DeckLinkTests(unittest.TestCase):
    def test_matches_official_sites_javascript_v1_fixture(self):
        url = ourygo_url('雨雨雨1', [1, 1, 2], [10], [4, 4, 11])
        query, sections = decode_url(url)
        # 由站点 ygoUtil.js 的 v1Code 实际运行得到，独立于 Python 实现。
        self.assertEqual(query['d'], ['AhKAAAAKAAAAkAAAFQAAAEQAAAWA'])
        self.assertEqual(query['name'], ['雨雨雨1'])
        self.assertEqual(sections, [[1, 1, 2], [10], [4, 4, 11]])

    def test_custom_ids_do_not_silently_truncate_and_counts_over_three_are_preserved(self):
        url = ourygo_url('玩家', [1] * 5, [10], [])
        self.assertEqual(decode_url(url)[1], [[1] * 5, [10], []])
        query = parse_qs(urlsplit(ourygo_url('玩家', [1 << 28], [], [])).query)
        self.assertEqual(query['v'], ['0'])
        self.assertEqual(query['main'], [str(1 << 28)])

    def test_directory_gives_latest_allowed_players_observed_deck_without_a_new_command(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 雨雨雨1 全部 静默')
            save_game(service, '雨雨雨1', [1, 1, 2], [10])
            service.clock.now += 10
            save_game(service, '雨雨雨1', [3, 4, 4], [11])
            token = link_token(service.handle_text('10001', '录像 雨雨雨1'))
            status, _, body = service.share.page(token, 'dir')
            self.assertEqual(status, 200)
            query, sections = decode_url(deck_url(body))
            self.assertEqual(query['name'], ['雨雨雨1'])
            self.assertEqual(sections, [[3, 4, 4], [11], []])
            self.assertIn('完整副卡组未记录'.encode(), body)
            self.assertIn('来源：'.encode(), body)

    def test_snapshot_does_not_leak_later_replays_or_other_players_decks(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            service.handle_text('10001', '订阅 乙 全部 静默')
            save_game(service, '甲', [1, 1, 2], [10])
            old = link_token(service.handle_text('10001', '录像 甲'))
            service.clock.now += 10
            save_game(service, '甲', [4, 5], [20])
            save_game(service, '乙', [8, 9], [30])
            new = link_token(service.handle_text('10001', '录像 甲'))
            other = link_token(service.handle_text('10001', '录像 乙'))
            self.assertEqual(decode_url(deck_url(service.share.page(old, 'dir')[2]))[1][0], [1, 1, 2])
            self.assertEqual(decode_url(deck_url(service.share.page(new, 'dir')[2]))[1][0], [4, 5])
            self.assertEqual(decode_url(deck_url(service.share.page(other, 'dir')[2]))[1][0], [8, 9])

    def test_deck_disappears_at_seven_days_and_expired_five_hour_page_reveals_nothing(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            start = service.clock.now
            save_game(service, '甲', [1, 2], [10])
            service.clock.now += REPLAY_WINDOW_SECONDS - 3600
            token = link_token(service.handle_text('10001', '录像 甲'))
            self.assertIsNotNone(deck_url(service.share.page(token, 'dir')[2]))
            service.clock.now = start + REPLAY_WINDOW_SECONDS
            self.assertIsNone(deck_url(service.share.page(token, 'dir')[2]))
            service.clock.now += 4 * 3600
            status, _, body = service.share.page(token, 'dir')
            self.assertEqual(status, 410)
            self.assertIsNone(deck_url(body))

    def test_unparseable_or_unmatched_player_does_not_borrow_opponents_deck(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            writer = ReplayWriter(service.replay_root, ['甲'])
            writer.on_saved = service.on_replay_saved
            writer.save(pack_replay(['乙', '丙'], [([4], []), ([5], [])]), ['甲', '乙'])
            token = link_token(service.handle_text('10001', '录像 甲'))
            self.assertIsNone(deck_url(service.share.page(token, 'dir')[2]))

    def test_help_examples_all_use_requested_player(self):
        text = '\n'.join(help_cards())
        self.assertNotIn('亚翁', text)
        self.assertIn('订阅 "雨雨雨1" 前100 推送', text)
        self.assertIn('录像 "雨雨雨1"', text)


if __name__ == '__main__':
    unittest.main()
