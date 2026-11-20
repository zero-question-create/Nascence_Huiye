# core/skills/builtin/read_media.py
# 读取媒体完整描述：不截断，供辉夜按 media_id 回看照片/表情包全文。
from __future__ import annotations

from core import media_store, skill_registry


def run(data: dict) -> dict:
    data = data if isinstance(data, dict) else {}
    media_id = str(data.get("media_id") or data.get("id") or "").strip()
    if not media_id:
        items = media_store.recent(10)
        if not items:
            return {"ok": False, "detail": "媒体库里还没有记录。"}
        lines = []
        for item in items:
            lines.append(
                f"- {item.get('id')} [{item.get('kind')}] "
                f"{(item.get('caption') or '')[:80]}"
            )
        return {
            "ok": True,
            "detail": "最近的媒体（用 media_id 读取全文）：\n" + "\n".join(lines),
            "content_lines": lines,
        }
    item = media_store.get(media_id)
    if not item:
        return {"ok": False, "detail": f"找不到媒体 {media_id}"}
    full = str(item.get("full_description") or item.get("caption") or "")
    ocr = str(item.get("ocr") or "")
    detail = f"{item.get('kind') or '媒体'} {media_id}\n{full}"
    if ocr:
        detail += f"\n\nOCR：\n{ocr}"
    return {
        "ok": True,
        "detail": detail,
        "media": item,
        "content_lines": [],
    }
