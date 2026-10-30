"""萌卡公开竞技历史：北京时间当月统计与积分图，不依赖订阅或录像。"""
from __future__ import annotations

import base64
import io
import json
import math
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .card_search import CardReply

CST = timezone(timedelta(hours=8))
API = 'https://sapi.moecube.com:444/ygopro/arena/'
_PLOT_LOCK = threading.Lock()


def moment(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return (result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result).astimezone(CST)


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('invalid score')
    return result


def monthly_stats(player, rows, now):
    start = now.astimezone(CST).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    games, seen = [], set()
    for row in rows:
        if row.get('type') != 'athletic' or player not in (row.get('usernamea'), row.get('usernameb')):
            continue
        stamp = moment(row['start_time'])
        if not start <= stamp <= now:
            continue
        key = (row.get('usernamea'), row.get('usernameb'), row['start_time'], row.get('end_time'))
        if key in seen:
            continue
        seen.add(key)
        side = 'a' if row['usernamea'] == player else 'b'
        games.append((stamp, number(row['pt' + side + '_ex']), number(row['pt' + side]), row))
    games.sort(key=lambda game: game[0])
    wins = sum(game[3].get('winner') == player for game in games)
    draws = sum(game[3].get('winner') == 'none' for game in games)
    points = ([games[0][1]] + [game[2] for game in games]) if games else []
    return {'games': games, 'count': len(games), 'wins': wins, 'draws': draws,
            'rate': wins / len(games) * 100 if games else None, 'points': points}


def chart_png(player, stats, now):
    # Agg无需桌面窗口；Figure对象与全局绘图锁保证并发QQ查询安全。
    with _PLOT_LOCK:
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.font_manager import FontProperties
        font_path = Path('C:/Windows/Fonts/msyh.ttc')
        font = FontProperties(fname=str(font_path)) if font_path.exists() else FontProperties()
        fig = Figure(figsize=(10, 5.6), dpi=150, facecolor='#f3f6fb')
        FigureCanvasAgg(fig)
        ax = fig.add_axes([.09, .15, .87, .66], facecolor='#ffffff')
        points = stats['points']
        xs = list(range(len(points)))
        ax.plot(xs, points, color='#4263eb', linewidth=1.8, marker='o', markersize=3.1)
        low, high = min(points), max(points)
        pad = max(10, (high - low) * .16)
        ax.set_ylim(low - pad, high + pad)
        ax.set_xlim(-max(1, len(points) * .03), max(1, len(points) - 1) + max(1, len(points) * .03))
        ax.fill_between(xs, points, low - pad, color='#4263eb', alpha=.07)
        ax.grid(axis='y', color='#e8edf5', linewidth=.8)
        ax.set_axisbelow(True)
        for spine in ax.spines.values():
            spine.set_color('#dce3ef')
        ax.tick_params(colors='#586780', labelsize=9)
        ax.set_xlabel('本月竞技对局（0为首场赛前积分）', fontproperties=font, color='#586780', labelpad=10)
        ax.set_ylabel('竞技积分', fontproperties=font, color='#586780')
        ax.annotate(f'{points[0]:.2f}', (0, points[0]), xytext=(4, 10), textcoords='offset points', fontsize=9, color='#586780')
        ax.annotate(f'{points[-1]:.2f}', (xs[-1], points[-1]), xytext=(-4, 12), ha='right', textcoords='offset points', fontsize=10, color='#304fcb', weight='bold')
        fig.text(.09, .91, f'{player} · {now.year}年{now.month}月积分走势', fontproperties=font, fontsize=18, color='#172b4d')
        fig.text(.09, .85, f'{stats["count"]} 场竞技对局  ·  胜率 {stats["rate"]:.2f}%  ·  数据来自萌卡', fontproperties=font, fontsize=10, color='#586780')
        output = io.BytesIO()
        fig.savefig(output, format='png', facecolor=fig.get_facecolor())
        return output.getvalue()


class PlayerHistory:
    def __init__(self, fetch=None, now=None, render=None):
        self.fetch = fetch or self._fetch
        self.now = now or (lambda: datetime.now(CST))
        self.render = render or chart_png
        self.cache = OrderedDict()
        self.lock = threading.Lock()

    @staticmethod
    def _fetch(endpoint, params, timeout):
        request = Request(API + endpoint + '?' + urlencode(params), headers={'User-Agent': 'qq-replay-bot/player-history'})
        with urlopen(request, timeout=timeout) as response:
            body = response.read(8 * 1024 * 1024 + 1)
        if len(body) > 8 * 1024 * 1024:
            raise ValueError('history response too large')
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError('invalid history response')
        return result

    def handle(self, player):
        if not player or len(player) > 100:
            return '格式：历史 玩家名，例如：历史 花无十日红。'
        # 不让公共查询并发淹没萌卡接口；缓存只存公开统计，最多16名、60秒。
        if not self.lock.acquire(timeout=1):
            return '历史查询正在处理中，请稍后再试。'
        try:
            now = self.now().astimezone(CST)
            key = (player, now.year, now.month)
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 60:
                self.cache.move_to_end(key)
                return cached[1]
            deadline = time.monotonic() + 30
            def request(endpoint, params):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                return self.fetch(endpoint, params, min(8, remaining))
            user = request('user', {'username': player})
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            rows, complete = [], False
            for page in range(1, 101):
                payload = request('history', {'username': player, 'type': 1, 'page': page, 'page_num': 100})
                batch = payload.get('data')
                if not isinstance(batch, list):
                    raise ValueError('invalid history rows')
                rows.extend(batch)
                # 接口忽略from_date，必须翻页到月初；不能将第一页冒充全月。
                if not batch or any(moment(row['start_time']) < start for row in batch) or len(rows) >= int(payload['total']):
                    complete = True
                    break
            if not complete:
                return '该玩家当月对局过多，未能完整取得历史；暂不返回不完整的胜率，请稍后重试。'
            stats = monthly_stats(player, rows, now)
            rank = int(user.get('arena_rank') or 0)
            # /user将pt截成整数；最近的当月竞技记录保留结算小数。
            score = stats['points'][-1] if stats['points'] else number(user.get('pt', 1000))
            rate = f'{stats["rate"]:.2f}%' if stats['count'] else '暂无'
            text = (f'玩家：{player}\n{now.month}月场次：{stats["count"]}\n{now.month}月胜率：{rate}'
                    f'\n当前分数：{score:.2f}\n当前排名：{rank if rank > 0 else "未上榜"}'
                    '\n统计口径：北京时间当月竞技对局，胜率=获胜场数/总场数（含平局）。')
            if stats['count']:
                try:
                    png = self.render(player, stats, now)
                    reply = CardReply(text, 'base64://' + base64.b64encode(png).decode('ascii'))
                    reply.image_fallback = '\n积分走势图发送失败，请稍后重新查询。'
                except Exception:
                    reply = text + '\n积分走势图生成失败，统计结果仍可查看。'
            else:
                reply = text + '\n本月暂无竞技记录，无法绘制走势图。'
                if not rows and not rank and not int(user.get('athletic_all') or 0):
                    reply += '\n未查到竞技资料，请确认玩家名完整且正确。'
            self.cache[key] = (time.monotonic(), reply)
            self.cache.move_to_end(key)
            while len(self.cache) > 16:
                self.cache.popitem(last=False)
            return reply
        except Exception:
            return '萌卡历史查询失败或超时，请稍后重试。'
        finally:
            self.lock.release()
