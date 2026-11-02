#!/usr/bin/env python3
"""Standard-library-client test: connect to NapCat OneBot WS and call get_login_info."""
import asyncio
import json
from pathlib import Path

import websockets

CONFIG = Path('/opt/ygobot/config.json')


async def main():
    token = json.loads(CONFIG.read_text(encoding='utf-8')).get('qq', {}).get('token', '')
    uri = 'ws://127.0.0.1:3001'
    headers = [('Authorization', 'Bearer ' + token)]
    ws = await websockets.connect(uri, extra_headers=headers)
    try:
        await ws.send(json.dumps({'action': 'get_login_info', 'params': {}, 'echo': 'probe'}))
        for _ in range(3):
            message = await asyncio.wait_for(ws.recv(), timeout=5)
            print(str(message)[:400])
    finally:
        await ws.close()


if __name__ == '__main__':
    asyncio.run(main())
