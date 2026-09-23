#!/bin/bash
# 模拟 SpeechCore 下发游戏相关事件
#
# 向 /homi_speech/sigc_event_topic 发布 SIGCEvent，由 hermes_bridge 接收后：
#   1) robot_game_view (start) → UDP URL:<gameUrl> + {"word","meaning"}
#   2) game_view_data          → UDP {"word","meaning"}
#   3) robot_game_view (stop)  → UDP EXIT
#
# 用法:
#   ./sim_game_events.sh --start                    # 启动游戏
#   ./sim_game_events.sh --push Apple 苹果          # 推送数据
#   ./sim_game_events.sh --stop                     # 停止游戏
#   ./sim_game_events.sh --full                     # 完整流程（启动→推送3个单词→停止）
#
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
WS_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

ARCH="$(uname -m)"
PRODUCT_NO="2.0"
[ "${ARCH}" != "aarch64" ] && PRODUCT_NO="0.0"
BUILD_TYPE="debug"
INSTALL_BASE="${WS_ROOT}/build_${PRODUCT_NO}/${BUILD_TYPE}/install"

TOPIC="/homi_speech/sigc_event_topic"
MSG_TYPE="homi_speech_interface/msg/SIGCEvent"

DEVICE_ID=""
ACTION=""
GAME_URL="http://36.140.17.36:10000/robot/web/game-html/english/english_show_800x480.html"
INIT_WORD="hello"
INIT_MEANING="你好"
PUSH_WORD=""
PUSH_MEANING=""
SESSION_ID=""
DO_FULL="n"

usage() {
  cat <<EOF
用法: $0 [选项]

模拟 SpeechCore 下发游戏相关事件。

游戏操作:
  --start                启动游戏（自动生成 sessionId）
  --push <word> <meaning> 推送游戏数据
  --stop                 停止游戏
  --full                 完整流程测试（启动→推送3个单词→停止）

启动游戏选项:
  --device-id <sn>       设备 SN（默认自动生成）
  --url <gameUrl>        游戏 URL（默认 english 游戏）
  --init-word <word>     初始化单词（默认 hello）
  --init-meaning <text>  初始化释义（默认 你好）

推送数据选项:
  --session-id <id>      会话 ID（默认自动生成或沿用上次启动的）

环境:
  --no=2.0               产品号（决定 install 路径）
  --build=debug          debug|release
  -h, --help             帮助

环境变量:
  ROS_DOMAIN_ID          默认 2（与实机一致）
  SIM_SKIP_ENV=1         跳过自动 source，沿用当前 shell

示例:
  # 终端 A: 启动 hermes_bridge
  source /opt/ros/foxy/setup.bash
  export ROS_DOMAIN_ID=2
  ./start.sh --target hermes

  # 终端 B: 模拟游戏事件
  $0 --start                      # 启动游戏
  $0 --push Apple 苹果            # 推送单词
  $0 --stop                       # 停止游戏

  # 或运行完整流程
  $0 --full
EOF
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage ;;
    --device-id) DEVICE_ID="$2"; shift ;;
    --start) ACTION="start" ;;
    --push)
      ACTION="push"
      PUSH_WORD="$2"
      PUSH_MEANING="$3"
      shift 2
      ;;
    --stop) ACTION="stop" ;;
    --full) DO_FULL="y" ;;
    --url) GAME_URL="$2"; shift ;;
    --init-word) INIT_WORD="$2"; shift ;;
    --init-meaning) INIT_MEANING="$2"; shift ;;
    --session-id) SESSION_ID="$2"; shift ;;
    --no=*)
      PRODUCT_NO="${1#*=}"
      INSTALL_BASE="${WS_ROOT}/build_${PRODUCT_NO}/${BUILD_TYPE}/install"
      ;;
    --build=*)
      BUILD_TYPE="${1#*=}"
      INSTALL_BASE="${WS_ROOT}/build_${PRODUCT_NO}/${BUILD_TYPE}/install"
      ;;
    *)
      echo "[ERROR] 未知参数: $1"
      usage
      ;;
  esac
  shift
done

# ---------- 环境 ----------
if [ -z "${SIM_SKIP_ENV:-}" ]; then
  if [ -f /opt/ros/foxy/setup.bash ]; then
    # shellcheck disable=SC1091
    source /opt/ros/foxy/setup.bash
    echo "[NOTE] ROS2 Foxy: /opt/ros/foxy"
  elif [ -n "${ROS_DISTRO:-}" ] && [ -f "/opt/ros/${ROS_DISTRO}/setup.bash" ]; then
    # shellcheck disable=SC1090
    source "/opt/ros/${ROS_DISTRO}/setup.bash"
    echo "[NOTE] ROS2 ${ROS_DISTRO}"
  else
    echo "[WARN] 未找到 /opt/ros/foxy/setup.bash"
  fi

  if [ -f /opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash ]; then
    # shellcheck disable=SC1091
    source /opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash
    echo "[NOTE] unitree cyclonedds"
  fi

  export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-2}"
  echo "[NOTE] ROS_DOMAIN_ID=${ROS_DOMAIN_ID}"
else
  echo "[NOTE] SIM_SKIP_ENV=1，跳过自动 source"
fi

if [ -f "${INSTALL_BASE}/setup.bash" ]; then
  # shellcheck disable=SC1090
  source "${INSTALL_BASE}/setup.bash"
  echo "[NOTE] workspace: ${INSTALL_BASE}"
else
  echo "[WARN] 未找到 ${INSTALL_BASE}/setup.bash"
fi

if ! command -v ros2 >/dev/null 2>&1; then
  echo "[ERROR] 未找到 ros2 命令"
  exit 1
fi

# ---------- 默认值 ----------
if [ -z "${DEVICE_ID}" ]; then
  DEVICE_ID="1222004229866666660001455"
fi

TS_MS="$(date +%s%3N 2>/dev/null || python3 -c 'import time; print(int(time.time()*1000))')"

# Session ID 文件（用于在多次调用间保持一致）
SESSION_FILE="/tmp/sim_game_session_id"

pub_game_start() {
  # 生成新的 session_id
  if [ -z "${SESSION_ID}" ]; then
    SESSION_ID="$(python3 -c 'import uuid; print(str(uuid.uuid4()))')"
  fi

  # 保存 session_id 供后续使用
  echo "${SESSION_ID}" > "${SESSION_FILE}"

  local event_id
  event_id="$(python3 -c 'import uuid; print(str(uuid.uuid4()))')"

  local request_id
  request_id="$(python3 -c 'import uuid; print(str(uuid.uuid4()))')"

  local payload
  payload="$(python3 - <<PY
import json
print(json.dumps({
  "deviceId": "${DEVICE_ID}",
  "domain": "DEVICE_ABILITY",
  "event": "robot_game_view",
  "eventId": "${event_id}",
  "requestId": "${request_id}",
  "seq": ${TS_MS},
  "response": False,
  "body": {
    "game": "start",
    "gameUrl": """${GAME_URL}""",
    "sessionId": "${SESSION_ID}",
    "initParams": {
      "word": """${INIT_WORD}""",
      "meaning": """${INIT_MEANING}"""
    }
  }
}, ensure_ascii=False))
PY
)"

  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo " 📤 游戏启动 (robot_game_view: start)"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  topic      : ${TOPIC}"
  echo "  deviceId   : ${DEVICE_ID}"
  echo "  sessionId  : ${SESSION_ID}"
  echo "  gameUrl    : ${GAME_URL}"
  echo "  initWord   : ${INIT_WORD}"
  echo "  initMeaning: ${INIT_MEANING}"
  echo ""

  local ros_data
  ros_data="$(python3 - <<PY
import json
payload = json.loads('''${payload}''')
inner = json.dumps(payload, ensure_ascii=False)
inner_escaped = inner.replace("'", "''")
print("{event: '" + inner_escaped + "'}")
PY
)"

  echo "[RUN] ros2 topic pub --once ${TOPIC} ${MSG_TYPE}"
  ros2 topic pub --once "${TOPIC}" "${MSG_TYPE}" "${ros_data}"
  echo "[OK ] 游戏启动事件已发布"
  echo "      session_id 已保存到: ${SESSION_FILE}"
  echo ""
}

pub_game_data() {
  local word="$1"
  local meaning="$2"

  # 读取保存的 session_id
  if [ -z "${SESSION_ID}" ] && [ -f "${SESSION_FILE}" ]; then
    SESSION_ID="$(cat "${SESSION_FILE}")"
  fi

  if [ -z "${SESSION_ID}" ]; then
    echo "[ERROR] 未找到 session_id，请先执行 --start 启动游戏"
    exit 1
  fi

  local event_id
  event_id="$(python3 -c 'import uuid; print(str(uuid.uuid4()))')"

  local payload
  payload="$(python3 - <<PY
import json
print(json.dumps({
  "deviceId": "${DEVICE_ID}",
  "domain": "DEVICE_ABILITY",
  "event": "game_view_data",
  "eventId": "${event_id}",
  "seq": ${TS_MS},
  "body": {
    "sessionId": "${SESSION_ID}",
    "seq": 1,
    "data": {
      "word": """${word}""",
      "meaning": """${meaning}"""
    }
  }
}, ensure_ascii=False))
PY
)"

  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo " 📨 推送游戏数据 (game_view_data)"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  topic     : ${TOPIC}"
  echo "  sessionId : ${SESSION_ID}"
  echo "  word      : ${word}"
  echo "  meaning   : ${meaning}"
  echo ""

  local ros_data
  ros_data="$(python3 - <<PY
import json
payload = json.loads('''${payload}''')
inner = json.dumps(payload, ensure_ascii=False)
inner_escaped = inner.replace("'", "''")
print("{event: '" + inner_escaped + "'}")
PY
)"

  echo "[RUN] ros2 topic pub --once ${TOPIC} ${MSG_TYPE}"
  ros2 topic pub --once "${TOPIC}" "${MSG_TYPE}" "${ros_data}"
  echo "[OK ] 游戏数据已推送"
  echo ""
}

pub_game_stop() {
  # 读取保存的 session_id
  if [ -z "${SESSION_ID}" ] && [ -f "${SESSION_FILE}" ]; then
    SESSION_ID="$(cat "${SESSION_FILE}")"
  fi

  if [ -z "${SESSION_ID}" ]; then
    echo "[ERROR] 未找到 session_id，请先执行 --start 启动游戏"
    exit 1
  fi

  local event_id
  event_id="$(python3 -c 'import uuid; print(str(uuid.uuid4()))')"

  local request_id
  request_id="$(python3 -c 'import uuid; print(str(uuid.uuid4()))')"

  local payload
  payload="$(python3 - <<PY
import json
print(json.dumps({
  "deviceId": "${DEVICE_ID}",
  "domain": "DEVICE_ABILITY",
  "event": "robot_game_view",
  "eventId": "${event_id}",
  "requestId": "${request_id}",
  "seq": ${TS_MS},
  "response": False,
  "body": {
    "game": "stop",
    "sessionId": "${SESSION_ID}"
  }
}, ensure_ascii=False))
PY
)"

  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo " 🛑 游戏停止 (robot_game_view: stop)"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  topic     : ${TOPIC}"
  echo "  sessionId : ${SESSION_ID}"
  echo ""

  local ros_data
  ros_data="$(python3 - <<PY
import json
payload = json.loads('''${payload}''')
inner = json.dumps(payload, ensure_ascii=False)
inner_escaped = inner.replace("'", "''")
print("{event: '" + inner_escaped + "'}")
PY
)"

  echo "[RUN] ros2 topic pub --once ${TOPIC} ${MSG_TYPE}"
  ros2 topic pub --once "${TOPIC}" "${MSG_TYPE}" "${ros_data}"
  echo "[OK ] 游戏停止事件已发布"

  # 清理 session 文件
  rm -f "${SESSION_FILE}"
  echo "      session_id 已清理"
  echo ""
}

# ---------- 主逻辑 ----------
# 如果设置了启动相关参数但未指定操作，默认为启动
if [ -z "${ACTION}" ] && [ "${DO_FULL}" != "y" ]; then
  # 检查是否设置了启动相关的自定义参数
  if [ "${GAME_URL}" != "http://36.140.17.36:10000/robot/web/game-html/english/english_show_800x480.html" ] || \
     [ "${INIT_WORD}" != "hello" ] || \
     [ "${INIT_MEANING}" != "你好" ]; then
    ACTION="start"
  fi
fi

if [ "${DO_FULL}" = "y" ]; then
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo " 🎮 完整流程测试"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""

  echo "[步骤 1/5] 启动游戏"
  pub_game_start
  sleep 2

  echo "[步骤 2/5] 推送单词 1: Apple 苹果"
  pub_game_data "Apple" "苹果"
  sleep 2

  echo "[步骤 3/5] 推送单词 2: Banana 香蕉"
  pub_game_data "Banana" "香蕉"
  sleep 2

  echo "[步骤 4/5] 推送单词 3: Orange 橙子"
  pub_game_data "Orange" "橙子"
  sleep 2

  echo "[步骤 5/5] 停止游戏"
  pub_game_stop

  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo " ✅ 完整流程测试完成"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  exit 0
fi

case "${ACTION}" in
  start)
    pub_game_start
    ;;
  push)
    if [ -z "${PUSH_WORD}" ] || [ -z "${PUSH_MEANING}" ]; then
      echo "[ERROR] --push 需要 word 和 meaning 参数"
      echo "用法: $0 --push <word> <meaning>"
      exit 1
    fi
    pub_game_data "${PUSH_WORD}" "${PUSH_MEANING}"
    ;;
  stop)
    pub_game_stop
    ;;
  *)
    echo "[ERROR] 请指定操作: --start, --push, --stop 或 --full"
    usage
    ;;
esac
