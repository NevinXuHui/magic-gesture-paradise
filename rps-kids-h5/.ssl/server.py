#!/usr/bin/env python3
import http.server
import ssl
import sys
from pathlib import Path

port = int(sys.argv[1])
cert_dir = Path(sys.argv[2])
dist_dir = Path(sys.argv[3])
local_ip = sys.argv[4]

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(dist_dir), **kwargs)

server = http.server.HTTPServer(('0.0.0.0', port), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(cert_dir / 'server.crt', cert_dir / 'server.key')
server.socket = context.wrap_socket(server.socket, server_side=True)

print(f"HTTPS 服务运行在：")
print(f"  本机：https://localhost:{port}")
print(f"  局域网：https://{local_ip}:{port}")
print(f"⚠️  首次访问需要在浏览器接受自签名证书警告")
print(f"按 Ctrl+C 停止")
server.serve_forever()
