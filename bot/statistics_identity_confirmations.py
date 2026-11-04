"""Read independently audited match identity without claiming full event capture."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

NAME = 'first-game-confirmations.sqlite3'


def metadata(facts, digests):
    database = facts.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        return {}
    path = Path(database).parent / NAME
    if not path.is_file():
        return {}
    found = {}
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        values = sorted(set(digests))
        for start in range(0, len(values), 400):
            chunk = values[start:start + 400]
            marks = ','.join('?' for _ in chunk)
            pairs = {}
            for digest, player in facts.execute(
                    'SELECT digest,player FROM stat_views WHERE digest IN (%s)' % marks, chunk):
                pairs.setdefault(digest, set()).add(player)
            for digest, session, number, first, players in db.execute(
                    'SELECT c.digest,c.session,c.number,c.first_digest,c.players '
                    'FROM confirmations c JOIN confirmations a ON a.digest=c.first_digest '
                    'AND a.session=c.session AND a.number=1 AND a.players=c.players '
                    'WHERE c.digest IN (%s)' % marks, chunk):
                if (type(number) is int and 1 <= number <= 20
                        and session == 'verified-log:' + first
                        and sorted(pairs.get(digest, ())) == json.loads(players)
                        and len(pairs.get(digest, ())) == 2):
                    found[digest] = (session, number, True)
    return found
