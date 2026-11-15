"""Read existing access/observation logs; print a bounded, credential-free report.

No HTTP requests, database connections, configuration writes, or service actions.
"""
import argparse
from collections import deque
from datetime import datetime, timezone
import ipaddress
import json
import math
from pathlib import Path
import random
import re
import time
import gzip
import zlib

MAX_BYTES = 32 * 1024 * 1024
SAMPLE_LIMIT = 2000
MAX_ROTATIONS = 8
PHASES = ('recordings_total_ms','recordings_eligible_ms','identity_read_ms',
          'recordings_opponent_ms','recordings_naming_ms','recordings_facts_ms',
          'recordings_opening_ms','recordings_filter_ms','recordings_sort_ms',
          'eligible_sql_ms','eligible_source_ms','eligible_source_index_ms')
COUNTS=('source_cache_hits','source_cache_misses','source_index_cache_hits','source_index_cache_misses')


def log_family(paths):
    """Expand real logrotate names; avoid counting path aliases twice."""
    result=[];seen=set()
    for value in paths:
        path=Path(value)
        match=re.fullmatch(r'(.+\.jsonl)(?:\.[1-9][0-9]*(?:\.gz)?)?',path.name)
        if match:
            base=path.with_name(match[1]);candidates=[]
            for number in range(MAX_ROTATIONS,0,-1):
                plain=base.with_name(base.name+'.'+str(number))
                candidates.append(plain if plain.is_file() else Path(str(plain)+'.gz'))
            candidates.extend((base.with_suffix('.previous'),base))
        else:candidates=[path]
        for candidate in candidates:
            try:
                stat=candidate.stat();key=(stat.st_dev,stat.st_ino) if stat.st_ino else str(candidate.resolve())
            except OSError:continue
            if key not in seen:seen.add(key);result.append(candidate)
    return result


def rows(path, coverage=None):
    """Bound each input to its last 32 MiB; ignore partial and malformed lines."""
    path=Path(path)
    info=dict(first=None,last=None,valid=0,truncated=False,status='ok',gzip=path.suffix=='.gz')
    if coverage is not None:coverage[path]=info
    try:
        opener=gzip.open if info['gzip'] else open
        with opener(path,'rb') as stream:
            if not info['gzip']:
                size=stream.seek(0,2);stream.seek(max(0,size-MAX_BYTES))
                info['truncated']=size>MAX_BYTES
                if size>MAX_BYTES:stream.readline()
            remaining=MAX_BYTES
            while remaining>0:
                raw=stream.readline(remaining+1)
                if not raw:break
                remaining-=len(raw)
                if remaining<0:info['truncated']=True;break
                if not raw.endswith(b'\n'):
                    continue
                try:
                    value = json.loads(raw)
                    if isinstance(value, dict):
                        info['valid']+=1
                        stamp=timestamp(value)
                        if stamp is not None and math.isfinite(stamp):
                            info['first']=stamp if info['first'] is None else min(info['first'],stamp)
                            info['last']=stamp if info['last'] is None else max(info['last'],stamp)
                        yield value
                except (ValueError, UnicodeError):
                    continue
            if info['gzip'] and remaining==0:info['truncated']=bool(stream.read(1))
    except (OSError,EOFError,zlib.error):
        info['status']='read_error'
        return


def timestamp(row):
    value = row.get('at', row.get('time'))
    if isinstance(value, (int, float)):
        return float(value)
    try:
        # Legacy timestamps without timezone follow the log-producing host timezone.
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def route(row):
    value = str(row.get('path', '')).split('?', 1)[0]
    # Never echo arbitrary path segments, query strings, users, tokens or URLs.
    if not re.fullmatch(r'/statistics/api/[a-z][a-z-]*(?:/[a-z-]+)?(?:\.png)?', value):
        return None
    return value


def source(row):
    try:
        local = ipaddress.ip_address(str(row.get('ip', ''))).is_loopback
    except ValueError:
        local = False
    ua = str(row.get('ua', '')).lower()
    if 'python-urllib' in ua and local:
        return '回环程序（用途未知）'
    if 'mozilla/' in ua:
        return '浏览器特征（非用户身份确认）'
    return '来源未确认'


def suggestion(path, lock_share, timeouts, pre):
    if timeouts or lock_share >= 0.5:
        return '先审计锁持有范围/重复计算；确认安全后评估缓存或锁隔离'
    if 'side-' in path:
        return '测量相同筛选重复度，再评估有界缓存；不改Side口径'
    if any(key in path for key in ('pie', 'history')):
        return '拆分数据准备与绘图耗时，再评估成图复用'
    if 'search' in path or path.endswith('/groups'):
        return '小范围剖析SQL次数/重复遍历，核对是否逐项查询'
    if pre:
        return '已有预计算响应；检查慢样本是否集中于未命中，勿直接加频'
    return '先拆分SQL/计算阶段；现有计时不足以确定算法瓶颈'


def diagnosis_add(summary, row, stamp):
    """Bounded, same-task pairs only; nested phases are never added together."""
    kind=row['kind']
    d=summary.setdefault(kind,dict(n=0,last=stamp,pairs={}))
    d['n']+=1;d['last']=max(d['last'],stamp)
    total=row.get('recordings_total_ms') if kind=='recordings' else row.get('elapsed_ms')
    for field in (*PHASES[1:], 'queue_wait_ms', 'lock_wait_ms'):
        value=row.get(field)
        if not finite(value) or not finite(total) or total<=0:continue
        # Queue time is outside execution: report its own seconds, never a share.
        p=d['pairs'].setdefault(field,[0,0.,0.,0.,stamp])
        p[0]+=1;p[1]+=value;p[2]+=total;p[3]=max(p[3],value);p[4]=max(p[4],stamp)


def automatic_diagnosis(summary, buckets, resources, coverage, until, fmt):
    lines=['', '### 自动诊断与优先行动', '',
        '规则分析，不是已证实根因或自动修复。排序依据为同任务配对样本的累计已测耗时；不是预计可节省时间。不同参数、缓存状态和版本未分组，不作优化前后比较。',
        '阶段占比只使用同时包含该阶段与总时长的成功任务；嵌套阶段分别解释，不相加。少于3条标为少量样本，旧字段缺失不作零。']
    candidates=[]
    actions={
        'recordings_eligible_ms':('资格处理','bot/top100.py:eligible_views；检查资格范围是否重复获取，优先验证一次任务内复用；保持排名、权限、源文件有效性一致'),
        'recordings_naming_ms':('卡组命名','bot/statistics_recording_filters.py；检查命名是否重复、是否仅处理必要候选；不改命名算法'),
        'recordings_facts_ms':('事实读取与合并','bot/statistics_recording_filters.py:_selected_facts；核查候选范围与卡号读取范围，比较同参数结果'),
        'recordings_opening_ms':('起手证据','bot/statistics_recording_filters.py:_opening_facts；核查读取范围与证据合并，未知不能当零'),
        'identity_read_ms':('身份读取（嵌套）','bot/statistics_match_identity.py:_read_capture_metadata；核查相同digest的重复读取/解析；复用前验证数据更新边界'),
        'eligible_sql_ms':('资格SQL获取/物化（嵌套）','bot/top100.py:eligible_views；检查实际SQL计划、读取行数及重复调用，不能直接认定缺索引'),
        'eligible_source_ms':('源文件有效性检查（嵌套）','bot/top100.py:eligible_views；核对存活缓存失效与路径读取；不延长失效时间绕过有效性检查'),
    }
    for kind,d in sorted(summary.items()):
        certainty='少量样本' if d['n']<3 else '已测多条；代表性未确认'
        for field,(label,action) in actions.items():
            p=d['pairs'].get(field)
            if p and p[1]>=1000 and p[1]/p[2]>=.2:
                confidence='少量样本' if p[0]<3 else certainty
                evidence=f'{kind}：{label}，配对{p[0]}/{d["n"]}条，累计{p[1]/1000:.2f}秒，占配对执行{p[1]/p[2]:.1%}；最近{fmt(p[4])}；{confidence}'
                candidates.append((p[1],evidence,action))
        for field,label,action in (
            ('queue_wait_ms','后台排队','bot/statistics_refresh.py；按相邻任务时间核对队头重任务及重复刷新；不直接增加线程或预计算频率'),
            ('lock_wait_ms','后台等锁','核对持锁任务快照与锁持有范围，先减少重计算；不直接放宽并发')):
            p=d['pairs'].get(field)
            if p and p[3]>=3000:
                confidence='少量样本' if p[0]<3 else certainty
                candidates.append((p[1],f'{kind}：{label}，配对{p[0]}条，累计{p[1]/1000:.2f}秒、最大{p[3]/1000:.2f}秒（独立于执行）；{confidence}；最近{fmt(p[4])}',action))
    lines+=['', '| 优先级 | 已测证据 | 下一步验证（不是已确认修法） |','|---|---|---|']
    for i,(_,evidence,action) in enumerate(sorted(candidates,key=lambda x:-x[0])[:10],1):
        lines.append(f'| {i} | {evidence} | {action} |')
    if not candidates:lines.append('未达到诊断阈值（阶段累计≥1秒且配对占比≥20%，排队/等锁单次≥3秒）；不代表没有瓶颈。')
    lines+=['', '**覆盖、限制与验证要求**']
    recording=summary.get('recordings')
    if not recording:lines.append('- 本窗口没有成功录像计算分阶段样本；请求缓存命中不能替代首次计算证据。')
    else:
        if until-recording['last']>6*3600:lines.append('- 录像样本已超过6小时未更新；不能将旧查询表现当作当前表现。')
        pair=recording['pairs'].get('recordings_eligible_ms')
        if not pair or pair[0]<recording['n']:lines.append('- 部分录像任务缺资格阶段计时；阶段覆盖不完整。')
        filt=recording['pairs'].get('recordings_filter_ms')
        if filt and filt[1]/filt[2]<.01:lines.append('- 已测最终Python条件筛选占配对执行不足1%；优先处理数据准备，不能外推到未测条件。')
    if any(info['truncated'] or info['status']!='ok' for info in coverage.values()):
        lines.append('- 输入存在截断或读取错误；当前排名只代表实际读取范围。')
    io=[r['psi_io_full'] for _,r in resources if finite(r.get('psi_io_full'))]
    mem=[r['mem_available'] for _,r in resources if finite(r.get('mem_available'))]
    if (io and max(io)>=20) or (mem and min(mem)<350):
        io_text=f'{max(io):.1f}%' if io else '未知'
        mem_text=f'{min(mem):.0f}MB' if mem else '未知'
        lines.append(f'- 已读资源点IO full峰值{io_text}、可用内存最低{mem_text}；存在资源压力线索，未证明慢任务因果；优化后复核内存、IO和QQ延迟。')
    else:lines.append('- 已读资源点未达到压力规则或字段不足；不能据此认定资源充足。')
    sample=sum(b['n'] for (path,_),b in buckets.items() if path=='/statistics/api/sample')
    lines.append(f'- 录像详情sample访问{sample}次；缺专属阶段和浏览器端到端证据，下载传输也未独立归因；不能用deck-detail构筑任务代替。')
    lines.append('- 验证每项候选修法：相同参数、首次/复用分开、资源条件可比；核对结果、排序、分页、权限及原统计口径。未记录参数/版本/缓存状态时，不自动宣称提速。')
    lines.append('- HTTP 5xx含准备轮询与回环程序，不能换算用户最终失败率；WAL增长不能由本报告直接归因。')
    return lines


def build_report(logs, observations=(), since=0, until=None):
    logs=log_family(logs)
    coverage={}
    until = time.time() if until is None else until
    buckets = {}
    malformed_time = 0
    rng = random.Random(0)
    resources = deque(maxlen=1000)
    owners = {}
    for file in observations:
        for row in rows(file,coverage):
            stamp = timestamp(row)
            if stamp is not None and since <= stamp <= until:
                resources.append((stamp, row))
    # Bounded joins, never load the full observation history into memory.
    resources = sorted(resources, key=lambda x: x[0])[-1000:]
    for file in logs:
        for row in rows(file,coverage):
            path = route(row)
            if path is None:
                continue
            stamp = timestamp(row)
            if stamp is None:
                malformed_time += 1
                continue
            if not since <= stamp <= until:
                continue
            owner = row.get('lock_owner_timeout')
            if isinstance(owner,dict):
                task = owner.get('task')
                origin = owner.get('source')
                allowed = {'roster','recordings','matrix','card-report','card-table','groups',
                    'deck-candidates','cards-extra','deck-detail','build','side-analysis',
                    'side-recent','usage','winrate','flow','other'}
                if task in allowed and origin in ('request','refresh-worker','unknown'):
                    k = (path,task,origin)
                    if k in owners or len(owners)<256:
                        item=owners.setdefault(k,[0,0.0]);item[0]+=1
                        age=owner.get('held_ms')
                        if finite(age):item[1]=max(item[1],age)
            key = (path, source(row))
            if key not in buckets and len(buckets) >= 256:
                continue
            b = buckets.setdefault(key, dict(n=0, timed=0, failed=0, total=0., wait=0.,
                timeouts=0, pre=0, slow=0, pressure=0, matched=0, values=[], first=stamp, last=stamp))
            b['n'] += 1
            b['first'] = min(b['first'], stamp)
            b['last'] = max(b['last'], stamp)
            status = row.get('status')
            if isinstance(status, int) and status >= 500:
                b['failed'] += 1
            ms = row.get('elapsed_ms')
            if not finite(ms):
                continue
            b['timed'] += 1
            b['total'] += ms
            wait = row.get('lock_wait_ms')
            if finite(wait):
                b['wait'] += wait
            b['timeouts'] += bool(row.get('lock_timeout'))
            b['pre'] += row.get('response_state') == 'precomputed'
            if len(b['values']) < SAMPLE_LIMIT:
                b['values'].append(ms)
            else:
                index = rng.randrange(b['timed'])
                if index < SAMPLE_LIMIT:
                    b['values'][index] = ms
            if ms >= 3000:
                b['slow'] += 1
                near = min(resources, key=lambda x: abs(x[0] - stamp), default=None)
                if near and abs(near[0] - stamp) <= 150:
                    b['matched'] += 1
                    r = near[1]
                    io, available = r.get('psi_io_full'), r.get('mem_available')
                    if (finite(io) and io >= 20) or (finite(available) and available < 350):
                        b['pressure'] += 1
    fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).astimezone().isoformat(timespec='seconds')
    lines = ['## 网页耗时诊断', '', f'生成：{fmt(until)}；筛选起点：{fmt(since)}。',
        '输入：' + '、'.join(Path(p).name for p in logs) + '。读取上限、压缩方向和实际覆盖见末表；不代表完整历史。',
        '按已测累计服务端等待排序；来源分组独立，不把回环程序当用户。失败为HTTP 5xx请求，未去重为用户操作。',
        '服务端计时不含浏览器渲染、代理排队；请求耗时相加不等于可消除时间或CPU成本。',
        'p50/p95最多用2000条蓄水池样本估算；少于20条不展示p95。无耗时的旧日志只计次数。', '',
        '| 接口/来源 | 证据时间 | 请求/已计时 | 5xx | 累计秒 | p50/p95毫秒 | 已测等锁秒 | 预计算响应 | 慢请求压力关联 | 下一步（候选，非根因） |',
        '|---|---|---:|---:|---:|---|---:|---:|---|---|']
    for (path, origin), b in sorted(buckets.items(), key=lambda item: item[1]['total'], reverse=True)[:30]:
        values = sorted(b['values'])
        p = lambda q: round(values[min(len(values)-1, int((len(values)-1)*q))]) if values else '-'
        p95 = p(.95) if b['timed'] >= 20 else '样本不足'
        advice = suggestion(path, b['wait']/max(b['total'], 1), b['timeouts'], b['pre']) if b['timed'] else '缺请求耗时，先补计时，不能排名收益'
        lines.append(f"| {path} / {origin} | {fmt(b['first'])} 至 {fmt(b['last'])} | {b['n']}/{b['timed']} | {b['failed']} | {b['total']/1000:.2f} | {p(.5)}/{p95} | {b['wait']/1000:.2f} | {b['pre']} | {b['pressure']}/{b['matched']}个已匹配慢请求（共{b['slow']}） | {advice} |")
    if not buckets:
        lines.append('没有读到本窗口的接口记录；请核对输入文件、时间和日志轮转，不能据此认定没有慢请求。')
    lines += ['', '### 超时当刻的持锁任务', '',
        '| 等待接口 | 持锁任务/来源 | 超时次数 | 当刻已持锁最大毫秒 |', '|---|---|---:|---:|']
    for (path,task,origin),(count,age) in sorted(owners.items(),key=lambda x:-x[1][0])[:30]:
        lines.append(f'| {path} | {task}/{origin} | {count} | {age:.1f} |')
    if not owners:lines.append('没有带持锁者字段的超时样本；旧日志未知，不表示没有阻塞。')
    workers={}
    diagnosis={}
    worker_files=log_family({Path(p).parent/'statistics-query-timings.jsonl' for p in logs})
    phases={name:[] for name in (*PHASES,*COUNTS)}
    recording_rounds=0
    for file in sorted(worker_files):
        if not file.is_file():continue
        for row in rows(file,coverage):
            stamp=timestamp(row)
            if stamp is None or not since<=stamp<=until:continue
            kind=row.get('kind')
            if kind not in {'roster','recordings','matrix','card-report','cards-extra','deck-detail','deck-candidates','card-table','group-list','deck-options','card-options'}:continue
            if row.get('status')==200:diagnosis_add(diagnosis,row,stamp)
            if kind=='recordings':
                recording_rounds+=1
                for name,summary in phases.items():
                    value=row.get(name)
                    if finite(value):
                        if not summary:summary.extend((0,0.0,0.0))
                        summary[0]+=1;summary[1]+=value;summary[2]=max(summary[2],value)
            w=workers.setdefault(kind,dict(n=0,queue=[],wait=[],hold=[],elapsed=[]));w['n']+=1
            for field,key in (('queue_wait_ms','queue'),('lock_wait_ms','wait'),
                              ('lock_hold_ms','hold'),('elapsed_ms','elapsed')):
                value=row.get(field)
                if finite(value):
                    # Totals/counts/max only: bounded memory even for large logs.
                    if not w[key]:w[key]=[0,0.0,0.0]
                    w[key][0]+=1;w[key][1]+=value;w[key][2]=max(w[key][2],value)
    lines += ['', '### 后台刷新（独立于当前请求）', '',
        '| 任务 | 完成轮次 | 排队/等锁/持锁/执行：已测次数、累计毫秒、最大毫秒 |', '|---|---:|---|']
    for kind,w in sorted(workers.items()):
        detail=' / '.join(f'{key}={v[0]},{v[1]:.1f},{v[2]:.1f}' if v else f'{key}=未知'
                        for key in ('queue','wait','hold','elapsed') for v in (w[key],))
        lines.append(f"| {kind} | {w['n']} | {detail} |")
    if not workers:lines.append('未读到同目录后台计时；仍在运行的任务不会出现在完成轮次中。')
    lines += ['', '### 录像计算分阶段（完成任务）', '',
        f'录像完成轮次：{recording_rounds}；下表只统计明确埋点，缺字段为未知。',
        '| 阶段 | 已测次数 | 累计毫秒 | 最大毫秒 |', '|---|---:|---:|---:|']
    for name in PHASES:
        summary=phases[name]
        lines.append(f'| {name} | {summary[0]} | {summary[1]:.3f} | {summary[2]:.3f} |' if summary else f'| {name} | 0 | 未知 | 未知 |')
    lines += ['', '| 源文件缓存计数 | 已测轮次 | 累计次数 | 单轮最大次数 |', '|---|---:|---:|---:|']
    for name in COUNTS:
        summary=phases[name]
        lines.append(f'| {name} | {summary[0]} | {summary[1]:.0f} | {summary[2]:.0f} |' if summary else f'| {name} | 0 | 未知 | 未知 |')
    lines.append('eligible_sql含资格SQL获取及行物化；eligible_source含路径索引与存活判断，source_index为其嵌套子项。source_cache按唯一digest判断计数，不是stat系统调用次数；源文件检查未执行时字段缺失。以上均属eligible内部，不能与eligible重复相加。')
    lines.append('total为整体；eligible含资格SQL、源文件检查与身份处理；identity_read是eligible/命名内部的身份读取与解析，属于嵌套子项，不能重复相加。facts/opening含SQL获取、遍历和证据合并，不能解释为纯SQL或纯CPU时间。请求缓存复用不会重新产生计算阶段。')
    lines += automatic_diagnosis(diagnosis,buckets,resources,coverage,until,fmt)
    lines += ['', '### 输入覆盖与截断', '',
        '每族最多8份编号轮转，.1与.1.gz同槽优先未压缩；物理文件别名去重，不按相同日志内容去重（避免删掉合法重复请求）。',
        '普通文件读取末尾32MiB；gzip最多读取前32MiB解压内容，超限标截断；以下时间是实际读到的有效记录范围，并非完整历史。',
        '| 文件 | 有效记录 | 首条至末条 | 截断/状态 |', '|---|---:|---|---|']
    for path,info in sorted(coverage.items()):
        span=f"{fmt(info['first'])} 至 {fmt(info['last'])}" if info['first'] is not None else '无有效时间'
        lines.append(f"| {path.name} | {info['valid']} | {span} | {info['truncated']}/{info['status']} |")
    lines.append('持锁者是超时当刻快照，不证明整段等待都由同一任务造成；排队按单调时钟测量。锁持有/执行包含SQL、Python与IO，尚未逐阶段拆分。')
    lines += ['', '等锁字段仅覆盖已埋点路径；缺字段不等于没有等锁。预计算响应来自明确响应头，其余缓存状态未知。',
        '资源关联使用最近150秒内的观测点；IO≥20%或可用内存<350MB仅作筛选线索，不能证明因果。',
        'WAL和后台任务沿用原探针报告按时间对照；本报告暂不自动归因。尚不能自动估算开发成本、节省时间或风险。',
        f'缺有效时间的接口日志：{malformed_time}条。部署/重启前后应分开窗口比较；不跨文件重复输入同一日志。']
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--access', type=Path, nargs='+', required=True)
    parser.add_argument('--observations', type=Path, nargs='*', default=[])
    parser.add_argument('--hours', type=float, default=24)
    args = parser.parse_args()
    if not math.isfinite(args.hours) or args.hours <= 0:
        parser.error('--hours must be positive and finite')
    print(build_report(args.access, args.observations, since=time.time()-args.hours*3600))
