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
from urllib.request import Request, urlopen, build_opener, ProxyHandler
from html import unescape
from datetime import date


def release_lines(page):
    """Read the region/pack/date pairs actually displayed by the source."""
    result = []
    pattern = r'<i\b[^>]*>\s*(OCG|TCG)\s*</i>\s*<span\b[^>]*class=[\"\x27]pack[\"\x27][^>]*>(.*?)</span>'
    for region, body in re.findall(pattern, page, re.S | re.I):
        text = unescape(re.sub(r'<[^>]*>', '', body))
        match = re.fullmatch(r'\s*([A-Z0-9][A-Z0-9-]{1,40})\s*\((\d{4}-\d{2}-\d{2})\)\s*', text)
        if not match:
            continue
        code, stamp = match.groups()
        try:
            date.fromisoformat(stamp)
        except ValueError:
            continue
        line = f'{region.upper()} {code} ({stamp})'
        if line not in result:
            result.append(line)
    return result


_IMAGE_CACHE = {}
_IMAGE_LOCK = None


def _numeric_code(code):
    """先行/异画卡在回放里可能写成 <101307061>，取图前去掉尖括号。"""
    text = str(code).strip()
    if text.startswith('<') and text.endswith('>'):
        text = text[1:-1].strip()
    return int(text)


def card_image_url(code):
    """新卡在 cdntx super-pre，旧卡在 momobako；按卡号探测并缓存。"""
    import threading
    code = _numeric_code(code)
    global _IMAGE_LOCK
    if _IMAGE_LOCK is None:
        _IMAGE_LOCK = threading.Lock()
    with _IMAGE_LOCK:
        cached = _IMAGE_CACHE.get(int(code))
        if cached:
            return cached
        candidates = [
            f"https://cdntx.moecube.com/ygopro-super-pre/data/pics/{int(code)}.jpg",
            f"https://cdn.233.momobako.com/ygopro/pics/{int(code)}.jpg",
        ]
        import urllib.request
        for url in candidates:
            try:
                request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(request, timeout=8) as response:
                    if response.status == 200:
                        _IMAGE_CACHE[int(code)] = url
                        return url
            except Exception:
                continue
        _IMAGE_CACHE[int(code)] = candidates[0]
        return candidates[0]


_IMAGE_BYTES = {}
_IMAGE_BYTES_LOCK = None


def card_image_payload(code, data_dir):
    """下载并缓存卡图，返回 base64://，避免 QQ 端再请求外部 CDN。"""
    import base64
    import threading
    global _IMAGE_BYTES_LOCK
    if _IMAGE_BYTES_LOCK is None:
        _IMAGE_BYTES_LOCK = threading.Lock()
    code = _numeric_code(code)
    with _IMAGE_BYTES_LOCK:
        cached = _IMAGE_BYTES.get(code)
        if cached:
            return 'base64://' + cached
        cache_dir = Path(data_dir) / 'card-images'
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / (str(code) + '.jpg')
        blob = b''
        if path.is_file() and path.stat().st_size <= 8 * 1024 * 1024:
            blob = path.read_bytes()
        if not blob:
            try:
                from .statistics_local_cards import picture
                found = picture(data_dir, code)
                if found:
                    blob = Path(found[0]).read_bytes()
            except Exception:
                blob = b''
        if not blob:
            for url in (
                f'https://cdntx.moecube.com/ygopro-super-pre/data/pics/{code}.jpg',
                f'https://cdn.233.momobako.com/ygopro/pics/{code}.jpg',
            ):
                try:
                    request = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urlopen(request, timeout=5) as response:
                        data = response.read(8 * 1024 * 1024 + 1)
                    if data[:2] == b'\xff\xd8' and len(data) <= 8 * 1024 * 1024:
                        blob = data
                        path.write_bytes(blob)
                        break
                except Exception:
                    continue
        if not blob:
            return card_image_url(code)
        encoded = base64.b64encode(blob).decode('ascii')
        _IMAGE_BYTES[code] = encoded
        return 'base64://' + encoded


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
    def __init__(self, data_dir: Path, fetch=None, clock=None, release_fetch=None, wall_clock=None):
        self.data_dir = Path(data_dir)
        self.alias_path = Path(data_dir) / "card_aliases.json"
        self.fetch = fetch or self._fetch
        self.clock = clock or time.monotonic
        self.sessions = OrderedDict()
        self.cache = OrderedDict()
        self.lock = threading.RLock()
        self.release_fetch = release_fetch or (self._fetch_release if fetch is None else lambda _: '')
        self.release_cache = OrderedDict()
        self.wall_clock = wall_clock or time.time
        self.release_path = Path(data_dir) / 'cache/card-releases.json'
        self.release_known = OrderedDict()
        try:
            if self.release_path.stat().st_size > 512*1024:
                raise ValueError('release cache too large')
            document = json.loads(self.release_path.read_text(encoding='utf8'))
            if document.get('version') != 1 or not isinstance(document.get('cards'), dict):
                raise ValueError('invalid release cache')
            for key, record in list(document['cards'].items())[-128:]:
                lines, checked = record.get('lines'), record.get('checked_at')
                if not str(key).isdigit() or not 0 < int(key) <= 0xffffffff:
                    continue
                if not isinstance(lines, list) or not lines or len(lines)>64:
                    continue
                if type(checked) not in (int,float) or not 0 <= checked <= 1e12:
                    continue
                valid = True
                for line in lines:
                    match = re.fullmatch(r'(?:OCG|TCG) [A-Z0-9][A-Z0-9-]{1,40} \((\d{4}-\d{2}-\d{2})\)', line) if isinstance(line,str) else None
                    if not match:
                        valid = False; break
                    try: date.fromisoformat(match.group(1))
                    except ValueError: valid = False; break
                if valid:self.release_known[int(key)] = dict(lines=lines,checked_at=checked)
        except (OSError,ValueError,AttributeError,TypeError):
            self.release_known.clear()

    def _save_release_known(self):
        # Public card metadata only; this is not player/game fact storage.
        try:
            self.release_path.parent.mkdir(parents=True,exist_ok=True)
            payload=dict(version=1,cards={str(k):v for k,v in self.release_known.items()})
            temporary=self.release_path.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf8')
            temporary.replace(self.release_path)
        except OSError:
            pass

    @staticmethod
    def _fetch_release(card_id):
        request = Request(f'https://ygocdb.com/card/{int(card_id)}',
                          headers={'User-Agent':'qq-replay-bot/card-search'})
        with build_opener(ProxyHandler({})).open(request, timeout=1.5) as response:
            body = response.read(2*1024*1024+1)
        if len(body)>2*1024*1024:
            raise ValueError('release page too large')
        return body.decode('utf8')

    def _reply_detail(self, card):
        # 随机一卡等入口不经过 handle()，这里自己加锁保护 release_cache。
        with self.lock:
            card_id = int(card['id'])
            cached = self.release_cache.get(card_id)
            note = ''
            known = self.release_known.get(card_id)
            if cached and self.clock() < cached[0]:
                lines, note = cached[1], cached[2]
            elif known and not cached and 0 <= self.wall_clock()-known['checked_at'] < 86400:
                lines = known['lines']
                remaining = 86400-(self.wall_clock()-known['checked_at'])
                self.release_cache[card_id] = (self.clock()+remaining,lines,note)
            else:
                try:
                    lines = release_lines(self.release_fetch(card_id))
                    if not lines:raise ValueError('release data not parsed')
                    self.release_known[card_id] = dict(lines=lines,checked_at=self.wall_clock())
                    self.release_known.move_to_end(card_id)
                    while len(self.release_known)>128:self.release_known.popitem(last=False)
                    self._save_release_known()
                    ttl = 86400
                except (OSError, ValueError, TypeError):
                    lines = known['lines'] if known else []
                    note = '卡包及发售日期：使用已缓存记录（本次刷新失败）' if lines else '卡包及发售日期：本次读取失败，请稍后重试'
                    ttl = 30
                self.release_cache[card_id] = (self.clock()+ttl, lines, note)
            self.release_cache.move_to_end(card_id)
            while len(self.release_cache)>128:self.release_cache.popitem(last=False)
            reply = self._detail(card)
            extra = '\n'.join(lines)
            if note:extra += ('\n' if extra else '')+note
            return CardReply(str(reply)+'\n\n'+extra, reply.image_url)

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
        try:
            result = self.fetch(path)
        except (OSError, ValueError, TimeoutError):
            time.sleep(1)
            result = self.fetch(path)
        self.cache[path] = (self.clock(), result)
        self.cache.move_to_end(path)
        while len(self.cache) > 128:
            self.cache.popitem(last=False)
        return result

    def _local_search(self, query, user):
        """百鸽不可用时，用本地 YGOPro 中文卡库兜底。"""
        from .statistics_local_cards import local_index
        value = self._alias((query or "").strip())
        tokens = [normalize(part) for part in re.split(r"\s+", value) if normalize(part)]
        if not tokens:
            return "请输入要查的卡名。"
        rows = []
        for info in local_index(self.data_dir).values():
            name = str(info.get("cn_name") or "")
            target = normalize(name)
            if target and all(token in target for token in tokens):
                rows.append(info)
        if not rows:
            return "百鸽查卡接口暂时连不上，本地卡库也没有找到这张卡。"
        rows.sort(key=lambda item: (normalize(item.get("cn_name") or "") != normalize(value),
                                    len(str(item.get("cn_name") or "")),
                                    int(item.get("id") or 0)))
        cards = [dict(id=int(item["id"]), cn_name=item.get("cn_name") or "",
                      text={"desc": item.get("description") or ""}) for item in rows[:20]]
        if len(cards) == 1:
            return self._reply_detail(cards[0])
        lines = [f"本地卡库找到 {len(cards)} 张（百鸽暂时连不上）："]
        lines += [f"{i + 1}. {card_name(card)}（{int(card['id']):08d}）"
                  for i, card in enumerate(cards[:10])]
        lines.append("请直接发送数字选择；候选只保存在当前会话。")
        self.sessions[str(user)] = {"query": value, "rows": cards, "offset": 0, "next": 0, "time": self.clock()}
        return "\n".join(lines)

    def api_has_exact_name(self, query):
        """百鸽 API 中是否存在与 query 归一化后完全同名的卡。"""
        wanted = normalize(query)
        if not wanted:
            return False
        try:
            result = self._request("?" + urlencode({"search": query, "start": 0}))
        except (OSError, ValueError, TypeError, TimeoutError):
            return False
        rows = result.get("result") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            return False
        keys = ("id", "cid", "cn_name", "sc_name", "md_name", "nwbbs_n", "cnocg_n", "jp_name", "en_name")
        for row in rows:
            if not isinstance(row, dict):
                continue
            if any(normalize(str(row.get(key) or "")) == wanted for key in keys):
                return True
        return False

    def _alias(self, query):
        aliases = {"灰": "灰流丽", "泡影": "无限泡影", "g": "增殖的G"}
        if self.alias_path.exists():
            custom = json.loads(self.alias_path.read_text(encoding="utf-8-sig"))
            if not isinstance(custom, dict) or any(not isinstance(v, (str, int)) or isinstance(v, bool) for v in custom.values()):
                raise ValueError("invalid aliases")
            aliases.update({normalize(k): str(v) for k, v in custom.items()})
        return aliases.get(normalize(query), query)

    def _detail(self, card):
        card_id = int(card["id"])
        text = card.get("text") or {}
        lines = [card_name(card), f"卡密：{card_id:08d}"]
        for name in (card.get("sc_name") or text.get("sc_name"), card.get("md_name") or text.get("md_name")):
            if name and name not in lines:
                lines.append(name)
        block = []
        if text.get("types"):
            block.append(str(text["types"])[:7000])
        if text.get("pdesc"):
            scale = self._pendulum_scale(card_id)
            if scale:
                block.append(f"灵摆刻度：{scale[0]} / {scale[1]}")
            block.append("【灵摆效果】")
            block.append(str(text["pdesc"])[:7000])
            if text.get("desc"):
                block.append("【怪兽效果】")
                block.append(str(text["desc"])[:7000])
        elif text.get("desc"):
            block.append(str(text["desc"])[:7000])
        if block:
            lines.append("\n".join(block))
        lines.append(f"百鸽：https://ygocdb.com/card/{card_id}")
        return CardReply("\n\n".join(lines), card_image_payload(card_id, self.data_dir))

    def _pendulum_scale(self, card_id):
        """从本地 YGOPro 文本里取灵摆刻度；没有本地库时返回 None。"""
        try:
            from .statistics_local_cards import card_details
            description = card_details(Path(self.data_dir), card_id).get('description') or ''
            match = re.search(r'←\s*(\d+).*?(\d+)\s*→', description)
            return (int(match.group(1)), int(match.group(2))) if match else None
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def has_candidates(self, user, max_age=300, now=None):
        """当前会话是否有查卡候选；max_age 控制"裸数字/next"的快捷接话窗口。"""
        user = str(user)
        with self.lock:
            session = self.sessions.get(user)
            if not session:
                return False
            moment = self.clock() if now is None else now
            return moment - session["time"] < max_age

    def handle(self, user, action, value=""):
        # handle_text 在线程中执行；保护候选和缓存，避免同一用户并发串页。
        with self.lock:
            try:
                return self._handle(str(user), action, value)
            except OSError:
                if action == "search":
                    return self._local_search(value, user)
                return "查卡接口暂时连不上，请稍后重试。"
            except (ValueError, KeyError, TypeError):
                return "查卡暂时失败：数据不是合法 JSON 或格式异常，请稍后重试。"

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
            return self._reply_detail(session["rows"][index])
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
                return self._reply_detail(matches[0])
            if len(rows) == 1:
                self.sessions.pop(user, None)
                return self._reply_detail(rows[0])
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
        lines.append("2分钟内直接发数字查看，发 next 翻页。序号对应你在当前会话中的列表；群里有候选时同样无需@。")
        return "\n".join(lines)
