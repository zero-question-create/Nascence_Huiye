# gateway/README.md

# Nascence 辉夜 · 对话网关

将「对话测试」从控制面板独立出来，形成**客户端 + 服务端**的网关结构：

- **客户端**（`gateway/client/`）：独立的 Web 对话页，含登录/注册界面。
  网关地址在 `gateway/client/client_config.json` 中配置。
- **服务端**（`gateway/server.py`）：接收 `/gateway/*` 前缀的请求，
  负责用户注册/登录的信息核对，并为每个用户分配独立的 `data/<用户名>/`
  目录存放其记忆（memory.db / faiss / 对话状态等）。

## 快速开始

```bash
# 1. 启动网关（默认端口 8899）
python gateway/server.py --port 8899
# Windows 也可双击 gateway/start_gateway.bat

# 2. 浏览器访问
http://127.0.0.1:8899/
```

首次使用在页面上「注册」即可创建账号；注册成功即自动登录。

## 架构

```
浏览器(客户端) ── /gateway/register、/gateway/login、/gateway/chat … ──▶ gateway/server.py
                                                          │
                                                          ▼
                                    按用户切换数据目录并加载其记忆
                                    data/alice/  data/bob/  …（相互隔离）
```

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/gateway/register` | POST | 注册 {username, password}，返回 token |
| `/gateway/login` | POST | 登录 {username, password}，返回 token |
| `/gateway/chat` | POST | 对话 {token, sender, text, mentioned} |
| `/gateway/history` | GET | 获取当前用户消息历史 {token} |
| `/gateway/bot` | GET | 获取 bot 名字 |

## 用户数据隔离

每个用户注册时自动创建 `data/<用户名>/` 目录，包含：

- `memory.db`：记忆 SQLite 库
- `faiss.index`：语义向量索引
- `memory.json` / `dialogue_state.json` / `message_state.json`：热记忆与对话状态
- `clock_state.txt`：时钟状态

多用户互不影响；切换用户时引擎会重置并加载对应用户的数据。

## 说明

- 对话处理复用项目核心 `core.cognition.process_dialogue` 与模型后端配置
  （文本走外部 API 或本地 GGUF，embedding 走本地 llama.cpp）。
- 聊天请求在服务端按 `_chat_lock` 串行处理（memory_engine 为进程级单例）。
