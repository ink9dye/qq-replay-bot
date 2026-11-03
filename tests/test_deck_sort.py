import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from bot.deck_sort import load_card_data, sort_key, sorted_cards
from bot.deck_image import _sections
from bot.statistics_build_detail import infer_side


class DeckSortTests(unittest.TestCase):
    def test_requested_global_monster_levels_attack_defense_code(self):
        info = {
            1: dict(type=0x11, level=1, attack=0, defense=0),
            2: dict(type=0x21, level=8, attack=2000, defense=0),
            3: dict(type=0x21, level=4, attack=3000, defense=0),
            4: dict(type=0x21, level=4, attack=2000, defense=2000),
            5: dict(type=0x21, level=4, attack=2000, defense=1000),
            6: dict(type=0x21, level=4, attack=2000, defense=1000),
            7: dict(type=2, level=0, attack=0, defense=0),
            8: dict(type=0x10002, level=0, attack=0, defense=0),
            9: dict(type=4, level=0, attack=0, defense=0),
            10: dict(type=0x100004, level=0, attack=0, defense=0)}
        cards=[dict(code=i, partition='main', name=str(20-i), copies=1) for i in range(10,0,-1)]
        self.assertEqual([c['code'] for c in sorted_cards(cards,info)],[2,3,4,5,6,1,7,8,9,10])
        self.assertEqual([c['code'] for c in _sections(cards,info)['main']],[2,3,4,5,6,1,7,8,9,10])

    def test_extra_monster_types_and_missing_metadata_are_stable(self):
        info={code:dict(type=kind,level=5,attack=1000,defense=0) for code,kind in [(1,0x41),(2,0x2001),(3,0x800001),(4,0x4000001)]}
        cards=[dict(code=i,partition='extra') for i in [99,4,3,2,1]]
        self.assertEqual([c['code'] for c in sorted_cards(cards,info)],[1,2,3,4,99])

    def test_targeted_local_read_level_mask_link_def_and_no_cache(self):
        with tempfile.TemporaryDirectory() as d:
            data=Path(d);root=data/'ygopro';root.mkdir();(root/'expansions').mkdir()
            (data/'card-library.json').write_text(json.dumps(dict(ygopro_root=str(root))))
            path=root/'cards.cdb'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE datas(id INTEGER PRIMARY KEY,type,level,atk,def)')
                db.executemany('INSERT INTO datas VALUES(?,?,?,?,?)',[(1,0x1000021,(8<<24)|4,2000,1000),(2,0x4000001,3,2300,0x100),(3,2,0,0,0)])
                db.commit()
            before=path.read_bytes();info=load_card_data(data,[1,2])
            self.assertEqual(set(info),{1,2});self.assertEqual(info[1]['level'],4);self.assertEqual(info[2]['defense'],0)
            self.assertEqual(path.read_bytes(),before)
            with closing(sqlite3.connect(path)) as db:
                db.execute('UPDATE datas SET level=8 WHERE id=1');db.commit()
            self.assertEqual(load_card_data(data,[1])[1]['level'],8)


class SidePerspectiveTests(unittest.TestCase):
    def fixture(self):
        rows=[dict(digest='first',build='base',main_complete=True,extra_complete=True,side_complete=False,_pair=('p','opponent')),
              dict(digest='second',build='sided',main_complete=True,extra_complete=True,side_complete=False,_pair=('p','opponent'))]
        for i,r in enumerate(rows):r.update(recorded_at=i+1,expires_at=100)
        cards={'base':[dict(partition='main',code=1,copies=3),dict(partition='extra',code=9,copies=1)],
               'sided':[dict(partition='main',code=1,copies=1),dict(partition='main',code=2729965,copies=2),dict(partition='extra',code=10,copies=1)]}
        metadata={'first':('s',1,True),'second':('s',2,True)}
        return rows,cards,metadata

    def test_latest_first_build_has_no_cosmic_in_main_and_two_in_side(self):
        rows,cards,metadata=self.fixture()
        r=infer_side(rows,rows[0],cards,metadata)
        self.assertEqual({c['code']:c['copies'] for c in r['cards']},{2729965:2,10:1})
        self.assertNotIn(2729965,[c['code'] for c in cards[rows[0]['build']]])

    def test_historical_sided_build_does_not_duplicate_swapped_in_cards(self):
        rows,cards,metadata=self.fixture()
        r=infer_side(rows,rows[1],cards,metadata)
        self.assertEqual({c['code']:c['copies'] for c in r['cards']},{1:2,9:1})
        self.assertNotIn(2729965,[c['code'] for c in r['cards']])

class BuildValidationTests(unittest.TestCase):
    def test_four_physical_copies_are_reported_without_rewriting_facts(self):
        from bot.statistics_build_detail import validate_build
        cards=[dict(code=2729965,partition='main',copies=2)]
        side=[dict(code=2729965,copies=2)]
        result=validate_build(cards,side)
        self.assertEqual(result['issues'][0]['kind'],'copy_overflow')
        self.assertEqual(result['issues'][0]['copies'],4)
        self.assertEqual((cards[0]['copies'],side[0]['copies']),(2,2))
        self.assertFalse(result['banlist_checked'])
        self.assertEqual(result['banlist_status'],'unverified_missing_version')
        self.assertIn('禁限表未校验',result['summary'])
        self.assertIn('卡号2729965',result['summary'])

    def test_direct_and_inferred_side_are_not_added_twice(self):
        from bot.statistics_build_detail import validate_build
        self.assertEqual(validate_build([dict(code=1,partition='side',copies=2)],[dict(code=1,copies=2)])['issues'],[])

    def test_verified_alternate_codes_and_extra_count_together(self):
        from bot.statistics_build_detail import validate_build
        r=validate_build([dict(code=1,partition='extra',copies=2)],[dict(code=2,copies=2)],{2:(1,None)})
        self.assertEqual(r['issues'][0]['copies'],4)

    def test_partial_main_is_not_claimed_to_be_below_minimum(self):
        from bot.statistics_build_detail import validate_build
        cards=[dict(code=1,partition='main',copies=3)]
        self.assertEqual(validate_build(cards,completeness={'main':False})['issues'],[])
        short=validate_build(cards,completeness={'main':True})
        self.assertEqual(short['issues'][0]['kind'],'main_below_minimum')
        self.assertEqual(short['status'],'warning')
        self.assertFalse(short['blocking'])

    def test_passing_summary_still_marks_banlist_unverified(self):
        from bot.statistics_build_detail import validate_build
        r=validate_build([dict(code=1,partition='main',copies=3)])
        self.assertEqual(r['status'],'no_basic_conflict')
        self.assertIn('实体张数检查通过',r['summary'])
        self.assertIn('禁限表未校验（缺少对应版本）',r['summary'])

    def test_conflicting_inference_is_blocked_but_direct_record_still_exports(self):
        from bot.statistics_build_detail import ydk, validate_build
        cards=[dict(code=2729965,partition='main',copies=2)]
        inferred=[dict(code=2729965,copies=2)]
        validation=validate_build(cards,inferred)
        result=dict(cards=cards,inferred_side=dict(cards=inferred,conflict=validation['blocking']))
        with self.assertRaises(ValueError):
            ydk(result,True)
        direct=ydk(result,False).splitlines()
        self.assertEqual(direct.count('2729965'),2)
        self.assertEqual(direct[direct.index('!side')+1:],[])

class SideSelectionTests(unittest.TestCase):
    def test_g3_additions_take_max_not_sum(self):
        rows=[];cards={};metadata={}
        for game in (1,2,3):
            digest=f'g{game}';build=digest
            rows.append(dict(digest=digest,build=build,main_complete=True,extra_complete=True,side_complete=False,_pair=('p','opponent'),recorded_at=game,expires_at=1000))
            metadata[digest]=('s',game,True)
            counts={1:3}
            if game==2:counts[2729965]=2
            if game==3:counts[2729965]=1
            cards[build]=[dict(partition='main',code=code,copies=n) for code,n in counts.items()]
        r=infer_side(rows,rows[0],cards,metadata)
        self.assertEqual({c['code']:c['copies'] for c in r['cards']},{2729965:2})
        self.assertEqual(r['observed_cards'][0]['copies'],2)

    def test_history_is_merged_only_when_first_main_and_extra_match(self):
        rows=[];cards={};metadata={}
        def add(session,game,code=None,extra=None):
            digest=f'{session}-{game}';build=digest
            rows.append(dict(digest=digest,build=build,main_complete=True,extra_complete=True,side_complete=False,_pair=('p','opponent'),recorded_at=game,expires_at=1000))
            metadata[digest]=(session,game,True)
            cards[build]=[dict(partition='main',code=1,copies=3),dict(partition='extra',code=9,copies=1)]
            if code:cards[build].append(dict(partition='main',code=code,copies=1))
            if extra:cards[build].append(dict(partition='extra',code=extra,copies=1))
        add('same',1);add('same',2,2729965)
        add('other',1);add('other',2,38817296)
        add('changed',1,500);add('changed',2,98476659)
        r=infer_side(rows,rows[0],cards,metadata)
        self.assertEqual({c['code']:c['copies'] for c in r['cards']},{2729965:1,38817296:1})
        kinds={c['code']:{x['kind'] for x in c['sources']} for c in r['cards']}
        self.assertEqual(kinds[2729965],{'same_match'})
        self.assertEqual(kinds[38817296],{'historical_same_build'})
        self.assertNotIn(98476659,[c['code'] for c in r['cards']])

class PopulationSelectionTests(unittest.TestCase):
    def test_latest_match_uses_its_first_game(self):
        from bot.statistics_population import latest_views
        def row(digest,game,recorded):
            return dict(player='p',digest=digest,recorded_at=recorded,received_at=recorded,
                        observed_session_id='s',observed_match_players=('o','p'),observed_game_no=game)
        views=[row('s-2',2,20),row('s-1',1,10),row('old-2',2,5),row('old-1',1,4)]
        self.assertEqual(latest_views({'views':views})['p']['digest'],'s-1')

    def test_same_session_id_with_another_opponent_is_not_one_match(self):
        from bot.statistics_population import latest_views
        def row(digest,game,recorded,opponent):
            return dict(player='p',digest=digest,recorded_at=recorded,received_at=recorded,
                        observed_session_id='s',observed_match_players=('p',opponent),observed_game_no=game)
        views=[row('a-2',2,20,'o1'),row('a-1',1,10,'o1'),row('b-1',1,30,'o2')]
        self.assertEqual(latest_views({'views':views})['p']['digest'],'b-1')

    def test_without_session_evidence_the_latest_observation_is_kept(self):
        from bot.statistics_population import latest_views
        views=[dict(player='p',digest='x',recorded_at=1,received_at=1,observed_session_id=None,observed_game_no=None),
               dict(player='p',digest='y',recorded_at=2,received_at=2,observed_session_id=None,observed_game_no=None)]
        self.assertEqual(latest_views({'views':views})['p']['digest'],'y')

class SideOverflowTests(unittest.TestCase):
    def test_overflow_protects_current_match_and_preserves_unselected_evidence(self):
        rows=[];cards={};metadata={}
        for number,codes in [(0,list(range(100,115))),(1,[999])]:
            for game in (1,2):
                digest=f'{number}-{game}';build=digest
                rows.append(dict(digest=digest,build=build,main_complete=True,extra_complete=True,side_complete=False,_pair=('p','opponent'),recorded_at=number*10+game,expires_at=1000))
                metadata[digest]=(str(number),game,True)
                cards[build]=[dict(partition='main',code=1,copies=3)]
                if game==2:cards[build]+=[dict(partition='main',code=code,copies=1) for code in codes]
        r=infer_side(rows,rows[2],cards,metadata)
        self.assertEqual(sum(c['copies'] for c in r['cards']),15)
        self.assertIn(999,[c['code'] for c in r['cards']])
        self.assertEqual(len(r['observed_cards']),16)
        self.assertEqual(len(r['latent_cards']),1)

    def test_changed_first_main_is_not_used_to_infer_current_side(self):
        rows,cards,metadata=SidePerspectiveTests().fixture()
        rows[0]['digest']='unrelated-first'
        metadata={'unrelated-first':('other',1,True),'second':('other',2,True)}
        selected=dict(rows[0],digest='selected',build='different')
        cards['different']=[dict(partition='main',code=500,copies=3)]
        rows.append(selected);metadata['selected']=('new',1,True)
        self.assertEqual(infer_side(rows,selected,cards,metadata)['cards'],[])

    def test_missing_first_game_is_not_replaced_by_the_selected_side_game(self):
        rows=[];cards={};metadata={}
        for session,game,code in (('s',2,2729965),('old',1,None),('old',2,38817296)):
            digest=f'{session}-{game}';build=digest
            rows.append(dict(digest=digest,build=build,main_complete=True,extra_complete=True,side_complete=False,_pair=('p','opponent'),recorded_at=game,expires_at=1000))
            metadata[digest]=(session,game,True)
            cards[build]=[dict(partition='main',code=1,copies=3),dict(partition='extra',code=9,copies=1)]
            if code:cards[build].append(dict(partition='main',code=code,copies=1))
        selected=rows[0]
        result=infer_side(rows,selected,cards,metadata)
        self.assertEqual(result['reason'],'first_game_unavailable')
        self.assertEqual(result['cards'],[])
