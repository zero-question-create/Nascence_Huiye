# qq_bot.py
# ========================================================================
# 功能：独立QQ接入模块 - WebSocket 服务器模式（NapCat 主动连接）
# 包含：白名单、姓名映射、CQ码清洗、@/引用解析、强制回复、门控静默
# ========================================================================

import asyncio
import base64
import json
import re
import threading
import websockets
import logging
import aiohttp
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import time
import random
from collections import deque
import datetime
import os
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
RUN_DIR = PROJECT_DIR / "run"
LOG_DIR = RUN_DIR / "logs"
for d in [LOG_DIR, PROJECT_DIR / "data" / "test"]:
    d.mkdir(parents=True, exist_ok=True)

_log_file = LOG_DIR / "qq_bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(str(_log_file), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

_is_shutting_down = False  # 标志是否处于优雅关停过程中

class _ShutdownHandshakeFilter(logging.Filter):
    """过滤关服瞬态由服务端主动关闭导致的握手失败噪音频出。"""
    def filter(self, record):
        if _is_shutting_down and ("opening handshake failed" in record.getMessage() or "server closing" in record.getMessage()):
            return False
        return True

logging.getLogger("websockets.server").addFilter(_ShutdownHandshakeFilter())

if sys.platform == "linux":
    local_lib = RUN_DIR / "lib" / "usr" / "lib" / "x86_64-linux-gnu"
    if local_lib.is_dir():
        os.environ.setdefault("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = f"{local_lib}:{os.environ['LD_LIBRARY_PATH']}".rstrip(":")
    local_qt = RUN_DIR / "qt5-plugins"
    if (local_qt / "platforminputcontexts").is_dir():
        os.environ["QT_PLUGIN_PATH"] = str(local_qt)
    os.environ.setdefault("QT_IM_MODULE", "fcitx")
    os.environ.setdefault("GTK_IM_MODULE", "fcitx")
    os.environ.setdefault("XMODIFIERS", "@im=fcitx")

from core.llm_interface import describe_image_from_path, describe_audio_from_path, describe_video_from_path, decompose_input
from utils.event_bus import BUS
from utils.metrics_probe import PROBE

# 导入你现有的核心模块
from core.cognition import inject_message_keywords, inject_keywords, cognitive_loop, extract_keywords_jieba, MODE_HALF_LIFE
from core.memory_engine import create_memory, access_memory, pathfind_activation, retrieve_similar, memories
from core.virtual_clock import clock
from core.biorhythm import BIORHYTHM
from config.constants import BOT_NAME
from config.api_config import config

# ---------- 配置常量 ----------
CONFIG_PATH = "config/qq_manifest.json"
WS_HOST = os.environ.get("WS_HOST", "0.0.0.0")
WS_PORT = int(os.environ.get("WS_PORT", 6700))  # NapCat 配置中填写的端口
WS_PATH = "/ws"


def _cfg_str(key, fallback):
    """读取最新配置并返回字符串值（支持运行时热更新）。"""
    try:
        value = config.get(key)
    except Exception:
        value = None
    return str(value or fallback)


def get_bot_qq():
    """机器人QQ号（动态读取，保存设置后无需重启即可生效）。"""
    return _cfg_str("bot_qq", "123456")


def get_active_group_id():
    """主动发言的目标群（动态读取，保存设置后无需重启即可生效）。"""
    return _cfg_str("active_group_id", "123456")


def get_napcat_token():
    """NapCat 鉴权 Token（动态读取，保存设置后无需重启即可生效）。"""
    return _cfg_str("napcat_token", "Nascence")


def get_napcat_http_url():
    """NapCat 正向 HTTP 服务地址（支持环境变量 NAPCAT_HTTP_URL 或动态配置，供语音下载等调用）。"""
    raw = os.environ.get("NAPCAT_HTTP_URL") or _cfg_str("napcat_http_url", "http://127.0.0.1:5700")
    raw = str(raw).strip().rstrip("/")
    if raw and not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw or "http://127.0.0.1:5700"


# 兼容旧引用（模块内部已改为动态读取）
BOT_QQ = get_bot_qq()
ACTIVE_GROUP_ID = get_active_group_id()
HTTP_ACCESS_TOKEN = get_napcat_token()
WS_ACCESS_TOKEN = get_napcat_token()
HTTP_API_BASE = get_napcat_http_url()

_recent_msg_list = []      # 有序存储 (sender_id, clean_text)
_recent_msg_set = set()    # 快速查找去重
MAX_RECENT_MESSAGES = 20

IGNORE_PREFIX = "#"     # 前缀特殊字符

_final_save_done = False            # 全局保存标识
_sleep_maintenance_done = False     # 本次睡眠是否已做过维护（避免重复全量落盘）
_napcat_websocket = None             # 当前连接的 NapCat 反向 WebSocket
_cognitive_task = None               # 永续认知循环 task
_shutdown_event = None               # 服务停止信号（在事件循环线程内 set）
_action_lock = asyncio.Lock()        # 串行发送 OneBot Action，避免响应混淆
_action_counter = 0
_pending_actions = {}                 # echo -> Future，用于匹配 NapCat Action 响应

# ========== 睡眠配置 ==========
# 睡眠由 core.biorhythm 的睡眠压力动力学 + 学得的作息决定（不硬编码钟点）
_sleeping = False               # 是否处于强制睡眠状态

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("QQBot")

# ---------- 加载映射表 ----------
class QQManifest:
    def __init__(self):
        try:
            # utf-8-sig：兼容 Windows 记事本等编辑器保存时写入的 UTF-8 BOM，
            # 否则文件首字符变成 \ufeff，json.load 会直接抛 JSONDecodeError。
            with open(CONFIG_PATH, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            self.whitelist = set(data.get("whitelist_groups", []))
            self.name_map = data.get("name_mapping", {})
            logger.info(f"映射表加载成功，白名单群组：{self.whitelist}")
        except FileNotFoundError:
            import shutil
            example_path = "config/qq_manifest.example.json"
            if os.path.exists(example_path):
                shutil.copy2(example_path, CONFIG_PATH)
                logger.info(f"已从 {example_path} 创建默认配置文件 {CONFIG_PATH}")
            else:
                with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                    json.dump({"whitelist_groups": [], "name_mapping": {}}, f, ensure_ascii=False, indent=2)
                logger.info(f"已创建空白的配置文件 {CONFIG_PATH}")
            logger.error("前往qq_manifest.json中修改群聊白名单")
            BUS.task_error.emit("前往qq_manifest.json中修改群聊白名单")
            with open(CONFIG_PATH, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            self.whitelist = set(data.get("whitelist_groups", []))
            self.name_map = data.get("name_mapping", {})
        except json.JSONDecodeError as e:
            # 配置文件语法损坏（多余逗号、缺引号等）。这里不再让整个 QQ 服务崩掉：
            # 退化为空名单并明确报错，用户改好文件重启即可。
            self.whitelist = set()
            self.name_map = {}
            msg = (
                f"{CONFIG_PATH} 解析失败（{e.msg}，第 {e.lineno} 行）："
                f"请检查 JSON 语法，注意不要有多余逗号或未转义的引号"
            )
            logger.error(msg)
            BUS.task_error.emit(msg)

manifest = QQManifest()

# ---------- 工具函数 ----------
def parse_cq_code(text: str) -> Tuple[str, List[str]]:
    """移除CQ码，提取被@的QQ号列表（兼容扩展字段如 text/name 以及参数顺序）"""
    # 匹配 [CQ:at,qq=123456] 或 [CQ:at,qq=123456,text=@xxx] 或 [CQ:at,text=@xxx,qq=123456] 等
    mentions = []
    for m in re.finditer(r'\[CQ:at,([^\]]+)\]', text):
        params_str = m.group(1)
        qq_match = re.search(r'(?:^|,)qq=(\w+)', params_str)
        if qq_match:
            mentions.append(qq_match.group(1))
    clean_text = re.sub(r'\[CQ:[^\]]+\]', '', text).strip()
    return clean_text, mentions


def build_augmented_input(sender_name: str, raw_text: str, mentions: List[str], group_id: str, is_mentioned_me: bool) -> str:
    """
    构建标准化增强输入（自然语言格式）
    """
    # 如果文本为空，给一个占位符（但正常情况下不会走到这里）
    text_content = raw_text if raw_text else "（没有说话）"
    
    if is_mentioned_me:
        return f'{sender_name}对{BOT_NAME}说：“{text_content}”'
    elif mentions:
        mapped_names = []
        for qq in mentions:
            name = manifest.name_map.get(group_id, {}).get(qq, qq)
            mapped_names.append(name)
        target_str = "、".join(mapped_names)
        return f'{sender_name}对{target_str}说：“{text_content}”'
    else:
        return f'{sender_name}说：“{text_content}”'

def extract_quote_message_id(message_segments: List[Dict]) -> Optional[str]:
    """从message数组中提取引用消息ID"""
    for seg in message_segments:
        if seg.get("type") == "reply":
            return seg.get("data", {}).get("id")
    return None

async def drift_loop():
    """后台发散任务：持续驱动生物钟，并随机激活记忆以强化半衰期、提供热身种子。

    冷热下沉统一由 MemoryWriter / HotSetPlanner 负责（见 core/memory_writer.py），
    这里不再重复调度，避免同一份淘汰工作被三处同时触发。
    """
    logger.info("[浅层意识] 发散任务已启动")
    while True:
        # 由生物钟自行判定入睡/醒来（不再依赖固定钟点）
        BIORHYTHM.tick()
        if BIORHYTHM.is_asleep() and not _sleeping:
            enter_sleep()
        elif not BIORHYTHM.is_asleep() and _sleeping:
            wake_up()

        # 素材暂存清理：与记忆无关，仍在这里按分钟级节流
        now = time.time()
        if now - getattr(drift_loop, "_last_cleanup", 0.0) >= 600:
            drift_loop._last_cleanup = now
            try:
                from core.asset_library import cleanup_pending
                cleanup_pending()
            except Exception as e:
                logger.error(f"[素材暂存] 清理异常: {e}")

        await asyncio.sleep(30)
        if BIORHYTHM.is_asleep():
            continue
        from core.memory_engine import get_random_memory_id
        mem_id = get_random_memory_id()
        if not mem_id:
            continue
        access_memory(mem_id)
        pathfind_activation([mem_id], max_stamina=1.0, top_k=3, max_steps=1)
        logger.info(f"[浅层意识] 激活记忆 {mem_id[:8]}...")

def is_sleeping() -> bool:
    """判断当前是否处于睡眠状态（由生物钟唯一判定）。

    历史问题：此前是 `_sleeping or BIORHYTHM.is_asleep()`，而 _sleeping 要等
    drift_loop 下一轮（最多 30 秒）才被清掉。认知循环自己 tick 到醒来后，
    这 30 秒里所有发送都会被误拦，表现为“醒了但话发不出去”。
    现在以生物钟为唯一权威，_sleeping 只做镜像，供日志与面板观测。
    """
    global _sleeping
    asleep = BIORHYTHM.is_asleep()
    _sleeping = asleep
    return asleep

def enter_sleep():
    """进入睡眠状态。

    睡眠维护（全量落盘 + faiss 重建，实测十几秒）放到线程池执行：
    它由 drift_loop 在事件循环里调用，同步跑会卡住收消息与生物钟 tick。
    睡眠期间 NapCat 必须保持连接——被 @ 唤醒全靠这条通道。
    """
    global _sleeping
    _sleeping = True
    logger.info(f"{BOT_NAME}进入了睡眠状态（NapCat 保持连接以便唤醒）")
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _sleep_maintenance_bg)
    except RuntimeError:
        # 无事件循环时（启动早期/CLI）同步执行
        _sleep_maintenance_bg()

def wake_up():
    """从睡眠中唤醒"""
    global _sleeping, _sleep_maintenance_done
    _sleeping = False
    _sleep_maintenance_done = False   # 下一觉需要重新做维护
    logger.info(f"{BOT_NAME}醒来了！")

def init_sleep_state():
    """启动时调用：只标记睡眠状态，不执行重量级维护。

    必须轻量：它跑在 WS 服务器启动之前，而睡眠维护（全量落盘 + faiss 重建）
    要十几秒。若在这里同步执行，NapCat 连不上来，被 @ 唤醒的通道就不存在了。
    真正的维护由 start_server 起来后异步补做（见 schedule_sleep_maintenance）。
    """
    global _sleeping
    if is_sleeping():
        _sleeping = True
        logger.info(f"{BOT_NAME}处于睡眠时段，等待 NapCat 连接以便被唤醒")
    else:
        logger.info(f"当前不在睡眠状态，{BOT_NAME}保持清醒。")


def schedule_sleep_maintenance():
    """在已有事件循环里异步补做睡眠维护（不阻塞消息收发）。

    睡眠期间 NapCat 必须保持连接（要被 @ 唤醒），因此维护只能放到线程池里跑。
    """
    if not is_sleeping():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有事件循环（如 CLI 路径），退化为同步执行
        enter_sleep()
        return
    loop.run_in_executor(None, _sleep_maintenance_bg)


def _sleep_maintenance_bg():
    """线程池执行的睡眠维护，异常只记日志，不影响服务。

    用 _sleep_maintenance_done 去重：同一觉只做一次，避免
    drift_loop 与 start_server 两条路径重复全量落盘。
    """
    global _sleep_maintenance_done
    if _sleep_maintenance_done:
        return
    _sleep_maintenance_done = True
    try:
        from utils.persistence import sleep_cleanup
        sleep_cleanup()
        logger.info("[睡眠维护] 链接剪枝、字词整理、全量保存已完成")
    except Exception as e:
        logger.error(f"[睡眠维护] 执行失败: {e}")

# ---------- 消息处理核心 ----------
# ---------- message_id 幂等 ----------
# NapCat 在重连、重试或事件重复投递时会把同一条消息再发一遍。
# 以 message_id 做短时效幂等，避免重复下载媒体、重复视觉调用、重复理解层。
_seen_message_ids = deque(maxlen=500)
_seen_message_id_set = set()
_SEEN_MESSAGE_TTL = 600.0
_seen_message_ts = {}


def _message_id_of(data: dict) -> str:
    value = data.get("message_id")
    if value is None:
        return ""
    return str(value).strip()


def _is_duplicate_message(message_id: str) -> bool:
    """同一条消息只处理一次。返回 True 表示应跳过。"""
    if not message_id:
        return False
    now = time.time()
    # 清理过期
    while _seen_message_ids:
        old = _seen_message_ids[0]
        if now - _seen_message_ts.get(old, 0) <= _SEEN_MESSAGE_TTL:
            break
        _seen_message_ids.popleft()
        _seen_message_id_set.discard(old)
        _seen_message_ts.pop(old, None)
    if message_id in _seen_message_id_set:
        return True
    _seen_message_ids.append(message_id)
    _seen_message_id_set.add(message_id)
    _seen_message_ts[message_id] = now
    return False


def _text_from_segments(message_segments) -> str:
    """从结构化消息段还原正文（raw_message 为空或只有 CQ 码时兜底）。"""
    if not isinstance(message_segments, list):
        return ""
    parts = []
    for seg in message_segments:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") == "text":
            text = seg.get("data", {}).get("text")
            if text:
                parts.append(str(text))
    return "".join(parts).strip()


def _media_phrase(media_type: str, desc: str) -> str:
    """把媒体类型+描述渲染成自然短语（历史与增强输入共用，保证口径一致）。"""
    if media_type == "sticker":
        return f"一个表情包，内容是“{desc}”"
    if media_type == "image":
        return f"一张图片，内容是“{desc}”"
    if media_type == "record":
        return f"一段录音，内容是“{desc}”"
    return f"一个视频，内容是“{desc}”"


# NapCat 给群表情包的 summary 常常只是占位词，不是内容描述。
_GENERIC_STICKER_SUMMARIES = {
    "动画表情", "表情", "表情包", "图片", "图片表情", "动态表情",
    "商城表情", "商城表情包", "图片消息", "gif", "mface",
}


def _sticker_desc_from_summary(summary: str) -> str | None:
    """官方摘要能否直接当描述用；占位词返回 None，表示必须走图片解析。

    之前只要摘要里带“表情”就直接采用，于是所有群表情包都叫“[动画表情]”：
    既让记忆里全是同一个描述，也会让素材库按描述把它们去重成一条。
    """
    text = str(summary or "").strip().strip("[]【】（）()").strip()
    if not text or text.lower() in _GENERIC_STICKER_SUMMARIES:
        return None
    return text[:30]


def _attach_media_memory_ids(media_list: list, memory_ids: list, media_refs: list = None):
    """把本轮纯媒体记忆的 ID 回填到对应的暂存条目上。

    动作层决定收藏时，需要这条 ID 才能在“图片记忆”和“我收藏了…”之间
    直接建因果边，让收藏贴着原图，而不是散落在记忆图别处。

    media_refs 与 media_list 一一对应（mface 这类没有暂存的条目为 None）；
    不用 pending 列表的尾部切片——中途可能有别的消息登记了新的暂存条目。
    """
    if not memory_ids or not media_refs:
        return
    try:
        from core import asset_library as ASSETS
        for ref, mem_id in zip(media_refs, memory_ids):
            if not ref:
                continue
            ASSETS.set_pending_memory_ids(ref, [mem_id])
    except Exception as e:
        logger.debug(f"媒体记忆 ID 回填失败: {e}")


# ---------- 回应目标 ----------
# 最近一条等待回应的消息。认知循环发言时据此决定 @ 谁、引用哪条。
_reply_target = None          # {"group_id","qq","name","message_id","ts","mention_me","quoted_me"}
_REPLY_TARGET_TTL = 180.0     # 3 分钟内的消息才视为“正在回应”
_last_at_time = {}            # {(group_id, qq): 上次 @ 的时间}，防刷屏
_quoted_messages = {}         # {(group_id, message_id): 上次引用的时间}，防重复引用
_QUOTED_MESSAGES_MAX = 128    # 引用记录上限，避免长期运行无限增长


def _remember_reply_target(group_id: str, qq: str, name: str, message_id: str,
                           mention_me: bool = False, quoted_me: bool = False):
    """记录本轮消息的回应目标（供认知循环 @ 或引用）。"""
    global _reply_target
    _reply_target = {
        "group_id": str(group_id),
        "qq": str(qq),
        "name": str(name or ""),
        "message_id": str(message_id or ""),
        "ts": time.time(),
        "mention_me": bool(mention_me),
        "quoted_me": bool(quoted_me),
    }


def take_reply_target(mention_only: bool = False, directed_only: bool = False) -> dict | None:
    """取出最近的回应目标；过期或不符合条件时返回 None。

    mention_only=True 时只返回“直接 @ 了机器人”的目标——普通群聊不主动 @ 人。
    directed_only=True 时返回“冲着机器人来的”目标：直接 @ 了它，或引用了它的话。
    """
    target = _reply_target
    if not target:
        return None
    if time.time() - target.get("ts", 0) > _REPLY_TARGET_TTL:
        return None
    if mention_only and not target.get("mention_me"):
        return None
    if directed_only and not (target.get("mention_me") or target.get("quoted_me")):
        return None
    return dict(target)


def _at_cooldown_ok(group_id: str, qq: str, cooldown: float = 30.0) -> bool:
    """同一个人 30 秒内最多被 @ 一次，避免连续回复时刷屏。"""
    key = (str(group_id), str(qq))
    now = time.time()
    last = _last_at_time.get(key, 0.0)
    if now - last < cooldown:
        return False
    _last_at_time[key] = now
    return True


def _quote_once_ok(group_id: str, message_id: str, ttl: float = _REPLY_TARGET_TTL) -> bool:
    """同一条消息在回应窗口内只引用一次。

    与 @ 的冷却不同，这里按 message_id 而不是按人：同一条消息回第二遍时
    再引用一次没有信息量，只会把聊天窗口挤满。
    """
    mid = str(message_id or "").strip()
    if not mid:
        return False
    key = (str(group_id), mid)
    now = time.time()
    if now - _quoted_messages.get(key, 0.0) < ttl:
        return False
    if len(_quoted_messages) >= _QUOTED_MESSAGES_MAX:
        for old_key in [k for k, ts in _quoted_messages.items() if now - ts >= ttl]:
            _quoted_messages.pop(old_key, None)
        # 清理后仍满（短时间内消息极多）：丢最早的一半，保证有界
        if len(_quoted_messages) >= _QUOTED_MESSAGES_MAX:
            for old_key in list(_quoted_messages)[: _QUOTED_MESSAGES_MAX // 2]:
                _quoted_messages.pop(old_key, None)
    _quoted_messages[key] = now
    return True


def _resolve_at_mentions(text: str, group_id: str) -> tuple:
    """把文本里的 @名字 解析成 OneBot at 消息段所需的信息。

    只在名字命中群成员映射（含昵称）时才转换；未知名字保持原文，
    绝不猜测 QQ 号。邮箱、URL、代码块里的 @ 一律忽略。

    返回 (qq_list, cleaned_text)。
    """
    if not text or "@" not in text:
        return [], text
    mapping = manifest.name_map.get(str(group_id), {}) or {}
    if not mapping:
        return [], text

    name_to_qq = {}
    for qq, name in mapping.items():
        if name:
            name_to_qq[str(name)] = str(qq)

    resolved = []
    cleaned = text
    # 用“@名字”最长匹配，避免 “@小王” 抢先匹配 “@小王同学” 的前缀
    for name in sorted(name_to_qq, key=len, reverse=True):
        token = f"@{name}"
        if token not in cleaned:
            continue
        pattern = re.escape(token) + r"(?![0-9A-Za-z_\u4e00-\u9fff])"
        if not re.search(pattern, cleaned):
            continue
        qq = name_to_qq[name]
        if qq == get_bot_qq() or qq == "all":
            continue
        resolved.append(qq)
        cleaned = re.sub(pattern, "", cleaned, count=1)
    return resolved, cleaned.strip()


async def handle_group_message(data: dict):
    """处理单条群消息

    睡眠期间的策略：未被 @ 的消息直接忽略；被 @ 则唤醒后正常回应。
    """
    import time as _time
    _started = _time.perf_counter()
    try:
        await _handle_group_message_impl(data)
    finally:
        PROBE.record_ms("message.handle_ms", (_time.perf_counter() - _started) * 1000.0)


async def _handle_group_message_impl(data: dict):
    group_id = str(data.get("group_id"))
    
    # 白名单过滤
    if group_id not in manifest.whitelist:
        logger.debug(f"群 {group_id} 不在白名单，忽略")
        return

    # 幂等：同一条消息重复投递时直接跳过，避免重复媒体下载与 LLM 调用
    message_id = _message_id_of(data)
    if _is_duplicate_message(message_id):
        logger.info(f"重复消息已跳过（message_id={message_id}）")
        return

    # 记录外部活动：作息惯性靠"安静多久"判断是否容易入睡。
    # 只认群消息，不认她自己的发言——认知循环每几秒就自转一次，
    # 若把自己的念头算作活动，她永远睡不着。
    try:
        BIORHYTHM.note_activity()
    except Exception:
        pass

    sender_id = str(data.get("user_id"))
    # 发送人判定：优先配置映射名 → 群昵称(card) → QQ昵称(nickname)
    sender_name = manifest.name_map.get(group_id, {}).get(sender_id)
    if sender_name:
        logger.debug(f"发送人命中配置映射: {sender_id} -> {sender_name}")
    else:
        card = data.get("sender", {}).get("card")
        nickname = data.get("sender", {}).get("nickname")
        if card:
            sender_name = card
        elif nickname:
            sender_name = nickname
        else:
            # 群昵称和QQ昵称均缺失（NapCat 应始终提供 nickname），忽略该消息
            logger.warning(f"发送人 {sender_id} 无昵称信息，忽略该消息")
            return

    raw_str = data.get("raw_message", "")
    clean_text, mentions = parse_cq_code(raw_str)
    # raw_message 可能只含 CQ 码（体积消息、多段消息），此时从结构化消息段补回正文。
    # 否则文本+图片的复合消息会被误判成“纯媒体”，正文永久丢失。
    segment_text = _text_from_segments(data.get("message", []))
    if not clean_text and segment_text:
        clean_text, seg_mentions = parse_cq_code(segment_text)
        for qq in seg_mentions:
            if qq not in mentions:
                mentions.append(qq)
        if not clean_text:
            clean_text = segment_text

    # 从结构化 message 数组补充 @ 目标（处理 NapCat 未以标准 CQ 码上报或多段消息）
    for seg in data.get("message", []):
        if seg.get("type") == "at":
            at_qq = str(seg.get("data", {}).get("qq", "")).strip()
            if at_qq and at_qq not in mentions:
                mentions.append(at_qq)

    # 检查纯文本手打 @（如 "@辉夜"）或 @全体成员
    bot_qq = get_bot_qq()
    text_has_bot_name = f"@{BOT_NAME}" in raw_str or f"@{BOT_NAME}" in clean_text
    is_mentioned_me = (bot_qq in mentions) or ("all" in mentions) or text_has_bot_name

    # 消息前缀忽略
    if clean_text.lstrip().startswith(IGNORE_PREFIX):
        logger.info(f"消息以忽略前缀 '{IGNORE_PREFIX}' 开头，忽略")
        return
    elif IGNORE_PREFIX in clean_text:           # 包含即忽略
        logger.info(f"消息包含忽略字符 '{IGNORE_PREFIX}' ，忽略")
        return
    clean_text = clean_text.replace(f"@{BOT_NAME}", "").strip()
    if clean_text:
        BUS.message.emit(sender_name, clean_text, "QQ")

    if is_mentioned_me:
        mentions = [m for m in mentions if m != bot_qq and m != "all"]

    # 解析说话对象：@机器人 -> BOT_NAME；@其他人 -> 映射名称列表
    target_name = None
    if is_mentioned_me:
        target_name = BOT_NAME
    elif mentions:
        mapped_names = []
        for qq in mentions:
            name = manifest.name_map.get(group_id, {}).get(qq, qq)
            mapped_names.append(name)
        target_name = "、".join(mapped_names)

    # 睡眠判定（在解析出 @ 之后）：未被 @ 则忽略；被 @ 则唤醒后继续回应
    if is_sleeping():
        if is_mentioned_me:
            logger.info(f"{BOT_NAME}正在睡觉，被 @ 唤醒")
            BIORHYTHM.wake("qq@")
            wake_up()   # 同步模块级睡眠标志，避免后续消息仍被当作睡眠期丢弃
        else:
            logger.debug(f"{BOT_NAME}正在睡觉，消息未被 @，忽略")
            return

    # 多模态处理
    # 媒体描述改为并发执行（最多 2 个），避免一条多图消息串行等待视觉模型；
    # 同时保持 media_list 顺序与消息段顺序一致。
    media_list = []  # 收集 (type, description)
    media_refs = []  # 与 media_list 对齐的暂存引用（无暂存时为 None）
    media_tasks = [] # [(order, coroutine)]
    _media_sem = asyncio.Semaphore(2)
    _MAX_MEDIA_DESC = 3   # 单条消息最多描述 3 个媒体段，其余只记数量

    async def _describe_one(order, seg_type, seg_data, tmp_path, file_format, owns_tmp_path):
        try:
            async with _media_sem:
                if seg_type == "image":
                    chk_gif = file_format == "gif"
                    chk_mface = bool(seg_data.get("emoji_id") or seg_data.get("emoji_package_id") or seg_data.get("key"))
                    summary = str(seg_data.get("summary") or "").strip()
                    chk_summary = "表情" in summary
                    chk_subtype = str(seg_data.get("sub_type")) == "1"
                    is_sticker = chk_gif or chk_mface or chk_summary or chk_subtype
                    media_type = "sticker" if is_sticker else "image"
                    # 有真正的官方摘要才省掉视觉调用；占位摘要必须走图片解析
                    desc = _sticker_desc_from_summary(summary) if (is_sticker and not chk_gif) else None
                    if desc is None:
                        max_chars = 30 if is_sticker else 60
                        desc = await describe_image_from_path(tmp_path, max_chars=max_chars)
                    if is_sticker and (not desc or desc.startswith("[图片识别失败") or desc.startswith("[图片文件不存在")):
                        desc = _sticker_desc_from_summary(summary) or "表情包"
                elif seg_type == "record":
                    media_type = "record"
                    desc = await describe_audio_from_path(tmp_path)
                else:
                    media_type = "video"
                    desc = await describe_video_from_path(tmp_path)
                return (order, media_type, desc, tmp_path, owns_tmp_path)
        except Exception as e:
            logger.warning(f"处理媒体失败: {e}")
            return (order, None, None, tmp_path, owns_tmp_path)

    for seg in data.get("message", []):
        seg_type = seg.get("type")
        if seg_type not in ("image", "record", "video", "mface"):
            continue
        seg_data = seg.get("data", {})

        if seg_type == "mface":
            # 商城表情包（NapCat 直报 mface）：没有可下载的图，只能用摘要；
            # 摘要若只是“[动画表情]”这类占位词，退化为中性描述。
            summary = str(seg_data.get("summary") or seg_data.get("key") or "")
            media_list.append(("sticker", _sticker_desc_from_summary(summary) or "商城表情包"))
            media_refs.append(None)
            continue

        # 超出描述上限的媒体不再下载：只记录数量，避免一条消息打爆视觉调用
        if len(media_tasks) + len(media_list) >= _MAX_MEDIA_DESC:
            continue

        # NapCat 常直接给出本地文件路径（QQ 缓存目录）。命中时优先复制本地文件，
        # 不再走网络下载，省掉一次远程取图。
        local_file = None
        for key in ("file", "path"):
            candidate = seg_data.get(key)
            if candidate and not str(candidate).startswith(("http://", "https://", "base64://")) and os.path.isfile(str(candidate)):
                local_file = str(candidate)
                break

        file_url = seg_data.get("url") or seg_data.get("path") or seg_data.get("file")
        if seg_type == "record" and file_url and not str(file_url).startswith(("http://", "https://", "base64://")):
            # NapCat 有时只回传文件名或本地路径，通过 get_record 获取可下载文件。
            try:
                napcat_api = get_napcat_http_url()
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{napcat_api}/get_record",
                        params={"file": file_url, "out_format": "wav"},
                        headers={"Authorization": f"Bearer {get_napcat_token()}"},
                        timeout=30,
                    ) as resp:
                        if resp.status == 200:
                            record_result = await resp.json()
                            file_url = record_result.get("data", {}).get("file") or file_url
            except Exception as e:
                logger.warning(f"通过 NapCat 获取语音失败: {e}")

        # 跨容器收图兜底：若既不是本地直接可读文件，也不是 HTTP 直链或 Base64，
        # 则尝试调用 NapCat 的 /get_image 接口获取下载直链或文件路径
        if not local_file and (not file_url or not str(file_url).startswith(("http://", "https://", "base64://"))):
            if seg_type in ("image", "sticker"):
                try:
                    napcat_api = get_napcat_http_url()
                    file_param = seg_data.get("file") or file_url or ""
                    if file_param:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                f"{napcat_api}/get_image",
                                params={"file": file_param},
                                headers={"Authorization": f"Bearer {get_napcat_token()}"},
                                timeout=30,
                            ) as resp:
                                if resp.status == 200:
                                    img_result = await resp.json()
                                    fetched = img_result.get("data", {}).get("url") or img_result.get("data", {}).get("file")
                                    if fetched:
                                        file_url = fetched
                except Exception as e:
                    logger.warning(f"通过 NapCat 获取图片失败: {e}")

        if not local_file and (not file_url or not str(file_url).startswith(("http://", "https://", "base64://"))):
            if not file_url or not os.path.isfile(str(file_url)):
                logger.warning(f"媒体没有可下载的 URL 或本地文件，类型={seg_type}")
                continue
        tmp_path = None
        owns_tmp_path = False
        try:
            parsed_url = urlparse(str(file_url or local_file))
            query_params = parse_qs(parsed_url.query)
            default_format = os.path.splitext(str(file_url or local_file))[1].lstrip(".") or seg_data.get("format", "amr")
            file_format = query_params.get('format', [default_format])[0]
            file_format = re.sub(r"[^a-zA-Z0-9]", "", file_format).lower() or "amr"

            if local_file:
                # NapCat 已把文件落在本地（同机部署环境），直接读，不复制不下载
                tmp_path = local_file
                owns_tmp_path = False
            elif str(file_url).startswith("base64://"):
                # Base64 编码的二进制数据，直接在内存解码为临时文件
                b64_str = str(file_url)[len("base64://"):]
                raw_bytes = base64.b64decode(b64_str)
                with tempfile.NamedTemporaryFile(delete=False, suffix=f".{file_format}") as tmp_file:
                    tmp_file.write(raw_bytes)
                    tmp_path = tmp_file.name
                    owns_tmp_path = True
            elif str(file_url).startswith(("http://", "https://")):
                async with aiohttp.ClientSession() as session:
                    async with session.get(file_url, timeout=30) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"下载媒体失败，HTTP {resp.status}")
                        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{file_format}") as tmp_file:
                            tmp_file.write(await resp.read())
                            tmp_path = tmp_file.name
                            owns_tmp_path = True
            else:
                tmp_path = str(file_url)
        except Exception as e:
            logger.warning(f"媒体准备失败: {e}")
            if tmp_path and owns_tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            continue

        media_tasks.append(_describe_one(len(media_tasks), seg_type, seg_data, tmp_path, file_format, owns_tmp_path))

    if media_tasks:
        described = await asyncio.gather(*media_tasks)
        for _order, media_type, desc, tmp_path, owns_tmp_path in sorted(described, key=lambda x: x[0]):
            if not media_type or not desc:
                continue
            media_list.append((media_type, desc))
            ref = None
            # 图片/表情包登记为暂存，供本轮动作抉择决定是否收藏
            # （必须在临时文件删除之前登记：暂存会复制一份副本）
            if media_type in ("image", "sticker"):
                try:
                    from core.asset_library import register_pending
                    ref = register_pending(media_type, tmp_path, desc)
                    if ref:
                        logger.info(f"媒体已登记暂存（{ref}）: {desc[:30]}")
                except Exception as e:
                    logger.warning(f"登记暂存媒体失败: {e}")
            media_refs.append(ref)
            if tmp_path and owns_tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    logger.info(f"临时文件删除失败，请及时清理：{tmp_path}")

    # 媒体描述异步执行，期间事件循环仍在处理其他消息；这里更新探针便于观测
    PROBE.bump("message.media_count", len(media_list))

    # 若为纯多媒体消息（无正文文字），向事件总线广播媒体摘要供面板显示
    if not clean_text and media_list:
        first_type, first_desc = media_list[0]
        BUS.message.emit(sender_name, f"[{first_type}] {first_desc}", "QQ")

    # ---------- 构建自然语言增强输入 ----------
    if media_list:
        # 生成媒体部分的自然描述
        media_parts = []
        for m_type, d in media_list:
            if m_type == "sticker":
                media_parts.append(f"一个表情包，内容是“{d}”")
            elif m_type == "image":
                media_parts.append(f"一张图片，内容是“{d}”")
            elif m_type == "record":
                media_parts.append(f"一段录音，内容是“{d}”")
            elif m_type == "video":
                media_parts.append(f"一个视频，内容是“{d}”")
        media_text = "、".join(media_parts)  # 用顿号分隔多个媒体
        
        target_clause = f"对{target_name}" if target_name else ""
        if clean_text:  # 有文字伴随
            augmented_input = f"{sender_name}{target_clause}说：“{clean_text}”，同时发送了{media_text}"
        else:           # 纯媒体
            augmented_input = f"{sender_name}{target_clause}发送了{media_text}"
    else:
        # 没有媒体，沿用原有文本格式（带@和引号）
        augmented_input = build_augmented_input(
            sender_name, clean_text, mentions, group_id, is_mentioned_me
        )

    # 消息去重（基于发送者+内容）
    msg_key = (sender_id, clean_text)
    if msg_key in _recent_msg_set and False:            # 暂时关闭重复判定
        logger.info(f"消息疑似刷量（同发送者重复），忽略: {sender_name}: {clean_text}")
        return
    
    # 加入记录
    _recent_msg_list.append(msg_key)
    _recent_msg_set.add(msg_key)
    
    # 超出容量时移除最旧的
    while len(_recent_msg_list) > MAX_RECENT_MESSAGES:
        old_key = _recent_msg_list.pop(0)
        _recent_msg_set.discard(old_key)

    # 空消息守卫：既无正文、也无媒体、也没有 @ 任何人的事件（如撤回、系统提示）
    # 直接丢弃，绝不把“（没有说话）”送进理解层浪费一次 LLM。
    if not clean_text and not media_list and not mentions and not is_mentioned_me:
        logger.debug(f"空消息（无文本/无媒体/无@），已忽略: sender={sender_name}")
        PROBE.bump("message.empty_dropped")
        return

    # 纯@无文本（且无多媒体）→ 简单回复，记录对话并通知，不存入深层记忆
    if is_mentioned_me and not clean_text and not media_list and not mentions:
        # 判断是否处于刚被吵醒的迷糊锁定状态
        if BIORHYTHM.woke_by_user and time.time() < BIORHYTHM.awake_until:
            wake_reply = random.choice([
                "唔……刚被叫醒，脑子还迷迷糊糊的，怎么啦？",
                "嗯……还在犯困呢，找我有什么事吗？",
                "哈欠……醒了醒了，怎么啦？"
            ])
        else:
            wake_reply = random.choice([
                "嗯？我在呢~",
                "怎么啦？我在这呢。",
                "在的，找我有什么事吗？"
            ])
        # 被叫的人就在这一句里，直接 @ 回去并引用这条呼叫，避免对方不知道在跟谁说话。
        # 兼容旧发送签名：自定义/测试发送函数可能不接受新参数，退化为普通发送。
        try:
            await send_group_msg(group_id, wake_reply, at_qq=sender_id,
                                 reply_msg_id=message_id or None)
        except TypeError:
            await send_group_msg(group_id, wake_reply)
        from core.llm_interface import add_to_history
        add_to_history(sender_name, "（叫了辉夜一声）", wake_reply, "QQ", target=BOT_NAME)
        BUS.message.emit(BOT_NAME, wake_reply, "QQ")
        return

    # 提取引用消息
    quote_id = extract_quote_message_id(data.get("message", []))
    extra_context = ""
    quote_info = None
    quoted_me = False          # 本条消息引用的是不是机器人自己说过的话
    if quote_id:
        quoted_message = await fetch_quoted_message(quote_id, group_id)
        if quoted_message:
            quoted_sender, quoted_text, quoted_sender_id = quoted_message
            quoted_me = (quoted_sender_id == get_bot_qq())
            extra_context = f"{sender_name}引用了{quoted_sender}之前的一句话：'{quoted_text}'"
            # 结构化引用随历史一起保存，供短期上下文还原引用关系
            quote_info = {"sender": quoted_sender, "text": quoted_text}

    logger.info(f"输入: {augmented_input} | 上下文: {extra_context}")

    from core.memory_engine import create_memory, add_link
    from core.virtual_clock import clock
    from core.llm_interface import add_to_history
    from utils.dialogue_state import set_state
    from utils.persistence import save_state

    # 记录本轮的回应目标：被 @ 时直接 @ 回发送者，被引用时引用回原消息；
    # 其余情况既不 @ 人也不引用。认知循环发言时从这里取“最近一条待回应的目标”。
    _remember_reply_target(group_id, sender_id, sender_name, message_id,
                           mention_me=is_mentioned_me, quoted_me=quoted_me)

    media_text = "、".join(_media_phrase(m_type, d) for m_type, d in media_list)

    user_mem_ids = []
    # ========== 纯媒体快捷路径 ==========
    # 没有正文的图片/表情包无需走理解层：视觉描述已经是完整信息。
    # 这里直接写记忆 + 提取关键词，省掉一次 LLM 调用与一轮“（没有说话）”垃圾记忆。
    if media_list and not clean_text:
        half_life = 2 * 24 * 3600
        target_clause = f"对{target_name}" if target_name else ""
        for m_type, desc in media_list:
            frag = f"{sender_name}{target_clause}发送了{_media_phrase(m_type, desc)}"
            try:
                user_mem_ids.append(create_memory(frag, half_life=half_life))
            except Exception as e:
                logger.warning(f"媒体记忆写入失败: {e}")
        media_keywords = extract_keywords_jieba(media_text)
        if media_keywords:
            from core.cognition import inject_keywords
            inject_keywords(media_keywords, "media")
        # 媒体描述对应的记忆 ID 回填给暂存条目：收藏时据此贴近原图建链
        _attach_media_memory_ids(media_list, user_mem_ids, media_refs)
        history_content = f"（发送了{media_text}）"
        add_to_history(sender_name, history_content, None, "QQ", quote=quote_info, target=target_name)
        logger.info(f"纯媒体消息：0 次理解层，记忆入库 {len(user_mem_ids)} 条")
        BUS.message.emit(BOT_NAME, f"[思考中...]", "QQ")
        return

    full_input = augmented_input
    if extra_context:
        full_input = f"{extra_context}\n{augmented_input}"

    # ========== 理解层：在解析单线程里执行，保证消息顺序且不阻塞事件循环 ==========
    from core.parse_worker import run_in_parse_worker

    def _parse_pipeline():
        """理解层 + 记忆入库 + 概念归档，整段在解析线程里串行执行。

        返回 (mem_fragments, mode, new_state, keywords, concept, time_intent, mem_ids)
        """
        fragments, parsed_mode, parsed_state, parsed_kws, parsed_concept, parsed_time = decompose_input(full_input)
        from core.memory_engine import create_memory as _create
        mem_ids = []
        life = MODE_HALF_LIFE.get(parsed_mode, 2 * 24 * 3600)
        for frag in fragments:
            try:
                mem_ids.append(_create(frag, half_life=life))
            except Exception as exc:
                logger.warning(f"记忆写入失败: {exc}")
        if parsed_concept and mem_ids:
            try:
                from core.concept_store import record_concept
                record_concept(parsed_concept, mem_ids)
            except Exception as exc:
                logger.warning(f"概念层写入失败: {exc}")
        return fragments, parsed_mode, parsed_state, parsed_kws, parsed_concept, parsed_time, mem_ids

    (mem_fragments, mode, new_state, llm_keywords,
     concept, time_intent, user_mem_ids) = await run_in_parse_worker(_parse_pipeline)

    # 更新对话状态（写 JSON，轻量，保持原位）
    if new_state:
        set_state(new_state)
        save_state()

    # 时间指代：交给认知循环，供概念路做时间窗筛选（不做词表解析）
    if time_intent and time_intent != "none":
        try:
            from core.cognition import inject_time_intent
            inject_time_intent(time_intent)
        except Exception as e:
            logger.warning(f"时间指代注入失败: {e}")

    # 将关键词追加进信箱：LLM 提取词 + 正文 jieba 词 + 媒体描述词
    # （旧实现丢弃了 LLM 关键词、只用 jieba；这里两者合并，来源可追溯）
    from core.cognition import inject_keywords
    merged_keywords = []
    for kw in (llm_keywords or []):
        text = str(kw or "").strip()
        if text:
            merged_keywords.append(text)
    for kw in extract_keywords_jieba(clean_text):
        if kw not in merged_keywords:
            merged_keywords.append(kw)
    for kw in extract_keywords_jieba(media_text):
        if kw not in merged_keywords:
            merged_keywords.append(kw)
    if merged_keywords:
        inject_keywords(merged_keywords, "msg")
        # 兼容旧入口（也进消费队列；信箱合并时会按内容去重）
        inject_message_keywords(merged_keywords)

    # 记录用户消息到对话历史（回复由 cognitive_loop 异步补充）
    # 引用与提及对象以结构化字段随消息保存，短期上下文才能还原"对谁说/在回应什么"
    history_content = clean_text.strip()
    if media_list:
        if history_content:
            history_content = f"{history_content}（发送了{media_text}）"
        else:
            history_content = f"（发送了{media_text}）"
    elif not history_content:
        history_content = "（没有说话）"

    add_to_history(sender_name, history_content, None, "QQ", quote=quote_info, target=target_name)

    logger.info(f"理解层完成，记忆入库 {len(user_mem_ids)} 条，关键词: {merged_keywords}")
    BUS.message.emit(BOT_NAME, f"[思考中...]", "QQ")

SEND_ACK_TIMEOUT = 5.0   # 等待 NapCat 回执的秒数；超时即视为未送达
MEDIA_ACK_TIMEOUT = 15.0  # 图片/表情包要经 NapCat 上传到 QQ，5 秒经常不够


async def _send_action_and_wait(websocket, payload: dict, echo: str,
                                timeout: float = None) -> bool:
    """发送 OneBot Action 并等待 NapCat 回执，返回是否确认成功。

    与 fetch_quoted_message 复用同一套 echo→Future 机制（ws_handler 主循环
    已在认领所有带 echo 的响应），因此不需要另开子进程或额外连接。
    仅挂起当前协程，事件循环与其他消息处理照常运行。

    判定标准：response['status'] == 'ok'。超时/断线/异常一律视为未送达。
    失败时把 NapCat 的 status/retcode/message 记下来——只写"未送达"时，
    图片发不出去根本看不出是文件、格式还是尺寸被拒。
    """
    wait = SEND_ACK_TIMEOUT if timeout is None else timeout
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    try:
        async with _action_lock:
            _pending_actions[echo] = future
            await websocket.send(json.dumps(payload, ensure_ascii=False))
        response = await asyncio.wait_for(future, timeout=wait)
        ok = str(response.get("status") or "").lower() == "ok"
        if not ok:
            detail = str(response.get("message") or "")
            hint = ""
            if "EACCES" in detail or "permission denied" in detail.lower():
                hint = (
                    "（NapCat 无法写入自己的 temp 目录：检查 /app/.config/QQ/NapCat/temp "
                    "的属主与权限，或给 NapCat 容器设置 NAPCAT_UID/NAPCAT_GID）"
                )
            logger.warning(
                f"NapCat 拒绝发送（echo={echo}，status={response.get('status')}，"
                f"retcode={response.get('retcode')}，message={detail[:200]}）{hint}"
            )
        return ok
    except asyncio.TimeoutError:
        logger.warning(f"发送未收到回执（echo={echo}，超时 {wait:g}s），按未送达处理")
        return False
    except Exception as e:
        logger.error(f"发送等待回执异常（echo={echo}）: {e}")
        return False
    finally:
        _pending_actions.pop(echo, None)


async def send_group_msg(group_id: str, text: str, reply_msg_id: int = None, at_qq=None) -> bool:
    """
    发送群消息，支持引用回复与 @。

    reply_msg_id: 被引用消息的 message_id（整数）
    at_qq: 要 @ 的 QQ 号（单个字符串/整数，或多个的列表）。
           只接受数字 QQ；机器人自己与 "all" 会被剥离。
    返回：是否确认送达（NapCat 回执 status=ok 才算成功）

    调用方必须依据返回值决定是否记入历史：此前无条件记"我说"，
    发送失败时会造成"说过但其实没说出口"的虚假记忆（F20）。
    """
    if is_sleeping():
        logger.debug("睡眠期间禁止发送消息，已拦截。")
        return False
    websocket = _napcat_websocket
    if websocket is None:
        logger.error("消息发送失败：当前没有连接的 NapCat WebSocket")
        return False

    # 文本中的 @名字 先解析成 at 段（只认群成员映射，绝不猜 QQ 号）
    parsed_at, text = _resolve_at_mentions(str(text or ""), group_id)
    # 只 @ 不说话时 NapCat 容易拒绝空文本段：给一个空格兜底
    if not text and parsed_at:
        text = " "

    # 显式 at_qq 与文本解析出的 @ 合并去重
    at_list = list(parsed_at)
    if at_qq is not None:
        candidates = at_qq if isinstance(at_qq, (list, tuple, set)) else [at_qq]
        for item in candidates:
            qq = str(item or "").strip()
            if not qq or qq == get_bot_qq() or qq == "all":
                continue
            if not qq.isdigit():
                continue
            if qq not in at_list:
                at_list.append(qq)

    global _action_counter
    _action_counter += 1
    echo = f"nascence-send-{_action_counter}"

    if reply_msg_id is not None or at_list:
        # 结构化消息段：引用 + @ + 正文。比 CQ 码更稳，也避免正文里出现 CQ 语法时被误解析。
        segments = []
        if reply_msg_id is not None:
            segments.append({"type": "reply", "data": {"id": str(reply_msg_id)}})
        for qq in at_list:
            segments.append({"type": "at", "data": {"qq": qq}})
        segments.append({"type": "text", "data": {"text": (" " if at_list else "") + text}})
        message_payload = segments
    else:
        message_payload = text

    payload = {
        "action": "send_group_msg",
        "params": {
            "group_id": int(group_id),
            "message": message_payload,
        },
        "echo": echo,
    }

    ok = await _send_action_and_wait(websocket, payload, echo)
    if ok:
        logger.info(f"已发送并确认送达（echo={echo}，引用={reply_msg_id}，@={at_list}）: {text}")
        from core.memory_engine import _bump_message_sent, _bump_self_ref
        _bump_message_sent()
        if "我" in text:
            _bump_self_ref()
    else:
        logger.warning(f"消息未确认送达（echo={echo}）: {text}")
    return ok

async def send_group_media(group_id: str, file_path: str, caption: str = "", as_sticker: bool = False) -> bool:
    """发送本地图片/表情包（消息段数组形式）。成功返回 True。

    as_sticker=True 时按表情包发送：NapCat 会把该消息段的 sub_type 传给
    createValidSendPicElement，最终落到 QQ 的 picSubType 字段——这才是 QQ
    区分"普通图片"与"收藏表情"的开关。只把文件改成 .gif 并不足以让它显示为表情包。
    """
    if is_sleeping():
        logger.debug("睡眠期间禁止发送媒体，已拦截。")
        return False
    global _action_counter
    websocket = _napcat_websocket
    if websocket is None:
        logger.error("媒体发送失败：当前没有连接的 NapCat WebSocket")
        return False
    if not file_path or not os.path.isfile(str(file_path)):
        logger.error(f"媒体发送失败：文件不存在 {file_path}")
        return False

    segments = []
    if caption:
        segments.append({"type": "text", "data": {"text": caption}})

    # 跨容器支持：采用 base64:// 协议发送二进制图片，完全不依赖容器间共享文件系统，
    # NapCat 无论在宿主机、其他容器或远程均可从 WebSocket 内存解码并上传至 QQ。
    try:
        file_size = os.path.getsize(str(file_path))
        if file_size <= 0:
            logger.error(f"媒体发送失败：文件为空 {file_path}")
            return False
        with open(str(file_path), "rb") as f:
            b64_content = base64.b64encode(f.read()).decode("utf-8")
        image_data = {"file": f"base64://{b64_content}"}
        logger.info(
            f"发送媒体：{os.path.basename(str(file_path))}"
            f"（{file_size / 1048576:.2f} MB，base64 {len(b64_content) / 1048576:.2f} MB，"
            f"sticker={as_sticker}）"
        )
    except Exception as e:
        logger.error(f"媒体 Base64 编码失败 ({file_path}): {e}")
        return False

    if as_sticker:
        # OneBot 图片子类型 1 = 表情包，NapCat 会映射为 picSubType
        image_data["sub_type"] = 1
    segments.append({"type": "image", "data": image_data})

    _action_counter += 1
    echo = f"nascence-media-{_action_counter}"
    payload = {
        "action": "send_group_msg",
        "params": {"group_id": int(group_id), "message": segments},
        "echo": echo,
    }
    # 与文本发送一样等待回执：此前只判断"本地 send 未抛异常"就返回 True，
    # NapCat 拒绝时也会被记为发送成功（F20）。
    ok = await _send_action_and_wait(websocket, payload, echo, timeout=MEDIA_ACK_TIMEOUT)
    if ok:
        logger.info(f"已发送并确认送达媒体（echo={echo}）: {file_path}")
        from core.memory_engine import _bump_message_sent
        _bump_message_sent()
    else:
        logger.warning(f"媒体未确认送达（echo={echo}）: {file_path}")
    return ok

async def fetch_quoted_message(msg_id: str, fallback_group_id: str = "") -> Optional[Tuple[str, str, str]]:
    """通过 WebSocket 获取引用消息，返回（原发送者名称，消息正文，原发送者 QQ）。"""
    global _action_counter
    websocket = _napcat_websocket
    if websocket is None:
        logger.error("获取引用消息失败：当前没有连接的 NapCat WebSocket")
        return None

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    try:
        async with _action_lock:
            _action_counter += 1
            echo = f"nascence-get-msg-{_action_counter}"
            _pending_actions[echo] = future
            await websocket.send(json.dumps({
                "action": "get_msg",
                "params": {"message_id": int(msg_id)},
                "echo": echo,
            }, ensure_ascii=False))
        response = await asyncio.wait_for(future, timeout=10)
        if response.get("status") != "ok":
            logger.error("获取引用消息失败 (msg_id=%s): %s", msg_id, response)
            return None
        message_data = response.get("data", {})
        raw_msg = message_data.get("raw_message", "")
        if not raw_msg:
            message = message_data.get("message", "")
            if isinstance(message, list):
                raw_msg = "".join(
                    str(segment.get("data", {}).get("text", ""))
                    for segment in message
                    if segment.get("type") == "text"
                )
            else:
                raw_msg = str(message)
        clean_msg, _ = parse_cq_code(raw_msg)
        if not clean_msg:
            return None

        quoted_sender_data = message_data.get("sender", {}) or {}
        quoted_sender_id = str(
            quoted_sender_data.get("user_id")
            or message_data.get("user_id")
            or ""
        )
        quoted_group_id = str(message_data.get("group_id") or fallback_group_id)
        if quoted_sender_id == get_bot_qq():
            quoted_sender = BOT_NAME
        else:
            quoted_sender = (
                manifest.name_map.get(quoted_group_id, {}).get(quoted_sender_id)
                or quoted_sender_data.get("card")
                or quoted_sender_data.get("nickname")
                or quoted_sender_id
                or "未知发送者"
            )

        logger.info(
            "通过 WebSocket 获取引用消息成功 (msg_id=%s, sender=%s): %s",
            msg_id,
            quoted_sender,
            clean_msg,
        )
        return quoted_sender, clean_msg, quoted_sender_id
    except asyncio.TimeoutError:
        logger.error("获取引用消息超时 (msg_id=%s)", msg_id)
    except Exception as e:
        logger.error(f"获取引用消息失败 (msg_id={msg_id}): {e}")
    finally:
        if 'echo' in locals():
            _pending_actions.pop(echo, None)
    return None

# ---------- WebSocket 服务器 ----------
async def ws_handler(websocket):
    """处理 NapCat 发来的 WebSocket 连接"""
    request = getattr(websocket, "request", None)
    request_path = getattr(request, "path", None) or getattr(websocket, "path", "/")
    headers = getattr(request, "headers", {}) if request else getattr(websocket, "request_headers", {})
    authorization = headers.get("Authorization", "") if headers else ""
    query_token = ""
    if "?" in request_path:
        request_path, query = request_path.split("?", 1)
        query_token = parse_qs(query).get("access_token", [""])[0]
    header_token = authorization.removeprefix("Bearer ").strip()
    if request_path != WS_PATH:
        logger.warning("拒绝非 /ws WebSocket 连接: %s", request_path)
        await websocket.close(code=1008, reason="invalid path")
        return
    ws_token = get_napcat_token()
    if ws_token and header_token != ws_token and query_token != ws_token:
        logger.warning("拒绝未通过 token 校验的 WebSocket 连接")
        await websocket.close(code=1008, reason="invalid token")
        return
    global _napcat_websocket, _cognitive_task
    _napcat_websocket = websocket
    logger.info("NapCat 已连接，已启用 WebSocket Action 发送")

    # NapCat 连接后启动永续认知循环
    if _cognitive_task is None or _cognitive_task.done():
        from core.cognition import request_graceful_stop
        request_graceful_stop()  # 确保旧实例已清理
        _cognitive_task = asyncio.create_task(
            cognitive_loop(
                send_func=send_group_msg,
                target_group_id=get_active_group_id(),
                media_send_func=send_group_media,
            )
        )
        logger.info("[认知循环] 已随 NapCat 连接启动")

    message_tasks = set()
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                echo = data.get("echo")
                if echo in _pending_actions:
                    future = _pending_actions.pop(echo)
                    if not future.done():
                        future.set_result(data)
                    continue
                if data.get("post_type") == "message" and data.get("message_type") == "group":
                    task = asyncio.create_task(handle_group_message(data))
                    message_tasks.add(task)
                    task.add_done_callback(message_tasks.discard)
            except json.JSONDecodeError:
                logger.warning("收到非JSON消息")
            except Exception as e:
                logger.error(f"消息处理异常: {e}")
    except websockets.exceptions.ConnectionClosed:
        logger.warning("NapCat 连接已断开")
    except Exception as e:
        logger.error(f"WebSocket 处理异常: {e}")
    finally:
        if _napcat_websocket is websocket:
            _napcat_websocket = None

        # NapCat 断开：认知循环完成当前轮（输出回复，不复搜）再释放
        if _cognitive_task and not _cognitive_task.done():
            from core.cognition import request_graceful_stop
            request_graceful_stop()
            logger.info("[认知循环] NapCat 断开，等待当前轮完成…")
            try:
                await asyncio.wait_for(_cognitive_task, timeout=60)
            except asyncio.TimeoutError:
                _cognitive_task.cancel()
                try:
                    await _cognitive_task
                except asyncio.CancelledError:
                    pass
                logger.warning("[认知循环] 超时强制取消")
            _cognitive_task = None
            logger.info("[认知循环] 已释放")

        for future in list(_pending_actions.values()):
            if not future.done():
                future.set_exception(ConnectionError("NapCat WebSocket 已断开"))
        _pending_actions.clear()
        if message_tasks:
            await asyncio.gather(*message_tasks, return_exceptions=True)
        logger.info("NapCat WebSocket 已释放")

def request_shutdown():
    """请求 QQ 服务优雅停止（由控制面板在 GUI 线程内 call_soon_threadsafe 触发）。

    仅置位停止信号；start_server 检测到后先等认知循环完成当前轮，再断开 NapCat。
    在事件循环线程中调用，Event.set() 线程安全。
    """
    if _shutdown_event is not None:
        _shutdown_event.set()

async def start_server():
    """启动 WebSocket 服务器，附带定时保存"""
    logger.info(f"当前记忆数: {len(memories)}")
    if not memories:
        logger.warning("[主动发言] 记忆库为空，发散任务将空闲等待")
    logger.info(f"启动 WebSocket 服务器: ws://{WS_HOST}:{WS_PORT}{WS_PATH}")

    global _shutdown_event, _napcat_websocket, _cognitive_task
    _shutdown_event = asyncio.Event()

    # ========== 后台定时保存任务 ==========
    async def auto_save():
        """每10分钟自动保存一次记忆和状态。

        下沉不再在这里先做一遍：热集淘汰由 HotSetPlanner 定时规划、
        MemoryWriter 统一落库，这里只负责保存快照。
        """
        while True:
            try:
                await asyncio.sleep(600)  # 10分钟
                from utils.persistence import save_all_data, save_state
                from core.memory_writer import PLANNER
                # 应用规划器算好的淘汰名单（若期间条目被访问，CAS 会跳过）
                try:
                    PLANNER.apply_plan(PLANNER.take_plan())
                except Exception as e:
                    logger.error(f"[定时下沉] 应用淘汰计划失败: {e}")
                save_all_data()
                save_state()
                logger.info("定时保存完成")
                try:
                    from utils.metrics_probe import PROBE
                    snap = PROBE.snapshot()
                    counters = snap.get("counters", {})
                    logger.info(
                        "[观测] LLM调用=%s 视觉=%s(缓存命中%s) 事件循环阻塞p95=%sms 锁等待p95=%sms",
                        counters.get("llm.calls", 0),
                        counters.get("vision.calls", 0),
                        counters.get("vision.cache_hit", 0),
                        snap.get("timings", {}).get("message.handle_ms", {}).get("p95_ms", 0),
                        snap.get("timings", {}).get("lock.data.wait", {}).get("p95_ms", 0),
                    )
                except Exception:
                    pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时保存失败: {e}")
    
    save_task = asyncio.create_task(auto_save())

    # 浅层意识发散任务（记忆热身，认知循环随 NapCat 连接启动）
    drift_task = asyncio.create_task(drift_loop())

    global _is_shutting_down
    _is_shutting_down = False

    # 针对 Windows 平台 ProactorEventLoop 关机时底层 IOCP 的 _attach AssertionError 竞态进行静默过滤
    loop = asyncio.get_running_loop()
    orig_handler = loop.get_exception_handler()
    def _graceful_exception_handler(current_loop, context):
        exc = context.get("exception")
        handle = str(context.get("handle", ""))
        if isinstance(exc, AssertionError) and "_attach" in handle:
            return
        if "BaseProactorEventLoop._start_serving" in handle:
            return
        if orig_handler:
            orig_handler(current_loop, context)
        else:
            current_loop.default_exception_handler(context)

    loop.set_exception_handler(_graceful_exception_handler)

    # 扩容 WebSocket 单帧上限至 32MB（默认仅 1MB），支持收发 Base64 图片与表情包不被截断
    server = await websockets.serve(ws_handler, WS_HOST, WS_PORT, max_size=32 * 1024 * 1024)
    try:
        logger.info(f"WebSocket 服务器已启动，等待 NapCat 连接: ws://{WS_HOST}:{WS_PORT}{WS_PATH}")
        # 服务器已就绪：此时补做睡眠维护，保证"启动即睡眠"也能被 @ 唤醒。
        # 若在 start_server 之前做，这十几秒里 NapCat 连不上来。
        if is_sleeping() and not _sleep_maintenance_done:
            schedule_sleep_maintenance()
        await _shutdown_event.wait()

        # ========== 服务停止：优雅关停 ==========
        _is_shutting_down = True
        logger.info("[服务停止] 请求认知循环完成当前轮…")
        from core.cognition import request_graceful_stop
        request_graceful_stop()
        if _cognitive_task and not _cognitive_task.done():
            try:
                # 认知循环当前轮含多次 LLM 调用，收尾时间收紧到 20 秒，
                # 避免单次 API 挂起把整个关停拖满一分钟以上。
                await asyncio.wait_for(_cognitive_task, timeout=20)
            except asyncio.TimeoutError:
                _cognitive_task.cancel()
                try:
                    await _cognitive_task
                except asyncio.CancelledError:
                    pass
                logger.warning("[服务停止] 认知循环超时强制取消")
            _cognitive_task = None
            logger.info("[服务停止] 认知循环已结束")
        else:
            logger.info("[服务停止] 认知循环未运行")

        # 认知循环结束后先断开 NapCat
        if _napcat_websocket is not None:
            try:
                await _napcat_websocket.close(code=1000, reason="service stopping")
                logger.info("[服务停止] NapCat 已断开")
            except Exception:
                logger.warning("[服务停止] NapCat 断开失败（可能已断开）")

        # 显式关闭监听服务器并等待底层资源释放
        server.close()
        await server.wait_closed()
        await asyncio.sleep(0.05)  # 留出短暂缓冲排空底层残余事件
    finally:
        _is_shutting_down = True
        global _final_save_done
        _napcat_websocket = None
        _final_save_done = False
        save_task.cancel()
        drift_task.cancel()
        if _cognitive_task and not _cognitive_task.done():
            _cognitive_task.cancel()
            try:
                await _cognitive_task
            except Exception:
                pass
        await asyncio.gather(save_task, drift_task, return_exceptions=True)
        try:
            server.close()
            await server.wait_closed()
        except Exception:
            pass
        loop.set_exception_handler(orig_handler)
        if not _final_save_done:
            _final_save_done = True
            from utils.persistence import save_all_data, save_state
            # 关停的最后一次保存必须落盘，绕过节流
            save_all_data(force=True)
            save_state()
            # 写者/规划器/解析线程收尾：stop 会清空队列并等待 commit
            try:
                from core.memory_writer import stop_default_workers
                stop_default_workers()
            except Exception as e:
                logger.warning(f"[服务停止] 记忆写者关闭失败: {e}")
            try:
                from core.parse_worker import stop_parse_worker
                stop_parse_worker()
            except Exception:
                pass
            logger.info("最终保存：记忆和状态已持久化")

# ---------- 启动入口 ----------
if __name__ == "__main__":
    try:
        # ========== 初始化记忆库（与 main.py 保持一致）==========
        from core.memory_engine import memories, get_model
        from utils.persistence import load_all_data, load_state, save_all_data, save_state
        from core.virtual_clock import clock
        from main import cold_start_batch_injection
        from utils.monitor import monitor_start
        from core.memory_engine import log_metrics_status
        from core.memory_writer import start_default_workers

        # 设置时间倍速（1倍）
        clock.enable_qq_mode()
        print(f"当前时间倍速：{clock.set_speed(1)}")

        # 启用服务端检测窗
        monitor_start()

        # 初始化语义模型
        get_model()

        # 加载记忆和状态
        load_all_data()
        load_state()
        log_metrics_status()
        start_default_workers()

        # 加载短期对话历史
        from core.llm_interface import load_dialogue_history
        load_dialogue_history()

        # 初始化指标计数器（从基线恢复累计值）
        from core.memory_engine import _init_metrics_counters
        _init_metrics_counters()

        # 如果记忆库为空，注入冷启动记忆
        if not memories:
            cold_start_batch_injection()
            save_all_data()
            save_state()
            print("[初始化] 冷启动记忆已注入")
        else:
            print(f"[初始化] 已加载 {len(memories)} 条记忆")

        # ========== 启动 WebSocket 服务器 ==========
        init_sleep_state()
        logger.info(f"启动 WebSocket 服务器: ws://{WS_HOST}:{WS_PORT}")
        asyncio.run(start_server())

    except KeyboardInterrupt:
        logger.info("Bot 已停止")
    except Exception as e:
        logger.error(f"启动失败: {e}")
        import traceback
        traceback.print_exc()
