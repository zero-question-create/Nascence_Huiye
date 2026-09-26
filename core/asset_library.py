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

IMAGE = "image"
STICKER = "sticker"
_KINDS = (IMAGE, STICKER)

ASSET_ROOT = "data/test/assets"
INDEX_FILE = os.path.join(ASSET_ROOT, "index.json")
STAGING_DIR = "data/test/media_staging"
NOTE_DIR = "data/notes"

MAX_ASSETS_PER_KIND = 300      # 单类收藏上限，超出按最久未使用淘汰
MAX_FILE_BYTES = 8 * 1024 * 1024   # 单个素材体积上限
PENDING_TTL = 900              # 本轮收到的媒体对动作层可见的时长（秒）
MAX_PENDING = 20               # 暂存条目上限

# 允许的素材扩展名；未知扩展按类型回落
_ALLOWED_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
_FALLBACK_EXT = {IMAGE: "jpg", STICKER: "gif"}

_lock = threading.RLock()
_index = None                  # 惰性加载的持久化字典
_pending = {}                  # {ref: {"kind","path","desc","ts"}}
_pending_seq = 0

NOTE_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff-]{1,32}$")
MAX_NOTE_CHARS = 1500          # 单次写入字符上限
MAX_NOTE_READ_CHARS = 3000     # 单次读取字符上限，避免长笔记挤占上下文


# ---------- 索引读写 ----------
def _empty_index() -> dict:
    return {kind: {} for kind in _KINDS} | {"last_sent": {kind: "" for kind in _KINDS}}


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
    """把源文件收进素材库；描述重复视为同一素材，返回已存在条目。

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
    """返回该类的候选全集（已排除上一条发送记录），按相关性排序。

    排序在单次调用内保持稳定，保证翻页时不会看到重复或跳过的条目。
    """
    if kind not in _KINDS:
        return []
    with _lock:
        index = _load()
        last = index["last_sent"].get(kind) or ""
        entries = [
            {"id": _asset_id(kind, desc), "desc": desc, "file": fn, "path": asset_path(kind, fn)}
            for desc, fn in index[kind].items()
            if desc != last
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


def last_sent(kind: str) -> str:
    with _lock:
        return _load()["last_sent"].get(kind) or ""


def mark_sent(kind: str, desc: str):
    """记录上一条发送内容，供去重（不做时间冷却）。"""
    if kind not in _KINDS:
        return
    with _lock:
        _load()["last_sent"][kind] = str(desc or "")
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
def register_pending(kind: str, src_path: str, desc: str) -> str | None:
    """把本轮收到的媒体暂存一份，返回短引用（如 m1）供动作层决定是否收藏。

    原图仍在 NapCat 的 QQ 缓存目录里（由 NapCat 自行管理），这里只留一份副本；
    未被收藏的副本由 cleanup_pending 按 TTL 清理。
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
        _pending[ref] = {"kind": kind, "path": dst, "desc": str(desc).strip(), "ts": time.time()}
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


def note_contains(name: str, text: str) -> bool:
    """判断笔记里是否已经写过同样的内容（用于写入前查重）。

    比对时忽略首尾空白与常见列表前缀（如 "1. "、"- "），
    这样"1. 交诗"与"交诗"视为同一条，避免同一件事被反复追加。
    """
    content = _normalize_note_line(text)
    if not content:
        return False
    note = read_note(name, max_chars=64 * 1024)
    if not note:
        return False
    for line in note["text"].splitlines():
        if _normalize_note_line(line) == content:
            return True
    return False


_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*•]|\d+[.、)）])\s*")


def _normalize_note_line(text: str) -> str:
    """归一化一行笔记：去掉列表前缀、首尾空白与常见收尾标点。"""
    line = _LIST_PREFIX_RE.sub("", str(text or "").strip())
    return line.strip().strip("。．.；;：:")


def write_note(name: str, text: str) -> str | None:
    """只允许在 data/notes/ 下写 .txt；文件名白名单校验，内容追加不覆盖。

    追加前先查重：同样内容已存在则不重复写入（返回路径但不追加），
    避免同一件事被反复记录。返回写入后的路径；校验失败返回 None。
    """
    base = _safe_note_base(name)
    if not base:
        return None
    content = str(text or "").strip()
    if not content:
        return None
    content = content[:MAX_NOTE_CHARS]

    os.makedirs(NOTE_DIR, exist_ok=True)
    path = os.path.join(NOTE_DIR, f"{base}.txt")

    # 已写过同样内容：不重复追加（与既有列表项比对时忽略 "1. "/"- " 等前缀）
    if note_contains(base, content):
        return path

    try:
        if os.path.exists(path) and os.path.getsize(path) + len(content.encode("utf-8")) > 64 * 1024:
            return None
        with open(path, "a", encoding="utf-8") as f:
            f.write(content + "\n")
    except OSError:
        return None
    return path


def read_note(name: str, max_chars: int = MAX_NOTE_READ_CHARS) -> dict | None:
    """读取 data/notes/ 下的 txt 笔记。

    返回 {"name", "path", "text", "truncated"}；文件不存在或名校验失败返回 None。
    与写入同一套沙箱：只认文件名，任何路径痕迹直接拒绝。
    """
    base = _safe_note_base(name)
    if not base:
        return None
    path = os.path.join(NOTE_DIR, f"{base}.txt")
    if not os.path.isfile(path):
        return None
    try:
        # utf-8-sig：用户手写的笔记可能被记事本加上 BOM，避免读出 \ufeff
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            text = f.read(max_chars + 1)
    except OSError:
        return None
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    text = text.strip()
    if not text:
        return None
    return {"name": base, "path": path, "text": text, "truncated": truncated}


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
    return os.path.isfile(os.path.join(NOTE_DIR, f"{base}.txt"))
