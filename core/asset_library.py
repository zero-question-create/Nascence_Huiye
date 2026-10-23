# core/asset_library.py
# ========================================================================
# 素材库：以持久化字典（描述 → 文件名）维护图片与表情包收藏。
#
# 规格约束：
#   - 图片、表情包作为收藏；文件本体存于 data/test/assets/<kind>/。
#   - 索引就是那个持久化字典：{描述: 文件名}，描述同时用于写入历史记忆。
#   - 不设发送冷却，改为去重：候选池排除上一条已发送的记录。
#   - 收到的媒体先暂存（staging），由动作层决定是否转正为收藏。
# ========================================================================

import hashlib
import json
import os
import random
import re
import shutil
import threading
import time
import uuid

from utils.atomic_io import atomic_write_json, atomic_write_text, atomic_append_line

IMAGE = "image"
STICKER = "sticker"
_KINDS = (IMAGE, STICKER)

ASSET_ROOT = "data/test/assets"
INDEX_FILE = os.path.join(ASSET_ROOT, "index.json")
STAGING_DIR = "data/test/media_staging"
NOTE_DIR = "data/notes"
NOTE_AUDIT_FILE = "data/test/note_audit.jsonl"

MAX_ASSETS_PER_KIND = 300      # 单类收藏上限，超出按最久未使用淘汰
MAX_FILE_BYTES = 8 * 1024 * 1024   # 单个素材体积上限
PENDING_TTL = 900              # 本轮收到的媒体对动作层可见的时长（秒）
MAX_PENDING = 20               # 暂存条目上限
RECENT_SENT_MEMORY = 3         # 记住最近发送的条数，用于去重

# 允许的素材扩展名；未知扩展按类型回落
_ALLOWED_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
_FALLBACK_EXT = {IMAGE: "jpg", STICKER: "gif"}

_lock = threading.RLock()
_index = None                  # 惰性加载的持久化字典
_pending = {}                  # {ref: {"kind","path","desc","ts"}}
_pending_seq = 0

NOTE_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff-]{1,32}$")
# 兼容旧引用的默认值；实际限额从 config 动态读取（见 _note_limit）
MAX_NOTE_CHARS = 200           # 单次写入（单条）字符上限
MAX_NOTE_READ_CHARS = 2000     # 单次读取字符上限，避免长笔记挤占上下文
DEFAULT_NOTE_ENTRY_CHARS = 200
DEFAULT_NOTE_FILE_CHARS = 8000
DEFAULT_NOTE_MAX_LINES = 100

# 按笔记名分锁：读-改-写必须串行，避免同一本笔记被并发改坏
_note_locks = {}
_note_locks_guard = threading.Lock()
# 笔记行缓存：{path: {"mtime", "size", "lines"}}，避免摘要时反复读整文件
_note_cache = {}


def _note_limit(key: str, fallback: int) -> int:
    """从全局配置读取笔记限额（支持运行期热更新）。"""
    try:
        from config.api_config import config
        return max(1, int(config.get(key, fallback)))
    except (TypeError, ValueError, ImportError):
        return fallback


def _note_lock(base: str) -> threading.RLock:
    with _note_locks_guard:
        lock = _note_locks.get(base)
        if lock is None:
            lock = threading.RLock()
            _note_locks[base] = lock
        return lock


# ---------- 索引读写 ----------
def _empty_index() -> dict:
    return {kind: {} for kind in _KINDS} | {
        "last_sent": {kind: "" for kind in _KINDS},
        "recent_sent": {kind: [] for kind in _KINDS},
    }


def _load() -> dict:
    """惰性加载索引；文件损坏时重建为空表，不抛异常中断主流程。"""
    global _index
    if _index is not None:
        return _index
    data = _empty_index()
    if os.path.exists(INDEX_FILE):
        try:
            with open(INDEX_FILE, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
            for kind in _KINDS:
                items = loaded.get(kind) or {}
                if isinstance(items, dict):
                    data[kind] = {str(k): str(v) for k, v in items.items()}
            last = loaded.get("last_sent") or {}
            for kind in _KINDS:
                data["last_sent"][kind] = str(last.get(kind) or "")
            # 兼容旧索引：没有 recent_sent 时用 last_sent 单条初始化
            recent = loaded.get("recent_sent") or {}
            for kind in _KINDS:
                items = recent.get(kind)
                if isinstance(items, list):
                    data["recent_sent"][kind] = [str(x) for x in items if x][-RECENT_SENT_MEMORY:]
                elif data["last_sent"][kind]:
                    data["recent_sent"][kind] = [data["last_sent"][kind]]
        except Exception:
            pass
    _index = data
    return _index


def _save():
    """原子落盘，避免读写竞争下产生半截文件。

    Windows 上 os.replace 偶发因杀毒/索引服务的瞬态占用而失败（WinError 5/32），
    这类占用转瞬即逝，短重试即可，不必让一次动作因为落盘抖动而失败。
    """
    with _lock:
        os.makedirs(ASSET_ROOT, exist_ok=True)
        tmp = INDEX_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_index, f, ensure_ascii=False, indent=2)
        for attempt in range(5):
            try:
                os.replace(tmp, INDEX_FILE)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))


def _asset_id(kind: str, desc: str) -> str:
    """由描述派生稳定短编号，供 LLM 指代；校验时反查回描述。"""
    digest = hashlib.sha1(f"{kind}|{desc}".encode("utf-8")).hexdigest()[:5]
    return f"{kind[0].upper()}{digest}"


def asset_path(kind: str, filename: str) -> str:
    return os.path.join(ASSET_ROOT, kind, filename)


# ---------- 收藏写入 ----------
def _to_gif_frame(im):
    """把一帧转成带透明索引的调色板图像。

    直接 convert("P") 会把透明区域压成不透明黑块，贴图会糊；
    这里先量化到 255 色，再把透明像素映射到一个专用索引色。
    """
    from PIL import Image

    rgba = im.convert("RGBA")
    alpha = rgba.getchannel("A")
    quant = rgba.convert("RGB").quantize(colors=255, method=Image.MEDIANCUT)

    # 透明索引取调色板里未使用的 255 号槽位
    transparent_index = 255
    mask = alpha.point(lambda a: 255 if a <= 128 else 0)
    quant.paste(transparent_index, mask)
    quant.info["transparency"] = transparent_index
    return quant


def _convert_to_gif(src_path: str, dst_path: str) -> bool:
    """把图片转成 GIF，交给 QQ 侧自动识别为表情包。

    目前 NapCat 没有可靠的"以表情包形式发送"接口，改走格式这条路：
    QQ 会把 .gif 当动画表情处理。多帧动图逐帧保留，静态图转单帧 GIF。
    成功返回 True；任一环节失败返回 False，由调用方回落为直接复制原文件。
    """
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(src_path) as im:
            is_animated = getattr(im, "is_animated", False)
            if is_animated:
                frames = []
                durations = []
                for i in range(getattr(im, "n_frames", 1)):
                    im.seek(i)
                    frames.append(_to_gif_frame(im))
                    durations.append(im.info.get("duration", 100) or 100)
                if not frames:
                    return False
                frames[0].save(
                    dst_path,
                    format="GIF",
                    save_all=True,
                    append_images=frames[1:],
                    duration=durations,
                    loop=im.info.get("loop", 0),
                    transparency=frames[0].info.get("transparency", 255),
                    disposal=2,
                )
            else:
                _to_gif_frame(im).save(
                    dst_path, format="GIF",
                    transparency=255,
                )
        return os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0
    except Exception:
        return False


def add_asset(kind: str, src_path: str, desc: str) -> dict | None:
    """把源文件收进素材库；描述相同视为同一素材，返回已存在条目。

    kind=sticker 时统一转存为 GIF，让 QQ 自动按表情包识别。
    """
    if kind not in _KINDS or not desc:
        return None
    desc = str(desc).strip()
    if not desc:
        return None
    with _lock:
        index = _load()
        existing = index[kind].get(desc)
        if existing:
            return {"kind": kind, "desc": desc, "file": existing, "path": asset_path(kind, existing), "added": False}

        src_path = str(src_path)
        if not os.path.isfile(src_path):
            return None
        try:
            if os.path.getsize(src_path) > MAX_FILE_BYTES:
                return None
        except OSError:
            return None

        os.makedirs(os.path.join(ASSET_ROOT, kind), exist_ok=True)

        # 表情包：先尝试转 GIF（QQ 据此识别为表情），失败再按原格式存
        if kind == STICKER:
            filename = f"{uuid.uuid4().hex[:12]}.gif"
            if _convert_to_gif(src_path, asset_path(kind, filename)):
                index[kind][desc] = filename
                _evict_if_needed(kind)
                _save()
                return {"kind": kind, "desc": desc, "file": filename,
                        "path": asset_path(kind, filename), "added": True}
            # 转换失败：退回原格式，至少保证素材不丢
            try:
                os.remove(asset_path(kind, filename))
            except OSError:
                pass

        ext = os.path.splitext(src_path)[1].lstrip(".").lower()
        if ext not in _ALLOWED_EXT:
            ext = _FALLBACK_EXT.get(kind, "jpg")
        filename = f"{uuid.uuid4().hex[:12]}.{ext}"
        try:
            shutil.copy2(src_path, asset_path(kind, filename))
        except OSError:
            return None

        index[kind][desc] = filename
        _evict_if_needed(kind)
        _save()
        return {"kind": kind, "desc": desc, "file": filename, "path": asset_path(kind, filename), "added": True}


def _evict_if_needed(kind: str):
    """超出上限时按「最早入库」淘汰，保持素材库有界。"""
    items = _index[kind]
    if len(items) <= MAX_ASSETS_PER_KIND:
        return
    overflow = len(items) - MAX_ASSETS_PER_KIND
    # 依赖 dict 的插入顺序：先入库的先淘汰
    for desc in list(items.keys())[:overflow]:
        filename = items.pop(desc)
        try:
            os.remove(asset_path(kind, filename))
        except OSError:
            pass


# ---------- 候选检索 ----------
def ordered_pool(kind: str, keywords: list = None) -> list:
    """返回该类的候选全集，按相关性排序。

    不做发送去重：是否重复发同一条由模型结合"最近发过什么"自行判断，
    硬性排除会挡掉"同一张图在新语境下确实想再发"这类真实需求。
    排序在单次调用内保持稳定，保证翻页时不会看到重复或跳过的条目。
    """
    if kind not in _KINDS:
        return []
    with _lock:
        index = _load()
        entries = [
            {"id": _asset_id(kind, desc), "desc": desc, "file": fn, "path": asset_path(kind, fn)}
            for desc, fn in index[kind].items()
        ]

    kws = [str(k) for k in (keywords or []) if k]
    rnd = random.random()
    for e in entries:
        hits = sum(1 for k in kws if k in e["desc"])
        # 随机量在相关性相同时打散顺序，避免永远只发最早收藏的那几张
        e["_rank"] = (-hits, (hash(e["id"]) ^ int(rnd * 1e9)) % 100000)
    entries.sort(key=lambda e: e["_rank"])
    for e in entries:
        e.pop("_rank", None)
    return entries


def resolve_token(kind: str, token: str) -> dict | None:
    """把 LLM 给出的编号（或原样描述）解析回素材条目。"""
    if kind not in _KINDS or not token:
        return None
    token = str(token).strip()
    with _lock:
        index = _load()
        for desc, fn in index[kind].items():
            if desc == token or _asset_id(kind, desc) == token:
                return {"kind": kind, "desc": desc, "file": fn, "path": asset_path(kind, fn)}
    return None


def _recent_sent_list(index: dict, kind: str) -> list:
    """取该类最近发送过的描述列表（新→旧）。"""
    items = (index.get("recent_sent") or {}).get(kind)
    if isinstance(items, list):
        return [str(x) for x in items if x]
    last = (index.get("last_sent") or {}).get(kind)
    return [str(last)] if last else []


def last_sent(kind: str) -> str:
    with _lock:
        return _load()["last_sent"].get(kind) or ""


def recent_sent(kind: str) -> list:
    """最近发送过的描述（新→旧），供去重与调试查看。"""
    with _lock:
        return _recent_sent_list(_load(), kind)


def mark_sent(kind: str, desc: str):
    """记录发送内容，供去重（不做时间冷却）。

    保留最近若干条而不只是最后一条：只在两条之间去重时，
    交替发送 A、B、A、B 也能绕过限制，看起来同样是"反复发同一批"。
    """
    if kind not in _KINDS:
        return
    desc = str(desc or "")
    if not desc:
        return
    with _lock:
        index = _load()
        index["last_sent"][kind] = desc
        items = _recent_sent_list(index, kind)
        items = [d for d in items if d != desc]     # 去重后置顶
        items.insert(0, desc)
        index["recent_sent"][kind] = items[:RECENT_SENT_MEMORY]
        _save()


def count(kind: str) -> int:
    with _lock:
        return len(_load()[kind])


def available_kinds() -> list:
    return [kind for kind in _KINDS if count(kind) > 0]


def stats() -> dict:
    with _lock:
        index = _load()
        return {
            "image": len(index[IMAGE]),
            "sticker": len(index[STICKER]),
            "last_sent": dict(index["last_sent"]),
        }


# ---------- 收到的媒体暂存 ----------
def register_pending(kind: str, src_path: str, desc: str, memory_ids: list = None) -> str | None:
    """把本轮收到的媒体暂存一份，返回短引用（如 m1）供动作层决定是否收藏。

    原图仍在 NapCat 的 QQ 缓存目录里（由 NapCat 自行管理），这里只留一份副本；
    未被收藏的副本由 cleanup_pending 按 TTL 清理。

    memory_ids: 该媒体描述对应的记忆 ID（可多条）。收藏时据此把“我收藏了…”
    紧接在图片记忆之后建立因果链接，让收藏这件事在记忆图里贴着原图。
    """
    global _pending_seq
    if kind not in _KINDS or not desc or not src_path:
        return None
    src_path = str(src_path)
    if not os.path.isfile(src_path):
        return None
    ext = os.path.splitext(src_path)[1].lstrip(".").lower()
    if ext not in _ALLOWED_EXT:
        ext = _FALLBACK_EXT.get(kind, "jpg")

    with _lock:
        cleanup_pending()
        os.makedirs(STAGING_DIR, exist_ok=True)
        _pending_seq += 1
        ref = f"m{_pending_seq}"
        dst = os.path.join(STAGING_DIR, f"{ref}_{uuid.uuid4().hex[:8]}.{ext}")
        try:
            shutil.copy2(src_path, dst)
        except OSError:
            return None
        _pending[ref] = {
            "kind": kind,
            "path": dst,
            "desc": str(desc).strip(),
            "ts": time.time(),
            "memory_ids": [str(m) for m in (memory_ids or []) if m],
        }
    return ref


def list_pending(max_age: float = PENDING_TTL) -> list:
    """列出仍在时效内的暂存媒体（新→旧）。"""
    now = time.time()
    with _lock:
        items = [
            {"ref": ref, **{k: v for k, v in item.items() if k != "ts"}}
            for ref, item in _pending.items()
            if now - item.get("ts", 0) <= max_age
        ]
    return items


def get_pending(ref: str) -> dict | None:
    with _lock:
        item = _pending.get(str(ref).strip())
        if not item:
            return None
        if time.time() - item.get("ts", 0) > PENDING_TTL:
            return None
        return {"ref": str(ref).strip(), **{k: v for k, v in item.items() if k != "ts"}}


def set_pending_memory_ids(ref: str, memory_ids: list):
    """把媒体描述对应的记忆 ID 记到暂存条目上（收藏时用于贴近原图建链）。"""
    with _lock:
        item = _pending.get(str(ref).strip())
        if not item:
            return False
        item["memory_ids"] = [str(m) for m in (memory_ids or []) if m]
        return True


def cleanup_pending(max_age: float = PENDING_TTL):
    """清理过期暂存条目与其文件副本。"""
    now = time.time()
    with _lock:
        for ref in list(_pending):
            item = _pending[ref]
            if now - item.get("ts", 0) <= max_age:
                continue
            try:
                os.remove(item.get("path", ""))
            except OSError:
                pass
            _pending.pop(ref, None)
        # 条目过多时同样按时间淘汰
        while len(_pending) > MAX_PENDING:
            oldest = min(_pending, key=lambda r: _pending[r].get("ts", 0))
            try:
                os.remove(_pending[oldest].get("path", ""))
            except OSError:
                pass
            _pending.pop(oldest, None)


# ---------- txt 笔记沙箱 ----------
_ENTRY_END_PUNCT = "。！？；…，、,.!?;"


def _safe_note_base(name: str) -> str | None:
    """校验笔记名并返回不含扩展名的基名；任何路径痕迹一律拒绝。

    含路径分隔符或 ".." 的输入直接拒绝，而不是静默归一化——
    静默改写会让"读/写到哪里"变得不可预期。
    """
    raw = str(name or "").strip()
    if not raw or "/" in raw or "\\" in raw or ".." in raw or "\x00" in raw:
        return None
    base = os.path.splitext(raw)[0]
    if not base or not NOTE_NAME_RE.match(base):
        return None
    return base


def _note_path(base: str) -> str:
    return os.path.join(NOTE_DIR, f"{base}.txt")


def _read_note_lines(path: str) -> list:
    """读取整份笔记的行（带 mtime/size 缓存，避免摘要时反复读大文件）。"""
    try:
        stat = os.stat(path)
    except OSError:
        return []
    cache = _note_cache.get(path)
    if cache and cache["mtime"] == stat.st_mtime and cache["size"] == stat.st_size:
        return list(cache["lines"])
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    _note_cache[path] = {"mtime": stat.st_mtime, "size": stat.st_size, "lines": list(lines)}
    return lines


def _invalidate_note_cache(path: str):
    _note_cache.pop(path, None)


def _audit_note(action: str, name: str, old_text: str = "", new_text: str = "", detail: str = ""):
    """笔记改动必须留痕（与"记忆即人格、改动需记录"的伦理约束一致）。"""
    record = {
        "ts": time.time(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "action": action,
        "name": name,
        "old_text": old_text,
        "new_text": new_text,
        "detail": detail,
    }
    try:
        atomic_append_line(NOTE_AUDIT_FILE, json.dumps(record, ensure_ascii=False))
    except Exception:
        pass


def _truncate_entry(text: str, limit: int) -> tuple:
    """单条超长时在句末标点处截断；返回 (文本, 是否截断)。"""
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    cut = max(head.rfind(p) for p in _ENTRY_END_PUNCT)
    if cut >= limit // 2:
        head = head[:cut + 1]
    return head.rstrip(), True


def recent_note_lines(name: str, limit: int = None) -> list:
    """取笔记最近的若干行，供抉择层了解"已经记过什么"。

    只读不改，也不做任何去重判定——是否重复记由模型自己判断。
    """
    base = _safe_note_base(name)
    if not base:
        return []
    if limit is None:
        limit = _note_limit("note_brief_recent_lines", 5)
    lines = [l.strip() for l in _read_note_lines(_note_path(base)) if l.strip()]
    return lines[-max(1, limit):]


def note_line_count(name: str) -> int:
    """笔记的条目总数（非空行数），供抉择层了解文件规模。"""
    base = _safe_note_base(name)
    if not base:
        return 0
    return sum(1 for l in _read_note_lines(_note_path(base)) if l.strip())


def note_stats(name: str) -> dict:
    """笔记规模（字符/行/字节），供提示词与限额判断。"""
    base = _safe_note_base(name)
    if not base:
        return {"name": str(name or ""), "valid": False, "chars": 0, "lines": 0, "bytes": 0}
    path = _note_path(base)
    lines = _read_note_lines(path)
    text = "\n".join(lines)
    try:
        nbytes = os.path.getsize(path)
    except OSError:
        nbytes = 0
    return {
        "name": base,
        "valid": True,
        "chars": len(text),
        "lines": sum(1 for l in lines if l.strip()),
        "bytes": nbytes,
    }


def write_note_ex(name: str, text: str) -> dict:
    """写入一条笔记，返回结构化结果（供动作层区分失败原因）。

    返回键：path / name / written / truncated / error / used_chars / used_lines
    error 取值：invalid_name | empty_text | note_full | write_failed | None
    """
    base = _safe_note_base(name)
    if not base:
        return {"path": None, "name": "", "written": "", "truncated": False,
                "error": "invalid_name", "used_chars": 0, "used_lines": 0}
    content = str(text or "").strip()
    if not content:
        return {"path": None, "name": base, "written": "", "truncated": False,
                "error": "empty_text", "used_chars": 0, "used_lines": 0}

    entry_limit = _note_limit("note_entry_max_chars", DEFAULT_NOTE_ENTRY_CHARS)
    file_limit = _note_limit("note_file_max_chars", DEFAULT_NOTE_FILE_CHARS)
    line_limit = _note_limit("note_max_lines", DEFAULT_NOTE_MAX_LINES)

    content, truncated = _truncate_entry(content, entry_limit)
    os.makedirs(NOTE_DIR, exist_ok=True)
    path = _note_path(base)

    with _note_lock(base):
        existing_lines = [l for l in _read_note_lines(path) if l.strip()]
        used_chars = sum(len(l) for l in existing_lines)
        used_lines = len(existing_lines)
        added_chars = len(content)
        if used_chars + added_chars + 1 > file_limit or used_lines + 1 > line_limit:
            return {"path": None, "name": base, "written": content, "truncated": truncated,
                    "error": "note_full", "used_chars": used_chars, "used_lines": used_lines}

        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(content + "\n")
        except OSError:
            return {"path": None, "name": base, "written": content, "truncated": truncated,
                    "error": "write_failed", "used_chars": used_chars, "used_lines": used_lines}
        _invalidate_note_cache(path)

    _audit_note("write", base, "", content, "truncated" if truncated else "")
    return {"path": path, "name": base, "written": content, "truncated": truncated,
            "error": None, "used_chars": used_chars + added_chars, "used_lines": used_lines + 1}


def write_note(name: str, text: str) -> str | None:
    """兼容旧接口：返回写入后的路径；校验失败/已满返回 None。

    需要区分失败原因（如"笔记已满"）时用 write_note_ex。
    """
    return write_note_ex(name, text).get("path")


def _find_line_indexes(lines: list, target: str) -> list:
    """返回归一化后与 target 相同的所有行下标（0-based）。"""
    return [
        i for i, line in enumerate(lines)
        if _normalize_note_line(line) == target
    ]


def edit_note_ex(name: str, old_text: str, new_text: str = None, line: int = None) -> dict:
    """修改笔记里的指定一行，返回结构化结果。

    - line 提供时按 1-based 行号定位（优先于 old_text）；
    - 否则要求 old_text 归一化后**唯一命中**，多命中时拒绝并返回候选行号；
    - new_text 为空字符串表示删除该行；为 None 表示未提供，报错而不是隐式删除。
    """
    base = _safe_note_base(name)
    if not base:
        return {"error": "invalid_name", "path": None, "candidates": []}
    if new_text is None:
        return {"error": "missing_new_text", "path": None, "candidates": []}
    path = _note_path(base)
    if not os.path.isfile(path):
        return {"error": "note_missing", "path": None, "candidates": []}

    replacement = str(new_text or "").strip()
    with _note_lock(base):
        lines = _read_note_lines(path)
        if not lines:
            return {"error": "note_empty", "path": None, "candidates": []}

        target_index = None
        if line is not None:
            try:
                idx = int(line) - 1
            except (TypeError, ValueError):
                return {"error": "invalid_line", "path": None, "candidates": []}
            if not (0 <= idx < len(lines)) or not lines[idx].strip():
                return {"error": "invalid_line", "path": None, "candidates": []}
            target_index = idx
        else:
            target = _normalize_note_line(old_text)
            if not target:
                return {"error": "invalid_old_text", "path": None, "candidates": []}
            hits = _find_line_indexes(lines, target)
            if not hits:
                return {"error": "not_found", "path": None, "candidates": []}
            if len(hits) > 1:
                candidates = [
                    {"line": i + 1, "text": lines[i].strip()[:60]} for i in hits[:10]
                ]
                return {"error": "ambiguous", "path": None, "candidates": candidates}
            target_index = hits[0]

        original = lines[target_index]
        if replacement:
            prefix = _list_prefix_of(original)
            lines[target_index] = f"{prefix}{replacement}"
        else:
            del lines[target_index]

        if not _write_note_lines(path, lines):
            return {"error": "write_failed", "path": None, "candidates": []}
        _invalidate_note_cache(path)

    _audit_note("edit" if replacement else "delete", base, original.strip(), replacement)
    return {
        "error": None,
        "path": path,
        "name": base,
        "line": target_index + 1,
        "old_text": original.strip(),
        "new_text": replacement,
        "deleted": not replacement,
        "kept": len(lines),
    }


def edit_note(name: str, old_text: str, new_text: str = "") -> dict | None:
    """兼容旧接口：修改成功返回 {"path","changed","kept"}，失败返回 None。"""
    result = edit_note_ex(name, old_text, new_text)
    if result.get("error"):
        return None
    return {
        "path": result["path"],
        "changed": 1,
        "kept": result["kept"],
        "line": result.get("line"),
        "deleted": result.get("deleted", False),
    }


def delete_note_line(name: str, old_text: str = None, line: int = None) -> dict:
    """删除笔记里的一行（独立动作，不再依赖"省略 new_text"）。"""
    return edit_note_ex(name, old_text, new_text="", line=line)


_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*•]|\d+[.、)）])\s*")


def _list_prefix_of(line: str) -> str:
    m = _LIST_PREFIX_RE.match(str(line or ""))
    return m.group(0) if m else ""


def _normalize_note_line(text: str) -> str:
    """归一化一行用于匹配：去掉列表前缀、首尾空白与常见收尾标点。"""
    line = _LIST_PREFIX_RE.sub("", str(text or "").strip())
    return line.strip().strip("。．.；;：:")


def _write_note_lines(path: str, lines: list) -> bool:
    """原子重写整个文件（先写临时文件再替换），避免中途失败留下半截内容。"""
    text = "".join(f"{line}\n" for line in lines)
    return atomic_write_text(path, text)


def read_note(name: str, max_chars: int = None) -> dict | None:
    """读取 data/notes/ 下的 txt 笔记。

    返回 {"name","path","text","truncated","total_chars","total_lines","start_line","end_line"}；
    文件不存在或名校验失败返回 None。截断时保留**最近**内容，并给出行号范围。
    """
    base = _safe_note_base(name)
    if not base:
        return None
    path = _note_path(base)
    if not os.path.isfile(path):
        return None
    if max_chars is None:
        max_chars = _note_limit("note_read_max_chars", MAX_NOTE_READ_CHARS)

    lines = _read_note_lines(path)
    text = "\n".join(lines).strip()
    if not text:
        return None
    total_lines = sum(1 for l in lines if l.strip())
    total_chars = len(text)
    truncated = total_chars > max_chars
    start_line = 1
    if truncated:
        text = text[-max_chars:]
        kept_lines = text.splitlines()
        start_line = max(1, len(lines) - len(kept_lines) + 1)
    return {
        "name": base,
        "path": path,
        "text": text,
        "truncated": truncated,
        "total_chars": total_chars,
        "total_lines": total_lines,
        "start_line": start_line,
        "end_line": len(lines),
    }


def list_notes() -> list:
    """列出已有笔记名（不含扩展名），供抉择时作为可选清单。"""
    if not os.path.isdir(NOTE_DIR):
        return []
    names = []
    for fn in sorted(os.listdir(NOTE_DIR)):
        if not fn.lower().endswith(".txt"):
            continue
        base = os.path.splitext(fn)[0]
        if NOTE_NAME_RE.match(base) and os.path.getsize(os.path.join(NOTE_DIR, fn)) > 0:
            names.append(base)
    return names


def note_exists(name: str) -> bool:
    base = _safe_note_base(name)
    if not base:
        return False
    return os.path.isfile(_note_path(base))
