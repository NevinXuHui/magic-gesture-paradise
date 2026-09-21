# 手势识别游戏 - 问题分析与解决方案

## 核心问题

1. **WebGL `activeTexture` 错误**
   - MediaPipe 需要 WebGL 支持
   - Electron Offscreen + Xvfb 环境中 WebGL 初始化失败
   
2. **渲染性能低**
   - Electron 在虚拟显示中 FPS 只有 2-11
   - 目标 30 FPS 无法达到
   - 导致画面闪烁

3. **磁盘空间不足**
   - 根分区使用 100%
   - 影响系统稳定性

## 为什么 Electron Offscreen 不工作

```
Xvfb (虚拟X) → Electron Offscreen → MediaPipe (需要WebGL)
                     ↓
              WebGL 上下文创建失败
                     ↓
         activeTexture undefined 错误
```

虚拟显示环境中：
- 没有真实GPU
- WebGL 软件渲染性能差
- MediaPipe 无法初始化

## 可行的解决方案

### 方案 1：使用浏览器模式（推荐）

不使用 Electron Offscreen，直接用浏览器：

```bash
# 停止表情
ros2 service call /expression/config homi_speech_interface/srv/ExpressionConfig \
  "{action: set, expression_enabled: 'false'}"

# 启动 Chromium 全屏
DISPLAY=:0 chromium-browser --kiosk --app=http://127.0.0.1:5174/?camera=server \
  --no-sandbox --disable-gpu-vsync
```

**优点**：
- 真实显示环境，WebGL 正常工作
- 性能好，60 FPS 无压力
- MediaPipe 能正常初始化

**缺点**：
- 需要停止表情系统
- 退出需要手动恢复

### 方案 2：预录制视频展示

不使用实时手势识别，改用预录制的演示视频：

```bash
cd /mine/Code/ROS/dice/electron
./start_cloud_daemon.sh --prewarm "$(pwd)/pages/gesture_demo.html"
./push_cmd.sh "演示" "手势识别游戏演示"
```

创建 `pages/gesture_demo.html`：
```html
<!DOCTYPE html>
<html>
<body style="margin:0;background:#000">
<video autoplay loop muted style="width:100%;height:100%;object-fit:contain">
  <source src="gesture_demo.mp4" type="video/mp4">
</video>
<div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
            color:white;font-size:48px;text-align:center;padding:20px;
            background:rgba(0,0,0,0.7);border-radius:15px">
  🎮 手势识别游戏<br>
  <span style="font-size:32px">请在电脑/平板上体验</span>
</div>
</body>
</html>
```

### 方案 3：简化版手势检测（不用 MediaPipe）

使用纯 JavaScript 检测（准确度降低，但能工作）：

- 使用 TensorFlow.js Lite（比 MediaPipe 轻量）
- 或使用简单的颜色/运动检测
- 牺牲准确度换取兼容性

### 方案 4：外部设备显示

在外部设备（平板/手机）上运行游戏：

```bash
# 机器狗只运行服务端摄像头
python3 server.py

# 在平板浏览器打开
http://机器狗IP:5174/?camera=server
```

## 临时解决方案（当前可用）

### 摄像头数据流测试

简单的摄像头显示（无游戏逻辑）：

```bash
cd /mine/Code/ROS/dice/手势识别/rps-kids-h5
SCREEN_URL="http://127.0.0.1:5174/stable-camera-test.html" ./start-screen.sh
```

这能显示摄像头画面但会闪烁，因为 Electron Offscreen 性能限制。

## 建议

**短期**：使用方案 2（预录制视频）展示概念

**中期**：使用方案 1（浏览器模式）在演示时运行

**长期**：考虑在外部平板设备运行游戏，机器狗只提供摄像头

## 清理磁盘空间

```bash
# 清理临时文件
sudo rm -rf /tmp/*
sudo apt clean
sudo journalctl --vacuum-time=1d

# 查看大文件
du -sh /* 2>/dev/null | sort -h | tail -10
```
