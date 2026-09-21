# 快速开始指南

## 🚀 启动服务

```bash
cd /mine/Code/ROS/dice/手势识别/rps-kids-h5
./start-server.sh
```

## 🎮 访问游戏

### 方式 1：服务端摄像头（推荐）

**适用场景**：远程访问、使用机器狗摄像头

```
http://192.168.123.99:5174/?camera=server
```

**特点**：
- ✅ 无需浏览器摄像头权限
- ✅ 使用服务器端摄像头（机器狗或USB）
- ✅ 支持远程访问
- ✅ 按识别节奏刷新，避免重复请求同一帧

### 方式 2：浏览器本地摄像头

**适用场景**：本机使用、有摄像头的设备

```
https://localhost:5174/
```

**特点**：
- 需要 HTTPS 或 localhost
- 使用访问设备的摄像头
- 需要浏览器授权

### 方式 3：机器狗实体屏

在机器狗 Ubuntu 设备执行：

```bash
cd /home/unitree/rps-kids-h5
npm run build
bash start-screen.sh
```

要求设备已运行液晶 MPV，并存在 `/tmp/mpv-socket`。首次运行缺少 FFmpeg、Xvfb 或 socat 时，启动器会自动通过 apt 安装。页面输出为 480×800，渲染窗口为 800×480，启动器通过设备 MPV IPC 接管画面，使用与 `/mine/Code/ROS/dice/electron` 相同的 `Xvfb → Electron → FFmpeg transpose=2 → NUT FIFO → MPV` 链路。

检查显示链路：

```bash
test -S /tmp/mpv-socket && echo 'MPV IPC OK'
command -v Xvfb ffmpeg socat
```

按 `Ctrl-C` 停止 H5，脚本会尝试调用上级 `electron/restore_expression.sh` 恢复默认表情。

## 🧪 测试页面

### 摄像头测试（优化版）

```
http://192.168.123.99:5174/camera-test-v2.html
```

显示实时 FPS 和流畅的摄像头预览。

## 🔧 调试模式

添加 `?debug=1` 参数显示调试信息：

```
http://192.168.123.99:5174/?camera=server&debug=1
```

显示：
- 摄像头画面和骨架
- 手势识别状态
- 性能指标
- 可调参数

## 📊 摄像头优先级

服务端自动选择：

1. **机器狗额头相机**（如果 `/tmp/foo_jpeg` 存在）
2. **USB 摄像头** `/dev/video0`

## ⚙️ 参数说明

| 参数 | 说明 | 示例 |
|------|------|------|
| `camera=server` | 启用服务端摄像头 | `?camera=server` |
| `debug=1` | 显示调试窗口 | `?debug=1` |
| `preview=win` | 预览动画（不启动摄像头） | `?preview=win` |

可组合使用：`?camera=server&debug=1`

## 🎯 游戏玩法

1. **握拳上下摇动** - 触发回合开始
2. **摆出手势** - 剪刀✌️、石头✊、布🖐️
3. **停稳片刻** - 系统自动识别并亮牌
4. **查看结果** - 约2.2秒后自动进入下一轮

## 🔍 故障排查

### 问题：页面显示"摄像头需要帮个忙"

**原因**：使用浏览器摄像头模式但未添加 `?camera=server` 参数

**解决**：在 URL 末尾添加 `?camera=server`

### 问题：API 未响应

**原因**：server.py 未启动

**解决**：
```bash
python3 server.py
```

### 问题：摄像头不更新

**原因**：旧版本代码使用了固定延迟刷新

**解决**：确保使用最新构建的版本（包含按识别节奏取帧）

### 问题：FPS 过低

**检查**：
1. 网络延迟（局域网应该 < 10ms）
2. 服务器 CPU 负载
3. 摄像头实际帧率

## 📱 快捷键

- **D** - 显示/隐藏调试窗口
- **F** - 全屏
- **R** - 重新连接摄像头

## 🔄 更新代码后

```bash
cd /mine/Code/ROS/dice/手势识别/rps-kids-h5
npm run build
pkill -f "server.py"
./start-server.sh
```

## 📈 性能指标

- **帧率**：最高约 15 FPS；推理较慢或网络延迟高时会更低
- **延迟**：< 100ms（局域网）
- **分辨率**：机器狗共享流 960x540；USB 摄像头由设备决定
- **带宽**：约 500-800 KB/s

## 🔗 相关文件

- `server.py` - Python 后端服务器
- `src/App.vue` - 主游戏逻辑
- `SERVER_CAMERA.md` - 详细技术文档
- `README.md` - 项目概述
