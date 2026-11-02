#!/usr/bin/env python3
"""Create/enable the NapCat OneBot WebSocket server on port 3001.

Uses the token already stored in config.json qq.token, and mirrors the field
layout of the working Windows NapCat config.
"""
import glob
import json
from pathlib import Path

BOT_CONFIG = Path('/opt/ygobot/config.json')
NAPCAT_CONFIGS = glob.glob('/opt/napcat/config/onebot11_*.json')


def main():
    token = json.loads(BOT_CONFIG.read_text(encoding='utf-8')).get('qq', {}).get('token', '')
    if not token:
        raise SystemExit('config.json qq.token 为空，先运行 sync_napcat_token.py')
    if not NAPCAT_CONFIGS:
        raise SystemExit('未找到 /opt/napcat/config/onebot11_*.json')

    for path in NAPCAT_CONFIGS:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        network = data.setdefault('network', {})
        servers = network.setdefault('websocketServers', [])
        entry = None
        for item in servers:
            if isinstance(item, dict) and str(item.get('port')) == '3001':
                entry = item
                break
        if entry is None:
            entry = {}
            servers.append(entry)
        entry.update({
            'enable': True,
            'name': entry.get('name') or '录像机器人',
            'host': '0.0.0.0',
            'port': 3001,
            'reportSelfMessage': False,
            'enableForcePushEvent': True,
            'messagePostFormat': 'array',
            'token': token,
            'debug': False,
            'heartInterval': 30000,
        })
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('websocket server enabled:', path)


if __name__ == '__main__':
    main()
