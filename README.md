# smartapp-runtime

本工作区以 [`smartapp-runtime`](smartapp-runtime/) 为核心架构，用于在设备上安装、启动、切换和停止 SmartApp。当前可运行两个 800 x 480 H5 游戏：

- [`english`](english/)：Web-only 英语展示游戏，SmartApp `0.2.0`。
- [`rps-kids-h5`](rps-kids-h5/)：Web + Python backend 的儿童剪刀石头布游戏，SmartApp `0.1.6`。

Runtime 一次只运行一个应用会话。Agent 通过 Unix Socket JSONL 协议控制应用；Runtime 统一管理应用包、Web 入口、可选 Python backend、实体屏 Renderer、状态恢复和消息路由。

## 架构

```text
Agent / 测试客户端
        |
        | Unix Socket + JSONL
        v
+--------------------------------------------------+
| SmartApp Runtime                                 |
| 包下载与校验 | 会话生命周期 | 状态恢复 | 消息路由 |
| Web 服务 :18080 | Renderer | 可选 backend :18081  |
+--------------------------------------------------+
              |                         |
              | 选择一个 SmartApp       |
              v                         v
     english (Web-only)       rps-kids-h5 (Web + Python)
              |                         |
              +------------+------------+
                           v
                Electron 实体屏 / 局域网浏览器
```

`english` 只需要 Runtime 提供静态页面。`rps-kids-h5` 还会启动由 Runtime 管理的 Python backend，从设备摄像头共享流取帧；H5 通过同源 `/api/status` 和 `/api/frame` 访问，Runtime 再代理到本机 backend。

### 端口

| 端口 | 监听地址 | 用途 |
| --- | --- | --- |
| `18080` | `0.0.0.0` | 当前 SmartApp 的 Web 页面；RPS 运行时同时承载 `/api/*` 代理 |
| `18081` | `127.0.0.1` | 动态 Python backend，当前仅 RPS 使用 |
| `18443` | `127.0.0.1` | `run.sh` 为本地验证包启动的 HTTPS 下载服务 |

`18081` 不对局域网开放。远程浏览器始终只访问 `18080`，因此 RPS 不需要跨域请求。

## 项目结构

| 目录 | 定位 |
| --- | --- |
| [`smartapp-runtime`](smartapp-runtime/) | 核心运行时和部署入口 |
| [`english`](english/) | English H5 游戏及 SmartApp 组包脚本 |
| [`rps-kids-h5`](rps-kids-h5/) | RPS H5 游戏、MediaPipe 推理、摄像头 backend 及组包脚本 |
| [`dog-h5-gesture`](dog-h5-gesture/) | 独立的浏览器手势识别实验项目，不是 Runtime 验证应用 |
| [`dog-pc-gestrue`](dog-pc-gestrue/) | 历史桌面端手势实验项目，不是 Runtime 验证应用 |

## 环境要求

- Linux，目标环境为 Ubuntu / RK3588
- Python 3.8+
- Node.js 20.19+ 或 22.12+
- Chrome / Chromium，或 Runtime 自带的 Electron Renderer
- OpenCV Python 绑定，且目标机构建需支持 GStreamer
- 设备摄像头共享流：`/tmp/neck_jpeg`、`/tmp/foo_fhd` 或 `/tmp/neck_hd`
- 实体屏模式需要 Xvfb、Electron、FFmpeg、MPV socket 和对应的 ROS2 表情服务

## 快速启动

以下命令均从仓库根目录开始执行。

### 1. 构建两个 SmartApp

English：

```bash
cd english
npm test
npm run build:smartapp
```

RPS：

```bash
cd rps-kids-h5
npm ci
npm test
npm run build:smartapp
```

对应产物为：

```text
english/build/smartapp/cloud_show_display-0.2.0.tar.gz
rps-kids-h5/build/smartapp/rock_paper_scissors-0.1.6.tar.gz
```

组包脚本会输出 `packageSize` 和 `sha256`。包内容发生变化后，必须同步更新 `smartapp-runtime/config/validation/<app>/start-app.json` 中的版本、大小和摘要。

### 2. 启动 Runtime

```bash
cd smartapp-runtime
./run.sh
```

`run.sh` 会检查 Python 环境和 Runtime 配置，启动本地验证包服务，然后在前台运行 Runtime。这个终端需要保持打开。

### 3. 启动一个 H5 游戏

另开一个终端，二选一执行：

```bash
cd smartapp-runtime
./test_client.sh english start
# 或
./test_client.sh rps start
```

启动另一个应用时，应先停止当前应用。成功时返回类似：

```json
{"event":"command_result","requestId":"deploy-start-1","ok":true,"state":"RUNNING","sessionId":"<session-id>"}
```

### 4. 打开页面

设备本机可以打开：

```text
http://127.0.0.1:18080/
```

局域网其他设备使用 Runtime 主机的实际 IP：

```text
http://<Runtime主机IP>:18080/
```

当前验证环境示例：

```text
http://192.168.123.99:18080/
```

## 应用控制

所有验证命令都在 `smartapp-runtime` 目录执行：

```bash
./test_client.sh status             # 查询 Runtime 和当前会话
./test_client.sh rps start          # 启动 RPS
./test_client.sh rps stop           # 停止 RPS
./test_client.sh rps restart        # 重启 RPS
./test_client.sh rps cloud-data     # 向 RPS 发送云端数据
./test_client.sh rps listen         # 持续订阅每局游戏结果
./test_client.sh english start      # 启动 English SmartApp
./test_client.sh english stop       # 停止 English SmartApp
./test_client.sh english restart    # 重启 English SmartApp
./test_client.sh english cloud-data # 向 English 发送云端数据
```

Runtime 同时只允许一个 Agent 客户端。执行 `listen` 时，不能在另一个终端同时发送 `status` 或 `stop`。

RPS 每局结束会上报：

```json
{"event":"app_data","sessionId":"rps-deploy-validation","appId":"rock_paper_scissors","dataType":"game_result","data":{"user":"fist","computer":"peace","outcome":"win","rounds":1}}
```

`user` 和 `computer` 的值为 `fist`、`peace`、`palm`；`outcome` 为玩家视角的 `win`、`lose`、`draw`。

## RPS 交互与调试

- 握拳上下摇动锁定主手并开始回合。
- 停止摇动后保持剪刀、石头或布，系统在手势稳定后结算。
- 直接按 `D` 显示或隐藏摄像头调试面板。
- `Ctrl+D` 是浏览器的收藏夹快捷键，页面也会主动忽略带 `Ctrl`、`Alt` 或 `Meta` 的按键。
- 也可通过 `http://<Runtime主机IP>:18080/?debug=1` 直接显示调试面板。
- 按 `R` 重连摄像头，按 `F` 切换全屏。

调试面板会显示摄像头画面、手部关节、推理耗时、FPS、当前手势、运动状态和识别参数，并支持导出轨迹日志。

## 本地前端开发

如果只开发 RPS 页面，不启动 SmartApp Runtime：

```bash
cd rps-kids-h5
npm ci
npm run dev -- --port 5174
```

打开 `http://localhost:5174/`。该模式由浏览器直接读取当前访问设备的摄像头，需要浏览器授权。

局域网中通过普通 HTTP 访问开发服务时，`getUserMedia` 会受浏览器安全上下文限制。需要浏览器本地摄像头时应使用 `localhost` 或可信 HTTPS；需要使用 Runtime 主机的摄像头时，应按上文启动 SmartApp 模式。

## SmartApp 包格式

应用包为 gzip tar 归档，且必须有唯一顶层 `<appId>/` 目录：

```text
<appId>/
├── manifest.json
├── web/
│   └── <web.entry>
└── backend/
    └── <backend.entry>
```

Web-only 应用可以省略 `backend/`，Python-only 应用可以省略 `web/`。Manifest 的 `appId`、`version` 必须与启动命令一致。详细规则见 [SmartApp Runtime 文档](smartapp-runtime/README.md) 和 [RPS SmartApp 封装指南](rps-kids-h5/docs/SMARTAPP_PACKAGE.md)。

## 测试

RPS 单元测试：

```bash
cd rps-kids-h5
npm test
```

Runtime 全量检查：

```bash
cd smartapp-runtime
./scripts/check.sh
```

Runtime 配置检查：

```bash
cd smartapp-runtime
PYTHONPATH=src .venv/bin/python -m smartapp_runtime \
  --config config/validation/runtime.toml \
  --check-config
```

自动化测试可以验证协议、生命周期和识别逻辑，但不等于真实摄像头、光照、角度和 RK3588 长时间运行验收。

## 运行数据与日志

验证 Runtime 的数据默认位于：

```text
smartapp-runtime/runtime-data/smartapp-runtime/
├── apps/                 # 已安装的不可变应用版本
├── logs/runtime.jsonl  # Runtime JSONL 日志
├── run/runtime.sock    # Agent Unix Socket
└── state/               # 会话、指针和进程状态
```

查看最近日志：

```bash
tail -f smartapp-runtime/runtime-data/smartapp-runtime/logs/runtime.jsonl
```

## 常见问题

### 页面可打开，但摄像头 API 失败

```bash
curl -i http://127.0.0.1:18080/api/status
```

正常时应返回 `HTTP/1.1 200 OK` 和 JSON 状态。`502 Bad Gateway` 表示 RPS backend 未启动、已退出或 `18081` 不可达。

### 浏览器出现 CORS 或请求 `127.0.0.1:18081`

这通常表示仍在使用 RPS `0.1.5` 或旧的浏览器资源。确认当前会话为 `0.1.6`，重启应用并强制刷新页面：

```bash
./test_client.sh rps restart
./test_client.sh status
```

### `D` 无法打开调试面板

请直接按 `D`，不要按 `Ctrl+D`。当光标位于输入框、文本框或滑块时，页面也会忽略该快捷键。可用 `?debug=1` 作为回退入口。

### 控制台显示 `favicon.ico` 404

该请求不影响游戏功能。DuckDuckGo、OneTab Pro 等 content script 报错来自浏览器扩展，也不是项目代码报错。

### Runtime 无法启动

检查 `18080`、`18081`、`18443` 是否被占用，检查 `runtime.sock` 和单实例锁是否属于另一个活动 Runtime，然后查看 `runtime.jsonl`。不要在未确认进程状态时直接删除运行目录。

## 详细文档

- [RPS 游戏说明](rps-kids-h5/README.md)
- [RPS SmartApp 封装指南](rps-kids-h5/docs/SMARTAPP_PACKAGE.md)
- [SmartApp Runtime 设计、协议与运维](smartapp-runtime/README.md)
- [Renderer 实体屏集成](smartapp-runtime/renderer/README.md)
- [通用手势识别 H5](dog-h5-gesture/README.md)
- [桌面端手势实验项目](dog-pc-gestrue/README.md)
