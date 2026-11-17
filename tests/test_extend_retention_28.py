import sqlite3
import tempfile
import unittest
from pathlib import Path
from tools.extend_retention_28 import extend


class ExtendRetentionTests(unittest.TestCase):
    def test_preview_backup_expiry_and_idempotence(self):
        with tempfile.TemporaryDirectory() as temp, sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE stat_games(digest TEXT,received_at REAL,expires_at REAL)')
            db.executemany('INSERT INTO stat_games VALUES(?,?,?)',
                           [('live', 100, 100 + 14*86400),
                            ('expired', -28*86400, -14*86400),
                            ('new', 100, 100 + 28*86400)])
            self.assertEqual(extend(db, 200), 1)
            self.assertEqual(db.execute("SELECT expires_at FROM stat_games WHERE digest='live'").fetchone()[0], 100+14*86400)
            backup = Path(temp)/'deadlines.json'
            self.assertEqual(extend(db, 200, backup), 1)
            self.assertIn('live', backup.read_text())
            self.assertEqual(db.execute("SELECT expires_at FROM stat_games WHERE digest='live'").fetchone()[0], 100+28*86400)
            self.assertEqual(extend(db, 200), 0)
