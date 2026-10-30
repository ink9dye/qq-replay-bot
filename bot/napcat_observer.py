"""Read-only process/port/console observer. No login, API, messages or process control."""
import argparse
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

VERSION = 1


class Journal:
    def __init__(self, directory, limit=1024*1024, filename='napcat-observer.jsonl'):
        if filename not in ('napcat-observer.jsonl','napcat-launcher.jsonl'):raise ValueError('invalid_log_name')
        self.path = Path(directory)/filename
        self.limit = limit
        self.session = uuid.uuid4().hex[:12]

    def emit(self, event, **fields):
        allowed = {'target_pid','parent_pid','exit_code','port_open','qq_state','state_age_seconds',
                   'kind','reason','source','available','processes','incident','error_type'}
        row = dict(time=time.time(), observer_pid=os.getpid(), version=VERSION,
                   session=self.session, event=event)
        for key, value in fields.items():
            if key not in allowed: continue
            if type(value) in (bool,int) or value is None: row[key]=value
            elif isinstance(value,str) and re.fullmatch('[a-zA-Z_0-9]{1,64}',value): row[key]=value
        raw=json.dumps(row)+'\n'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        paths=[self.path.with_name(self.path.name+s) for s in ('','.1','.2')]
        for p in paths:
            if p.exists() and time.time()-p.stat().st_mtime>7*86400:p.unlink()
        if self.path.exists() and self.path.stat().st_size+len(raw)>self.limit:
            paths[2].unlink(missing_ok=True)
            if paths[1].exists():paths[1].replace(paths[2])
            paths[0].replace(paths[1])
        with self.path.open('a',encoding='utf8') as f:f.write(raw)


def markers(text):
    """Only fixed categories leave memory, never raw console lines or error descriptions.

    Console observations are explicitly clues, not authenticated server verdicts.
    """
    result=[]
    for line in text.splitlines():
        if re.search(r'接收\s*<-|发送\s*->|\breceive\b|\bsend\b',line,re.I):continue
        if not re.search(r'\[(?:error|warn|info|NapCat|Core)\]',line,re.I):continue
        kind=None
        if '[KickedOffLine]' in line:kind='kicked_offline_marker'
        elif '快速登录错误' in line or '自动快速登录失败' in line:kind='quick_login_failed_marker'
        elif '[Login] Login Error' in line:kind='login_failed_marker'
        elif '二维码已过期' in line:kind='qr_expired_marker'
        elif '需要新设备验证' in line:kind='device_verification_marker'
        elif '请扫描下面的二维码' in line or '正在获取二维码' in line:kind='qr_requested_marker'
        elif '自动快速登录成功' in line:kind='quick_login_success_marker'
        if kind:
            reason='unspecified'
            if '身份已失效' in line:reason='identity_expired_text'
            elif '其他设备' in line or '其它设备' in line:reason='other_device_text'
            elif '安全' in line or '风险' in line:reason='security_text'
            elif '网络' in line:reason='network_text'
            result.append((hashlib.sha256(line.encode()).hexdigest(),kind,reason))
    return result


def relay_process(process, journal, output=None):
    """Future normal launches: keep console output visible, persist only fixed markers."""
    def emit(event, **fields):
        try:journal.emit(event,**fields)
        except OSError:pass
    target=getattr(process,'pid',None)
    emit('launcher_child_started',target_pid=target)
    stream=getattr(process,'stdout',None)
    output=output if output is not None else getattr(sys.stdout,'buffer',None)
    if stream is not None:
        while True:
            raw=stream.readline(65536)
            if not raw:break
            if output is not None:
                try:output.write(raw);output.flush()
                except OSError:pass
            for _,kind,reason in markers(raw.decode('utf8',errors='replace')):
                emit('launcher_console_marker',target_pid=target,kind=kind,reason=reason,source='console_unverified')
    code=process.wait()
    emit('launcher_child_exit',target_pid=target,exit_code=code,reason='observed_wait_returncode')
    return code


class ProcessHandle:
    def __init__(self,pid):
        self.api=ctypes.WinDLL('kernel32',use_last_error=True)
        self.api.OpenProcess.restype=ctypes.c_void_p
        self.api.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
        self.api.CloseHandle.argtypes=[ctypes.c_void_p]
        self.handle=self.api.OpenProcess(0x1000,False,pid)
    def exit_code(self):
        code=ctypes.c_ulong()
        if self.handle and self.api.GetExitCodeProcess(self.handle,ctypes.byref(code)):
            return None if code.value==259 else code.value
        return None
    def close(self):
        if self.handle:self.api.CloseHandle(self.handle);self.handle=None


def console_text(pid):
    """Read existing console output only. Never writes input, closes windows or signals QQ."""
    from ctypes import wintypes as W
    class COORD(ctypes.Structure):_fields_=[('X',W.SHORT),('Y',W.SHORT)]
    class RECT(ctypes.Structure):_fields_=[('Left',W.SHORT),('Top',W.SHORT),('Right',W.SHORT),('Bottom',W.SHORT)]
    class INFO(ctypes.Structure):_fields_=[('size',COORD),('cursor',COORD),('attributes',W.WORD),('window',RECT),('maximum',COORD)]
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateFileW.restype=W.HANDLE
    api.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,ctypes.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
    api.GetConsoleScreenBufferInfo.argtypes=[W.HANDLE,ctypes.POINTER(INFO)]
    api.ReadConsoleOutputCharacterW.argtypes=[W.HANDLE,W.LPWSTR,W.DWORD,COORD,ctypes.POINTER(W.DWORD)]
    api.CloseHandle.argtypes=[W.HANDLE]
    # This observer is launched without a console, so no user's console is detached.
    if not api.AttachConsole(pid):return None
    h=None
    try:
        h=api.CreateFileW('CONOUT$',0x80000000,3,None,3,0,None)
        if h in (None,ctypes.c_void_p(-1).value):return None
        info=INFO()
        if not api.GetConsoleScreenBufferInfo(h,ctypes.byref(info)):return None
        width=info.size.X
        if width<=0:return None
        start=max(0,info.cursor.Y-min(200,60000//width)+1)
        length=min(60000,width*(info.cursor.Y-start+1))
        buf=ctypes.create_unicode_buffer(length+1);read=W.DWORD()
        if not api.ReadConsoleOutputCharacterW(h,buf,length,COORD(0,start),ctypes.byref(read)):return None
        # Reconstruct physical lines; terminal wrapping can still make a marker unavailable.
        return '\n'.join(buf.value[i:i+width] for i in range(0,read.value,width))
    finally:
        if h not in (None,ctypes.c_void_p(-1).value):api.CloseHandle(h)
        api.FreeConsole()


def snapshot(root):
    script=r'''$ErrorActionPreference='Stop';$r=[IO.Path]::GetFullPath($env:QQ_OBSERVER_ROOT).TrimEnd('\')+'\';
$p=@(Get-CimInstance Win32_Process -Filter "Name='QQ.exe' OR Name='NapCatWinBootMain.exe'" | Where-Object {$_.ExecutablePath -and $_.ExecutablePath.StartsWith($r,[StringComparison]::OrdinalIgnoreCase)} | Select-Object ProcessId,ParentProcessId,Name);
$open=[bool](Get-NetTCPConnection -LocalPort 3001 -State Listen -ErrorAction SilentlyContinue | Where-Object {$_.OwningProcess -in $p.ProcessId});
@{processes=$p;port_open=$open}|ConvertTo-Json -Depth 3 -Compress'''
    env=dict(os.environ,QQ_OBSERVER_ROOT=str(root))
    proc=subprocess.run(['powershell.exe','-NoProfile','-Command',script],env=env,
                        capture_output=True,text=True,timeout=12,creationflags=0x08000000)
    if proc.returncode:raise OSError('snapshot_unavailable')
    return json.loads(proc.stdout)


class Observer:
    def __init__(self,journal):
        self.log=journal;self.handles={};self.previous={};self.console_available={}
        self.seen=set();self.port=None;self.qq=None;self.incident=uuid.uuid4().hex[:12]
    def tick(self, state, qq):
        if self.port is True and not state['port_open']:self.incident=uuid.uuid4().hex[:12]
        current={int(p['ProcessId']):p for p in state['processes']}
        for pid in self.previous.keys()-current.keys():
            handle=self.handles.pop(pid,None);code=handle.exit_code() if handle else None
            self.log.emit('process_exit_observed',target_pid=pid,exit_code=code,
                          reason='exit_code_observed' if code is not None else 'exit_reason_unknown',incident=self.incident)
            if handle:handle.close()
        for pid,p in current.items():
            if pid not in self.previous:
                self.handles[pid]=ProcessHandle(pid)
                self.log.emit('process_present',target_pid=pid,parent_pid=int(p['ParentProcessId']),
                              kind='launcher' if p['Name']=='NapCatWinBootMain.exe' else 'qq',incident=self.incident)
            text=console_text(pid)
            available=text is not None
            if self.console_available.get(pid)!=available:
                self.log.emit('console_coverage',target_pid=pid,available=available,incident=self.incident)
                self.console_available[pid]=available
            if text:
                for digest,kind,reason in markers(text):
                    if digest not in self.seen:
                        self.seen.add(digest)
                        self.log.emit('console_marker',target_pid=pid,kind=kind,reason=reason,
                                      source='console_unverified',incident=self.incident)
                if len(self.seen)>4096:self.seen=set(list(self.seen)[-2048:])
        if self.port!=state['port_open']:
            self.port=state['port_open'];self.log.emit('interface_listener',port_open=self.port,incident=self.incident)
        value=qq.get('state') if isinstance(qq.get('state'),str) else 'unknown'
        if value!=self.qq:
            self.qq=value
            updated=qq.get('updated_at')
            age=max(0,int(time.time()-updated)) if type(updated) in (int,float) else None
            self.log.emit('bot_state_observed',qq_state=value,state_age_seconds=age,incident=self.incident)
        self.previous=current


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--tail',action='store_true')
    args=parser.parse_args();root=args.root.resolve()
    # Read only this setting, never save or expose configuration or credentials.
    config=json.loads((root/'config.json').read_text(encoding='utf-8-sig'))
    data=Path(config.get('data_dir') or root/'data')
    if not data.is_absolute():data=root/data
    if args.tail:
        for name in ('napcat-observer.jsonl','napcat-launcher.jsonl'):
            path=data/'logs'/name
            print(name)
            if not path.exists():print('尚无此层日志。');continue
            print('\n'.join(path.read_text(encoding='utf8').splitlines()[-25:]))
        return
    import msvcrt
    folder=data/'logs';folder.mkdir(exist_ok=True)
    lock=(folder/'napcat-observer.lock').open('a+b');lock.seek(0)
    if not lock.read(1):lock.write(b'0');lock.flush()
    lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:return
    journal=Journal(folder);observer=Observer(journal);last_beat=0
    journal.emit('observer_start',reason='read_only_no_login_no_restart')
    while True:
        try:
            state=snapshot(root)
            try:qq=json.loads((data/'qq-status.json').read_text(encoding='utf-8-sig'))
            except (OSError,ValueError):qq={}
            observer.tick(state,qq)
            if time.time()-last_beat>=60:
                journal.emit('observer_heartbeat',processes=len(state['processes']),port_open=state['port_open'])
                last_beat=time.time()
        except Exception as exc:
            # Never store exception text, command output, console text or configuration.
            try:journal.emit('observation_error',error_type=type(exc).__name__)
            except OSError:pass
        time.sleep(5)


if __name__=='__main__':main()
