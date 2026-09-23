# Hermes Bridge 配置说明

## 配置文件位置

`config/hermes_bridge.yaml`

## 参数说明

### 基础连接配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `hermes_url` | string | `http://localhost:8800` | Hermes Gateway 服务地址 |
| `app_id` | string | `hermes-app-id` | XiaoliChannel 应用 ID（需与 Gateway 配置一致） |
| `app_secret` | string | `hermes-app-secret` | XiaoliChannel 应用密钥 |

### 通信模式配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `use_sse` | bool | `false` | 通信模式：`false`=长轮询（推荐），`true`=SSE 流式 |
| `poll_timeout_sec` | int | `35` | 长轮询超时时间（秒） |
| `poll_interval_sec` | int | `1` | 轮询失败后重试间隔（秒） |

**推荐配置：** 使用长轮询模式（`use_sse: false`），更稳定且易于调试。

### 消息处理配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `message_timeout_sec` | int | `60` | 消息处理超时时间（秒） |
| `enable_streaming` | bool | `true` | 是否启用流式编辑（编辑消息功能） |
| `forward_asr_to_channel` | bool | `true` | 是否将最终 ASR 解析结果推送给 XiaoliChannel / Hermes Gateway；`false` 时仅打日志不推送 |

### 访问控制配置

#### 策略配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `dm_policy` | string | `open` | DM（私聊）策略：`open`（开放）/ `allowlist`（白名单）/ `disabled`（禁用） |
| `group_policy` | string | `open` | 群聊策略：`open`（开放）/ `allowlist`（白名单）/ `disabled`（禁用） |
| `require_mention` | bool | `false` | 群聊是否需要 @提及才响应 |

#### 白名单配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `allow_from` | list[string] | `[]` | DM 白名单用户 ID 列表（当 `dm_policy: allowlist` 时生效） |
| `group_allow_from` | list[string] | `[]` | 群聊白名单用户 ID 列表（当 `group_policy: allowlist` 时生效） |
| `mention_patterns` | list[string] | `[]` | @提及正则模式列表，如 `['@bot', '@小狸']` |

**访问控制示例：**

```yaml
# 示例 1：仅允许特定用户的 DM
dm_policy: 'allowlist'
allow_from: ['user123', 'user456']
group_policy: 'disabled'

# 示例 2：群聊需要 @提及
dm_policy: 'open'
group_policy: 'open'
require_mention: true
mention_patterns: ['@小狸', '@bot']

# 示例 3：完全开放
dm_policy: 'open'
group_policy: 'open'
require_mention: false
```

### 错误处理配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_consecutive_failures` | int | `3` | 最大连续失败次数（达到后触发退避） |
| `retry_delay_sec` | int | `2` | 失败重试延迟（秒） |
| `backoff_delay_sec` | int | `30` | 连续失败退避延迟（秒） |

**错误处理机制：**
1. 连续失败次数 < `max_consecutive_failures`：等待 `retry_delay_sec` 后重试
2. 连续失败次数 ≥ `max_consecutive_failures`：等待 `backoff_delay_sec` 后重试（指数退避）
3. 自动检测过期会话错误，清除上下文令牌后重试

### 消息去重配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_processed_ids` | int | `1000` | 最大已处理消息 ID 缓存数量 |
| `max_fingerprints` | int | `500` | 最大内容指纹缓存数量 |
| `fingerprint_ttl_sec` | int | `300` | 内容指纹 TTL（秒，默认 5 分钟） |

**去重机制：**
- **消息 ID 去重：** 基于 `message_id` 防止重复处理同一条消息
- **内容指纹去重：** 基于 `SHA256(user_id + text)` 防止用户短时间内发送重复内容

### 文本批处理配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `batch_delay_sec` | float | `2.0` | 文本批处理延迟（秒），用于合并快速连续的消息 |

**批处理行为：**
- 同一 device_id 在 `batch_delay_sec` 时间内的多条消息会被合并
- 合并后的消息用换行符连接
- 适用场景：用户快速发送多条短消息，避免重复触发处理流程

### 日志配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `console_log_level` | string | `INFO` | 控制台日志级别：DEBUG / INFO / WARNING / ERROR |
| `file_log_level` | string | `DEBUG` | 文件日志级别 |
| `send_timeout_sec` | int | `5` | 发送消息超时时间（秒） |

## 完整配置示例

### 生产环境推荐配置

```yaml
/hermes_bridge:
  ros__parameters:
    # 连接配置
    hermes_url: 'http://hermes-gateway:8800'
    app_id: 'your-app-id'
    app_secret: 'your-app-secret'

    # 长轮询模式（生产推荐）
    use_sse: false
    poll_timeout_sec: 35
    poll_interval_sec: 1

    # 消息处理
    message_timeout_sec: 60
    enable_streaming: true

    # 访问控制：仅允许白名单用户
    dm_policy: 'allowlist'
    group_policy: 'disabled'
    require_mention: false
    allow_from: ['trusted-user-1', 'trusted-user-2']
    group_allow_from: []
    mention_patterns: []

    # 错误处理（默认值）
    max_consecutive_failures: 3
    retry_delay_sec: 2
    backoff_delay_sec: 30

    # 消息去重（默认值）
    max_processed_ids: 1000
    max_fingerprints: 500
    fingerprint_ttl_sec: 300

    # 文本批处理（默认值）
    batch_delay_sec: 2.0

    # 日志配置
    console_log_level: 'INFO'
    file_log_level: 'DEBUG'
    send_timeout_sec: 5
```

### 开发环境配置

```yaml
/hermes_bridge:
  ros__parameters:
    # 连接本地 Gateway
    hermes_url: 'http://localhost:8800'
    app_id: 'dev-app-id'
    app_secret: 'dev-app-secret'

    # 长轮询模式
    use_sse: false
    poll_timeout_sec: 35
    poll_interval_sec: 1

    # 消息处理
    message_timeout_sec: 60
    enable_streaming: true

    # 访问控制：完全开放
    dm_policy: 'open'
    group_policy: 'open'
    require_mention: false
    allow_from: []
    group_allow_from: []
    mention_patterns: []

    # 错误处理（更快重试）
    max_consecutive_failures: 2
    retry_delay_sec: 1
    backoff_delay_sec: 10

    # 消息去重（较小缓存）
    max_processed_ids: 500
    max_fingerprints: 200
    fingerprint_ttl_sec: 300

    # 文本批处理（更快响应）
    batch_delay_sec: 1.0

    # 日志配置（详细日志）
    console_log_level: 'DEBUG'
    file_log_level: 'DEBUG'
    send_timeout_sec: 5
```

### 游戏功能配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `game_udp_host` | string | `127.0.0.1` | cloud_daemon UDP 地址 |
| `game_udp_port` | int | `24021` | cloud_daemon UDP 端口 |
| `touch_status_topic` | string | `/touch_status` | 触摸话题；头部 pin 36/38/39 上升沿且游戏进行中则发 EXIT |

协议与 `push_cmd.sh` 一致，节点内直接发 UDP：

- start: `URL:<gameUrl>`，有 word 再发 `{"word","meaning"}`，否则发 `SHOW`
- update: `{"word","meaning"}`
- stop: `EXIT`

### 寻物控制接口

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `findobj_ctl_topic` | string | `/findobj_ctl` | 寻物控制话题（本体→算法功能开关，平台→算法） |
| `findobj_status_topic` | string | `/findobj/search_status` | 寻物状态话题（算法 → 平台） |

**`/findobj_ctl` 接口说明：**

统一的寻物控制接口，支持两种来源：
1. **平台下发**：`item_search` 事件 → hermes_bridge 发布到 `/findobj_ctl`
2. **本体控制**：本体直接发布到 `/findobj_ctl`

消息格式（JSON）：

```json
{
  "SN": "1222004229866666660002891",
  "prompt": "ball",
  "scene": "阳台",
  "status": "00"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `SN` | string | ✅ | 设备序列号 |
| `prompt` | string | 条件必填 | 寻找的物品（status=00 时必填） |
| `scene` | string | ❌ | 场景名称（可选） |
| `status` | string | ✅ | 控制状态码 |

**status 状态码：**

| status | 含义 | 场景说明 |
|--------|------|----------|
| `00` | 开启寻物 | 有 scene：先导航到记忆点再寻物<br>无 scene：触发普通寻物 |
| `01` | 结束寻物 | 终止当前寻物任务 |
| `02` | 暂停寻物 | 暂停当前寻物任务 |
| `03` | 继续寻物 | 继续已暂停的任务 |

**示例：**

```bash
# 普通寻物（无场景）
ros2 topic pub --once /findobj_ctl std_msgs/msg/String \
  "{data:'{\"SN\":\"1222004229866666660002891\",\"prompt\":\"ball\",\"status\":\"00\"}'}"

# 场景寻物（先导航）
ros2 topic pub --once /findobj_ctl std_msgs/msg/String \
  "{data:'{\"SN\":\"1222004229866666660002891\",\"prompt\":\"ball\",\"scene\":\"阳台\",\"status\":\"00\"}'}"

# 暂停寻物
ros2 topic pub --once /findobj_ctl std_msgs/msg/String \
  "{data:'{\"SN\":\"1222004229866666660002891\",\"status\":\"02\"}'}"

# 继续寻物
ros2 topic pub --once /findobj_ctl std_msgs/msg/String \
  "{data:'{\"SN\":\"1222004229866666660002891\",\"status\":\"03\"}'}"

# 结束寻物
ros2 topic pub --once /findobj_ctl std_msgs/msg/String \
  "{data:'{\"SN\":\"1222004229866666660002891\",\"status\":\"01\"}'}"
```

**说明：** `hermes_bridge` 节点订阅 `/findobj_ctl` 仅用于日志记录和监控，实际控制由算法侧直接订阅该话题处理。

### 自主配送演示接口

**事件：** `dog_auto_delivery_demo`

**域：** `DEVICE_ABILITY`

**平台下发格式：**

```json
{
  "deviceId": "dog_001",
  "domain": "DEVICE_ABILITY",
  "event": "dog_auto_delivery_demo",
  "eventId": "mission_20260819_001",
  "seq": "1724044800000",
  "body": {
    "targetTo": "",
    "targetGoods": "mimi",
    "sourcePlace": {
      "name": "起点",
      "x": 0.0,
      "y": 0.0,
      "angle": 0.0
    },
    "targetPlace": {
      "name": "目标点",
      "x": 5.0,
      "y": 3.0,
      "angle": 90.0
    },
    "targetPlace_en": ""
  }
}
```

**字段说明：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `targetTo` | string | ❌ | 目标收件人（可选） |
| `targetGoods` | string | ✅ | 配送货物名称（如 `mimi` / `floss`） |
| `sourcePlace` | object | ✅ | 起点坐标信息 |
| `targetPlace` | object | ✅ | 目标点坐标信息 |
| `targetPlace_en` | string | ❌ | 目标点英文名（可选） |

**坐标对象 (sourcePlace / targetPlace)：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | ✅ | 点位名称 |
| `x` | float | ✅ | X 坐标 |
| `y` | float | ✅ | Y 坐标 |
| `angle` | float | ✅ | 朝向角度（度） |

**处理流程：**

1. `hermes_bridge` 收到平台 `dog_auto_delivery_demo` 事件
2. 解析事件字段并打印日志
3. 将完整事件 JSON 透传到 `/dog_mission/platform_event` topic
4. 由 `robdog_control` 或其他导航模块订阅该 topic 并处理配送任务

**透传格式（std_msgs/String）：**

将完整的平台事件 JSON 作为字符串发布到 `/dog_mission/platform_event`。

**响应格式：**

```json
{
  "deviceId": "dog_001",
  "domain": "DEVICE_ABILITY",
  "event": "dog_auto_delivery_demo",
  "eventId": "mission_20260819_001",
  "seq": "1724044801000",
  "body": {
    "code": 0,
    "msg": "ok"
  }
}
```

（响应逻辑由实际处理模块负责回传）

**测试示例：**

```bash
# 模拟平台下发配送任务
ros2 topic pub --once /homi_speech/sigc_event_topic homi_speech_interface/msg/SIGCEvent \
  "{event: '{\"deviceId\":\"dog_001\",\"domain\":\"DEVICE_ABILITY\",\"event\":\"dog_auto_delivery_demo\",\"eventId\":\"mission_001\",\"seq\":\"1724044800000\",\"body\":{\"targetTo\":\"\",\"targetGoods\":\"mimi\",\"sourcePlace\":{\"name\":\"起点\",\"x\":0.0,\"y\":0.0,\"angle\":0.0},\"targetPlace\":{\"name\":\"目标点\",\"x\":5.0,\"y\":3.0,\"angle\":90.0},\"targetPlace_en\":\"\"}}'}"
```

**说明：** 

- `hermes_bridge` 将完整事件透传到 `/dog_mission/platform_event`
- 实际配送任务控制由订阅该 topic 的导航模块处理
- 平台响应需由导航模块通过 `/homi_speech/sigc_data_service` 回传

## 运行时修改参数

可以通过 ROS2 参数服务在运行时修改部分参数：

```bash
# 查看当前参数
ros2 param list /hermes_bridge

# 修改参数（示例：修改批处理延迟）
ros2 param set /hermes_bridge batch_delay_sec 1.5

# 修改日志级别
ros2 param set /hermes_bridge console_log_level DEBUG
```

**注意：** 某些参数（如 `hermes_url`、`app_id`）在节点启动时读取，运行时修改不会生效，需要重启节点。

## 故障排查

### 连接问题
- 检查 `hermes_url` 是否可访问
- 确认 `app_id` 和 `app_secret` 与 Gateway 配置一致
- 查看日志中的错误信息

### 消息不响应
- 检查 `dm_policy` 和 `group_policy` 配置
- 如果设置了 `require_mention: true`，确保消息包含 @提及
- 检查用户/聊天是否在白名单中（`allow_from` / `group_allow_from`）

### 频繁断线重连
- 增加 `poll_timeout_sec` 值
- 检查网络稳定性
- 调整 `max_consecutive_failures` 和 `backoff_delay_sec`

### 内存占用过高
- 减小 `max_processed_ids` 和 `max_fingerprints`
- 缩短 `fingerprint_ttl_sec`
