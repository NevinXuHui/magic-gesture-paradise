#!/bin/bash
set -eu

# SmartApp Runtime 测试客户端脚本
# 用于快速测试Runtime各种命令

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
SOCKET="/tmp/smartapp-runtime/run/runtime.sock"
CLIENT="$SCRIPT_DIR/examples/agent_client.py"
PYTHON=${PYTHON:-python3}

echo "=========================================="
echo "SmartApp Runtime 测试客户端"
echo "=========================================="

# 检查socket是否存在
if [ ! -S "$SOCKET" ]; then
    echo "❌ Runtime未运行或Socket不存在: $SOCKET"
    echo "请先运行: ./run.sh"
    exit 1
fi

echo "✓ Socket已连接: $SOCKET"
echo ""

# 显示菜单
show_menu() {
    echo "可用命令:"
    echo "  1) 查询状态 (get_status)"
    echo "  2) 启动应用 (start_app) - 示例"
    echo "  3) 停止应用 (stop_app) - 示例"
    echo "  4) 发送云端数据 (cloud_data) - 示例"
    echo "  5) 自定义JSON命令"
    echo "  0) 退出"
    echo ""
}

# 发送命令
send_command() {
    local json="$1"
    echo "📤 发送命令:"
    echo "$json" | "$PYTHON" -m json.tool 2>/dev/null || echo "$json"
    echo ""
    echo "📥 响应:"
    "$PYTHON" "$CLIENT" --socket "$SOCKET" --json "$json"
    echo ""
}

# 主循环
while true; do
    show_menu
    read -p "选择操作 [0-5]: " choice
    echo ""

    case $choice in
        1)
            send_command '{"requestId":"req-status-1","command":"get_status"}'
            ;;
        2)
            echo "示例: 启动 demo_app"
            send_command '{
                "requestId":"req-start-1",
                "command":"start_app",
                "sessionId":"session-demo-1",
                "appId":"demo_app",
                "version":"1.0.0",
                "packageUrl":"https://packages.example.invalid/demo_app-1.0.0.tar.gz",
                "packageSize":12345,
                "sha256":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                "initData":{"locale":"zh-CN"}
            }'
            ;;
        3)
            read -p "输入sessionId [session-demo-1]: " sid
            sid=${sid:-session-demo-1}
            send_command "{\"requestId\":\"req-stop-1\",\"command\":\"stop_app\",\"sessionId\":\"$sid\",\"reason\":\"operator\"}"
            ;;
        4)
            read -p "输入sessionId [session-demo-1]: " sid
            sid=${sid:-session-demo-1}
            send_command "{\"requestId\":\"req-data-1\",\"command\":\"cloud_data\",\"sessionId\":\"$sid\",\"seq\":1,\"target\":\"auto\",\"dataType\":\"gesture\",\"trigger\":\"cloud\",\"data\":{\"name\":\"rock\"}}"
            ;;
        5)
            echo "输入完整的JSON命令 (单行):"
            read -r custom_json
            if [ -n "$custom_json" ]; then
                send_command "$custom_json"
            else
                echo "❌ 命令为空"
            fi
            ;;
        0)
            echo "👋 退出"
            exit 0
            ;;
        *)
            echo "❌ 无效选择"
            ;;
    esac

    read -p "按Enter继续..." dummy
    echo ""
done
