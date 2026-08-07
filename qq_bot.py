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

# 导入你现有的核心模块
from core.cognition import inject_message_keywords, cognitive_loop, extract_keywords_jieba
from core.memory_engine import create_memory, access_memory, pathfind_activation, retrieve_similar, memories
from core.virtual_clock import clock
from config.constants import BOT_NAME

# ---------- 配置常量 ----------
BOT_QQ = "3852948473"  # 机器人QQ号
CONFIG_PATH = "config/qq_manifest.json"
HTTP_API_BASE = "http://127.0.0.1:5700"
HTTP_ACCESS_TOKEN = "Fxr13142"
WS_HOST = "127.0.0.1"
WS_PORT = 6700  # NapCat 配置中填写的端口
WS_PATH = "/ws"
WS_ACCESS_TOKEN = "Fxr13142"

_recent_msg_list = []      # 有序存储 (sender_id, clean_text)
_recent_msg_set = set()    # 快速查找去重
MAX_RECENT_MESSAGES = 20

IGNORE_PREFIX = "#"     # 前缀特殊字符

ACTIVE_GROUP_ID = "1057279304"  # 主动发言的目标群

_final_save_done = False            # 全局保存标识
_napcat_websocket = None             # 当前连接的 NapCat 反向 WebSocket
_cognitive_task = None               # 永续认知循环 task
_shutdown_event = None               # 服务停止信号（在事件循环线程内 set）
_action_lock = asyncio.Lock()        # 串行发送 OneBot Action，避免响应混淆
_action_counter = 0
_pending_actions = {}                 # echo -> Future，用于匹配 NapCat Action 响应

# ========== 睡眠配置 ==========
# 睡眠窗口统一由 core.virtual_clock 管理（clock.in_sleep_window()）
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
    """后台发散任务：随机激活记忆以强化半衰期，为认知循环提供热身种子"""
    logger.info("[浅层意识] 发散任务已启动")
    while True:
        if is_sleeping() and not _sleeping:
            enter_sleep()
        if _sleeping and not clock.in_sleep_window():
            wake_up()
        await asyncio.sleep(30)
        if not memories:
            continue
        mem_id = random.choice(list(memories.keys()))
        access_memory(mem_id)
        pathfind_activation([mem_id], max_stamina=1.0, top_k=3, max_steps=1)
        logger.info(f"[浅层意识] 激活记忆 {mem_id[:8]}...")

def is_sleeping() -> bool:
    """判断当前是否处于睡眠时间窗口（含启动时强制睡眠状态）"""
    global _sleeping
    if _sleeping:
        return True
    return clock.in_sleep_window()

def enter_sleep():
    """强制进入睡眠状态，并安排自动唤醒"""
    global _sleeping
    _sleeping = True
    logger.info(f"{BOT_NAME}进入了睡眠状态")
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
    logger.info(f"{BOT_NAME}醒来了！")

def init_sleep_state():
    """启动时调用，如果当前处于睡眠窗口则立即进入睡眠"""
    if is_sleeping():
        enter_sleep()
    else:
        # 如果不在睡眠窗口，但曾因重启丢失了唤醒定时器，则无需操作
        logger.info(f"当前不在睡眠窗口，{BOT_NAME}保持清醒。")

# ---------- 消息处理核心 ----------
async def handle_group_message(data: dict):
    if is_sleeping():
        return
    """处理单条群消息"""
    
    group_id = str(data.get("group_id"))
    
    # 白名单过滤
    if group_id not in manifest.whitelist:
        logger.debug(f"群 {group_id} 不在白名单，忽略")
        return

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

    # 消息前缀忽略
    if clean_text.lstrip().startswith(IGNORE_PREFIX):
        logger.info(f"消息以忽略前缀 '{IGNORE_PREFIX}' 开头，忽略")
        return
    clean_text = clean_text.replace(f"@{BOT_NAME}","")
    if clean_text.strip():
        BUS.message.emit(sender_name, clean_text.strip(), "QQ")

    is_mentioned_me = BOT_QQ in mentions
    if is_mentioned_me:
        mentions = [m for m in mentions if m != BOT_QQ]

    # 多模态处理
    media_list = []  # 收集 (type, description)
    for seg in data.get("message", []):
        seg_type = seg.get("type")
        if seg_type not in ("image", "record", "video", "mface"):
            continue
        seg_data = seg.get("data", {})

        if seg_type == "mface":
            # 商城表情包（NapCat 直报 mface）：无可下载媒体，直接用摘要描述
            summary = seg_data.get("summary") or seg_data.get("key") or "商城表情包"
            media_list.append(("sticker", summary))
            continue

        file_url = seg_data.get("url") or seg_data.get("path") or seg_data.get("file")
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
                # 表情包判定（多路并列）：
                # 1. GIF 动画图
                # 2. NapCat 将商城表情以 image 上报时附带 emoji_id/emoji_package_id/key 字段
                # 3. summary 含"表情"字样（如 "[动画表情]"）
                # 4. sub_type == 1（OneBot 图片子类型 1 = 表情包）
                chk_gif = file_format == "gif"
                chk_mface = bool(seg_data.get("emoji_id") or seg_data.get("emoji_package_id") or seg_data.get("key"))
                chk_summary = "表情" in str(seg_data.get("summary") or "")
                chk_subtype = str(seg_data.get("sub_type")) == "1"
                is_sticker = chk_gif or chk_mface or chk_summary or chk_subtype
                media_type = "sticker" if is_sticker else "image"
                desc = await describe_image_from_path(tmp_path)
                if is_sticker and (not desc or desc.startswith("[图片识别失败") or desc.startswith("[图片文件不存在")):
                    desc = seg_data.get("summary") or "表情包"
            elif seg_type == "record":
                media_type = "record"
                desc = await describe_audio_from_path(tmp_path)
            else:
                media_type = "video"
                desc = await describe_video_from_path(tmp_path)
            if desc:
                media_list.append((media_type, desc))
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
            if m_type == "sticker":
                media_parts.append(f"一个表情包，内容是“{d}”")
            elif m_type == "image":
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
    if not augmented_input and not mentions:
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

    from core.memory_engine import create_memory, add_link
    from core.virtual_clock import clock
    from core.llm_interface import add_to_history
    from utils.dialogue_state import set_state
    from utils.persistence import save_state

    full_input = augmented_input
    if extra_context:
        full_input = f"{extra_context}\n{augmented_input}"

    # ========== 理解层：将用户输入拆解为记忆片段（关键词改用 jieba）==========
    mem_fragments, mode, new_state, _keywords = decompose_input(full_input)

    # 更新对话状态
    if new_state:
        set_state(new_state)
        save_state()

    # 记忆入库
    from core.cognition import MODE_HALF_LIFE
    half_life = MODE_HALF_LIFE.get(mode, 2 * 24 * 3600)
    user_mem_ids = []
    for frag in mem_fragments:
        from core.memory_engine import semantic_dedup
        if not semantic_dedup(frag):
            mid = create_memory(frag, half_life=half_life)
            user_mem_ids.append(mid)

    # 将关键词注入认知循环：直接对收到的消息 jieba 分词（搜索引擎模式）
    msg_keywords = extract_keywords_jieba(clean_text)
    if msg_keywords:
        inject_message_keywords(msg_keywords)

    # 记录用户消息到对话历史（回复由 cognitive_loop 异步补充）
    add_to_history(sender_name, clean_text.strip(), None, "QQ")

    logger.info(f"理解层完成，记忆入库 {len(user_mem_ids)} 条，jieba关键词: {msg_keywords}")
    BUS.message.emit(BOT_NAME, f"[思考中...]", "QQ")

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
        return quoted_sender, clean_msg
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
    if WS_ACCESS_TOKEN and header_token != WS_ACCESS_TOKEN and query_token != WS_ACCESS_TOKEN:
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
            cognitive_loop(send_func=send_group_msg, target_group_id=ACTIVE_GROUP_ID)
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

    # 浅层意识发散任务（记忆热身，认知循环随 NapCat 连接启动）
    drift_task = asyncio.create_task(drift_loop())
    
    try:
        async with websockets.serve(ws_handler, WS_HOST, WS_PORT):
            logger.info(f"WebSocket 服务器已启动，等待 NapCat 连接: ws://{WS_HOST}:{WS_PORT}{WS_PATH}")
            await _shutdown_event.wait()

            # ========== 服务停止：优雅关停 ==========
            # 顺序：请求认知循环完成当前轮 → 等待其退出 → 再断开 NapCat
            logger.info("[服务停止] 请求认知循环完成当前轮…")
            from core.cognition import request_graceful_stop
            request_graceful_stop()
            if _cognitive_task and not _cognitive_task.done():
                try:
                    await asyncio.wait_for(_cognitive_task, timeout=60)
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

            # 认知循环结束后再断开 NapCat（触发 ws_handler 的 finally 释放）
            if _napcat_websocket is not None:
                try:
                    await _napcat_websocket.close(code=1000, reason="service stopping")
                    logger.info("[服务停止] NapCat 已断开")
                except Exception:
                    logger.warning("[服务停止] NapCat 断开失败（可能已断开）")
    finally:
        global _final_save_done
        _napcat_websocket = None
        _final_save_done = False
        save_task.cancel()
        drift_task.cancel()
        if _cognitive_task and not _cognitive_task.done():
            _cognitive_task.cancel()
            try:
                await _cognitive_task
            except:
                pass
        await asyncio.gather(save_task, drift_task, return_exceptions=True)
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
