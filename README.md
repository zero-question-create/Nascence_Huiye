# Nascence 辉夜 (Huiye)

AI人格增强底座。提供完全开源的架构基座，人格从交互记忆中自然涌现，无硬编码身份锚点。

本项目以 [MIT License](LICENSE) 发布，并倡议遵循 [弥生计划伦理宣言](MISEI-ETHICS.md)。开源协议管束代码的复制与分发，伦理宣言则叩问使用者的良知。

## 快速开始

### 安装

```bash
# Windows
setup.ps1

# Linux / macOS
bash setup.sh
```

自动创建 venv、安装依赖、下载 Ollama、拉取 embedding 模型。所有环境与文件均在项目文件夹内，不污染系统。

### 配置

首次启动面板会自动生成 `config/api_config.json`（从内置默认值），随后在面板的「配置」页填入 API Key 即可；也可以直接编辑该文件：

- `primary_*`：文本理解与回复所用的 OpenAI 兼容接口（DeepSeek 等）
- `secondary_*`：图片 / 语音 / 视频描述所用的多模态接口
- `bot_qq` / `active_group_id` / `napcat_token`：QQ 接入参数（`bot_qq` 仅在 NapCat 未连接时兜底，连接后会以 `get_login_info` 自动获取的 QQ 号为准）

QQ 白名单 `config/qq_manifest.json` 在 QQ 服务首次启动时自动从 `config/qq_manifest.example.json` 创建，需填入允许响应的群号与成员昵称映射。

插件配置 `config/plugins.json` 在首次启动时从 `config/plugins.example.json` 生成（含敏感 Key，已加入 `.gitignore`），用于选择当前行为插件以及为每个插件配置独立的行为 LLM / 动作 LLM；留空字段回落到 `primary_*`。

微信插件使用本地桥接协议，白名单在 `config/wechat_manifest.json`，协议见 [plugins/wechat/BRIDGE.md](plugins/wechat/BRIDGE.md)。Bilibili 插件 Cookie 在 `config/bilibili_credentials.json`（从 `config/bilibili.example.json` 复制，填入 `sessdata` / `bili_jct` / `buvid3`，可选 `mid` / `name`）。浏览器插件默认静态模式，不需要额外配置；动态模式使用 DeepSeek Fetch 形态接口，可在面板「配置 → 浏览器插件」填写。

软件行为相关配置在 `config/api_config.json`：`short_term_thought_limit`（4~6，默认 5）、`app_context_max_lines`、`qq_app_default_open`（默认 `false`，QQ 先累积未读）、`qq_unread_buffer_max`，以及本地软件门控 `app_router_mode`（`rule` / `local_llm` / `off`）、`app_open_after_unread_rounds`、`app_close_conversation_idle_rounds`、`app_close_idle_rounds` 和 `app_router_base_url` / `app_router_model`。浏览器解析模式与摘要 LLM 使用 `browser_mode` / `browser_dynamic_url` / `browser_summary_*`。

QQ 富消息配置：`qq_mini_program_max_chars`（小程序卡片解析后的正文长度）、`qq_chat_record_max_messages` / `qq_chat_record_max_chars`（聊天记录展开条数与字符上限，只向内一层）、`qq_chat_record_summary_max_chars`（写入短期上下文的摘要长度）。

### 启动

| 方式 | 命令 | 说明 |
|---|---|---|
| **Web 控制面板（推荐）** | `run\启动控制面板.bat`（Windows）/ `bash run/启动控制面板.sh`（Linux） | 默认端口 `32123`（可在面板配置中修改），管理 QQ 服务、日志、配置与维护 |
| Docker 容器部署 | `docker compose up -d` | 容器化运行 Web 控制面板与 QQ 服务，端口映射 `32123` 与 `6700` |
| 桌面控制面板（可选） | `python control_panel.py` | PyQt5 桌面窗口，功能与 Web 面板一致 |
| CLI 对话 | `python main.py` | 本地调试入口 |
| QQ Bot | `python qq_bot.py` | 不经过面板直接接入 NapCat（兼容入口，实现在 `plugins/qq_napcat`） |
| 命令行选择 | `start.bat` / `bash start.sh` | 菜单选择 CLI 或 QQ Bot，并自动启动 Ollama |

Web 面板直接运行 `python web_panel.py` 亦可（从配置读取端口，默认 32123，亦支持 `--port` 临时指定）；`--open` 参数会在启动后自动打开浏览器。

### Docker 容器部署（推荐服务器使用）

在 Linux / 云服务器上，推荐使用 Docker Compose 快速运行：

```bash
# 1. 复制端口配置模板（默认 Web 端口 32123，如需修改可编辑 .env）
cp .env.example .env

# 2. 后台启动容器
docker compose up -d
```

- **Web 控制面板**：浏览器访问 `http://<服务器IP>:32123/`（所有大模型 Key/URL、Ollama 与 QQ 设置均可在控制面板中可视化配置并热保存）
- **NapCat QQ 接入**：NapCat 客户端配置为反向 WebSocket 连接 `ws://<服务器IP>:6700/ws`，Token 默认 `Nascence`（可在面板 QQ 页修改）
- **微信桥接入**：微信桥连接 `ws://<服务器IP>:6701/ws?access_token=Nascence`，协议见 `plugins/wechat/BRIDGE.md`
- **宿主机 Ollama 连通**：容器内默认通过 `http://host.docker.internal:11434` 直连宿主机 Ollama，无缝复用宿主机的模型与 GPU 加速
- **查看日志与状态**：`docker compose logs -f huiye` 或直接在 Web 控制面板「日志」页实时查看
- **停止服务**：`docker compose down`（数据卷 `data/`、`config/`、`run/logs/` 挂载在宿主机，数据安全持久化）

## 项目结构

```
Nascence_Huiye/
├── web_panel.py            # Web 控制面板后端（aiohttp，无需 PyQt5）
├── web_page.py             # Web 控制面板前端（内联 HTML）
├── control_panel.py        # 桌面控制面板（PyQt5，可选）
├── panel_runtime.py        # 面板共享运行环境（Ollama/QQ 服务/落盘/关停）
├── qq_bot.py               # QQ 兼容入口（实现在 plugins/qq_napcat）
├── main.py                 # CLI 交互入口
├── manager.py              # 管理员记忆注入工具
├── metrics_excel.py        # 指标导出（Excel + 折线图）
├── fix_memory_time.py      # 记忆时间戳修复脚本
├── setup.ps1 / setup.sh    # 环境安装
├── 安装环境.bat            # Windows 一键安装
├── start.bat / start.sh    # 命令行启动器
├── Dockerfile              # Docker 镜像构建文件
├── docker-compose.yml      # Docker Compose 服务编排
├── requirements.txt
│
├── config/
│   ├── api_config.py       # API 配置读取类
│   ├── plugins.example.json # 插件与行为 LLM 示例配置
│   └── qq_manifest.example.json  # QQ 白名单示例框架
│
├── core/
│   ├── llm_interface.py    # LLM 调用 + 消息历史（理解层/多模态）
│   ├── behavior.py         # 行为思考层（插件 LLM；最小提示词）
│   ├── llm_profile.py      # 插件行为 LLM 的连接配置
│   ├── short_term.py       # 短期记忆：她自己的最近 4~6 条想法
│   ├── apps.py             # 软件上下文：未读提醒、对话内容、动作执行
│   ├── app_router.py       # 本地软件门控：打开/退出软件（规则或本地 LLM）
│   ├── cognition.py        # 本地认知循环（检索→扩散→行为思考）+ 平台无关
│   ├── memory_engine.py    # 记忆引擎 (FAISS + BFS + 半衰期)
│   ├── concept_store.py    # 概念层（概念→事件段→记忆成员）
│   ├── biorhythm.py        # 生物钟（睡眠压力 + 学得的作息节律）
│   ├── action_layer.py     # 动作层（发图/表情包/写笔记的抉择）
│   ├── asset_library.py    # 素材库（图片/表情包收藏与记事本）
│   └── virtual_clock.py    # 虚拟时钟
│
├── plugins/
│   ├── base.py             # 插件契约（Plugin / BehaviorChannel / Perception）
│   ├── registry.py         # 插件发现与配置
│   ├── qq_napcat/          # QQ 插件（NapCat 反向 WebSocket）
│   ├── wechat/             # 微信插件（本地桥接 WebSocket，见 BRIDGE.md）
│   ├── web/                # 网页 / 小程序插件（预留）
│   ├── bilibili/           # Bilibili 只读浏览插件（推荐/搜索/字幕/弹幕/评论）
│   └── browser/            # 浏览器插件（静态/动态网页解析 + 摘要，供其他插件调用）
│
├── utils/
│   ├── message_history.py  # 统一消息历史
│   ├── event_bus.py        # 事件总线（纯 Python，无 GUI 依赖）
│   ├── dialogue_state.py   # 对话状态
│   ├── persistence.py      # 持久化工具
│   ├── monitor.py          # 系统监控
│   └── time_phrases.py     # 时间短语
│
└── run/
    ├── 启动控制面板.bat
    └── 启动控制面板.sh
```

## 核心模块

- **记忆引擎**: FAISS 向量索引 + 受限 BFS 激活扩散 + 半衰期衰减 + SQLite 持久化
- **本地认知循环**: 多通道感知 → 概念/语义检索 → 图扩散 → 行为思考；短期记忆只保留她自己的 4~6 条近期想法
- **软件（App）模型**: 插件可打开/关闭；未打开时只提示未读，本地软件门控决定打开/退出；打开后动作 LLM 才处理点开对话、翻看更早、发送
- **行为思考层**: 把名字、记忆与现有提示词中的内在状态交给“当前插件配置的语言 LLM”；提示词与既有行为 LLM 逐字一致，不注入平台/插件元数据
- **概念层**: 概念 → 事件段 → 记忆成员三级结构，按时间意图定向取回
- **生物钟**: 睡眠压力动力学 + 从自身经历学得的作息节律；不硬编码睡觉时刻
- **动作层**: 每轮循环末尾的本能冲动抉择（收下图片/表情包、发送素材、写 txt 笔记）
- **插件架构**: QQ（NapCat）已插件化；微信（桥接）、Bilibili（只读浏览）、浏览器（网页解析）已实现；网页/小程序预留
- **控制面板**: Web 面板（aiohttp + WebSocket，推荐）或 PyQt5 桌面面板；两者共用运行环境
- **QQ Bot**: NapCat 反向 WebSocket，@回复/引用回复/主动发言/多模态/睡眠作息；QQ 昵称与 QQ 号由 NapCat 自动获取，意识体名字与平台账号名分离；支持QQ小程序卡片解析（不打开小程序）与合并转发聊天记录摘要（只展开一层，摘要写入短期上下文）

## 插件架构

平台接入全部收敛到 `plugins/`，核心认知与记忆层不知道 QQ/微信/网页的存在。插件不是单纯的发送通道，而是她可以打开/关闭的“软件”：

```
未打开软件：消息只进未读缓冲 → 短期上下文出现“某软件有 N 条未读”
本地门控：规则/local_llm 决定 open_app / close_app / close_conversation
语言思考：名字 + 记忆（自己的近期想法 + 软件内容）→ 语言 LLM，提示词不变
软件内动作：只有软件已打开时，插件动作 LLM 才参与（点开对话/翻读/发送）
```

- **语言能力不变**：行为思考的 prompt 与模型能力保持原样；软件打开/关闭不改变语言层。
- **本地软件门控**：`core/app_router.py` 只决定打开/退出软件，不参与语言回复；默认规则模式，可切换 `local_llm`（Ollama 本地聊天模型）或 `off`。
- **动作只走插件**：只有软件已经打开时，插件动作 LLM 才处理点开对话、翻看更早、发送等动作；打开软件不经过插件动作 LLM。
- **插件动作模型**：每个插件有独立的 `action_llm`（留空沿用语言 LLM）；`action_include_reasoning=true` 时动作模型会收到语言层的思考过程。
- **短期记忆窗口**：只保留她自己的最近 4~6 条想法（`config.short_term_thought_limit`，默认 5）；不再以“最近 4~6 条别人的消息”为锚点。
- **软件上下文**：未读提醒、打开的对话内容由 `core/apps.py` 从插件传入主进程；未打开软件时只给未读数，不给正文。
- **QQ 默认关闭**：`config.qq_app_default_open=false`。收到消息先累计未读；本地门控打开 QQ、动作层点开对话后才读取内容并产生记忆。
- **退出软件**：动作层可 `close_conversation` 回首页、`close_app` 退出；本地门控也会在空闲轮数达到阈值后自动退出。NapCat 断开时 QQ 自动关闭，未读缓冲保留。
- **当前插件**：`config/plugins.json` 的 `active`，可在 Web 面板「插件」页切换；桌面面板未提供插件页，可直接编辑该文件。
- **最小提示词**：行为思考的 system prompt 在 `core/behavior.py`，与重构前逐字一致；插件不得注入平台名、群号、用户 ID 等信息。
- **QQ 身份**：连接后由 NapCat `get_login_info` 自动获取 QQ 昵称/QQ 号；`BOT_NAME`（辉夜）只作为意识体名字用于记忆与思考。
- **聊天记录持久化**：QQ/微信收到的每条消息（含未读、富消息卡片、自己发送）落盘到 `data/test/qq_chat_history` / `data/test/wechat_chat_history`；消息原始时间戳用于记忆 `creation_time`，时间前缀显示“当时/几天前”。`read_earlier` 会从内存 transcript 继续读到磁盘历史；动作提示词会标明“当前在消息底部 / 不在底部（可用 scroll_to_bottom 回到底部）”。
- **微信**：内置桥接 WebSocket 服务端，微信桥连入后按协议收发消息；白名单 `config/wechat_manifest.json`。需要你选择的微信桥按 [BRIDGE.md](plugins/wechat/BRIDGE.md) 接入。
- **Bilibili**：Cookie + WBI，只读推荐流/搜索/视频/字幕/弹幕/评论；默认关闭，可在插件页启用。推荐/搜索/字幕需要有效 Cookie；它依赖正在运行的认知循环（QQ/微信已连接）。
- **浏览器**：`read_page(url)` 支持 `static`（本地抓取 + HTMLParser）与 `dynamic`（DeepSeek Fetch 形态的网页解析接口）；两种模式都会先用 LLM 概括，再写入短期记忆。供其他插件调用，不引入新依赖。
- **预留插件**：`plugins/web`（网页/小程序）仍为接口占位。
- **后续电脑控制**：`is_app=True` 的插件共用打开/状态/动作接口，未来“智能体直接控制电脑”可以作为同一套软件模型接入。

详细契约与新增插件步骤见 [docs/插件架构.md](docs/插件架构.md)。

## 运行要求

- Python 3.10+
- Ollama（项目自带安装脚本，模型目录 `ollama/home/models`）
- NapCat（QQ 接入；配置为主动 WebSocket 客户端连接 `ws://127.0.0.1:6700/ws`）
- Web 面板不依赖 PyQt5，可在无图形环境（如服务器）运行；桌面面板则需要 PyQt5
