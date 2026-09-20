# 服务端摄像头支持

## 概述

rps-kids-h5 现在支持两种摄像头模式：

### 1. 浏览器本地摄像头（默认）
- 使用 `getUserMedia` API
- 需要 HTTPS 或 localhost
- 读取访问设备的摄像头

### 2. 服务端摄像头（新增）
- 通过 Python 后端桥接
- 支持 HTTP 远程访问
- 读取服务器端的摄像头

## 快速开始

### 启动服务端摄像头模式

```bash
cd /mine/Code/ROS/dice/手势识别/rps-kids-h5
./start-server.sh
```

### 访问地址

- **本机**：http://localhost:5174/?camera=server
- **局域网**：http://192.168.123.99:5174/?camera=server
- **测试页面**：http://192.168.123.99:5174/camera-test.html

## 摄像头优先级

服务端自动按以下顺序选择摄像头：

1. **机器狗额头相机**：如果 `/tmp/foo_jpeg` 共享流存在
2. **USB 摄像头**：`/dev/video0`

## API 端点

### GET /api/status

返回摄像头状态：

```json
{
  "ready": true,
  "camera": "USB摄像头",
  "source": "server",
  "bridge_frame_age_ms": 25,
  "error": ""
}
```

### GET /api/frame

返回当前视频帧（JPEG 格式；机器狗共享流为 960x540，USB 摄像头保持设备实际比例）

## URL 参数

### `?camera=server`

启用服务端摄像头模式。

示例：
- `http://localhost:5174/?camera=server`
- `http://192.168.123.99:5174/?camera=server&debug=1`

### `?debug=1`

显示调试窗口和骨架绘制。

## 技术实现

### 后端 (server.py)

- 使用 OpenCV 读取摄像头
- GStreamer 支持机器狗共享流
- ThreadingHTTPServer 提供并发支持
- 自动重连机制

### 前端 (App.vue)

- 检测 `?camera=server` URL 参数
- 通过 `fetch` 获取 JPEG，转换为 `ImageBitmap` 后直接送入 Worker
- 原始帧先绘制到调试 canvas，再进行识别，避免黑屏依赖推理返回
- 通过 `/api/frame` 按识别节奏刷新图像（上限约 15fps）
- MediaPipe 推理仍在浏览器 Worker 执行

## 故障排查

### 错误：服务端摄像头 API 未响应

**原因**：server.py 未启动

**解决**：
```bash
python3 server.py
```

### 错误：USB 摄像头打开失败

**原因**：
1. 摄像头未连接
2. 权限不足
3. 被其他程序占用

**解决**：
```bash
# 检查设备
ls -la /dev/video*

# 检查占用
fuser /dev/video0

# 测试摄像头
v4l2-ctl --device=/dev/video0 --all
```

### 错误：页面显示"请从本机 localhost 或 HTTPS 打开网页"

**原因**：未添加 `?camera=server` 参数，前端使用了默认的浏览器摄像头模式

**解决**：在 URL 末尾添加 `?camera=server`

## 性能特点

- **帧率**：约 15fps（可调整 `capture()` 中的 `66ms` 间隔）
- **分辨率**：机器狗共享流 960x540；USB 摄像头由设备决定
- **延迟**：取决于网络和服务器性能
- **带宽**：约 500-800 KB/s

## 与 dog-h5-gesture 的区别

| 特性 | dog-h5-gesture | rps-kids-h5（新） |
|------|----------------|-------------------|
| 服务端摄像头 | 专用模式 | 可选模式（URL参数） |
| 浏览器摄像头 | 不支持 | 默认支持 |
| 切换方式 | 无 | `?camera=server` 参数 |
| 部署复杂度 | 需要 Python 后端 | 可选后端，前端独立运行 |

## 开发说明

### 修改帧率

编辑 `src/App.vue` 的 `capture()` 函数：

```javascript
// capture() 中的取帧间隔由 now-lastDispatch<66 控制，66ms 约等于 15fps。
```

### 修改分辨率

编辑 `server.py` 的 `capture()` 函数：

```python
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)   # 修改宽度
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)  # 修改高度
```

## 构建

```bash
npm ci
npm run build
```

构建产物在 `dist/` 目录，包含所有前端修改。
