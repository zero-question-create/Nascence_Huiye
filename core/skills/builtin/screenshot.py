# core/skills/builtin/screenshot.py
# 截屏技能：抓当前窗口或全屏，保存文件并交给视觉模型做完整分析。
from __future__ import annotations

import os
import time
from pathlib import Path

from config.api_config import config
from core import media_store, skill_registry
from core.llm_interface import describe_image_full

PROJECT_DIR = Path(__file__).resolve().parents[3]
SHOT_DIR = PROJECT_DIR / "data" / "test" / "screenshots"


def _summary(text: str, limit: int = 1200) -> str:
    text = " ".join(str(text or "").split())
    return text[: max(200, limit)]


def run(data: dict) -> dict:
    data = data if isinstance(data, dict) else {}
    if not bool(config.get("desktop_allow_screenshot", False)):
        return {
            "ok": False,
            "detail": "截屏被 desktop_allow_screenshot 关闭；请先在电脑插件配置里允许截屏。",
        }
    settings = skill_registry.skill_config("screenshot")
    scope = str(data.get("target") or settings.get("screenshot_scope") or "active").lower()
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    filename = SHOT_DIR / f"shot_{int(time.time() * 1000)}.png"
    hwnd = 0
    try:
        if scope in ("screen", "full", "desktop"):
            from PIL import ImageGrab

            image = ImageGrab.grab(all_screens=True)
            image.save(filename)
        else:
            from plugins.desktop import uia

            hwnd = int(data.get("hwnd") or 0) or uia.foreground_window()
            if not hwnd:
                return {"ok": False, "detail": "没有找到可截图的窗口"}
            result = uia.backend().screenshot(hwnd, str(filename))
            if not result.get("ok"):
                return {"ok": False, "detail": f"截图失败：{result.get('error')}"}
    except Exception as exc:
        return {"ok": False, "detail": f"截图失败：{exc}"}
    if not filename.is_file():
        return {"ok": False, "detail": "截图文件没有生成"}

    # 视觉调用是异步的；技能执行入口是同步的，这里用独立事件循环执行。
    import asyncio

    try:
        description = asyncio.run(describe_image_full(str(filename), timeout=120.0))
    except RuntimeError:
        # 已在事件循环里（未来从 async 调用）时，退回线程池执行。
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            description = pool.submit(
                lambda: asyncio.run(describe_image_full(str(filename), timeout=120.0))
            ).result()
    if description.startswith("[图片识别失败") or description.startswith("[图片文件不存在"):
        return {"ok": False, "detail": description, "path": str(filename)}

    record = media_store.save_media(
        "screenshot",
        str(filename),
        caption=_summary(description, 200),
        full_description=description,
        message_id=f"shot_{int(time.time())}",
        meta={"hwnd": hwnd, "scope": scope},
    )
    return {
        "ok": True,
        "detail": f"截图分析：{_summary(description, settings.get('screenshot_max_chars', 1200))}",
        "path": str(filename),
        "media_id": (record or {}).get("id", ""),
        "content_lines": [],
    }
