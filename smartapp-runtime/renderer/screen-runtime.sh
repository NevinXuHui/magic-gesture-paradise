#!/usr/bin/env bash
set -Eeuo pipefail

RENDERER_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SCREEN_URL=${1:?missing SmartApp URL}
: "${SCREEN_DISPLAY:=:99}"
: "${SCREEN_FPS:=30}"
: "${SCREEN_RUNTIME_DIR:=/run/smartapp-renderer}"
: "${MPV_SOCKET:=/tmp/mpv-socket}"

find_bin() {
  local requested="$1" fallback="$2"
  if [[ -n "$requested" ]]; then
    if [[ -x "$requested" ]]; then
      printf '%s\n' "$requested"
      return
    fi
    command -v "$requested" 2>/dev/null && return
  fi
  command -v "$fallback" 2>/dev/null || true
}

ELECTRON_BIN=''
if [[ -n "${ELECTRON_BIN_OVERRIDE:-}" ]]; then
  ELECTRON_BIN=$(find_bin "$ELECTRON_BIN_OVERRIDE" electron)
elif [[ -x "$RENDERER_DIR/node_modules/.bin/electron" ]]; then
  ELECTRON_BIN="$RENDERER_DIR/node_modules/.bin/electron"
else
  ELECTRON_BIN=$(find_bin '' electron)
fi
FFMPEG_BIN=$(find_bin "${FFMPEG_BIN:-}" ffmpeg)
XVFB_BIN=$(find_bin "${XVFB_BIN:-}" Xvfb)
[[ -n "$ELECTRON_BIN" ]] || { echo '未找到 Electron' >&2; exit 1; }
[[ -n "$FFMPEG_BIN" ]] || { echo '未找到 FFmpeg' >&2; exit 1; }
[[ -n "$XVFB_BIN" ]] || { echo '未找到 Xvfb' >&2; exit 1; }
[[ -S "$MPV_SOCKET" ]] || { echo "未找到 MPV socket: $MPV_SOCKET" >&2; exit 1; }

if ! mkdir -p "$SCREEN_RUNTIME_DIR" 2>/dev/null; then
  SCREEN_RUNTIME_DIR=/tmp/smartapp-renderer
  mkdir -p "$SCREEN_RUNTIME_DIR"
fi
FIFO_PATH="$SCREEN_RUNTIME_DIR/video.nut"
DISPLAY_NUMBER=${SCREEN_DISPLAY#:}
XVFB_PID=''
ELECTRON_PID=''

cleanup() {
  trap - EXIT INT TERM
  if [[ -n "$ELECTRON_PID" ]] && kill -0 "$ELECTRON_PID" 2>/dev/null; then
    kill "$ELECTRON_PID" 2>/dev/null || true
    wait "$ELECTRON_PID" 2>/dev/null || true
  fi
  if [[ -n "$XVFB_PID" ]] && kill -0 "$XVFB_PID" 2>/dev/null; then
    kill "$XVFB_PID" 2>/dev/null || true
    wait "$XVFB_PID" 2>/dev/null || true
  fi
  rm -f "$FIFO_PATH" "/tmp/.X${DISPLAY_NUMBER}-lock"
}
trap cleanup EXIT INT TERM

if [[ -e "/tmp/.X11-unix/X${DISPLAY_NUMBER}" ]]; then
  echo "显示号 ${SCREEN_DISPLAY} 已被占用" >&2
  exit 1
fi
rm -f "$FIFO_PATH" "/tmp/.X${DISPLAY_NUMBER}-lock"
mkfifo "$FIFO_PATH"
chmod 0600 "$FIFO_PATH"
"$XVFB_BIN" "$SCREEN_DISPLAY" -screen 0 802x482x24 -nolisten tcp -ac \
  >"$SCREEN_RUNTIME_DIR/xvfb.log" 2>&1 &
XVFB_PID=$!
for _ in {1..100}; do
  [[ -e "/tmp/.X11-unix/X${DISPLAY_NUMBER}" ]] && break
  sleep 0.05
done
[[ -e "/tmp/.X11-unix/X${DISPLAY_NUMBER}" ]] || { echo 'Xvfb 启动失败' >&2; exit 1; }

"$RENDERER_DIR/disable-expression.sh" >/dev/null 2>&1 || true
export DISPLAY="$SCREEN_DISPLAY" SCREEN_URL SCREEN_FPS SCREEN_FIFO="$FIFO_PATH"
export FFMPEG_BIN MPV_SOCKET
export MESA_GL_VERSION_OVERRIDE=3.3 MESA_GLSL_VERSION_OVERRIDE=330
"$ELECTRON_BIN" --no-sandbox --user-data-dir="$SCREEN_RUNTIME_DIR/user-data" \
  "$RENDERER_DIR/screen-renderer.js" "$SCREEN_URL" <&0 &
ELECTRON_PID=$!
set +e
wait "$ELECTRON_PID"
status=$?
set -e
ELECTRON_PID=''
exit "$status"
