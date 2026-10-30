"""百鸽查卡：保留搜索歧义，按用户隔离候选，纯标准库。"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class CardReply(str):
    def __new__(cls, text, image_url):
        result = super().__new__(cls, text)
        result.image_url = image_url
        return result


def normalize(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).casefold()


def card_name(card):
    text = card.get("text") or {}
    return card.get("cn_name") or text.get("name") or card.get("sc_name") or str(card.get("id", "未知"))


class CardSearch:
    def __init__(self, data_dir: Path, fetch=None, clock=None):
        self.alias_path = Path(data_dir) / "card_aliases.json"
        self.fetch = fetch or self._fetch
        self.clock = clock or time.monotonic
        self.sessions = OrderedDict()
        self.cache = OrderedDict()
        self.lock = threading.RLock()

    @staticmethod
    def _fetch(path):
        request = Request("https://ygocdb.com/api/v0/" + path,
                          headers={"User-Agent": "qq-replay-bot/card-search"})
        with urlopen(request, timeout=8) as response:
            body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ValueError("response too large")
        result = json.loads(body.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("invalid response")
        return result

    def _request(self, path):
        cached = self.cache.get(path)
        if cached and self.clock() - cached[0] < 3600:
            return cached[1]
        result = self.fetch(path)
        self.cache[path] = (self.clock(), result)
        self.cache.move_to_end(path)
        while len(self.cache) > 128:
            self.cache.popitem(last=False)
        return result

    def _alias(self, query):
        aliases = {"灰": "灰流丽", "泡影": "无限泡影", "g": "增殖的G"}
        if self.alias_path.exists():
            custom = json.loads(self.alias_path.read_text(encoding="utf-8-sig"))
            if not isinstance(custom, dict) or any(not isinstance(v, (str, int)) or isinstance(v, bool) for v in custom.values()):
                raise ValueError("invalid aliases")
            aliases.update({normalize(k): str(v) for k, v in custom.items()})
        return aliases.get(normalize(query), query)

    @staticmethod
    def _detail(card):
        card_id = int(card["id"])
        text = card.get("text") or {}
        lines = [card_name(card), f"卡片密码：{card_id:08d}"]
        for name in (card.get("sc_name") or text.get("sc_name"), card.get("md_name") or text.get("md_name")):
            if name and name not in lines:
                lines.append(name)
        for field in ("types", "pdesc", "desc"):
            if text.get(field):
                lines.append(str(text[field])[:7000])
        lines.append(f"百鸽：https://ygocdb.com/card/{card_id}")
        return CardReply("\n\n".join(lines), f"https://cdn.233.momobako.com/ygopro/pics/{card_id}.jpg")

    def handle(self, user, action, value=""):
        # handle_text 在线程中执行；保护候选和缓存，避免同一用户并发串页。
        with self.lock:
            try:
                return self._handle(str(user), action, value)
            except (OSError, ValueError, KeyError, TypeError):
                return "查卡暂时失败：请稍后重试。若编辑过 data/card_aliases.json，请检查它是合法的外号到卡名或密码的 JSON 对象。"

    def _handle(self, user, action, value):
        now = self.clock()
        for key in list(self.sessions):
            if now - self.sessions[key]["time"] >= 300:
                del self.sessions[key]
        if action == "select":
            session = self.sessions.get(user)
            if not session:
                return "查卡候选已过期或不存在，请先发送“查卡 关键词”。"
            index = int(value) - 1
            if index < 0 or index >= len(session["rows"]):
                return "序号不在当前候选范围内，请使用列表中的序号。"
            return self._detail(session["rows"][index])
        if action == "next":
            session = self.sessions.get(user)
            if not session:
                return "查卡候选已过期或不存在，请先发送“查卡 关键词”。"
            if session["offset"] < len(session["rows"]):
                session["offset"] += 5
                if session["offset"] < len(session["rows"]):
                    return self._list(session)
            if not session["next"]:
                session["offset"] = max(0, ((len(session["rows"]) - 1) // 5) * 5)
                return "已经是最后一页。\n" + self._list(session)
            query, start = session["query"], session["next"]
        else:
            if not value.strip() or len(value) > 100:
                return "格式：查卡 关键词（最多100字），例如：查卡 闪刀。"
            query, start = self._alias(value.strip()), 0
        result = self._request("?" + urlencode({"search": query, "start": start}))
        rows = result.get("result")
        if not isinstance(rows, list):
            raise ValueError("missing results")
        rows = [r for r in rows if isinstance(r, dict) and str(r.get("id", "")).isdigit()]
        if not rows:
            return "没有找到卡片，请换卡名片段、多关键词或卡片密码；外号可在 data/card_aliases.json 中补充。"
        if action != "next":
            matches = [r for r in rows if normalize(query) in {
                normalize(r.get(k, "")) for k in ("id", "cid", "cn_name", "sc_name", "md_name", "nwbbs_n", "cnocg_n", "jp_name", "en_name")
            }]
            if len(matches) == 1:
                self.sessions.pop(user, None)
                return self._detail(matches[0])
            if len(rows) == 1 and not result.get("next"):
                self.sessions.pop(user, None)
                return self._detail(rows[0])
        session = {"query": query, "rows": rows[:100], "offset": 0,
                   "next": int(result.get("next") or 0), "time": now}
        self.sessions[user] = session
        self.sessions.move_to_end(user)
        while len(self.sessions) > 1000:
            self.sessions.popitem(last=False)
        return self._list(session)

    @staticmethod
    def _list(session):
        offset = session["offset"]
        lines = [f"“{session['query']}”的候选："]
        lines += [f"{i + 1}. {card_name(card)}（{int(card['id']):08d}）"
                  for i, card in enumerate(session["rows"]) if offset <= i < offset + 5]
        lines.append("5分钟内发送“选卡1”或“选卡 1”查看；发送“查卡下一页”翻页。序号对应当前列表。群内每一步都需先@工具号。")
        return "\n".join(lines)
