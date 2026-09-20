#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")"

if [ ! -f dist/index.html ]; then
  echo '缺少 dist/index.html，请先运行 npm run build。' >&2
  exit 1
fi

PORT="${PORT:-5174}"

echo "启动服务端摄像头模式..."
echo "访问地址："
echo "  本机：http://localhost:${PORT}/?camera=server"
echo "  局域网：http://192.168.123.99:${PORT}/?camera=server"
echo ""
echo "摄像头优先级："
echo "  1. 机器狗额头相机（如果 /tmp/foo_jpeg 存在）"
echo "  2. USB 摄像头 /dev/video0"
echo ""
echo "按 Ctrl+C 停止"

exec python3 server.py
