# Hermes XiaoliChannel 流式响应问题修复总结

## 背景

在 Hermes Gateway 通过 `xiaolichannel` 与 ROS2 `hermes_bridge` 对接 SpeechCore 的链路中，发现回复存在以下异常：

- 最终响应被截断，例如只返回：`我是 Kiro， ▉`
- `device_msg_response` 中已经标记 `is_final=True`，但文本仍包含流式光标 `▉`
- `body.stopReason` 已经是 `stop`，但内容不是完整最终回复
- 开启 Hermes 全局 streaming 后，上游模型接口出现空 SSE 错误：

```text
Provider returned an empty stream with no finish_reason
(possible upstream error or malformed SSE response)
```

最终确认问题不是单一位置导致，而是 **Hermes Gateway 流式编辑链路、xiaolichannel adapter、hermes_bridge 运行态处理逻辑** 多处组合问题。

---

## 涉及链路

整体链路如下：

```text
SpeechCore
  -> /homi_speech/sigc_event_topic
  -> hermes_bridge
  -> XiaoliChannel API: /stream 或 /getupdates
  -> Hermes Gateway
  -> LLM Provider
  -> XiaoliChannel API: /sendmessage, /editmessage
  -> hermes_bridge
  -> SpeechCore device_msg_response
```

关键 HTTP 接口：

```text
GET  /getupdates
GET  /stream
POST /sendmessage
POST /editmessage
GET  /getconfig
```

---

## 问题 1：`/sendmessage` 把流式首包误判为最终消息

### 现象

日志中出现：

```text
[SendMessage] ... reply_to=xxx text=我是 Kiro， ▉
处理 Gateway 消息: ... is_final=True text=我是 Kiro， ▉
发送 device_msg_response 到 SpeechCore: ... is_final=True text=我是 Kiro， ▉
```

也就是文本仍然带 `▉`，但已经被标记为最终消息。

### 原因

`hermes_bridge.py` 中原逻辑为：

```python
"is_final": bool(reply_to),  # 带 reply_to 的消息通常是最后一条
```

但 Hermes Gateway 流式响应的首包也可能带 `reply_to`，用于回复原始用户消息。此时内容仍是流式预览，例如：

```text
我是 Kiro， ▉
```

因此仅凭 `reply_to` 判断 final 是错误的。

### 修复

新增流式预览判断：

```python
reply_to = data.get("reply_to", "")
is_streaming_preview = "▉" in text
is_final = bool(reply_to) and not is_streaming_preview
```

入队时使用修正后的 `is_final`：

```python
self._incoming_messages.put({
    "message_id": message_id,
    "chat_id": chat_id,
    "user_id": user_id,
    "text": text,
    "is_final": is_final,
    "timestamp": int(time.time())
})
```

### 效果

流式首包仍可转发给 SpeechCore，但不会带 `stopReason: "stop"`：

```text
is_final=False text=二次验证流式首包 1781845450 ▉
```

---

## 问题 2：`/editmessage` 只返回成功，没有转发给 SpeechCore

### 现象

Hermes Gateway 调用了 `/editmessage`，接口返回成功，但 SpeechCore 没有收到中间更新和最终更新。

原日志只有：

```text
[EditMessage] message_id=xxx content=...
```

没有后续：

```text
处理 Gateway 消息
发送 device_msg_response 到 SpeechCore
```

### 原因

`_handle_edit_message()` 原实现只是简单返回成功：

```python
self.logger.debug(f"[EditMessage] message_id={message_id} content={safe_id(content, 50)}")

return web.json_response({
    "ret": 0,
    "message_id": message_id
})
```

也就是说，Hermes Gateway 以为 edit 成功了，但机器人侧根本没有收到 edit 内容。

### 修复

`/editmessage` 需要解析：

- `app_id`
- `chat_id`
- `message_id`
- `content`
- `context_token`
- `finalize`

并把内容放入 `_incoming_messages`，由原有 `_process_incoming_messages()` 转发到 SpeechCore：

```python
finalize = bool(data.get("finalize", False))

self._incoming_messages.put({
    "message_id": queue_message_id,
    "chat_id": chat_id,
    "user_id": data.get("user_id", ""),
    "text": content,
    "is_final": finalize,
    "timestamp": int(time.time())
})
```

同时保留 `app_id` 校验，错误 app_id 不允许转发：

```python
if app_id != self.app_id:
    return web.json_response({
        "ret": -1,
        "errcode": -1,
        "errmsg": "Invalid app_id"
    })
```

### 效果

中间 edit：

```text
[EditMessage] ... finalize=False content=二次验证流式中间更新 1781845451 ▉
处理 Gateway 消息: ... is_final=False text=二次验证流式中间更新 1781845451 ▉
发送 device_msg_response 到 SpeechCore: ... is_final=False text=二次验证流式中间更新 1781845451 ▉
```

最终 edit：

```text
[EditMessage] ... finalize=True content=二次验证流式最终完成 1781845452，完整响应无光标。
处理 Gateway 消息: ... is_final=True text=二次验证流式最终完成 1781845452，完整响应无光标。
发送 device_msg_response 到 SpeechCore: ... is_final=True text=二次验证流式最终完成 1781845452，完整响应无光标。
```

---

## 问题 3：`editmessage` 复用原 `message_id`，被去重逻辑吞掉

### 现象

修复 `/editmessage` 转发后，仍发现 edit 没有进入最终转发路径。

### 原因

`_process_incoming_messages()` 使用 `message_id` 做去重：

```python
if message_id in self._processed_message_ids:
    self.logger.debug(f"跳过重复消息 ID: {message_id}")
    continue
```

而 edit API 复用原始 `message_id`，例如：

```text
sendmessage message_id=msg_1781845450827
editmessage message_id=msg_1781845450827
```

因此中间 edit 和最终 edit 会被误判为重复消息。

### 修复

`/editmessage` 入队时生成队列内部唯一 ID：

```python
queue_message_id = f"{message_id}:edit:{int(time.time() * 1000)}"
```

入队使用 `queue_message_id`：

```python
self._incoming_messages.put({
    "message_id": queue_message_id,
    ...
})
```

外部 HTTP 返回仍返回原 `message_id`：

```python
return web.json_response({
    "ret": 0,
    "message_id": message_id
})
```

### 效果

edit 更新不会再被去重吞掉：

```text
处理 Gateway 消息: message_id=msg_1781845450827:edit:1781845451833 ... is_final=False
处理 Gateway 消息: message_id=msg_1781845450827:edit:1781845452837 ... is_final=True
```

---

## 问题 4：Hermes xiaolichannel adapter 需要支持 final edit

### 原因

Hermes Gateway 的流式消费者会根据平台能力决定是否执行最终 `edit_message(finalize=True)`。

xiaolichannel 的 `edit_message()` 里只有 `finalize=True` 时才会做最终格式化/终态通知：

```python
text = self.format_message(content) if finalize else content
```

如果没有显式声明平台需要 final edit，可能会跳过最后一次 edit，导致光标无法移除。

### 修复

在 `XiaoliChannelAdapter` 中增加：

```python
REQUIRES_EDIT_FINALIZE: bool = True
```

作用：确保 Hermes Gateway 在流式完成后调用：

```python
edit_message(..., finalize=True)
```

---

## 问题 5：Hermes 全局 `streaming.enabled=true` 会触发上游模型空 SSE 错误

### 现象

开启 Hermes Gateway 全局 streaming 后出现：

```text
Provider returned an empty stream with no finish_reason
(possible upstream error or malformed SSE response)
```

### 原因

该错误来自 **模型 Provider 上游 SSE**，不是 xiaolichannel `/stream`。当前上游模型接口在流式模式下返回空流或不规范 SSE。

### 处理

为了保证机器人链路稳定，当前将 Hermes 全局 token streaming 关闭：

```yaml
streaming:
  enabled: false
```

说明：

- 这不影响 `hermes_bridge` 对 `/editmessage` 的正确处理能力；
- 非流式最终响应可以稳定完整返回；
- 若后续需要启用 token streaming，需要先修复模型 Provider 上游 SSE 返回规范。

---

## 部署方式

必须使用项目构建脚本部署，不要只手动复制文件：

```bash
cd /mine/Code/ROS/release/robot-application/xiaoli_application_ros2
./build.sh --no=2.0 --select=claw_client --nopack
```

启动方式：

```bash
source build_2.0/debug/install/setup.zsh
ros2 launch claw_client claw_client.launch.py
```

验证 install 路径：

```text
build_2.0/debug/install/claw_client/lib/python3.8/site-packages/claw_client/hermes_bridge.py
```

---

## 验证结果

### 运行进程

```text
hermes_bridge pid=631834
0.0.0.0:8800 LISTEN
```

### HTTP 验证步骤

1. `/sendmessage` 首包，带 `reply_to` 且带 `▉`

结果：

```text
is_final=False text=二次验证流式首包 1781845450 ▉
```

2. `/editmessage finalize=false`

结果：

```text
is_final=False text=二次验证流式中间更新 1781845451 ▉
```

3. `/editmessage finalize=true`

结果：

```text
is_final=True text=二次验证流式最终完成 1781845452，完整响应无光标。
```

最终 payload：

```json
{
  "body": {
    "content": [
      {
        "type": "text",
        "text": "二次验证流式最终完成 1781845452，完整响应无光标。"
      }
    ],
    "timestamp": 1781845452838,
    "stopReason": "stop"
  }
}
```

关键验证点：

- 首包带 `▉`，但 `is_final=False`
- 中间 edit 带 `▉`，但 `is_final=False`
- 最终 edit 无 `▉`
- 最终 edit `is_final=True`
- 最终 payload 带 `stopReason: "stop"`
- 错误 `app_id` 会返回 `Invalid app_id`，不会转发给 SpeechCore

---

## 最终结论

本次问题已修复：

- `hermes_bridge` 不再把流式预览误判为最终消息；
- `/editmessage` 会正确转发中间更新和最终更新；
- edit 复用 `message_id` 不再被去重吞掉；
- 最终响应不再携带 `▉`；
- 最终响应会正确带 `stopReason: "stop"`；
- 当前因上游模型 Provider SSE 不稳定，Hermes 全局 token streaming 保持关闭，以保证完整稳定响应。

如果后续要重新开启 Hermes 全局 token streaming，需要优先验证并修复模型 Provider 的 SSE 输出，确保其返回有效 chunk 和 `finish_reason`。
