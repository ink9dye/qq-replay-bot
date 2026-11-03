"""QQ text view of worker capacity, queues and the latest received small games."""
import json
import time
from datetime import datetime,timezone,timedelta
from pathlib import Path
from .device_dashboard import DeviceDashboard
from .watch_receipts import read_receipts

TZ=timezone(timedelta(hours=8))


def clean(value,limit=100):
    return ' '.join(str(value).split()).replace('[','［').replace(']','］')[:limit]


def stamp(value):
    try:return datetime.fromtimestamp(float(value),TZ).strftime('%m-%d %H:%M:%S')
    except (TypeError,ValueError,OverflowError,OSError):return '时间未记录'


def recent_games(data_dir, recordings):
    rows=sorted(read_receipts(Path(data_dir)/'watch-receipts.json'),key=lambda r:r.get('received_at',0),reverse=True)
    seen=set();result=[]
    for row in rows:
        if row['digest'] not in seen:
            seen.add(row['digest']);result.append(row)
        if len(result)==9:return result
    for row in recordings:
        if row['digest'] in seen:continue
        seen.add(row['digest'])
        result.append(dict(digest=row['digest'],players=row['players'],worker='',received_at=None,
                           saved_at=row.get('saved_at')))
        if len(result)==9:break
    return result


def format_status(snapshot, games, capture, now):
    fresh=snapshot.get('remote_ok',False)
    workers=[w for w in snapshot.get('workers',[]) if w.get('online')]
    capture_fresh=0<=now-float(capture.get('updated_at') or 0)<=90
    local=int(capture.get('active_observations') or 0) if capture_fresh else None
    waiting=str(max(0,int(capture.get('waiting_rooms') or 0))) if capture_fresh else '未知（采集状态过期）'
    summary=snapshot.get('summary') or {}
    total=str(summary.get('active',0)+local) if fresh and local is not None else '未知'
    lines=['观战设备｜'+stamp(now)+' 北京时间',f'正在观战：{total} 个房间',
           f'等待观战：{waiting} 个房间','']
    if fresh:
        lines.append(f'在线设备：{len(workers)} 台（使用 / 客户端容量）')
        groups={row['ip']:row for row in snapshot.get('ips',[])}
        for w in workers[:30]:
            group=groups.get(w.get('ip'),{})
            free=max(0,min(w['capacity']-w['active'],group.get('limit',w['capacity'])-group.get('active',w['active'])))
            lines.append(f"• {clean(w['name'],64)}：{w['active']}/{w['capacity']}，当前最多再接 {free}")
        if not workers:lines.append('暂无在线设备。')
        lines.append('同一出口的设备共享 IP 余量，“再接”数量不可直接相加。')
    else:lines.append('设备心跳暂时无法读取，未把旧状态当作当前在线。')
    lines+=['',f'最近收到的小局（{len(games)}/9，按录像去重）：']
    for i,g in enumerate(games,1):
        names=' vs '.join(clean(p,70) for p in g.get('players',[])[:4]) or '玩家未记录'
        worker=clean(g.get('worker') or ('历史未记录来源' if g.get('received_at') is None else '来源未确认'),64)
        when='接收 '+stamp(g['received_at']) if g.get('received_at') is not None else '入库 '+stamp(g.get('saved_at'))+'（接收时间未记录）'
        lines.append(f'{i}. {names}\n   来源：{worker}｜{when}')
    if not games:lines.append('来源记录启用后尚未收到小局录像；不回扫旧录像。')
    lines.append('等待包含未到入场时间和待设备接单；已认领不等于录像已收到。')
    return '\n'.join(lines)


def device_message(service):
    dashboard=getattr(service,'_device_dashboard',None)
    if dashboard is None:
        cfg=service.config
        remote=cfg.get('remote') or (cfg.get('watch') or {}).get('remote') or {}
        dashboard=DeviceDashboard(service.store.path,service.replay_root,str(remote.get('token') or ''),clock=service.clock,include_recordings=False)
        service._device_dashboard=dashboard
    snapshot=dashboard.snapshot()
    try:capture=json.loads((service.data_dir/'capture-status.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):capture={}
    return format_status(snapshot,recent_games(service.data_dir,snapshot.get('recordings',[])),capture,float(service.clock()))
