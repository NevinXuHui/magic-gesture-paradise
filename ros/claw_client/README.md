# SpeechCoreClientNode 接口文档

## 概述

`SpeechCoreClientNode` 是一个 ROS2 节点基类，提供与 SpeechCore 平台进行双向通信的接口。

- **包名**: `claw_client`
- **模块**: `claw_client.claw_client_node`
- **类名**: `SpeechCoreClientNode`
- **继承**: `rclpy.node.Node`

## 设计模式

- **发送**: 调用 `send(data)` 方法，通过 `SIGCData` 服务发送数据到 SpeechCore
- **接收**: 重写 `on_speech_event(event_json)` 方法处理来自 SpeechCore 的平台事件
- **扩展**: 继承此类并实现自定义业务逻辑

---

## 构造函数

### `__init__(node_name: str = 'claw_client_node')`

初始化节点并建立与 SpeechCore 的连接。

**参数**:
- `node_name` (str): 节点名称，默认为 `'claw_client_node'`

**功能**:
- 初始化日志系统
- 读取设备 ID
- 创建 SpeechCore 服务客户端
- 订阅 SpeechCore 事件话题
- 初始化鉴权信息缓存
- 可选的语音助手模式自动设置

---

## ROS2 参数

### 日志参数

| 参数名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `console_log_level` | string | `'INFO'` | 控制台日志级别 (DEBUG/INFO/WARNING/ERROR) |
| `file_log_level` | string | `'INFO'` | 文件日志级别 (DEBUG/INFO/WARNING/ERROR) |

### 通信参数

| 参数名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `send_timeout_sec` | int | `5` | 发送数据超时时间（秒） |

### 语音助手参数

| 参数名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `assistant_startup_mode` | int | `-1` | 语音助手启动模式<br>`-1`: 不设置（保持默认）<br>`0`: 关闭（唤醒后不触发对话）<br>`1`: 正常模式（ASR+NLP+TTS）<br>`2`: 仅ASR模式 |

---

## 公共接口

### 1. 发送数据

#### `send(data: str) -> int`

向 SpeechCore 发送 JSON 数据。

**参数**:
- `data` (str): 要发送的 JSON 字符串

**返回值**:
- `int`: 错误码
  - `0`: 成功
  - `-1`: 服务未就绪
  - `-2`: 超时
  - `-3`: 异常

**示例**:
```python
request = {
    "deviceId": self.device_id,
    "domain": "OPEN_CLAW",
    "event": "app_msg_request",
    "eventId": "msg_12345",
    "seq": "12345",
    "body": {"text": "你好"}
}
error_code = self.send(json.dumps(request, ensure_ascii=False))
if error_code == 0:
    print("发送成功")
```

---

### 2. 接收事件

#### `on_speech_event(event_json: str)`

处理来自 SpeechCore 的平台事件（需要子类重写）。

**参数**:
- `event_json` (str): SpeechCore 下发的事件 JSON 字符串

**说明**:
- 基类实现为空方法（`pass`）
- 子类应该重写此方法实现自定义事件处理逻辑
- 此方法会自动被 `_on_sigc_event` 回调调用

**示例**:
```python
class MyBridge(SpeechCoreClientNode):
    def on_speech_event(self, event_json: str):
        """处理 SpeechCore 事件"""
        try:
            data = json.loads(event_json)
            event = data.get("event", "")
            
            if event == "app_msg_request":
                # 处理应用消息请求
                self.handle_app_message(data)
        except json.JSONDecodeError:
            self.logger.warning(f"JSON 解析失败: {event_json[:100]}")
```

---

### 3. 服务鉴权接口

#### `request_service_credential_async(service: str, callback=None) -> bool`

异步请求服务鉴权信息（非阻塞）。

**参数**:
- `service` (str): 服务名称（如 `"openclaw"`）
- `callback` (callable, optional): 可选的回调函数，签名为 `callback(credential: Dict[str, Any])`

**返回值**:
- `bool`: `True` = 请求发送成功，`False` = 发送失败

**鉴权响应格式**:
```python
{
    "service": "openclaw",
    "credentialType": "api_key",
    "credential": "xxxxxxxxx",
    "expireAt": 1749703600000,  # 过期时间戳（毫秒）
    "endpoint": "https://llm.xxx.com/v1",
    "extra": {}
}
```

**特性**:
- 自动缓存鉴权信息
- 检查缓存有效期
- 支持异步回调通知

**示例**:
```python
def on_credential_received(credential):
    print(f"收到鉴权信息: {credential['endpoint']}")

# 发送异步请求
success = self.request_service_credential_async("openclaw", callback=on_credential_received)
if not success:
    print("请求发送失败")
```

---

#### `get_service_credential(service: str, timeout_sec: float = 5.0) -> Optional[Dict[str, Any]]`

获取服务鉴权信息（同步接口，阻塞等待响应）。

**参数**:
- `service` (str): 服务名称（如 `"openclaw"`）
- `timeout_sec` (float): 等待响应超时时间（秒），默认 5.0

**返回值**:
- `Dict[str, Any]`: 鉴权信息字典（格式同上）
- `None`: 获取失败

**特性**:
- 自动缓存鉴权信息
- 阻塞等待响应
- 超时返回 None

**示例**:
```python
credential = self.get_service_credential("openclaw", timeout_sec=10.0)
if credential:
    print(f"API Key: {credential['credential']}")
    print(f"Endpoint: {credential['endpoint']}")
else:
    print("获取鉴权信息失败")
```

---

## 公共属性

| 属性名 | 类型 | 说明 |
|--------|------|------|
| `device_id` | str | 设备 ID（从 `/etc/cmcc_robot/cmcc_dev.ini` 读取） |
| `logger` | Logger | 日志记录器 |
| `send_timeout_sec` | int | 发送超时时间（秒） |
| `platform_client` | Client | SpeechCore 服务客户端 |
| `sigc_event_sub` | Subscription | SpeechCore 事件订阅 |

---

## ROS2 接口

### 服务客户端

| 服务名 | 服务类型 | 说明 |
|--------|---------|------|
| `/homi_speech/sigc_data_service` | `homi_speech_interface/srv/SIGCData` | 向 SpeechCore 发送数据 |
| `/homi_speech/assistant_enable_service` | `homi_speech_interface/srv/AssistantQuiet` | 设置语音助手模式（可选） |

### 话题订阅

| 话题名 | 消息类型 | 说明 |
|--------|---------|------|
| `/homi_speech/sigc_event_topic` | `homi_speech_interface/msg/SIGCEvent` | 接收 SpeechCore 平台事件 |

---

## 内部机制

### 设备 ID 读取

从配置文件 `/etc/cmcc_robot/cmcc_dev.ini` 的 `[factory]` 节读取 `devSn` 字段：

```ini
[factory]
devSn=1222004229866666660004657
```

如果读取失败，返回默认值 `"unknown"`（避免开发环境无法启动）。

---

### 鉴权缓存机制

**缓存键格式**: `{service}:{device_id}`

**缓存逻辑**:
1. 检查缓存中是否存在有效的鉴权信息
2. 如果存在且未过期，直接返回缓存
3. 否则向平台请求新的鉴权信息
4. 收到响应后更新缓存

**过期检查**:
```python
if cached.get("expireAt", 0) > int(time.time() * 1000):
    # 缓存有效
    return cached
```

---

### 语音助手模式自动设置

当 `assistant_startup_mode >= 0` 时，节点启动时会自动：

1. 创建 `/homi_speech/assistant_enable_service` 服务客户端
2. 启动定时器（1秒间隔）等待服务就绪
3. 最多重试 10 次（10 秒）
4. 服务就绪后调用服务设置模式
5. 记录设置结果日志

**模式映射**:
- `mode=0` → `enable=False` (关闭)
- `mode=1` → `enable=True` (正常模式)
- `mode=2` → `enable=True` (仅ASR模式)

---

## 使用示例

### 基本用法

```python
from claw_client.claw_client_node import SpeechCoreClientNode
import rclpy
import json

class MyBridge(SpeechCoreClientNode):
    def __init__(self):
        super().__init__(node_name='my_bridge')
        
    def on_speech_event(self, event_json: str):
        """处理 SpeechCore 事件"""
        try:
            data = json.loads(event_json)
            self.logger.info(f"收到事件: {data.get('event')}")
        except json.JSONDecodeError:
            pass

def main():
    rclpy.init()
    node = MyBridge()
    rclpy.spin(node)
    rclpy.shutdown()

if __name__ == '__main__':
    main()
```

---

### 发送数据到 SpeechCore

```python
def send_message_to_platform(self, text: str):
    """发送文本消息到平台"""
    payload = {
        "deviceId": self.device_id,
        "domain": "OPEN_CLAW",
        "event": "device_msg_response",
        "eventId": f"msg_{int(time.time() * 1000)}",
        "seq": str(int(time.time() * 1000)),
        "body": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}]
        }
    }
    
    error_code = self.send(json.dumps(payload, ensure_ascii=False))
    if error_code == 0:
        self.logger.info("消息发送成功")
    else:
        self.logger.error(f"消息发送失败: error_code={error_code}")
```

---

### 使用鉴权信息

```python
def connect_to_llm_service(self):
    """连接到 LLM 服务"""
    # 同步获取鉴权信息
    credential = self.get_service_credential("openclaw", timeout_sec=10.0)
    
    if not credential:
        self.logger.error("获取鉴权信息失败")
        return
    
    api_key = credential['credential']
    endpoint = credential['endpoint']
    
    # 使用鉴权信息连接服务
    self.logger.info(f"连接到 LLM 服务: {endpoint}")
    # ... 初始化 OpenAI 客户端等
```

---

### 异步鉴权回调

```python
def __init__(self):
    super().__init__(node_name='my_bridge')
    
    # 异步请求鉴权信息
    self.request_service_credential_async(
        "openclaw", 
        callback=self.on_credential_ready
    )

def on_credential_ready(self, credential: Dict[str, Any]):
    """鉴权信息就绪回调"""
    self.logger.info("鉴权信息已就绪")
    self.api_key = credential['credential']
    self.endpoint = credential['endpoint']
    # 继续初始化其他组件...
```

---

## 配置示例

### YAML 配置文件示例

```yaml
/my_bridge:
  ros__parameters:
    # 日志配置
    console_log_level: 'INFO'
    file_log_level: 'DEBUG'
    
    # 通信配置
    send_timeout_sec: 10
    
    # 语音助手模式
    assistant_startup_mode: 1  # 正常模式
```

### 运行时参数覆盖

```bash
ros2 run claw_client my_bridge --ros-args \
  -p console_log_level:=DEBUG \
  -p assistant_startup_mode:=0
```

### 手动设置语音助手模式（Shell）

```bash
# 设置为仅ASR模式
ros2 service call /homi_speech/assistant_enable_service \
  homi_speech_interface/srv/AssistantQuiet \
  "{mode: 2}"

# 设置为正常模式
ros2 service call /homi_speech/assistant_enable_service \
  homi_speech_interface/srv/AssistantQuiet \
  "{mode: 1}"

# 关闭语音助手
ros2 service call /homi_speech/assistant_enable_service \
  homi_speech_interface/srv/AssistantQuiet \
  "{mode: 0}"
```

---

## 错误处理

### 发送失败处理

```python
error_code = self.send(data)

if error_code == -1:
    self.logger.error("SpeechCore 服务未就绪，请检查 homi_speech 节点是否运行")
elif error_code == -2:
    self.logger.error("发送超时，请检查网络连接")
elif error_code == -3:
    self.logger.error("发送异常，请查看详细日志")
elif error_code == 0:
    self.logger.info("发送成功")
```

### 鉴权失败处理

```python
credential = self.get_service_credential("openclaw", timeout_sec=5.0)

if credential is None:
    self.logger.error("获取鉴权信息失败，可能原因:")
    self.logger.error("  1. SpeechCore 服务未运行")
    self.logger.error("  2. 平台鉴权服务异常")
    self.logger.error("  3. 网络连接问题")
    return
```

---

## 日志输出特性

### 详细数据打印

节点会详细打印所有发送和接收的数据，便于调试和排查问题。

#### 1. 发送数据日志格式

```
================================================================================
📤 [发送数据到 SpeechCore]
完整数据: {"deviceId":"xxx","domain":"OPEN_CLAW",...}
格式化数据:
{
  "deviceId": "xxx",
  "domain": "OPEN_CLAW",
  "event": "device_msg_response",
  ...
}
================================================================================
✅ 发送完成, error_code=0
```

#### 2. 接收数据日志格式

```
================================================================================
📥 [收到 SpeechCore 事件]
完整数据: {"event":"credential_get","eventId":"xxx",...}
格式化数据:
{
  "event": "credential_get",
  "eventId": "xxx",
  "body": {
    "service": "openclaw",
    "credential": "xxxxxxxxx",
    ...
  }
}
================================================================================
```

#### 3. 语音助手模式设置日志

**发送请求：**
```
================================================================================
🎤 [设置语音助手模式 - 发送请求]
服务: /homi_speech/assistant_enable_service
模式: 仅ASR模式 (mode=2)
发送数据: {mode: 2}
================================================================================
```

**接收响应：**
```
================================================================================
🎤 [语音助手模式设置 - 接收响应]
服务: /homi_speech/assistant_enable_service
接收数据: {error_code: 0}
状态: ✅ 设置成功
================================================================================
```

### 日志级别控制

通过参数 `console_log_level` 和 `file_log_level` 控制日志输出级别：

- `DEBUG`: 显示所有调试信息
- `INFO`: 显示正常运行信息（默认）
- `WARNING`: 仅显示警告信息
- `ERROR`: 仅显示错误信息

---

## 注意事项

### 1. **线程安全**
- `send()` 方法使用 `spin_until_future_complete`，会阻塞当前线程
- 鉴权缓存使用字典，非线程安全（仅在 ROS2 executor 线程中访问）

### 2. **设备 ID 配置**
- 生产环境：确保 `/etc/cmcc_robot/cmcc_dev.ini` 存在且包含正确的 `devSn`
- 开发环境：设备 ID 会回退到 `"unknown"`，不影响启动

### 3. **鉴权信息缓存**
- 缓存基于 `expireAt` 字段自动过期
- 建议在长时间运行的服务中定期检查缓存有效性
- 鉴权响应会打印完整数据便于调试

### 4. **语音助手模式设置**
- 仅在 `assistant_startup_mode >= 0` 时生效
- 需要 `homi_speech` 节点运行才能成功设置
- 设置失败不会影响节点启动
- 所有请求和响应都会详细记录

### 5. **事件处理**
- `credential_get` 响应会被内部处理，不会传递给 `on_speech_event`
- 其他事件需要子类实现 `on_speech_event` 方法处理
- 所有事件都会打印完整的原始数据和格式化数据

### 6. **调试建议**
- 生产环境建议使用 `INFO` 级别，减少日志输出
- 调试问题时可临时切换到 `DEBUG` 级别
- 所有数据传输都有详细日志，便于追踪问题
- 使用 emoji 标识（📤📥🎤）快速定位不同类型的日志

---

## 依赖

### ROS2 包依赖
- `rclpy`
- `homi_speech_interface`

### Python 依赖
- `configparser` (标准库)
- `json` (标准库)
- `time` (标准库)

---

## 版本历史

| 版本 | 日期 | 变更说明 |
|------|------|----------|
| 1.2.5 | 2026-06-18 | - 添加详细的发送/接收数据打印（完整JSON + 格式化）<br>- 添加语音助手模式设置的详细日志<br>- 使用 emoji 标识不同类型的日志（📤📥🎤）<br>- 优化日志格式，提升可读性 |
| 1.2.4 | 2026-06 | - 添加异步非阻塞鉴权机制<br>- 添加语音助手模式自动设置<br>- 增强日志输出 |
| 1.0.0 | - | 初始版本 |

---

## 相关文档

- [HermesBridge 接口文档](./HERMES_BRIDGE_API.md)
- [OpenClawBridge 接口文档](./OPENCLAW_BRIDGE_API.md)
- [SpeechCore 通信协议](./SPEECHCORE_PROTOCOL.md)

---

**文档生成时间**: 2026-06-17  
**维护者**: xuhui@cmhi.chinamobile.com
