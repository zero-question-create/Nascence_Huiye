# Nascence 辉夜 (Huiye)

长期对话 AI 系统。人格从交互记忆中自然涌现，无硬编码身份锚点。

本项目以 [BigScience Open RAIL-M License](LICENSE) 发布，并受 [弥生计划伦理宣言](MISEI-ETHICS.md) 的约束。许可证管束代码的复制与分发，伦理宣言则叩问使用者的良知。

## 快速开始

### 安装

```bash
# Windows
setup.ps1

# Linux
bash setup.sh
```

自动创建 venv、安装依赖、下载 llama.cpp 与三个 GGUF 模型（文本 / 向量 / 多模态）。

### 配置

三个模型能力（文本 / 语义向量 / 多模态图片）均可选择「本地默认模型」或「外部 API」，
在 WebUI 的「模型」页配置。

### 启动

| 方式 | 命令 | 说明 |
|---|---|---|
| **控制面板（推荐）** | `run\启动控制面板.bat` | WebUI：总览/认知循环/对话/模型/日志/配置/维护 |
| CLI 对话 | `python main.py` | 本地测试 |
| WebUI 直接启动 | `python webui.py` | 浏览器访问 http://127.0.0.1:8787 |

## 项目结构

```
Nascence_Huiye/
├── webui.py                 # Web 控制面板（aiohttp + WebSocket）
├── webui/                   # Web 前端（HTML/CSS/JS）
├── main.py                  # CLI 交互入口
├── manager.py               # 管理员工具
├── fix_memory_time.py       # 修复记忆时间戳
├── test_time.py             # 时间测试
├── setup.ps1 / setup.sh     # 环境安装
├── 安装环境.bat             # Windows 一键安装
├── start.bat / start.sh     # 命令行启动器
├── requirements.txt
├── .gitignore
│
├── config/
│   └── api_config.py        # 配置读取类（三后端开关）
│
├── core/
│   ├── model_backend.py     # llama.cpp 本地模型托管（文本/向量/多模态）
│   ├── cognition_runner.py  # 认知循环独立运行器（启停控制）
│   ├── llm_interface.py     # LLM 调用 + 消息历史
│   ├── cognition.py         # 认知层（理解→检索→扩散→拼接）
│   ├── memory_engine.py     # 记忆引擎 (FAISS + BFS + 半衰期)
│   └── virtual_clock.py     # 虚拟时钟
│
├── utils/
│   ├── message_history.py   # 统一消息历史
│   ├── event_bus.py         # 事件总线
│   ├── dialogue_state.py    # 对话状态
│   ├── persistence.py       # 持久化工具
│   ├── monitor.py           # 系统监控
│   └── time_phrases.py      # 时间短语
│
└── run/
    ├── 启动控制面板.bat      # 启动 WebUI
    ├── install_llama.ps1    # 下载 llama.cpp + GGUF 模型
    └── install_llama.sh     # Linux 版安装脚本
```

## 核心模块

- **记忆引擎**: FAISS 向量索引 + 受限 BFS 激活扩散 + 半衰期衰减 + SQLite 持久化
- **认知层**: 多通道感知 → 语义检索 → 图扩散 → LLM 拼接
- **认知循环**: 独立线程常驻的"默认模式网络"，主动思考与发言（显示于对话测试页）
- **控制面板**: WebUI，运行状态监控、日志分栏、对话测试、模型开关、认知循环启停
- **本地模型**: llama.cpp 托管三个 GGUF（Qwen3-4B / qwen3-embed-0.6b / Qwen2.5-VL-3B）
