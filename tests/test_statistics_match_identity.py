import unittest,json,sqlite3
from bot.statistics_match_identity import inherit_first_game_types

class MatchIdentityTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row;self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE stat_evidence(digest,payload_json)');self.db.execute('CREATE TABLE stat_views(digest,player)')
    def row(self,digest,player,label,session,number,pair=('甲','乙')):
        self.db.execute('INSERT INTO stat_evidence VALUES(?,?)',(digest,json.dumps(dict(capture=dict(session_id=session,game_no=number,captured_from_start=True)))))
        self.db.executemany('INSERT INTO stat_views VALUES(?,?)',[(digest,p)for p in pair])
        return dict(digest=digest,player=player,deck_type=label)
    def test_post_side_uses_own_first_game_and_does_not_change_cards(self):
        first=self.row('a','甲','首局类型','s',1)
        side=self.row('b','甲','换边误判','s',2);side['build']='post-side-cards'
        inherit_first_game_types(self.db,[side,first])
        self.assertEqual(side['deck_type'],'首局类型');self.assertEqual(side['observed_deck_type'],'换边误判')
        self.assertEqual(side['classification_source_digest'],'a');self.assertEqual(side['build'],'post-side-cards')
    def test_missing_first_or_same_session_different_opponent_never_borrows(self):
        first=self.row('a','甲','不能借用','s',1)
        side=self.row('b','甲','换边类型','s',2,('甲','丙'))
        inherit_first_game_types(self.db,[first,side]);self.assertIsNone(side['deck_type'])
        self.assertEqual(side['classification_source'],'post_side_first_game_missing')
    def test_ambiguous_first_game_and_unknown_first_never_use_side_guess(self):
        first=self.row('a','甲',None,'s',1);side=self.row('b','甲','换边类型','s',2)
        inherit_first_game_types(self.db,[first,side]);self.assertIsNone(side['deck_type'])
        other=self.row('c','甲','另一首局','s',1)
        inherit_first_game_types(self.db,[first,other,side]);self.assertIsNone(side['deck_type'])
