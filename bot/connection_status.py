"""本地连接状态与手动恢复入口，不调用QQ登录或发送接口。"""
import json
import sys
import time
from pathlib import Path
from .reliability import write_state

ROOT = Path(__file__).resolve().parent.parent


def wait_for_resume(data, request_id, timeout=8, pause=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            value = json.loads((data / 'qq-status.json').read_text(encoding='utf-8'))
            if value.get('last_resume_request_id') == request_id:
                if value.get('state') == 'online':
                    return True
                if value.get('state') in ('manual_attention', 'stopped'):
                    return False
        except (OSError, ValueError):
            pass
        pause(.25)
    return False


def main():
    config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8-sig'))
    data = Path(config.get('data_dir') or ROOT / 'data')
    data.mkdir(parents=True, exist_ok=True)
    if '--logs' in sys.argv:
        path = data / 'logs' / 'qq-diagnostics.jsonl'
        print('QQ诊断日志：' + str(path))
        try:
            with path.open('rb') as handle:
                handle.seek(0, 2)
                handle.seek(max(0, handle.tell() - 65536))
                lines = handle.read().decode('utf-8', errors='replace').splitlines()
            for line in lines[-25:]:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict) and value.get('version') == 1:
                        print(json.dumps(value, ensure_ascii=False))
                except ValueError:
                    continue
        except OSError:
            print('尚无详细诊断日志。新代码需要在观战安全空档加载，已有运行进程不会自动升级。')
        print('只记录时间、链路阶段、耗时、错误类别及请求结果，不记录聊天正文或账号凭证。')
        return
    if '--pause' in sys.argv:
        write_state(data / 'qq-paused.json', 'manual_attention', '用户主动暂停QQ收发。')
        print('已请求暂停QQ收发；观战和录像继续。')
        return
    if '--resume' in sys.argv:
        paused = (data / 'qq-paused.json').exists()
        from .launcher import request_qq_resume
        (data / 'qq-paused.json').unlink(missing_ok=True)
        request_id = request_qq_resume(data, str((config.get('qq') or {}).get('expected_self_id') or ''))
        print('正在重新连接机器人与NapCat接口；不会重启观战、网页或重新登录QQ。', flush=True)
        if wait_for_resume(data, request_id):
            print('机器人接口已重新连接。请在QQ发送一条指令确认实际收发。')
        else:
            print('重连请求尚未确认，不能视为恢复成功。请运行 check-status.bat 查看原因。')
            print('若提示运行版本较旧，请在观战安全空档加载更新；不要为此反复登录QQ。')
        if paused:
            print('此次按你的操作解除本机暂停；官方QQ若要求验证，仍需你手动处理。')
        return
    if (data / 'qq-paused.json').exists():
        print('状态：已暂停收发，需要手动处理。')
        print('请在官方QQ处理验证或限制后，双击 resume-qq.bat。')
    else:
        try:
            value = json.loads((data / 'qq-status.json').read_text(encoding='utf8'))
            labels = {'online': 'QQ接口在线', 'reconnecting': '正在重连接口',
                      'cooldown': '重连冷却中', 'manual_attention': '需要手动处理',
                      'stopped': 'QQ连接程序已停止'}
            print('状态：' + labels.get(value['state'], value['state']))
            print(value.get('reason') or '')
            if value.get('connection_control_version', 0) < 2:
                print('当前运行版本尚不支持在线时立即重连接口；源码更新后需在观战安全空档加载。')
            for key, label in (('last_heartbeat_at', '最近接口心跳'), ('last_message_at', '最近收到QQ消息')):
                stamp = value.get(key)
                print(label + ('：' + time.strftime('%m-%d %H:%M:%S', time.localtime(stamp))
                              if isinstance(stamp, (int, float)) else '：尚无记录'))
            print('在线心跳仅表示接口在线，不保证群消息已经同步。')
            next_attempt = value.get('next_attempt_at')
            if value.get('state') in ('cooldown', 'reconnecting') and isinstance(next_attempt, (int, float)):
                remaining = next_attempt - time.time()
                if remaining > 0:
                    print('距下次自动重试约 %d 秒；要立刻重试，请双击 resume-qq.bat。' % int(remaining))
        except (OSError, KeyError, ValueError):
            print('尚无连接状态，请先启动 start-all.bat。')
    print('本工具只显示本机状态，不会输出密码或Token。')


if __name__ == '__main__':
    main()
