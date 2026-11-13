"""Independent first-game evidence for recording-filter test samples."""
import json
from unittest.mock import patch

import test_card_statistics as C


def set_up(test):
    # Production caches assume one database/root. Each test uses a new temp root.
    for target, value in (('bot.top100._ALIVE_CACHE', {}),
                          ('bot.top100._ALIVE_FILES', {'at': 0.0, 'paths': None})):
        guard = patch(target, value)
        guard.start()
        test.addCleanup(guard.stop)
    C.CardStatisticsTests.setUp(test)
    for row in test.rows:
        digest = row['digest']
        payload = json.loads(test.store._db.execute(
            'SELECT payload_json FROM stat_evidence WHERE digest=?', (digest,)).fetchone()[0])
        payload['capture'] = dict(session_id='fixture-' + digest, game_no=1,
                                  captured_from_start=True)
        test.store._db.execute('UPDATE stat_evidence SET payload_json=? WHERE digest=?',
                               (json.dumps(payload), digest))
    test.store._db.commit()
