# SmartApp 封装指南

## 应用模式

本项目是 SmartApp Runtime v1 的 `Web+Python` Hybrid 应用：

- Runtime 在 `127.0.0.1:18080` 提供 `web/` 静态资源。
- Runtime 启动 `backend/main.py`，并通过环境变量注入动态服务地址。
- backend 在 `127.0.0.1:18081` 提供 `/api/status` 和 `/api/frame`。
- 实体屏加载、退出回收和默认表情恢复由 Runtime 的 Renderer Controller 负责。

应用包不包含 Electron、FFmpeg、MPV、Xvfb 或独立静态服务器。

实体屏实现位于同级 `smartapp-runtime/renderer/`，由 Runtime 的 `process` Renderer 统一调用。

## Manifest

[manifest.json](../manifest.json) 使用 Runtime 的严格 v1 schema：

```json
{
  "schemaVersion": 1,
  "appId": "rock_paper_scissors",
  "version": "0.1.9",
  "web": {"enabled": true, "entry": "index.html"},
  "backend": {"enabled": true, "entry": "main.py", "dynamicService": true},
  "routing": {"defaultTarget": "python"}
}
```

发布新版本时，必须同时修改 `manifest.json` 和 `package.json` 的版本号。组包脚本会拒绝版本不一致的构建。

## Backend 生命周期

Runtime 通过 stdin/stdout JSON Lines 管理 backend：

```text
Runtime -> backend  runtime_init
backend -> Runtime  app_ready
Runtime -> backend  cloud_data（运行期间，可选）
Runtime -> backend  app_stop
```

stdout 只输出协议消息；运行日志写入 stderr。收到 `app_stop`、SIGTERM 或 SIGINT 后，backend 会停止 HTTP 服务和摄像头线程。

`runtime_init.data.cameraSource` 用于选择摄像头，允许值为 `forehead` 和 `neck`，缺省为 `neck`。两者分别严格映射到 `/tmp/foo_jpeg` 和 `/tmp/neck_jpeg`，所选流不存在时不会回退到另一个相机。例如：

```json
{"event":"runtime_init","data":{"cameraSource":"forehead"}}
```

游戏结果由 H5 通过 Renderer Bridge 上报，不经过摄像头 backend：

```json
{"event":"app_data","dataType":"game_result","data":{"user":"fist","computer":"peace","outcome":"win","rounds":1}}
```

Renderer 将事件转发给 Runtime，Runtime 再补充权威的 `sessionId` 和 `appId` 并推送给 Agent。

Runtime 注入以下环境变量：

```text
SMARTAPP_SESSION_ID
SMARTAPP_APP_ID
SMARTAPP_VERSION
SMARTAPP_DYNAMIC_HOST
SMARTAPP_DYNAMIC_PORT
```

## 构建应用包

```bash
npm ci
npm test
npm run build:smartapp
```

输出示例：

```text
SmartApp 包：.../build/smartapp/rock_paper_scissors-0.1.9.tar.gz
packageSize=<归档字节数>
sha256=<64 位 SHA-256>
```

每次重新构建后都应使用脚本最新输出的大小和摘要，不要复用旧值。

## 云端启动参数

云端 `robot_game_view` 中的关键字段必须与实际归档一致：

```json
{
  "game": "start",
  "appid": "rock_paper_scissors",
  "version": "0.1.9",
  "sessionId": "<session-id>",
  "packageUrl": "<HTTPS tar.gz URL>",
  "packageSize": 0,
  "sha256": "<构建输出>",
  "data": {}
}
```

具体字段名应以设备业务 Agent 到 Runtime 的适配协议为准；Runtime 原生命令使用 `appId`。

## 目标机要求

- SmartApp Runtime 已启动，并配置真实 Renderer Controller。
- `/usr/bin/python3` 是 CPython 3.8.10，已安装 OpenCV Python 绑定；其他推理依赖已在归档的 `backend/vendor/`。
- OpenCV 构建包含 GStreamer 支持。
- 领结 JPEG 共享流位于 `/tmp/neck_jpeg`；额头 JPEG 共享流位于 `/tmp/foo_jpeg`。
- 额头模式启动前需调用 `/video_gst/get_video_service`，以 `{resolution: jpeg}` 开启额头 JPEG 流。
- `18080` 和 `18081` 回环端口未被其他进程占用。

## 已有 Electron 云端展示框架的狗端试运行

狗端已有 `/root/electron/push_cmd.sh` 时，可直接用它加载本机 URL，无需覆盖现有 Electron 进程。先把归档解压到 `rps-kids-h5/build/run/`，从仓库目录启动：

```bash
cd /home/unitree/magic-gesture-paradise/rps-kids-h5
mkdir -p build/run
tar -xzf build/smartapp/rock_paper_scissors-0.1.9.tar.gz -C build/run
python3 scripts/run-dog-standalone.py --camera-source forehead
```

狗屏加载 `/root/electron/push_cmd.sh http://127.0.0.1:18080/`，PC 在同一网络打开 `http://<狗IP>:18080/?debug=1`。服务将 `/api/` 同源转发给本机 Python 推理端口 18081，浏览器不加载 MediaPipe 模型。结束展示可执行 `/root/electron/push_cmd.sh EXIT`；停止服务则结束 `run-dog-standalone.py` 进程。该模式复用狗现有的 Electron 显示通道，不取代正式 SmartApp Runtime 的会话与 Agent 协议。

## 本地开发与正式部署边界

`start-local.sh` 和 `start-https.sh` 只用于浏览器本地开发，使用访问设备自己的摄像头。正式设备部署只分发 SmartApp tar.gz，不运行这两个脚本。

## 0.1.8 Python 推理变更

模型由 backend/inference.py 使用原生 MediaPipe CPU 加载，不再在 Electron Worker 中加载 WASM。`/api/status` 仅在真实帧完成推理后 ready；`/api/recognition?preview=0` 返回最新序号、帧年龄、宽高、关键点与分类。前端去重并拒绝过期帧。调试时 `preview=1` 附带同一帧 JPEG 的 Base64。`/api/frame` 保留兼容用途。

包内模型位于 backend/models/gesture_recognizer.task。0.1.8 包内的 `backend/vendor/` 自带 CPython 3.8 / Linux ARM64 的 MediaPipe 0.10.9 和精简依赖；系统 OpenCV 必须保留 GStreamer 支持，详见项目 README。构建时可设置 `SMARTAPP_WHEELHOUSE=/path/to/wheels` 完全离线使用预下载的轮子。PC 另外提供 server.py 与 POST /api/infer。SmartApp 的 runtime_init、app_ready、app_stop 和 H5 game_result 上报格式保持不变。构建脚本支持 macOS/Linux；目标狗端的导入、模型推理和真实摄像头仍需实机验收。
