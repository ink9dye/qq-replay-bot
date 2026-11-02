"""本机录像分享页：按整场分组的响应式卡片，不依赖外部样式或脚本。"""
import html
from collections import Counter
from datetime import datetime

from .deck_links import ourygo_url, relative_age, matchup_labels, card_names
from .match_history import build_match_groups, supplemented_deck
from .replay_window import replay_in_window
from .watch_core import safe_filename_part

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
.deck-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:16px}.deck-col{background:#f6f8fc;border:1px solid var(--line);border-radius:12px;padding:12px 14px;min-width:0}.deck-col h4{margin:0;font-size:14px}.deck-col .muted{font-size:12px}.card-list{list-style:none;margin:8px 0 0;padding:0;font-size:13px}.card-list li{display:flex;justify-content:space-between;gap:8px;padding:2px 0;border-bottom:1px dashed #e8ecf5}.card-list li:last-child{border-bottom:0}.card-count{color:var(--muted);white-space:nowrap}.match-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-start}.deck-details{margin-top:14px;border-top:1px solid var(--line);padding-top:10px}
.card-images{display:flex;flex-wrap:wrap;gap:4px;margin-top:10px}.card-item{position:relative;display:inline-block;line-height:0;border-radius:5px}.card-item img{width:60px;border-radius:5px;box-shadow:0 1px 3px rgba(0,0,0,.2);transition:transform .12s,box-shadow .12s}.card-item:hover img{box-shadow:0 4px 10px rgba(0,0,0,.35);transform:translateY(-2px)}.card-item .card-count{position:absolute;right:2px;bottom:2px;background:rgba(0,0,0,.72);color:#fff;font-size:11px;padding:0 4px;border-radius:4px;line-height:1.5}.card-modal{position:fixed;inset:0;z-index:999;display:flex;align-items:center;justify-content:center}.card-modal[hidden]{display:none}.card-modal-backdrop{position:absolute;inset:0;background:rgba(17,17,17,.5)}.card-modal-panel{position:relative;width:min(680px,92vw);height:min(88vh,720px);background:#fff;border-radius:12px;box-shadow:0 12px 44px rgba(0,0,0,.4);padding:10px}.card-modal-close{position:absolute;top:-13px;right:-13px;width:32px;height:32px;border-radius:50%;border:none;background:#111;color:#fff;font-size:20px;line-height:1;cursor:pointer}.card-modal iframe{width:100%;height:100%;border:0;border-radius:8px}
@media(max-width:640px){.page{padding:24px 15px 40px}h1{font-size:26px}.hero,.match-top{align-items:flex-start;flex-direction:column;gap:12px}.stats{gap:8px}.stat{padding:12px}.stat strong{font-size:22px}.panel,.match{padding:18px}.game{flex-wrap:wrap;gap:10px}.game-no{width:55px}.game>.button{margin-left:65px}.section-heading{align-items:flex-start;flex-direction:column;gap:3px}.deck h3{font-size:16px}.deck-grid{grid-template-columns:1fr}.card-item img{width:48px}}
'''


def e(value):
    return html.escape(str(value), quote=True)


def date(stamp):
    return datetime.fromtimestamp(stamp).strftime('%Y-%m-%d %H:%M:%S')


def deck_card_images(main, extra, side, names):
    """把主卡、额外、side 渲染成三列卡图网格，点击卡图弹出卡片详情。"""
    def column(title, cards):
        if not cards:
            return ''
        items = []
        for code, count in Counter(cards).items():
            raw = str(code).strip()
            if raw.startswith('<') and raw.endswith('>'):
                raw = raw[1:-1].strip()
            name = names.get(code) or names.get(raw) or str(raw)
            badge = f'<span class="card-count">×{count}</span>' if count > 1 else ''
            items.append(
                f'<a class="card-item" href="javascript:void(0)" onclick="openCardModal(\'{raw}\')" title="{e(name)}">'
                f'<img src="{CARD_IMAGE_URL.format(code=raw)}" onerror="this.onerror=null;this.src=&quot;https://cdn.233.momobako.com/ygopro/pics/{raw}.jpg&quot;" alt="{e(name)}" loading="lazy">{badge}</a>'
            )
        return (f'<div class="deck-col"><h4>{e(title)}</h4><span class="muted">{len(cards)} 张</span>'
                f'<div class="card-images">{"".join(items)}</div></div>')
    return ('<div class="deck-grid">' + column('主卡组', main) + column('额外卡组', extra)
            + column('side', side) + '</div>')


def deck_download_button(filename, main, extra, side, label='下载卡组 .ydk'):
    attrs = (f'data-name="{e(filename)}" '
             f'data-main="{",".join(map(str, main))}" '
             f'data-extra="{",".join(map(str, extra))}" '
             f'data-side="{",".join(map(str, side))}"')
    return f'<button class="button" type="button" onclick="downloadYdk(this)" {attrs}>{e(label)}</button>'


CARD_IMAGE_URL = 'https://cdntx.moecube.com/ygopro-super-pre/data/pics/{code}.jpg'
CARD_DETAIL_URL = 'https://ygocdb.com/card/{code}'

CARD_MODAL = (
    '<div id="card-modal" class="card-modal" hidden>'
    '<div class="card-modal-backdrop" onclick="closeCardModal()"></div>'
    '<div class="card-modal-panel"><button class="card-modal-close" type="button" aria-label="关闭" onclick="closeCardModal()">×</button>'
    '<iframe id="card-frame" title="卡片详情"></iframe></div></div>'
)

PAGE_SCRIPT = (
    '<script>'
    'async function refreshArena(){var box=document.getElementById("arena-profile");if(!box)return;'
    'var note=document.getElementById("arena-profile-note");var old=note.textContent;'
    'note.textContent="正在查询萌卡竞技资料…";var ctl=new AbortController();var timer=setTimeout(function(){ctl.abort();},12000);'
    'try{var res=await fetch(box.dataset.profileUrl,{cache:"no-store",signal:ctl.signal});if(!res.ok)throw Error();'
    'var data=await res.json();if(data.score===null){note.textContent="首次同步暂未成功，点击重试；不会显示虚构分数或排名。";return;}'
    'document.querySelectorAll("[data-arena-score]").forEach(function(n){n.textContent=Number(data.score).toLocaleString("en-US",{useGrouping:false,maximumFractionDigits:2})+"分";});'
    'document.querySelectorAll("[data-arena-rank]").forEach(function(n){n.textContent=data.rank>0?"第"+data.rank+"名":"未上榜";});'
    'note.textContent=(data.stale?"接口暂未恢复，显示最近成功记录 · ":"萌卡竞技资料 · ")+"更新于 "+new Date(data.updated_at*1000).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false});'
    '}catch(err){note.textContent=old+"；本次更新失败，可点击重试。";}finally{clearTimeout(timer);}}'
    'document.addEventListener("DOMContentLoaded",refreshArena);'
    'function downloadMatch(btn){var urls=(btn.getAttribute("data-files")||"").split(",");'
    'urls.forEach(function(url,i){if(!url)return;setTimeout(function(){var a=document.createElement("a");'
    'a.href=url;a.download="";document.body.appendChild(a);a.click();document.body.removeChild(a);},i*450);});}'
    'function openCardModal(code){document.getElementById("card-frame").src="https://ygocdb.com/card/"+code;'
    'document.getElementById("card-modal").hidden=false;}'
    'function closeCardModal(){document.getElementById("card-modal").hidden=true;'
    'document.getElementById("card-frame").src="about:blank";}'
    'function downloadYdk(btn){var name=(btn.getAttribute("data-name")||"deck");'
    'var main=(btn.getAttribute("data-main")||"").split(",").filter(Boolean);'
    'var extra=(btn.getAttribute("data-extra")||"").split(",").filter(Boolean);'
    'var side=(btn.getAttribute("data-side")||"").split(",").filter(Boolean);'
    'var lines=["#created by qq-replay-bot","#main"].concat(main,["#extra"],extra,["!side"],side);'
    'var blob=new Blob([lines.join("\\n")+"\\n"],{type:"text/plain"});'
    'var a=document.createElement("a");a.href=URL.createObjectURL(blob);'
    'a.download=name+".ydk";document.body.appendChild(a);a.click();document.body.removeChild(a);'
    'setTimeout(function(){URL.revokeObjectURL(a.href);},2000);}'
    '</script>'
)


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
    deck_groups = build_match_groups(app.store, app.deck_observations(link), link['player'])
    current = supplemented_deck(deck_groups)
    token = e(link['token'])
    player = e(link['player'])
    cache = app.replay_root.parent / 'cache'
    names = card_names(cache)
    points = points_provider(link['player'])
    point_label = f'{points:g}分' if points is not None else '等待首次同步'
    rank = getattr(points, 'rank', None)
    rank_label = (f'第{rank}名' if rank else '未上榜') if rank is not None else '等待首次同步'
    updated = getattr(points, 'updated_at', None)
    sync_note = ('显示最近成功记录 · ' if getattr(points, 'stale', False) else '萌卡竞技资料 · ')
    sync_note += ('更新于 ' + date(updated)) if updated else '稍后刷新可重试'
    profile_html = (f'<section class="panel" id="arena-profile" data-profile-url="/s/{token}/profile"><span class="eyebrow">ARENA PROFILE</span><h2>{e(link["player"])} · 竞技资料</h2>'
                    f'<div class="metrics"><span class="metric">竞技分数<b data-arena-score>{e(point_label)}</b></span>'
                    f'<span class="metric">竞技排名<b data-arena-rank>{e(rank_label)}</b></span></div><p class="note" id="arena-profile-note">{e(sync_note)}</p>'
                    '<button class="button" type="button" onclick="refreshArena()">刷新分数与排名</button></section>')
    deck_html = '<section class="panel deck"><h2>最近观战卡组</h2><p class="muted">当前有效录像中暂未解析到该玩家的卡组。</p></section>'
    if current:
        main, extra, side = current['main'], current['extra'], current['side']
        group = current['group']
        title = title_provider(main, extra, side, cache) or '卡组类型待识别'
        heading = e(f'{link["player"]} · {title}（{relative_age(group["stamp"], float(app.clock()))}） · ')
        heading += f'<span data-arena-score>{e(point_label)}</span> · <span data-arena-rank>{e(rank_label)}</span>'
        try:
            url = ourygo_url(link['player'], main, extra, side)
        except ValueError:
            url = ''
        action = f'<a class="button" href="{e(url)}" target="_blank" rel="noopener noreferrer">在 OURYGO 查看</a>' if url else ''
        deck_filename = safe_filename_part(f'{link["player"]} {title}') or 'deck'
        source_names = '；'.join(path.name for _, _, path in group['entries'])
        deck_html = (f'<section class="panel deck"><span class="eyebrow">DECK ARCHIVE</span><h2>最近观战卡组</h2>'
                     f'<h3>{heading}</h3><div class="metrics"><span class="metric">主卡<b>{len(main)}</b></span>'
                     f'<span class="metric">额外<b>{len(extra)}</b></span><span class="metric">推断side<b>{len(side)}</b></span></div>'
                     f'{deck_card_images(main, extra, side, names)}'
                     f'<div class="actions">{deck_download_button(deck_filename, main, extra, side)}{action}</div>'
                     f'<p class="note">卡组来自该玩家最新观战记录，不受录像列表的对手排名条件限制。'
                     f'本场已观测 {current["game_count"]} 局，'
                     f'以收到的最早第 {current["base_no"]} 局作为主卡与额外基准。后续换入的卡按 watch 逻辑推断为side。</p>'
                     f'<p class="note">另从主卡构筑完全相同的 {current["history_count"]} 场历史录像补充side；'
                     '重复卡按观测到的最大张数合并，不跨场累加。未出现过的side无法推断，不保证为完整副卡组。</p>'
                     f'<details><summary>查看本场来源 · {e(date(group["stamp"]))}</summary><p>{e(source_names)}</p></details></section>')
    matches = []
    for index, group in enumerate(groups, 1):
        _, first, first_path = group['entries'][0]
        matchup, _ = matchup_labels(first, first_path, cache)
        label = '整场对战' if group['confirmed'] else '录像记录 · 场次待确认'
        file_urls = ','.join(f'/s/{token}/file/{replay["id"]}' for _, replay, _ in group['entries'])
        download_button = (f'<button class="button primary" type="button" onclick="downloadMatch(this)" '
                           f'data-files="{e(file_urls)}">下载本场全部录像</button>')
        deck_details = ''
        if group['deck']:
            main, extra, side, _, _ = group['deck']
            try:
                url = ourygo_url(link['player'], main, extra, side)
                ourygo = f'<a class="button" href="{e(url)}" target="_blank" rel="noopener noreferrer">在 OURYGO 查看</a>'
            except ValueError:
                ourygo = ''
            deck_details = (f'<details class="deck-details"><summary>本场卡组 · 主卡 {len(main)} · 额外 {len(extra)} · side {len(side)}</summary>'
                            f'{deck_card_images(main, extra, side, names)}'
                            f'<div class="actions">{deck_download_button(safe_filename_part(link["player"]) or "deck", main, extra, side)}{ourygo}</div></details>')
        games = []
        for order, (number, replay, path) in enumerate(group['entries'], 1):
            game_label = f'录像 {order}' if group['id'].startswith('legacy:') else f'第 {number} 局'
            matchup_game, filename = matchup_labels(replay, path, cache)
            games.append(f'<div class="game"><span class="game-no">{game_label}</span><div class="game-content">'
                         f'<span class="game-name">{e(filename)}</span>'
                         f'<p class="note">{e(matchup_game)} · {e(date(replay["recorded_at"]))} · {path.stat().st_size:,} 字节</p>'
                         f'<a class="button" href="/s/{token}/analysis/{replay["id"]}">解析本小局</a></div></div>')
        matches.append(f'<article class="match"><div class="match-top"><div><span class="pill">{label} {index:02d}</span>'
                       f'<h3>{e(matchup)}</h3><p class="match-meta">收录于 {e(date(group["stamp"]))} · {len(group["entries"])} 份录像</p></div>'
                       f'<div class="match-actions">{download_button}</div></div>'
                       f'{deck_details}'
                       f'<div class="game-list">{"".join(games)}</div></article>')
    empty = '<div class="panel empty"><h3>还没有符合条件的录像</h3><p class="muted">新的录像保存后，请重新向机器人查询。</p></div>'
    return (f'<header class="hero"><div><div class="eyebrow">REPLAY COLLECTION</div><h1>{player} 的观战录像</h1>'
            '<p class="muted">按整场对战整理，保留每局录像与观战卡组。</p></div>'
            f'<span class="pill">链接最晚有效至 {e(date(link["expires_at"]))}</span></header>'
            f'<div class="stats"><div class="stat"><strong>{len(groups)}</strong><span>场对战记录</span></div>'
            f'<div class="stat"><strong>{len(available)}</strong><span>份录像</span></div>'
            '<div class="stat"><strong>14 天</strong><span>可查看范围</span></div></div>'
            + profile_html + deck_html +
            f'<div class="section-heading"><h2>对战记录</h2><a class="button primary" href="/s/{token}/zip">打包下载全部录像</a></div>'
            + (''.join(matches) or empty) +
            f'<footer class="footer"><p>收录范围：{e(date(link["range_start"]))} 至 {e(date(link["created_at"]))}；只展示本链接生成时符合你订阅条件的录像。</p>'
            '<p>录像和卡组保存满14天自动删除。每条分享链接最长有效5小时，上方时间为最晚到期时间。</p>'
            '<p>临时公网地址没有固定保活时长，实际可用至令牌到期、电脑休眠、隧道断线或地址变化中最先发生的时刻。若无法打开，请重新发送“录像 玩家名”取得当前链接。</p>'
            '<p>卡组类型为推测。竞技分数与排名仅在打开本玩家网页或点击刷新时查询，成功结果缓存60秒；失败时保留最近成功记录并标注更新时间。OURYGO链接内含卡组数据，取得后不会随录像页失效。</p></footer>'
            + CARD_MODAL + PAGE_SCRIPT)
