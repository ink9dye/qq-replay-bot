import json,sqlite3,threading,unittest
from unittest.mock import patch
from urllib.request import build_opener,HTTPCookieProcessor
from http.cookiejar import CookieJar
from bot.statistics_server import create_server
import test_card_statistics as C


class StatisticsServerTests(unittest.TestCase):
    def test_standalone_server_reads_existing_data_without_store_or_watch_start(self):
        C.CardStatisticsTests.setUp(self)
        db=sqlite3.connect(self.root/'bot.sqlite3');self.store._db.backup(db);db.close()
        (self.root/'config.json').write_text(json.dumps(dict(data_dir=str(self.root),replay_dir=str(self.replays))),encoding='utf8')
        before=(self.root/'bot.sqlite3').read_bytes()
        with patch('bot.store.Store.__init__',side_effect=AssertionError('No migration Store allowed')):
            server=create_server(self.root,0)
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_address[1]);opener=build_opener(HTTPCookieProcessor(CookieJar()))
        with opener.open(base+'/health') as r:health=json.load(r)
        self.assertEqual(health['service'],'qq-statistics-web');self.assertFalse(health['starts_watch'])
        secret=(self.root/'statistics-owner.key').read_text(encoding='ascii')
        with opener.open(base+'/statistics/access/'+secret) as r:self.assertEqual(r.status,200)
        with opener.open(base+'/statistics/api/roster') as r:self.assertEqual(json.load(r)['covered_players'],1)
        self.assertEqual((self.root/'bot.sqlite3').read_bytes(),before)
