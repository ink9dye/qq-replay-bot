# -*- coding: utf-8 -*-
"""人工卡组名：按“卡组构成”登记名称，并对相似卡组做匹配。"""
import json
import time
from collections import Counter

THRESHOLD = 0.75
MARGIN = 0.05
TABLE = 'stat_manual_decks'


def signature(main, extra):
    """主卡+额外的多重集合签名（用于完全相同卡组去重）。"""
    def part(name, cards):
        return name + ':' + ','.join('%d*%d' % (code, copies) for code, copies in sorted(cards))
    return part('m', main) + '|' + part('e', extra)


def _cards(db, build):
    main, extra = [], []
    for row in db.execute('SELECT code,copies,partition FROM stat_build_cards WHERE build=? AND copies>0',
                          (build,)):
        (main if row['partition'] == 'main' else extra).append((int(row['code']), int(row['copies'])))
    return main, extra


def _counter(cards):
    counter = {}
    for code, copies in cards:
        counter[int(code)] = counter.get(int(code), 0) + int(copies)
    return counter


def _total(counter):
    return sum(counter.values())


def _entries(db):
    """返回 [(label, 卡号→张数 dict, 总张数)]；参考只构建一次。"""
    try:
        rows = list(db.execute('SELECT label,main,extra FROM stat_manual_decks'))
    except Exception:
        return []
    result = []
    for row in rows:
        try:
            main = [(int(code), int(copies)) for code, copies in json.loads(row['main'])]
            extra = [(int(code), int(copies)) for code, copies in json.loads(row['extra'])]
        except (ValueError, TypeError):
            continue
        counter = _counter(main + extra)
        result.append((str(row['label']), counter, _total(counter)))
    return result


def similarity_counters(left, right):
    """加权 Jaccard；left/right 为 卡号→张数 的 dict/Counter。"""
    total_left = _total(left)
    total_right = _total(right)
    if len(left) > len(right):
        left, right = right, left
    inter = 0
    for code, count in left.items():
        other = right.get(code)
        if other:
            inter += count if count < other else other
    union = total_left + total_right - inter
    if not union:
        return 0.0
    return inter / union


def similarity(left, right):
    """兼容旧接口：left/right 为 [(code, copies)] 的主卡+额外序列。"""
    a = {}
    b = {}
    for cards in left:
        for code, copies in cards:
            a[int(code)] = a.get(int(code), 0) + int(copies)
    for cards in right:
        for code, copies in cards:
            b[int(code)] = b.get(int(code), 0) + int(copies)
    return similarity_counters(a, b)


def remember(db, build, label):
    """登记一条“卡组 → 名字”；同一卡组重复命名会覆盖。"""
    if not label or not str(label).strip():
        return None
    main, extra = _cards(db, build)
    if not main:
        return None
    value = str(label).strip()
    db.execute('CREATE TABLE IF NOT EXISTS %s('
               'signature TEXT PRIMARY KEY, label TEXT NOT NULL, main TEXT NOT NULL, '
               'extra TEXT NOT NULL, updated_at REAL NOT NULL)' % TABLE)
    db.execute('INSERT OR REPLACE INTO %s(signature,label,main,extra,updated_at) VALUES(?,?,?,?,?)' % TABLE,
               (signature(main, extra), value, json.dumps(main), json.dumps(extra), time.time()))
    return value


def match(entries, main, extra, threshold=THRESHOLD, margin=MARGIN):
    """返回 (label, score, second_label, second_score)。

    领先差只和“不同标签”的第二名比较；同一名字的多条参考不参与 margin。
    """
    counter = _counter(list(main) + list(extra))
    total = _total(counter)
    per_label = {}
    for entry in entries:
        if len(entry) == 3:
            label, ref, ref_total = entry
        else:
            label, ref = entry
            ref_total = _total(ref)
        score = similarity_counters(counter, ref)
        if score > per_label.get(label, 0.0):
            per_label[label] = score
    if not per_label:
        return None, 0.0, None, 0.0
    ordered = sorted(per_label.items(), key=lambda item: item[1], reverse=True)
    best_label, best = ordered[0]
    second_label, second = ordered[1] if len(ordered) > 1 else (None, 0.0)
    if best >= threshold and best - second >= margin:
        return best_label, best, second_label, second
    return None, best, second_label, second


def labels_for_builds(db, builds):
    """批量给 build 匹配人工卡组名，返回 {build: (label, score)}。"""
    builds = [build for build in builds if build]
    if not builds:
        return {}
    entries = _entries(db)
    if not entries:
        return {}
    result = {}
    cached = {}
    for start in range(0, len(builds), 200):
        chunk = builds[start:start + 200]
        marks = ','.join('?' for _ in chunk)
        cards = {}
        for row in db.execute('SELECT build,code,copies,partition FROM stat_build_cards '
                              'WHERE build IN (%s) AND copies>0' % marks, chunk):
            cards.setdefault(row['build'], ([], []))
            part = 0 if row['partition'] == 'main' else 1
            cards[row['build']][part].append((int(row['code']), int(row['copies'])))
        for build in chunk:
            main, extra = cards.get(build, ([], []))
            if not main:
                continue
            counter = _counter(main + extra)
            key = tuple(sorted(counter.items()))
            if key not in cached:
                cached[key] = match(entries, main, extra)
            label, score, _second_label, _second = cached[key]
            if label:
                result[build] = (label, score)
    return result


def seed_from_manual_builds(db):
    """把已有的构筑级 manual 命名登记进“卡组→名字”表。"""
    try:
        rows = list(db.execute("SELECT fingerprint,deck_type FROM stat_builds "
                               "WHERE classification_source='manual' AND deck_type IS NOT NULL"))
    except Exception:
        return 0
    count = 0
    for row in rows:
        if remember(db, row['fingerprint'], row['deck_type']):
            count += 1
    return count
