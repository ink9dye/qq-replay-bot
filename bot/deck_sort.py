"""Deck display ordering, using only the requested local card records.

References: YGOPro deck_sort_lv / ReadDB and the user-provided Monitor demo.
Main/Side monsters use global descending levels, as requested by the user.
ReadDB: level low byte; Link defense is a marker, not DEF.
https://github.com/Fluorohydride/ygopro/blob/master/gframe/data_manager.cpp
"""
from contextlib import closing
import sqlite3


def load_card_data(data_dir, codes):
    from .statistics_local_cards import library_root
    root = library_root(data_dir)
    wanted = sorted({int(code) for code in codes if str(code).isdigit() and int(code) > 0})
    if root is None or not wanted:
        return {}
    result = {}
    for path in [root / 'cards.cdb', *sorted((root / 'expansions').glob('*.cdb'))]:
        pending = [code for code in wanted if code not in result]
        if not pending:
            break
        if not path.is_file() or not path.resolve().is_relative_to(root):
            continue
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                columns = {row[1] for row in db.execute('PRAGMA table_info(datas)')}
                if not {'id', 'type'} <= columns:
                    continue
                fields = ','.join(name if name in columns else 'NULL' for name in ('id','type','level','atk','def'))
                for start in range(0, len(pending), 400):
                    chunk = pending[start:start + 400]
                    marks = ','.join('?' for _ in chunk)
                    for code, kind, level, attack, defense in db.execute(
                            'SELECT ' + fields + ' FROM datas WHERE id IN (' + marks + ')', chunk):
                        if type(kind) is not int:
                            continue
                        result.setdefault(code, dict(type=kind, level=(level or 0) & 0xff,
                            attack=attack if type(attack) is int else 0,
                            defense=0 if kind & 0x4000000 else defense if type(defense) is int else 0))
        except (sqlite3.Error, OSError):
            continue
    return result


def sort_key(card, card_data=None):
    code = int(card.get('code') or 0)
    info = (card_data or {}).get(code)
    if not info or not info.get('type'):
        return (1, code)  # Missing public metadata stays deterministic at the end.
    kind = info['type']
    category = kind & 7
    if category not in (1, 2, 4):
        return (1, code)  # Unsupported public card type also stays at the end.
    if category == 1:
        subtype = 0
        if card.get('partition') == 'extra':
            subtype = next((i for i, flag in enumerate((0x40, 0x2000, 0x800000, 0x4000000)) if kind & flag), 4)
        return (0, category, subtype, -info['level'], -info['attack'], -info['defense'], code)
    if category == 2:
        subtype = next((i for i, flag in enumerate((0x10000, 0x20000, 0x40000, 0x80000, 0x80), 1) if kind & flag), 0)
    elif category == 4:
        subtype = 1 if kind & 0x20000 else 2 if kind & 0x100000 else 0
    else:
        subtype = kind & 0xfffffff8
    return (0, category, subtype, code)


def sorted_cards(cards, card_data=None):
    return sorted(cards, key=lambda card: sort_key(card, card_data))
