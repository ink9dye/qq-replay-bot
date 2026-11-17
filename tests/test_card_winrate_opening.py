import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock, AsyncMock
from bot.card_winrate import _compute, messages
from bot.service import BotService, TextPages


class OpeningWinrateTests(unittest.TestCase):
    def test_opening_is_independent_of_usage_and_reuses_all_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'stats.db'
            with sqlite3.connect(path) as db:
                db.executescript("CREATE TABLE stat_build_cards(build TEXT,code INTEGER,partition TEXT,copies INTEGER);"
                                 "INSERT INTO stat_build_cards VALUES('build',1,'main',3);")
            db.close()
            cases = [('win', 0, 1, 1), ('loss', 1, 0, 0), ('draw', 1, 2, 0),
                     ('win', 1, None, 1), ('win', 0, None, None)]
            rows = [dict(digest=str(i), player='a', build='build', outcome=o, first_player=seat)
                    for i,(o,u,n,seat) in enumerate(cases)]
            facts = {(str(i),'a'):dict(used=u, initial_count=n, drawn=1)
                     for i,(o,u,n,seat) in enumerate(cases)}
            opponents = {r['digest']:[dict(player='b',deck_type='deck')] for r in rows}
            with patch('bot.card_winrate._basis',return_value=rows) as basis, \
                 patch('bot.card_identity.identity',return_value=dict(codes=[1],canonical_card=1)), \
                 patch('bot.card_identity.facts',return_value=facts) as lookup, \
                 patch('bot.card_statistics._opponent_rows',return_value=opponents) as others, \
                 patch('bot.card_winrate._popular_decks',return_value=[dict(deck_type='deck',players=10,share=10)]) as decks:
                result = _compute(path,tmp,0,1)
            for call in (basis,lookup,others,decks):call.assert_called_once()
        used = result['matchups']['base']; opening = result['opening']['matchups']['base']
        self.assertEqual((used['games'],used['wins'],used['losses'],used['draws']), (3,1,1,1))
        self.assertEqual((opening['games'],opening['wins'],opening['losses'],opening['draws']), (2,1,0,1))
        self.assertEqual(opening['rate'],50)
        self.assertEqual(result['opening']['order']['first']['games'],1)
        self.assertEqual(result['opening']['order']['second']['draws'],1)
        self.assertEqual(result['opening']['popular_matchups']['rows'][0]['games'],2)
        result.update(code=1,name='test')
        pages = messages(result)
        self.assertEqual(len(pages),2)
        self.assertIn('200',pages[0]);self.assertIn('200',pages[1])
        self.assertNotEqual(pages[0],pages[1])
        # Even missing opening facts always produce a distinct empty second page.
        self.assertIn('initial_hand',result['opening']['matchups']['unit'])

    def test_service_sends_two_pages_in_order(self):
        service = BotService.__new__(BotService)
        service._statistics_command_lock = None
        service.write_log = Mock()
        service.handle_text = Mock(return_value=TextPages(['used','opening']))
        send = AsyncMock(return_value=True)
        asyncio.run(service._slow_text_job('u','\u5355\u5361\u80dc\u7387 test','','g',send))
        self.assertEqual([c.args for c in send.call_args_list],[('g','used'),('g','opening')])

    def test_reply_wraps_both_messages(self):
        service = BotService.__new__(BotService)
        service.store = Mock(path='database'); service.replay_root = Path('.')
        service.clock = lambda: 1
        with patch('bot.card_winrate.collect',return_value={'test':1}), \
             patch('bot.card_winrate.messages',return_value=['used','opening']):
            reply = service._card_winrate_reply('test')
        self.assertEqual(reply.pages,['used','opening'])
