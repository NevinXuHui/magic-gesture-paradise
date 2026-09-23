#!/bin/bash
# 模拟 SpeechCore 下发 DEVICE_ABILITY/dog_auto_delivery_demo 配送演示指令
#
# 向 /homi_speech/sigc_event_topic 发布 SIGCEvent，由 hermes_bridge 接收后：
#   透传到 /dog_mission/platform_event，由导航模块订阅处理
#
# 用法:
#   ./sim_dog_delivery.sh
#   ./sim_dog_delivery.sh --goods mimi --source 起点 --target 目标点
#   ./sim_dog_delivery.sh --goods floss --sx 1.0 --sy 2.0 --tx 5.0 --ty 3.0
#   ./sim_dog_delivery.sh --device-id dog_001 --goods mimi
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
OUTPUT_TOPIC="/dog_mission/platform_event"

DEVICE_ID=""
TARGET_TO="张三"
TARGET_GOODS="floss"
SOURCE_NAME="起点"
SOURCE_X="0.0"
SOURCE_Y="0.0"
SOURCE_ANGLE="0.0"
TARGET_NAME="目标点"
TARGET_X="5.0"
TARGET_Y="3.0"
TARGET_ANGLE="90.0"
TARGET_PLACE_EN="shafa"
EVENT_ID=""

usage() {
  cat <<EOF
用法: $0 [选项]

模拟 SpeechCore 下发配送演示任务。

基础选项:
  --device-id <id>     设备 ID（默认读 /etc/cmcc_robot/cmcc_dev.ini 或 dog_001）
  --goods <name>       货物名称（mimi/floss，默认 mimi）
  --target-to <name>   收件人（可选）
  --event-id <id>      自定义 eventId（默认自动生成）

起点坐标:
  --source <name>      起点名称（默认"起点"）
  --sx <x>             起点 X 坐标（默认 0.0）
  --sy <y>             起点 Y 坐标（默认 0.0）
  --sa <angle>         起点朝向角度（默认 0.0）

目标点坐标:
  --target <name>      目标点名称（默认"目标点"）
  --tx <x>             目标点 X 坐标（默认 5.0）
  --ty <y>             目标点 Y 坐标（默认 3.0）
  --ta <angle>         目标点朝向角度（默认 90.0）
  --target-en <name>   目标点英文名（可选）

环境:
  --no=2.0             产品号（决定 install 路径，默认 aarch64→2.0）
  --build=debug        debug|release
  -h, --help           帮助

环境变量:
  ROS_DOMAIN_ID        默认 2（与实机一致）
  SIM_SKIP_ENV=1       跳过自动 source，沿用当前 shell

示例:
  # 终端 A: 启动 hermes_bridge（DOMAIN_ID=2）
  source /opt/ros/foxy/setup.bash
  source /opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash
  export ROS_DOMAIN_ID=2
  ./start.sh --target hermes

  # 终端 B: 模拟平台下发配送任务
  $0 --goods mimi --source 起点 --target 快递站

  # 终端 C: 监听透传 topic
  source /opt/ros/foxy/setup.bash
  source /opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash
  export ROS_DOMAIN_ID=2
  ros2 topic echo /dog_mission/platform_event

  # 自定义坐标
  $0 --goods floss --sx 1.0 --sy 2.0 --tx 5.0 --ty 3.0 --ta 180.0
EOF
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage ;;
    --device-id) DEVICE_ID="$2"; shift ;;
    --goods) TARGET_GOODS="$2"; shift ;;
    --target-to) TARGET_TO="$2"; shift ;;
    --event-id) EVENT_ID="$2"; shift ;;
    --source) SOURCE_NAME="$2"; shift ;;
    --sx) SOURCE_X="$2"; shift ;;
    --sy) SOURCE_Y="$2"; shift ;;
    --sa) SOURCE_ANGLE="$2"; shift ;;
    --target) TARGET_NAME="$2"; shift ;;
    --tx) TARGET_X="$2"; shift ;;
    --ty) TARGET_Y="$2"; shift ;;
    --ta) TARGET_ANGLE="$2"; shift ;;
    --target-en) TARGET_PLACE_EN="$2"; shift ;;
    --no=*) PRODUCT_NO="${1#*=}"
            INSTALL_BASE="${WS_ROOT}/build_${PRODUCT_NO}/${BUILD_TYPE}/install"
            ;;
    --build=*) BUILD_TYPE="${1#*=}"
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
    echo "[NOTE] unitree cyclonedds: /opt/ros/unitree_ros2/cyclonedds_ws/install"
  else
    echo "[WARN] 未找到 unitree cyclonedds_ws setup，DDS 可能与实机不一致"
  fi

  export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-2}"
  echo "[NOTE] ROS_DOMAIN_ID=${ROS_DOMAIN_ID}"
else
  echo "[NOTE] SIM_SKIP_ENV=1，跳过自动 source（沿用当前 shell 环境）"
  echo "[NOTE] ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-<unset>}"
fi

if [ -f "${INSTALL_BASE}/setup.bash" ]; then
  # shellcheck disable=SC1090
  source "${INSTALL_BASE}/setup.bash"
  echo "[NOTE] workspace: ${INSTALL_BASE}"
else
  echo "[WARN] 未找到 ${INSTALL_BASE}/setup.bash，尝试仅用系统 ROS"
  echo "       若缺 homi_speech_interface，请先: ${SCRIPT_DIR}/build.sh"
fi

if ! command -v ros2 >/dev/null 2>&1; then
  echo "[ERROR] 未找到 ros2 命令"
  exit 1
fi

# ---------- 默认 device id ----------
if [ -z "${DEVICE_ID}" ]; then
  if [ -f /etc/cmcc_robot/cmcc_dev.ini ]; then
    DEVICE_ID="$(python3 - <<'PY'
import configparser
c=configparser.ConfigParser()
c.read('/etc/cmcc_robot/cmcc_dev.ini')
print(c.get('factory','devSn', fallback='dog_001'))
PY
)"
  else
    DEVICE_ID="dog_001"
  fi
fi

TS_MS="$(date +%s%3N 2>/dev/null || python3 -c 'import time; print(int(time.time()*1000))')"
[ -z "${EVENT_ID}" ] && EVENT_ID="mission-${TS_MS}"

pub_dog_delivery() {
  local payload
  payload="$(python3 - <<PY
import json
print(json.dumps({
  "deviceId": """${DEVICE_ID}""",
  "domain": "DEVICE_ABILITY",
  "event": "dog_auto_delivery_demo",
  "eventId": """${EVENT_ID}""",
  "seq": str(${TS_MS}),
  "body": {
    "targetTo": """${TARGET_TO}""",
    "targetGoods": """${TARGET_GOODS}""",
    "sourcePlace": {
      "name": """${SOURCE_NAME}""",
      "x": ${SOURCE_X},
      "y": ${SOURCE_Y},
      "angle": ${SOURCE_ANGLE}
    },
    "targetPlace": {
      "name": """${TARGET_NAME}""",
      "x": ${TARGET_X},
      "y": ${TARGET_Y},
      "angle": ${TARGET_ANGLE}
    },
    "targetPlace_en": """${TARGET_PLACE_EN}"""
  }
}, ensure_ascii=False))
PY
)"

  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo " 模拟 SpeechCore → dog_auto_delivery_demo"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  topic       : ${TOPIC}"
  echo "  deviceId    : ${DEVICE_ID}"
  echo "  eventId     : ${EVENT_ID}"
  echo "  targetGoods : ${TARGET_GOODS}"
  echo "  targetTo    : ${TARGET_TO:-<空>}"
  echo "  起点        : ${SOURCE_NAME} (${SOURCE_X}, ${SOURCE_Y}, ${SOURCE_ANGLE}°)"
  echo "  目标点      : ${TARGET_NAME} (${TARGET_X}, ${TARGET_Y}, ${TARGET_ANGLE}°)"
  echo "  target_en   : ${TARGET_PLACE_EN:-<空>}"
  echo ""
  echo "  payload     : ${payload}"
  echo ""

  # SIGCEvent.event 为 JSON 字符串
  local ros_data
  ros_data="$(python3 - <<PY
import json
payload = json.loads('''${payload}''')
inner = json.dumps(payload, ensure_ascii=False)
inner_escaped = inner.replace("'", "''")
print("{event: '" + inner_escaped + "'}")
PY
)"

  echo "[RUN] ros2 topic pub --once ${TOPIC} ${MSG_TYPE} \"...\""
  ros2 topic pub --once "${TOPIC}" "${MSG_TYPE}" "${ros_data}"
  echo "[OK ] dog_auto_delivery_demo 已发布"
  echo ""
  echo "可观察:"
  echo "  ros2 topic echo ${OUTPUT_TOPIC}"
  echo "  # hermes_bridge 日志应出现: 收到 dog_auto_delivery_demo / 已透传到 /dog_mission/platform_event"
}

pub_dog_delivery
