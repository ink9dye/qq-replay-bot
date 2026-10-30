"""本项目NapCat单实例入口；仅在用户主动启动时尝试一次登录。"""
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def existing_napcat(root):
    root = Path(root)
    if not root.exists():
        return []
    script = r'''$taskRoot=[IO.Path]::GetFullPath($env:REPLAY_BOT_ROOT).TrimEnd('\')+'\';
Get-CimInstance Win32_Process | Where-Object {
 ($_.Name -eq 'QQ.exe' -or $_.Name -eq 'NapCatWinBootMain.exe') -and $_.ExecutablePath -and
 $_.ExecutablePath.StartsWith($taskRoot,[StringComparison]::OrdinalIgnoreCase) -and
 ($_.Name -ne 'QQ.exe' -or $_.CommandLine -notlike '*--type=*')
} | Select-Object ProcessId,Name | ConvertTo-Json -Compress'''
    try:
        env = dict(os.environ, REPLAY_BOT_ROOT=str(root.resolve()))
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', script], env=env,
            capture_output=True, timeout=8, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise OSError('无法确认现有NapCat进程，未启动新副本。')
        payload = json.loads(result.stdout.strip() or '[]')
        return payload if isinstance(payload, list) else [payload]
    except (subprocess.SubprocessError, ValueError) as exc:
        raise OSError('无法确认现有NapCat进程，未启动新副本。') from exc


def main():
    from .launcher import startup_lock
    from .setup import read_config
    binaries = list(ROOT.glob('NapCat*/NapCat*.Shell/NapCatWinBootMain.exe'))
    if len(binaries) != 1:
        print('NapCat安装目录不明确，请检查项目目录；未自动启动。')
        return 2
    if '--check-path' in sys.argv:
        print('NapCat launcher and working directory OK.')
        return 0
    with startup_lock(ROOT, 'napcat-runtime.lock') as acquired:
        if not acquired or existing_napcat(ROOT):
            print('本项目已有NapCat实例。请使用现有管理页登录工具号，不会再开一个或重启QQ。')
            return 0
        config = read_config(ROOT / 'config.json')
        data = Path(config.get('data_dir') or ROOT / 'data')
        if (data / 'qq-paused.json').exists() and '--attempt' not in sys.argv:
            print('QQ连接处于手动处理状态。请先在官方QQ处理限制，再运行 resume-qq.bat。')
            return 2
        expected = str((config.get('qq') or {}).get('expected_self_id') or '')
        if not expected.isdigit() or int(expected) <= 0:
            print('请先在config.json填写qq.expected_self_id，确保只登录指定工具号。')
            return 2
        print('仅启动一次指定工具号。需要验证时请手动处理，不会自动反复登录。', flush=True)
        from .napcat_observer import Journal, relay_process
        process = subprocess.Popen([str(binaries[0]), expected], cwd=str(binaries[0].parent),
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return relay_process(process, Journal(data/'logs',filename='napcat-launcher.jsonl'))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError):
        print('启动检查失败，未自动重启QQ。请查看现有窗口或运行 check-status.bat。')
        raise SystemExit(2)
