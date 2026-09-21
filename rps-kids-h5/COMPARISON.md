# 两个系统的对比与整合说明

## 系统对比

### 云端页面展示系统 (`/mine/Code/ROS/dice/electron/`)

**用途：** 通用的云端 H5 页面展示框架，用于显示远程或本地的 HTML 页面

**架构：**
```
UDP 指令(24021) → cloud_daemon.py(ROS) → UDP(24023) → Electron main.js
                                                            ↓
                                            Xvfb :98 → FFmpeg → FIFO
                                                            ↓
                                                    MPV (/tmp/mpv-socket)
```

**特点：**
- ✅ ROS 2 集成，可与表情系统交互
- ✅ UDP 协议推送内容（JSON 格式）
- ✅ 支持远程 URL 和本地页面
- ✅ 守护进程常驻，支持预热
- ✅ 摸头退出（3 次）
- ❌ 无摄像头支持
- ❌ 无用户交互（单向推送）

**典型应用：**
- 英文单词展示
- 通知/公告展示
- 信息推送

---

### 手势识别游戏 (`/mine/Code/ROS/dice/手势识别/rps-kids-h5/`)

**用途：** 互动手势识别游戏（剪刀石头布），基于摄像头和 AI 识别

**架构：**
```
摄像头 → server.py(:5174) → HTTP API
                               ↓
                    Electron screen-renderer.js
                               ↓
                    Xvfb :99 → FFmpeg → FIFO
                               ↓
                    MPV (/tmp/mpv-socket)
```

**特点：**
- ✅ 服务端摄像头（机器狗相机或 USB）
- ✅ 手势识别（MediaPipe + WASM）
- ✅ 交互式游戏
- ✅ Vue 3 + Element Plus
- ❌ 无 ROS 集成
- ❌ 非守护进程（脚本启动）
- ❌ 无远程内容推送

**典型应用：**
- 儿童互动游戏
- 手势识别演示
- AI 能力展示

---

## 核心差异表

| 维度 | 云端展示 | 手势游戏 |
|------|---------|---------|
| **框架** | 纯 Electron + 静态页面 | Vue 3 + Vite 构建 |
| **内容来源** | 远程 URL / 本地 HTML | 本地构建的 `dist/` |
| **交互模式** | 被动接收（UDP 推送） | 主动交互（摄像头） |
| **摄像头** | ❌ 无 | ✅ 必需 |
| **守护进程** | ✅ Python (ROS) | ❌ Bash 脚本 |
| **端口** | 24021-24023 | 5174 |
| **显示编号** | `:98` | `:99` |
| **依赖项** | Node.js + Electron | Node.js + Electron + Python + OpenCV |
| **启动方式** | `./start_cloud_daemon.sh` | `bash start-screen.sh` |
| **停止方式** | `./stop_cloud_daemon.sh` | `Ctrl-C` |
| **ROS 集成** | ✅ 深度集成 | ❌ 无 |
| **表情管理** | 自动禁用/恢复 | 自动禁用/恢复 |
| **适用场景** | 信息展示、内容推送 | 互动游戏、AI 演示 |

---

## 共同依赖

两个系统都需要：

### 1. 系统工具
```bash
sudo apt install -y xvfb ffmpeg socat
```

### 2. Node.js + Electron
```bash
# Node.js 18+
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install -y nodejs

# Electron（各自目录独立安装）
cd /mine/Code/ROS/dice/electron && npm install
cd /mine/Code/ROS/dice/手势识别/rps-kids-h5 && npm install
```

### 3. MPV Socket
```bash
# 机器狗表情系统提供
ls -la /tmp/mpv-socket
```

---

## 额外依赖

### 云端展示额外需要：

```bash
# ROS 2 Foxy
sudo apt install ros-foxy-ros-base python3-rclpy

# ROS 消息接口（机器人厂商提供）
# sudo apt install ros-foxy-homi-speech-interface
```

### 手势游戏额外需要：

```bash
# Python OpenCV（用于摄像头）
sudo apt install python3-opencv

# 或使用 pip
pip3 install opencv-python
```

---

## 同时运行两个系统

两个系统可以**独立安装**，但**不能同时运行**（争抢液晶屏）。

### 方案 1：交替使用

```bash
# 运行云端展示
cd /mine/Code/ROS/dice/electron
./start_cloud_daemon.sh --prewarm "$(pwd)/pages/english_show_800x480.html"
echo -n "SHOW" | socat - UDP4-DATAGRAM:127.0.0.1:24021

# 停止云端展示
./stop_cloud_daemon.sh

# 运行手势游戏
cd /mine/Code/ROS/dice/手势识别/rps-kids-h5
npm run build
bash start-screen.sh

# 停止手势游戏（Ctrl-C）
```

### 方案 2：创建统一管理脚本

创建 `/mine/Code/ROS/dice/screen-manager.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail

ELECTRON_DIR="/mine/Code/ROS/dice/electron"
GAME_DIR="/mine/Code/ROS/dice/手势识别/rps-kids-h5"

case "${1:-}" in
  cloud)
    echo "启动云端页面展示..."
    cd "$ELECTRON_DIR"
    ./start_cloud_daemon.sh --prewarm "$(pwd)/pages/english_show_800x480.html"
    echo -n "SHOW" | socat - UDP4-DATAGRAM:127.0.0.1:24021
    ;;
  game)
    echo "停止云端展示（如果运行中）..."
    cd "$ELECTRON_DIR"
    ./stop_cloud_daemon.sh 2>/dev/null || true
    
    echo "启动手势识别游戏..."
    cd "$GAME_DIR"
    [[ -f dist/index.html ]] || npm run build
    bash start-screen.sh
    ;;
  stop)
    echo "停止所有屏幕应用..."
    cd "$ELECTRON_DIR"
    ./stop_cloud_daemon.sh 2>/dev/null || true
    
    pkill -f screen-renderer.js 2>/dev/null || true
    pkill -f "Xvfb :99" 2>/dev/null || true
    
    "$ELECTRON_DIR/restore_expression.sh"
    ;;
  *)
    cat <<EOF
用法: $0 {cloud|game|stop}

  cloud  - 启动云端页面展示
  game   - 启动手势识别游戏
  stop   - 停止所有并恢复表情
EOF
    exit 1
    ;;
esac
```

使用：
```bash
chmod +x /mine/Code/ROS/dice/screen-manager.sh

# 显示云端内容
/mine/Code/ROS/dice/screen-manager.sh cloud

# 切换到游戏
/mine/Code/ROS/dice/screen-manager.sh game

# 停止所有
/mine/Code/ROS/dice/screen-manager.sh stop
```

---

## 资源占用对比

### 云端展示（空闲状态）

- **内存**: ~160-180 MB (Electron)
- **CPU**: <5% (IDLE 模式 1fps)
- **磁盘**: ~200 MB (Electron + 页面)

### 云端展示（活动状态）

- **内存**: ~180-200 MB
- **CPU**: 20-40% (30fps 渲染)
- **磁盘**: 同上

### 手势游戏（运行中）

- **内存**: ~180-250 MB (Electron + 摄像头)
- **CPU**: 30-60% (30fps + AI 推理)
- **磁盘**: ~300 MB (Electron + Vue 构建产物 + 模型)

---

## 开发建议

### 1. 添加新的展示内容

**使用云端展示系统：**
```bash
cd /mine/Code/ROS/dice/electron

# 方案 A：下载远程页面到本地
curl -o pages/my_page.html "http://example.com/page.html"

# 方案 B：创建本地 HTML
cat > pages/my_page.html << 'EOF'
<!DOCTYPE html>
<html>
<head><title>My Page</title></head>
<body>
  <h1>Hello World</h1>
  <script>
    window.englishShow = {
      show: function(data) {
        console.log('Received:', data);
        // 处理展示逻辑
      }
    };
  </script>
</body>
</html>
EOF

# 启动
./start_cloud_daemon.sh --prewarm "$(pwd)/pages/my_page.html"
echo -n "SHOW" | socat - UDP4-DATAGRAM:127.0.0.1:24021

# 推送内容
./push_cmd.sh "Hello" "你好"
```

### 2. 添加新的游戏

**基于手势游戏框架：**
```bash
cd /mine/Code/ROS/dice/手势识别/rps-kids-h5

# 1. 修改 Vue 组件
vim src/App.vue

# 2. 修改游戏逻辑
vim src/lib/game.js

# 3. 测试
npm run dev

# 4. 构建
npm run build

# 5. 部署
bash start-screen.sh
```

---

## 未来整合可能性

### 方案：统一框架

创建一个**通用的屏幕管理框架**，支持：

1. **内容推送模式**（当前云端展示）
   - UDP 接口
   - ROS 集成
   - 守护进程

2. **交互游戏模式**（当前手势游戏）
   - 摄像头支持
   - AI 推理
   - Vue 前端

3. **混合模式**
   - 支持推送 + 交互
   - 统一的生命周期管理
   - 统一的表情系统对接

**技术栈：**
- 统一使用 Vue 3 框架
- Electron 主进程管理生命周期
- 插件化的摄像头/推送模块
- 统一的 MPV 控制接口

---

## 总结

| 选择标准 | 推荐系统 |
|---------|---------|
| 需要推送外部内容 | 云端展示 |
| 需要 ROS 集成 | 云端展示 |
| 需要摄像头交互 | 手势游戏 |
| 需要 AI 识别 | 手势游戏 |
| 需要快速原型 | 云端展示（静态 HTML） |
| 需要复杂 UI | 手势游戏（Vue 3） |
| 守护进程常驻 | 云端展示 |
| 临时启动使用 | 手势游戏 |

两个系统各有优势，根据具体需求选择使用！
