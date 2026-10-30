"""本机录像分享页：按整场分组的响应式卡片，不依赖外部样式或脚本。"""
import html
from datetime import datetime

from .deck_links import ourygo_url, relative_age, matchup_labels
from .match_history import build_match_groups, supplemented_deck
from .replay_window import replay_in_window

PAGE_CSS = '''
:root{color-scheme:light;--ink:#18243c;--muted:#68758c;--line:#e5eaf3;--accent:#4965e8}
*{box-sizing:border-box}body{margin:0;background:#f3f5fb;color:var(--ink);font:15px/1.65 "Segoe UI","Microsoft YaHei",sans-serif}
.page{max-width:1080px;margin:auto;padding:42px 24px 60px}h1,h2,h3,p{margin:0}h1{font-size:32px;letter-spacing:-.6px;line-height:1.35}h2{font-size:20px}h3{font-size:18px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}.eyebrow{font-size:12px;letter-spacing:2px;color:var(--accent);font-weight:700;margin-bottom:10px}
.muted,.note{color:var(--muted)}.note{font-size:13px}.hero{display:flex;justify-content:space-between;align-items:center;gap:24px;margin-bottom:24px}.hero p{margin-top:10px}
.pill{display:inline-block;background:#eef1ff;color:#435bc6;padding:5px 12px;border-radius:99px;font-size:12px;white-space:nowrap}.button{display:inline-flex;justify-content:center;align-items:center;padding:9px 17px;border:1px solid var(--line);border-radius:10px;background:white;color:var(--ink);font-weight:600;font-size:14px;white-space:nowrap;gap:6px}
.button:hover{text-decoration:none;background:#f6f8ff;border-color:#c5d0fc}.primary{background:var(--accent);border-color:var(--accent);color:white}.primary:hover{background:#3b54d0;color:white}.actions{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:22px}.stat,.panel,.match{border:1px solid var(--line);background:white;border-radius:18px;box-shadow:0 5px 22px #1a315006}.stat{padding:16px 20px}.stat strong{font-size:25px;display:block}.stat span{font-size:13px;color:var(--muted)}
.panel{padding:24px;margin-bottom:28px}.deck{border-top:3px solid var(--accent);background:linear-gradient(125deg,#fff,#f8f9ff)}.deck h3{margin:12px 0}.metrics{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}.metric{padding:7px 12px;background:#eef2fc;border-radius:9px;font-size:13px}.metric b{margin-left:7px}
.section-heading{display:flex;justify-content:space-between;align-items:baseline;margin:25px 0 14px;gap:15px}.match{padding:22px;margin-bottom:16px}.match-top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}.match h3{font-size:17px;margin:10px 0 4px}.match-meta{font-size:13px;color:var(--muted);margin-bottom:12px}
.game-list{margin-top:16px;border-top:1px solid var(--line)}.game{display:flex;gap:14px;align-items:center;padding:16px 0;border-bottom:1px solid #eef1f7}.game:last-child{border-bottom:0;padding-bottom:0}.game-no{flex-shrink:0;width:64px;padding:7px 0;text-align:center;background:#f0f3fb;border-radius:9px;font-weight:600;font-size:13px}.game-content{flex:1;min-width:0}.game-name{font-size:14px;display:block;overflow-wrap:anywhere}.game .note{font-size:12px;margin-top:3px}.empty{text-align:center;padding:34px 20px}.footer{font-size:12px;color:var(--muted);margin-top:22px}.footer p+p{margin-top:6px}details{margin-top:12px}summary{cursor:pointer;color:var(--muted);font-size:13px}details p{font-size:12px;overflow-wrap:anywhere;margin-top:8px}
@media(max-width:640px){.page{padding:24px 15px 40px}h1{font-size:26px}.hero,.match-top{align-items:flex-start;flex-direction:column;gap:12px}.stats{gap:8px}.stat{padding:12px}.stat strong{font-size:22px}.panel,.match{padding:18px}.game{flex-wrap:wrap;gap:10px}.game-no{width:55px}.game>.button{margin-left:65px}.section-heading{align-items:flex-start;flex-direction:column;gap:3px}.deck h3{font-size:16px}}
'''


def e(value):
    return html.escape(str(value), quote=True)


def date(stamp):
    return datetime.fromtimestamp(stamp).strftime('%Y-%m-%d %H:%M:%S')


def directory_body(app, link, title_provider, points_provider):
    from .share import safe_replay_file
    available = []
    for identifier in link['replay_ids']:
        replay = app.store.get_replay(int(identifier))
        if not replay or replay['focus_player'] != link['player'] or not replay_in_window(replay, float(app.clock())):
            continue
        path = safe_replay_file(app.replay_root, replay['rel_path'])
        if path:
            available.append((replay, path))
    groups = build_match_groups(app.store, available, link['player'])
    current = supplemented_deck(groups)
    token = e(link['token'])
    player = e(link['player'])
    cache = app.replay_root.parent / 'cache'
    deck_html = '<section class="panel deck"><h2>最近观战卡组</h2><p class="muted">当前有效录像中暂未解析到该玩家的卡组。</p></section>'
    if current:
        main, extra, side = current['main'], current['extra'], current['side']
        group = current['group']
        title = title_provider(main, extra + side, cache) or '卡组类型待识别'
        points = points_provider(link['player'])
        point_label = f'{points:g}分' if points is not None else '分数暂不可用'
        heading = f'{link["player"]} · {title}（{relative_age(group["stamp"], float(app.clock()))}） · {point_label}'
        try:
            url = ourygo_url(link['player'], main, extra, side)
        except ValueError:
            url = ''
        action = f'<a class="button primary" href="{e(url)}" target="_blank" rel="noopener noreferrer">查看卡组 · OURYGO</a>' if url else ''
        source_names = '；'.join(path.name for _, _, path in group['entries'])
        deck_html = (f'<section class="panel deck"><span class="eyebrow">DECK ARCHIVE</span><h2>最近观战卡组</h2>'
                     f'<h3>{e(heading)}</h3><div class="metrics"><span class="metric">主卡<b>{len(main)}</b></span>'
                     f'<span class="metric">额外<b>{len(extra)}</b></span><span class="metric">推断side<b>{len(side)}</b></span></div>'
                     f'<div class="actions">{action}</div><p class="note">本场已收录 {current["game_count"]} 局，'
                     f'以收到的最早第 {current["base_no"]} 局作为主卡与额外基准。后续换入的卡按 watch 逻辑推断为side。</p>'
                     f'<p class="note">另从主卡构筑完全相同的 {current["history_count"]} 场历史录像补充side；'
                     '重复卡按观测到的最大张数合并，不跨场累加。未出现过的side无法推断，不保证为完整副卡组。</p>'
                     f'<details><summary>查看本场来源 · {e(date(group["stamp"]))}</summary><p>{e(source_names)}</p></details></section>')
    matches = []
    for index, group in enumerate(groups, 1):
        _, first, first_path = group['entries'][0]
        matchup, _ = matchup_labels(first, first_path, cache)
        label = '整场对战' if group['confirmed'] else '录像记录 · 场次待确认'
        cards = ''
        if group['deck']:
            main, extra, side, _, _ = group['deck']
            try:
                url = ourygo_url(link['player'], main, extra, side)
                cards = f'<a class="button" href="{e(url)}" target="_blank" rel="noopener noreferrer">本场卡组 · side {len(side)}</a>'
            except ValueError:
                pass
        games = []
        for order, (number, replay, path) in enumerate(group['entries'], 1):
            game_label = f'录像 {order}' if group['id'].startswith('legacy:') else f'第 {number} 局'
            matchup_game, filename = matchup_labels(replay, path, cache)
            games.append(f'<div class="game"><span class="game-no">{game_label}</span><div class="game-content">'
                         f'<a class="game-name" href="/s/{token}/file/{replay["id"]}" download="{e(filename)}">{e(filename)}</a>'
                         f'<p class="note">{e(matchup_game)} · {e(date(replay["recorded_at"]))} · {path.stat().st_size:,} 字节</p></div>'
                         f'<a class="button" href="/s/{token}/file/{replay["id"]}" download="{e(filename)}">下载录像</a></div>')
        matches.append(f'<article class="match"><div class="match-top"><div><span class="pill">{label} {index:02d}</span>'
                       f'<h3>{e(matchup)}</h3><p class="match-meta">收录于 {e(date(group["stamp"]))} · {len(group["entries"])} 份录像</p></div>{cards}</div>'
                       f'<div class="game-list">{"".join(games)}</div></article>')
    empty = '<div class="panel empty"><h3>还没有符合条件的录像</h3><p class="muted">新的录像保存后，请重新向机器人查询。</p></div>'
    return (f'<header class="hero"><div><div class="eyebrow">REPLAY COLLECTION</div><h1>{player} 的观战录像</h1>'
            '<p class="muted">按整场对战整理，保留每局录像与观战卡组。</p></div>'
            f'<span class="pill">链接有效至 {e(date(link["expires_at"]))}</span></header>'
            f'<div class="stats"><div class="stat"><strong>{len(groups)}</strong><span>场对战记录</span></div>'
            f'<div class="stat"><strong>{len(available)}</strong><span>份录像</span></div>'
            '<div class="stat"><strong>7 天</strong><span>可查看范围</span></div></div>'
            + deck_html +
            f'<div class="section-heading"><h2>对战记录</h2><a class="button primary" href="/s/{token}/zip">打包下载全部录像</a></div>'
            + (''.join(matches) or empty) +
            f'<footer class="footer"><p>收录范围：{e(date(link["range_start"]))} 至 {e(date(link["created_at"]))}；只展示本链接生成时符合你订阅条件的录像。</p>'
            '<p>录像保存满7天后不再提供查看或下载，本地文件保留。每条分享链接生成5小时后失效，新录像需重新查询。</p>'
            '<p>卡组类型为推测，竞技分数最多缓存5分钟。OURYGO链接内含卡组数据，取得后不会随录像页失效。</p></footer>')
