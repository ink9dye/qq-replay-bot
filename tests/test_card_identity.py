import hashlib,json,tempfile,unittest
from pathlib import Path
from bot.store import Store
from bot.card_identity import identity,verify_alias
from test_deck_links import pack_replay
from test_top100 import roster

NOW=1800000000


class CardIdentityTests(unittest.TestCase):
    def test_retired_reference_rules_cannot_write_labels_or_renew_facts(self):
        expires=self.store._db.execute('SELECT expires_at FROM stat_games').fetchone()[0]
        with self.assertRaisesRegex(RuntimeError, 'legacy_similarity_naming_removed'):
            self.store.refresh_classifications(self.root/'references.json')
        self.assertEqual(self.store._db.execute('SELECT expires_at FROM stat_games').fetchone()[0], expires)
        self.assertIsNone(self.store._db.execute('SELECT deck_type FROM stat_builds LIMIT 1').fetchone()[0])

    def test_normal_monster_flavor_text_can_differ_but_token_restrictions_cannot(self):
        row=dict(name='同名卡',description='叙述甲',type=0x11,atk=1000,**{'def':1000},level=4,race=1,attribute=1,setcode=0)
        other=dict(row,description='叙述乙')
        self.assertIsNone(verify_alias(row,other))
        row['type']=other['type']=0x1000011
        self.assertEqual(verify_alias(row,other),'different_effect_text')
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

    def test_old_other_partition_alias_draw_counts_for_carried_identity_without_duplicates(self):
        db=self.store._db
        db.execute("DELETE FROM stat_build_cards WHERE partition='main' AND code=1")
        db.execute("UPDATE stat_card_facts SET partition='other',copies=0 WHERE player='甲' AND code=1")
        db.commit()
        metric=self.store.card_statistics(self.replays,7,'main',NOW)['metrics']
        self.assertEqual(metric['drawn_win_rate']['denominator'],1)
        from bot.card_identity import facts
        merged=next(iter(facts(db,[1,7],'main').values()))
        self.assertEqual(merged['draw_count'],1)
        row=dict(db.execute("SELECT * FROM stat_card_facts WHERE player='甲' AND code=1").fetchone());row['partition']='main'
        columns=list(row);db.execute('INSERT INTO stat_card_facts('+','.join(columns)+') VALUES('+','.join('?'for _ in columns)+')',[row[c]for c in columns]);db.commit()
        self.assertEqual(next(iter(facts(db,[1,7],'main').values()))['draw_count'],1)

    def test_new_report_alias_draw_stored_in_carried_partition_preserving_raw_cards(self):
        path=self.replays/'alias-draw.yrp';blob=pack_replay(['甲','乙'],[([7,2],[9]),([3,4],[8])]);path.write_bytes(blob)
        digest=hashlib.sha256(blob).hexdigest();row,_=self.store.add_replay(digest,'甲',path.name,['甲','乙'],{},NOW,saved_at=NOW)
        report=dict(version=4,digest=digest,playback='completed',winner=0,result_source='live_server',players=[dict(name='甲',main=[7,2],extra=[9],draw_record_complete=True,initial_draws_accounted=True,drawn_cards=[dict(code=1,draw_count=1,initial_count=1)],used_cards=[],extra_appearances=[]),dict(name='乙',main=[3,4],extra=[8],draw_record_complete=True,drawn_cards=[],used_cards=[],extra_appearances=[])])
        self.store.import_replay_facts(row,path,report,NOW)
        db=self.store._db;fact=db.execute("SELECT * FROM stat_card_facts WHERE digest=? AND player='甲' AND partition='main' AND code=1",(digest,)).fetchone()
        self.assertEqual((fact['copies'],fact['drawn'],fact['draw_count']),(0,1,1))
        build=db.execute("SELECT build FROM stat_views WHERE digest=? AND player='甲'",(digest,)).fetchone()[0]
        self.assertEqual({r[0]for r in db.execute("SELECT code FROM stat_build_cards WHERE build=? AND partition='main'",(build,))},{7,2})
        self.assertEqual(self.store.card_statistics(self.replays,7,'main',NOW)['metrics']['drawn_win_rate']['denominator'],2)

    def test_unverified_alias_or_invalid_reference_keeps_raw_identity(self):
        self.assertEqual(identity(self.store._db,3)['codes'],[3])
        (self.root/'card-equivalence.json').write_text(json.dumps(dict(version=1,groups=[dict(canonical=1,codes=[1,7]),dict(canonical=2,codes=[2,7])])),encoding='utf-8')
        result=identity(self.store._db,7)
        self.assertEqual(result['codes'],[7])
        self.assertEqual(result['status'],'invalid_reference_exact_only')
