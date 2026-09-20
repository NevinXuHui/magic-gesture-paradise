#!/bin/bash
set -eu

# SmartApp Runtime 一键启动脚本
# 自动处理环境检查、安装、配置验证和启动

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
VENV_DIR="$SCRIPT_DIR/.venv"
CONFIG_FILE="$SCRIPT_DIR/config/runtime.example.toml"
PYTHON=${PYTHON:-python3}

echo "=========================================="
echo "SmartApp Runtime 启动脚本"
echo "=========================================="

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

# 验证配置
echo ""
echo "🔍 验证配置文件..."
PYTHONPATH="$SCRIPT_DIR/src" "$PYTHON" -m smartapp_runtime \
    --config "$CONFIG_FILE" \
    --check-config

if [ $? -eq 0 ]; then
    echo "✓ 配置验证通过"
else
    echo "❌ 配置验证失败"
    exit 1
fi

# 启动Runtime
echo ""
echo "=========================================="
echo "🚀 启动 SmartApp Runtime"
echo "=========================================="
echo "配置文件: $CONFIG_FILE"
echo "Unix Socket: /tmp/smartapp-runtime/run/runtime.sock"
echo "静态Web服务: http://127.0.0.1:18080"
echo "后端服务: http://127.0.0.1:18081"
echo ""
echo "按 Ctrl+C 停止服务"
echo "=========================================="
echo ""

# 前台运行
PYTHONPATH="$SCRIPT_DIR/src" "$PYTHON" -m smartapp_runtime \
    --config "$CONFIG_FILE"
