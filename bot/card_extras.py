"""随机卡、私聊猜卡和 OCG 禁限；百鸽全量卡库按 MD5 增量缓存。"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen
from zipfile import ZipFile, BadZipFile

from .card_search import CardReply, card_name, normalize
from .daily_card import DailyCards


class PagedReply(str):
    def __new__(cls, cards):
        obj = super().__new__(cls, '\n\n'.join(cards))
        obj.forward_cards = cards
        return obj


def download(path):
    request = Request('https://ygocdb.com/api/v0/' + path,
                      headers={'User-Agent': 'qq-replay-bot/card-extras'})
    with urlopen(request, timeout=15) as response:
        raw = response.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError('response too large')
    return raw


class CardCatalog:
    def __init__(self, data_dir, fetch=None, clock=None):
        self.directory = Path(data_dir) / 'cache' / 'baige'
        self.fetch = fetch or download
        self.clock = clock or time.time
        self.checked = None
        self.cards = None
        self.lock = threading.RLock()

    def load(self):
        with self.lock:
            now = self.clock()
            if self.cards is not None and self.checked is not None and now - self.checked < 86400:
                return self.cards
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self.directory / 'cards.json'
            local = target.read_bytes() if target.exists() else None
            # 一天最多检查一次；下载失败不把失败时刻记为已更新。
            if local is not None and now - target.stat().st_mtime < 86400:
                payload = local
            else:
                checksum = self.fetch('cards.zip.md5').decode('ascii').strip().strip('"')
                if not re.fullmatch('[a-fA-F0-9]{32}', checksum):
                    raise ValueError('bad checksum')
                if local is not None and hashlib.md5(local).hexdigest().lower() == checksum.lower():
                    payload = local
                else:
                    with ZipFile(BytesIO(self.fetch('cards.zip'))) as archive:
                        entry = archive.getinfo('cards.json')
                        if entry.file_size > 64 * 1024 * 1024:
                            raise ValueError('catalog too large')
                        payload = archive.read(entry)
                    if hashlib.md5(payload).hexdigest().lower() != checksum.lower():
                        raise ValueError('checksum mismatch')
            raw = json.loads(payload.decode('utf-8'))
            if not isinstance(raw, dict):
                raise ValueError('invalid catalog')
            cards = {str(k): v for k, v in raw.items()
                     if isinstance(v, dict) and str(v.get('id', '')).isdigit()
                     and isinstance(v.get('text'), dict)}
            if not cards:
                raise ValueError('empty catalog')
            if payload != local:
                temp = target.with_suffix('.tmp')
                temp.write_bytes(payload)
                temp.replace(target)
            target.touch()
            self.cards, self.checked = cards, now
            return cards


class CardExtras:
    def __init__(self, data_dir, search, catalog=None, clock=None, today=None, choose=None):
        self.search = search
        self.daily_cards = DailyCards(data_dir)
        self.catalog = catalog or CardCatalog(data_dir)
        self.clock = clock or time.monotonic
        self.today = today or (lambda: datetime.now(timezone(timedelta(hours=8))).date())
        self.choose = choose or random.SystemRandom().choice
        self.games = OrderedDict()
        self.expired_reveals = OrderedDict()
        self.reveal_sequence = 0
        self.answer_names = set()
        self.answer_catalog = None
        self.lock = threading.RLock()

    def _expire_games(self):
        now = self.clock()
        for user, game in list(self.games.items()):
            if now - game[0] >= 300:
                del self.games[user]
                detail = self.search._detail(game[1])
                self.reveal_sequence += 1
                self.expired_reveals[self.reveal_sequence] = (
                    user, CardReply('猜卡已满5分钟，自动公布答案：\n\n' + detail, detail.image_url))
        while len(self.expired_reveals) > 1000:
            self.expired_reveals.popitem(last=False)

    def pending_reveals(self):
        with self.lock:
            self._expire_games()
            return [(key, user, reply) for key, (user, reply) in self.expired_reveals.items()]

    def acknowledge_reveal(self, key):
        with self.lock:
            self.expired_reveals.pop(key, None)

    def is_guess_answer(self, user, text):
        with self.lock:
            game = self.games.get(str(user))
            if game is None or self.clock() - game[0] >= 300:
                return False
            if not text.strip() or len(text) > 100:
                return False
            try:
                return normalize(self.search._alias(text.strip())) in self.answer_names
            except (OSError, ValueError, TypeError):
                return False

    def handle(self, user, action, value=''):
        with self.lock:
            try:
                return self._handle(str(user), action, value)
            except (OSError, ValueError, KeyError, TypeError, BadZipFile, sqlite3.Error):
                if action == 'daily_card':
                    return '每日一卡暂时无法获取，请稍后重试；今天已经抽到的卡片不会改变。'
                return '卡片功能暂时无法获取有效数据，请稍后重试。未将缺失数据当作“无限制”。'

    def _random_card(self):
        catalog = self.catalog.load()
        if self.answer_catalog is not catalog:
            self.answer_names = {normalize(name) for card in catalog.values()
                                 for name in [str(card['id']), *self._names(card)]}
            self.answer_catalog = catalog
        cards = [c for c in catalog.values()
                 if int(c['id']) > 0 and not (int((c.get('data') or {}).get('type', 0)) & 0x4000)
                 and (c.get('text') or {}).get('desc')]
        if not cards:
            raise ValueError('no playable cards')
        return self.choose(cards)

    @staticmethod
    def _names(card):
        return [str(card[k]) for k in ('cn_name', 'sc_name', 'md_name', 'nwbbs_n', 'cnocg_n', 'jp_name', 'en_name') if card.get(k)]

    def _handle(self, user, action, value):
        now = self.clock()
        self._expire_games()
        if action == 'daily_card':
            # 按 QQ 号绑定（不是群/会话）：群聊、私聊、换群、重启都是同一张。
            day = self.today()
            card = self.daily_cards.get(user, day, self._random_card)
            detail = self.search._reply_detail(card)
            return CardReply(f'每日一卡｜{day.isoformat()}（北京时间）\n\n' + detail, detail.image_url)
        if action == 'random':
            # 与“查卡”命中单张时走同一条详情格式（含卡包与发售日期）。
            return self.search._reply_detail(self._random_card())
        if action == 'guess_start':
            if user in self.games:
                return '当前会话已有一道猜卡题。直接发送卡名作答，或发送“揭晓猜卡”。'
            card = self._random_card()
            hint = '\n'.join(str(card['text'].get(k) or '') for k in ('types', 'pdesc', 'desc'))
            for name in sorted(set(self._names(card)), key=len, reverse=True):
                hint = re.sub(re.escape(name), '【卡名隐藏】', hint, flags=re.IGNORECASE)
            self.games[user] = (now, card)
            while len(self.games) > 1000:
                self.games.popitem(last=False)
            return '猜猜这是哪张卡（5分钟内有效，到时自动公布答案）：\n\n' + hint[:600] + '\n\n直接发送完整卡名作答，无需前缀或@（也接受外号、其他译名或密码）；发送“揭晓猜卡”提前查看答案。'
        if action in ('guess_answer', 'guess_reveal'):
            game = self.games.get(user)
            if not game:
                return '当前会话没有进行中的猜卡，或题目已过期。发送“猜卡”开始。'
            card = game[1]
            if action == 'guess_answer':
                answer = self.search._alias(value.strip())
                if normalize(answer) not in {normalize(n) for n in self._names(card) + [str(card['id']), f"{int(card['id']):08d}"]}:
                    return '还没猜对，可以继续；请用完整卡名、外号或密码作答。也可以发送“揭晓猜卡”。'
            del self.games[user]
            detail = self.search._detail(card)
            prefix = '猜对了！\n\n' if action == 'guess_answer' else '答案揭晓：\n\n'
            return CardReply(prefix + detail, detail.image_url)
        if action == 'limits_card':
            return self._card_limit(value)
        if action == 'limits_table':
            return self._table(value)
        raise ValueError('unknown action')

    def _limits(self):
        with self.search.lock:
            data = self.search._request('limits.json?show=all')
        if not isinstance(data.get('ja'), dict):
            raise ValueError('missing OCG data')
        return data

    def _select_table(self, month=''):
        data = self._limits()
        tables = dict((data.get('old') or {}).get('ja') or {})
        current = dict(data['ja'])
        tables[current['date']] = current
        target = self.today()
        if month:
            try:
                target = date.fromisoformat(month + '-01')
            except ValueError:
                return None, '月份格式：禁限表 OCG 2020-04。'
            if target > self.today():
                return None, '该月份尚未到来，不能作为已生效的历史禁限表查询。'
        available = sorted(key for key in tables if date.fromisoformat(key) <= target)
        if not available:
            return None, '数据源未收录该日期或更早的 OCG 表，不能推断当时的禁限状态。'
        effective = available[-1]
        table = dict(tables[effective])
        if any(not isinstance(table.get(k), dict) for k in ('forbidden', 'limited', 'semi_limited')):
            raise ValueError('incomplete banlist')
        table['date'] = effective
        return table, ''

    def _card_limit(self, query):
        query = self.search._alias(query)
        cards = self.catalog.load()
        target = normalize(query)
        matches = [c for c in cards.values() if target in {normalize(n) for n in self._names(c) + [str(c['id']), str(c.get('cid', '')), f"{int(c['id']):08d}"]}]
        if len(matches) != 1:
            candidates = [c for c in cards.values() if any(target in normalize(n) for n in self._names(c))]
            if not candidates:
                return '没有找到卡片。请使用完整卡名、外号或卡片密码。'
            return '卡名不唯一，请用完整卡名或密码重新查询：\n' + '\n'.join(f"{card_name(c)}（{int(c['id']):08d}）" for c in candidates[:10])
        card = matches[0]
        if not card.get('cid'):
            return '这张卡缺少官方数据库编号，暂不能确定禁限状态。'
        table, error = self._select_table()
        if error:
            return error
        status = '无限制（未列入此表；通常最多3张）'
        for field, label in [('forbidden', '禁止（0张）'), ('limited', '限制（1张）'), ('semi_limited', '准限制（2张）')]:
            if str(card['cid']) in table[field]:
                status = label
                break
        return f"{card_name(card)}\nOCG：{status}\n生效日期：{table['date']}\n仅查询禁限状态，不判断该卡是否已在OCG发售。\n来源：https://ygocdb.com/api"

    def _table(self, month):
        table, error = self._select_table(month)
        if error:
            return error
        catalog = self.catalog.load()
        title = f"OCG 禁限卡表 · 生效日期 {table['date']}"
        if month:
            title += f'\n查询时点：{month}-01（取当日已生效的最近一期）'
        pages = [title + '\n来源：百鸽 https://ygocdb.com/api\n未列入表不代表已发售或具备赛事使用资格。']
        for key, label in [('forbidden', '禁止（0张）'), ('limited', '限制（1张）'), ('semi_limited', '准限制（2张）')]:
            names = [card_name(catalog[cid]) if cid in catalog else f'{name}（CID {cid}）'
                     for cid, name in table[key].items()]
            if not names:
                pages.append(label + '\n无')
            for start in range(0, len(names), 20):
                pages.append(f'{label} · 共{len(names)}张\n' + '\n'.join(names[start:start + 20]))
        return PagedReply(pages)
