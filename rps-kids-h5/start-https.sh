#!/usr/bin/env bash
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$APP_DIR"

if [ ! -f dist/index.html ]; then
  echo '缺少 dist/index.html，请先运行 npm run build。' >&2
  exit 1
fi

CERT_DIR="$APP_DIR/.ssl"
PORT="${PORT:-5174}"
LOCAL_IP=${LOCAL_IP:-127.0.0.1}

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

# Reuse the inference service; a static-only server cannot handle /api/infer.
exec bash start-python.sh --host 0.0.0.0 --cert "$CERT_DIR/server.crt" --key "$CERT_DIR/server.key"
