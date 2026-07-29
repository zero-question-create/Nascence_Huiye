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

自动创建 venv、安装依赖、下载 Ollama、拉取 embedding 模型。

### 配置

编辑 `config/api_config.json`，填入 API Key。QQ 白名单由系统自动从 `config/qq_manifest.example.json` 创建。

### 启动

| 方式 | 命令 | 说明 |
|---|---|---|
| **控制面板（推荐）** | `run\启动控制面板.bat` | GUI 管理三种互斥模式 |
| CLI 对话 | `python main.py` | 本地测试 |
| QQ Bot | `python qq_bot.py` | NapCat 接入 |
| 自训练 | `python self_training.py` | 自主循环 |
| 命令行选择 | `start.bat` | 菜单选择 + 自动启动 Ollama |

## 项目结构

```
Nascence_Huiye/
├── control_panel.py        # 控制面板 (PySide6)
├── self_training.py        # 自主循环（感知→决策→动作→语言）
├── qq_bot.py               # QQ 接入 (OneBot)
├── main.py                 # CLI 交互入口
├── manager.py              # 管理员工具
├── final_test.py           # 集成测试
├── download_model.py       # 下载 embedding 模型
├── fix_memory_time.py      # 修复记忆时间戳
├── test_LLM_api.py         # API 测试
├── test_time.py            # 时间测试
├── setup.ps1 / setup.sh    # 环境安装
├── 安装环境.bat            # Windows 一键安装
├── start.bat / start.sh    # 命令行启动器
├── requirements.txt
├── .gitignore
│
├── config/
│   ├── api_config.py       # API 配置读取类
│   └── qq_manifest.example.json  # QQ 示例框架
│
├── core/
│   ├── llm_interface.py    # LLM 调用 + 消息历史
│   ├── cognition.py        # 认知层（理解→检索→扩散→拼接）
│   ├── memory_engine.py    # 记忆引擎 (FAISS + BFS + 半衰期)
│   └── virtual_clock.py    # 虚拟时钟
│
├── utils/
│   ├── caiye.py            # 彩叶角色
│   ├── message_history.py  # 统一消息历史
│   ├── event_bus.py        # 事件总线
│   ├── world_creator.py    # 客观世界创建
│   ├── world_layer.py      # 世界场景/事件/移动
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
- **认知层**: 多通道感知 → 语义检索 → 图扩散 → LLM 拼接
- **自主循环**: 三通道感知（视觉/听觉/身体感觉）→ 决策 → 动作/语言/彩叶回应
- **控制面板**: PySide6 GUI，运行状态监控、日志分栏、服务互斥、对话测试
- **QQ Bot**: NapCat 反向 WebSocket，@回复/主动发言/多模态/睡眠作息
- **客观世界**: 场景描述 + 环境事件 + 空间移动，持久化场景状态
