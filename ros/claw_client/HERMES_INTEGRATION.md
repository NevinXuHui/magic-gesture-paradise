# Hermes Gateway 集成方案

## 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                        SpeechCore 层                              │
│  (/homi_speech/sigc_data_service + /homi_speech/sigc_event_topic) │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ↓
┌─────────────────────────────────────────────────────────────────┐
│                    HermesBridge 节点                              │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  功能：SpeechCore ↔ Hermes Gateway 双向通信              │  │
│  │  - 接收 SpeechCore 事件 (domain=HERMES)                 │  │
│  │  - 发送消息到 Hermes Gateway (XiaoliChannel API)        │  │
│  │  - 长轮询/SSE 接收 Gateway 回复                          │  │
│  │  - 转换消息格式 (SpeechCore ↔ XiaoliChannel)           │  │
│  └──────────────────────────────────────────────────────────┘  │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ↓ HTTP/SSE
┌─────────────────────────────────────────────────────────────────┐
│                    Hermes Gateway                                 │
│  (XiaoliChannel 平台适配器)                                       │
│  - 长轮询: GET /getupdates                                       │
│  - SSE 流: GET /stream                                           │
│  - 发送消息: POST /sendmessage                                   │
│  - 编辑消息: POST /editmessage                                   │
└─────────────────────────────────────────────────────────────────┘
```

## 核心组件

### 1. HermesBridge 节点

**继承**: `SpeechCoreClientNode`

**职责**:
- 接收 SpeechCore 下发的 `app_msg_request` (domain=HERMES)
- 将用户消息转换为 XiaoliChannel 格式并发送到 Hermes Gateway
- 通过长轮询或 SSE 接收 Gateway 的回复
- 将 Gateway 回复转换为 `device_msg_response` 并发回 SpeechCore

### 2. XiaoliChannel 通信协议

#### 2.1 长轮询模式 (默认)

```python
# 拉取更新
GET /getupdates?app_id={app_id}&offset={last_update_id}&timeout=35

响应:
{
    "ret": 0,
    "updates": [
        {
            "update_id": 123,
            "message": {
                "message_id": "msg-456",
                "chat_id": "chat-001",
                "user_id": "user-001",
                "text": "Hello, AI!",
                "timestamp": 1717563600000
            }
        }
    ]
}
```

#### 2.2 SSE 流式模式

```python
# 建立 SSE 连接
GET /stream?app_id={app_id}&offset={last_update_id}

服务器推送:
data: {"update_id": 123, "message": {...}}

data: {"update_id": 124, "message": {...}}
```

#### 2.3 发送消息

```python
# 发送文本消息
POST /sendmessage
{
    "app_id": "xxx",
    "chat_id": "chat-001",
    "text": "AI 回复内容",
    "context_token": "ctx-789"  # 会话上下文（可选）
}

响应:
{
    "ret": 0,
    "message_id": "msg-999",
    "context_token": "ctx-800"  # 新的上下文 token
}
```

#### 2.4 编辑消息（流式输出）

```python
# 编辑已发送的消息
POST /editmessage
{
    "app_id": "xxx",
    "chat_id": "chat-001",
    "message_id": "msg-999",
    "text": "更新后的内容"
}
```

## 消息格式映射

### SpeechCore → Hermes

**输入** (SpeechCore `app_msg_request`):
```json
{
    "deviceId": "device-123",
    "domain": "HERMES",
    "event": "app_msg_request",
    "eventId": "evt-456",
    "body": {
        "text": "查询天气",
        "signal": "/clear"  // 可选控制信号
    }
}
```

**输出** (XiaoliChannel 消息):
```json
{
    "app_id": "hermes-app-id",
    "chat_id": "chat-{deviceId}",
    "user_id": "user-{deviceId}",
    "text": "查询天气"
}
```

### Hermes → SpeechCore

**输入** (XiaoliChannel update):
```json
{
    "update_id": 100,
    "message": {
        "message_id": "msg-789",
        "chat_id": "chat-device-123",
        "user_id": "hermes-gateway",
        "text": "今天多云，温度 24°C",
        "timestamp": 1717563600000
    }
}
```

**输出** (SpeechCore `device_msg_response`):
```json
{
    "deviceId": "device-123",
    "domain": "HERMES",
    "event": "device_msg_response",
    "eventId": "evt-456",
    "body": {
        "role": "assistant",
        "content": [{"type": "text", "text": "今天多云，温度 24°C"}],
        "stopReason": "stop"
    }
}
```

## 实现细节

### 消息去重

使用 `message_id` 和内容指纹防止重复处理：

```python
self._processed_message_ids = set()  # 已处理的消息 ID
self._content_fingerprints = {}      # 内容指纹缓存
```

### Context Token 管理

维护每个设备的会话上下文：

```python
self._context_tokens = {}  # {device_id: context_token}
```

发送消息时携带 `context_token`，收到回复后更新本地缓存。

### 流式输出支持

1. 首次发送消息，获取 `message_id`
2. 后续增量更新调用 `/editmessage`
3. 最终消息带 `stopReason: "stop"`

### 控制信号

- `/clear`: 清除上下文，生成新的 `user_id`
- `/stop`: 中断当前请求

## 配置参数

```yaml
# config/hermes_bridge.yaml
/hermes_bridge:
  ros__parameters:
    # Hermes Gateway 地址
    hermes_url: 'http://localhost:8800'
    
    # XiaoliChannel 认证
    app_id: 'hermes-app-id'
    app_secret: 'hermes-app-secret'
    
    # 通信模式
    use_sse: false              # true=SSE, false=长轮询
    poll_timeout_sec: 35        # 长轮询超时
    poll_interval_sec: 1        # 轮询间隔（无更新时）
    
    # 消息处理
    message_timeout_sec: 60     # 消息处理超时
    enable_streaming: true      # 是否启用流式编辑
    
    # 日志
    console_log_level: 'INFO'
    file_log_level: 'DEBUG'
```

## 与 OpenClawBridge 的对比

| 特性 | OpenClawBridge | HermesBridge |
|------|----------------|--------------|
| **通信协议** | Webhook + SSE | 长轮询/SSE |
| **认证方式** | HMAC 签名 | App ID/Secret |
| **消息拉取** | 被动推送 | 主动轮询 |
| **会话管理** | user_id | context_token |
| **流式输出** | SSE 实时推送 | 编辑消息 |
| **错误重试** | 自动重连 | 指数退避 |
| **平台特性** | 简化协议 | 标准 IM 协议 |

## 部署流程

### 1. 启动 Hermes Gateway

```bash
cd /mine/Code/hermes-agent

# 配置环境变量
export XIAOLICHANNEL_APP_ID=hermes-app-id
export XIAOLICHANNEL_APP_SECRET=hermes-app-secret

# 启动 Gateway
hermes gateway run
```

### 2. 配置 claw_client

```bash
cd /mine/Code/ROS/unitree-debug/xiaoli_application_ros2

# 编辑配置
vim src/claw_client/config/hermes_bridge.yaml

# 构建
colcon build --packages-select claw_client
```

### 3. 启动 ROS2 节点

```bash
# 启动完整系统（包含 hermes_bridge）
ros2 launch launch_package unitree_ctrl_robdog.py

# 或单独启动 hermes_bridge
ros2 run claw_client hermes_bridge
```

## 测试验证

### 单元测试

```bash
# 使用 Hermes 模拟服务器
python /mine/Code/hermes-agent/tests/gateway/xiaolichannel_mock_server.py &

# 运行 ROS2 节点
ros2 run claw_client hermes_bridge

# 通过 Web UI 发送测试消息
# http://localhost:8800
```

### 集成测试

```bash
# 1. 启动 Hermes Gateway
hermes gateway run &

# 2. 启动 SpeechCore 和 ROS2 节点
ros2 launch launch_package unitree_ctrl_robdog.py

# 3. 发送测试消息到 SpeechCore
ros2 topic pub /homi_speech/sigc_event_topic homi_speech_interface/msg/SIGCEvent \
  "{event: '{\"domain\":\"HERMES\",\"event\":\"app_msg_request\",\"deviceId\":\"test-device\",\"body\":{\"text\":\"Hello\"}}'}"

# 4. 观察日志
ros2 topic echo /homi_speech/sigc_event_topic
```

## 故障排查

### Gateway 无法连接

**检查**:
```bash
# 测试 Gateway 连通性
curl http://localhost:8800/getconfig?app_id=hermes-app-id

# 检查环境变量
echo $XIAOLICHANNEL_APP_ID
echo $XIAOLICHANNEL_APP_SECRET
```

### 消息未收到

**检查**:
```bash
# 查看 HermesBridge 日志
ros2 node info /hermes_bridge

# 查看长轮询状态
# 日志中应有 "poll start" 和 "poll complete"
```

### 流式输出不工作

**检查**:
```bash
# 确认配置
ros2 param get /hermes_bridge enable_streaming

# 查看编辑消息调用
# 日志中应有 "edit_message: message_id=xxx"
```

## 未来优化

1. **双模式切换**: 自动检测 SSE 可用性，回退到长轮询
2. **负载均衡**: 支持多个 Hermes Gateway 实例
3. **离线队列**: 网络断开时缓存消息
4. **性能监控**: 统计延迟、成功率等指标
5. **动态配置**: 支持运行时修改参数

## 参考资料

- [Hermes Gateway 架构](file:///mine/Code/hermes-agent/ARCHITECTURE.md)
- [XiaoliChannel 测试指南](file:///mine/Code/hermes-agent/tests/gateway/XIAOLICHANNEL_TESTING.md)
- [OpenClawBridge 实现](file:///mine/Code/ROS/unitree-debug/xiaoli_application_ros2/src/claw_client/claw_client/openclaw_bridge.py)
