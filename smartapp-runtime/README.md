# SmartApp Runtime

SmartApp Runtime 是一个前台运行的单进程守护程序，负责 SmartApp 应用包的安全下载、不可变缓存、Web/后端组件生命周期、消息路由以及 Agent Unix 套接字协议。它一次只允许一个活动应用/会话。

## 环境与安装

- 兼容目标为 CPython 3.8–3.14；只有实际执行过本页完整检查的版本才计入当次交付的支持证据。
- Python 3.11 及以上使用标准库 `tomllib`。Python 3.8–3.10 需要项目已声明的条件依赖 `tomli>=2.0.1,<3`。
- 运行时依赖 POSIX 能力（Unix 套接字、文件锁和进程组）；PID 身份恢复仅在 Linux `/proc` 上生效。

从仓库根目录安装：

```bash
python3 -m venv "smartapp-runtime/.venv"
"smartapp-runtime/.venv/bin/python" -m pip install "./smartapp-runtime"
```

离线安装 Python 3.8–3.10 时，需提前将与目标平台匹配的 `tomli` wheel（以及构建环境需要的 `setuptools>=61` wheel）放入本地目录；本仓库不内置 wheel：

```bash
"smartapp-runtime/.venv/bin/python" -m pip install \
  --no-index --find-links "/path/to/offline-wheels" \
  "tomli>=2.0.1,<3" "./smartapp-runtime"
```

## 架构与生命周期

依赖方向为 `domain <- ports <- application <- infrastructure/adapters <- bootstrap`。领域层定义命令、会话、状态、Manifest 和稳定错误码；应用层用单 actor 协调器串行化状态变更，并用事务式生命周期执行启动/回滚；基础设施层实现安装、持久化、进程、HTTP 和 IPC。

启动顺序固定为：

1. 获取单实例锁；
2. 建立运行时目录并配置 JSONL 日志；
3. 恢复上次中断的状态；
4. 启动只监听回环地址的静态 Web 服务；
5. 启动 Runtime 协调器；
6. 启动 Agent Unix 套接字服务。

正常退出按相反的所有权顺序执行：Agent -> 协调器/应用资源 -> 静态服务 -> 实例锁。`SIGINT` 和 `SIGTERM` 都走该清理流程。

## 配置与运行

[`config/runtime.example.toml`](config/runtime.example.toml) 列出了当前接受的全部配置键和默认值，主动示例使用 `/tmp` 和 `fake` Renderer；文件末尾还有已注释的 `/data/smartapp` 与实体屏 `process` Renderer 生产模板。网络 host 必须是回环 IP 字面量，端口必须在 1–65535，不接受主机名、`0.0.0.0` 或端口 0。

只校验配置：

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m smartapp_runtime \
  --config "smartapp-runtime/config/runtime.example.toml" --check-config
```

`--check-config` 仅解析并验证 TOML，不构建运行时、不创建目录/锁/套接字，也不启动端口。

前台运行：

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m smartapp_runtime \
  --config "smartapp-runtime/config/runtime.example.toml"
```

Runtime 不后台化自身，生产环境由 systemd 监管。[`config/smartapp-runtime.service`](config/smartapp-runtime.service) 是保守模板：其 `/opt` 和 `/etc` 路径必须按实际安装位置修改。部署前创建专用非 root 账号，并让运行根目录归该账号所有：

```bash
sudo install -d -o smartapp -g smartapp -m 0750 "/data/smartapp"
sudo install -d -o root -g root -m 0755 "/etc/smartapp-runtime"
sudo install -o root -g root -m 0644 \
  "smartapp-runtime/config/smartapp-runtime.service" \
  "/etc/systemd/system/smartapp-runtime.service"
```

将独立的生产 TOML 放到 `/etc/smartapp-runtime/runtime.toml`。Unix 套接字在启动后强制为 `0660`；谁能连接由套接字父目录、专用用户/组和 systemd `UMask` 共同控制。

### 验证应用控制

先通过 `./run.sh` 启动验证 Runtime，再使用配置驱动的控制脚本管理应用：

```bash
./test_client.sh status
./test_client.sh start
./test_client.sh cloud-data
./test_client.sh stop
./test_client.sh restart
```

不传子命令时进入交互菜单。命令载荷来自 `config/validation/*.json`，所有请求均由 `examples/agent_client.py` 发送；修改会话、安装包或业务数据时应编辑对应 JSON 文件。`cloud-data.json` 的 `seq` 必须在同一会话中严格递增。

## Agent JSONL 协议

Agent 通过配置的 Unix 套接字连接。协议是严格 UTF-8 JSON Lines：每帧必须是单个 JSON 对象并以 `\n` 结束，拒绝重复键、非有限数字、未知字段和超限帧。仅允许一个活动客户端。`requestId` 是 1–128 位可打印 ASCII，用于请求/结果关联。

四种命令的完整形状如下（不允许额外字段）：

```json
{"requestId":"req-start-1","command":"start_app","sessionId":"session-1","appId":"demo_app","version":"1.0.0","packageUrl":"https://packages.example.invalid/demo_app-1.0.0.tar.gz","packageSize":12345,"sha256":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","initData":{"locale":"zh-CN"}}
{"requestId":"req-data-1","command":"cloud_data","sessionId":"session-1","seq":1,"target":"auto","dataType":"gesture","trigger":"cloud","data":{"name":"rock"}}
{"requestId":"req-stop-1","command":"stop_app","sessionId":"session-1","reason":"operator"}
{"requestId":"req-status-1","command":"get_status"}
```

`cloud_data.target` 可省略（默认 `auto`），也可为 `python`、`web` 或 `broadcast`；`dataType` 和 `trigger` 可省略。`seq` 必须在同一会话内严格递增。`stop_app.reason` 可为 `wake_word`、`cloud_stop`、`replace`、`runtime_error` 或 `operator`。安装包 URL 在实际下载时必须是 HTTPS，重定向的每一跳也必须是 HTTPS。

所有命令均返回：

```json
{"event":"command_result","requestId":"req-status-1","ok":true,"state":"IDLE"}
{"event":"command_result","requestId":"req-data-1","ok":false,"state":"RUNNING","sessionId":"session-1","error":{"code":"SEQ_OUT_OF_ORDER","message":"cloud data sequence is out of order"}}
```

应用上行事件与结果可交错：

```json
{"event":"app_data","sessionId":"session-1","appId":"demo_app","dataType":"gesture_result","data":{"accepted":true}}
```

`limits.max_message_bytes` 限制单条输入和输出 JSONL 帧；`max_queue_messages` 和 `max_queue_bytes` 同时限制 FIFO 消息数和编码字节。已被服务端接受的输出 FIFO 在客户端断开后保留；重连时先排空旧结果，再迁移当前会话的 Router 上行队列，最后才读取新命令。断开 Agent **不会**隐式停止当前应用。

标准库客户端示例会打印交错事件，直到遇到对应 `requestId` 的 `command_result`：

```bash
python3 "smartapp-runtime/examples/agent_client.py" \
  --socket "/tmp/smartapp-runtime/run/runtime.sock" \
  --json '{"requestId":"req-status-1","command":"get_status"}'
```

## 应用包、Manifest 和组件协议

包必须是 gzip 压缩 tar，并且每个成员都位于唯一顶层 `<appId>/` 下。只允许普通文件和目录，拒绝绝对路径、`..`、链接、特殊节点、重复路径、稀疏文件和超限内容。布局为：

```text
<appId>/
├── manifest.json
├── web/
│   └── <web.entry>
└── backend/
    └── <backend.entry>
```

Manifest v1 必须精确包含以下键；即使某组件关闭，其 `entry` 键仍必须存在：

```json
{
  "schemaVersion": 1,
  "appId": "demo_app",
  "version": "1.0.0",
  "web": {"enabled": true, "entry": "index.html"},
  "backend": {"enabled": true, "entry": "main.py", "dynamicService": false},
  "routing": {"defaultTarget": "python"}
}
```

- `schemaVersion` 必须恰为 `1`，`appId`/`version` 必须与启动命令一致。
- 至少启用 Web 或 backend 之一。Web-only 的 `defaultTarget` 必须是 `web`，Python-only 必须是 `python`，Hybrid 必须显式为二者之一。
- `entry` 是相对 POSIX 路径；启用组件的入口必须是包内无链接的普通文件。
- `dynamicService=true` 仅能用于已启用 backend。Runtime 启动前检查配置端口可用，并通过 `SMARTAPP_DYNAMIC_HOST`/`SMARTAPP_DYNAMIC_PORT` 传入后端；实际监听由应用后端负责。

后端 stdin/stdout 同样是 UTF-8 JSONL。Runtime 启动后先写入：

```json
{"event":"runtime_init","sessionId":"session-1","data":{"locale":"zh-CN"}}
```

后端 stdout 的第一条合法消息必须精确为 `{"event":"app_ready"}`；超过 `timeouts.startup` 未 READY 则启动失败。运行后 Runtime 下发 `cloud_data` 对象；后端上行必须精确为：

```json
{"event":"app_data","dataType":"gesture_result","data":{"accepted":true}}
```

停止时 Runtime 尝试发送 `{"event":"app_stop","reason":"operator"}`，然后按 graceful timeout -> SIGTERM -> SIGKILL 回收拥有的进程组。连续违规达到 `process.protocol_violation_limit` 时会终止会话。

Web/Hybrid 启动时，Runtime 设置当前 Web 指针，让回环 HTTP 服务提供入口，等待 Renderer ready，然后向 Renderer 发送包含 `sessionId/appId/version/data` 的 `runtime_init`。云端 `cloud_data` 按 Manifest 默认目标或显式 `target` 路由到 backend/Web；backend/Web 的 `app_data` 统一上行到 Agent。

## 缓存、恢复和日志

包下载同时校验宣言大小和 SHA-256，完整复验后才解压，最后原子发布到 `apps/<appId>/<version>`。该 app/version 目录不可变：只有安装元数据、Manifest、包大小和 SHA-256 均一致才命中缓存；同版本内容不同会以 `INSTALL_CONFLICT` 失败，不会覆盖旧内容。

当前/上一版指针和运行状态使用原子替换持久化。启动事务失败时回滚 Web 和版本指针，并按所有权清理 Renderer/后端。守护进程重启时会清理超过恢复窗口的 `.part`/`.staging` 事务残留，回滚指针，恢复默认画面，并且只在 Linux 上对 PID、`/proc` 启动时间和精确后端入口标记同时匹配的进程组发送信号。

日志是单行 JSON，可输出到 stderr 或配置文件。URL 中的用户信息、query 和 fragment 会被移除，控制字符被清理，任意结构化日志参数不会直接序列化；仅白名单关联字段 `requestId/sessionId/appId/state/errorCode` 可进入记录。仍不应将密钥、token 或完整业务 payload 主动写入日志消息。

## 稳定错误码与排查

| 错误码 | 含义/首要排查方向 |
|---|---|
| `VALIDATION_ERROR` | 命令、JSON、配置或路由参数不合法；核对精确 schema。 |
| `UNSUPPORTED_SCHEMA` | Manifest `schemaVersion` 不是 1。 |
| `SESSION_CONFLICT` | 会话正在启停或同 session 身份不同。 |
| `SESSION_MISMATCH` | `sessionId` 或组件 handle 不属于当前会话。 |
| `SEQ_OUT_OF_ORDER` | `cloud_data.seq` 没有严格递增。 |
| `QUEUE_FULL` | 下行或 Agent 输出 FIFO 达到消息/字节限制。 |
| `DOWNLOAD_FAILED` | HTTPS、超时、状态码或下载 I/O 失败。 |
| `PACKAGE_TOO_LARGE` | 声明或实际包超限。 |
| `SIZE_MISMATCH` | 实际下载大小与 `packageSize` 不同。 |
| `HASH_MISMATCH` | SHA-256 与命令不同。 |
| `ARCHIVE_UNSAFE` | tar 布局、节点类型或解压限额不安全。 |
| `MANIFEST_INVALID` | Manifest 键、类型、路由或入口文件不合法。 |
| `INSTALL_CONFLICT` | 已有 app/version 无法证明与请求内容相同。 |
| `PORT_IN_USE` | dynamic backend 配置端口不可用。 |
| `START_TIMEOUT` | backend READY 或整体启动超时。 |
| `BACKEND_EXITED` | 后端在启动/运行中意外退出。 |
| `BACKEND_PROTOCOL_ERROR` | 后端 stdout JSONL 连续违规达限。 |
| `RENDERER_FAILED` | Renderer 命令失败、超时或未 ready。 |
| `UPSTREAM_QUEUE_FULL` | 应用上行队列达限；检查 Agent 连接与消费速度。 |
| `RECOVERY_FAILED` | 开机恢复、指针/状态、进程或文件系统清理失败。 |
| `INTERNAL_ERROR` | 未分类的运行时内部失败；使用关联 ID 检查 JSONL 日志。 |

常见排查顺序：先运行 `--check-config`，再检查运行根目录和 socket 父目录归属，确认 18080/18081 未被占用，然后按 `requestId/sessionId/errorCode` 过滤日志。不要手工覆盖 `apps/<appId>/<version>`；需要更新时使用新 version，并保留上一版供指针回滚。

## 本地验证与目标机验收

本地开发配置的 `renderer.kind="fake"` 只模拟 ready/消息行为，不启动 Electron、Chromium、ROS2、摄像头或 GPU。`command` Renderer 保留给已有的短命令式显示控制器。生产实体屏使用 `renderer.kind="process"`：Runtime 持有一个严格 JSONL 子进程，完成 ready、下行消息、H5 `app_data` 上行、停止和进程组回收；[renderer/README.md](renderer/README.md) 提供 Xvfb、Electron Offscreen、FFmpeg、MPV 与 JS Bridge 的部署说明。

本地非破坏性检查：

```bash
"smartapp-runtime/scripts/check.sh"
```

可用 `PYTHON` 选择解释器：

```bash
PYTHON=python3.11 "smartapp-runtime/scripts/check.sh"
```

目标 RK3588 设备上发布前必须另行完成，且本仓库的自动化结果不等于下列验收通过：

- [ ] 在 RK3588 实机确认 Electron 加载、ready、Web 双向 Bridge 与前后台切换；
- [ ] ROS2 环境、`ROS_DOMAIN_ID`/`RMW_IMPLEMENTATION` 和节点发现；
- [ ] 摄像头驱动、设备权限、帧率与长时间稳定性；
- [ ] RK3588 CPU/GPU/NPU 资源、内存上限和热稳定性；
- [ ] 实际显示服务、GPU/相机设备节点和专用账号权限；
- [ ] 真实 HTTPS 包服务、证书/时钟、断网、重连和升级回滚；
- [ ] systemd 停机超时、异常重启、开机恢复和电源中断注入。

## 开发者检查

从仓库根目录执行完整 Python 套件：

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests" -p "test_*.py" -v
```

测试使用临时根目录、随机端口、离线内存包和 FakeRenderer，覆盖 Web-only、Python-only、Hybrid 以及故障/恢复路径；不访问公网，也不代替上述目标机验收。
