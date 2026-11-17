# -*- coding: utf-8 -*-
"""QQ「对局胜率 [玩家名]」：主卡局/Side局的先攻、后攻小局胜率
+ 对前百饼图占比最高的5个具体卡组的大局（整场BO3）胜率。

口径（2026-11-17 用户纠正，最新构筑维度）：
- 直接复用 statistics_build_detail.query(scope='build') 的 matches，
  与构筑页当前构筑录像范围完全一致（28天、有效源、排除状态同源）；
  同场换备后的小局随场次保留，不另写构筑匹配算法；
- 主卡局 = 该场第 1 局；Side局 = 第 2/3 局；
- 先攻/后攻按该小局该玩家自己的座位（first_player=1/0），不用首局座位代替；
- 胜负明确才计入各分项，平局进分母；胜负未知不计入任何胜率，座位未知只进总体；
- 大局 = 同一场 BO3（observed_session_id 相同）按该玩家已收录小局汇总：
  胜局多即胜、负局多即负；打平/全未知/对手不唯一（或卡组无法识别）的场不计入；
- 对手卡组按该场第 1 局（主卡局）对手实际构筑命名，第 1 局缺失时依次取第 2/3 局
  ——同一场各小局的构筑推断与命名可能微差（如“鹰身亚马逊”/“亚马逊”），
  统一以主卡局为准，与「换备」的场次标签口径一致；
- Top5 卡组复用「单卡胜率」的前百饼图人数口径（每人最新有效首局、具体卡组）。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

CACHE_SECONDS = 30.0
CACHE_MAX = 16
POPULAR_LIMIT = 5
MIN_GAMES = 10       # 小局分项标“样本少”的门槛
MIN_MATCHES = 5      # 大局标“样本少”的门槛

_CACHE: dict = {}
_LOCK = threading.Lock()

_SECTION_LABELS = (
    ("main_first", "主卡局先攻"),
    ("main_second", "主卡局后攻"),
    ("side_first", "Side局先攻"),
    ("side_second", "Side局后攻"),
)


def _new_bucket():
    return dict(games=0, wins=0, losses=0, draws=0)


def _add(bucket, outcome):
    bucket["games"] += 1
    if outcome == "win":
        bucket["wins"] += 1
    elif outcome == "loss":
        bucket["losses"] += 1
    else:
        bucket["draws"] += 1


def _finalize(bucket, low_sample_after):
    result = dict(bucket)
    result["rate"] = 100.0 * bucket["wins"] / bucket["games"] if bucket["games"] else None
    result["low_sample"] = 0 < bucket["games"] < low_sample_after
    return result


def _compute(database, replay_root, now, player):
    from .statistics_build_detail import query as build_query
    from .card_statistics import _opponent_rows
    from .card_winrate import _popular_decks

    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro",
                                uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        detail = build_query(db, Path(replay_root), float(now), str(player), scope='build')
        if detail is None:
            return dict(player=player, found=False)
        rows = [dict(row, observed_game_no=row['game_no'],
                     observed_session_id=row['session_id']) for row in detail['matches']]

        total = _new_bucket()
        sections = {key: _new_bucket() for key, _ in _SECTION_LABELS}
        outcome_unknown = 0
        seat_unknown = 0
        sessions: dict[str, dict] = {}
        game_order: dict[str, int] = {}
        for row in rows:
            outcome = row.get("outcome")
            game_no = row.get("observed_game_no")
            if isinstance(game_no, int) and game_no > 0:
                game_order[row["digest"]] = game_no
            if outcome not in ("win", "loss", "draw"):
                outcome_unknown += 1
            else:
                _add(total, outcome)
                section = None
                if isinstance(game_no, int) and game_no == 1:
                    section = "main"
                elif isinstance(game_no, int) and game_no >= 2:
                    section = "side"
                seat = row.get("first_player")
                if section and seat in (1, 0):
                    _add(sections["%s_%s" % (section, "first" if seat == 1 else "second")], outcome)
                else:
                    seat_unknown += 1
            session_id = row.get("observed_session_id")
            if isinstance(session_id, str) and session_id:
                session = sessions.setdefault(session_id, dict(digests=set(), wins=0, losses=0))
                session["digests"].add(row["digest"])
                if outcome == "win":
                    session["wins"] += 1
                elif outcome == "loss":
                    session["losses"] += 1

        opponents = _opponent_rows(db, {row["digest"] for row in rows})
        matches = {}
        for deck in _popular_decks(db, replay_root, now)[:POPULAR_LIMIT]:
            matches[deck["deck_type"]] = dict(deck_type=deck["deck_type"],
                                              players=deck["players"], share=deck["share"],
                                              games=0, wins=0, losses=0)
        undetermined = 0
        ambiguous = 0
        for session in sessions.values():
            other_players = set()
            deck = None
            for digest in sorted(session["digests"],
                                 key=lambda item: (game_order.get(item, 99), item)):
                for other in opponents.get(digest, ()):
                    if other["player"] != player:
                        other_players.add(other["player"])
                        if deck is None and other.get("deck_type"):
                            deck = other["deck_type"]
            if len(other_players) != 1 or deck is None:
                ambiguous += 1
                continue
            if session["wins"] > session["losses"]:
                result = "wins"
            elif session["losses"] > session["wins"]:
                result = "losses"
            else:
                undetermined += 1
                continue
            bucket = matches.get(deck)
            if bucket is None:
                continue
            bucket["games"] += 1
            bucket[result] += 1

        no_session = sum(1 for row in rows if not row.get("observed_session_id"))
        return dict(
            player=player,
            found=True,
            basis_build=dict(detail['selected'], deck_type=detail['person'].get('deck_type')),
            scope='build',
            total=_finalize(total, MIN_GAMES),
            sections={key: _finalize(value, MIN_GAMES)
                      for key, value in sections.items()},
            outcome_unknown=outcome_unknown,
            seat_unknown=seat_unknown,
            no_session=no_session,
            popular_rows=[dict(row, rate=(100.0 * row["wins"] / row["games"] if row["games"] else None),
                               low_sample=0 < row["games"] < MIN_MATCHES)
                          for row in matches.values()],
            popular_limit=POPULAR_LIMIT,
            undetermined=undetermined,
            ambiguous=ambiguous,
        )


def collect(database, replay_root, now, player):
    text = str(player or "").strip()
    if not text:
        raise ValueError("请写上玩家名，例如：对局胜率 玩家名")
    if len(text) > 100:
        raise ValueError("玩家名最多100字。")
    key = (str(Path(database).resolve()), text)
    current = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and 0 <= current - hit[0] < CACHE_SECONDS:
            return dict(hit[1])
    payload = _compute(database, replay_root, float(now), text)
    with _LOCK:
        _CACHE[key] = (time.monotonic(), payload)
        if len(_CACHE) > CACHE_MAX:
            for old in sorted(_CACHE, key=lambda item: _CACHE[item][0])[:len(_CACHE) - CACHE_MAX]:
                _CACHE.pop(old, None)
    return dict(payload)


def _rate_line(label, bucket):
    if bucket["games"]:
        mark = "（样本少）" if bucket["low_sample"] else ""
        return ("%s胜率 %.1f%%（%d胜/%d负/%d平，共%d局）%s" % (
            label, bucket["rate"], bucket["wins"], bucket["losses"],
            bucket["draws"], bucket["games"], mark))
    return "%s胜率：暂无有效样本" % label


def message(payload):
    """纯文字回复：总体 + 主卡/Side×先/后攻小局胜率 + 对饼图Top5卡组的大局胜率。"""
    player = payload.get("player")
    if not payload.get("found"):
        return "没有找到 %s 的有效小局基准（最近28天没有可用的最新首局构筑）。" % player
    lines = ["对局胜率：%s" % player]
    basis = payload.get('basis_build') or {}
    if basis:
        lines.append("基准：最新构筑 %s；与当前构筑录像范围一致。" % (
            basis.get('deck_type') or '未识别'))
    total = payload.get("total") or {}
    if total.get("games"):
        mark = "（样本少）" if total.get("low_sample") else ""
        lines.append("总体小局胜率 %.1f%%（%d胜/%d负/%d平，共%d局）%s" % (
            total["rate"], total["wins"], total["losses"], total["draws"],
            total["games"], mark))
    else:
        lines.append("总体小局胜率：暂无胜负明确的样本")
    sections = payload.get("sections") or {}
    for key, label in _SECTION_LABELS:
        lines.append(_rate_line(label, sections.get(key) or {}))
    if payload.get("outcome_unknown"):
        lines.append("胜负未知 %d局：不计入任何胜率。" % payload["outcome_unknown"])
    if payload.get("seat_unknown"):
        lines.append("先后攻未知 %d局：仅计入总体。" % payload["seat_unknown"])
    rows = payload.get("popular_rows") or []
    lines.append("对前百饼图占比最高的%d个具体卡组（大局）：" % len(rows))
    for index, row in enumerate(rows, 1):
        prefix = "%d. %s（饼图占比%.1f%%，%d人）" % (
            index, row["deck_type"], row["share"], row["players"])
        if row["games"]:
            mark = "（样本少）" if row.get("low_sample") else ""
            lines.append("%s 大局胜率 %.1f%%（%d胜/%d负，%d场）%s" % (
                prefix, row["rate"], row["wins"], row["losses"], row["games"], mark))
        else:
            lines.append("%s：暂无有效大局样本" % prefix)
    if not rows:
        lines.append("暂无已识别的具体卡组。")
    undetermined = int(payload.get("undetermined") or 0)
    ambiguous = int(payload.get("ambiguous") or 0)
    if undetermined or ambiguous:
        lines.append("另有 %d场无法判定、%d场对手或卡组不唯一，未计入大局。" % (
            undetermined, ambiguous))
    if payload.get("no_session"):
        lines.append("另有 %d局缺少场次关联，只计入小局分项。" % payload["no_session"])
    lines.append("口径：仅统计当前保留（28天）内最新构筑对应录像范围的小局（含同场换备局）；主卡局=该场第1局，"
                 "Side局=第2/3局；先后攻按该小局自己座位；平局进分母；"
                 "大局=同一场BO3按已收录小局汇总，胜局多即胜；对手卡组按该场第1局命名；"
                 "小局不足%d局、大局不足%d场标“样本少”。" % (MIN_GAMES, MIN_MATCHES))
    lines.append("饼图榜按前百人数取前%d，不合并大类或“其他”；样本不足仍保留，无样本不算0%%。" % POPULAR_LIMIT)
    return "\n".join(lines)
