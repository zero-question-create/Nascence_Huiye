# qq_bot.py
# ========================================================================
# 功能：独立QQ接入模块 - WebSocket 服务器模式（NapCat 主动连接）
# 包含：白名单、姓名映射、CQ码清洗、@/引用解析、强制回复、门控静默
# ========================================================================

import asyncio
import json
import re
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
for d in [LOG_DIR, PROJECT_DIR / "data" / "test", PROJECT_DIR / "data" / "models"]:
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

from core.llm_interface import describe_image_from_path, describe_audio_from_path, describe_video_from_path
from utils.event_bus import BUS

# 导入你现有的核心模块
from core.cognition import process_dialogue
from core.memory_engine import create_memory, access_memory, pathfind_activation, retrieve_similar, memories
from core.virtual_clock import clock

# ---------- 配置常量 ----------
BOT_QQ = "3852948473"  # 机器人QQ号
CONFIG_PATH = "config/qq_manifest.json"
HTTP_API_BASE = "http://127.0.0.1:5700"
HTTP_ACCESS_TOKEN = "Fxr13142"
SILENT_MARKER = "[SILENT]"
FALLBACK_REPLY_WHEN_MENTIONED = "嗯......"
WS_HOST = "127.0.0.1"
WS_PORT = 6700  # NapCat 配置中填写的端口
WS_PATH = "/ws"
WS_ACCESS_TOKEN = "Fxr13142"
# 最近回复缓存（用于去重）
_reply_history = []  # 存储最近回复
MAX_REPLY_HISTORY = 3

_recent_msg_list = []      # 有序存储 (sender_id, clean_text)
_recent_msg_set = set()    # 快速查找去重
MAX_RECENT_MESSAGES = 20

IGNORE_PREFIX = "#"     # 前缀特殊字符

# 主动节奏控制（秒）
active_cooldown = 300
MAX_ACTIVE_COOLDOWN = 15 * 60
MIN_ACTIVE_COOLDOWN = 5 * 60
MAX_RESEARCH = 2

ACTIVE_GROUP_ID = "1057279304"  # 主动发言的目标群

# 浅层意识池
shallow_pool = deque(maxlen=20)

# 主动发言状态
active_state = {
    "cooldown_until": 0.0,
    "research_remaining": MAX_RESEARCH,
}

_final_save_done = False            # 全局保存标识
_active_speak_task = None           # 当前正在运行的 try_active_speak 任务
_napcat_websocket = None             # 当前连接的 NapCat 反向 WebSocket
_action_lock = asyncio.Lock()        # 串行发送 OneBot Action，避免响应混淆
_action_counter = 0
_pending_actions = {}                 # echo -> Future，用于匹配 NapCat Action 响应

# ========== 睡眠配置 ==========
from core.cognition import SLEEP_START_HOUR, SLEEP_END_HOUR, DROWSY_MARGIN
_sleeping = False               # 是否处于强制睡眠状态

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("QQBot")

# ---------- 加载映射表 ----------
class QQManifest:
    def __init__(self):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
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
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.whitelist = set(data.get("whitelist_groups", []))
            self.name_map = data.get("name_mapping", {})

manifest = QQManifest()

# ---------- 工具函数 ----------
def parse_cq_code(text: str) -> Tuple[str, List[str]]:
    """移除CQ码，提取被@的QQ号列表"""
    pattern = re.compile(r'\[CQ:at,qq=(\d+)(?:,name=[^\]]+)?\]')
    mentions = pattern.findall(text)
    clean_text = re.sub(r'\[CQ:[^\]]+\]', '', text).strip()
    return clean_text, mentions

def build_augmented_input(sender_name: str, raw_text: str, mentions: List[str], group_id: str, is_mentioned_me: bool) -> str:
    """
    构建标准化增强输入（自然语言格式）
    """
    # 如果文本为空，给一个占位符（但正常情况下不会走到这里）
    text_content = raw_text if raw_text else "（没有说话）"
    
    if is_mentioned_me:
        return f'{sender_name}对辉夜说：“{text_content}”'
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
    """后台发散任务"""
    logger.info("[浅层意识] 发散任务已启动")
    while True:
        if is_sleeping() and not _sleeping:
            enter_sleep()
        now = time.time()
        nowdate = datetime.datetime.now().time()
        start = datetime.time(SLEEP_START_HOUR, 0)
        end = datetime.time(SLEEP_END_HOUR, 0)
        if start >= end and (nowdate < start and nowdate >= end) and _sleeping:
            wake_up()
        await asyncio.sleep(max(active_state["cooldown_until"] - now, 30))
        if not memories:
            continue
        mem_id = random.choice(list(memories.keys()))
        access_memory(mem_id)
        pathfind_activation([mem_id], max_stamina=1.0, top_k=3, max_steps=1)
        shallow_pool.append((mem_id, time.time()))
        logger.info(f"[浅层意识] 激活记忆 {mem_id[:8]}...，当前池大小: {len(shallow_pool)}")

def is_sleeping() -> bool:
    """判断当前是否处于睡眠时间窗口（含启动时强制睡眠状态）"""
    global _sleeping
    if _sleeping:
        return True
    now = datetime.datetime.now().time()
    start = datetime.time(SLEEP_START_HOUR, 0)
    end = datetime.time(SLEEP_END_HOUR, 0)
    if start < end:
        return start <= now < end
    else:
        # 跨天窗口，例如 23:00 - 06:00
        return now >= start or now < end

def enter_sleep():
    """强制进入睡眠状态，并安排自动唤醒"""
    global _sleeping
    _sleeping = True
    logger.info("辉夜进入了睡眠状态")
    try:
        from utils.persistence import sleep_cleanup
        sleep_cleanup()
        logger.info("[睡眠维护] 链接剪枝、字词整理、全量保存已完成")
    except Exception as e:
        logger.error(f"[睡眠维护] 执行失败: {e}")

def wake_up():
    """从睡眠中唤醒（由定时器触发）"""
    global _sleeping
    _sleeping = False
    logger.info("辉夜醒来了！")

def init_sleep_state():
    """启动时调用，如果当前处于睡眠窗口则立即进入睡眠"""
    if is_sleeping():
        enter_sleep()
    else:
        # 如果不在睡眠窗口，但曾因重启丢失了唤醒定时器，则无需操作
        logger.info("当前不在睡眠窗口，辉夜保持清醒。")

# ---------- 消息处理核心 ----------
async def handle_group_message(data: dict):
    if is_sleeping():
        return
    """处理单条群消息"""
    global _reply_history
    
    group_id = str(data.get("group_id"))
    current_msg_id = data.get("message_id")
    
    # 白名单过滤
    if group_id not in manifest.whitelist:
        logger.debug(f"群 {group_id} 不在白名单，忽略")
        return

    sender_id = str(data.get("user_id"))
    sender_name = (
        data.get("sender", {}).get("card") or 
        data.get("sender", {}).get("nickname") or 
        sender_id
    )
    mapped_name = manifest.name_map.get(group_id, {}).get(sender_id)
    if mapped_name:
        sender_name = mapped_name

    raw_str = data.get("raw_message", "")
    clean_text, mentions = parse_cq_code(raw_str)

    # 消息前缀忽略
    if clean_text.lstrip().startswith(IGNORE_PREFIX):
        logger.info(f"消息以忽略前缀 '{IGNORE_PREFIX}' 开头，忽略")
        return
    clean_text = clean_text.replace("@辉夜","")
    if clean_text.strip():
        BUS.message.emit(sender_name, clean_text.strip(), "QQ")

    is_mentioned_me = BOT_QQ in mentions
    if is_mentioned_me:
        mentions = [m for m in mentions if m != BOT_QQ]

    # 多模态处理
    media_list = []  # 收集 (type, description)
    for seg in data.get("message", []):
        seg_type = seg.get("type")
        if seg_type not in ("image", "record", "video"):
            continue
        seg_data = seg.get("data", {})
        file_url = seg_data.get("url") or seg_data.get("file")
        if seg_type == "record" and file_url and not str(file_url).startswith(("http://", "https://")):
            # NapCat 有时只回传文件名或本地路径，通过 get_record 获取可下载文件。
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{HTTP_API_BASE}/get_record",
                        params={"file": file_url, "out_format": "wav"},
                        headers={"Authorization": f"Bearer {HTTP_ACCESS_TOKEN}"},
                        timeout=30,
                    ) as resp:
                        if resp.status == 200:
                            record_result = await resp.json()
                            file_url = record_result.get("data", {}).get("file") or file_url
            except Exception as e:
                logger.warning(f"通过 NapCat 获取语音失败: {e}")

        if not file_url or not str(file_url).startswith(("http://", "https://")):
            if not file_url or not os.path.isfile(str(file_url)):
                logger.warning(f"媒体没有可下载的 URL 或本地文件，类型={seg_type}")
                continue
        tmp_path = None
        owns_tmp_path = False
        try:
            parsed_url = urlparse(str(file_url))
            query_params = parse_qs(parsed_url.query)
            default_format = os.path.splitext(str(file_url))[1].lstrip(".") or seg_data.get("format", "amr")
            file_format = query_params.get('format', [default_format])[0]
            file_format = re.sub(r"[^a-zA-Z0-9]", "", file_format).lower() or "amr"

            if str(file_url).startswith(("http://", "https://")):
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

            if seg_type == "image":
                desc = await describe_image_from_path(tmp_path)
            elif seg_type == "record":
                desc = await describe_audio_from_path(tmp_path)
            else:
                desc = await describe_video_from_path(tmp_path)
            if desc:
                media_list.append((seg_type, desc))
        except Exception as e:
            logger.warning(f"处理媒体失败: {e}")
        finally:
            if tmp_path and owns_tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    logger.info(f"临时文件删除失败，请及时清理：{tmp_path}")

    # ---------- 构建自然语言增强输入 ----------
    if media_list:
        # 生成媒体部分的自然描述
        media_parts = []
        for m_type, d in media_list:
            if m_type == "image":
                media_parts.append(f"一张图片，内容是“{d}”")
            elif m_type == "record":
                media_parts.append(f"一段录音，内容是“{d}”")
            elif m_type == "video":
                media_parts.append(f"一个视频，内容是“{d}”")
        media_text = "、".join(media_parts)  # 用顿号分隔多个媒体
        
        if clean_text:  # 有文字伴随
            augmented_input = f"{sender_name}说：“{clean_text}”，同时发送了{media_text}"
        else:           # 纯媒体
            augmented_input = f"{sender_name}发送了{media_text}"
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

    # 纯@无文本 → 简单回复，不存入记忆（因为是无效交互）
    if not clean_text and not mentions:
        # “纯@机器人”（只@了机器人且无文字）
        await send_group_msg(group_id, "嗯？我在呢~")
        return

    # 提取引用消息
    quote_id = extract_quote_message_id(data.get("message", []))
    extra_context = ""
    if quote_id:
        quoted_message = await fetch_quoted_message(quote_id, group_id)
        if quoted_message:
            quoted_sender, quoted_text = quoted_message
            extra_context = f"{sender_name}引用了{quoted_sender}之前的一句话：'{quoted_text}'"

    logger.info(f"输入: {augmented_input} | 上下文: {extra_context}")

    # 重置主动发言冷却
    reset_active_speaker()

    from core.memory_engine import create_memory, add_link
    from core.virtual_clock import clock

    # 调用认知层
    result = await process_dialogue(
        augmented_input=augmented_input,
        extra_context=extra_context
    )

    # 兼容处理返回值
    if isinstance(result, tuple):
        draft_reply = result[0] if result else ""
        mem_ids = result[1] if len(result) > 1 else []
    else:
        draft_reply = str(result) if result else ""
        mem_ids = []

    if isinstance(draft_reply, tuple):
        draft_reply = draft_reply[0] if draft_reply else ""
    elif not isinstance(draft_reply, str):
        draft_reply = str(draft_reply) if draft_reply else ""

    # ========== 关键新增：如果生成了有效回复，存入记忆并建立因果链接 ==========
    should_send = False
    final_reply = None

    # 判断是否是兜底回复（无记忆）
    is_empty_reply = (
        not draft_reply or 
        draft_reply.strip() in [ "静默", "静默。"] or
        draft_reply.strip() == SILENT_MARKER
    )

    if is_empty_reply:
        if is_mentioned_me:
            final_reply = FALLBACK_REPLY_WHEN_MENTIONED
            should_send = True
            # 兜底回复存入记忆（格式：我告诉{对方}，{回复}）
            bot_mem_id = create_memory(f"我告诉{sender_name}，{final_reply}", half_life=2 * 24 * 3600)
            # 建立链接需要用户记忆ID，但用户记忆ID未存（因为没经过认知层），简单处理：不建立链接或从认知层获取
            logger.info(f"被@时LLM无记忆，使用兜底回复并存入记忆: {final_reply}")
        else:
            should_send = False
    else:
        # 正常回复：一切已由认知层存入记忆，这里不需要额外存储
        final_reply = draft_reply
        should_send = True

    # 加入对话记忆
    from core.llm_interface import add_to_history
    add_to_history(sender_name, clean_text.strip(), final_reply, "QQ")

    # ========== 发送前检查重复（仅当未被@时） ==========
    if should_send and final_reply:
        if not is_mentioned_me:
            is_dup = final_reply in _reply_history
            if is_dup and False:                                    # 临时关闭重复判定
                logger.info(f"检测到重复回复，跳过发送: {final_reply}")
                # 注意：即使跳过发送，记忆已经存了，所以知识不会丢
                return

        _reply_history.append(final_reply)
        if len(_reply_history) > MAX_REPLY_HISTORY:
            _reply_history.pop(0)

        BUS.message.emit("辉夜", final_reply, "QQ")
        await send_group_msg(group_id, final_reply,  reply_msg_id=current_msg_id)

async def send_group_msg(group_id: str, text: str, reply_msg_id: int = None):
    """
    发送群消息，支持引用回复。
    reply_msg_id: 被引用消息的 message_id（整数）
    """
    if is_sleeping():
        logger.debug("睡眠期间禁止发送消息，已拦截。")
        return
    global _action_counter
    websocket = _napcat_websocket
    if websocket is None:
        logger.error("消息发送失败：当前没有连接的 NapCat WebSocket")
        return

    try:
        message_content = text
        if reply_msg_id is not None:
            message_content = f"[CQ:reply,id={reply_msg_id}]{text}"

        async with _action_lock:
            _action_counter += 1
            echo = f"nascence-send-{_action_counter}"
            payload = {
                "action": "send_group_msg",
                "params": {
                    "group_id": int(group_id),
                    "message": message_content,
                },
                "echo": echo,
            }
            await websocket.send(json.dumps(payload, ensure_ascii=False))
        logger.info(f"已通过 NapCat WebSocket 发送消息（echo={echo}，引用={reply_msg_id}）: {text}")
        from core.memory_engine import _count_message_sent, _count_self_ref
        _count_message_sent += 1
        if "我" in text:
            _count_self_ref += 1
    except Exception as e:
        logger.error(f"通过 NapCat WebSocket 发送消息异常: {e}")

async def fetch_quoted_message(msg_id: str, fallback_group_id: str = "") -> Optional[Tuple[str, str]]:
    """通过 WebSocket 获取引用消息，返回（原发送者名称，消息正文）。"""
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
        if quoted_sender_id == BOT_QQ:
            quoted_sender = "辉夜"
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
        return quoted_sender, clean_msg
    except asyncio.TimeoutError:
        logger.error("获取引用消息超时 (msg_id=%s)", msg_id)
    except Exception as e:
        logger.error(f"获取引用消息失败 (msg_id={msg_id}): {e}")
    finally:
        if 'echo' in locals():
            _pending_actions.pop(echo, None)
    return None

async def try_active_speak(group_id: str) -> bool:
    """进行一次主动思考（使用统一的 LLM 决策）"""
    from core.memory_engine import _count_active_attempt, _count_active_success
    _count_active_attempt += 1
    if not shallow_pool:
        logger.info("[主动发言] 浅层池为空，跳过")
        return False

    # 取最近激活的记忆作为种子，并获取上下文
    seed_mem_id, _ = shallow_pool[-1]
    # 临时调试日志
    from core.memory_engine import links
    out_links = sum(1 for (src, tgt) in links if src == seed_mem_id)
    logger.info(f"[主动发言] 种子记忆现有出边数: {out_links}")
    logger.info(f"[主动发言] 使用种子记忆: {seed_mem_id[:8]}...")
    
    activated = pathfind_activation([seed_mem_id], max_stamina=5.0, top_k=5, max_steps=1)

    # 在生成带时间标记的记忆文本处
    from core.virtual_clock import clock
    from utils.time_phrases import get_relative_time_phrase

    context_mems = []
    for mem, _ in activated:  # 解包元组 (memory_dict, score)
        virtual_ts = mem.get("creation_time", 0)
        real_ts = clock.to_real_time(virtual_ts)
        phrase = get_relative_time_phrase(real_ts)
        context_mems.append(f"[{phrase}] {mem['content']}")

    logger.info(f"[主动发言] 扩散获得 {len(context_mems)} 条上下文")
    if not shallow_pool:
        return False

    # 获取当前对话状态
    from utils.dialogue_state import get_state
    state = get_state()

    if asyncio.current_task() and asyncio.current_task().cancelled():
        logger.info("[主动发言] 任务已被取消，中止思考")
        return False

    # 调用决策 LLM（在线程池中执行同步函数）
    from core.llm_interface import active_speak_decision
    loop = asyncio.get_event_loop()
    decision = await loop.run_in_executor(
        None, active_speak_decision, context_mems, state
    )
    if asyncio.current_task() and asyncio.current_task().cancelled():
        logger.info("[主动发言] 任务已被取消，中止思考")
        return False

    if decision["action"] == "SEND":
        # 直接发言
        text = decision["content"]
        await send_group_msg(group_id, text)
        create_memory(f"我说：{text}", half_life=12 * 3600)
        from core.llm_interface import add_to_history
        add_to_history(None, None, text)
        _count_active_success += 1
        return True

    elif decision["action"] == "SEARCH":
        # 用给出的关键词定向检索，然后再调用一次决策（可选：也可以直接搜索后发言）
        keywords = decision["keywords"]
        search_results = []
        for kw in keywords:
            search_results.extend(retrieve_similar(kw, k=3))
        seen = set()
        all_mems = []
        for score, mem in sorted(search_results, key=lambda x: x[0], reverse=True):
            if mem["content"] not in seen:
                seen.add(mem["content"])
                virtual_ts = mem.get("creation_time", 0)
                real_ts = clock.to_real_time(virtual_ts)
                phrase = get_relative_time_phrase(real_ts)
                all_mems.append(f"[{phrase}] {mem['content']}")
                if len(all_mems) >= 8:
                    break

        if asyncio.current_task() and asyncio.current_task().cancelled():
            logger.info("[主动发言] 任务已被取消，中止思考")
            return False

        # 将搜索到的记忆和原有记忆合并，再问一次是否要说话
        combined_mems = context_mems + all_mems
        decision2 = await loop.run_in_executor(
            None, active_speak_decision, combined_mems, state
        )
        if asyncio.current_task() and asyncio.current_task().cancelled():
            logger.info("[主动发言] 任务已被取消，中止思考")
            return False

        if decision2["action"] == "SEND":
            text = decision2["content"]
            await send_group_msg(group_id, text)
            create_memory(f"我说：{text}", half_life=12 * 3600)
            _count_active_success += 1
            return True
        else:
            # 复搜后仍不想说话，返回 False 进入冷却/继续复搜
            return False

    return False  # NONE 或异常

async def active_speaker_loop(group_id: str):
    global _active_speak_task
    logger.info("[主动发言] 发言任务已启动")
    while True:
        await asyncio.sleep(1)
        if is_sleeping():
            continue
        now = time.time()

        if now < active_state["cooldown_until"]:
            continue

        # 冷却已到期，但配额为0，说明是刚从冷却中恢复，立刻重置配额，避免再次进入冷却
        if active_state["research_remaining"] <= 0:
            active_state["research_remaining"] = MAX_RESEARCH
            logger.info("[主动发言] 冷却结束，重置复搜配额，开始尝试发言")

        logger.info(f"[主动发言] 执行复搜，剩余次数: {active_state['research_remaining']}")
        active_state["research_remaining"] -= 1
        _active_speak_task = asyncio.create_task(try_active_speak(group_id))
        try:
            spoken = await _active_speak_task
        except asyncio.CancelledError:
            logger.info("[主动发言] 思考被用户消息打断，中止本轮尝试")
            spoken = False
        _active_speak_task = None
        
        active_cooldown = random.randint(MIN_ACTIVE_COOLDOWN, MAX_ACTIVE_COOLDOWN)
        logger.info(f"[主动发言] 本轮冷却：{active_cooldown}")
        
        if spoken:
            active_state["cooldown_until"] = now + active_cooldown
            active_state["research_remaining"] = MAX_RESEARCH
            logger.info("[主动发言] 发言成功，进入冷却")
        else:
            if active_state["research_remaining"] > 0:
                await asyncio.sleep(3)
            else:
                active_state["cooldown_until"] = now + active_cooldown
                logger.info("[主动发言] 配额用尽，进入冷却")

def reset_active_speaker():
    global _active_speak_task
    # 如果有正在进行的主动发言任务，取消它
    if _active_speak_task and not _active_speak_task.done():
        _active_speak_task.cancel()
        logger.info("[主动发言] 用户消息打断，取消当前思考")
    # 不再清空 shallow_pool，让它自然积累
    buchang = random.randint(active_cooldown-30,active_cooldown-15)
    active_state["cooldown_until"] = time.time() + active_cooldown - buchang
    logger.info(f"[主动发言] 发言后补偿后冷却：{active_cooldown-buchang}")
    active_state["research_remaining"] = MAX_RESEARCH

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
    if WS_ACCESS_TOKEN and header_token != WS_ACCESS_TOKEN and query_token != WS_ACCESS_TOKEN:
        logger.warning("拒绝未通过 token 校验的 WebSocket 连接")
        await websocket.close(code=1008, reason="invalid token")
        return
    global _napcat_websocket
    _napcat_websocket = websocket
    logger.info("NapCat 已连接，已启用 WebSocket Action 发送")
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
                # 忽略其他事件
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
        for future in list(_pending_actions.values()):
            if not future.done():
                future.set_exception(ConnectionError("NapCat WebSocket 已断开"))
        _pending_actions.clear()
        if message_tasks:
            await asyncio.gather(*message_tasks, return_exceptions=True)
        logger.info("NapCat WebSocket 已释放")

async def start_server():
    """启动 WebSocket 服务器，附带定时保存"""
    logger.info(f"当前记忆数: {len(memories)}")
    if not memories:
        logger.warning("[主动发言] 记忆库为空，发散任务将空闲等待")
    logger.info(f"启动 WebSocket 服务器: ws://{WS_HOST}:{WS_PORT}{WS_PATH}")
    
    # ========== 后台定时保存任务 ==========
    async def auto_save():
        """每10分钟自动保存一次记忆和状态"""
        while True:
            try:
                await asyncio.sleep(600)  # 10分钟
                from utils.persistence import save_all_data, save_state
                from core.memory_engine import _evict_cold_memories
                _evict_cold_memories()
                save_all_data()
                save_state()
                logger.info("定时保存与冷数据下沉完成")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时保存失败: {e}")
    
    save_task = asyncio.create_task(auto_save())

    drift_task = asyncio.create_task(drift_loop())
    speaker_task = asyncio.create_task(active_speaker_loop(ACTIVE_GROUP_ID))
    
    try:
        async with websockets.serve(ws_handler, WS_HOST, WS_PORT):
            logger.info(f"WebSocket 服务器已启动，等待 NapCat 连接: ws://{WS_HOST}:{WS_PORT}{WS_PATH}")
            await asyncio.Future()  # 永久运行
    finally:
        global _final_save_done
        global _napcat_websocket
        _napcat_websocket = None
        logger.info("NapCat WebSocket 已释放")
        _final_save_done = False
        save_task.cancel()
        drift_task.cancel()
        speaker_task.cancel()
        await asyncio.gather(save_task, drift_task, speaker_task, return_exceptions=True)
        if not _final_save_done:
            _final_save_done = True
            from utils.persistence import save_all_data, save_state
            save_all_data()
            save_state()
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
