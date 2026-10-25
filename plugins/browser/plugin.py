# plugins/browser/plugin.py
# ========================================================================
# 浏览器插件（网页解析）。
#
# 定位：给其他插件/后续的网页、小程序、Bilibili 插件提供“把网页变成文本”的
# 基础能力；它不是聊天软件，不出现在本地软件门控里（is_app=False）。
#
# 两种模式：
#   static  — 直接抓取 + 标准库 HTMLParser，提取标题/描述/正文/链接；
#   dynamic — POST 给网页解析接口（默认 DeepSeek Fetch 形态
#             https://api.deepseek.com/v1/tools/fetch），拿回 Markdown。
# 两种模式都会用一个 LLM 把内容概括成摘要，再写入主进程的短期上下文。
#
# 不引入新依赖：aiohttp 已是项目依赖。
# ========================================================================

import asyncio
import json
import re
from html import unescape
from html.parser import HTMLParser
from typing import List
from urllib.parse import urljoin, urlparse

import aiohttp

from config.api_config import config
from plugins.base import Capability, Plugin
from utils.monitor import append_log

_SKIP_TAGS = {
    "script", "style", "noscript", "svg", "canvas", "iframe",
    "nav", "footer", "aside", "form", "button", "select", "option",
}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "header", "h1", "h2", "h3",
    "h4", "h5", "h6", "li", "ul", "ol", "tr", "td", "th", "blockquote",
    "pre", "br", "hr", "table",
}
_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([a-zA-Z0-9_\-]+)""", re.I)


def _cfg_int(key: str, default: int) -> int:
    try:
        return max(1, int(config.get(key, default)))
    except (TypeError, ValueError):
        return default


def _cfg_str(key: str, default: str) -> str:
    value = config.get(key)
    return str(value if value is not None else default).strip()


def _sniff_charset(raw: bytes) -> str:
    match = _CHARSET_RE.search(raw[:4096])
    if match:
        try:
            return match.group(1).decode("ascii", errors="ignore") or "utf-8"
        except Exception:
            pass
    return "utf-8"


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.title: List[str] = []
        self.description = ""
        self.links: List[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs or [])
        if tag == "meta":
            name = str(attrs.get("name") or attrs.get("property") or "").lower()
            if name in ("description", "og:description", "twitter:description"):
                content = str(attrs.get("content") or "").strip()
                if content and not self.description:
                    self.description = unescape(content)
            return
        if tag == "a":
            href = str(attrs.get("href") or "").strip()
            if href:
                self.links.append(href)
        if tag == "title":
            self._in_title = True
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = unescape(str(data or ""))
        if not text:
            return
        if self._in_title:
            self.title.append(text)
        else:
            self.parts.append(text)


def _clean_text(parts: List[str], max_chars: int) -> str:
    lines = []
    for raw_line in "".join(parts).replace("\r", "\n").replace("\xa0", " ").split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip()
        if not line:
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…（已截断）"
    return text


def _truncate(text: str, max_chars: int) -> str:
    text = str(text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…（已截断）"


class BrowserPlugin(Plugin):
    plugin_id = "browser"
    label = "浏览器"
    description = "静态网页抓取与正文解析；供其他插件读取网页使用。"
    capabilities = (Capability.PAGE_READ,)
    implemented = True
    is_app = False

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def _read_static(self, url: str, max_chars: int = None) -> dict:
        """静态模式：直接抓取并在本地解析 HTML。"""
        max_bytes = _cfg_int("browser_max_bytes", 2 * 1024 * 1024)
        max_chars = max_chars or _cfg_int("browser_max_chars", 20000)
        timeout = _cfg_int("browser_timeout", 20)
        user_agent = _cfg_str(
            "browser_user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
        )
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        }

        try:
            timeout_obj = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=timeout_obj, headers=headers) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    final_url = str(resp.url)
                    if resp.status >= 400:
                        return {
                            "ok": False, "url": final_url, "status": resp.status,
                            "error": f"HTTP {resp.status}",
                        }
                    raw = await resp.content.read(max_bytes + 1)
                    truncated = len(raw) > max_bytes
                    raw = raw[:max_bytes]
                    content_type = str(resp.headers.get("Content-Type") or "").lower()
                    charset = resp.charset or _sniff_charset(raw)
                    try:
                        body = raw.decode(charset, errors="replace")
                    except LookupError:
                        body = raw.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            return {"ok": False, "url": url, "error": f"请求超时（{timeout}s）"}
        except Exception as e:
            return {"ok": False, "url": url, "error": f"抓取失败：{e}"}

        if "html" not in content_type and not body.lstrip().startswith("<"):
            text = _clean_text([body], max_chars)
            return {
                "ok": True, "url": final_url, "title": "", "description": "",
                "text": text, "links": [], "truncated": truncated,
                "rendered": False, "content_type": content_type,
            }

        parser = _TextExtractor()
        try:
            parser.feed(body)
            parser.close()
        except Exception as e:
            return {"ok": False, "url": final_url, "error": f"HTML 解析失败：{e}"}

        title = re.sub(r"\s+", " ", "".join(parser.title)).strip()
        text = _clean_text(parser.parts, max_chars)
        links = []
        seen = set()
        for href in parser.links:
            absolute = urljoin(final_url, href)
            if urlparse(absolute).scheme not in ("http", "https"):
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            links.append(absolute)
            if len(links) >= 60:
                break
        return {
            "ok": True,
            "url": final_url,
            "title": title,
            "description": re.sub(r"\s+", " ", parser.description).strip(),
            "text": text,
            "links": links,
            "truncated": truncated,
            "rendered": False,
            "content_type": content_type,
        }

    async def _read_dynamic(self, url: str, max_chars: int = None) -> dict:
        """动态模式：调用可提供网页解析的 LLM/工具接口（DeepSeek Fetch 形态）。

        POST <browser_dynamic_url>
          Authorization: Bearer <browser_dynamic_api_key 或 primary_api_key>
          {"url": "..."}
        返回 {"title": "...", "content": "Markdown ..."}
        """
        endpoint = _cfg_str("browser_dynamic_url", "https://api.deepseek.com/v1/tools/fetch")
        api_key = _cfg_str("browser_dynamic_api_key", "") or _cfg_str("primary_api_key", "")
        if not api_key or api_key == "YOUR_API_KEY_HERE":
            return {"ok": False, "url": url, "error": "动态模式未配置网页解析 API Key"}
        timeout = _cfg_int("browser_dynamic_timeout", 30)
        max_chars = max_chars or _cfg_int("browser_max_chars", 20000)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            timeout_obj = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=timeout_obj) as session:
                async with session.post(endpoint, json={"url": url}, headers=headers) as resp:
                    body = await resp.text(errors="replace")
                    if resp.status != 200:
                        return {
                            "ok": False, "url": url, "status": resp.status,
                            "error": f"网页解析接口 HTTP {resp.status}: {body[:200]}",
                        }
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError as e:
                        return {"ok": False, "url": url, "error": f"网页解析返回不是 JSON：{e}"}
        except asyncio.TimeoutError:
            return {"ok": False, "url": url, "error": f"动态解析超时（{timeout}s）"}
        except Exception as e:
            return {"ok": False, "url": url, "error": f"动态解析失败：{e}"}

        if not isinstance(payload, dict):
            return {"ok": False, "url": url, "error": "网页解析返回结构异常"}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        title = str(data.get("title") or "").strip()
        content = data.get("content") or data.get("markdown") or data.get("text") or ""
        content = str(content or "").strip()
        if not content:
            return {"ok": False, "url": url, "error": "网页解析没有返回正文"}
        truncated = len(content) > max_chars
        content = _truncate(content, max_chars)
        return {
            "ok": True,
            "url": str(data.get("url") or url),
            "title": title,
            "description": str(data.get("description") or "").strip(),
            "text": content,
            "links": list(data.get("links") or [])[:60],
            "truncated": truncated,
            "rendered": True,
            "content_type": "text/markdown",
            "provider": endpoint,
        }

    async def _summarize(self, title: str, text: str) -> str:
        """用一个 LLM 把网页内容概括成可进入短期记忆的摘要。"""
        text = str(text or "").strip()
        if not text:
            return ""
        max_chars = _cfg_int("browser_summary_max_chars", 400)
        try:
            from core.llm_profile import LLMProfile
            from core.llm_interface import call_api_thinking

            profile = LLMProfile(
                plugin_id="browser",
                api_key=_cfg_str("browser_summary_api_key", "") or None,
                base_url=_cfg_str("browser_summary_base_url", "") or None,
                model=_cfg_str("browser_summary_model", "") or None,
                temperature=0.2,
                max_tokens=500,
                timeout=_cfg_int("browser_summary_timeout", 30),
            )
            system = "概括网页内容，保留关键信息，写成一段不超过200字的摘要。只输出摘要。"
            user = f"标题：{title or '（无）'}\n正文：\n{text[:12000]}"
            reply = await asyncio.to_thread(
                call_api_thinking,
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=500,
                thinking=False,
                timeout=_cfg_int("browser_summary_timeout", 30),
                profile=profile,
            )
            summary = str(reply or "").strip()
            if summary:
                return summary[:max_chars]
        except Exception as e:
            append_log(f"[浏览器] 网页概括失败，使用截断正文: {e}")

        fallback = re.sub(r"\s+", " ", text).strip()
        if title:
            fallback = f"{title}：{fallback}"
        return fallback[:max_chars]

    @staticmethod
    def _remember_summary(title: str, url: str, summary: str) -> bool:
        if not summary:
            return False
        try:
            from core.apps import APP_CONTEXT
            APP_CONTEXT.remember(f"我读了网页「{title or url}」：{summary}")
            return True
        except Exception:
            return False

    async def read_page(self, url: str, *, render: bool = False, max_chars: int = None,
                        mode: str = None, remember: bool = True) -> dict:
        """抓取/解析网页并概括，返回 {ok,summary,text,title,...}。

        mode: static（本地抓取+HTMLParser）| dynamic（DeepSeek Fetch 形态的网页解析接口）
        remember: 概括成功后写入主进程的软件上下文（短期记忆）。
        """
        url = str(url or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return {"ok": False, "url": url, "error": "只支持 http/https 网址"}

        mode = str(mode or _cfg_str("browser_mode", "static")).strip().lower()
        if mode not in ("static", "dynamic"):
            mode = "static"
        max_chars = max_chars or _cfg_int("browser_max_chars", 20000)

        if mode == "dynamic":
            result = await self._read_dynamic(url, max_chars)
            result["mode_used"] = "dynamic"
            if not result.get("ok"):
                dynamic_error = result.get("error")
                result = await self._read_static(url, max_chars)
                result["mode_used"] = "static_fallback"
                result["dynamic_error"] = dynamic_error
        else:
            result = await self._read_static(url, max_chars)
            result["mode_used"] = "static"
        result["requested_mode"] = mode
        result["render_requested"] = bool(render)

        if not result.get("ok"):
            return result

        summary = await self._summarize(result.get("title") or "", result.get("text") or "")
        result["summary"] = summary
        result["remembered"] = bool(
            remember and self._remember_summary(result.get("title") or "", result.get("url") or url, summary)
        )
        return result

    async def read_text(self, url: str, *, render: bool = False, max_chars: int = None,
                        mode: str = None, remember: bool = True) -> str:
        """便捷入口：优先返回摘要；失败时返回空字符串。"""
        result = await self.read_page(
            url, render=render, max_chars=max_chars, mode=mode, remember=remember,
        )
        if not result.get("ok"):
            return ""
        return result.get("summary") or result.get("text", "")


PLUGIN = BrowserPlugin
