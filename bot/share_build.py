"""The common build UI behind an existing expiring, replay-scoped QQ link."""
import json,re,sqlite3,hashlib
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
from .statistics_build_detail import query,ydk,ydk_filename
from .statistics_recording_archive import archive
from .statistics_recording_names import safe_filename


def route(app,path,parts):
    tail=parts[2:]
    if tail and tail[0] not in ('assets','api','replay','recording-zip','build-ydk','card-image'):return None
    from .share import TOKEN_RE,safe_replay_file,attachment_header,format_time
    from .replay_window import replay_in_window
    token=parts[1];link=app.store.get_link(token) if TOKEN_RE.fullmatch(token) else None
    if link is None:return app._text(404,'找不到这个分享链接。')
    if app.clock()>=link['expires_at']:return app._text(410,'分享链接已失效，请重新查询。')
    base='/s/'+token
    def reply(value,kind='application/json; charset=utf-8',filename=None,fallback='download'):
        body=value if isinstance(value,bytes) else (value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)).encode('utf8')
        if app.clock()>=link['expires_at']:return app._text(410,'分享链接已失效，请重新查询。')
        return 200,kind,body,({'Content-Disposition':attachment_header(filename,fallback)}if filename else {})
    if not tail:
        source=Path(__file__).with_name('statistics_build.html').read_text(encoding='utf8')
        source=re.sub(r'<nav class="main-nav".*?</nav>','',source,flags=re.S)
        source=source.replace('这是本机私有链接，接收者需在同一电脑有有效会话。','这是QQ分享链接，仅展示本链接授权的构筑和录像；到期后需重新查询。')
        source=source.replace('复制本机详情链接','复制本页分享链接')
        source=source.replace('下载此范围全部已收录录像ZIP','打包下载此范围全部已收录录像ZIP')
        source=source.replace('<div class="profile">','<p class="note">分享链接有效至 '+format_time(link['expires_at'])+'；重新查询不会延长此链接。</p><div class="profile">')
        source=source.replace('该玩家全部有效小局','本链接全部有效小局')
        source=source.replace('<script src="/statistics/assets/statistics_nav.js"></script>','')
        return reply(source.replace('/statistics/assets/',base+'/assets/'),'text/html; charset=utf-8')
    if len(tail)==2 and tail[0]=='assets':
        name=tail[1]
        if name not in ('statistics_theme.css','statistics_ui.css','statistics_nav.css','statistics_build.js','statistics_pt.js','statistics_samples.js'):return app._text(404,'找不到资源。')
        if name=='statistics_samples.js':
            source="window.StatisticsSamples={el:(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n},time:t=>new Date(t*1000).toLocaleString('zh-CN',{hour12:false}),api:async path=>{const r=await fetch("+json.dumps(base+'/api/')+"+path,{cache:'no-store'});if(!r.ok)throw Error('分享链接或来源已失效，或不在授权范围内。');return r.json()}};"
        else:source=Path(__file__).with_name(name).read_text(encoding='utf8')
        if name=='statistics_build.js':
            source=source.replace("player=args.get('player')||''",'player='+json.dumps(link['player'],ensure_ascii=False))
            source=source.replace('/statistics/sample/',base+'/replay/').replace('/statistics/build?',base+'?').replace('/statistics/recording-zip?',base+'/recording-zip?').replace('/statistics/build-ydk?',base+'/build-ydk?').replace('/statistics/card-image/',base+'/card-image/')
            source=source.replace('已复制本机私有链接','已复制分享链接').replace('该玩家全部有效小局','本链接全部有效小局')
        if name=='statistics_pt.js':source=source.replace("new URLSearchParams(location.search).get('player')",json.dumps(link['player'],ensure_ascii=False))
        return reply(source,'text/css; charset=utf-8'if name.endswith('.css')else'application/javascript; charset=utf-8')
    args=parse_qs(urlsplit(path).query)
    if tail==['api','pt-history']:
        from .statistics_pt import InteractiveHistory
        if not hasattr(app,'share_pt_history'):app.share_pt_history=InteractiveHistory()
        return reply(app.share_pt_history.query(link['player']))
    if tail==['api','card-info'] or len(tail)==2 and tail[0]=='card-image':
        code=args.get('code',[''])[0] if tail[0]=='api' else tail[1]
        if not code.isdigit() or not 0<int(code)<=4294967295:return app._text(400,'卡号无效。')
        from .statistics_local_cards import card_details,picture
        if tail[0]=='api':return reply(card_details(app.store.path.parent,int(code)))
        found=picture(app.store.path.parent,int(code))
        return reply(found[0].read_bytes(),found[1])if found else app._text(404,'暂无卡图。')
    with closing(sqlite3.connect(app.store.path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN')
        def eligible(ids):
            result={}
            for identifier in ids:
                replay=app.store.get_replay(int(identifier))
                if not replay or replay['focus_player']!=link['player'] or not replay_in_window(replay,app.clock()) or not safe_replay_file(app.replay_root,replay['rel_path']):continue
                for fact in db.execute('SELECT digest FROM stat_files WHERE replay_id=?',(int(identifier),)):result[fact['digest']]=int(identifier)
            return result
        downloads=eligible(link['replay_ids']);observations=eligible(link.get('deck_replay_ids',link['replay_ids']));allowed=set(downloads)|set(observations)
        digest=args.get('digest',[None])[0];scope=args.get('scope',['build'])[0]
        if digest is not None and (not re.fullmatch('[0-9a-f]{64}',digest) or digest not in allowed):return app._text(404,'来源不在本链接授权范围内。')
        if scope not in ('build','player'):return app._text(400,'范围无效。')
        if len(tail)==2 and tail[0]=='replay':
            identifier=downloads.get(tail[1])
            if identifier is None:return app._text(404,'本链接不能下载这份录像。')
            if not db.execute("SELECT 1 FROM stat_views v JOIN stat_games g ON g.digest=v.digest WHERE v.digest=? AND v.player=? AND g.expires_at>? AND NOT EXISTS(SELECT 1 FROM stat_exclusions e WHERE e.digest=v.digest AND (e.player='' OR e.player=v.player) AND e.scope='default')",(tail[1],link['player'],app.clock())).fetchone():return app._text(404,'来源已失效或被排除。')
            status,kind,blob=app.page(token,'file',str(identifier))
            if status==200 and hashlib.sha256(blob).hexdigest()!=tail[1]:return app._text(409,'录像来源已变化。')
            detail=query(db,app.replay_root,app.clock(),link['player'],tail[1],'player',allowed_digests=allowed,history_digests=set(downloads))
            match=next((g for g in detail['matches'] if g['digest']==tail[1]),None) if detail else None
            name=match['download_filename'] if match else safe_filename(link['player']+' '+tail[1][:12])
            return (status,kind,blob,{'Content-Disposition':attachment_header(name,'replay.yrp')})
        if tail==['recording-zip']:
            try:
                blob,name,_=archive(db,app.replay_root,app.clock(),link['player'],digest,scope,args.get('match',[None])[0],clock=app.clock,allowed_digests=allowed,history_digests=set(downloads))
                return reply(blob,'application/zip',name)
            except ValueError:return app._text(409,'当前授权范围没有可下载的有效录像。')
        if tail not in (['api','build-detail'],['build-ydk']):return app._text(404,'找不到这个页面。')
        result=query(db,app.replay_root,app.clock(),link['player'],digest,scope,allowed_digests=allowed,history_digests=set(downloads))
        if tail==['build-ydk']:
            if result is None:return app._text(404,'本链接暂无有效构筑。')
            try:
                inferred=args.get('inferred')==['1']
                name,fallback=ydk_filename(result,inferred)
                return reply(ydk(result,inferred),'text/plain; charset=utf-8',name,fallback)
            except ValueError:return app._text(409,'推测Side存在冲突，不能导出。')
        if result:
            from .statistics_card_names import index
            from .card_search import card_name
            names=index(app.store.path.parent)
            for card in result['cards']+result['inferred_side']['cards']:
                info=names.get(str(card['code']));card['name']=card_name(info)if info else'卡名暂缺'
                card['swap_contexts']=[c for c in card.get('swap_contexts',[]) if c['digest']in downloads]
            for match in result['matches']:
                for card in match['changes']['added']+match['changes']['removed']:
                    info=names.get(str(card['code']));card['name']=card_name(info)if info else'卡名暂缺'
        return reply(result)
