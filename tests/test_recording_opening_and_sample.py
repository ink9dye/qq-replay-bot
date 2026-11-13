import unittest
from unittest.mock import patch
from recording_filter_fixture import set_up
import test_card_statistics as C
from bot.statistics_recording_filters import query
from bot.statistics_samples import details
from bot.statistics_build_detail import query as legacy_build

class OpeningAndSampleTests(unittest.TestCase):
    setUp=set_up

    def test_opening_default_differs_from_later_draws_and_unknown_is_not_zero(self):
        db=self.store._db
        db.execute('UPDATE stat_card_facts SET initial_count=0 WHERE digest=? AND code=1',
                   (self.rows[1]['digest'],))
        opening=query(db,self.replays,C.NOW,{'card':['1']})
        drawn=query(db,self.replays,C.NOW,{'card':['1'],'mode':['drawn']})
        self.assertEqual(opening['total'],1)
        self.assertEqual(drawn['total'],3)
        self.assertEqual(opening['filter_reasons']['initial_evidence_missing'],1)
        db.execute('UPDATE stat_card_facts SET initial_count=1 WHERE digest=? AND code=3',
                   (self.rows[0]['digest'],))
        self.assertEqual(query(db,self.replays,C.NOW,{'opponent_card':['3']})['total'],1)

    def test_sample_name_cards_and_download_bytes_preserved_without_build_report(self):
        db=self.store._db;digest=self.rows[0]['digest'];player=next(iter(db.execute(
            'SELECT player FROM stat_current_top100 ORDER BY rank')))[0]
        old=legacy_build(db,self.replays,C.NOW,player,digest)
        name=next(r for r in old['matches'] if r['digest']==digest)
        with patch('bot.statistics_build_detail.query',side_effect=AssertionError('heavy report called')):
            data,file=details(db,self.replays,C.NOW,player,digest)
            download,source=details(db,self.replays,C.NOW,player,digest,file_only=True)
        self.assertEqual(data['recording_title'],name['recording_title'])
        self.assertEqual(data['download_filename'],name['download_filename'])
        self.assertEqual(download['download_filename'],data['download_filename'])
        self.assertEqual(source.read_bytes(),file.read_bytes())
        self.store.set_fact_exclusion(digest,player,reason='test')
        self.assertIsNone(details(db,self.replays,C.NOW,player,digest))
