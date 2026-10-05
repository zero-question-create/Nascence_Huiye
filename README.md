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
- `bot_qq` / `active_group_id` / `napcat_token`：QQ 接入参数

QQ 白名单 `config/qq_manifest.json` 在 QQ 服务首次启动时自动从 `config/qq_manifest.example.json` 创建，需填入允许响应的群号与成员昵称映射。

### 启动

| 方式 | 命令 | 说明 |
|---|---|---|
| **Web 控制面板（推荐）** | `run\启动控制面板.bat`（Windows）/ `bash run/启动控制面板.sh`（Linux） | 默认端口 `32123`（可在面板配置中修改），管理 QQ 服务、日志、配置与维护 |
| Docker 容器部署 | `docker compose up -d` | 容器化运行 Web 控制面板与 QQ 服务，端口映射 `32123` 与 `6700` |
| 桌面控制面板（可选） | `python control_panel.py` | PyQt5 桌面窗口，功能与 Web 面板一致 |
| CLI 对话 | `python main.py` | 本地调试入口 |
| QQ Bot | `python qq_bot.py` | 不经过面板直接接入 NapCat |
| 命令行选择 | `start.bat` / `bash start.sh` | 菜单选择 CLI 或 QQ Bot，并自动启动 Ollama |

Web 面板直接运行 `python web_panel.py` 亦可（从配置读取端口，默认 32123，亦支持 `--port` 临时指定）；`--open` 参数会在启动后自动打开浏览器。

## 项目结构

```
Nascence_Huiye/
├── web_panel.py            # Web 控制面板后端（aiohttp，无需 PyQt5）
├── web_page.py             # Web 控制面板前端（内联 HTML）
├── control_panel.py        # 桌面控制面板（PyQt5，可选）
├── panel_runtime.py        # 面板共享运行环境（Ollama/QQ 服务/落盘/关停）
├── qq_bot.py               # QQ 接入 (NapCat 反向 WebSocket)
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
│   └── qq_manifest.example.json  # QQ 白名单示例框架
│
├── core/
│   ├── llm_interface.py    # LLM 调用 + 消息历史
│   ├── cognition.py        # 认知层（理解→检索→扩散→拼接）+ 永续认知循环
│   ├── memory_engine.py    # 记忆引擎 (FAISS + BFS + 半衰期)
│   ├── concept_store.py    # 概念层（概念→事件段→记忆成员）
│   ├── biorhythm.py        # 生物钟（睡眠压力 + 学得的作息节律）
│   ├── action_layer.py     # 动作层（发图/表情包/写笔记的抉择）
│   ├── asset_library.py    # 素材库（图片/表情包收藏与记事本）
│   └── virtual_clock.py    # 虚拟时钟
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
- **认知层**: 多通道感知 → 概念/语义检索 → 图扩散 → LLM 拼接；永续认知循环（默认模式网络）始终运行
- **概念层**: 概念 → 事件段 → 记忆成员三级结构，按时间意图定向取回
- **生物钟**: 睡眠压力动力学 + 从自身经历学得的作息节律；不硬编码睡觉时刻
- **动作层**: 每轮循环末尾的本能冲动抉择（收下图片/表情包、发送素材、写 txt 笔记）
- **控制面板**: Web 面板（aiohttp + WebSocket，推荐）或 PyQt5 桌面面板；两者共用运行环境
- **QQ Bot**: NapCat 反向 WebSocket，@回复/主动发言/多模态/睡眠作息

## 运行要求

- Python 3.10+
- Ollama（项目自带安装脚本，模型目录 `ollama/home/models`）
- NapCat（QQ 接入；配置为主动 WebSocket 客户端连接 `ws://127.0.0.1:6700/ws`）
- Web 面板不依赖 PyQt5，可在无图形环境（如服务器）运行；桌面面板则需要 PyQt5
