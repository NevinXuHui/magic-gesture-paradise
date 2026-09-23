# SSE（Server-Sent Events）使用指南

## 概述

SSE 是一种服务器向客户端推送数据的技术，用于实时接收 OpenClaw 的回复消息。

## 为什么使用 SSE？

### 轮询模式的问题

```
客户端                    服务器
  |                         |
  |------ 请求 1 ---------->|
  |<----- 响应（无数据）-----|
  |                         |
  | 等待 1 秒...            |
  |                         |
  |------ 请求 2 ---------->|
  |<----- 响应（无数据）-----|
  |                         |
  | 等待 1 秒...            |
  |                         | 新消息到达！
  |------ 请求 3 ---------->|
  |<----- 响应（有数据）-----|
```

**问题**：
- 延迟高（平均 500ms）
- 资源浪费（大量空请求）
- 可能丢消息（间隔内多条）

### SSE 模式的优势

```
客户端                    服务器
  |                         |
  |------ 建立连接 -------->|
  |<===== 持久连接 =========|
  |                         |
  |                         | 新消息到达！
  |<----- 推送消息 1 -------|
  |                         |
  |                         | 新消息到达！
  |<----- 推送消息 2 -------|
  |                         |
  |<===== 持久连接 =========|
```

**优势**：
- 实时推送（延迟 < 100ms）
- 资源高效（1 个连接）
- 不丢消息（服务器主动推送）

## SSE 协议格式

### 基本格式

```
data: {"id":"123","text":"Hello"}

data: {"id":"124","text":"World"}
```

每条消息：
- 以 `data: ` 开头
- 后跟 JSON 数据
- 以两个换行符结束

### 消息结构

```json
{
  "id": "reply-1776148808518524494",
  "text": "好的,我来查询天气。",
  "chatId": "xiaoli-chat",
  "timestamp": 1776148808518
}
```

## Python 客户端实现

### 基础连接

```python
import urllib.request
import json

url = "http://localhost:8080/stream?chatId=xiaoli-chat"
req = urllib.request.Request(url)
req.add_header('Accept', 'text/event-stream')

with urllib.request.urlopen(req, timeout=None) as response:
    for line in response:
        line = line.decode('utf-8').strip()
        if line.startswith('data: '):
            data = json.loads(line[6:])
            print(f"收到消息: {data['text']}")
```

### 完整实现（带重连）

```python
import urllib.request
import json
import time
import threading

class SSEClient:
    def __init__(self, url, on_message):
        self.url = url
        self.on_message = on_message
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._listen, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _listen(self):
        while not self.stop_event.is_set():
            try:
                req = urllib.request.Request(self.url)
                req.add_header('Accept', 'text/event-stream')

                with urllib.request.urlopen(req, timeout=None) as response:
                    print("SSE 连接成功")
                    for line in response:
                        if self.stop_event.is_set():
                            break

                        line = line.decode('utf-8').strip()
                        if line.startswith('data: '):
                            try:
                                data = json.loads(line[6:])
                                self.on_message(data)
                            except json.JSONDecodeError as e:
                                print(f"JSON 解析失败: {e}")

            except Exception as e:
                if not self.stop_event.is_set():
                    print(f"SSE 连接异常: {e}")
                    print("5 秒后重连...")
                    time.sleep(5)

# 使用示例
def handle_message(data):
    print(f"收到消息: {data['text']}")

client = SSEClient(
    "http://localhost:8080/stream?chatId=xiaoli-chat",
    handle_message
)
client.start()

# 保持运行
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    client.stop()
```

## 测试 SSE 连接

### 使用 curl

```bash
# 连接 SSE 端点
curl -N http://localhost:8080/stream?chatId=xiaoli-chat

# 应该看到：
# data: {"id":"reply-xxx","text":"消息内容","chatId":"xiaoli-chat","timestamp":1776148808518}
```

### 使用 Python 脚本

```python
import urllib.request

url = "http://localhost:8080/stream?chatId=xiaoli-chat"
req = urllib.request.Request(url)
req.add_header('Accept', 'text/event-stream')

print("连接 SSE...")
with urllib.request.urlopen(req, timeout=None) as response:
    print("连接成功，等待消息...")
    for line in response:
        print(line.decode('utf-8').strip())
```

### 使用浏览器

打开浏览器开发者工具，在 Console 中运行：

```javascript
const eventSource = new EventSource('http://localhost:8080/stream?chatId=xiaoli-chat');

eventSource.onmessage = (event) => {
    const data = JSON.parse(event.data);
    console.log('收到消息:', data.text);
};

eventSource.onerror = (error) => {
    console.error('SSE 错误:', error);
};
```

## 消息去重

### 为什么需要去重？

- 网络重连可能导致重复消息
- 多个客户端可能收到相同消息
- 服务器重启后可能重发消息

### 去重实现

```python
class SSEClient:
    def __init__(self, url, on_message):
        self.url = url
        self.on_message = on_message
        self.processed_ids = set()  # 已处理的消息 ID

    def _handle_message(self, data):
        msg_id = data.get('id')
        if msg_id in self.processed_ids:
            return  # 跳过重复消息

        self.processed_ids.add(msg_id)
        self.on_message(data)

        # 定期清理旧 ID（可选）
        if len(self.processed_ids) > 1000:
            # 保留最近 500 个
            self.processed_ids = set(list(self.processed_ids)[-500:])
```

## 性能优化

### 1. 连接池

如果需要连接多个频道：

```python
class SSEManager:
    def __init__(self):
        self.clients = {}

    def subscribe(self, chat_id, on_message):
        if chat_id not in self.clients:
            url = f"http://localhost:8080/stream?chatId={chat_id}"
            client = SSEClient(url, on_message)
            client.start()
            self.clients[chat_id] = client

    def unsubscribe(self, chat_id):
        if chat_id in self.clients:
            self.clients[chat_id].stop()
            del self.clients[chat_id]
```

### 2. 消息队列

避免阻塞 SSE 线程：

```python
import queue

class SSEClient:
    def __init__(self, url):
        self.url = url
        self.message_queue = queue.Queue()

    def _listen(self):
        # SSE 线程：只负责接收
        for line in response:
            if line.startswith('data: '):
                data = json.loads(line[6:])
                self.message_queue.put(data)

    def get_message(self, timeout=1.0):
        # 主线程：从队列获取
        try:
            return self.message_queue.get(timeout=timeout)
        except queue.Empty:
            return None
```

### 3. 心跳检测

检测连接是否存活：

```python
import time

class SSEClient:
    def __init__(self, url):
        self.url = url
        self.last_message_time = time.time()

    def _listen(self):
        for line in response:
            self.last_message_time = time.time()
            # 处理消息...

    def is_alive(self):
        # 如果 60 秒没收到消息，认为连接断开
        return time.time() - self.last_message_time < 60
```

## 故障排查

### 连接失败

**症状**：`Connection refused`

**解决方案**：
1. 检查服务器是否运行：`curl http://localhost:8080/health`
2. 检查端口是否正确
3. 检查防火墙设置

### 消息延迟

**症状**：消息延迟超过 1 秒

**排查步骤**：
1. 检查网络延迟：`ping localhost`
2. 检查服务器日志
3. 检查客户端处理时间

### 连接断开

**症状**：连接频繁断开

**解决方案**：
1. 实现自动重连（见上面示例）
2. 检查网络稳定性
3. 检查服务器负载

### 消息重复

**症状**：收到相同消息多次

**解决方案**：
1. 实现消息去重（使用消息 ID）
2. 检查是否有多个客户端连接
3. 检查重连逻辑

## 最佳实践

### 1. 始终实现重连

```python
while not self.stop_event.is_set():
    try:
        # 连接 SSE
        self._connect()
    except Exception as e:
        print(f"连接失败: {e}")
        time.sleep(5)  # 等待后重连
```

### 2. 使用消息 ID 去重

```python
if msg_id in self.processed_ids:
    return  # 跳过重复消息
self.processed_ids.add(msg_id)
```

### 3. 异步处理消息

```python
def _handle_message(self, data):
    # 不要在 SSE 线程中做耗时操作
    threading.Thread(
        target=self._process_message,
        args=(data,),
        daemon=True
    ).start()
```

### 4. 优雅关闭

```python
def stop(self):
    self.stop_event.set()
    if self.thread:
        self.thread.join(timeout=5)
```

### 5. 错误处理

```python
try:
    data = json.loads(line[6:])
    self.on_message(data)
except json.JSONDecodeError as e:
    print(f"JSON 解析失败: {e}")
except Exception as e:
    print(f"消息处理失败: {e}")
```

## 参考资料

- [MDN: Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events)
- [HTML5 SSE 规范](https://html.spec.whatwg.org/multipage/server-sent-events.html)
- [OpenClaw Bridge 实现](./openclaw_bridge.py)

## 更新日期

2026-04-14
