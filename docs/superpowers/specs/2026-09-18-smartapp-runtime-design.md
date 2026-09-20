# SmartApp Runtime 一期架构设计

- 状态：待实施
- 日期：2026-09-18
- 适用范围：RK3588 / Ubuntu 设备端 SmartApp Runtime 一期
- 依据：《智能应用-小游戏整体技术方案设计》

## 1. 背景

当前仓库包含三个独立的 Vue/Vite 手势识别与剪刀石头布应用，尚无 SmartApp Runtime、设备业务 Agent、Electron 渲染控制或统一应用包管理实现。本设计在仓库根目录新建独立 Python 项目 `smartapp-runtime/`，先实现可测试、可部署的 Runtime 核心及本地适配器。

真实设备 Agent、Electron Offscreen、FFmpeg/FIFO/MPV、ROS2 和 DRM/KMS 的完整代码不在当前仓库内。本期通过稳定端口隔离这些依赖，不虚构硬件集成已完成。

## 2. 目标

1. 提供单活动应用、单活动会话的可预测生命周期。
2. 支持 Python-only、Web-only 和 Web+Python Hybrid 三种应用模式。
3. 实现下载、SHA-256 校验、安全解压、原子安装与版本指针。
4. 实现 backend 进程组监管、JSON Lines 通信、超时退出和子进程回收。
5. 实现常驻的 `127.0.0.1:18080` 静态服务。
6. 实现 Agent 本地 IPC、会话/序列号校验和应用消息路由。
7. 实现幂等停止、失败清理、启动恢复与结构化日志。
8. 通过临时目录、随机端口和 Fake Renderer 建立不依赖硬件的自动测试门禁。

## 3. 非目标

- 不支持多应用并发或同一应用多实例。
- 不新建 AbilityProxy 或重新抽象现有 ROS2/SDK 原子能力。
- 不解释题目、分数、动作等应用业务字段。
- 不允许应用在运行时安装 Python 依赖。
- 不支持云端直接下发任意脚本并执行。
- 不实现增量更新、断点续传、远程调试或复杂版本 GC。
- 不将 SHA-256 当作发布者身份认证。一期只运行可信第一方应用包。
- 不在当前仓库中伪造真实 Electron、ROS2 或屏幕链路的集成验收。

## 4. 技术决策

### 4.1 形态

采用“Python 异步模块化单体 + Ports/Adapters”：

- Runtime 本身是一个守护进程。
- backend 是 Runtime 监管的子进程，不是 Runtime 微服务。
- 所有会话状态由单个 `RuntimeCoordinator` 拥有。
- 核心层只依赖端口，网络、文件系统、子进程、渲染器和时钟均由适配器实现。

不采用多服务拆分，因为一期的单应用约束要求下，额外进程边界会增加部署、故障恢复和状态一致性成本。

### 4.2 运行时与依赖

- 最低支持 Python 3.8，`pyproject.toml` 声明 `requires-python = ">=3.8"`。
- Runtime 核心仅使用 Python 3.8 可用的标准库 API，降低离线设备部署风险。
- 配置文件使用 TOML：Python 3.11 及以上使用标准库 `tomllib`；Python 3.8–3.10 使用条件依赖 `tomli>=2.0.1,<3; python_version < "3.11"`。
- 除 Python 3.8–3.10 的 `tomli` 兼容依赖外，生产 Runtime 不引入 Web 框架、ORM 或异步框架。离线部署包必须同时携带 `tomli` wheel。
- 并发协调使用 `asyncio`；阻塞文件、tar 和 HTTPS 操作通过 `loop.run_in_executor` 隔离。
- 测试使用标准库 `unittest`，不要求全局安装测试工具。

### 4.3 Python 3.8 兼容约束

- 不使用 `match/case`、`dataclass(slots=True)`、`enum.StrEnum`、`asyncio.TaskGroup`、`asyncio.timeout`、`Path.is_relative_to`、`str.removeprefix` 等 3.8 之后引入的 API。
- 可选类型和容器注解使用 `typing.Optional/List/Dict/Tuple`，不依赖 PEP 604 联合类型或内置泛型的新语义。
- 字符串枚举使用 `class X(str, Enum)`。
- 异步超时使用 `asyncio.wait_for`，异步任务组由显式任务集合和取消/等待逻辑管理。
- 路径边界判定通过 `Path.resolve()` 后调用 `relative_to()` 并捕获 `ValueError`，不做字符串前缀判断。
- 兼容性测试矩阵覆盖 Python 3.8、3.9、3.10、3.11、3.12、3.13 和 3.14；至少 Python 3.8 与当前最新支持版本必须通过完整测试，才能声称兼容。

## 5. 项目结构

```text
smartapp-runtime/
├── pyproject.toml
├── README.md
├── config/
│   ├── runtime.example.toml
│   └── smartapp-runtime.service
├── src/smartapp_runtime/
│   ├── __init__.py
│   ├── __main__.py
│   ├── config.py
│   ├── domain/
│   │   ├── commands.py
│   │   ├── errors.py
│   │   ├── manifest.py
│   │   ├── models.py
│   │   └── state.py
│   ├── application/
│   │   ├── coordinator.py
│   │   ├── lifecycle.py
│   │   └── router.py
│   ├── ports/
│   │   ├── agent.py
│   │   ├── downloader.py
│   │   ├── process.py
│   │   ├── renderer.py
│   │   ├── repository.py
│   │   └── static_server.py
│   ├── infrastructure/
│   │   ├── ipc/
│   │   ├── packages/
│   │   ├── persistence/
│   │   ├── processes/
│   │   └── web/
│   ├── adapters/
│   │   ├── command_renderer.py
│   │   └── fake_renderer.py
│   └── bootstrap.py
├── examples/
│   ├── agent_client.py
│   └── manifests/
└── tests/
    ├── unit/
    ├── integration/
    ├── e2e/
    └── fixtures/
```

依赖方向只允许 `infrastructure/adapters -> ports/application -> domain`，核心层不得反向导入基础设施层。

## 6. 运行目录

生产默认根目录为 `/data/smartapp`：

```text
/data/smartapp/
├── apps/<appId>/<version>/
│   ├── manifest.json
│   ├── .smartapp-install.json
│   ├── web/
│   └── backend/
├── apps/<appId>/current -> <version>
├── apps/<appId>/previous -> <version>
├── current_web -> apps/<appId>/<version>/web
├── downloads/
├── tmp/
├── state/runtime-state.json
└── logs/
```

所有路径均可通过配置覆盖。测试必须使用临时目录，不访问 `/data/smartapp`。

已安装的 `<appId>/<version>` 为不可变目录。如同版本已存在且 `.smartapp-install.json` 中的 SHA-256 不同，Runtime 必须拒绝覆盖。

## 7. 领域模型

### 7.1 标识符

`appId`、`version`、`sessionId` 必须先校验再用于路径或日志。

- `appId`：`^[a-z][a-z0-9_]{0,63}$`
- `version`：`^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$`
- `sessionId`：`^[0-9A-Za-z][0-9A-Za-z._:-]{0,127}$`
- `requestId`：长度 1–128，只允许可打印 ASCII 字符

### 7.2 命令

- `StartApp`：`requestId/sessionId/appId/version/packageUrl/packageSize/sha256/initData`
- `StopApp`：`requestId/sessionId/reason`
- `CloudData`：`requestId/sessionId/seq/target/dataType/trigger/data`
- `GetStatus`：`requestId`

`reason` 只允许 `wake_word/cloud_stop/replace/runtime_error/operator`。

`target` 只允许 `auto/python/web/broadcast`。

`seq` 为 0 到 `2^63-1` 的整数，按会话和方向分别维护。一期 Agent -> Runtime 方向由 Runtime 去重；Runtime -> Agent 的云端 envelope 序列号由 Agent 补齐。

### 7.3 状态

```text
BOOT_RECOVERY
IDLE
PREPARING
DOWNLOADING
VERIFYING
INSTALLING
STARTING
RUNNING
STOPPING
CLEANING
```

状态变更只能由 `RuntimeCoordinator` 执行并持久化。

## 8. Manifest v1

```json
{
  "schemaVersion": 1,
  "appId": "rock_paper_scissors",
  "version": "1.2.0",
  "web": {
    "enabled": true,
    "entry": "index.html"
  },
  "backend": {
    "enabled": false,
    "entry": "main.py",
    "dynamicService": false
  },
  "routing": {
    "defaultTarget": "web"
  }
}
```

校验规则：

1. `schemaVersion` 必须等于 `1`。
2. `appId/version` 必须与 `StartApp` 一致。
3. `web.enabled` 与 `backend.enabled` 至少一个为 `true`。
4. Web-only 的 `defaultTarget` 必须为 `web`。
5. Python-only 的 `defaultTarget` 必须为 `python`。
6. Hybrid 的 `defaultTarget` 必须显式为 `web` 或 `python`。
7. `web.entry` 相对 `web/`，`backend.entry` 相对 `backend/`。
8. entry 必须是应用目录内的普通文件，不得包含绝对路径、`..` 或经由符号链接。
9. `backend.dynamicService=true` 时，Runtime 在启动前确认 `127.0.0.1:18081` 可用，并注入动态服务环境变量。backend 仍通过 `app_ready` 报告整体就绪。
10. 未知顶层字段和未知组件字段均拒绝，防止拼写错误被静默忽略。

## 9. Agent IPC 协议

### 9.1 传输

- Unix Domain Socket，默认 `/run/smartapp/runtime.sock`。
- Socket 默认权限 `0660`，运行用户和 Agent 通过同一系统组访问。
- UTF-8 JSON Lines，每行一条消息。
- 单条消息默认上限 1 MiB，可配置但不允许无上限。
- 同时只保留一个活动 Agent 连接；第二个连接被拒绝，不替换现有连接。
- Agent 断开不自动停止正在运行的应用。上行队列有界；溢出时拒绝新上行消息并记录错误。

### 9.2 输入示例

```json
{"requestId":"req-1","command":"start_app","sessionId":"abc123","appId":"rock_paper_scissors","version":"1.2.0","packageUrl":"https://cdn.example.com/rock_paper_scissors-1.2.0.tar.gz","packageSize":12345678,"sha256":"<64 hex chars>","initData":{"mode":"normal"}}
```

```json
{"requestId":"req-2","command":"cloud_data","sessionId":"abc123","seq":15,"target":"auto","dataType":"question","data":{"word":"Apple"}}
```

```json
{"requestId":"req-3","command":"stop_app","sessionId":"abc123","reason":"wake_word"}
```

### 9.3 输出示例

长操作的结果在操作完成后异步返回，`requestId` 用于关联：

```json
{"event":"command_result","requestId":"req-1","ok":true,"state":"RUNNING","sessionId":"abc123"}
```

```json
{"event":"command_result","requestId":"req-1","ok":false,"state":"IDLE","error":{"code":"HASH_MISMATCH","message":"package sha256 mismatch"}}
```

应用上行数据不伪造云端 envelope：

```json
{"event":"app_data","sessionId":"abc123","appId":"rock_paper_scissors","dataType":"game_result","data":{"result":"win"}}
```

Agent 负责添加 `deviceId/eventId/seq` 并转换为云端 `game_view_data`。

## 10. Runtime 与应用协议

### 10.1 backend

Runtime 通过 stdin 发送 JSON Lines：backend 只能通过 stdout 发送协议消息，普通日志必须写入 stderr。

Runtime -> backend：

```json
{"event":"runtime_init","sessionId":"abc123","data":{"mode":"normal"}}
{"event":"cloud_data","seq":15,"dataType":"question","data":{"word":"Apple"}}
{"event":"app_stop","reason":"wake_word"}
```

backend -> Runtime：

```json
{"event":"app_ready"}
{"event":"app_data","dataType":"game_result","data":{"result":"win"}}
```

约束：

- backend 必须在启动超时内输出一次 `app_ready`。
- 重复 `app_ready`、未知事件、非法 JSON 和超长行均记为协议违规。
- `app_data` 只在 `RUNNING` 状态接收。
- backend 非预期退出触发整个会话清理。

### 10.2 H5 Bridge

H5 使用与 backend 相同的逻辑消息结构。`RendererPort` 提供：

- `load(url, session)`
- `wait_ready(timeout)`
- `send(message)`
- `stop()`
- `restore_default()`
- H5 -> Runtime 消息回调

当前仓库提供 Fake Renderer 与命令行 Renderer 适配器。命令行适配器只使用预配置的 argv 数组，不经过 Shell；命令成功代表本地渲染控制器已接受操作。生产 Electron 适配器必须另外实现 Bridge ready 和 H5 双向消息，但不改变核心端口。

## 11. 并发模型

`RuntimeCoordinator` 是 Actor：

1. 所有外部命令和内部工作流结果进入同一个有界队列。
2. Actor 单消费者串行修改状态。
3. 下载、解压、启动等长操作使用受监管工作任务；工作任务只产生进度/完成/失败事件，不直接修改会话状态。
4. 每个启动工作流带有单调 `generation`；过期 generation 的迟到结果被忽略。
5. `StopApp` 或新 Session 可设置取消令牌，中断下载/启动工作流并进入统一清理。
6. 运行中收到新 Session 时，只保留最新的待启动请求。旧 Session 完全进入 `IDLE` 后才启动新 Session。

该模型保证可取消的长操作不会阻塞 stop，同时避免多任务直接竞争会话状态。

## 12. 状态机与幂等性

```text
BOOT_RECOVERY
  -> IDLE
  -> PREPARING
       -> STARTING                         缓存命中
       -> DOWNLOADING -> VERIFYING
          -> INSTALLING -> STARTING        缓存未命中
  -> RUNNING
  -> STOPPING
  -> CLEANING
  -> IDLE
```

规则：

- 相同 `sessionId/appId/version/sha256` 的重复 `StartApp` 共享原启动结果。
- 相同 `sessionId` 却携带不同应用、版本或哈希时返回 `SESSION_CONFLICT`。
- 不同 Session 的 `StartApp` 先停止旧应用，再启动最新请求。
- 重复 `StopApp` 安全成功。旧 Session 的 stop 返回“已无活动资源”，不影响新 Session。
- 旧 Session 的 `CloudData` 和 `seq <= lastSeenSeq` 的数据被拒绝并记录 debug 日志。
- `STARTING` 阶段默认最多缓存 128 条、合计 4 MiB 业务消息。`RUNNING` 后按接收顺序刷新。
- `STOPPING/CLEANING/IDLE` 期间不接受业务数据。

## 13. 包管理与安全

### 13.1 下载

- 生产 Downloader 只接受 HTTPS URL。
- 默认下载超时 60 秒，最多 3 次重定向；每次重定向的新 URL 仍必须为 HTTPS。
- 数据写入随机命名的 `.part`，边读取边统计字节和 SHA-256。
- 下载字节超过声明 `packageSize` 或配置的绝对上限时立即终止。
- 完成后必须同时满足精确大小和 SHA-256。

默认压缩包上限为 512 MiB。

### 13.2 解压

仅支持 gzip tar (`.tar.gz`)。不直接调用无约束的 `extractall`；解压器遍历每个成员并只创建目录或普通文件。

必须拒绝：

- 绝对路径、`..` 和规范化后逃逸 staging 的路径；
- 符号链接、硬链接、设备文件、FIFO 和 socket；
- 重复规范化路径；
- setuid/setgid 位；
- 超过配额的文件数、总解压大小、单文件大小或路径长度；
- 不是单一 `<appId>/` 根目录的包；
- manifest 与命令不一致的包。

默认解压后上限 1 GiB、文件数 10,000、单文件 512 MiB、相对路径 240 字符。新建目录使用 `0755`，普通文件使用 `0644`，不保留 tar 中的特权位。

### 13.3 安装

1. 解压到同一文件系统的随机 `.staging`。
2. 完成 manifest 和入口校验。
3. 写入 `.smartapp-install.json`，包含 SHA-256、包大小和安装时间。
4. `fsync` 文件与必要目录。
5. 使用同文件系统的原子 rename 移入最终版本目录。
6. 任何失败均删除本次 `.part/.staging`，不修改活动指针。

## 14. 启动、激活与回滚

1. 验证已安装版本的安装元数据和 manifest。
2. 保存 `current/current_web` 指针快照。
3. Web 应用先通过随机临时符号链接 + `os.replace` 原子切换 `current_web`。
4. backend 应用启动受监管进程，发送 `runtime_init`。
5. Web 应用调用 Renderer `load(http://127.0.0.1:18080/<entry>?v=<version>)`。
6. 等待所有启用组件 ready。默认启动超时 15 秒。
7. 所有组件 ready 后，先将旧 `current` 写入 `previous`，再将 `current` 原子切换到新版本。
8. 进入 `RUNNING`，刷新启动期缓冲消息。

启动失败时：

- 停止已启动组件；
- 恢复旧 `current_web`；
- 不修改 `current/previous`；
- 恢复日常显示；
- 返回结构化错误并进入 `IDLE`。

不得在新版本失败后静默启动旧版本。

## 15. Process Supervisor

### 15.1 启动

- 使用 `asyncio.create_subprocess_exec`，禁止 `shell=True`。
- argv 固定为配置的 Python 解释器、`-u` 和经验证的 backend entry。
- `cwd` 为应用版本根目录。
- `start_new_session=True`，使 backend 成为独立进程组领导者。
- 只传递配置允许的环境变量，并覆盖：
  - `SMARTAPP_SESSION_ID`
  - `SMARTAPP_APP_ID`
  - `SMARTAPP_VERSION`
  - `SMARTAPP_DYNAMIC_HOST=127.0.0.1`
  - `SMARTAPP_DYNAMIC_PORT=18081`
- ROS2 相关 `ROS_* / RMW_* / AMENT_* / COLCON_* / LD_LIBRARY_PATH / PYTHONPATH` 通过显式配置的 pass-through 列表传递，不隐式继承全部守护进程环境。

### 15.2 输出

- stdout 逐行读取，单行默认上限 1 MiB。
- stderr 逐行写入结构化应用日志，超长单行截断并标记。
- 连续 5 次协议违规默认终止应用，阈值可配置。
- 空行可忽略；stdout EOF 视为 backend 退出的一部分。

### 15.3 停止

```text
停止新数据投递
-> 发送 app_stop
-> 等待默认 3 秒优雅退出
-> SIGTERM 整个进程组
-> 等待默认 3 秒
-> SIGKILL 整个进程组
-> waitpid/等待 asyncio 子进程完成
```

所有停止步骤均幂等；进程已退出、进程组不存在和输入管道已关闭都不能阻止后续清理。

## 16. 静态服务

- 默认监听 `127.0.0.1:18080`，可为测试配置随机端口。
- 仅允许 GET 和 HEAD。
- `/healthz` 返回 Runtime 静态服务健康状态。
- 请求路径 URL decode 和规范化后必须位于当前 `current_web` 解析目录内。
- 禁止目录列表；目录请求只允许返回其 `index.html`。
- 返回 `Cache-Control: no-store` 和 `X-Content-Type-Options: nosniff`。
- 每个请求在开始时获取一次当前 web root 快照，避免软链接切换时混用两个版本的路径。
- Runtime 不反向代理 18081；需要动态服务的 H5 由 Bridge 或注入配置获取 `127.0.0.1:18081`。

## 17. Message Router

### 17.1 下行

1. 校验当前 Session、状态和 `seq`。
2. `auto` 解析为 manifest `routing.defaultTarget`。
3. `python` 要求 backend 已启用。
4. `web` 要求 web 已启用。
5. `broadcast` 要求至少一个端点，并向所有已启用端点投递。
6. `STARTING` 时进入有界缓冲；`RUNNING` 时直接顺序投递。

### 17.2 上行

- backend/H5 只能发送 `app_data`。
- Router 补齐当前 `sessionId/appId`，不信任应用自报的会话标识。
- Agent 未连接时进入有界上行队列；默认上限同样为 128 条/4 MiB。
- 队列溢出时丢弃最新消息，不删除已接受但尚未发送的较早消息，并记录 `UPSTREAM_QUEUE_FULL`。

Runtime 不解释 `data`。

## 18. 停止与清理

统一停止顺序：

1. 进入 `STOPPING`，禁止新数据投递。
2. 向 backend/H5 发送 `app_stop`。
3. 停止 backend，必要时 SIGTERM/SIGKILL 进程组。
4. 调用 Renderer `stop()`。
5. 释放会话占用的 18081、队列、管道和任务。
6. 调用 Renderer `restore_default()`。
7. 进入 `CLEANING`，清理会话临时文件。
8. 持久化空活动会话并进入 `IDLE`。

任一步失败只记录错误并继续后续清理。停止的成功条件是所有受控资源均已释放，而不是每个优雅步骤都成功。

## 19. 持久化与崩溃恢复

### 19.1 状态文件

`state/runtime-state.json` 使用临时文件 + `fsync` + `os.replace` 写入，包含：

- schema 版本；
- 当前 Runtime 状态；
- 活动 Session 的标识信息；
- 活动工作流 generation；
- backend 进程组领导 PID、Linux `/proc` 启动时间和期望命令特征；
- 当前指针快照；
- 最后错误。

不持久化业务消息队列。Runtime 崩溃后，Agent/云端根据业务协议重试。

### 19.2 启动恢复

1. 使用 `fcntl.flock` 获取单实例锁；获取失败则终止启动。
2. 清理超过恢复保护窗口的 `.part/.staging`。
3. 校验 `current/previous/current_web` 只指向合法安装目录；非法指针被移除并记录。
4. 仅当 PID、`/proc/<pid>/stat` 启动时间和 cmdline 安装路径特征全部匹配时，才向旧进程组发送 SIGTERM/SIGKILL；任一不匹配则不杀进程，避免 PID 复用误杀。
5. 调用 Renderer `restore_default()`。
6. 将活动会话清空并进入 `IDLE`。

非 Linux 环境只执行文件恢复，不根据持久化 PID 杀进程；该限制会记录为 warning。

## 20. 配置

`runtime.example.toml` 必须包含以下类别：

- `paths`：root、socket、log、python executable。
- `network`：18080/18081 主机与端口。生产主机必须为 loopback。
- `timeouts`：download、startup、graceful stop、SIGTERM、renderer。
- `limits`：包大小、解压大小、文件数、路径长度、消息大小、队列大小。
- `process`：环境变量 pass-through 列表、协议违规阈值。
- `renderer`：`fake` 或 `command`，以及命令行适配器的 argv 数组。
- `logging`：级别、JSON Lines 日志目标。

启动时完整校验配置；未知字段、非法端口、非 loopback 生产绑定、负数配额和不可写路径均导致启动失败。

## 21. 错误模型

对外错误包含稳定 `code`、可读 `message` 和可选安全 `details`。一期错误码：

- `VALIDATION_ERROR`
- `UNSUPPORTED_SCHEMA`
- `SESSION_CONFLICT`
- `SESSION_MISMATCH`
- `SEQ_OUT_OF_ORDER`
- `QUEUE_FULL`
- `DOWNLOAD_FAILED`
- `PACKAGE_TOO_LARGE`
- `SIZE_MISMATCH`
- `HASH_MISMATCH`
- `ARCHIVE_UNSAFE`
- `MANIFEST_INVALID`
- `INSTALL_CONFLICT`
- `PORT_IN_USE`
- `START_TIMEOUT`
- `BACKEND_EXITED`
- `BACKEND_PROTOCOL_ERROR`
- `RENDERER_FAILED`
- `UPSTREAM_QUEUE_FULL`
- `RECOVERY_FAILED`
- `INTERNAL_ERROR`

对外 message 不包含未经清理的包内容、完整环境变量、认证 URL 查询参数或任意 stderr 原文。

## 22. 可观测性

日志为 JSON Lines，统一字段：

```text
timestamp, level, event, state, requestId, sessionId, appId, version, errorCode
```

要求记录：

- Runtime 启动/停止与配置摘要；
- 每次状态迁移及原因；
- 下载开始/完成、缓存命中、校验和安装；
- backend/renderer 启动、ready、退出和强杀；
- 消息拒绝、过期 seq、队列溢出和协议违规；
- 恢复动作和不安全的指针/归档。

日志对 URL 用户信息与查询参数做脱敏。`initData/data` 默认不记录全文，只记录字节数和数据类型。

## 23. 测试策略

### 23.1 单元测试

- Python 3.8–3.14 的语法编译与完整用例矩阵，包括 `tomli/tomllib` 两条配置解析路径。
- 标识符、命令与 Manifest 校验。
- 合法/非法状态迁移。
- 重复 start、会话冲突、幂等 stop。
- seq 去重与 target 路由。
- 队列条数/字节上限。
- 错误码映射与日志脱敏。

### 23.2 集成测试

- HTTPS Downloader 的大小、哈希、超时和重定向校验。
- 安全 tar 解压：`../`、绝对路径、符号/硬链接、设备文件、重复路径、解压炸弹。
- 原子安装、缓存命中、同版本哈希冲突。
- `current/previous/current_web` 切换与启动失败回滚。
- backend ready、非法 JSON、非预期退出、子孙进程组回收。
- 18080 的 GET/HEAD、404、目录穿越、目录列表禁用与缓存头。
- Unix Socket 的连接限制、超长消息、命令相关和断线重连。

下载测试使用本地 TLS 测试服务器或 Fake Downloader；不访问公网。

### 23.3 本地端到端测试

- Python-only 应用进入 `RUNNING`、双向消息、正常停止。
- Web-only 应用切换 web root、Fake Renderer ready、静态资源可访问。
- Hybrid 的 `auto/python/web/broadcast` 路由。
- 新 Session 替换旧 Session，全程不存在两个同时 `RUNNING` 应用。
- 哈希错误拒绝安装，原指针不变。
- backend/renderer 启动失败回滚。
- 唤醒词 stop 后无受控子进程残留，并调用恢复日常显示。
- Runtime 中断后重启，清理临时文件与经验证的遗留进程。

### 23.4 实机验收

以下项目只能在 RK3588 / Ubuntu 目标设备验收：

- Electron Offscreen -> FFmpeg -> FIFO -> MPV DRM/KMS 显示链路；
- ROS2/SDK 原子能力和额头相机；
- 设备 Agent 真实信令长连接；
- 18081 动态视频/通信性能；
- 进程、内存、温度、长时稳定性和断电恢复。

## 24. 部署

- CLI 入口：`python -m smartapp_runtime --config <path>`。
- systemd 示例使用专用用户/组，`Restart=on-failure`。
- Runtime 启动用户需对数据目录、Socket 目录和渲染控制端口有最小必要权限。
- systemd 安全限制不能破坏 backend 的 ROS2、GPU、摄像头或 DRM 访问；示例单元只提供保守基线，设备团队在实机验证后收紧。
- README 明确区分本地 Fake Renderer 验证和生产硬件集成。

## 25. 一期交付物

1. `smartapp-runtime/` 独立 Python 包及 CLI。
2. 严格配置模型、Manifest v1 和错误码。
3. Runtime Coordinator、Actor 队列和单会话状态机。
4. HTTPS Downloader、安全解压和原子安装。
5. Process Supervisor 与 backend JSON Lines 协议。
6. 18080 静态服务。
7. Message Router 和 Unix Socket Agent 接口。
8. Renderer 端口、Fake Renderer 和命令行 Renderer 适配器。
9. 持久化、单实例锁和崩溃恢复。
10. Python-only、Web-only、Hybrid 测试 fixtures。
11. 单元、集成和本地端到端测试。
12. README、协议说明、配置示例和 systemd 示例。

## 26. 验收标准

1. Python-only、Web-only、Hybrid 均能从冷启动进入 `RUNNING`，且只启动 manifest 声明的组件。
2. 相同 `appId/version/sha256` 二次启动不产生下载请求。
3. 包大小或 SHA-256 错误、路径穿越、链接、特殊文件和超配额包均被拒绝，目标目录外零写入。
4. 安装或启动失败不修改 `current/previous`，并恢复旧 `current_web`。
5. `initData` 原样投递；`game_view_data` 的 Session、seq 和 target 路由符合本设计。
6. 旧 Session、重复/乱序 seq 不进入应用。
7. 重复 start 幂等，新 Session 不产生两个同时活动应用。
8. 唤醒词 stop、云端 stop 和运行故障均能完成全量清理，受控子进程无残留。
9. backend 或 renderer 非预期失败后会话返回 `IDLE`，失败版本不成为 current。
10. 18080 仅回环访问，不能目录穿越、查看目录列表或读取非当前 web root 文件。
11. 安装、启动、状态迁移、停止与失败均可通过结构化日志按 Session 关联。
12. 全部自动测试不依赖公网、ROS2、Electron、摄像头或 `/data/smartapp`。
13. Python 3.8 和当前最新支持版本的完整测试必须通过；3.9–3.13 的兼容矩阵不得出现版本特定失败。

## 27. 后续集成边界

本期完成后，真实设备集成仅需在已冻结端口上扩展：

- 设备 Agent：将 `robot_game_view/game_view_data/唤醒词` 规范化为 Unix Socket 命令，并将 `app_data` 封装回云端信令。
- Electron Renderer：实现 `RendererPort`、H5 Bridge ready、双向消息与显示链路故障回调。
- 应用包：将 `rps-kids-h5/dist` 按 Manifest v1 封装为首个 Web-only 应用。
- Hybrid 应用：将额头摄像头服务拆为应用 backend 或设备能力适配器，不合并进 Runtime 核心。

这些集成不得让核心层导入 ROS2、Electron 或当前 H5 项目代码。
