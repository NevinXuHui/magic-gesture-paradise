# 手势识别游戏 - 使用说明

## ⚠️ 重要提示

**当前系统限制**：Electron Offscreen + Xvfb 环境无法支持 MediaPipe 的 WebGL 需求。
游戏会显示错误：`Cannot read properties of undefined (reading 'activeTexture')`

## 📋 基本使用

### 启动游戏

```bash
cd /mine/Code/ROS/dice/手势识别/rps-kids-h5
./start-screen.sh
```

**启动后**：
- ✅ 摄像头数据正常获取
- ✅ Electron 渲染 30 FPS
- ✅ MPV 显示画面
- ❌ MediaPipe 手势识别失败（WebGL 错误）

### 停止游戏

**方法 1：使用停止脚本（推荐）**
```bash
./stop-screen.sh
```

**方法 2：按 Ctrl-C**
- 在运行 `start-screen.sh` 的终端按 Ctrl-C
- 脚本会自动恢复默认表情

**方法 3：手动停止**
```bash
pkill -f "screen-renderer|Xvfb :99"
./restore_expression.sh
```

## 🔧 可用的替代方案

### 方案 1：摄像头测试页面（可用但会闪烁）

显示实时摄像头画面，无手势识别：

```bash
cd /mine/Code/ROS/dice/手势识别/rps-kids-h5
SCREEN_URL="http://127.0.0.1:5174/stable-camera-test.html" ./start-screen.sh
```

### 方案 2：外部设备运行

在平板/电脑上运行游戏：

```bash
# 机器狗运行服务端
python3 server.py

# 外部设备浏览器打开
http://<机器狗IP>:5174/?camera=server
```

### 方案 3：使用云端展示系统

显示静态信息页面：

```bash
cd /mine/Code/ROS/dice/electron
./start_cloud_daemon.sh
./push_cmd.sh "手势游戏" "请在外部设备体验"
```

## 📁 相关文件

- `start-screen.sh` - 启动脚本
- `stop-screen.sh` - 停止脚本
- `server.py` - 服务端摄像头
- `screen-renderer.js` - Electron 渲染器
- `dist/` - 构建产物（需先 `npm run build`）

## 🐛 故障排查

### 问题：屏幕显示 activeTexture 错误

**原因**：MediaPipe 需要 WebGL，但 Xvfb + Electron Offscreen 环境不支持

**解决**：使用外部设备或安装 Chromium 浏览器

### 问题：Ctrl-C 无法退出

**解决**：
```bash
# 使用停止脚本
./stop-screen.sh

# 或强制终止
pkill -9 -f "screen-renderer"
./restore_expression.sh
```

### 问题：画面闪烁

**原因**：Electron Offscreen 渲染性能限制

**解决**：使用外部设备或真实浏览器

## 📊 性能指标

正常运行时的日志输出：

```
[screen-renderer] STATS paint=30.0fps write=29.1fps skip=0.0/s rss=212MB
```

- `paint` - Electron 渲染帧率（目标 30 FPS）
- `write` - FFmpeg 写入帧率
- `skip` - 跳帧次数
- `rss` - 内存使用

## 🎯 完整工作流程

```bash
# 1. 确保已构建
cd /mine/Code/ROS/dice/手势识别/rps-kids-h5
npm run build

# 2. 启动游戏
./start-screen.sh

# 3. 观察日志确认启动成功
# 应该看到：H5 已交给设备液晶：480x800，30 FPS

# 4. 使用 Ctrl-C 或停止脚本退出
./stop-screen.sh

# 5. 确认表情已恢复
# 屏幕应显示默认表情视频
```

## 📚 更多文档

- `SETUP_SCREEN.md` - 完整部署指南
- `SOLUTION.md` - 问题分析和解决方案
- `README.md` - 项目说明
- `COMPARISON.md` - 与云端展示系统的对比

## ✅ 推荐方案总结

| 场景 | 推荐方案 |
|------|---------|
| 演示展示 | 使用云端展示系统显示静态页面 |
| 实际游戏 | 外部平板/电脑浏览器运行 |
| 测试摄像头 | 使用 stable-camera-test.html |
| 开发调试 | 电脑浏览器 + 机器狗服务端 |
