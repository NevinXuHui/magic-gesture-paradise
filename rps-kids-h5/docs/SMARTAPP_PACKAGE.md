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
  "version": "0.1.6",
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
SmartApp 包：.../build/smartapp/rock_paper_scissors-0.1.6.tar.gz
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
  "version": "0.1.6",
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
- `/usr/bin/python3` 已安装 OpenCV Python 绑定。
- OpenCV 构建包含 GStreamer 支持。
- 摄像头共享流位于 `/tmp/neck_jpeg`、`/tmp/foo_fhd` 或 `/tmp/neck_hd`。
- `18080` 和 `18081` 回环端口未被其他进程占用。

## 本地开发与正式部署边界

`start-local.sh` 和 `start-https.sh` 只用于浏览器本地开发，使用访问设备自己的摄像头。正式设备部署只分发 SmartApp tar.gz，不运行这两个脚本。
