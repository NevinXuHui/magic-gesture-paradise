#!/usr/bin/env bash
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$APP_DIR"

if [ ! -f dist/index.html ]; then
  echo '缺少 dist/index.html，请先在开发电脑执行 npm run build。' >&2
  exit 1
fi

CERT_DIR="$APP_DIR/.ssl"
PORT="${PORT:-8090}"
LOCAL_IP=$(hostname -I | awk '{print $1}')

# 生成自签名证书
if [ ! -f "$CERT_DIR/server.crt" ]; then
  echo "生成自签名证书..."
  mkdir -p "$CERT_DIR"
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$CERT_DIR/server.key" \
    -out "$CERT_DIR/server.crt" \
    -days 365 \
    -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:${LOCAL_IP}" \
    2>/dev/null
  echo "✓ 证书已生成：$CERT_DIR/"
fi

# 创建 HTTPS 服务器
cat > "$APP_DIR/.ssl/server.py" << PYEOF
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
PYEOF

exec python3 "$APP_DIR/.ssl/server.py" "$PORT" "$CERT_DIR" "$APP_DIR/dist" "$LOCAL_IP"
