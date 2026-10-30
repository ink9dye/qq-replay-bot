"""受原分享令牌保护的单小局解析页；原生内核在独立进程中运行。"""
import html
import json
import os
import subprocess
import sys
import threading
from collections import Counter
from pathlib import Path

from .replay_analysis import ROOT, VERSION, digest_file, sidecar, write_json

_LOCK = threading.Lock()
_RUNNING = set()
_SLOTS = threading.BoundedSemaphore(1)


def request_report(path, game_root, context=None, force=False):
    key = str(path.absolute())
    with _LOCK:
        if key in _RUNNING: return None
    try:
        report = {} if force else json.loads(sidecar(path).read_text(encoding='utf-8'))
        if report.get('version') == VERSION and report.get('digest') == digest_file(path):
            from .replay_analysis import apply_live_evidence
            apply_live_evidence(report, path)
            if context and report.get('match_evidence'):
                # 三局推断只对本分享快照可见的小局做；不把跨快照结论写入共享缓存。
                from .replay_result import supplement_history
                supplement_history(report, context, fetch=lambda *a: {'data': [report['match_evidence']]})
            return report
    except (OSError, ValueError):
        pass
    with _LOCK:
        if key in _RUNNING:
            return None
        if not _SLOTS.acquire(blocking=False):
            return None
        _RUNNING.add(key)
    threading.Thread(target=_work, args=(path, game_root, key), daemon=True).start()
    return None


def _work(path, game_root, key):
    try:
        command = [sys.executable, '-m', 'bot.replay_analysis', str(path), '--game-root', str(game_root), '--history']
        result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=50,
            env=dict(os.environ, PYTHONIOENCODING='utf-8'),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ValueError('录像解析进程未正常结束，请检查内核与游戏文件')
        report = json.loads(result.stdout.decode('utf-8'))
        if path.is_file() and report.get('digest') == digest_file(path):
            write_json(sidecar(path), report)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        if path.is_file():
            try:
                write_json(sidecar(path), {'version': VERSION, 'digest': digest_file(path),
                    'playback': 'failed', 'result': 'unknown', 'winner': None,
                    'players': [], 'events': [], 'warnings': ['解析未完成或超时；请检查内核及游戏版本。']})
            except OSError:
                pass
    finally:
        with _LOCK: _RUNNING.discard(key)
        _SLOTS.release()


def match_context(app, link, replay):
    mapping = app.store.session_map(link['player'])
    current = mapping.get(replay['id'])
    if not current: return None
    games = []
    for identifier in link['replay_ids']:
        known = mapping.get(int(identifier))
        if not known or known['session_id'] != current['session_id']: continue
        item = app.store.get_replay(int(identifier))
        if not item or item['focus_player'] != link['player']: continue
        from .replay_window import replay_in_window
        from .share import safe_replay_file
        if not replay_in_window(item, float(app.clock())) or not safe_replay_file(app.replay_root, item['rel_path']):
            continue
        games.append(dict(game_no=known['game_no'], digest=item['digest'],
                          recorded_at=item['recorded_at'], players=item['players']))
    return {'confirmed': True, 'games': games}


def render_report(report, token, identifier):
    e = lambda value: html.escape(str(value), quote=True)
    back = f'<a class="button" href="/s/{e(token)}">返回录像目录</a>'
    if report is None:
        return (f'<h1>正在解析本小局</h1><p>正在还原操作过程并匹配对战记录，通常需要几秒到一分钟。</p>{back}'
                '<p class="muted">本页会自动刷新。</p><script>setTimeout(()=>location.reload(),2500)</script>')
    players = report.get('players', [])
    names = [p['name'] for p in players]
    first, winner = report.get('first_player'), report.get('winner')
    first_label = names[first] if type(first) is int and first in range(len(names)) else '未知'
    winner_label = ('平局' if winner == 2 else names[winner] if type(winner) is int and winner in range(len(names)) else '未知')
    sources = {'native_replay': '录像内核重放的胜负消息', 'live_server': '观战时服务器的小局胜负消息',
               'history_inferred': '公开对战记录与BO3规则推断'}
    source = sources.get(report.get('result_source'), '暂无可确认的胜负证据')
    status = {'completed': '重放完成', 'responses_exhausted': '全部记录操作已重放', 'failed': '重放未完成', 'live_complete': '已从完整观战消息还原'}.get(report['playback'], '未知')
    parts = ['<style>table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #e8edf5}th{color:#60708a}pre{font-size:13px}</style>',
        f'<header class="hero"><div><div class="eyebrow">单小局解析</div><h1>{e(" vs ".join(names) or "录像解析")}</h1>'
        f'<p>{e(report.get("file", ""))}</p></div>{back}</header>',
        f'<section class="panel"><h2>本小局结果</h2><p>先手：<b>{e(first_label)}</b>　胜者：<b>{e(winner_label)}</b></p>'
        f'<p>重放状态：{e(status)}；已消费 {report.get("responses_consumed", 0)} / {report.get("responses_total", "?")} 次记录操作。</p>'
        f'<p>胜负来源：{e(source)}</p><p>{e(report.get("inference_rule", ""))}</p>'
        f'<p>本局先后手选择权：{e(report.get("initiative_chooser", "未知或首局"))}；实际先手以本局回合消息为准。</p>'
        f'<p>{e(report.get("history_status", ""))}</p>'
        f'<a class="button primary" href="/s/{e(token)}/file/{identifier}">下载原录像</a> '
        f'<a class="button" href="/s/{e(token)}/analysis-json/{identifier}">查看解析原始记录</a></section>']
    parts.append(f'<p><a class="button" href="/s/{e(token)}/analysis-refresh/{identifier}">重新解析并匹配对战记录</a></p>')
    for warning in report.get('warnings', []): parts.append(f'<p class="note">{e(warning)}</p>')
    card_names = report.get('card_names', {})
    for player in players:
        main_used = [c for c in player['used_cards'] if c.get('category') == 'main_side']
        rows = ''.join(f'<tr><td>{e(c["name"])}</td><td>{c["code"]}</td><td>已用过</td></tr>' for c in main_used)
        parts.append(f'<section class="panel"><h2>{e(player["name"])} · {e(player.get("deck_type") or "类型待识别")}</h2>'
            f'<h3>主卡／换入的side</h3><p>本局用过 {len(main_used)} 种</p><div style="overflow:auto"><table><thead><tr><th>卡名</th><th>卡号</th><th>本局记录</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')
        deck = '、'.join(f'{card_names.get(str(code), code)} ×{count}' for code, count in Counter(player['main']).items())
        parts.append(f'<details><summary>本小局主卡构筑 · {len(player["main"])}张</summary><p>{e(deck)}</p></details>')
        extra = player.get('extra_appearances', [{'code': code, 'name': card_names.get(str(code), str(code)),
            'carried': True, 'appeared': False, 'appearance_count': 0} for code in sorted(set(player['extra']))])
        complete = report['playback'] in ('completed', 'responses_exhausted', 'live_complete')
        extra_rows = ''.join(f'<tr><td>{e(c["name"])}</td><td>{c["code"]}</td><td>'
            f'{("已出场" if c["appeared"] else "未出场") if complete else "未知"}</td>'
            f'<td>{c["appearance_count"] if complete else "—"}</td><td>{"是" if c["carried"] else "否"}</td></tr>' for c in extra)
        parts.append(f'<h3>额外卡组 · 单独统计出场</h3><p class="note">后续出场率以当局额外带有该卡的有效小局为分母；出场胜率只计算该卡成功出场的小局。</p>'
            f'<div style="overflow:auto"><table><thead><tr><th>卡名</th><th>卡号</th><th>本局出场</th><th>出场次数</th><th>本局携带</th></tr></thead><tbody>{extra_rows}</tbody></table></div>')
        deck = '、'.join(f'{card_names.get(str(code), code)} ×{count}' for code, count in Counter(player['extra']).items())
        parts.append(f'<details><summary>本小局额外构筑 · {len(player["extra"])}张</summary><p>{e(deck)}</p></details>')
        other = [c for c in player['used_cards'] if c.get('category') == 'other']
        if other:
            parts.append('<details><summary>其他来源卡片（不并入本方构筑统计）</summary><p>'
                         + e('、'.join(c['name'] for c in other)) + '</p></details>')
        parts.append('</section>')
    if report.get('match_evidence'):
        parts.append('<section class="panel"><details><summary>公开整场记录证据（不等同于每小局结果）</summary><pre style="white-space:pre-wrap">'
            + e(json.dumps(report['match_evidence'], ensure_ascii=False, indent=2)) + '</pre></details></section>')
    parts.append(f'<footer class="footer">录像指纹：{e(report.get("digest", ""))}。解析和胜负记录随对应录像在保存满14天后删除。</footer>')
    return ''.join(parts)
