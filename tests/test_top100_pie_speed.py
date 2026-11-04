import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bot import top100_pie as pie
from bot.statistics_dashboard import deck_images


class PieSpeedTests(unittest.TestCase):
    def test_png_reused_until_distribution_or_art_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload={'pie':{'total':1,'inner':[{'image':123}]},'items':[],'covered':1,'current':100}
            art=Path(tmp)/'art.jpg';art.write_bytes(b'first')
            blob=b'\x89PNG\r\n\x1a\n'+b'fake test content'
            with patch.object(pie,'collect',return_value=payload),patch.object(pie,'_card_file',return_value=art),patch.object(pie,'_render_payload',side_effect=lambda data:(blob,data)) as render:
                self.assertEqual(pie.render(Path(tmp)/'db',tmp,1000)[0],blob)
                self.assertEqual(pie.render(Path(tmp)/'db',tmp,1001)[0],blob)
                self.assertEqual(render.call_count,1)
                art.write_bytes(b'changed art bytes')
                pie.render(Path(tmp)/'db',tmp,1002)
                self.assertEqual(render.call_count,2)
                payload['covered']=2
                pie.render(Path(tmp)/'db',tmp,1003)
                self.assertEqual(render.call_count,3)

    def test_missing_art_only_schedules_shared_downloader(self):
        with patch.object(pie.Path,'is_file',return_value=False),patch('bot.statistics_local_cards.picture',return_value=None) as loader:
            self.assertIsNone(pie._card_file(123))
            loader.assert_called_once()

    def test_representative_sql_filters_before_aggregation_without_changing_ties(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        db.executescript('CREATE TABLE stat_builds(fingerprint,deck_type); CREATE TABLE stat_build_cards(build,partition,code,copies);')
        db.executemany('INSERT INTO stat_builds VALUES(?,?)',[('a','wanted'),('b','unrelated'),('c',None),('d','')])
        db.executemany('INSERT INTO stat_build_cards VALUES(?,?,?,?)',[('a','main',2,3),('a','main',1,3),('b','main',9,999),('c','main',5,1),('d','main',5,2)])
        sql=[];db.set_trace_callback(sql.append)
        self.assertEqual(deck_images(db,{'wanted','未识别'}),{'wanted':1,'未识别':5})
        self.assertIn(' AND (b.deck_type IN ',sql[-1])
