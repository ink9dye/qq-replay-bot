"""只清理已登记且内容未变化的本项目数据备份；源码备份不在登记范围。"""
import hashlib
import json
import math
from pathlib import Path


def copy_path(data_dir,row,workspace_root=None):
    relative=Path(row['path'])
    if relative.is_absolute() or '..' in relative.parts:raise ValueError('invalid_entry')
    scope=row.get('scope','backup')
    if scope=='backup':root=(Path(data_dir)/'code-backups').resolve()
    elif scope=='workspace_validation':
        root=Path(workspace_root or Path.home()/'Documents/ChatGPT/qq机器人').resolve()
        allowed=(Path('phase1/preview-data'),Path('match-page-preview'))
        if not any(relative.is_relative_to(prefix) for prefix in allowed):raise ValueError('outside_validation_scope')
    else:raise ValueError('invalid_scope')
    path=root/relative
    if not path.resolve().is_relative_to(root):raise ValueError('outside_copy_root')
    return path


def purge(data_dir, now,workspace_root=None):
    data_dir=Path(data_dir)
    registry=data_dir/'fact-copy-retention.json'
    if not registry.exists():return dict(removed=0,blocked=[])
    entries=json.loads(registry.read_text(encoding='utf-8'))['entries']
    result=dict(removed=0,blocked=[])
    for row in entries:
        deadline=float(row['expires_at'])
        if not math.isfinite(deadline):
            result['blocked'].append('invalid_entry');continue
        try:path=copy_path(data_dir,row,workspace_root)
        except ValueError:
            result['blocked'].append('invalid_copy_scope');continue
        if float(now)<deadline or not path.exists():continue
        if path.is_symlink() or not path.is_file():
            result['blocked'].append(row['path']);continue
        if hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
            result['blocked'].append(row['path']);continue
        try:path.unlink();result['removed']+=1
        except OSError:result['blocked'].append(row['path'])
    return result


def next_delay(data_dir, now,workspace_root=None):
    registry=Path(data_dir)/'fact-copy-retention.json'
    if not registry.exists():return 60.0
    try:
        entries=json.loads(registry.read_text(encoding='utf-8'))['entries']
        delays=[60.0]
        for row in entries:
            try:path=copy_path(data_dir,row,workspace_root)
            except ValueError:continue
            if path.is_file():delays.append(float(row['expires_at'])-float(now))
        return max(1.0,min(delays))
    except (OSError,ValueError,TypeError,KeyError):return 60.0
