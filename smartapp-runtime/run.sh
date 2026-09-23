#!/bin/bash
set -eu

# SmartApp Runtime 一键启动脚本
# 自动处理环境检查、安装、配置验证和启动

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
VENV_DIR="$SCRIPT_DIR/.venv"
CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/config/validation/runtime.toml}"
PYTHON=${PYTHON:-python3.8}
VENV_PYTHON="$VENV_DIR/bin/python"
LOCAL_PACKAGE_CERT="$SCRIPT_DIR/smartapp-rps-test.crt"
LOCAL_PACKAGE_KEY="$SCRIPT_DIR/smartapp-rps-test.key"
DEVICE_ELECTRON="/root/electron/node_modules/electron/dist/electron"
RPS_PACKAGE_SOURCE="$SCRIPT_DIR/../rps-kids-h5/build/smartapp/rock_paper_scissors-0.1.11.tar.gz"
ENGLISH_PACKAGE_SOURCE="$SCRIPT_DIR/../english/build/smartapp/cloud_show_display-0.2.0.tar.gz"
LOCAL_PACKAGE_DIR=''
PACKAGE_SERVER_PID=''
PACKAGE_SERVER_LOG=''

cleanup_package_server() {
    if [ -n "$PACKAGE_SERVER_PID" ] && kill -0 "$PACKAGE_SERVER_PID" 2>/dev/null; then
        kill "$PACKAGE_SERVER_PID" 2>/dev/null || true
        wait "$PACKAGE_SERVER_PID" 2>/dev/null || true
    fi
    if [ -n "$LOCAL_PACKAGE_DIR" ] && [ -d "$LOCAL_PACKAGE_DIR" ]; then
        rm -rf -- "$LOCAL_PACKAGE_DIR"
    fi
}

trap cleanup_package_server EXIT

echo "=========================================="
echo "SmartApp Runtime 启动脚本"
echo "=========================================="

# 配置中的 Renderer 命令使用相对项目根目录的路径。
cd "$SCRIPT_DIR"
if [ -z "${SSL_CERT_FILE:-}" ] && [ -f "$LOCAL_PACKAGE_CERT" ]; then
    export SSL_CERT_FILE="$LOCAL_PACKAGE_CERT"
fi

# Validation runs on the robot before the renderer's pinned Electron may have
# been installed locally. Reuse the device Electron when it is available.
if [ -z "${ELECTRON_BIN_OVERRIDE:-}" ] \
    && [ ! -x "$SCRIPT_DIR/renderer/node_modules/electron/dist/electron" ] \
    && [ -x "$DEVICE_ELECTRON" ]; then
    export ELECTRON_BIN_OVERRIDE="$DEVICE_ELECTRON"
fi

# 本地验证包服务不应经过系统 HTTP(S) 代理。
case ",${NO_PROXY:-}," in
    *,127.0.0.1,*) ;;
    *) export NO_PROXY="${NO_PROXY:+$NO_PROXY,}127.0.0.1,localhost" ;;
esac
export no_proxy="$NO_PROXY"

# 检查Python版本
echo "📌 检查Python环境..."
if ! command -v "$PYTHON" &> /dev/null; then
    echo "❌ 错误: 未找到 $PYTHON"
    exit 1
fi

PYTHON_VERSION=$("$PYTHON" --version 2>&1 | awk '{print $2}')
echo "✓ Python版本: $PYTHON_VERSION"

# 检查并创建虚拟环境
if [ ! -d "$VENV_DIR" ]; then
    echo ""
    echo "📦 创建虚拟环境..."
    "$PYTHON" -m venv "$VENV_DIR"
    echo "✓ 虚拟环境已创建: $VENV_DIR"

    echo ""
    echo "📦 安装项目依赖..."
    "$VENV_DIR/bin/python" -m pip install --upgrade pip > /dev/null 2>&1
    "$VENV_DIR/bin/python" -m pip install "$SCRIPT_DIR" > /dev/null 2>&1
    echo "✓ 依赖安装完成"
else
    echo "✓ 虚拟环境已存在"
fi

if [ ! -x "$VENV_PYTHON" ]; then
    echo "❌ 错误: 虚拟环境不可用: $VENV_PYTHON"
    exit 1
fi

PYTHON_VERSION=$($VENV_PYTHON --version 2>&1 | awk '{print $2}')
echo "✓ 虚拟环境Python版本: $PYTHON_VERSION"

# 验证配置
echo ""
echo "🔍 验证配置文件..."
PYTHONPATH="$SCRIPT_DIR/src" "$VENV_PYTHON" -m smartapp_runtime \
    --config "$CONFIG_FILE" \
    --check-config

if [ $? -eq 0 ]; then
    echo "✓ 配置验证通过"
else
    echo "❌ 配置验证失败"
    exit 1
fi

# Fail before starting the temporary package server when this runtime root is
# already owned by another process. Use the runtime's lock implementation so
# this check has the same path and filesystem safety rules as normal startup.
RUNTIME_LOCK_FILE=$(PYTHONPATH="$SCRIPT_DIR/src" "$VENV_PYTHON" -c '
import sys
from smartapp_runtime.config import load_config
from smartapp_runtime.infrastructure.persistence.paths import RuntimePaths

config = load_config(sys.argv[1])
print(RuntimePaths.from_root(config.paths.root).lock_file)
' "$CONFIG_FILE")

if ! PYTHONPATH="$SCRIPT_DIR/src" "$VENV_PYTHON" -c '
import sys
from pathlib import Path
from smartapp_runtime.infrastructure.persistence.lock import SingleInstanceLock

lock = SingleInstanceLock(Path(sys.argv[1]))
lock.acquire()
lock.release()
' "$RUNTIME_LOCK_FILE" 2>/dev/null; then
    LOCK_HOLDER=$(fuser "$RUNTIME_LOCK_FILE" 2>/dev/null | awk '{$1=$1; print}' || true)
    echo ""
    echo "❌ SmartApp Runtime 已在运行，或实例锁不可用" >&2
    echo "实例锁: $RUNTIME_LOCK_FILE" >&2
    if [ -n "$LOCK_HOLDER" ]; then
        echo "持锁进程 PID: $LOCK_HOLDER" >&2
    fi
    echo "可执行 ./test_client.sh status 查询当前实例；先停止当前实例后才能重新启动。" >&2
    exit 1
fi

if [ ! -f "$RPS_PACKAGE_SOURCE" ] || [ ! -f "$ENGLISH_PACKAGE_SOURCE" ] \
    || [ ! -f "$LOCAL_PACKAGE_CERT" ] || [ ! -f "$LOCAL_PACKAGE_KEY" ]; then
    echo "❌ 本地验证包资源不完整，请先构建 rps-kids-h5 和 english SmartApp 包" >&2
    exit 1
fi

echo "📦 启动本地 HTTPS 包服务..."
# The package server only exposes validation inputs. Keep temporary links out of
# runtime-data; packages are downloaded and installed by start_app on demand.
LOCAL_PACKAGE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/smartapp-validation-packages.XXXXXX")
PACKAGE_SERVER_LOG="$LOCAL_PACKAGE_DIR/server.log"
ln -s "$RPS_PACKAGE_SOURCE" "$LOCAL_PACKAGE_DIR/$(basename "$RPS_PACKAGE_SOURCE")"
ln -s "$ENGLISH_PACKAGE_SOURCE" "$LOCAL_PACKAGE_DIR/$(basename "$ENGLISH_PACKAGE_SOURCE")"
"$VENV_PYTHON" "$SCRIPT_DIR/scripts/validation_package_server.py" \
    --directory "$LOCAL_PACKAGE_DIR" \
    --cert "$LOCAL_PACKAGE_CERT" \
    --key "$LOCAL_PACKAGE_KEY" \
    > "$PACKAGE_SERVER_LOG" 2>&1 &
PACKAGE_SERVER_PID=$!
for _ in $(seq 1 50); do
    if ! kill -0 "$PACKAGE_SERVER_PID" 2>/dev/null; then
        echo "❌ 本地 HTTPS 包服务启动失败" >&2
        cat "$PACKAGE_SERVER_LOG" >&2 || true
        exit 1
    fi
    if (echo >/dev/tcp/127.0.0.1/18443) 2>/dev/null; then
        break
    fi
    sleep 0.1
done
echo "✓ 本地包服务: https://127.0.0.1:18443"

# 启动Runtime
echo ""
echo "=========================================="
echo "🚀 启动 SmartApp Runtime"
echo "=========================================="
echo "配置文件: $CONFIG_FILE"
echo "Unix Socket: runtime-data/smartapp-runtime/run/runtime.sock"
echo "静态Web服务监听: http://0.0.0.0:18080"
echo "后端服务: http://127.0.0.1:18081"
echo ""
echo "按 Ctrl+C 停止服务"
echo "=========================================="
echo ""

# 前台运行
PYTHONPATH="$SCRIPT_DIR/src" "$VENV_PYTHON" -m smartapp_runtime \
    --config "$CONFIG_FILE"
