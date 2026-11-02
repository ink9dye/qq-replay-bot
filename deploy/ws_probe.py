#!/usr/bin/env python3
"""Raw WebSocket handshake probe against the local NapCat OneBot server."""
import base64
import json
import os
import socket
from pathlib import Path

CONFIG = Path('/opt/ygobot/config.json')
HOST = '127.0.0.1'
PORT = 3001


def main():
    token = json.loads(CONFIG.read_text(encoding='utf-8')).get('qq', {}).get('token', '')
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        'GET / HTTP/1.1\r\n'
        'Host: %s:%d\r\n'
        'Upgrade: websocket\r\n'
        'Connection: Upgrade\r\n'
        'Sec-WebSocket-Key: %s\r\n'
        'Sec-WebSocket-Version: 13\r\n'
        'Authorization: Bearer %s\r\n'
        '\r\n'
    ) % (HOST, PORT, key, token)
    sock = socket.create_connection((HOST, PORT), timeout=5)
    sock.sendall(request.encode('latin1'))
    data = sock.recv(4096).decode('latin1', 'replace')
    head = data.split('\r\n\r\n', 1)[0]
    print(head)
    print('token_len =', len(token))
    sock.close()


if __name__ == '__main__':
    main()
