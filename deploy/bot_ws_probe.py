#!/usr/bin/env python3
"""Reproduce the bot's own WebSocketClient behaviour against NapCat."""
import asyncio
import json
import sys

sys.path.insert(0, '/opt/ygobot')

import watch  # noqa: E402


async def main():
    token = json.load(open('/opt/ygobot/config.json', encoding='utf-8'))['qq']['token']
    client = watch.WebSocketClient('ws://127.0.0.1:3001')
    await client.connect({'Authorization': 'Bearer ' + token})
    print('connected')
    await client.send_text(json.dumps({'action': 'get_login_info', 'params': {}, 'echo': 'probe'}))
    for index in range(4):
        try:
            message = await asyncio.wait_for(client.recv_text(), timeout=5)
        except Exception as exc:  # noqa: BLE001
            print(index, 'ERROR', repr(exc))
            break
        print(index, repr(message)[:300])
        if message is None:
            break


if __name__ == '__main__':
    asyncio.run(main())
