import hashlib,json,tempfile,unittest
from pathlib import Path
from bot.store import Store
from bot.card_identity import identity,verify_alias
from test_deck_links import pack_replay
from test_top100 import roster

NOW=1800000000


class CardIdentityTests(unittest.TestCase):
    def test_normal_monster_flavor_text_can_differ_but_token_restrictions_cannot(self):
        row=dict(name='同名卡',description='叙述甲',type=0x11,atk=1000,**{'def':1000},level=4,race=1,attribute=1,setcode=0)
        other=dict(row,description='叙述乙')
        self.assertIsNone(verify_alias(row,other))
        row['type']=other['type']=0x4011
        self.assertEqual(verify_alias(row,other),'different_effect_text')
        other=dict(row,name='另一张卡')
        self.assertEqual(verify_alias(row,other),'different_printed_name')

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.replays=self.root/'replays';self.replays.mkdir()
        self.store=Store(self.root/'db.sqlite3');self.addCleanup(self.store.close)
        self.store.replace_top100(roster(['甲']),NOW)
        (self.root/'card-equivalence.json').write_text(json.dumps(dict(version=1,reference_version='tested',groups=[dict(canonical=1,codes=[1,7])])),encoding='utf-8')
        path=self.replays/'game.yrp';blob=pack_replay(['甲','乙'],[([1,7,2],[9]),([3,4],[8])]);path.write_bytes(blob)
        digest=hashlib.sha256(blob).hexdigest()
        row,_=self.store.add_replay(digest,'甲',path.name,['甲','乙'],{},NOW-1,saved_at=NOW-1)
        report=dict(version=4,digest=digest,winner=0,result_source='live_server',playback='completed',first_player=0,
            players=[dict(name='甲',main=[1,7,2],extra=[9],draw_record_complete=True,initial_draws_accounted=True,
                drawn_cards=[dict(code=1,draw_count=1,initial_count=1,used_after_draw=False)],used_cards=[dict(code=7,events=1)]),
                dict(name='乙',main=[3,4],extra=[8],drawn_cards=[],used_cards=[],draw_record_complete=True,initial_draws_accounted=True)])
        self.store.import_replay_facts(row,path,report,NOW)

    def test_art_variants_merge_copies_and_one_small_game_win_sample(self):
        normal=self.store.card_statistics(self.replays,1,'main',NOW)
        art=self.store.card_statistics(self.replays,7,'main',NOW)
        self.assertEqual(normal['metrics'],art['metrics'])
        self.assertEqual(art['card'],1)
        self.assertEqual(art['metrics']['drawn_win_rate']['denominator'],1)
        self.assertEqual(art['metrics']['used_win_rate']['wins'],1)
        people=self.store.player_card_statistics(self.replays,7,NOW)
        self.assertEqual(people['people'][0]['copies']['main'],2)
        bins=people['observed_distributions']['main']['bins']
        self.assertEqual(next(b['players'] for b in bins if b['copies']==2),1)

    def test_cross_variant_use_after_draw_order_is_not_guessed(self):
        metric=self.store.card_statistics(self.replays,1,'main',NOW)['metrics']['use_after_draw_rate']
        self.assertEqual(metric['denominator'],0)
        self.assertEqual(metric['unknown_samples'],1)

    def test_unverified_alias_or_invalid_reference_keeps_raw_identity(self):
        self.assertEqual(identity(self.store._db,3)['codes'],[3])
        (self.root/'card-equivalence.json').write_text(json.dumps(dict(version=1,groups=[dict(canonical=1,codes=[1,7]),dict(canonical=2,codes=[2,7])])),encoding='utf-8')
        result=identity(self.store._db,7)
        self.assertEqual(result['codes'],[7])
        self.assertEqual(result['status'],'invalid_reference_exact_only')
