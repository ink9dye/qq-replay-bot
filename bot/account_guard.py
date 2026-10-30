"""本机NapCat账号检测，不向QQ发送消息，不打印管理密码。"""
import hashlib
import json
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler


class AccountMismatch(RuntimeError):
    def __init__(self, expected, actual):
        self.expected, self.actual = str(expected), str(actual)
        super().__init__(f'登录账号错误：当前 {self.actual}，应为工具号 {self.expected}。请在 NapCat 切换账号；机器人已暂停收发。')


def webui_login_id(root):
    """OneBot端口没启动时仍可识别登错号；WebUI不可用时交给原连接检查。"""
    paths = list(Path(root).glob('NapCat*/NapCat*.Shell/versions/*/resources/app/napcat/config/webui.json'))
    if not paths:
        return None
    try:
        path = max(paths, key=lambda p: p.stat().st_mtime)
        cfg = json.loads(path.read_text(encoding='utf-8-sig'))
        port = int(cfg.get('port') or 6099)
        opener = build_opener(ProxyHandler({}))
        def post(route, body, credential=None):
            headers = {'Content-Type': 'application/json'}
            if credential:
                headers['Authorization'] = 'Bearer ' + credential
            request = Request(f'http://127.0.0.1:{port}/api/{route}', data=json.dumps(body).encode(), headers=headers)
            with opener.open(request, timeout=1) as response:
                return json.load(response)
        hashed = hashlib.sha256((str(cfg['token']) + '.napcat').encode()).hexdigest()
        credential = post('auth/login', {'hash': hashed})['data']['Credential']
        data = post('QQLogin/GetQQLoginInfo', {}, credential).get('data') or {}
        return str(data['uin']) if data.get('uin') else None
    except Exception:
        return None


async def verify_transport_account(transport, expected):
    if not expected:
        return
    echo = 'replay-bot-account-check'
    await transport.send_text(json.dumps({'action': 'get_login_info', 'params': {}, 'echo': echo}))
    while True:
        raw = await transport.recv_text()
        if raw is None:
            raise ConnectionError('account check disconnected')
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get('echo') != echo:
            continue
        actual = str((data.get('data') or {}).get('user_id') or '')
        if data.get('retcode') != 0 or not actual.isdigit():
            raise RuntimeError('无法核实工具号，请检查 NapCat 登录状态。')
        if actual != str(expected):
            raise AccountMismatch(expected, actual)
        return
