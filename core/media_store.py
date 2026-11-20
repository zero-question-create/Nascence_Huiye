# core/media_store.py
# ========================================================================
# 媒体完整内容存储。
#
# 与 asset_library 的区别：
# - asset_library 管理“可发送的图片/表情包素材”；
# - media_store 保存每条消息里图片/表情包的完整 LLM 描述、OCR 和文件，
#   供 read_media 全量回读，不把 30/60 字摘要当成唯一内容。
#
# 主键使用内容 SHA256，避免“描述相同 = 同一张图”导致数据丢失。
# ========================================================================

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

PROJECT_DIR = Path(__file__).resolve().parent.parent
STORE_FILE = PROJECT_DIR / "data" / "test" / "media_store.json"
MEDIA_DIR = PROJECT_DIR / "data" / "test" / "media"
_LOCK = threading.RLock()


def _load() -> dict:
    if STORE_FILE.is_file():
        try:
            with STORE_FILE.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("version", 1)
                data.setdefault("items", {})
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {"version": 1, "items": {}}


def _save(data: dict) -> None:
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE_FILE)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def save_media(
    kind: str,
    source_path: str,
    caption: str = "",
    full_description: str = "",
    ocr: str = "",
    message_id: str = "",
    sender: str = "",
    pack_id: str = "",
    emoji_id: str = "",
    meta: dict = None,
) -> Optional[dict]:
    """保存一条媒体记录；文件按 hash 复制到 data/test/media。"""
    source = Path(str(source_path or ""))
    if not source.is_file():
        return None
    try:
        digest = _hash_file(source)
    except OSError:
        return None
    media_id = f"m_{digest[:16]}"
    suffix = source.suffix.lower() or ".bin"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    stored = MEDIA_DIR / f"{media_id}{suffix}"
    with _LOCK:
        data = _load()
        item = data["items"].get(media_id) or {}
        if not stored.exists():
            try:
                shutil.copy2(source, stored)
            except OSError:
                stored = source
        item.update({
            "id": media_id,
            "hash": digest,
            "kind": str(kind or "image"),
            "path": str(stored),
            "source_path": str(source),
            "caption": str(caption or "")[:500],
            "full_description": str(full_description or ""),
            "ocr": str(ocr or ""),
            "message_id": str(message_id or ""),
            "sender": str(sender or ""),
            "pack_id": str(pack_id or ""),
            "emoji_id": str(emoji_id or ""),
            "meta": meta if isinstance(meta, dict) else {},
            "updated_at": time.time(),
        })
        item.setdefault("created_at", time.time())
        data["items"][media_id] = item
        _save(data)
        return dict(item)


def get(media_id: str) -> Optional[dict]:
    with _LOCK:
        item = (_load().get("items") or {}).get(str(media_id or ""))
        return dict(item) if item else None


def find_by_message(message_id: str) -> list[dict]:
    message_id = str(message_id or "")
    if not message_id:
        return []
    with _LOCK:
        return [
            dict(item) for item in (_load().get("items") or {}).values()
            if item.get("message_id") == message_id
        ]


def recent(limit: int = 10) -> list[dict]:
    with _LOCK:
        items = list((_load().get("items") or {}).values())
    items.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    return [dict(item) for item in items[: max(1, int(limit or 10))]]


def count() -> int:
    with _LOCK:
        return len((_load().get("items") or {}))
