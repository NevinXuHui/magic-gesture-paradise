#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG_DIR="$SCRIPT_DIR/config/validation"
RUNTIME_CONFIG="$CONFIG_DIR/runtime-rps.toml"
CLIENT="$SCRIPT_DIR/examples/agent_client.py"
PYTHON=${PYTHON:-"$SCRIPT_DIR/.venv/bin/python"}
SOCKET=''
APP='rps'
START_CONFIG='start-app.json'
STOP_CONFIG='stop-app.json'
CLOUD_CONFIG='cloud-data.json'

usage() {
    cat <<'EOF'
用法: ./test_client.sh [应用] <命令>

应用:
  rps          操作 rps-kids-h5（默认）
  english     操作 english SmartApp

命令:
  start        启动应用
  stop         停止应用
  restart      停止后重新启动应用
  status       查询 Runtime 和应用状态
  cloud-data   发送当前应用的 cloud_data
  data         cloud-data 的别名
  help         显示帮助

不传命令时进入交互菜单。所有请求均通过 examples/agent_client.py 发送。
EOF
}

select_app() {
    case "$1" in
        rps)
            APP='rps'
            START_CONFIG='start-app.json'
            STOP_CONFIG='stop-app.json'
            CLOUD_CONFIG='cloud-data.json'
            ;;
        english)
            APP='english'
            START_CONFIG='english-start-app.json'
            STOP_CONFIG='english-stop-app.json'
            CLOUD_CONFIG='english-cloud-data.json'
            ;;
        *)
            fail "不支持的应用: $1（可选 rps 或 english）"
            ;;
    esac
}

fail() {
    echo "test_client: $*" >&2
    exit 2
}

prepare() {
    [[ -x "$PYTHON" ]] || fail "Python 不可用: $PYTHON"
    [[ -f "$CLIENT" ]] || fail "客户端不存在: $CLIENT"
    [[ -r "$RUNTIME_CONFIG" ]] || fail "Runtime 配置不可读: $RUNTIME_CONFIG"

    SOCKET=$(PYTHONPATH="$SCRIPT_DIR/src" "$PYTHON" -c \
        'import sys; from smartapp_runtime.config import load_config; print(load_config(sys.argv[1]).paths.socket)' \
        "$RUNTIME_CONFIG") || fail "无法读取 Runtime Socket 配置"
    [[ -n "$SOCKET" ]] || fail "Runtime Socket 配置为空"
    [[ -S "$SOCKET" ]] || fail "Runtime 未运行或 Socket 不存在: $SOCKET；请先执行 $SCRIPT_DIR/run.sh"
}

send_config() {
    local name=$1
    local timeout=$2
    local config_file="$CONFIG_DIR/$name"
    local status

    [[ -r "$config_file" ]] || fail "命令配置不可读: $config_file"
    echo "发送配置: $config_file"
    "$PYTHON" "$CLIENT" \
        --socket "$SOCKET" \
        --file "$config_file" \
        --timeout "$timeout"
    status=$?
    echo
    return "$status"
}

run_command() {
    case "$1" in
        start)
            send_config "$START_CONFIG" 180
            ;;
        stop)
            send_config "$STOP_CONFIG" 20
            ;;
        restart)
            send_config "$STOP_CONFIG" 20 && send_config "$START_CONFIG" 180
            ;;
        status)
            send_config status.json 10
            ;;
        cloud-data|data)
            send_config "$CLOUD_CONFIG" 10
            ;;
        *)
            usage >&2
            return 2
            ;;
    esac
}

interactive_menu() {
    local choice
    while true; do
        cat <<'EOF'
SmartApp 应用控制
  1) 查询状态
  2) 启动应用
  3) 停止应用
  4) 发送云端数据
  5) 重启应用
  0) 退出
EOF
        read -r -p "选择操作 [0-5]: " choice || return 0
        case "$choice" in
            1) run_command status || true ;;
            2) run_command start || true ;;
            3) run_command stop || true ;;
            4) run_command cloud-data || true ;;
            5) run_command restart || true ;;
            0) return 0 ;;
            *) echo "无效选择: $choice" >&2 ;;
        esac
    done
}

if [[ ${1:-} == rps || ${1:-} == english ]]; then
    select_app "$1"
    shift
fi
if [[ $# -gt 1 ]]; then
    usage >&2
    exit 2
fi
if [[ ${1:-} == help || ${1:-} == --help || ${1:-} == -h ]]; then
    usage
    exit 0
fi

if [[ $# -eq 0 ]]; then
    select_app "$APP"
fi

prepare
if [[ $# -eq 0 ]]; then
    interactive_menu
else
    run_command "$1"
fi
