#!/bin/bash
# 模拟 SpeechCore 下发 DEVICE_ABILITY/item_search 寻物指令
#
# 向 /homi_speech/sigc_event_topic 发布 SIGCEvent，由 hermes_bridge 接收后：
#   1) 发布 /findobj/search_start  {"SN","prompt","scene"}
#   2) 等待 /findobj/search_status 状态回传（可用本脚本 --status 模拟）
#
# 用法:
#   ./sim_item_search.sh
#   ./sim_item_search.sh --code basketball --name 篮球 --scene 阳台
#   ./sim_item_search.sh --device-id 1222004229866666660002891 --code chair
#   ./sim_item_search.sh --status 09          # 模拟端侧状态回传
#   ./sim_item_search.sh --status 13 --sn ... # 模拟到达目标
#   ./sim_item_search.sh --full              # 下发寻物 + 依次模拟关键状态
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
STATUS_TOPIC="/findobj/search_status"
STATUS_MSG_TYPE="std_msgs/msg/String"

DEVICE_ID=""
ITEM_CODE="basketball"
ITEM_NAME="篮球"
POINT_NAME=""
EVENT_ID=""
STATUS_CODE=""
DO_FULL="n"
ONCE="y"

usage() {
  cat <<EOF
用法: $0 [选项]

模拟 SpeechCore 下发寻物 / 或模拟 findobj 状态回传。

寻物下发:
  --device-id <sn>     设备 SN（默认读 /etc/cmcc_robot/cmcc_dev.ini 或 test-device）
  --code <itemCode>    物品 code → prompt（默认 basketball）
  --name <itemName>    物品中文名（默认 篮球，仅日志/TTS 增强用）
  --scene <pointName>  场景/点位 → scene（默认空）
  --event-id <id>      自定义 eventId（默认自动生成）

状态回传:
  --status <code>      发布 /findobj/search_status，code 如 00/09/10/13/19
  --sn <sn>            状态消息里的 SN（默认与 --device-id 相同）
  --full               下发寻物后，依次模拟 00→09→10→13→19

环境:
  --no=2.0             产品号（决定 install 路径，默认 aarch64→2.0）
  --build=debug        debug|release
  -h, --help           帮助

环境变量:
  ROS_DOMAIN_ID        默认 2（与实机一致）
  SIM_SKIP_ENV=1       跳过自动 source，沿用当前 shell

示例:
  # 终端 A: 启动 hermes_bridge（同样需 DOMAIN_ID=2）
  source /opt/ros/foxy/setup.bash
  source /opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash
  export ROS_DOMAIN_ID=2
  ./start.sh --target hermes

  # 终端 B: 模拟平台下发寻物（脚本内会自动 source 上述环境）
  $0 --code basketball --name 篮球

  # 另开终端听寻物启动话题
  source /opt/ros/foxy/setup.bash
  source /opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash
  export ROS_DOMAIN_ID=2
  ros2 topic echo /findobj/search_start

  # 模拟端侧回报「到达目标旁」
  $0 --status 13
EOF
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage ;;
    --device-id) DEVICE_ID="$2"; shift ;;
    --code) ITEM_CODE="$2"; shift ;;
    --name) ITEM_NAME="$2"; shift ;;
    --scene) POINT_NAME="$2"; shift ;;
    --event-id) EVENT_ID="$2"; shift ;;
    --status) STATUS_CODE="$2"; shift ;;
    --sn) DEVICE_ID="$2"; shift ;;
    --full) DO_FULL="y" ;;
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
# 与实机/联调一致：
#   source /opt/ros/foxy/setup.bash
#   source /opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash
#   export ROS_DOMAIN_ID=2
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
print(c.get('factory','devSn', fallback='test-device'))
PY
)"
  else
    DEVICE_ID="1222004229866666660001455"
  fi
fi

TS_MS="$(date +%s%3N 2>/dev/null || python3 -c 'import time; print(int(time.time()*1000))')"
[ -z "${EVENT_ID}" ] && EVENT_ID="sim-item-search-${TS_MS}"

pub_item_search() {
  local payload
  payload="$(python3 - <<PY
import json
print(json.dumps({
  "deviceId": "${DEVICE_ID}",
  "domain": "DEVICE_ABILITY",
  "event": "item_search",
  "eventId": "${EVENT_ID}",
  "seq": str(${TS_MS}),
  "response": False,
  "body": {
    "itemName": """${ITEM_NAME}""",
    "itemCode": """${ITEM_CODE}""",
    "pointName": """${POINT_NAME}""",
  },
}, ensure_ascii=False))
PY
)"

  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo " 模拟 SpeechCore → item_search"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  topic    : ${TOPIC}"
  echo "  deviceId : ${DEVICE_ID}"
  echo "  eventId  : ${EVENT_ID}"
  echo "  itemCode : ${ITEM_CODE}  (→ prompt)"
  echo "  itemName : ${ITEM_NAME}"
  echo "  pointName: ${POINT_NAME:-<empty>}  (→ scene)"
  echo "  payload  : ${payload}"
  echo ""

  # SIGCEvent.event 为 JSON 字符串；用 python 拼 ros2 参数更稳妥
  local ros_data
  ros_data="$(python3 - <<PY
import json
payload = json.loads('''${payload}''')
# ros2 topic pub 的 YAML：event 字段为转义后的 JSON 字符串
inner = json.dumps(payload, ensure_ascii=False)
# 对 YAML 单引号转义
inner_escaped = inner.replace("'", "''")
print("{event: '" + inner_escaped + "'}")
PY
)"

  echo "[RUN] ros2 topic pub --once ${TOPIC} ${MSG_TYPE} \"...\""
  ros2 topic pub --once "${TOPIC}" "${MSG_TYPE}" "${ros_data}"
  echo "[OK ] item_search 已发布"
  echo ""
  echo "可观察:"
  echo "  ros2 topic echo /findobj/search_start"
  echo "  # hermes_bridge 日志应出现: 收到 item_search / 已下发寻物启动"
}

pub_status() {
  local code="$1"
  local sn="${2:-$DEVICE_ID}"
  local prompt="${3:-$ITEM_CODE}"
  local scene="${4:-$POINT_NAME}"
  local status_json
  status_json="$(python3 - <<PY
import json
print(json.dumps({
  "SN": "${sn}",
  "status": "${code}",
  "prompt": """${prompt}""",
  "scene": """${scene}""",
  "msg": "",
}, ensure_ascii=False))
PY
)"

  local ros_data
  ros_data="$(python3 - <<PY
import json
data = '''${status_json}'''
# std_msgs/String: {data: '...'}
escaped = data.replace("'", "''")
print("{data: '" + escaped + "'}")
PY
)"

  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo " 模拟 findobj → search_status=${code}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  topic : ${STATUS_TOPIC}"
  echo "  data  : ${status_json}"
  echo ""
  ros2 topic pub --once "${STATUS_TOPIC}" "${STATUS_MSG_TYPE}" "${ros_data}"
  echo "[OK ] status=${code} 已发布"
}

if [ -n "${STATUS_CODE}" ] && [ "${DO_FULL}" != "y" ]; then
  pub_status "${STATUS_CODE}"
  exit 0
fi

pub_item_search

if [ "${DO_FULL}" = "y" ]; then
  echo "[NOTE] --full: 依次模拟状态 00 → 09 → 10 → 13 → 19"
  sleep 0.5
  for code in 00 09 10 13 19; do
    sleep 1
    pub_status "${code}"
  done
  echo ""
  echo "[DONE] 全流程模拟完成"
fi
