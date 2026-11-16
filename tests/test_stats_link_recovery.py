from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bot.service import BotService
from bot.store import Store
from bot.share import ShareApp, ShareLinkUnavailable


class StatsLinkRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'bot.sqlite3')
        self.addCleanup(self.store.close)
        self.service = object.__new__(BotService)
        self.service.config = {'share':{'stats_link_hours':2}}
        self.service.store = self.store
        self.service.share = ShareApp(self.store,self.root/'replays',lambda:1000,
                                     public_base_url='https://stats.example')
        self.service._wait_public_url = lambda:True
        self.logs=[]
        self.service.write_log=self.logs.append

    def test_healthy_connection_keeps_existing_path(self):
        with patch('bot.service.Store',side_effect=AssertionError('unneeded recovery')):
            result=self.service._stats_text('123')
        self.assertIn('https://stats.example/stats/',result)
        self.assertEqual(self.logs,[])

    def test_closed_shared_connection_recovers_without_replacing_store(self):
        self.store.close()
        result=self.service._stats_text('123')
        self.assertIn('https://stats.example/stats/',result)
        self.assertIs(self.service.store,self.store)
        with closing(sqlite3.connect(self.root/'bot.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT user_id,expires_at FROM stats_links').fetchone(),('123',8200))
        self.assertIn('AttributeError',self.logs[0])

    def test_chained_busy_failure_recovers_and_reuses_link(self):
        original=self.service.share.create_stats_link('123')
        error=ShareLinkUnavailable('secret URL must not be logged')
        error.__cause__=sqlite3.OperationalError('private token')
        with patch.object(self.service.share,'create_stats_link',side_effect=error):
            result=self.service._stats_text('123')
        self.assertIn(original['url'],result)
        self.assertIn('OperationalError',self.logs[0])
        self.assertNotIn('secret',self.logs[0]);self.assertNotIn('private',self.logs[0])
        with closing(sqlite3.connect(self.root/'bot.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM stats_links').fetchone()[0],1)

    def test_recovery_failure_is_logged_without_exception_text(self):
        with patch.object(self.service.share,'create_stats_link',side_effect=ValueError('secret')), \
             patch('bot.service.Store',side_effect=RuntimeError('credential')):
            result=self.service._stats_text('123')
        self.assertIn('暂时生成失败',result)
        self.assertEqual(len(self.logs),2)
        self.assertNotIn('secret',''.join(self.logs));self.assertNotIn('credential',''.join(self.logs))

    def test_recovery_preserves_public_url_provider(self):
        self.service.share.public_url_provider=lambda:'https://provider.example'
        self.store.close()
        result=self.service._stats_text('123')
        self.assertIn('https://provider.example/stats/',result)


if __name__=='__main__': unittest.main()
