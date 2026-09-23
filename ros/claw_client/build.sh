#!/bin/bash
# claw_client 编译脚本（ament_python / ROS2 Foxy）
#
# 用法:
#   ./build.sh
#   ./build.sh --symlink          # 开发模式：源码改动即时生效（默认）
#   ./build.sh --no-symlink       # 正式安装拷贝
#   ./build.sh --no=2.0           # 指定产品编号（默认 aarch64→2.0，其它→0.0）
#   ./build.sh --build=debug      # debug|release（默认 debug）
#   ./build.sh --clean            # 清理本包 build/log 后重编
#   ./build.sh --deps             # 同时编译依赖 homi_speech_interface
#
# 不用 nounset：/opt/ros/*/setup.bash 会读未导出变量
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
# src/claw_client → 仓库根 xiaoli_application_ros2
WS_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ARCH="$(uname -m)"
PKG_NAME="claw_client"

# 默认：开发机常用 symlink；aarch64 产品号 2.0
PRODUCT_NO=""
BUILD_TYPE="debug"
SYMLINK="y"
DO_CLEAN="n"
BUILD_DEPS="n"
EXTRA_COLCON_ARGS=()

usage() {
  cat <<EOF
用法: $0 [选项]

选项:
  --no=0.0|1.0|1.1|2.0   产品编号（决定 install 根目录）
  --build=debug|release  构建类型目录（默认 debug）
  --symlink              colcon --symlink-install（默认开启）
  --no-symlink           关闭 symlink-install
  --clean                清理本包 build/log 后重编
  --deps                 同时编译依赖 homi_speech_interface
  --help / -h            显示帮助

环境变量:
  CLAW_INSTALL_BASE      覆盖 install 根目录（默认 build_<no>/<type>/install）
                         权限异常时可设为包内可写目录，如:
                           CLAW_INSTALL_BASE=./install_local ./build.sh --clean

产物安装到:
  \${CLAW_INSTALL_BASE:-\${WS_ROOT}/build_\${PRODUCT_NO}/\${BUILD_TYPE}/install}/${PKG_NAME}

示例:
  $0
  $0 --no=2.0 --build=debug
  $0 --clean --deps
EOF
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage ;;
    --no=*) PRODUCT_NO="${1#*=}" ;;
    --build=*) BUILD_TYPE="${1#*=}" ;;
    --symlink) SYMLINK="y" ;;
    --no-symlink) SYMLINK="n" ;;
    --clean) DO_CLEAN="y" ;;
    --deps) BUILD_DEPS="y" ;;
    *)
      echo "[ERROR] 未知参数: $1"
      usage
      ;;
  esac
  shift
done

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
    echo "[ERROR] --build 仅支持 debug|release，收到: ${BUILD_TYPE}"
    exit 1
    ;;
esac

PATH_BASE="${WS_ROOT}/build_${PRODUCT_NO}/${BUILD_TYPE}"
INSTALL_BASE="${PATH_BASE}/install"
BUILD_BASE="${SCRIPT_DIR}/build"
LOG_BASE="${SCRIPT_DIR}/log"
PKG_BUILD_DIR="${BUILD_BASE}/${PKG_NAME}"

# 允许覆盖 install 根（权限异常时可用本地目录）
#   CLAW_INSTALL_BASE=/tmp/claw_install ./build.sh
if [ -n "${CLAW_INSTALL_BASE:-}" ]; then
  INSTALL_BASE="${CLAW_INSTALL_BASE}"
fi

# 实际写探测：fakeowner 挂载上 os.access 可能撒谎
path_is_writable() {
  local target="$1"
  local probe
  if [ -d "${target}" ]; then
    probe="${target}/.claw_write_probe_$$"
  elif [ -f "${target}" ]; then
    probe="${target}.claw_write_probe_$$"
  else
    # 目标不存在：测父目录
    local parent
    parent="$(dirname "${target}")"
    mkdir -p "${parent}" 2>/dev/null || true
    probe="${parent}/.claw_write_probe_$$"
  fi
  if ( : >"${probe}" ) 2>/dev/null; then
    rm -f "${probe}" 2>/dev/null || true
    return 0
  fi
  rm -f "${probe}" 2>/dev/null || true
  return 1
}

safe_rm_rf() {
  # 返回 0=成功或不存在；1=权限失败
  local target="$1"
  if [ ! -e "${target}" ]; then
    return 0
  fi
  if rm -rf "${target}" 2>/dev/null; then
    return 0
  fi
  # 再试 chmod 后删
  chmod -R u+w "${target}" 2>/dev/null || true
  if rm -rf "${target}" 2>/dev/null; then
    return 0
  fi
  return 1
}

TOTAL_START="$(date +%s.%N)"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " claw_client 编译"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "[NOTE] arch:        ${ARCH}"
echo "[NOTE] product:     ${PRODUCT_NO}"
echo "[NOTE] build type:  ${BUILD_TYPE}"
echo "[NOTE] ws root:     ${WS_ROOT}"
echo "[NOTE] package:     ${SCRIPT_DIR}"
echo "[NOTE] install:     ${INSTALL_BASE}"
echo "[NOTE] symlink:     ${SYMLINK}"
echo "[NOTE] build deps:  ${BUILD_DEPS}"
echo ""

# ---------- 环境 ----------
if ! command -v colcon >/dev/null 2>&1; then
  echo "[ERROR] 未找到 colcon，请先安装 ROS2 / colcon"
  exit 1
fi

if [ -f /opt/ros/foxy/setup.bash ]; then
  # shellcheck disable=SC1091
  source /opt/ros/foxy/setup.bash
  echo "[NOTE] 已加载 ROS2 Foxy: /opt/ros/foxy"
elif [ -n "${ROS_DISTRO:-}" ] && [ -f "/opt/ros/${ROS_DISTRO}/setup.bash" ]; then
  # shellcheck disable=SC1091
  source "/opt/ros/${ROS_DISTRO}/setup.bash"
  echo "[NOTE] 已加载 ROS2 ${ROS_DISTRO}"
else
  echo "[WARN] 未找到 /opt/ros/foxy/setup.bash，继续尝试（可能失败）"
fi

# ---------- 清理（必须在 source setup 之前）----------
# 1) symlink-install 的 package.dsv 会引用 build 下 pythonpath_develop.dsv
# 2) colcon 每次都会重写 install/.../hook/* ；旧文件若无写权限会 Permission denied
# 3) 本脚本只编 claw_client，重建前摘掉旧 install/claw_client 是安全的
DEVELOP_HOOK="${PKG_BUILD_DIR}/share/${PKG_NAME}/hook/pythonpath_develop.dsv"

# 默认每次重建都 purge 本包 install，避免 hook 文件 Permission denied
NEED_PURGE_INSTALL="y"
NEED_PURGE_BUILD="n"
if [ "${DO_CLEAN}" = "y" ]; then
  NEED_PURGE_BUILD="y"
elif [ -d "${PKG_BUILD_DIR}" ] && ! path_is_writable "${PKG_BUILD_DIR}"; then
  echo "[WARN] build 目录不可写，将强制清理: ${PKG_BUILD_DIR}"
  NEED_PURGE_BUILD="y"
elif [ -d "${INSTALL_BASE}/${PKG_NAME}" ] && [ ! -f "${DEVELOP_HOOK}" ]; then
  echo "[WARN] 检测到残缺 develop hook: ${DEVELOP_HOOK}"
fi

if [ "${NEED_PURGE_BUILD}" = "y" ]; then
  echo "[NOTE] 清理本包构建缓存: ${PKG_BUILD_DIR}"
  if ! safe_rm_rf "${PKG_BUILD_DIR}"; then
    echo "[ERROR] 无法删除 build 目录（Permission denied）: ${PKG_BUILD_DIR}"
    echo "        常见原因：目录由其他用户/容器创建，或 /mine fakeowner 权限不一致"
    echo "        请在有权限的环境执行:"
    echo "          chmod -R u+w \"${PKG_BUILD_DIR}\" && rm -rf \"${PKG_BUILD_DIR}\""
    echo "        或换本地可写目录:"
    echo "          CLAW_INSTALL_BASE=${SCRIPT_DIR}/install_local ${SCRIPT_DIR}/build.sh --clean"
    exit 1
  fi
  rm -rf "${LOG_BASE}/${PKG_NAME}" 2>/dev/null || true
fi

if [ "${NEED_PURGE_INSTALL}" = "y" ]; then
  echo "[NOTE] 移除旧 install/${PKG_NAME}（即将重建）..."
  if ! safe_rm_rf "${INSTALL_BASE}/${PKG_NAME}"; then
    echo "[ERROR] 无法删除 install/${PKG_NAME}（Permission denied）"
    echo "        path: ${INSTALL_BASE}/${PKG_NAME}"
    echo "        colcon 需要重写 hook/*.dsv/*.sh，旧文件不可写会直接失败。"
    echo ""
    echo "        修复方式任选其一:"
    echo "        1) 清掉旧产物后再编:"
    echo "             chmod -R u+w \"${INSTALL_BASE}/${PKG_NAME}\" \"${PKG_BUILD_DIR}\" 2>/dev/null"
    echo "             rm -rf \"${INSTALL_BASE}/${PKG_NAME}\" \"${PKG_BUILD_DIR}\""
    echo "             ${SCRIPT_DIR}/build.sh"
    echo "        2) 改用本地可写 install 根:"
    echo "             CLAW_INSTALL_BASE=${SCRIPT_DIR}/install_local ${SCRIPT_DIR}/build.sh --clean"
    echo "        3) 在创建这些文件的同一环境/用户下编译"
    exit 1
  fi
fi

mkdir -p "${BUILD_BASE}" "${LOG_BASE}" "${INSTALL_BASE}"

# 写权限预检（避免 colcon 跑到一半才 Permission denied）
if ! path_is_writable "${INSTALL_BASE}"; then
  echo "[ERROR] install 根目录不可写: ${INSTALL_BASE}"
  echo "        请设置可写目录: CLAW_INSTALL_BASE=${SCRIPT_DIR}/install_local $0"
  exit 1
fi
if ! path_is_writable "${BUILD_BASE}"; then
  echo "[ERROR] build 根目录不可写: ${BUILD_BASE}"
  exit 1
fi

# 若 install 已存在，先 source，以便解析已有依赖（homi_speech_interface 等）
# 注意：此时 claw_client 已被摘掉，source 不会再踩失效 develop hook
if [ -f "${INSTALL_BASE}/setup.bash" ]; then
  # shellcheck disable=SC1090
  if source "${INSTALL_BASE}/setup.bash"; then
    echo "[NOTE] 已加载工作空间: ${INSTALL_BASE}"
  else
    echo "[WARN] 加载工作空间失败（可能仍有失效 hook），继续尝试编译..."
  fi
else
  echo "[WARN] 尚未有 ${INSTALL_BASE}/setup.bash"
  echo "       若缺 homi_speech_interface，请先:"
  echo "         cd ${WS_ROOT} && ./build.sh --no=${PRODUCT_NO} --select=homi_speech_interface --nopack"
  echo "       或对本脚本加 --deps"
fi

# 运行依赖检查（hermes_bridge 需要 aiohttp + psutil）
for dep_spec in "aiohttp:aiohttp>=3.8,<4" "psutil:psutil>=5.9,<7"; do
  dep="${dep_spec%%:*}"
  pip_spec="${dep_spec#*:}"
  if ! python3 -c "import ${dep}" >/dev/null 2>&1; then
    echo "[WARN] 当前 Python 未安装 ${dep}（hermes_bridge 运行需要）"
    echo "       安装: pip3 install '${pip_spec}'"
  fi
done

# ---------- 选择编译目标 ----------
SELECT_PKGS="${PKG_NAME}"
if [ "${BUILD_DEPS}" = "y" ]; then
  SELECT_PKGS="homi_speech_interface ${PKG_NAME}"
fi

COLCON_ARGS=(
  --log-base "${LOG_BASE}"
  build
  --packages-select ${SELECT_PKGS}
  --build-base "${BUILD_BASE}"
  --install-base "${INSTALL_BASE}"
)

if [ "${SYMLINK}" = "y" ]; then
  COLCON_ARGS+=(--symlink-install)
fi

# ament_python 包无需 cmake 参数；保留扩展位
if [ ${#EXTRA_COLCON_ARGS[@]} -gt 0 ]; then
  COLCON_ARGS+=("${EXTRA_COLCON_ARGS[@]}")
fi

echo ""
echo ">>> colcon ${COLCON_ARGS[*]}"
echo ""

cd "${WS_ROOT}"
# colcon 需要能在 src 下发现包。仓库布局是 src/claw_client，符合标准。
colcon "${COLCON_ARGS[@]}"

# ---------- 校验产物 ----------
BRIDGE_BIN="${INSTALL_BASE}/${PKG_NAME}/lib/${PKG_NAME}/hermes_bridge"
if [ ! -e "${BRIDGE_BIN}" ]; then
  echo "[ERROR] 编译后未找到 hermes_bridge: ${BRIDGE_BIN}"
  exit 1
fi

TOTAL_END="$(date +%s.%N)"
TOTAL_TIME="$(awk "BEGIN {printf \"%.2f\", ${TOTAL_END} - ${TOTAL_START}}")"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " ✅ 编译完成  (${TOTAL_TIME}s)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  包安装:  ${INSTALL_BASE}/${PKG_NAME}"
echo "  可执行:"
for exe in hermes_bridge openclaw_bridge claw_client_node external_chat test_send; do
  p="${INSTALL_BASE}/${PKG_NAME}/lib/${PKG_NAME}/${exe}"
  if [ -e "${p}" ]; then
    echo "    - ${exe}"
  fi
done
echo ""
echo "  启动:"
echo "    ${SCRIPT_DIR}/start.sh"
echo "    ${SCRIPT_DIR}/start.sh --target hermes"
echo "    ${SCRIPT_DIR}/start.sh --launch"
echo ""
