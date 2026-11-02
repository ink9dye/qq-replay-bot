#!/usr/bin/env python3
"""Align the NapCat OneBot WebSocket token with the bot config.

Generates a fresh token, writes it into every NapCat onebot11 config entry
that listens on port 3001, and stores the same token in config.json qq.token.
"""
import glob
import json
import secrets
from pathlib import Path

PORT = '3001'
CONFIG = Path('/opt/ygobot/config.json')


def walk(node, token):
    changed = 0
    if isinstance(node, dict):
        if str(node.get('port')) == PORT and 'token' in node:
            if node.get('token') != token:
                node['token'] = token
                changed += 1
        for value in node.values():
            changed += walk(value, token)
    elif isinstance(node, list):
        for item in node:
            changed += walk(item, token)
    return changed


def main():
    token = secrets.token_hex(16)
    total = 0
    files = glob.glob('/opt/napcat/config/onebot11_*.json')
    if not files:
        files = glob.glob('/opt/napcat/config/**/onebot11_*.json', recursive=True)
    if not files:
        raise SystemExit('未找到 NapCat onebot11 配置，请确认 /opt/napcat/config 已挂载')
    for path in files:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        changed = walk(data, token)
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        total += changed
        print('updated', path, 'entries:', changed)
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    config.setdefault('qq', {})['token'] = token
    CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('config.json qq.token updated')
    print('done, entries changed:', total)


if __name__ == '__main__':
    main()
