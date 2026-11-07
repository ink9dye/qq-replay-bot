"""Small World public card properties for analysis within the browser.

Five-property connection approach adapted from KennethJAllen/ygo-small-world
(MIT, Copyright 2023 Kenneth Allen). See licenses/ygo-small-world-MIT.txt.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

EXTRA = 0x40 | 0x2000 | 0x800000 | 0x4000000


@dataclass(frozen=True)
class Card:
    code: int
    name: str
    kind: int
    properties: tuple

    @property
    def main_monster(self):
        return bool(self.kind & 1) and not self.kind & (EXTRA | 0x4000)


def read_cards(root):
    """Base CDB wins conflicts, matching existing local-card precedence.

    Read only a few public columns per query; no persistent full-card cache.
    Fail closed on a broken library instead of reporting incomplete results.
    """
    root = Path(root).resolve()
    paths = [root / 'cards.cdb', *sorted((root / 'expansions').glob('*.cdb'))]
    cards = {}
    for path in paths:
        if not path.is_file() or not path.resolve().is_relative_to(root):
            continue
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            for code, name, kind, race, attribute, level, atk, defense in db.execute(
                'SELECT d.id,t.name,d.type,d.race,d.attribute,d.level,d.atk,d.def '
                'FROM datas d JOIN texts t ON t.id=d.id'
            ):
                if (type(code) is not int or not 0 < code <= 0xffffffff
                        or not isinstance(name, str) or not name.strip()
                        or any(type(v) is not int for v in (kind, race, attribute, level, atk, defense))):
                    continue
                # The upper bytes store Pendulum scales, not the monster level.
                props = (race or None, attribute or None, (level & 0xff) or None,
                         atk if atk >= 0 else None, defense if defense >= 0 else None)
                cards.setdefault(code, Card(code, name.strip(), kind, props))
    if not cards:
        raise ValueError('empty local card library')
    return cards




