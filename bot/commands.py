# -*- coding: utf-8 -*-
"""私聊指令。玩家名含空格时用英文双引号包起来。"""

from __future__ import annotations

import re
from typing import Optional

from .help import HELP

_TOKEN = re.compile(r'"([^"]*)"|(\S+)')
_SCOPE = re.compile(r"前(\d+)$")

def tokenize(text: str) -> list[str]:
    tokens = []
    for quoted, plain in _TOKEN.findall(text or ""):
        token = quoted or plain
        if token:
            tokens.append(token)
    return tokens


def parse_scope(text: str) -> Optional[int]:
    """全部返回 None。前100 返回 100。排名未知时不算命中，由调用方判断。"""
    if text == "全部":
        return None
    matched = _SCOPE.fullmatch(text or "")
    if not matched:
        raise ValueError("范围请写“全部”或“前100”")
    top = int(matched.group(1))
    if top <= 0:
        raise ValueError("名次必须是正整数")
    return top


def parse_command(text: str) -> dict:
    tokens = tokenize(text.strip())
    if not tokens:
        return {"op": "empty"}
    head, rest = tokens[0], tokens[1:]
    if head == "历史":
        player = " ".join(rest).strip()
        if not player or len(player) > 100:
            return {"op": "error", "message": "格式：历史 玩家名，例如：历史 花无十日红。"}
        return {"op": "player_history", "player": player}
    if head == "构筑":
        player = " ".join(rest).strip()
        if not player or len(player) > 100:
            return {"op": "error", "message": "格式：构筑 玩家名，例如：构筑 花无十日红。"}
        return {"op": "deck_build", "player": player}
    if head in ("随机一卡", "随机卡", "猜卡", "揭晓猜卡"):
        if rest:
            return {"op": "error", "message": f"直接发送“{head}”即可。"}
        return {"op": {"随机一卡": "random", "随机卡": "random", "猜卡": "guess_start", "揭晓猜卡": "guess_reveal"}[head]}
    if head == "猜卡答案":
        answer = " ".join(rest).strip()
        if not answer or len(answer) > 100:
            return {"op": "error", "message": "格式：猜卡答案 完整卡名（也接受外号或密码）"}
        return {"op": "guess_answer", "value": answer}
    if head in ("禁限", "禁限表"):
        args = list(rest)
        if args and args[0].upper() == "OCG":
            args.pop(0)
        elif args and args[0].upper() in ("TCG", "MD", "CN", "简中"):
            return {"op": "error", "message": "目前只接入 OCG 禁限表，例如：禁限 OCG 灰流丽。"}
        if head == "禁限表":
            if len(args) > 1 or (args and not re.fullmatch(r"[0-9]{4}-[0-9]{2}", args[0])):
                return {"op": "error", "message": "格式：禁限表 OCG 或 禁限表 OCG 2020-04。历史月份按当月1日查询。"}
            return {"op": "limits_table", "value": args[0] if args else ""}
        query = " ".join(args).strip()
        if not query or len(query) > 100:
            return {"op": "error", "message": "格式：禁限 OCG 卡名，例如：禁限 OCG 灰流丽。"}
        return {"op": "limits_card", "value": query}
    if head in ("查卡", "ck", "CK"):
        return {"op": "card_search", "query": " ".join(rest)}
    if head == "帮助":
        return {"op": "help"}
    if head == "统计":
        page = rest[0] if rest else ""
        if len(rest) > 1 or page not in ("", "卡组", "录像", "卡片", "系列"):
            return {"op": "error", "message": "格式：统计 或 统计 卡组/录像/卡片/系列"}
        pages = {"": "", "卡组": "statistics/decks", "录像": "statistics/recordings",
                 "卡片": "statistics/card-table", "系列": "statistics/groups"}
        return {"op": "stats_page", "page": pages[page]}
    if head == "我的订阅":
        return {"op": "list"}
    if head == "订阅":
        if len(rest) != 3:
            return {"op": "error", "message": "格式：订阅 “玩家名” 全部或前100 推送或静默"}
        player, scope_text, mode_text = rest
        if not player.strip():
            return {"op": "error", "message": "玩家名不能为空"}
        try:
            top = parse_scope(scope_text)
        except ValueError as exc:
            return {"op": "error", "message": str(exc)}
        if mode_text == "推送":
            notify = True
        elif mode_text == "静默":
            notify = False
        else:
            return {"op": "error", "message": "通知方式请写“推送”或“静默”"}
        return {"op": "subscribe", "player": player.strip(), "rank_top": top, "notify": notify}
    if head in ("推送", "静默", "取消订阅", "录像"):
        player = " ".join(rest).strip()
        if not player:
            return {"op": "error", "message": f"请写上玩家名：{head} “玩家名”"}
        op = {"推送": "push", "静默": "silent", "取消订阅": "unsubscribe", "录像": "replays"}[head]
        return {"op": op, "player": player}
    if head == "预定":
        if len(rest) < 2:
            return {"op": "error", "message": "格式：预定 卡名 罕贵 预期价，例如：预定 决斗者之力 UTR 160"}
        try:
            target = float(rest[-1])
        except ValueError:
            return {"op": "error", "message": "预期价格要写数字，例如 160"}
        if target <= 0:
            return {"op": "error", "message": "预期价格要大于 0"}
        args = list(rest[:-1])
        rarity = ""
        if len(args) >= 2 and re.fullmatch(r"[A-Za-z0-9]{1,12}", args[-1]):
            rarity = args[-1].upper()
            args = args[:-1]
        name = " ".join(args).strip()
        if not name:
            return {"op": "error", "message": "格式：预定 卡名 罕贵 预期价"}
        return {"op": "price_reserve", "name": name, "rarity": rarity, "target": target}
    if head == "我的预定":
        if rest:
            return {"op": "error", "message": "直接发送“我的预定”即可。"}
        return {"op": "price_reservations"}
    if head == "取消预定":
        if not rest:
            return {"op": "error", "message": "格式：取消预定 卡名 罕贵"}
        args = list(rest)
        rarity = ""
        if len(args) >= 2 and re.fullmatch(r"[A-Za-z0-9]{1,12}", args[-1]):
            rarity = args[-1].upper()
            args = args[:-1]
        name = " ".join(args).strip()
        if not name:
            return {"op": "error", "message": "格式：取消预定 卡名 罕贵"}
        return {"op": "price_cancel", "name": name, "rarity": rarity}
    return {"op": "unknown", "text": " ".join(tokens).strip()}
