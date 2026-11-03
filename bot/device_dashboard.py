"""Bounded, cached owner-only view of remote workers and recently ingested replays."""
import json
import sqlite3
import threading
import time
import urllib.request
from contextlib import closing
from pathlib import Path

# Upper bound on replay rows inspected for the ingest summary. The replays table
# keeps 14 days (a few thousand rows), so this covers far more than 24 hours while
# never scanning the multi-million-row statistics tables.
MAX_INGEST_ROWS=4000
MAX_RECENT_ROWS=40
HOURLY_BUCKETS=24


class DeviceDashboard:
    def __init__(self, database, replay_root, token='', fetch=None, clock=time.time, include_recordings=True):
        self.database=Path(database)
        self.replay_root=Path(replay_root)
        self.token=token
        self.fetch=fetch or self._fetch_remote
        self.clock=clock
        self.include_recordings=include_recordings
        self._lock=threading.Lock()
        self._cached=None
        self._cached_at=-float('inf')
        self._remote={}
        self._status_at=None
        # 服务端推断“房间结束但没回传”用：上一轮仍在并发列表里的房间 + 最近判定。
        self._rooms_cache={}
        self._ended=[]
        self._receipts_path=Path(database).parent/'watch-receipts.json'

    def _fetch_remote(self):
        if not self.token: raise ValueError('remote_not_configured')
        request=urllib.request.Request('http://127.0.0.1:8791/watch-api/status',
            headers={'Authorization':'Bearer '+self.token})
        with urllib.request.urlopen(request,timeout=2) as response:
            data=json.loads(response.read(1024*1024).decode('utf-8'))
        if data.get('ok') is not True: raise ValueError('remote_unavailable')
        return data

    def _recordings(self):
        with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
            db.row_factory=sqlite3.Row
            # Primary-key tail only: never scan/sort the complete statistics database.
            rows=db.execute('SELECT id,digest,focus_player,players_json,recorded_at,saved_at '
                            'FROM replays ORDER BY id DESC LIMIT 240').fetchall()
        games={}
        for row in rows:
            digest=row['digest']
            if digest in games:
                if row['focus_player'] not in games[digest]['focus']: games[digest]['focus'].append(row['focus_player'])
                continue
            if len(games)>=60: continue
            try: players=json.loads(row['players_json'])
            except (TypeError,ValueError): players=[]
            games[digest]=dict(id=row['id'],digest=digest,players=players if isinstance(players,list) else [],
                focus=[row['focus_player']],recorded_at=row['recorded_at'],saved_at=row['saved_at'])
        return list(games.values())

    def _ingest(self, now):
        """When the last replay was ingested and how many landed per hour (24h).

        Read-only and bounded by primary-key tail: no sorting of the full table.
        """
        with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
            rows=db.execute('SELECT saved_at FROM replays WHERE saved_at IS NOT NULL '
                            'ORDER BY id DESC LIMIT '+str(MAX_INGEST_ROWS)).fetchall()
        stamps=[float(row[0]) for row in rows if row[0] is not None]
        last=max(stamps) if stamps else None
        cutoff=now-HOURLY_BUCKETS*3600
        hour_start=int(now//3600)*3600
        buckets={hour_start-index*3600:0 for index in range(HOURLY_BUCKETS)}
        for value in stamps:
            if value<cutoff: continue
            key=int(value//3600)*3600
            if key in buckets: buckets[key]+=1
        return dict(last_saved_at=last,
                    age_seconds=max(0.0,now-last) if last is not None else None,
                    count_1h=sum(1 for value in stamps if value>=now-3600),
                    count_6h=sum(1 for value in stamps if value>=now-6*3600),
                    count_24h=sum(1 for value in stamps if value>=cutoff),
                    hourly=[dict(t=key,n=buckets[key]) for key in sorted(buckets)])

    def _recent(self, limit):
        """最新入库的小局（只读、主键尾部有界），给 QQ 状态命令用。"""
        with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
            rows=db.execute('SELECT id,digest,focus_player,players_json,recorded_at,saved_at '
                            'FROM replays ORDER BY id DESC LIMIT '+str(MAX_RECENT_ROWS)).fetchall()
        games=[];seen=set()
        for _id,digest,_focus,players_json,recorded_at,saved_at in rows:
            if digest in seen: continue
            seen.add(digest)
            try: players=json.loads(players_json)
            except (TypeError,ValueError): players=[]
            games.append(dict(digest=digest,players=players if isinstance(players,list) else [],
                              recorded_at=recorded_at,saved_at=saved_at))
            if len(games)>=max(1,int(limit)): break
        return games

    def activity(self, limit=9):
        """QQ 状态命令用的有界快照：最近入库时间/场次 + 最新小局；失败返回 None。"""
        now=self.clock()
        try:
            return dict(ingest=self._ingest(now),games=self._recent(limit))
        except (OSError,sqlite3.Error):
            return None

    def _read_receipts(self):
        """最近上传回执（有界）；用来在房间刚消失、任务板已看不到 upload 数时补判。"""
        try:
            data=json.loads(self._receipts_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            return []
        if not isinstance(data,list):
            return []
        return [row for row in data if isinstance(row,dict) and row.get('digest')][-9:]

    @staticmethod
    def _receipt_uploads(room, receipts):
        claimed=float(room.get('claimed_at') or 0)
        players={str(p) for p in (room.get('players') or []) if p}
        count=0
        for row in receipts:
            worker=str(row.get('worker') or '')
            if worker and room.get('worker') and worker!=room.get('worker'):
                continue
            rp={str(p) for p in (row.get('players') or []) if p}
            if players and rp and not (players & rp):
                continue
            try: received=float(row.get('received_at') or 0)
            except (TypeError,ValueError): received=0.0
            if claimed and received and received<claimed:
                continue
            count+=1
        return count

    def snapshot(self):
        with self._lock:
            now=self.clock()
            if self._cached is not None and 0<=now-self._cached_at<5: return self._cached
            remote_ok=True
            try:
                incoming=self.fetch()
                if incoming.get('ok') is not True: raise ValueError('invalid_snapshot')
                self._remote=incoming
                self._status_at=now
            except Exception:
                # Keep the last sample, but do not label stale devices as online/offline.
                remote_ok=False
            workers=[];ips={}
            for name,info in self._remote.get('workers',{}).items():
                active=max(0,int(info.get('active') or 0));capacity=max(1,int(info.get('capacity') or 1))
                online=bool(info.get('online'))
                row=dict(name=str(name)[:64],ip=str(info.get('ip') or ''),active=active,capacity=capacity,online=online)
                workers.append(row)
                if online:
                    group=ips.setdefault(row['ip'],dict(ip=row['ip'],active=0,capacity=0,devices=0))
                    group['active']+=active;group['capacity']+=capacity;group['devices']+=1
            workers.sort(key=lambda item:(not item['online'],item['name']))
            ip_cap=int(self._remote.get('ip_cap') or 0)
            for group in ips.values(): group['limit']=min(group['capacity'],ip_cap) if ip_cap else group['capacity']
            try:
                recordings=self._recordings() if self.include_recordings else []
                ingest=self._ingest(now) if self.include_recordings else None
                recordings_ok=True
            except (OSError,sqlite3.Error): recordings=[];ingest=None;recordings_ok=False
            online=[row for row in workers if row['online']]
            raw_stats=self._remote.get('stats') or {}
            # 服务端任务板已经知道每个已认领任务对应哪一间；这里只做有界透传。
            # 局数不在服务端解析（客户端只负责收发），页面留空或按已回传数展示。
            rooms=[]
            for raw in self._remote.get('rooms') or []:
                if not isinstance(raw,dict):continue
                try:claimed=float(raw.get('claimed_at') or 0)
                except (TypeError,ValueError):claimed=0.0
                try:last_upload=float(raw.get('last_upload_at') or 0)
                except (TypeError,ValueError):last_upload=0.0
                players=raw.get('players') if isinstance(raw.get('players'),list) else []
                rooms.append(dict(worker=str(raw.get('worker') or '')[:64],job=str(raw.get('job') or '')[:64],
                    room=str(raw.get('room') or '')[:80],title=str(raw.get('title') or '')[:120],
                    players=[str(p)[:100] for p in players[:4]],claimed_at=claimed,
                    uploaded=max(0,min(999,int(raw.get('uploaded') or 0))),last_upload_at=last_upload,
                    focus=[str(p)[:100] for p in (raw.get('focus') or [])[:4]],
                    reason=str(raw.get('reason') or '')[:80]))
                if len(rooms)>=64:break
            # 用户口径：房间从并发列表消失（并发数减少）且没有回传，就判定“未能传回录像”。
            # 只读服务端任务板和上传回执，不需要客户端额外上报。
            receipts=self._read_receipts() if rooms or self._rooms_cache else []
            live={}
            for room in rooms:
                if room['uploaded']<=0:
                    room['uploaded']=self._receipt_uploads(room,receipts)
                live[(room['worker'],room['job'])]=room
            if remote_ok:
                for key,old in list(self._rooms_cache.items()):
                    if key in live:continue
                    uploaded=max(int(old.get('uploaded') or 0),self._receipt_uploads(old,receipts))
                    self._ended.append(dict(worker=old.get('worker',''),job=old.get('job',''),
                        room=old.get('room',''),title=old.get('title',''),
                        players=list(old.get('players') or []),claimed_at=float(old.get('claimed_at') or 0),
                        ended_at=now,uploaded=uploaded,
                        verdict=('已回传 %d 个'%uploaded) if uploaded else '未能传回录像'))
                self._ended=self._ended[-40:]
                self._rooms_cache=live
            else:
                for key,room in live.items():
                    if key in self._rooms_cache:
                        self._rooms_cache[key]=dict(self._rooms_cache[key],
                            uploaded=max(int(self._rooms_cache[key].get('uploaded') or 0),int(room.get('uploaded') or 0)))
            ended=self._ended[-40:]
            result=dict(generated_at=now,status_at=self._status_at,remote_ok=remote_ok,
                error='' if remote_ok else '设备状态暂时不可用；以下为最后一次成功采样，不能视为当前状态。',
                workers=workers,ips=list(ips.values()),rooms=rooms,ended=ended,
                summary=dict(online=len(online),active=sum(row['active'] for row in online),
                    capacity=sum(row['limit'] for row in ips.values()),pending=int(self._remote.get('pending') or 0)),
                stats={key:int(raw_stats.get(key) or 0) for key in ('offered','claimed','reported','unclaimed','expired','uploaded','failed')},
                recordings=recordings,recordings_ok=recordings_ok,ingest=ingest)
            self._cached=result;self._cached_at=now
            return result
