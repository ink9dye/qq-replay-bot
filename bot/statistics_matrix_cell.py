"""P09 矩阵格子展开：该组合我方抽到后胜率较高的卡 + 该组合录像。

口径（与既有统计一致，不新造）：
- 只取"行玩家视角"的小局：行=我方卡组，列=对手卡组，方向固定；
- 抽到只认 stat_card_facts.drawn=1，且该卡必须在当局我方主卡组构筑内；
  未知/不完整抽卡记录不算抽到，也不当 0%；
- 门槛 = 该卡抽到且已知胜负的小局数 / 本格已知胜负视角数，按百分比精确比较（含边界），默认 10%；
- 不在这里重复组合总体/先攻/后攻胜率，也不输出牌手名单；那部分由矩阵格子自身展示。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
import secrets
import time

from .card_identity import reference as identity_reference
from .card_search import card_name
from .statistics_card_names import index as card_name_index
from .top100 import eligible_views

VERSION = 1
MAX_CARDS = 50
MAX_RECORDINGS = 200
OUTCOMES = ('win', 'loss', 'draw')

# 矩阵格刚算出的候选样本只在本进程保留 120 秒，供紧随其后的 Side 按需分析
# 复用同一集合；不是持久缓存，也不写磁盘。token 不可猜，不暴露玩家数据。
_CELL_SAMPLE_TTL = 120.0
_CELL_SAMPLE_MAX = 16
_CELL_SAMPLES = {}
_CELL_SAMPLE_ORDER = []


def _remember_cell_samples(samples):
    now = time.time()
    token = secrets.token_urlsafe(12)[:16]
    _CELL_SAMPLES[token] = (now, list(samples))
    _CELL_SAMPLE_ORDER.append((now, token))
    while _CELL_SAMPLE_ORDER and (len(_CELL_SAMPLES) > _CELL_SAMPLE_MAX
                                  or _CELL_SAMPLE_ORDER[0][0] < now - _CELL_SAMPLE_TTL):
        _, old = _CELL_SAMPLE_ORDER.pop(0)
        _CELL_SAMPLES.pop(old, None)
    return token


def cached_cell_samples(token):
    if not token:
        return None
    entry = _CELL_SAMPLES.get(str(token))
    if not entry or entry[0] < time.time() - _CELL_SAMPLE_TTL:
        return None
    return entry[1]


def threshold_value(raw):
    text = str(raw if raw is not None else '10').strip()
    if not text or len(text) > 10 or 'e' in text.lower():
        raise ValueError('样本占比无效。')
    if len(text.partition('.')[2]) > 6:
        raise ValueError('样本占比最多 6 位小数。')
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError('样本占比无效。')
    if not value.is_finite() or value < 0 or value > 100:
        raise ValueError('样本占比应在 0 到 100 之间。')
    return value


def parse_limit(raw):
    text = str(raw if raw is not None else MAX_CARDS).strip()
    if not text.isdigit() or len(text) > 4:
        raise ValueError('显示行数无效。')
    return max(1, min(MAX_CARDS, int(text)))


def _names(db, replay_root):
    data_dir = Path(replay_root).parent if Path(replay_root).name == 'replays' else Path(replay_root)
    try:
        cards = card_name_index(data_dir)
    except Exception:
        return {}
    return {int(code): card_name(info) for code, info in cards.items() if str(code).isdigit()}


def query(db, replay_root, now, row, column, first_player=None, internal_only=False,
          threshold='10', limit=MAX_CARDS):
    if not (row is None or isinstance(row, str)) or not (column is None or isinstance(column, str)):
        raise ValueError('卡组条件无效。')
    row = row.strip() if isinstance(row, str) else None
    column = column.strip() if isinstance(column, str) else None
    if (isinstance(row, str) and (not row or len(row) > 128)) or \
            (isinstance(column, str) and (not column or len(column) > 128)):
        raise ValueError('卡组条件无效。')
    if first_player is not None and type(first_player) is not bool:
        raise ValueError('先后攻条件无效。')
    ratio = threshold_value(threshold)
    limit = parse_limit(limit)

    basis = eligible_views(db, replay_root, now, internal_only)
    opponents = {}
    for item in db.execute('SELECT v.digest,v.player,b.deck_type FROM stat_views v '
                           'JOIN stat_builds b ON b.fingerprint=v.build'):
        opponents.setdefault(item['digest'], []).append(dict(item))
    samples = []
    for view in basis['views']:
        other = [p for p in opponents.get(view['digest'], []) if p['player'] != view['player']]
        if len(other) != 1:
            continue
        if view['deck_type'] != row or other[0]['deck_type'] != column:
            continue
        if first_player is not None and view['first_player'] != int(first_player):
            continue
        samples.append(dict(view, opponent=other[0]['player']))

    known = [s for s in samples if s['outcome'] in OUTCOMES]
    denominator = len(known)

    builds = {s['build'] for s in samples}
    main = {}
    if builds:
        marks = ','.join('?' for _ in builds)
        for item in db.execute("SELECT build,code,SUM(copies) copies FROM stat_build_cards "
                               "WHERE partition='main' AND copies>0 AND build IN (%s) GROUP BY build,code" % marks,
                               list(builds)):
            main.setdefault(item['build'], set()).add(item['code'])

    facts = {}
    digests = {s['digest'] for s in samples}
    if digests:
        marks = ','.join('?' for _ in digests)
        for item in db.execute('SELECT digest,player,code FROM stat_card_facts WHERE drawn=1 AND digest IN (%s)'
                               % marks, list(digests)):
            facts.setdefault((item['digest'], item['player']), set()).add(item['code'])

    mapping, _, _ = identity_reference(db)
    stats = {}
    for sample in known:
        carried = main.get(sample['build'], ())
        for raw in facts.get((sample['digest'], sample['player']), ()):
            if raw not in carried:
                continue
            code = mapping.get(raw, (raw,))[0]
            item = stats.setdefault(code, dict(n=0, wins=0, losses=0))
            item['n'] += 1
            item['wins'] += sample['outcome'] == 'win'
            item['losses'] += sample['outcome'] == 'loss'

    names = _names(db, replay_root)
    cards = []
    for code, item in stats.items():
        games = item['n']
        if not denominator or not games:
            continue
        if Decimal(games) * 100 < ratio * denominator:
            continue
        cards.append(dict(code=code, name=names.get(code) or ('#' + str(code)), n=games,
                          wins=item['wins'], losses=item['losses'], draws=games - item['wins'] - item['losses'],
                          rate=round(100 * item['wins'] / games, 1),
                          sample_percentage=round(100 * games / denominator, 2)))
    cards.sort(key=lambda card: (-card['rate'], -card['n'], card['code']))
    total_cards = len(cards)
    shown = cards[:limit]

    recordings = {}
    for sample in samples:
        if sample['digest'] in recordings:
            continue
        recordings[sample['digest']] = dict(
            digest=sample['digest'], player=sample['player'], opponent=sample['opponent'],
            outcome=sample['outcome'], first_player=sample['first_player'],
            recorded_at=sample['recorded_at'])
    ordered = sorted(recordings.values(), key=lambda item: item['recorded_at'], reverse=True)

    notes = []
    if not denominator:
        notes.append('本组合没有已知胜负的小局，胜率与排行不计算。')
    elif not total_cards:
        notes.append('本组合没有达到门槛的抽到记录；可降低样本占比。')
    if total_cards > len(shown):
        notes.append('共 %d 张达到门槛，只显示前 %d 张。' % (total_cards, len(shown)))
    return dict(
        version=VERSION, row=row, column=column,
        filters=dict(first='all' if first_player is None else ('first' if first_player else 'second'),
                     internal=bool(internal_only), threshold=str(ratio)),
        perspectives=len(samples), small_games=len({s['digest'] for s in samples}),
        denominator=denominator, unknown=len(samples) - denominator,
        cards=shown, cards_total=total_cards, truncated=total_cards > len(shown),
        recordings=ordered[:MAX_RECORDINGS], recordings_total=len(ordered),
        notes=notes, side_token=_remember_cell_samples(samples),
    )
