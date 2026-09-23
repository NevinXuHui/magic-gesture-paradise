#!/bin/bash
# claw_client 启动脚本
#
# 默认启动 hermes_bridge（XiaoliChannel HTTP 服务，默认监听 :8800），
# 供 Hermes Gateway 通过 XIAOLI_BASE_URL=http://localhost:8800 连接。
#
# 用法:
#   ./start.sh
#   ./start.sh --target hermes|openclaw|node|test
#   ./start.sh --launch                 # 用 launch/claw_client.launch.py
#   ./start.sh --params /path/to.yaml
#   ./start.sh --host 0.0.0.0 --port 8800
#   ./start.sh --no=2.0 --build=debug
#   ./start.sh --check                  # 只检查环境，不启动
#   ./start.sh --bg                     # 后台启动，pid 写到 /tmp/claw_client.pid
#
# 不用 nounset：/opt/ros/*/setup.bash 会读未导出变量（如 AMENT_TRACE_SETUP_FILES）
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
WS_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ARCH="$(uname -m)"
PKG_NAME="claw_client"

PRODUCT_NO=""
BUILD_TYPE="debug"
TARGET="hermes"          # hermes | openclaw | node | test
USE_LAUNCH="n"
PARAMS_FILE=""
SERVER_HOST=""
SERVER_PORT=""
DO_CHECK_ONLY="n"
BACKGROUND="n"
PID_FILE_OVERRIDE="${CLAW_CLIENT_PID_FILE:-}"
EXTRA_ROS_ARGS=()

# 可执行名映射
declare -A TARGET_EXE=(
  [hermes]="hermes_bridge"
  [openclaw]="openclaw_bridge"
  [node]="claw_client_node"
  [test]="test_send"
  [chat]="external_chat"
)

# 默认参数文件（相对 share 或源码 config）
declare -A TARGET_DEFAULT_PARAMS=(
  [hermes]="hermes_bridge.yaml"
  [openclaw]="openclaw_bridge.yaml"
  [node]=""
  [test]=""
  [chat]=""
)

usage() {
  cat <<EOF
用法: $0 [选项]

选项:
  --target hermes|openclaw|node|test|chat
                         启动目标（默认 hermes = hermes_bridge / XiaoliChannel）
                         chat = external_chat 同步收发
  --launch               使用 ros2 launch claw_client claw_client.launch.py
                         （当前 launch 固定起 hermes_bridge）
  --params <yaml>        指定 ROS 参数文件
  --host <addr>          覆盖 server_host（仅 hermes）
  --port <n>             覆盖 server_port（仅 hermes，默认配置 8800）
  --no=0.0|1.0|1.1|2.0   产品编号（决定 install 根目录）
  --build=debug|release  构建类型（默认 debug）
  --check                只检查环境 / 产物 / 端口，不真正启动
  --bg                   后台启动（日志: /var/log/cmcc_robot/claw_client_node.log）
  --pid-file <path>      后台 pid 文件（默认 /tmp/claw_client_<target>.pid）
  --                     之后参数原样传给 ros2（--ros-args ...）
  --help / -h            显示帮助

环境变量（可选）:
  ROS_DOMAIN_ID          默认沿用已有值，未设置时不强制
  RMW_IMPLEMENTATION     默认沿用已有值
  CLAW_CLIENT_PID_FILE   覆盖默认 pid 文件

示例:
  $0
  $0 --target hermes --port 8800
  $0 --launch
  $0 --params ${SCRIPT_DIR}/config/hermes_bridge.yaml
  $0 --check
  $0 --bg && tail -f /var/log/cmcc_robot/claw_client_node.log
  $0 --target chat -- --text "你好" --timeout 30
  ros2 run claw_client external_chat --text "你好"
EOF
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage ;;
    --target)
      TARGET="$2"; shift 2; continue ;;
    --target=*)
      TARGET="${1#*=}" ;;
    --launch)
      USE_LAUNCH="y" ;;
    --params)
      PARAMS_FILE="$2"; shift 2; continue ;;
    --params=*)
      PARAMS_FILE="${1#*=}" ;;
    --host)
      SERVER_HOST="$2"; shift 2; continue ;;
    --host=*)
      SERVER_HOST="${1#*=}" ;;
    --port)
      SERVER_PORT="$2"; shift 2; continue ;;
    --port=*)
      SERVER_PORT="${1#*=}" ;;
    --no=*)
      PRODUCT_NO="${1#*=}" ;;
    --build=*)
      BUILD_TYPE="${1#*=}" ;;
    --check)
      DO_CHECK_ONLY="y" ;;
    --bg)
      BACKGROUND="y" ;;
    --pid-file)
      PID_FILE_OVERRIDE="$2"; shift 2; continue ;;
    --pid-file=*)
      PID_FILE_OVERRIDE="${1#*=}" ;;
    --)
      shift
      EXTRA_ROS_ARGS+=("$@")
      break
      ;;
    *)
      echo "[ERROR] 未知参数: $1（若要传给 ros2，请放在 -- 之后）"
      usage
      ;;
  esac
  shift
done

case "${TARGET}" in
  hermes|openclaw|node|test|chat) ;;
  *)
    echo "[ERROR] --target 仅支持 hermes|openclaw|node|test|chat，收到: ${TARGET}"
    exit 1
    ;;
esac

# pid/log 路径在解析完 --target 后再定
PID_FILE="${PID_FILE_OVERRIDE:-/tmp/claw_client_${TARGET}.pid}"
LOG_FILE="/var/log/cmcc_robot/claw_client_node.log"

if [ -z "${PRODUCT_NO}" ]; then
  if [ "${ARCH}" = "aarch64" ]; then
    PRODUCT_NO="2.0"
  else
    PRODUCT_NO="0.0"
  fi
fi

case "${BUILD_TYPE}" in
  debug|release) ;;
  *)
    echo "[ERROR] --build 仅支持 debug|release"
    exit 1
    ;;
esac

PATH_BASE="${WS_ROOT}/build_${PRODUCT_NO}/${BUILD_TYPE}"
INSTALL_BASE="${PATH_BASE}/install"
EXE_NAME="${TARGET_EXE[${TARGET}]}"
EXE_PATH="${INSTALL_BASE}/${PKG_NAME}/lib/${PKG_NAME}/${EXE_NAME}"
SHARE_CONFIG_DIR="${INSTALL_BASE}/${PKG_NAME}/share/${PKG_NAME}/config"
SRC_CONFIG_DIR="${SCRIPT_DIR}/config"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " claw_client 启动"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "[NOTE] target:      ${TARGET} (${EXE_NAME})"
echo "[NOTE] product:     ${PRODUCT_NO} / ${BUILD_TYPE}"
echo "[NOTE] install:     ${INSTALL_BASE}"
echo "[NOTE] launch:      ${USE_LAUNCH}"
echo ""

# ---------- 加载 ROS 环境 ----------
load_env() {
  if [ -f /opt/ros/foxy/setup.bash ]; then
    # shellcheck disable=SC1091
    source /opt/ros/foxy/setup.bash
    echo "[NOTE] ROS2 Foxy OK"
  elif [ -n "${ROS_DISTRO:-}" ] && [ -f "/opt/ros/${ROS_DISTRO}/setup.bash" ]; then
    # shellcheck disable=SC1091
    source "/opt/ros/${ROS_DISTRO}/setup.bash"
    echo "[NOTE] ROS2 ${ROS_DISTRO} OK"
  else
    echo "[ERROR] 未找到 ROS2 setup.bash"
    return 1
  fi

  if [ -f "${INSTALL_BASE}/setup.bash" ]; then
    # shellcheck disable=SC1090
    source "${INSTALL_BASE}/setup.bash"
    echo "[NOTE] workspace OK: ${INSTALL_BASE}"
  else
    echo "[ERROR] 未找到 ${INSTALL_BASE}/setup.bash"
    echo "        请先编译: ${SCRIPT_DIR}/build.sh --no=${PRODUCT_NO} --build=${BUILD_TYPE}"
    return 1
  fi
}

resolve_params() {
  if [ -n "${PARAMS_FILE}" ]; then
    if [ ! -f "${PARAMS_FILE}" ]; then
      echo "[ERROR] 参数文件不存在: ${PARAMS_FILE}"
      return 1
    fi
    return 0
  fi

  local default_name="${TARGET_DEFAULT_PARAMS[${TARGET}]:-}"
  if [ -z "${default_name}" ]; then
    return 0
  fi

  if [ -f "${SHARE_CONFIG_DIR}/${default_name}" ]; then
    PARAMS_FILE="${SHARE_CONFIG_DIR}/${default_name}"
  elif [ -f "${SRC_CONFIG_DIR}/${default_name}" ]; then
    PARAMS_FILE="${SRC_CONFIG_DIR}/${default_name}"
  else
    echo "[WARN] 未找到默认参数文件 ${default_name}，将使用节点内置默认值"
  fi
}

check_ready() {
  local ok=0

  if [ ! -e "${EXE_PATH}" ]; then
    echo "[ERROR] 未找到可执行文件: ${EXE_PATH}"
    echo "        请先运行: ${SCRIPT_DIR}/build.sh"
    ok=1
  else
    echo "[OK  ] binary: ${EXE_PATH}"
  fi

  # hermes_bridge 第三方依赖
  if [ "${TARGET}" = "hermes" ]; then
    for dep in aiohttp psutil; do
      if ! python3 -c "import ${dep}" >/dev/null 2>&1; then
        echo "[ERROR] 缺少 Python 依赖 ${dep}（hermes_bridge 必需）"
        case "${dep}" in
          aiohttp) echo "        pip3 install 'aiohttp>=3.8,<4'" ;;
          psutil)  echo "        pip3 install 'psutil>=5.9,<7'" ;;
        esac
        ok=1
      else
        echo "[OK  ] ${dep}"
      fi
    done
  else
    if ! python3 -c "import aiohttp" >/dev/null 2>&1; then
      echo "[WARN] 未安装 aiohttp（仅 hermes_bridge 需要）"
    fi
  fi

  if ! python3 -c "import rclpy" >/dev/null 2>&1; then
    echo "[ERROR] 无法 import rclpy（ROS 环境未正确 source？）"
    ok=1
  else
    echo "[OK  ] rclpy"
  fi

  # 检查 homi_speech_interface
  if ! python3 -c "from homi_speech_interface.msg import AssistantEvent" >/dev/null 2>&1; then
    echo "[WARN] 无法 import homi_speech_interface.msg.AssistantEvent"
    echo "       SpeechCore 相关功能可能不可用；可: ./build.sh --deps"
  else
    echo "[OK  ] homi_speech_interface"
  fi

  if [ -n "${PARAMS_FILE}" ]; then
    echo "[OK  ] params: ${PARAMS_FILE}"
  fi

  # hermes 端口占用提示
  local port="${SERVER_PORT:-8800}"
  if [ "${TARGET}" = "hermes" ]; then
    if command -v ss >/dev/null 2>&1; then
      if ss -lnt 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}\$"; then
        echo "[WARN] 端口 ${port} 似乎已被占用："
        ss -lntp 2>/dev/null | grep -E "[:.]${port}\s" || true
        echo "       hermes_bridge 启动可能失败；或 Gateway 已在连别的进程"
      else
        echo "[OK  ] port ${port} free"
      fi
    fi
  fi

  return "${ok}"
}

build_ros_cmd() {
  local -a cmd=()

  if [ "${USE_LAUNCH}" = "y" ]; then
    cmd=(ros2 launch "${PKG_NAME}" claw_client.launch.py)
    # launch 文件目前写死 parameters=[hermes_config]；额外覆盖通过 ros args 不直接透传
    # 若用户指定了 --params/--host/--port，改为直接 run，避免 launch 限制
    if [ -n "${PARAMS_FILE}" ] || [ -n "${SERVER_HOST}" ] || [ -n "${SERVER_PORT}" ]; then
      echo "[WARN] --launch 与 --params/--host/--port 同时使用时，改走 ros2 run 以便覆盖参数"
      USE_LAUNCH="n"
    else
      ROS_CMD=("${cmd[@]}")
      return 0
    fi
  fi

  cmd=(ros2 run "${PKG_NAME}" "${EXE_NAME}")

  local -a ros_args=()
  if [ -n "${PARAMS_FILE}" ]; then
    ros_args+=(--params-file "${PARAMS_FILE}")
  fi
  if [ -n "${SERVER_HOST}" ]; then
    ros_args+=(-p "server_host:=${SERVER_HOST}")
  fi
  if [ -n "${SERVER_PORT}" ]; then
    ros_args+=(-p "server_port:=${SERVER_PORT}")
  fi

  if [ ${#ros_args[@]} -gt 0 ]; then
    cmd+=(--ros-args "${ros_args[@]}")
  fi

  if [ ${#EXTRA_ROS_ARGS[@]} -gt 0 ]; then
    cmd+=("${EXTRA_ROS_ARGS[@]}")
  fi

  ROS_CMD=("${cmd[@]}")
}

# ---------- main flow ----------
load_env
resolve_params

echo ""
echo ">>> 环境检查"
if ! check_ready; then
  exit 1
fi

if [ "${DO_CHECK_ONLY}" = "y" ]; then
  echo ""
  echo "[NOTE] --check 完成，未启动节点"
  exit 0
fi

build_ros_cmd

echo ""
echo ">>> 启动命令:"
printf '  '; printf '%q ' "${ROS_CMD[@]}"; echo
echo ""

if [ "${TARGET}" = "hermes" ]; then
  local_port="${SERVER_PORT:-8800}"
  echo "[NOTE] XiaoliChannel HTTP 将监听 :${local_port}"
  echo "[NOTE] Hermes Gateway 侧请配置:"
  echo "         XIAOLI_BASE_URL=http://localhost:${local_port}"
  echo "         XIAOLICHANNEL_APP_ID / APP_SECRET 与 hermes_bridge.yaml 一致"
  echo ""
fi

if [ "${BACKGROUND}" = "y" ]; then
  if [ -f "${PID_FILE}" ]; then
    old_pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [ -n "${old_pid}" ] && kill -0 "${old_pid}" 2>/dev/null; then
      echo "[ERROR] 已有实例在跑 (pid=${old_pid}, pidfile=${PID_FILE})"
      echo "        先: kill ${old_pid}  或  rm ${PID_FILE}"
      exit 1
    fi
  fi
  # 后台
  nohup "${ROS_CMD[@]}" >"${LOG_FILE}" 2>&1 &
  new_pid=$!
  echo "${new_pid}" >"${PID_FILE}"
  echo "[OK  ] 已后台启动 pid=${new_pid}"
  echo "       pidfile: ${PID_FILE}"
  echo "       log:     ${LOG_FILE}"
  echo "       停服:    kill \$(cat ${PID_FILE})"
  # 稍等看是否秒退
  sleep 1
  if ! kill -0 "${new_pid}" 2>/dev/null; then
    echo "[ERROR] 进程已退出，日志如下:"
    tail -n 40 "${LOG_FILE}" || true
    exit 1
  fi
  exit 0
fi

# 前台：Ctrl+C 交给 ros2
exec "${ROS_CMD[@]}"
