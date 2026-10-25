# 微信桥接协议

微信没有像 OneBot 那样统一的本地协议，所以本插件自带一个 **WebSocket 服务端**，由你选择的微信桥作为客户端连入。

```
微信桥（WeChatFerry / ntchat / 其他本地桥）
        │  ws://<host>:6701/ws?access_token=Nascence
        ▼
plugins/wechat/runtime.py  →  AppContext / 记忆 / 认知循环
```

## 连接

- 地址：`ws://127.0.0.1:6701/ws`（端口/Token 可在 `api_config.json` 的 `wechat_ws_port` / `wechat_token` 修改）
- 鉴权：`Authorization: Bearer <token>` 或 `?access_token=<token>`
- 单帧上限 32MB

## 桥 → 主进程：事件

### 1. 登录身份

连接后先发一次（主进程用来自我识别，后台不配置微信昵称）：

```json
{"post_type":"self_info","self_id":"wxid_bot","name":"微信昵称"}
```

### 2. 消息

```json
{
  "post_type": "message",
  "message_type": "private",
  "message_id": "10086",
  "self_id": "wxid_bot",
  "sender_id": "wxid_friend",
  "sender_name": "小王",
  "conversation_id": "wxid_friend",
  "conversation_name": "小王",
  "text": "在吗",
  "media": [],
  "timestamp": 1790000000
}
```

- `message_type`：`private` 或 `group`
- `conversation_id`：私聊填对方 wxid，群聊填群 ID；必须出现在 `config/wechat_manifest.json` 的 `whitelist_conversations` 中
- `media`：可选，元素形如
  ```json
  {"type":"image","description":"一张猫的照片","path":"/abs/path/cat.jpg"}
  ```
  `type` 支持 `image` / `sticker` / `record` / `video` / `file` / `link`。主进程优先使用 `description`；`path` 供后续素材收藏/发送使用。
- 重复 `message_id` 会被去重。

## 主进程 → 桥：动作

所有动作都带 `echo`；桥必须在响应中原样带回：

```json
{"action":"send_text","params":{"conversation_id":"wxid_friend","text":"在的"},"echo":"..."}
```

响应：

```json
{"echo":"...","status":"ok","data":{"ok":true}}
```

支持的动作：

| action | params | 说明 |
|---|---|---|
| `send_text` | `conversation_id`, `text` | 发文本 |
| `send_media` | `conversation_id`, `path`, `as_sticker` | 发本地文件；桥与主进程需在同一文件系统，或桥自行读取该路径 |
| `get_history` | `conversation_id`, `limit` | 可选；返回 `{"items":[{"sender_id","sender_name","text","timestamp"}]}`，供“翻看更早” |

## 行为边界

- QQ/微信都是“软件”：未打开时消息只进未读缓冲，不调用理解层、不写记忆。
- 打开软件/对话后，桥上报的消息才会进入主进程。
- 微信昵称由桥 `self_info` 自动上报，不使用 `BOT_NAME` 冒充平台昵称。
- 微信桥不负责情绪、记忆、人格；这些都在主进程。

## 最小桥接示例（伪代码）

```python
import asyncio, json, websockets

async def bridge():
    async with websockets.connect("ws://127.0.0.1:6701/ws?access_token=Nascence") as ws:
        await ws.send(json.dumps({"post_type": "self_info", "self_id": "wxid_bot", "name": "微信昵称"}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("action") == "send_text":
                # 调用你的微信桥发送
                ok = await my_wechat.send_text(msg["params"]["conversation_id"], msg["params"]["text"])
                await ws.send(json.dumps({"echo": msg["echo"], "status": "ok" if ok else "failed", "data": {"ok": ok}}))
```

实际微信桥负责登录、收消息、发消息与媒体；本插件不直接接触微信协议。
