import json
import os


def normalize_proxy_environment():
    """让 httpx/OpenAI 能识别常见代理工具导出的 socks scheme，并保护本地服务不走代理。"""
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        value = os.environ.get(name)
        if value and value.lower().startswith("socks://"):
            os.environ[name] = "socks5://" + value[len("socks://"):]

    # 保护本地服务（Ollama、NapCat 等）不经过代理
    local_entries = {"localhost", "127.0.0.1", "::1"}
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    current = {e.strip() for e in no_proxy.replace(",", " ").replace(";", " ").split() if e.strip()}
    if not local_entries.issubset(current):
        merged = ", ".join(sorted(current | local_entries))
        os.environ["NO_PROXY"] = merged
        os.environ["no_proxy"] = merged


normalize_proxy_environment()

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "api_config.json")

DEFAULT_CONFIG = {
    "primary_api_key": "YOUR_API_KEY_HERE",
    "primary_base_url": "https://api.deepseek.com",
    "primary_model": "deepseek-v4-flash",
    "secondary_api_key": "YOUR_API_KEY_HERE",
    "secondary_base_url": "https://api.lucisapi.ai/v1",
    "secondary_model": "gpt-5.6-sol",
    # 全局行为模型：只负责软件内动作；留空逐字段回落 primary。
    # 语言模型始终使用 primary，插件不可覆盖（保证意识连续性）。
    "behavior_api_key": "",
    "behavior_base_url": "",
    "behavior_model": "",
    "behavior_temperature": None,
    "behavior_max_tokens": None,
    "behavior_timeout": None,
    "ollama_base_url": "http://localhost:11434",
    "ollama_embed_model": "shaw/dmeta-embedding-zh",
    # 语义向量：ollama=原生 Ollama 接口；openai=OpenAI 兼容 /v1/embeddings 网络接口
    "embedding_api_mode": "ollama",
    "embedding_api_key": "",
    "embedding_dim": 768,
    "bot_qq": "123456",                     # 兜底 QQ 号；连接后以 NapCat get_login_info 自动获取的为准
    "active_group_id": "123456",
    "napcat_token": "Nascence",
    "napcat_http_url": "http://127.0.0.1:5700",  # NapCat 正向 HTTP 服务地址（用于语音等文件下载）
    "panel_port": 32123,                    # 控制面板 Web 端口（修改后下次启动生效）
    # ===== 生物钟参数（动力学速率，非钟点；不预设节律周期）=====
    "biorhythm_wake_seconds": 18.40 * 3600, # 清醒时睡眠压力上升的指数时间常数（秒）
    "biorhythm_sleep_seconds": 9.1 * 3600,  # 睡眠时睡眠压力回落的指数时间常数（秒）
    "biorhythm_onset_threshold": 0.62,      # 入睡阈值（S 达到 0.62 即精力 0.38 时入睡）
    "biorhythm_wake_threshold": 0.20,       # 醒来阈值（S 低于 0.20 即精力回到 0.80 时醒来）
    # ===== 作息惯性（学得的昼夜节律，非硬编码钟点）=====
    "biorhythm_rhythm_weight": 0.25,        # 作息窗口内最大睡意抬升量（峰值时 S≥0.37 即可入睡）
    "biorhythm_idle_to_sleep": 300,         # 靠作息入睡所需的安静时长（秒），即"没人发消息"多久
    "biorhythm_never_sleep": False,         # 永不会困；取消勾选时恢复启动时的精力值
    "biorhythm_rhythm_min_nights": 5,       # 积累多少晚后作息开始生效
    "biorhythm_rhythm_full_nights": 10,     # 积累多少晚后作息强度完全生效
    # ===== 动作抉择（本能冲动）=====
    "action_enabled": True,                 # 是否允许每轮末尾做动作抉择
    "action_max_pages": 4,                  # 单个动作类型的翻页上限
    "action_page_size": 8,                  # 每页候选数量
    "action_note_enabled": True,            # 是否允许写 txt 笔记
    "action_spontaneous_prob": 0.25,        # 没有新媒体时，每轮仍去问动作层的概率
    # ===== 笔记限额（满了拒绝写入，不做 FIFO 淘汰）=====
    "note_entry_max_chars": 200,            # 单条笔记字符上限
    "note_file_max_chars": 8000,            # 单个笔记文件字符上限
    "note_max_lines": 100,                  # 单个笔记文件条目上限
    "note_read_max_chars": 2000,            # 单次读回上限
    "note_brief_max_chars": 400,            # 动作层摘要总长
    "note_brief_recent_lines": 5,           # 动作层摘要每本最近条目数
    # ===== 短期记忆与“软件”行为 =====
    "short_term_thought_limit": 5,          # 本地短期记忆窗口：她自己最近 4~12 条想法
    "app_context_max_lines": 40,            # 插件传入主进程的软件上下文上限（未读、对话内容等）
    "qq_app_default_open": False,           # QQ 默认关闭：先累积未读，等动作层打开
    # ===== 电脑 / 虚拟桌面插件 =====
    "desktop_use_real_desktop": False,      # True=不启动虚拟工作桌面，直接操作真实 Windows 桌面
    "desktop_max_steps": 12,                # 单个电脑任务最多几步
    "desktop_task_timeout": 180,            # 单个电脑任务超时（秒）
    "desktop_thought_window": 10,           # 插件运行时把思考窗口设为 9~11 条
    "desktop_allow_screenshot": False,      # 是否允许操作手显式截图（默认不截图）
    "desktop_allow_coordinate_input": True, # 允许坐标鼠标/键盘（UIA 语义动作失败时兜底）
    "desktop_allow_synthetic_keys": True,   # 允许合成按键/键入（Enter、快捷键、剪贴板输入）
    "desktop_allow_shell_windows": True,    # 允许操作 Progman/WorkerW/任务栏等 shell 窗口（放行所有窗口）
    "desktop_coordinate_fence": True,       # 坐标必须落在目标窗口/工作桌面内
    "desktop_snapshot_bounds": True,        # 快照里附带元素屏幕矩形 @l,t,r,b
    "desktop_observe_software": True,       # 软件打开后由电脑插件读窗口内容
    "desktop_launch_mode": "auto",          # auto=已打开就复用镜像，否则新开；new=总是新开；reuse=只复用镜像
    "desktop_window_mode": "float",         # float=窗口浮在工作区内（宿主键鼠可输入）；embed=真子窗口（可能无法接收宿主键盘）
    "desktop_reuse_mode": "mirror",         # 复用已有窗口时：mirror=只复制画面；float=把真实窗口移进工作区（可交互）
    "desktop_wallpaper": "",                # 壁纸图片路径；留空用系统壁纸
    "desktop_notes_dirs": "",               # 桌面笔记目录（逗号/分号分隔）；留空=项目所在桌面、用户桌面/文档/下载
    "desktop_worker_width": 1280,           # 虚拟桌面窗口尺寸
    "desktop_worker_height": 800,
    # 以下仅作“以后外联 dsh”的预留，运行路径当前不读取：
    "desktop_backend": "local",
    "desktop_dsh_command": "",
    "desktop_dsh_launcher": "",
    "desktop_dsh_home": "",
    # ===== 本地软件门控（打开/退出软件，不参与语言回复）=====
    "app_router_mode": "rule",              # rule | local_llm | off
    "app_router_follow_global_behavior": False,  # 跟随全局 behavior_* 行为模型
    "app_router_base_url": "http://127.0.0.1:11434/v1",  # OpenAI 兼容接口：本地 Ollama 或网络模型均可
    "app_router_api_key": "ollama",             # 本地 Ollama 可填 ollama；网络模型填真实 Key
    "app_router_model": "",                 # local_llm 模式必填，例如 qwen2.5:3b
    "app_router_timeout": 20,
    "app_router_max_tokens": 160,
    "app_open_after_unread_rounds": 2,      # 未读持续几轮后打开软件
    "app_close_conversation_idle_rounds": 120,  # 对话空闲几轮后退出对话（默认放宽，避免聊天中途丢上下文）
    "app_close_idle_rounds": 240,            # 软件空闲几轮后退出软件
    "qq_unread_buffer_max": 200,            # 每个对话缓冲的未读消息上限（原始事件）
    "qq_reader_mode": "auto",               # auto | desktop | self：打开对话时谁来读窗口
    "qq_desktop_poll_seconds": 5,           # QQ 桌面读取：扫描窗口未读的间隔
    "qq_desktop_history_pages": 12,         # 打开对话时向上翻多少页加载历史
    "qq_desktop_use_vision": True,          # QQ 桌面读取：UIA 读不到时用视觉模型兜底
    # ===== QQ 富消息（小程序 / 合并转发聊天记录）=====
    "qq_mini_program_max_chars": 200,       # 小程序卡片解析后写入正文的最大字符数（不打开小程序）
    "qq_chat_record_max_messages": 80,      # 单条聊天记录最多展开的消息条数（只向内一层）
    "qq_chat_record_max_chars": 4000,       # 送进摘要 LLM 的聊天记录字符上限
    "qq_chat_record_summary_max_chars": 160,  # 摘要最大字符数（写入短期上下文的内容）
    # ===== 微信插件（本地桥接 WebSocket）=====
    "wechat_ws_host": "0.0.0.0",
    "wechat_ws_port": 6701,
    "wechat_token": "Nascence",
    "wechat_unread_buffer_max": 200,
    "wechat_history_limit": 20,
    # ===== Bilibili 插件 =====
    "bilibili_credentials_file": "config/bilibili_credentials.json",
    "bilibili_request_interval": 0.6,       # 请求间隔（秒）
    "bilibili_timeout": 20,
    "bilibili_feed_limit": 8,
    "bilibili_comments_page_size": 20,
    "bilibili_browse_cooldown_seconds": 1800,  # 关闭后多久再次出现“可浏览”提醒
    # ===== 浏览器插件（网页解析）=====
    "browser_mode": "static",               # static | dynamic
    "browser_timeout": 20,
    "browser_max_bytes": 2 * 1024 * 1024,
    "browser_max_chars": 20000,
    "browser_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    # 动态模式：DeepSeek Fetch 形态的网页解析接口
    "browser_dynamic_url": "https://api.deepseek.com/v1/tools/fetch",
    "browser_dynamic_api_key": "",          # 留空使用 primary_api_key
    "browser_dynamic_timeout": 30,
    # 摘要 LLM：留空字段使用 primary_*
    "browser_summary_api_key": "",
    "browser_summary_base_url": "",
    "browser_summary_model": "",
    "browser_summary_timeout": 30,
    "browser_summary_max_chars": 400,
    # ===== 媒体描述 =====
    "image_desc_max_chars": 60,             # 图片描述最大字符数
    "sticker_desc_max_chars": 30,           # 表情包描述最大字符数
    "media_desc_cache_size": 500,           # 图片描述内容哈希缓存条数
    # ===== 记忆写入 =====
    "memory_writer_mode": "process",        # process | thread | legacy（process 失败自动退回 thread）
    "memory_writer_batch": 100,             # 单批最多命令数
    "memory_writer_flush_ms": 100,          # 攒批最长等待毫秒
    "hot_planner_mode": "thread",           # process | thread（热集名单由独立进程/线程整理）
    # ===== 概念召回 =====
    "concept_recall_episodes": 5,           # 概念路每次带回的事件段数（回看类问题会额外补最早一段）
    # ===== 睡眠提示 =====
    "pre_sleep_margin": 0.04,               # 距入睡阈值多近时进入 wind-down
    "pre_sleep_hold_seconds": 60,           # wind-down 期间推迟入睡的秒数
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
            for k in DEFAULT_CONFIG:
                cfg.setdefault(k, DEFAULT_CONFIG[k])
    else:
        cfg = dict(DEFAULT_CONFIG)

    # 环境变量仅保留 Web 端口设置（其余配置全部收敛在控制面板内可视化管理）
    env_port = os.environ.get("PANEL_PORT")
    if env_port and str(env_port).strip():
        try:
            cfg["panel_port"] = int(str(env_port).strip())
        except ValueError:
            pass

    # 高级覆盖开关（可选，仅当显式设置 HUIYE_ENV_OVERRIDE=1 时生效）
    if os.environ.get("HUIYE_ENV_OVERRIDE") == "1":
        env_map = {
            "PRIMARY_API_KEY": "primary_api_key",
            "PRIMARY_BASE_URL": "primary_base_url",
            "PRIMARY_MODEL": "primary_model",
            "SECONDARY_API_KEY": "secondary_api_key",
            "SECONDARY_BASE_URL": "secondary_base_url",
            "SECONDARY_MODEL": "secondary_model",
            "BEHAVIOR_API_KEY": "behavior_api_key",
            "BEHAVIOR_BASE_URL": "behavior_base_url",
            "BEHAVIOR_MODEL": "behavior_model",
            "OLLAMA_BASE_URL": "ollama_base_url",
            "OLLAMA_EMBED_MODEL": "ollama_embed_model",
            "BOT_QQ": "bot_qq",
            "ACTIVE_GROUP_ID": "active_group_id",
            "NAPCAT_TOKEN": "napcat_token",
            "NAPCAT_HTTP_URL": "napcat_http_url",
        }
        for ek, ck in env_map.items():
            val = os.environ.get(ek)
            if val is not None and str(val).strip():
                cfg[ck] = str(val).strip()

    return cfg

def reload_config():
    """重读磁盘配置并原地更新全局 config，使运行中的模块立即感知最新设置。"""
    fresh = load_config()
    config.clear()
    config.update(fresh)
    return config

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

config = load_config()
