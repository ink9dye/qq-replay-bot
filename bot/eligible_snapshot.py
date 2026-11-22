"""资格视角快照：把 eligible_views 的结果按范围落盘，供报表与额外卡统计复用。

口径不变：快照内容就是 eligible_views 一次真实输出（视角行、名册、分类修订、诊断计数），
刷新任务直接调用原函数，不重写资格逻辑；查询条件仍按请求实时筛选。
对手配对仍由原 _report_opponents 在请求里按需计算（要读 stat_views × stat_builds），
这里不落它的派生结果，避免两套口径。
只读副本，不写业务库：文件在 data/cache/eligible-views.sqlite3，
删掉它（或让定时器停掉）即回到原路径。

三个年龄/兼容性轴必须分开：

- FRESH_AGE（刷新目标）：超过它就该重建，由定时任务决定，不影响请求可用性；
- MAX_AGE（业务可接受的数据年龄）：刷新被跳过或失败时，仍读最近一次成功发布的兼容快照，
  响应里带上数据时间；默认成品本身允许 8 小时，这里取 6 小时更保守；
- 版本/范围/internal/分类修订/名册可用性/质量开关：不兼容就当作没有快照，绝不混用。

名册成员、分类修订在刷新之间可能变化，这与“对局数据 10 分钟延迟”同属一个量级，
读取端只按 expires_at 再做一次保留期过滤。

跨修订复用（v3）：每次导入新对局都会推进 stat_classification_state.revision（新构筑首次命名），
这不是口径变化。所以命中判定不再要求 revision 完全相等，而是要求“变化只能由新增对局解释”：

- revision 增量 > stat_builds 的 MAX(rowid) 增量：说明有已有构筑被改名/清空（只有 assign 写 deck_type，
  而 assign 改已有名字时不会插入新构筑行）→ 判为口径变化，回原路径；
- 排除记录条数变化（删档/无效判定）→ 判为口径变化；
- 名册成员（不是名次）变化 → 100:all / 100:100 的资格边界变了，判为口径变化；
- 系列目录 revision/status 变化 → 标签口径变了，判为口径变化。

允许的漂移只有“新增对局、名次波动、结果从未知变为已判定”，它们与默认成品一样按快照时间标注，
并由下一轮刷新（10 分钟）收敛；6 小时上限只是刷新长期失败时的容错，不是正常年龄。
"""
from __future__ import annotations

import json
import marshal
import os
import sqlite3
import sys
import zlib
from pathlib import Path

VERSION = 3
FRESH_AGE = 1500.0         # 25 分钟：定时器 10 分钟一轮，连续两轮被守卫跳过才到点
NOTE_AGE = 300.0           # 5 分钟：超过它就在结果里标注实际数据时间
MAX_AGE = 6 * 3600.0       # 6 小时：刷新失败时仍可展示的上限，响应会标注数据时间
FILE_NAME = 'eligible-views.sqlite3'
# 2026-11-22: 前 200 内战单独一份快照，读取端按 filters.internal_only 选择；
# 两份文件互不覆盖，守卫/发布逻辑完全复用。
CIVIL_FILE_NAME = 'eligible-views-civil200.sqlite3'
DEFAULT_SCOPE = ('100', 'all')

# 资源门槛分两档：后台构建要保护整机，请求内的兜底查询只需避免明显超载。
REFRESH_LIMITS = dict(available_mb=300, psi=50.0, load=3.5)
REQUEST_LIMITS = dict(available_mb=200, psi=80.0, load=4.0)


class ResourceBusy(RuntimeError):
    """请求内兜底计算超出资源预算：明确拒绝，而不是排队几分钟把整机拖垮。"""

    code = 'resource_busy'
    retry_after = 60

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def path(database, name=FILE_NAME):
    return Path(database).parent / 'cache' / name


def _readonly(target):
    return sqlite3.connect(target.resolve().as_uri() + '?mode=ro', uri=True, timeout=2.0)


def _available_mb():
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _io_pressure():
    try:
        for line in Path('/proc/pressure/io').read_text().splitlines():
            if line.startswith('some'):
                return float(line.split('avg10=')[1].split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0
    return 0.0


def _load_average():
    try:
        return os.getloadavg()[0]
    except (AttributeError, OSError):
        return None


def resource_state(mode='refresh'):
    """返回不满足资源门槛的原因；None 表示可以继续。非 Linux 环境不拦。"""
    limits = REFRESH_LIMITS if mode == 'refresh' else REQUEST_LIMITS
    free = _available_mb()
    if free is not None and free < limits['available_mb']:
        return 'memory_low %.0fMB' % free
    pressure = _io_pressure()
    if pressure >= limits['psi']:
        return 'io_pressure %.0f' % pressure
    load = _load_average()
    if load is not None and load > limits['load']:
        return 'load_high %.2f' % load
    return None


def _database_file(db):
    try:
        row = db.execute('PRAGMA database_list').fetchone()
    except sqlite3.Error:
        return None
    return Path(row[2]) if row and row[2] else None


def _meta(conn):
    try:
        row = conn.execute("SELECT payload FROM blobs WHERE name='meta'").fetchone()
        value = json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _blob(conn, name):
    """行数据用 marshal（同进程同版本写入/读取，加载约为 JSON 的三分之一）；
    旧快照或损坏时退回 JSON，仍解析不了就当未命中。"""
    row = conn.execute('SELECT payload FROM blobs WHERE name=?', (name,)).fetchone()
    if row is None:
        return None
    raw = zlib.decompress(row[0])
    if raw[:1] == b'[' or raw[:1] == b'{':
        return json.loads(raw.decode('utf8'))
    return marshal.loads(raw)


def status(database, name=FILE_NAME):
    """给运维看的快照状态；文件缺失或损坏时返回 None。"""
    target = path(database, name)
    if not target.is_file():
        return None
    conn = None
    try:
        conn = _readonly(target)
        meta = _meta(conn)
    except (sqlite3.Error, OSError):
        return None
    finally:
        if conn is not None:
            conn.close()
    if not meta:
        return None
    return dict(built_at=meta.get('built_at'), scope=meta.get('scope'),
                internal_only=meta.get('internal_only'), views=meta.get('views'),
                build_seconds=meta.get('build_seconds'), revision=meta.get('revision'),
                bytes=target.stat().st_size)


def age_seconds(database, now, name=FILE_NAME):
    """最近一次成功发布的年龄；读不到返回 None。"""
    state = status(database, name)
    built = state.get('built_at') if state else None
    if not isinstance(built, (int, float)):
        return None
    age = now - float(built)
    return age if age >= 0 else None


def publish(database, payload, name=FILE_NAME):
    """整份原子替换：新版本完整写完后才 rename，读取方只会看到一份完整文件。"""
    target = path(database, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + '.tmp')
    tmp.unlink(missing_ok=True)
    meta = dict(payload['meta'])
    meta['version'] = VERSION
    meta['python'] = list(sys.version_info[:2])
    rows = zlib.compress(marshal.dumps(payload['rows']), 6)
    opponents = payload.get('opponents')
    blobs = [('meta', json.dumps(meta, ensure_ascii=False).encode('utf8')), ('rows', rows)]
    if opponents is not None:
        blobs.append(('opponents', zlib.compress(marshal.dumps(opponents), 6)))
    conn = sqlite3.connect(str(tmp), timeout=10.0)
    try:
        conn.execute('PRAGMA journal_mode=DELETE')
        conn.execute('CREATE TABLE blobs(name TEXT PRIMARY KEY, payload BLOB NOT NULL)')
        conn.executemany('INSERT INTO blobs VALUES(?,?)', blobs)
        conn.commit()
    finally:
        conn.close()
    os.chmod(tmp, 0o600)
    # POSIX 下 rename 可以直接覆盖被打开的文件；Windows 上会短暂占用，重试两次即可。
    for attempt in (0, 1, 2):
        try:
            os.replace(tmp, target)
            break
        except PermissionError:
            if attempt == 2:
                raise
            import time as _time
            _time.sleep(0.1)
    return status(database, name)


def _live_state(db, now):
    """名册与分类修订是快照有效性的锚点。"""
    from .top100 import snapshot
    roster = snapshot(db, now)
    revision = db.execute(
        'SELECT revision FROM stat_classification_state WHERE singleton=1').fetchone()[0]
    return roster, revision


def _anchors(db, now):
    """口径锚点：全部是单行/小表读取，热态不到 1 毫秒，可以放在每个请求里。

    revision 只应随“新构筑首次命名”推进；stat_builds 只插入不更新，新构筑命名时 MAX(rowid) 同时增大。
    因此 revision 增量一旦超过 rowid 增量，就说明有已有构筑被改名或清空（assign 改旧名字不插行）。
    """
    from .top100 import snapshot
    roster = snapshot(db, now)
    revision = db.execute(
        'SELECT revision FROM stat_classification_state WHERE singleton=1').fetchone()[0]
    builds_rowid = db.execute('SELECT COALESCE(MAX(rowid),0) FROM stat_builds').fetchone()[0]
    exclusions = db.execute('SELECT COUNT(*) FROM stat_exclusions').fetchone()[0]
    members = tuple(sorted(str(m.get('player') or '') for m in roster.get('members') or ()))
    return dict(roster=roster, revision=int(revision or 0), builds_rowid=int(builds_rowid or 0),
                exclusions=int(exclusions or 0), roster_members=members)


def _drift(meta, anchors, catalog):
    """返回跨修订复用的依据；不能复用返回 None。

    只放行“新增对局带来的一切变化”，其余口径变化一律回原路径。
    """
    built_rev = meta.get('revision')
    built_rowid = meta.get('builds_rowid')
    # 旧快照没有锚点（v2 及更早）：不猜，按未命中处理，等下一轮刷新重建。
    if not isinstance(built_rev, int) or not isinstance(built_rowid, int):
        return None
    if anchors['revision'] < built_rev or anchors['builds_rowid'] < built_rowid:
        return None
    new_builds = anchors['builds_rowid'] - built_rowid
    revision_delta = anchors['revision'] - built_rev
    if revision_delta > new_builds:
        return None                                  # 已有构筑被改名/清空
    if anchors['exclusions'] != meta.get('exclusions'):
        return None                                  # 删档/排除判定变化
    if tuple(meta.get('roster_members') or ()) != anchors['roster_members']:
        return None                                  # 前 100 成员变化
    if catalog is None:
        return None
    if meta.get('catalog_revision') != catalog.get('revision'):
        return None                                  # 系列目录变化：标签口径不可比
    if meta.get('catalog_status') != catalog.get('status'):
        return None
    return dict(revision_delta=revision_delta, new_builds=new_builds)


def _quality_enabled():
    try:
        from . import statistics_quality
        return bool(statistics_quality.enabled())
    except Exception:
        return False


def _load_from(db, now, filters, catalog=None, name=FILE_NAME):
    """返回快照内容或 None；任何不兼容都当作未命中，绝不混用两个口径。"""
    from .statistics_rank_scope import current
    database = _database_file(db)
    if database is None:
        return None
    target = path(database, name)
    if not target.is_file():
        return None
    try:
        conn = _readonly(target)
    except (sqlite3.Error, OSError):
        return None
    try:
        meta = _meta(conn)
        if not meta or meta.get('version') != VERSION:
            return None
        # marshal 的格式随解释器版本变化；Python 升级后先回原路径，等下一轮刷新重写。
        if list(meta.get('python') or ()) != list(sys.version_info[:2]):
            return None
        built = meta.get('built_at')
        if not isinstance(built, (int, float)):
            return None
        age = now - built
        if not 0 <= age <= MAX_AGE:
            return None
        scope = current()
        expected = [scope['own'], scope['opponent']] if scope else list(DEFAULT_SCOPE)
        built_scope = list(meta.get('scope') or ())
        want_internal = bool(filters.internal_only)
        built_internal = bool(meta.get('internal_only'))
        derived_internal = False
        if built_internal:
            # 专为内战构建的快照只有内战行：可服务内战与 100:100，不能服务 100:all。
            if expected == ['100', '100'] and built_scope in (['100', 'all'], ['100', '100']):
                pass
            elif want_internal and expected == built_scope:
                pass
            else:
                return None
        elif expected == built_scope and not want_internal:
            pass
        elif (built_scope == list(DEFAULT_SCOPE) and expected[0] == '100'
              and (want_internal or expected[1] == '100')):
            # 100:all 快照里同局双方都在前 100 时两行都在：
            # 内战（internal_only）与 100:100（对手也限前 100）都能按 digest 在内存里派生。
            derived_internal = True
        else:
            return None

        if getattr(filters, 'scope', 'default') != 'default':
            return None
        if bool(meta.get('quality_enabled')) != _quality_enabled():
            return None
        anchors = _anchors(db, now)
        roster = anchors['roster']
        if bool(roster.get('available')) != bool(meta.get('roster_available')):
            return None
        drift = _drift(meta, anchors, catalog)
        if drift is None:
            return None
        rows = _blob(conn, 'rows')
        opponents = _blob(conn, 'opponents') if meta.get('has_opponents') else None
    except (sqlite3.Error, ValueError, TypeError, OSError, EOFError, zlib.error):
        return None
    finally:
        conn.close()
    if not isinstance(rows, list):
        return None
    # 保留期在两次刷新之间会前进：按扫描时的 now 再过滤一次，语义与原 SQL 一致。
    kept = [row for row in rows
            if isinstance(row, dict) and float(row.get('expires_at') or 0.0) > now]
    # JSON 把元组写成列表；observed_match_players 在原函数里是元组，这里还原成同类型。
    # series 在原函数里本来就是列表，保持不动。
    for row in kept:
        pair = row.get('observed_match_players')
        if isinstance(pair, list):
            row['observed_match_players'] = tuple(pair)
    dropped = len(rows) - len(kept)
    diagnostics = meta.get('diagnostics')
    if dropped and isinstance(diagnostics, dict):
        diagnostics = dict(diagnostics)
        for key in ('after_scope_and_internal_views', 'after_game_no_filter_views'):
            if isinstance(diagnostics.get(key), int):
                diagnostics[key] = max(0, diagnostics[key] - dropped)
    if derived_internal and kept:
        players = {}
        for row in kept:
            players.setdefault(row['digest'], set()).add(row['player'])
        kept = [row for row in kept if len(players.get(row['digest'], ())) >= 2]
        # 100:all 快照没有“内战/100:100 在 game_no 之前”的计数；只保留能确证的一项，
        # 其余诊断计数不回填，避免把 100:all 的数字冒充成派生范围的数字。
        diagnostics = {'after_game_no_filter_views': len(kept)}
    return dict(rows=kept, meta=meta, roster=roster, opponents=opponents,
                diagnostics=diagnostics if isinstance(diagnostics, dict) else {},
                stale=age > FRESH_AGE, age=age, built_at=float(built),
                derived=derived_internal, drift=drift)


def _load(db, now, filters, catalog=None):
    """内战请求先读 200 专用快照；不兼容再回退到 100:all 快照（派生逻辑不变）。"""
    if bool(getattr(filters, 'internal_only', False)):
        result = _load_from(db, now, filters, catalog, CIVIL_FILE_NAME)
        if result is not None:
            return result
    return _load_from(db, now, filters, catalog, FILE_NAME)


def report_basis(db, replay_root, now, filters, catalog):
    """报表/额外卡统计的共用入口：命中返回与 eligible_views 同构的 basis，否则 None。

    未命中一律回原路径，不返回半份结果，也不在请求里触发全量重算。
    命中的旧快照（超过 FRESH_AGE 但在 MAX_AGE 内）带 `snapshot` 元信息，
    调用方据此在结果里标注数据时间。
    """
    loaded = _load(db, now, filters, catalog)
    if loaded is None:
        return None
    from .card_series import SEMANTICS
    basis = dict(roster=loaded['roster'],
                 classification_revision=meta_get(loaded['meta'], 'revision'),
                 views=loaded['rows'], diagnostics=loaded['diagnostics'],
                 series_reference=dict(status=catalog['status'],
                                       revision=catalog['revision'],
                                       semantics=SEMANTICS))
    basis['snapshot'] = dict(built_at=loaded['built_at'], age=round(loaded['age'], 1),
                             stale=loaded['stale'],
                             derived=loaded['derived'],
                             revision_delta=loaded['drift']['revision_delta'],
                             new_builds=loaded['drift']['new_builds'])
    if loaded.get('opponents'):
        basis['opponents'] = loaded['opponents']
    return basis


def meta_get(meta, key, default=None):
    return meta.get(key, default) if isinstance(meta, dict) else default


def build(db, replay_root, now, scope=DEFAULT_SCOPE, internal_only=False):
    """跑一次真实的 eligible_views 并组装快照；失败抛异常，不写盘。"""
    import time
    from .statistics_rank_scope import population_scope
    from .top100 import eligible_views

    started = time.monotonic()
    with population_scope(scope[0], scope[1]):
        from .card_series import reference
        catalog = reference(db)
        basis = eligible_views(db, replay_root, now, internal_only, 'default', catalog,
                               annotate_series=True, with_diagnostics=False)
        rows = basis['views']
    roster, revision = _live_state(db, now)
    anchors = _anchors(db, now)
    diagnostics = dict(basis.get('diagnostics') or {})
    opponents = _opponents(db, rows, catalog)
    meta = dict(built_at=float(now), scope=[scope[0], scope[1]],
                internal_only=bool(internal_only), revision=revision,
                builds_rowid=anchors['builds_rowid'], exclusions=anchors['exclusions'],
                roster_members=list(anchors['roster_members']),
                catalog_revision=catalog.get('revision'), catalog_status=catalog.get('status'),
                roster_updated_at=roster.get('updated_at'),
                roster_available=bool(roster.get('available')),
                quality_enabled=_quality_enabled(), diagnostics=diagnostics,
                views=len(rows),
                build_seconds=round(time.monotonic() - started, 2),
                source_max_recorded=max((row.get('recorded_at') or 0.0) for row in rows) if rows else None,
                has_opponents=bool(opponents))
    return dict(meta=meta, rows=rows, opponents=opponents)


def _opponents(db, rows, catalog):
    """复用报表同一函数，把对手配对结果一起落盘；只保留筛选真正消费的字段。

    读取端命中时直接用这份结果，缺失 digest 仍回原查询，因此不会出现另一套口径。
    """
    if not rows:
        return {}
    from .card_statistics import Filters
    from .statistics_card_report import _report_opponents
    raw = _report_opponents(db, rows, catalog, Filters())
    kept = {}
    for digest, items in raw.items():
        values = []
        for item in items:
            values.append(dict(player=item.get('player'), deck_type=item.get('deck_type'),
                               series=item.get('series')))
        if values:
            kept[digest] = values
    return kept
