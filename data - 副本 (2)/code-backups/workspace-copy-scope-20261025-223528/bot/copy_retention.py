"""只清理已登记且内容未变化的本项目数据备份；源码备份不在登记范围。"""
import hashlib
import json
import math
from pathlib import Path


def purge(data_dir, now):
    data_dir=Path(data_dir)
    registry=data_dir/'fact-copy-retention.json'
    if not registry.exists():return dict(removed=0,blocked=[])
    entries=json.loads(registry.read_text(encoding='utf-8'))['entries']
    root=(data_dir/'code-backups').resolve()
    result=dict(removed=0,blocked=[])
    for row in entries:
        relative=Path(row['path'])
        deadline=float(row['expires_at'])
        if not math.isfinite(deadline) or relative.is_absolute() or '..' in relative.parts:
            result['blocked'].append('invalid_entry');continue
        path=root/relative
        if not path.resolve().is_relative_to(root):
            result['blocked'].append('outside_backup_root');continue
        if float(now)<deadline or not path.exists():continue
        if path.is_symlink() or not path.is_file():
            result['blocked'].append(row['path']);continue
        if hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
            result['blocked'].append(row['path']);continue
        try:path.unlink();result['removed']+=1
        except OSError:result['blocked'].append(row['path'])
    return result


def next_delay(data_dir, now):
    registry=Path(data_dir)/'fact-copy-retention.json'
    if not registry.exists():return 60.0
    try:
        entries=json.loads(registry.read_text(encoding='utf-8'))['entries']
        root=Path(data_dir)/'code-backups'
        return max(1.0,min([60.0]+[float(row['expires_at'])-float(now) for row in entries
            if not Path(row['path']).is_absolute() and '..' not in Path(row['path']).parts
            and (root/row['path']).is_file()]))
    except (OSError,ValueError,TypeError,KeyError):return 60.0
