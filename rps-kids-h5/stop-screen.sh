#!/usr/bin/env bash
set -euo pipefail

echo "正在停止手势游戏..."

# 停止 Electron 和 Xvfb
pkill -f "screen-renderer.js" 2>/dev/null || true
pkill -f "Xvfb :99" 2>/dev/null || true

# 等待进程退出
sleep 2

# 恢复默认表情
RESTORE_SCRIPT="$(dirname "$0")/../../electron/restore_expression.sh"
if [[ -x "$RESTORE_SCRIPT" ]]; then
  "$RESTORE_SCRIPT"
else
  # 直接通过 MPV 恢复
  echo '{"command":["loadfile","/usr/bin/cmcc_robot/install/expression/share/expression/resource/video/default/default.mp4","replace"]}' | \
    socat - UNIX-CONNECT:/tmp/mpv-socket 2>/dev/null || true
  echo '{"command":["set_property","loop-file","inf"]}' | \
    socat - UNIX-CONNECT:/tmp/mpv-socket 2>/dev/null || true
  echo '{"command":["set_property","pause",false]}' | \
    socat - UNIX-CONNECT:/tmp/mpv-socket 2>/dev/null || true
fi

# 清理临时文件
rm -f /run/rps-kids-h5/video.nut /tmp/.X99-lock 2>/dev/null || true

echo "✓ 已停止，默认表情已恢复"
