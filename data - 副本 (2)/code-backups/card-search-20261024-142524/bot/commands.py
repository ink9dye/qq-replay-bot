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
    if head == "帮助":
        return {"op": "help"}
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
    return {"op": "unknown", "text": " ".join(tokens).strip()}
