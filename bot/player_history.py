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


def athletic_stats(player, rows, now, since=None):
    """按时间排列的竞技对局与积分序列；since 给定时只统计该时刻之后（月度口径仍可用）。"""
    games, seen = [], set()
    for row in rows:
        if row.get('type') != 'athletic' or player not in (row.get('usernamea'), row.get('usernameb')):
            continue
        stamp = moment(row['start_time'])
        if stamp > now or (since is not None and stamp < since):
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


def monthly_stats(player, rows, now):
    start = now.astimezone(CST).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return athletic_stats(player, rows, now, since=start)


def chart_png(player, stats, now):
    # Agg无需桌面窗口；Figure对象与全局绘图锁保证并发QQ查询安全。
    with _PLOT_LOCK:
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.font_manager import FontProperties
        # Linux 生产机没有 Windows 雅黑；显式选一个已安装的中文字体，避免标题/轴标签变方框。
        font_candidates = (
            Path('C:/Windows/Fonts/msyh.ttc'),
            Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
            Path('/usr/share/fonts/truetype/wqy/wqy-microhei.ttc'),
            Path('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'),
        )
        font_path = next((path for path in font_candidates if path.is_file()), None)
        font = FontProperties(fname=str(font_path)) if font_path else FontProperties()
        fig = Figure(figsize=(10, 5), dpi=100, facecolor='white')
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(111)
        points = stats['points'][1:]          # 第1局赛前不算一局，从第1局结算后开始画
        xs = list(range(1, len(points) + 1))
        ax.plot(xs, points, color='blue', marker='o', markersize=4, linewidth=1.2)
        ax.set_title(f'{player} 积分走势', fontproperties=font, fontsize=13)
        ax.set_xlabel('局数', fontproperties=font)
        ax.set_ylabel('积分', fontproperties=font)
        if points:
            ax.set_xlim(0.5, len(points) + 0.5)   # 与参考图一致：横轴从第1局开始
        ax.grid(True)
        fig.subplots_adjust(left=.09, right=.97, top=.92, bottom=.13)
        output = io.BytesIO()
        fig.savefig(output, format='png', facecolor='white')
        return output.getvalue()


class PlayerHistory:
    # 成功结果额外留一份"旧数据"，用于接口抖动时先给数、再说明是缓存。
    STALE_SECONDS = 6 * 3600

    def __init__(self, fetch=None, now=None, render=None, log=None):
        self.fetch = fetch or self._fetch
        self.now = now or (lambda: datetime.now(CST))
        self.render = render or chart_png
        self.log = log
        self.cache = OrderedDict()
        self.last_good = OrderedDict()
        self.lock = threading.Lock()

    def _remember_good(self, key, reply) -> None:
        self.last_good[key] = (time.monotonic(), reply)
        self.last_good.move_to_end(key)
        while len(self.last_good) > 32:
            self.last_good.popitem(last=False)

    def _stale_reply(self, key):
        """接口失败时能拿到的最近一次成功结果（含年龄秒）；没有就返回 None。"""
        if key is None:
            return None
        entry = self.last_good.get(key)
        if not entry:
            return None
        stamp, reply = entry
        age = time.monotonic() - stamp
        if age > self.STALE_SECONDS:
            return None
        return reply, age

    @staticmethod
    def _with_note(reply, note: str):
        if isinstance(reply, CardReply):
            result = CardReply(str(reply) + note, getattr(reply, 'image_url', None))
            result.image_fallback = getattr(reply, 'image_fallback', None)
            return result
        return str(reply) + note

    def _log(self, message: str) -> None:
        if self.log is not None:
            try:
                self.log(message)
            except Exception:
                pass

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
        started = time.monotonic()
        key = None
        now = None
        errors = {}
        try:
            now = self.now().astimezone(CST)
            key = (player, now.year, now.month)
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 300:
                self.cache.move_to_end(key)
                return cached[1]
            deadline = time.monotonic() + 40

            def request(endpoint, params, budget=12, retries=1):
                """单次慢不等于失败：萌卡接口在 5—12 秒间波动，超时重试一次。"""
                seen = []
                for attempt in range(retries + 1):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        errors[endpoint] = 'deadline'
                        raise TimeoutError()
                    try:
                        return self.fetch(endpoint, params, min(budget, remaining))
                    except (OSError, TimeoutError, ValueError) as exc:
                        seen.append(type(exc).__name__)
                        if attempt >= retries:
                            errors[endpoint] = '/'.join(seen)
                            raise

            # 主数据：最近 100 局竞技对局；资料接口并行取，避免两个串行慢请求叠加。
            from concurrent.futures import ThreadPoolExecutor
            pool = ThreadPoolExecutor(max_workers=2)
            history_ok = True
            try:
                # 接口对 page_num=100 会卡住（实测 30 秒超时）；改成两页 50，0.2 秒返回。
                first_future = pool.submit(request, 'history',
                                           {'username': player, 'type': 1, 'page_num': 50}, 10, 1)
                second_future = pool.submit(request, 'history',
                                            {'username': player, 'type': 1, 'page_num': 50, 'page': 2}, 10, 1)
                user_future = pool.submit(request, 'user', {'username': player}, 5, 1)
                try:
                    first = first_future.result()
                except Exception:
                    first, history_ok = None, False
                try:
                    second = second_future.result()
                except Exception:
                    second = None
                try:
                    user = user_future.result()
                except Exception:
                    user = {}
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            if not isinstance(first, dict) or not isinstance(first.get('data'), list):
                first, history_ok = None, False
            if isinstance(first, dict) and len(first.get('data') or []) >= 100:
                second = None
            merged = []
            for part in (first, second):
                if isinstance(part, dict) and isinstance(part.get('data'), list):
                    merged.extend(part['data'])
            merged.sort(key=lambda row: row.get('start_time') or '', reverse=True)
            recent = {'data': merged[:100]} if first is not None else None
            # 一条腿断了不该把整条回复扔出去：历史挂了仍用资料字段作答。
            if not history_ok and not user:
                raise TimeoutError()
            rows = (recent or {}).get('data') if recent is not None else []
            if history_ok and not isinstance(rows, list):
                raise ValueError('invalid history rows')
            rows = rows if isinstance(rows, list) else []
            unique, seen_rows = [], set()
            for row in rows:
                row_key = (row.get('usernamea'), row.get('usernameb'),
                           row.get('start_time'), row.get('end_time'))
                if row_key in seen_rows:
                    continue
                seen_rows.add(row_key)
                unique.append(row)
            rows = unique
            rows = [row for row in rows
                    if row.get('type') == 'athletic'
                    and player in (row.get('usernamea'), row.get('usernameb'))]
            wins = sum(row.get('winner') == player for row in rows)
            draws = sum(row.get('winner') == 'none' for row in rows)
            losses = len(rows) - wins - draws
            sample = len(rows)

            if not any(field in user for field in ('athletic_all', 'athletic_win', 'arena_rank', 'pt')):
                user = {}
            total_games = int(user.get('athletic_all') or 0)
            total_wins = int(user.get('athletic_win') or 0)
            rank = int(user.get('arena_rank') or 0)
            lines = [f'玩家：{player}']
            if sample:
                record = f'{wins}胜{losses}负' + (f'{draws}平' if draws else '')
                lines.append(f'最近 {sample} 局：{record}，胜率 {wins / sample * 100:.2f}%')
            elif history_ok:
                lines.append('最近 100 局：暂无竞技记录')
            else:
                lines.append('最近 100 局：暂未取到（接口超时/失败）')
            if total_games:
                lines.append(f'总场数：{total_games}')
                lines.append(f'总胜率：{total_wins / total_games * 100:.2f}%')
            else:
                lines.append('总场数：暂未取到')
                lines.append('总胜率：暂未取到')
            if not user:
                lines.append('当前排名：暂未取到')
                lines.append('当前分数：暂未取到')
            else:
                lines.append(f'当前排名：{rank}' if rank > 0 else '当前排名：未上榜')
                try:
                    score = number(user.get('pt'))
                except (TypeError, ValueError):
                    score = None
                lines.append(f'当前分数：{score:g}' if score is not None else '当前分数：暂未取到')
            text = '\n'.join(lines)
            if not rows and not user:
                text += '\n未查到竞技资料，请确认玩家名完整且正确。'

            # 图按 APK 口径：最近 N 场（N=本次已取到的竞技对局数），一次请求、不再翻月。
            stats = athletic_stats(player, rows, now)
            if stats['count']:
                try:
                    png = self.render(player, stats, now)
                    reply = CardReply(text, 'base64://' + base64.b64encode(png).decode('ascii'))
                    reply.image_fallback = '\n积分走势图发送失败，请稍后重新查询。'
                except Exception:
                    reply = text + '\n积分走势图生成失败，统计结果仍可查看。'
            else:
                reply = text + ('\n历史接口暂时不可用，本次只显示资料字段。' if not history_ok
                                else '\n最近没有可绘制走势的竞技对局。')
            self.cache[key] = (time.monotonic(), reply)
            self.cache.move_to_end(key)
            while len(self.cache) > 16:
                self.cache.popitem(last=False)
            self._remember_good(key, reply)
            if errors:
                self._log('历史查询部分降级：玩家=%s，耗时 %.1f 秒，失败腿=%s' % (
                    player, time.monotonic() - started, self._errors_text(errors)))
            return reply
        except Exception as exc:
            elapsed = time.monotonic() - started
            stale = self._stale_reply(key)
            if stale is not None:
                cached_reply, age = stale
                minutes = max(1, int(age // 60))
                reply = self._with_note(
                    cached_reply,
                    f'\n（以上为 {minutes} 分钟前的缓存；刚才萌卡接口失败或超时，本次查询 {elapsed:.0f} 秒）')
                self._log('历史查询失败，已返回缓存：玩家=%s，耗时 %.1f 秒，失败腿=%s，缓存 %d 分钟' % (
                    player, elapsed, self._errors_text(errors), minutes))
                return reply
            self._log('历史查询失败：玩家=%s，耗时 %.1f 秒，失败腿=%s，异常=%s' % (
                player, elapsed, self._errors_text(errors), type(exc).__name__))
            return '萌卡历史查询失败或超时，请稍后重试。'
        finally:
            self.lock.release()

    @staticmethod
    def _errors_text(errors: dict) -> str:
        if not errors:
            return '无（非接口异常）'
        return '，'.join(f'{name}={reason}' for name, reason in sorted(errors.items()))
