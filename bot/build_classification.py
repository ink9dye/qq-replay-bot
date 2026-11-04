"""构筑分类存储原语；命名规则统一由 series_naming 计算。"""

def initialize(db):
    fields={r[1] for r in db.execute('PRAGMA table_info(stat_builds)')}
    for name,definition in [('classification_status',"TEXT NOT NULL DEFAULT 'unknown'"),
                            ('classification_source',"TEXT NOT NULL DEFAULT 'unclassified'"),
                            ('series_status',"TEXT NOT NULL DEFAULT 'unknown'"),
                            ('classification_score','REAL')]:
        if name not in fields:db.execute('ALTER TABLE stat_builds ADD COLUMN '+name+' '+definition)
    db.executescript('''
CREATE TABLE IF NOT EXISTS stat_build_series (
 build TEXT NOT NULL REFERENCES stat_builds(fingerprint) ON DELETE CASCADE,
 series TEXT NOT NULL, PRIMARY KEY(build,series)
);
CREATE TABLE IF NOT EXISTS stat_classification_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO stat_classification_state VALUES(1,0);
''')
    db.commit()

def assign(db,build,deck_type,series,version,source='manual',score=None):
    previous=db.execute('SELECT * FROM stat_builds WHERE fingerprint=?',(build,)).fetchone()
    if previous is None:return False
    if deck_type is not None and (not isinstance(deck_type,str) or not deck_type.strip() or len(deck_type)>120):
        raise ValueError('invalid_deck_type')
    if not isinstance(series,(list,tuple)) or any(not isinstance(s,str) or not s.strip() or len(s)>80 for s in series):
        raise ValueError('invalid_series')
    tags=sorted({s.strip() for s in series})
    label=deck_type.strip() if deck_type is not None else None
    status='recognized' if label else 'unknown'
    series_status='known' if tags else 'unknown'
    old_tags=[r[0] for r in db.execute('SELECT series FROM stat_build_series WHERE build=? ORDER BY series',(build,))]
    if (previous['deck_type'],previous['classification_version'],previous['classification_source'],
            previous['classification_status'],previous['classification_score'],previous['series_status'],old_tags)==(label,version,source,status,score,series_status,tags):
        return False
    db.execute('UPDATE stat_builds SET deck_type=?,classification_version=?,classification_source=?,classification_status=?,classification_score=?,series_status=? WHERE fingerprint=?',
               (label,version,source,status,score,series_status,build))
    db.execute('DELETE FROM stat_build_series WHERE build=?',(build,))
    db.executemany('INSERT INTO stat_build_series VALUES(?,?)',[(build,s) for s in tags])
    db.execute('UPDATE stat_classification_state SET revision=revision+1 WHERE singleton=1')
    return True
