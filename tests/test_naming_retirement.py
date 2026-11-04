import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from bot import build_classification, statistics_deck_naming, statistics_match_identity
from bot.service import BotService


class NamingRetirementTests(unittest.TestCase):
    def test_old_similarity_algorithm_and_manual_writer_are_absent(self):
        for name in ('rules', 'apply_rules', 'normalize_references'):
            self.assertFalse(hasattr(build_classification, name))
        with self.assertRaisesRegex(ValueError, 'manual_naming_removed'):
            statistics_deck_naming.apply_name(None, 'build', 'name')

    def test_replay_sync_does_not_run_legacy_reference_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'reference_decks.json').write_text('{"decks":[]}', encoding='utf-8')
            store=SimpleNamespace(all_replays=lambda: [], refresh_classifications=lambda *a: self.fail('legacy scorer ran'))
            service=SimpleNamespace(root=root,data_dir=root,replay_root=root,store=store,
                clock=lambda:1000,_fact_signatures={},_classification_rules_signature=None,_series_rules_signature=None)
            self.assertEqual(BotService.sync_replay_facts(service), 0)

    def test_manual_side_game_does_not_override_first_game_rule(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close);db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE stat_views(digest, player)')
        for digest in ('first','side'):
            db.executemany('INSERT INTO stat_views VALUES(?,?)',[(digest,'p'),(digest,'opponent')])
        rows=[dict(digest='first',player='p',build='a',deck_type='字段名',classification_source='auto_series'),
              dict(digest='side',player='p',build='b',deck_type='旧人工名',classification_source='manual')]
        with patch.object(statistics_match_identity,'capture_metadata',return_value={'first':('match',1,True),'side':('match',2,True)}), \
                patch.object(statistics_match_identity,'_core_signatures',return_value={'a':(1,2),'b':(1,2)}):
            statistics_match_identity.inherit_first_game_types(db,rows)
        self.assertEqual(rows[1]['deck_type'],'字段名')
        self.assertEqual(rows[1]['classification_source'],'same_match_first_game')
