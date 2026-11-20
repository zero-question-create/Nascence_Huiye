# core/skills/builtin/bilibili_video.py
# B站视频分析：默认最低画质，下载后抽关键帧交给视觉模型，返回摘要。
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from config.api_config import config
from core import media_store, skill_registry
from core.llm_interface import call_api_thinking, describe_audio_from_path, describe_image_full

PROJECT_DIR = Path(__file__).resolve().parents[3]
BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")
QUALITY_FORMATS = {
    # B站 DASH 把视频/音频分开；显式合并，避免选到纯音频或不可用格式。
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
}


def _extract_bvid(text: str) -> str:
    match = BVID_RE.search(str(text or ""))
    return match.group(0) if match else ""


def _quality_format(quality: str) -> str:
    return QUALITY_FORMATS.get(str(quality or "360p").lower(), QUALITY_FORMATS["360p"])


def _ffmpeg_exe() -> str:
    env = os.environ.get("NHY_FFMPEG", "").strip()
    if env and Path(env).is_file():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        from imageio_ffmpeg import get_ffmpeg_exe

        path = get_ffmpeg_exe()
        if path and Path(path).is_file():
            return path
    except Exception:
        pass
    return ""


def _fmt_bytes(value) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "未知"
    if number >= 1024 * 1024:
        return f"{number / 1024 / 1024:.1f} MB"
    if number >= 1024:
        return f"{number / 1024:.0f} KB"
    return f"{number:.0f} B"


def _too_large(max_mb: float, actual_mb: float) -> dict:
    return {
        "ok": False,
        "detail": f"视频过大：{actual_mb:.1f} MB，超过当前上限 {max_mb:.0f} MB；"
        "请在插件面板提高上限或改用更低画质。",
        "too_large": True,
    }


async def _fetch_meta(bvid: str) -> dict:
    from plugins.bilibili.client import BiliClient

    client = BiliClient()
    try:
        data = await client.watch(bvid)
    finally:
        await client.close()
    return data


def _download_video(url: str, target_dir: Path, quality: str, headers: dict, max_mb: float):
    try:
        import yt_dlp
    except Exception:
        return None, "未安装 yt-dlp；请先安装 requirements.txt 中的 yt-dlp。", None
    opts = {
        "format": _quality_format(quality),
        "outtmpl": str(target_dir / "video.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 1,
        "socket_timeout": 25,
        "http_headers": headers or {},
    }
    ffmpeg = _ffmpeg_exe()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return None, "yt-dlp 没有解析到视频信息", None
            duration = info.get("duration")
            filesize = info.get("filesize") or info.get("filesize_approx")
            if filesize and float(filesize) > max_mb * 1024 * 1024:
                return None, (
                    f"视频过大：约 {float(filesize) / 1024 / 1024:.1f} MB，"
                    f"超过当前上限 {max_mb:.0f} MB"
                ), info
            ydl.download([url])
    except Exception as exc:
        return None, f"视频下载失败：{exc}", None
    files = sorted(
        target_dir.glob("video.*"),
        key=lambda path: path.stat().st_size if path.exists() else 0,
        reverse=True,
    )
    if not files:
        return None, "下载完成但没有找到视频文件", None
    return files[0], "", info


def _probe_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        return float(result.stdout.strip() or 0)
    except Exception:
        return 0.0


def _extract_frames(path: Path, out_dir: Path, count: int, duration: float) -> list[Path]:
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        return []
    count = max(1, min(int(count or 8), 24))
    duration = duration if duration > 0 else _probe_duration(path)
    if duration <= 0:
        duration = 60
    frames = []
    for index in range(count):
        timestamp = duration * (index + 0.5) / count
        target = out_dir / f"frame_{index:02d}.jpg"
        try:
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.2f}",
                 "-i", str(path), "-frames:v", "1", "-q:v", "2", str(target)],
                capture_output=True, timeout=30,
            )
            if target.is_file() and target.stat().st_size > 0:
                frames.append(target)
        except Exception:
            continue
    return frames


def _extract_audio(path: Path, out_dir: Path) -> Path | None:
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        return None
    target = out_dir / "audio.wav"
    try:
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-vn", "-ac", "1", "-ar", "16000", str(target)],
            capture_output=True, timeout=60,
        )
        return target if target.is_file() and target.stat().st_size > 0 else None
    except Exception:
        return None


async def _analyze(
    bvid: str,
    meta: dict,
    video_path: Path,
    frames: list[Path],
    audio_path: Path | None,
) -> str:
    frame_descs = []
    for index, frame in enumerate(frames):
        desc = await describe_image_full(str(frame), timeout=120.0)
        frame_descs.append(f"第 {index + 1} 帧：{desc}")
    audio_desc = ""
    if audio_path is not None:
        try:
            audio_desc = await describe_audio_from_path(str(audio_path))
        except Exception as exc:
            audio_desc = f"音频分析失败：{exc}"
    prompt = (
        "你是视频内容分析助手。根据 B 站视频的元数据、字幕、弹幕/评论和关键帧描述，"
        "输出一份完整但结构清晰的中文摘要：\n"
        "1. 视频主题与内容脉络；\n"
        "2. 关键画面/人物/文字信息；\n"
        "3. 字幕或音频里明确出现的重要信息；\n"
        "4. 不确定的地方写“不确定”，不要编造。\n\n"
        f"标题：{meta.get('title')}\n作者：{meta.get('author')}\n"
        f"简介：{str(meta.get('desc') or '')[:2000]}\n"
        f"字幕：{str(meta.get('subtitles') or '')[:6000]}\n"
        f"关键帧：\n" + "\n".join(frame_descs)[:12000] +
        (f"\n音频：{audio_desc[:3000]}" if audio_desc else "")
    )
    try:
        return await asyncio.to_thread(
            lambda: call_api_thinking(
                [{"role": "user", "content": prompt}],
                max_tokens=1000,
                thinking=False,
                timeout=90,
            )
        )
    except Exception:
        # LLM 总结失败也要把原始分析返回，不能丢数据。
        return "\n".join(frame_descs)[:8000]


def run(data: dict) -> dict:
    data = data if isinstance(data, dict) else {}
    source = str(data.get("url") or data.get("bvid") or "").strip()
    bvid = _extract_bvid(source)
    if not bvid:
        return {"ok": False, "detail": "请给一个 B 站 BV 号或视频链接。"}
    settings = skill_registry.skill_config("bilibili_video")
    quality = str(data.get("quality") or settings.get("bilibili_quality") or "360p").lower()
    if quality != "360p" and not bool(settings.get("bilibili_allow_higher_quality", True)):
        return {"ok": False, "detail": "更高画质已在插件面板关闭，当前只允许 360p。"}
    max_mb = float(settings.get("bilibili_max_mb") or 80)
    max_seconds = float(settings.get("bilibili_max_seconds") or 600)

    try:
        meta = asyncio.run(_fetch_meta(bvid))
    except Exception as exc:
        return {"ok": False, "detail": f"读取 B 站视频信息失败：{exc}"}
    duration = float(meta.get("duration") or 0)
    if bool(data.get("analyze", True)) and max_seconds and duration > max_seconds:
        return {
            "ok": False,
            "detail": f"视频时长 {duration:.0f}s 超过上限 {max_seconds:.0f}s；"
            "请在插件面板提高上限或换更短的视频。",
        }

    # 只读模式：不需要下载视频时，直接返回元数据/字幕摘要。
    if not bool(data.get("analyze", True)):
        detail = (
            f"《{meta.get('title')}》 {meta.get('author')} · {duration:.0f}s\n"
            f"简介：{str(meta.get('desc') or '')[:1000]}\n"
            f"字幕：{str(meta.get('subtitles') or '')[:2000]}"
        )
        return {"ok": True, "detail": detail, "meta": meta, "content_lines": []}

    headers = {}
    try:
        from plugins.bilibili.client import BiliClient

        headers = dict(BiliClient().headers)
    except Exception:
        pass
    work = Path(tempfile.mkdtemp(prefix="nhy_bili_", dir=str(PROJECT_DIR / "data" / "test")))
    try:
        video_path, error, info = _download_video(
            f"https://www.bilibili.com/video/{bvid}/", work, quality, headers, max_mb
        )
        if not video_path:
            return {"ok": False, "detail": error or "没有下载到视频文件", "too_large": "过大" in str(error)}
        size_mb = video_path.stat().st_size / 1024 / 1024
        if size_mb > max_mb:
            return _too_large(max_mb, size_mb)
        frames = _extract_frames(
            video_path, work, int(settings.get("bilibili_frames") or 8), duration
        )
        audio = _extract_audio(video_path, work) if data.get("include_audio") else None
        summary = asyncio.run(_analyze(bvid, meta, video_path, frames, audio))
        if not summary:
            return {"ok": False, "detail": "视频分析没有得到内容"}
        # 摘要存成文本媒体记录，便于 read_media 回看。
        summary_path = work / f"{bvid}_summary.txt"
        summary_path.write_text(summary, encoding="utf-8")
        record = media_store.save_media(
            "video_analysis",
            str(summary_path),
            caption=summary[:200],
            full_description=summary,
            message_id=f"bili_{bvid}",
            meta={"bvid": bvid, "quality": quality, "frames": len(frames)},
        )
        return {
            "ok": True,
            "detail": summary,
            "media_id": (record or {}).get("id", ""),
            "meta": {
                "bvid": bvid,
                "title": meta.get("title"),
                "duration": duration,
                "quality": quality,
                "size_mb": round(size_mb, 1),
                "frames": len(frames),
            },
            "content_lines": [],
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)
