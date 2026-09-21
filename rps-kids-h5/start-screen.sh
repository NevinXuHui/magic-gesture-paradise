#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$APP_DIR"
export PATH="$APP_DIR/node_modules/.bin:$PATH"

: "${PORT:=5174}"
: "${SCREEN_FPS:=30}"
: "${SCREEN_DISPLAY:=:99}"
: "${MPV_SOCKET:=/tmp/mpv-socket}"
: "${SCREEN_RUNTIME_DIR:=/run/rps-kids-h5}"
: "${AUTO_INSTALL_DEPS:=1}"

if ! mkdir -p "$SCREEN_RUNTIME_DIR" 2>/dev/null; then
  SCREEN_RUNTIME_DIR="/tmp/rps-kids-h5"
  mkdir -p "$SCREEN_RUNTIME_DIR"
fi

FIFO_PATH="$SCREEN_RUNTIME_DIR/video.nut"
ELECTRON_LOG="${SCREEN_ELECTRON_LOG:-/tmp/rps-kids-h5-electron.log}"
XVFB_LOG="${SCREEN_XVFB_LOG:-/tmp/rps-kids-h5-xvfb.log}"
MPV_LOAD_TIMEOUT="${MPV_LOAD_TIMEOUT:-8}"
X_DISPLAY_NUMBER="${SCREEN_DISPLAY#:}"
ELECTRON_PID=''
XVFB_PID=''
SCREEN_OWNED=0
SERVER_PID=''

find_bin() {
  local requested="$1"
  local fallback="$2"
  if [[ -n "$requested" ]]; then
    command -v "$requested" 2>/dev/null || [[ -x "$requested" ]] && { printf '%s\n' "$requested"; return; }
  fi
  command -v "$fallback" 2>/dev/null || true
}

ELECTRON_BIN=$(find_bin "${ELECTRON_BIN:-}" electron)
FFMPEG_BIN=$(find_bin "${FFMPEG_BIN:-}" ffmpeg)
XVFB_BIN=$(find_bin "${XVFB_BIN:-}" Xvfb)
SOCAT_BIN=$(find_bin "${SOCAT_BIN:-}" socat)

install_apt_packages() {
  local -a runner=()
  if [[ "$(id -u)" -ne 0 ]]; then
    command -v sudo >/dev/null 2>&1 || return 1
    sudo -n true >/dev/null 2>&1 || return 1
    runner=(sudo -n)
  fi
  command -v apt-get >/dev/null 2>&1 || return 1
  echo "自动安装显示依赖：$*"
  DEBIAN_FRONTEND=noninteractive "${runner[@]}" apt-get update
  DEBIAN_FRONTEND=noninteractive "${runner[@]}" apt-get install -y --no-install-recommends "$@"
}

missing_packages=()
[[ -n "$FFMPEG_BIN" ]] || missing_packages+=(ffmpeg)
[[ -n "$XVFB_BIN" ]] || missing_packages+=(xvfb)
[[ -n "$SOCAT_BIN" ]] || missing_packages+=(socat)
if ((${#missing_packages[@]} > 0)) && [[ "$AUTO_INSTALL_DEPS" == '1' ]]; then
  install_apt_packages "${missing_packages[@]}" || {
    echo '自动安装依赖失败，请确认 apt-get 可用，或设置 AUTO_INSTALL_DEPS=0 手动安装。' >&2
    exit 1
  }
  FFMPEG_BIN=$(find_bin "${FFMPEG_BIN:-}" ffmpeg)
  XVFB_BIN=$(find_bin "${XVFB_BIN:-}" Xvfb)
  SOCAT_BIN=$(find_bin "${SOCAT_BIN:-}" socat)
fi

[[ -n "$ELECTRON_BIN" ]] || { echo '未找到 Electron。请先执行 npm install。' >&2; exit 1; }
[[ -n "$FFMPEG_BIN" ]] || { echo '未找到 FFmpeg。请安装 ffmpeg，或设置 AUTO_INSTALL_DEPS=1。' >&2; exit 1; }
[[ -n "$XVFB_BIN" ]] || { echo '未找到 Xvfb。请安装 xvfb，或设置 AUTO_INSTALL_DEPS=1。' >&2; exit 1; }
[[ -n "$SOCAT_BIN" ]] || { echo '未找到 socat。请安装 socat，或设置 AUTO_INSTALL_DEPS=1。' >&2; exit 1; }
[[ -S "$MPV_SOCKET" ]] || { echo "未找到设备 MPV socket：$MPV_SOCKET" >&2; exit 1; }

if [[ ! -f dist/index.html ]]; then
  echo '缺少 dist/index.html，请先运行 npm run build。' >&2
  exit 1
fi

mpv_cmd() {
  printf '%s\n' "$1" | "$SOCAT_BIN" - "UNIX-CONNECT:${MPV_SOCKET}" >/dev/null
}

wait_for_mpv() {
  for _ in {1..100}; do
    if printf '{"command":["get_property","path"]}\n' |
      timeout 2 "$SOCAT_BIN" - "UNIX-CONNECT:${MPV_SOCKET}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

restore_expression() {
  local restore_script="$APP_DIR/../../electron/restore_expression.sh"
  if [[ -x "$restore_script" ]]; then
    "$restore_script" >/dev/null 2>&1 || true
  fi
}

disable_expression() {
  [[ -f /opt/ros/foxy/setup.bash ]] || return 0
  [[ -f /usr/bin/cmcc_robot/install/setup.bash ]] || return 0
  set +u  # 临时关闭未定义变量检查
  source /opt/ros/foxy/setup.bash
  source /usr/bin/cmcc_robot/install/setup.bash
  set -u  # 重新启用未定义变量检查
  export ROS_DOMAIN_ID=2
  timeout 8 ros2 service call \
    /expression/config \
    homi_speech_interface/srv/ExpressionConfig \
    "{action: set, default_video: '', default_image: '', expression_enabled: 'false', status_publish_enabled: 'true'}" \
    >/dev/null 2>&1 || true
}

cleanup() {
  trap - EXIT INT TERM
  echo "正在清理并恢复表情..."

  # 先恢复表情
  if [[ "$SCREEN_OWNED" -eq 1 ]]; then
    restore_expression
  fi

  # 停止 Electron（使用 SIGTERM，然后如果需要用 SIGKILL）
  if [[ -n "$ELECTRON_PID" ]] && kill -0 "$ELECTRON_PID" 2>/dev/null; then
    echo "停止 Electron (PID $ELECTRON_PID)..."
    kill "$ELECTRON_PID" 2>/dev/null || true
    sleep 1
    # 如果还在运行，强制终止
    if kill -0 "$ELECTRON_PID" 2>/dev/null; then
      kill -9 "$ELECTRON_PID" 2>/dev/null || true
    fi
  fi

  # 停止 Xvfb
  if [[ -n "$XVFB_PID" ]] && kill -0 "$XVFB_PID" 2>/dev/null; then
    kill "$XVFB_PID" 2>/dev/null || true
  fi

  # 停止服务端（如果是我们启动的）
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
  fi

  # 清理临时文件
  rm -f "$FIFO_PATH" "/tmp/.X${X_DISPLAY_NUMBER}-lock"

  echo "清理完成"
}
trap cleanup EXIT INT TERM

if ! curl -fsS --max-time 1 "http://127.0.0.1:${PORT}/api/status" >/dev/null 2>&1; then
  echo "启动服务端摄像头：127.0.0.1:${PORT}"
  PORT="$PORT" python3 "$APP_DIR/server.py" &
  SERVER_PID=$!
  for _ in {1..50}; do
    curl -fsS --max-time 1 "http://127.0.0.1:${PORT}/api/status" >/dev/null 2>&1 && break
    sleep 0.1
  done
  curl -fsS --max-time 1 "http://127.0.0.1:${PORT}/api/status" >/dev/null 2>&1 || {
    echo '服务端摄像头未能就绪。' >&2
    exit 1
  }
else
  echo "复用已运行的服务端摄像头：127.0.0.1:${PORT}"
fi

rm -f "$FIFO_PATH" "/tmp/.X${X_DISPLAY_NUMBER}-lock"
mkfifo "$FIFO_PATH"
chmod 0600 "$FIFO_PATH"

echo "启动 Xvfb ${SCREEN_DISPLAY}：802x482x24"
"$XVFB_BIN" "$SCREEN_DISPLAY" -screen 0 802x482x24 -nolisten tcp -ac >"$XVFB_LOG" 2>&1 &
XVFB_PID=$!
for _ in {1..100}; do
  [[ -e "/tmp/.X11-unix/X${X_DISPLAY_NUMBER}" ]] && break
  sleep 0.05
done
[[ -e "/tmp/.X11-unix/X${X_DISPLAY_NUMBER}" ]] || { echo 'Xvfb 启动失败。' >&2; exit 1; }

export DISPLAY="$SCREEN_DISPLAY"
export ELECTRON_BIN FFMPEG_BIN SCREEN_FPS SCREEN_FIFO="$FIFO_PATH"
export SCREEN_URL="${SCREEN_URL:-http://127.0.0.1:${PORT}/?camera=server&debug=0&screen=1}"
# Mesa 软件渲染（不使用 LIBGL_ALWAYS_SOFTWARE，避免 Electron 崩溃）
export MESA_GL_VERSION_OVERRIDE=3.3
export MESA_GLSL_VERSION_OVERRIDE=330

echo "启动 H5 Electron：${SCREEN_URL}"
"$ELECTRON_BIN" --no-sandbox --user-data-dir="$SCREEN_RUNTIME_DIR/user-data" "$APP_DIR/screen-renderer.js" >"$ELECTRON_LOG" 2>&1 &
ELECTRON_PID=$!

for _ in {1..300}; do
  grep -q 'First paint' "$ELECTRON_LOG" 2>/dev/null && break
  kill -0 "$ELECTRON_PID" 2>/dev/null || break
  sleep 0.05
done
if ! kill -0 "$ELECTRON_PID" 2>/dev/null; then
  echo 'Electron 启动失败，日志如下：' >&2
  sed -n '1,160p' "$ELECTRON_LOG" >&2 || true
  exit 1
fi

wait_for_mpv || { echo "设备 MPV socket 不可用：$MPV_SOCKET" >&2; exit 1; }
disable_expression
mpv_cmd '{"command":["set_property","hwdec","no"]}'
mpv_cmd '{"command":["set_property","hwdec-codecs","no"]}'
mpv_cmd '{"command":["set_property","loop-file",false]}'
mpv_cmd '{"command":["set_property","keepaspect",true]}'
mpv_cmd '{"command":["set_property","cache",false]}'
mpv_cmd '{"command":["set_property","demuxer-readahead-secs",0]}'
mpv_cmd '{"command":["set_property","video-sync","desync"]}'
mpv_cmd "{\"command\":[\"vf\",\"set\",\"fps=${SCREEN_FPS}\"]}"

load_response="$(timeout "$MPV_LOAD_TIMEOUT" bash -c \
  'printf '\''{"command":["loadfile","%s","replace"]}\n'\'' "$1" |
    socat - "UNIX-CONNECT:$2"' _ "$FIFO_PATH" "$MPV_SOCKET" 2>/dev/null || true)"
printf 'mpv loadfile: %s\n' "${load_response:-<empty>}"
mpv_cmd '{"command":["set_property","hwdec","no"]}' || true
mpv_cmd '{"command":["set_property","pause",false]}'

sleep 0.3
current_path="$(printf '{"command":["get_property","path"]}\n' |
  timeout 2 "$SOCAT_BIN" - "UNIX-CONNECT:${MPV_SOCKET}" 2>/dev/null || true)"
if [[ "$current_path" != *"video.nut"* ]]; then
  echo "MPV 未切换到 H5 FIFO：${current_path:-<empty>}" >&2
  exit 1
fi

SCREEN_OWNED=1
echo "H5 已交给设备液晶：480x800，${SCREEN_FPS} FPS"
echo "Electron 日志：${ELECTRON_LOG}"
echo '按 Ctrl-C 退出并尝试恢复默认表情。'

# 使用循环检查而不是 wait，这样 Ctrl-C 能够及时响应
while kill -0 "$ELECTRON_PID" 2>/dev/null; do
  sleep 1
done

