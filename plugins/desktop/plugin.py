# plugins/desktop/plugin.py
# ========================================================================
# 电脑插件：把“操作本机软件”做成 NHY 的一个软件。
#
# - available_for_actions=True：不用先被 AppRouter 打开，动作层可以直接
#   用 app_do 交任务；真正的工作区/程序由任务自己启动。
# - app_status().open 始终 False：电脑不参与“当前软件/发送落点”的竞争，
#   聊天软件的通道不会被它抢走。
# - 进度通过 APP_CONTEXT.remember 回传，认知循环下一轮就能看到
#   “正在操作：第 N 步 …”。
# - 启动时把短期思考窗口注入为 9~11（默认 10），停止时还原。
# ========================================================================

from __future__ import annotations

import asyncio
import os
import re
import threading
import time

from config.api_config import config
from plugins.base import Capability, Plugin
from . import uia
from .worker_client import DesktopWorker


def _prefer_terms(text: str) -> list:
    """把任务文本拆成可用于窗口标题匹配的短词（中文 2~4 字 + 英文/数字）。"""
    text = str(text or "").lower()
    terms = set(re.findall(r"[a-z0-9][a-z0-9._-]{2,}", text))
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for size in (4, 3, 2):
            for index in range(0, len(run) - size + 1):
                term = run[index:index + size]
                if term not in {"一个", "这个", "那个", "给我", "帮我", "把链接"}:
                    terms.add(term)
    return list(terms)


def _window_score(title: str, prefer: str) -> int:
    """标题和任务越相关分数越高；空白标签页降权，但不排除任何程序。"""
    title = str(title or "").strip()
    lowered = title.lower()
    score = 0
    for term in _prefer_terms(prefer):
        if term and term in lowered:
            score += 3 if len(term) >= 3 else 1
    if "新标签页" in title or "new tab" in lowered:
        score -= 6
    return score


class DesktopPlugin(Plugin):
    plugin_id = "desktop"
    label = "电脑"
    description = "本地虚拟桌面：把本机软件放进工作区打开，用无障碍树操作；默认不截图。"
    capabilities = (Capability.COMPUTER,)
    implemented = True
    is_app = True

    def __init__(self, settings: dict = None):
        super().__init__(settings)
        self._lock = threading.RLock()
        self._running = False
        self._task = None
        self._thread = None
        self._last_progress = 0.0
        self._last_observe = 0.0
        self._observe_stop = threading.Event()
        self._observe_thread = None
        self._active_notepad = 0
        try:
            width = int(config.get("desktop_worker_width", 1280))
            height = int(config.get("desktop_worker_height", 800))
        except (TypeError, ValueError):
            width, height = 1280, 800
        self._worker = DesktopWorker(width=width, height=height)

    # ---------- 虚拟桌面进程 ----------
    @staticmethod
    def _real_mode() -> bool:
        return bool(config.get("desktop_use_real_desktop", False))

    def _ensure_worker(self) -> bool:
        if self._real_mode():
            return False
        try:
            if self._worker.running:
                return True
            self._worker.wallpaper = str(config.get("desktop_wallpaper") or "")
            return bool(self._worker.start())
        except Exception:
            return False

    def worker_state(self) -> dict:
        if not self._worker.running:
            return {}
        state = self._worker.state()
        return state if isinstance(state, dict) else {}

    def worker_launch(self, name: str) -> dict:
        if not self._worker.running:
            return {"ok": False, "error": "工作桌面没有运行"}
        result = self._worker.launch(name)
        if result.get("ok"):
            # 新窗口进来后更新桌面概览，让语言层马上“看见”。
            self._push_overview()
            hwnd = int(result.get("hwnd") or 0)
            if result.get("mirrored") and hwnd:
                # 镜像只是画面；真实窗口还在，主动读一次 UIA 树，
                # 否则语言层只知道“镜像了一个 QQ”，不知道里面有什么。
                parsed, lines = self._read_mirrored_window(name, hwnd)
                result["parsed"] = parsed
                if lines:
                    result["content_lines"] = lines[:10]
            elif result.get("floating"):
                self._observe_floating_windows(force=True)
        return result

    def _read_mirrored_window(self, name: str, hwnd: int) -> tuple:
        import re

        lines = []
        meaningful = False
        try:
            snap = uia.backend().snapshot(hwnd, max_nodes=300, depth=12)
            lines = [ln for ln in (snap.get("text") or "").splitlines() if ln.strip()]
            meaningful = self._tree_meaningful(lines)
            if not meaningful:
                # QQ/Electron 的浅树前几十行全是空 Pane，必须加深后再判断。
                snap = uia.backend().snapshot(
                    hwnd, max_nodes=1200, depth=28, chrome=True
                )
                deeper = [ln for ln in (snap.get("text") or "").splitlines() if ln.strip()]
                if self._tree_meaningful(deeper) or len(deeper) > len(lines):
                    lines = deeper
                    meaningful = self._tree_meaningful(lines)
        except Exception as exc:
            lines = [f"（读取窗口树失败：{exc}）"]

        readable = []
        for raw in lines:
            match = re.match(r'\s*\S+ ([A-Za-z]+) "([^"]*)"(.*)', raw)
            if not match:
                continue
            role, text, tail = match.groups()
            if role in ("Pane", "Document", "Group", "TitleBar", "ScrollBar", "Custom"):
                if not text.strip() and 'value="' not in tail:
                    continue
            if text.strip() or 'value="' in tail:
                readable.append(raw.strip())
        if not readable:
            readable = lines[:40]
        readable = readable[:40]

        try:
            from core.apps import APP_CONTEXT

            key = f"desktop_app_{name}"
            if meaningful:
                body = f"{name}窗口里现在能看到：\n" + "\n".join(readable)
                APP_CONTEXT.remember_state(key, body)
                return True, readable
            APP_CONTEXT.remember_state(
                key, f"{name}目前只是镜像显示；窗口树里没有可读文本。"
            )
        except Exception:
            pass
        return False, readable

    def worker_endpoint(self) -> dict:
        """给外联 dsh 的 MCP 子进程用的工作桌面地址。"""
        if not self._worker.running:
            return {}
        return {"port": int(self._worker.port), "token": self._worker.token}

    # ---------- 桌面概览（让语言层知道桌面上有什么） ----------
    def _workspace_overview(self) -> str:
        if self._real_mode():
            try:
                titles = [
                    str(item.get("title") or "").strip()
                    for item in uia.list_windows(only_titled=True)
                    if str(item.get("title") or "").strip()
                ][:10]
            except Exception:
                titles = []
            text = "我现在直接使用真实 Windows 桌面。"
            if titles:
                text += "可见窗口：" + "、".join(titles) + "。"
            return text + "我可以直接看窗口里的控件、点击、输入和按键。"
        state = self.worker_state()
        if not state:
            return ""
        apps = state.get("apps") or {}
        capability = "我可以看窗口里的文字控件，按名字点、往输入框写、按键；默认不看截图。"
        if not apps:
            names = []
            try:
                seen = set()
                for name, spec in uia.backend().apps().items():
                    path = str(spec.get("path") if isinstance(spec, dict) else spec).lower()
                    if path in seen:
                        continue
                    seen.add(path)
                    names.append(str(name))
            except Exception:
                names = []
            line = "工作桌面打开着，还没有程序。"
            if names:
                line += "可以打开：" + "、".join(names[:10]) + "。"
            return line + capability

        titles = []
        for name, hwnd in apps.items():
            title = uia.window_title(int(hwnd)) or "（无标题）"
            titles.append(f"{name}（{title}）")
        text = "工作桌面打开着，上面有：" + "、".join(titles) + "。"
        last = int(state.get("last_hwnd") or 0)
        if last:
            try:
                snap = uia.backend().snapshot(last, max_nodes=60, depth=6)
                lines = [ln for ln in (snap.get("text") or "").splitlines() if ln.strip()][:22]
                if lines:
                    text += "\n窗口里现在能看到：\n" + "\n".join(lines)
            except Exception:
                pass
        return text + "\n" + capability

    @staticmethod
    def _is_notepad_title(title: str) -> bool:
        title = str(title or "").lower()
        return "记事本" in title or "notepad" in title

    def _notepad_hwnd(self) -> int:
        state = self.worker_state()
        apps = state.get("apps") or {}
        floats = {int(h) for h in (state.get("floating") or [])}
        worker_candidates = []
        for name, hwnd in apps.items():
            label = str(name).lower()
            if "记事本" in str(name) or "notepad" in label:
                worker_candidates.append((int(hwnd), int(hwnd) in floats))
        if worker_candidates:
            worker_candidates.sort(key=lambda item: item[1], reverse=True)
            self._active_notepad = worker_candidates[0][0]
            return int(worker_candidates[0][0])
        # 1) 前台记事本优先：用户正在哪个记事本里打字，就绑哪一个。
        try:
            fg = uia.foreground_window()
            if fg and self._is_notepad_title(uia.window_title(fg)):
                self._active_notepad = int(fg)
                return int(fg)
        except Exception:
            pass
        # 2) 上一次观察到的记事本，仍存在就继续用。
        if self._active_notepad:
            try:
                if uia.window_rect(self._active_notepad) and self._is_notepad_title(
                    uia.window_title(self._active_notepad)
                ):
                    return int(self._active_notepad)
            except Exception:
                pass
        # 3) 多个记事本时，优先取内容非空的那一个，而不是枚举到的第一个空窗口。
        candidates = []
        try:
            for item in uia.list_windows(only_titled=True):
                title = str(item.get("title") or "")
                if not self._is_notepad_title(title):
                    continue
                hwnd = int(item["hwnd"])
                value = ""
                try:
                    snap = uia.backend().snapshot(hwnd, max_nodes=80, depth=10)
                    for line in (snap.get("text") or "").splitlines():
                        if "Edit" in line and 'value="' in line:
                            value = line
                            break
                except Exception:
                    pass
                candidates.append((len(value), hwnd))
        except Exception:
            candidates = []
        if candidates:
            candidates.sort(reverse=True)
            self._active_notepad = candidates[0][1]
            return int(candidates[0][1])
        # 4) 真实桌面模式下没有 worker 状态，最后再找顶层记事本。
        try:
            for item in uia.list_windows(only_titled=True):
                if self._is_notepad_title(str(item.get("title") or "")):
                    self._active_notepad = int(item["hwnd"])
                    return int(item["hwnd"])
        except Exception:
            pass
        return 0

    def deliver_text(self, text: str) -> bool:
        """没有聊天通道时，把要说的话写进虚拟桌面里的记事本。"""
        text = str(text or "").strip()
        if not text:
            return False
        hwnd = self._notepad_hwnd()
        if not hwnd:
            return False
        try:
            snap = uia.backend().snapshot(hwnd, max_nodes=120, depth=10)
            ref = ""
            for line in (snap.get("text") or "").splitlines():
                if "Edit" in line:
                    ref = line.strip().split(" ", 1)[0]
                    if "文本编辑器" in line:
                        break
            if not ref:
                return False
            result = uia.backend().append_value(ref, text)
            if not result.get("ok"):
                return False
            self._observe_floating_windows(force=True)
            try:
                from core.apps import APP_CONTEXT

                APP_CONTEXT.remember_state(
                    "desktop_notepad_reply", f"我在记事本里写下了：{text}"
                )
            except Exception:
                pass
            return True
        except Exception:
            return False

    def _push_overview(self) -> None:
        """把桌面概览写成“状态”，同一 key 覆盖，不再每轮追加。"""
        try:
            overview = self._workspace_overview()
        except Exception:
            return
        if not overview:
            return
        try:
            from core.apps import APP_CONTEXT

            APP_CONTEXT.remember_state("desktop_overview", overview)
        except Exception:
            pass

    def _readable_tree_lines(self, lines: list, limit: int = 40) -> list:
        import re

        readable = []
        for raw in lines:
            match = re.match(r'\s*\S+ ([A-Za-z]+) "([^"]*)"(.*)', raw)
            if not match:
                continue
            role, text, tail = match.groups()
            if role in ("Pane", "Document", "Group", "TitleBar", "ScrollBar", "Custom"):
                if not text.strip() and 'value="' not in tail:
                    continue
            if text.strip() or 'value="' in tail:
                readable.append(raw.strip())
        return (readable or lines)[:limit]

    def _observe_loop(self) -> None:
        while not self._observe_stop.is_set():
            if self._running:
                self._observe_floating_windows(force=True)
            self._observe_stop.wait(1.5)

    def _start_observer(self) -> None:
        if self._observe_thread and self._observe_thread.is_alive():
            return
        self._observe_stop.clear()
        self._observe_thread = threading.Thread(
            target=self._observe_loop, name="DesktopObserver", daemon=True
        )
        self._observe_thread.start()

    def _stop_observer(self) -> None:
        self._observe_stop.set()
        thread = self._observe_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._observe_thread = None

    def _observe_floating_windows(self, force: bool = False) -> None:
        """轮询浮动窗口内容，宿主刚键入的文字也能进入 APP_CONTEXT。"""
        now = time.time()
        if not force and now - self._last_observe < 1.2:
            return
        self._last_observe = now
        try:
            if self._real_mode():
                hwnd = self._notepad_hwnd()
                if not hwnd:
                    return
                self._active_notepad = hwnd
                snap = uia.backend().snapshot(hwnd, max_nodes=300, depth=12)
                lines = [ln for ln in (snap.get("text") or "").splitlines() if ln.strip()]
                if not self._tree_meaningful(lines):
                    return
                body = "记事本窗口里现在能看到：\n" + "\n".join(
                    self._readable_tree_lines(lines)
                )
                from core.apps import APP_CONTEXT

                APP_CONTEXT.remember_state("desktop_app_记事本", body)
                return
            state = self.worker_state()
            floats = [int(h) for h in (state.get("floating") or [])]
            apps = state.get("apps") or {}
            names = {int(h): str(name) for name, h in apps.items()}
            from core.apps import APP_CONTEXT

            for hwnd in floats[:4]:
                name = names.get(hwnd)
                if not name or not uia.window_title(hwnd):
                    continue
                snap = uia.backend().snapshot(hwnd, max_nodes=300, depth=12)
                lines = [ln for ln in (snap.get("text") or "").splitlines() if ln.strip()]
                if not self._tree_meaningful(lines):
                    continue
                body = f"{name}窗口里现在能看到：\n" + "\n".join(
                    self._readable_tree_lines(lines)
                )
                APP_CONTEXT.remember_state(f"desktop_app_{name}", body)
        except Exception:
            pass

    def _ensure_overview_present(self) -> None:
        """认知循环启动/清空上下文后，把桌面概览补回去。"""
        if not self._worker.running:
            return
        try:
            from core.apps import APP_CONTEXT

            if APP_CONTEXT.state_text("desktop_overview"):
                return
        except Exception:
            return
        self._push_overview()

    # ---------- 软件阅读（兼容 QQ/微信等软件插件） ----------
    def provides_reading(self) -> bool:
        return True

    def _app_window_name(self, app_id: str) -> str:
        mapping = {"qq_napcat": "QQ", "wechat": "微信"}
        if app_id in mapping:
            return mapping[app_id]
        try:
            from plugins import registry

            plugin = registry.get_plugin(app_id)
            if plugin is not None and plugin.label:
                return plugin.label
        except Exception:
            pass
        return app_id

    def _find_app_window(self, name: str, prefer: str = "") -> int:
        # 已经嵌进工作桌面的窗口不在顶层枚举里，先从 worker 状态找。
        try:
            for key, hwnd in (self.worker_state().get("apps") or {}).items():
                label = str(key).lower()
                if label == str(name).lower() or str(name).lower() in label:
                    return int(hwnd)
        except Exception:
            pass
        try:
            table = uia.backend().apps()
        except Exception:
            table = {}
        exe = ""
        for key, spec in table.items():
            if str(key).lower() == str(name).lower():
                exe = str(spec.get("path") if isinstance(spec, dict) else spec)
                break
        target = os.path.normcase(os.path.abspath(exe)) if exe else ""
        # 允许直接按 exe/lnk 路径找窗口，不再要求它出现在内置表里。
        if not target and os.path.isfile(str(name or "").strip().strip('"')):
            target = os.path.normcase(os.path.abspath(str(name).strip().strip('"')))
        windows = uia.list_windows(only_titled=True)
        candidates = []
        if target:
            for item in windows:
                image = uia.process_image(int(item["pid"]))
                if image and os.path.normcase(os.path.abspath(image)) == target:
                    candidates.append(item)
        if not candidates:
            needle = str(name).strip().lower()
            if needle:
                for item in windows:
                    if needle in str(item["title"] or "").lower():
                        candidates.append(item)
        if not candidates:
            return 0
        # 同一程序有多个窗口时，选标题最贴近当前任务的，而不是枚举到的第一个。
        # 典型场景：浏览器已经有多个标签页，第一个是空白页。
        candidates.sort(
            key=lambda item: _window_score(str(item.get("title") or ""), prefer),
            reverse=True,
        )
        return int(candidates[0]["hwnd"])

    @staticmethod
    def _tree_meaningful(lines: list) -> bool:
        """窗口树里是否有真正的控件内容（排除空 Pane/Document 这类外壳）。"""
        skip = {"Pane", "Document", "TitleBar", "ScrollBar", "Group", "MenuBar", "StatusBar", "Custom"}
        import re

        for line in lines:
            match = re.match(r'\s*\S+ ([A-Za-z]+) "([^"]*)"', line)
            if not match:
                continue
            role, text = match.groups()
            if role in skip:
                continue
            if text.strip():
                return True
            if "value=\"" in line and not line.rstrip().endswith('value=""'):
                return True
        return False

    async def _vision_describe(self, image, name: str) -> str:
        """解析失败时用视觉模型兜底读窗口。"""
        path = None
        try:
            import tempfile
            from pathlib import Path

            from core.llm_interface import describe_image_from_path

            path = Path(tempfile.gettempdir()) / f"nhy_window_{os.getpid()}.png"
            image.save(path)
            prompt = (
                f"这是{name}的窗口截图。请用中文简要描述界面上能看到的内容和文字"
                "（聊天消息、联系人/群名、按钮、输入框等），只写看到的，不要猜测。"
            )
            text = await describe_image_from_path(
                str(path), prompt=prompt, max_chars=800, max_tokens=3000, timeout=120.0
            )
            return str(text or "").strip()
        except Exception:
            return ""
        finally:
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass

    def has_app_window(self, app_id: str) -> bool:
        """平台软件的真实窗口是否已经在运行（只查，不启动，避免顶号）。"""
        try:
            return bool(self._find_app_window(self._app_window_name(app_id)))
        except Exception:
            return False

    def _read_window_tree(self, hwnd: int, depth: int, max_nodes: int, chrome: bool = False):
        """读一次窗口树，返回 (lines, meaningful, error)。"""
        try:
            snap = uia.backend().snapshot(
                hwnd, max_nodes=max_nodes, depth=depth, chrome=chrome
            )
        except Exception as exc:
            return [], False, str(exc)
        lines = [ln for ln in (snap.get("text") or "").splitlines() if ln.strip()][:80]
        return lines, self._tree_meaningful(lines), ""

    async def observe_app(self, app_id: str) -> dict:
        """软件被打开后读窗口：UIA → 截图+视觉模型兜底 → 最后才移进桌面。"""
        if not bool(config.get("desktop_observe_software", True)):
            return {}
        app_id = str(app_id or "").strip()
        if not app_id or app_id == self.plugin_id:
            return {}
        try:
            from core.apps import APP_CONTEXT
        except Exception:
            return {}
        name = self._app_window_name(app_id)
        hwnd = self._find_app_window(name)
        if not hwnd:
            # 不自动启动 QQ/微信：用另一个客户端登录会把 NapCat / 微信桥顶号。
            APP_CONTEXT.remember_state(
                f"desktop_app_{app_id}",
                f"{name}没有打开的窗口；未读消息由平台插件直接提供，我不会自动登录以免顶号。",
            )
            return {"ok": False, "detail": f"{name}没有打开的窗口（未自动登录）"}

        lines = []
        snap_error = ""
        try:
            snap = uia.backend().snapshot(hwnd, max_nodes=150, depth=10)
            lines = [ln for ln in (snap.get("text") or "").splitlines() if ln.strip()][:40]
        except Exception as exc:
            snap_error = str(exc)

        meaningful = self._tree_meaningful(lines)
        retried = False
        if not meaningful:
            # 解析失败先“加参数重试”：更深的树、更多节点、包含标题栏/滚动条。
            deeper, deeper_ok, deeper_err = self._read_window_tree(hwnd, 24, 800, True)
            retried = True
            if deeper_ok or len(deeper) > len(lines):
                lines = deeper
                meaningful = deeper_ok
            if deeper_err and not snap_error:
                snap_error = deeper_err
        mirrored = False
        embedded = False
        vision_text = ""
        if self._worker.running:
            result = self._worker.mirror(hwnd)
            mirrored = bool(result.get("ok"))

        if not meaningful:
            loop = asyncio.get_running_loop()
            image = await loop.run_in_executor(None, uia.capture_window_visible, hwnd)
            if image is not None:
                vision_text = await self._vision_describe(image, name)
            # 截图/视觉也读不出来，才强制把窗口移进虚拟桌面。
            if not vision_text and not mirrored and self._worker.running:
                embedded = self._worker.embed_window(hwnd)
                if embedded:
                    self._push_overview()

        if meaningful:
            body = f"{name}窗口里现在能看到：\n" + "\n".join(lines)
        elif vision_text:
            body = f"{name}窗口（截图识别）：\n{vision_text}"
        elif snap_error:
            body = f"{name}已经打开了，但我读不出它的窗口内容：{snap_error}"
        else:
            body = f"{name}已经打开了，窗口内容暂时读不出来。"
        if mirrored:
            body += "\n（窗口正被你使用，我在工作桌面上放了一份实时镜像）"
        elif embedded:
            body += "\n（窗口读不出来，我把它移进了工作桌面）"
        APP_CONTEXT.remember_state(f"desktop_app_{app_id}", body)
        if meaningful:
            mode = "镜像" if mirrored else ""
        elif vision_text:
            mode = "截图识别"
        elif embedded:
            mode = "移入桌面"
        else:
            mode = "无内容"
        detail = f"看了一眼{name}" + (f"（{mode}）" if mode else "")
        return {"ok": True, "detail": detail, "content_lines": lines or ([vision_text] if vision_text else [])}

    def _prune_app_states(self) -> None:
        """软件关掉后，清掉它的窗口阅读状态。"""
        try:
            from core.apps import APP_CONTEXT
            from plugins import registry

            for plugin in registry.app_plugins():
                if plugin.plugin_id == self.plugin_id:
                    continue
                try:
                    opened = bool((plugin.app_status() or {}).get("open"))
                except Exception:
                    continue
                if not opened:
                    APP_CONTEXT.remember_state(f"desktop_app_{plugin.plugin_id}", "")
        except Exception:
            pass

    # ---------- 面板动作 ----------
    def show_workspace(self) -> dict:
        if self._real_mode():
            return {"ok": False, "error": "当前是真实桌面模式，没有虚拟工作桌面"}
        if not self._ensure_worker():
            return {"ok": False, "error": "工作桌面没起来"}
        self._running = True
        self._apply_thought_window()
        if not self._worker.front():
            self._worker.show()
        self._push_overview()
        return {"ok": True, "detail": "工作桌面已显示"}

    def hide_workspace(self) -> dict:
        if self._real_mode():
            return {"ok": False, "error": "当前是真实桌面模式，没有虚拟工作桌面"}
        if not self._worker.running:
            return {"ok": False, "error": "工作桌面没有运行"}
        self._worker.hide()
        self._clear_states()
        self._remember("我把工作桌面收起来了，里面的程序还在。")
        return {"ok": True, "detail": "工作桌面已收起"}

    def reload_wallpaper(self) -> dict:
        if not self._worker.running:
            return {"ok": False, "error": "工作桌面没有运行"}
        path = str(config.get("desktop_wallpaper") or "").strip()
        self._worker.wallpaper = path
        result = self._worker.set_wallpaper(path)
        if result.get("ok"):
            return {"ok": True, "detail": "壁纸已切换"}
        return {"ok": False, "error": result.get("error") or "壁纸切换失败"}

    def self_test(self) -> dict:
        """面板自测：走一遍完整链路（工作桌面 → 操作手 → UIA）。"""
        import asyncio

        try:
            result = asyncio.run(
                self.perform_task(
                    "打开记事本，在编辑区写一行「自测成功」，然后结束，不要保存",
                    "面板自测",
                )
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": bool(result.get("ok")),
            "detail": result.get("detail") or result.get("error") or "",
        }

    # ---------- 服务 ----------
    async def start(self) -> None:
        self._running = True
        self._apply_thought_window()
        if self._real_mode():
            self._push_overview()
            self._start_observer()
            self._remember("我现在直接使用真实 Windows 桌面，不再启动虚拟工作桌面。")
            return
        if self._ensure_worker():
            self._worker.show()
            self._push_overview()
            self._start_observer()
        else:
            self._remember("工作桌面没起来，我先直接操作本机窗口。")

    async def stop(self) -> None:
        self._running = False
        self._stop_observer()
        self._restore_thought_window()
        self._clear_states()
        with self._lock:
            self._task = None
        try:
            self._worker.stop()
        except Exception:
            pass

    def is_running(self) -> bool:
        return bool(self._running)

    def apply_settings(self) -> None:
        """面板保存后热应用（目前只影响思考窗口）。"""
        if self._running:
            self._apply_thought_window()

    def conflict_note(self, others: list) -> str:
        """电脑插件与平台插件是两种接入方式：只提示，不阻止同时安装。"""
        labels = [
            str(getattr(item, "label", "") or getattr(item, "plugin_id", ""))
            for item in (others or [])
            if getattr(item, "plugin_id", "") != self.plugin_id
        ]
        if not labels:
            return ""
        return (
            "电脑会直接操作本机窗口；和 "
            + "、".join(labels)
            + " 同时运行可能重复处理同一个软件，也可能互相抢前台。"
            + "两个可以同时装，建议只保留一种接入方式。"
        )

    def available_for_actions(self) -> bool:
        return True

    # ---------- 软件状态 ----------
    def app_status(self) -> dict:
        with self._lock:
            task = dict(self._task) if self._task else {}
        state = self.worker_state()
        self._prune_app_states()
        self._ensure_overview_present()
        self._observe_floating_windows()
        return {
            "id": self.plugin_id,
            "label": self.label,
            "open": False,
            "unread": 0,
            "current_conversation": "",
            "conversations": [],
            "busy": bool(task.get("status") == "running"),
            "task": str(task.get("text") or ""),
            "step": int(task.get("step") or 0),
            "workspace": {
                "running": bool(self._worker.running),
                "visible": bool(state.get("visible")),
                "windows": len(state.get("apps") or {}),
                "last_hwnd": int(state.get("last_hwnd") or 0),
                "real": bool(self._real_mode()),
            },
        }

    async def open_app(self) -> dict:
        if self._real_mode():
            self._push_overview()
            return {"ok": True, "detail": "我直接使用真实桌面"}
        if not self._ensure_worker():
            self._remember("工作桌面没起来，我先直接操作本机窗口。")
            return {"ok": False, "detail": "工作桌面没起来"}
        # 认知循环打开电脑时只显示，不抢前台；只有用户在面板点“显示工作桌面”才前置。
        self._worker.show()
        self._push_overview()
        return {"ok": True, "detail": "我坐到了电脑前"}

    async def close_app(self) -> dict:
        if self._real_mode():
            self._clear_states()
            self._remember("我停止使用真实桌面了。")
            return {"ok": True, "detail": "我停止使用真实桌面了"}
        if self._worker.running:
            self._worker.hide()
        self._clear_states()
        self._remember("我把工作桌面收起来了，里面的程序还在。")
        return {"ok": True, "detail": "我把工作桌面收起来了"}

    # ---------- 任务 ----------
    async def perform_task(self, task: str, context: str = "") -> dict:
        task = str(task or "").strip()
        if not task:
            return {"ok": False, "detail": "没有要交办的事"}
        with self._lock:
            if self._task and self._task.get("status") == "running":
                return {"ok": False, "detail": f"电脑还在忙：{self._task.get('text')}"}
            self._task = {
                "text": task,
                "status": "running",
                "step": 0,
                "started": time.time(),
            }
        # 新任务开始：清掉上一轮电脑任务的“我坐到电脑前/结束了”记录，
        # 避免开着 QQ 音乐还在上下文里看到上次网易云的任务摘要。
        try:
            from core.apps import APP_CONTEXT

            APP_CONTEXT.forget_prefix((
                "[现在] 我坐到电脑前",
                "[现在] 电脑那边结束了",
                "[现在] 电脑还在忙",
            ))
            APP_CONTEXT.remember_state("desktop_progress", "")
        except Exception:
            pass
        self._running = True
        self._apply_thought_window()
        if self._real_mode():
            self._start_observer()
        else:
            self._ensure_worker()
            if self._worker.running:
                self._worker.show()
                self._start_observer()
        thread = threading.Thread(
            target=self._run_task, args=(task, context), name="DesktopTask", daemon=True
        )
        self._thread = thread
        thread.start()
        return {
            "ok": True,
            "detail": f"我坐到电脑前，准备{task}",
            "content_lines": [],
        }

    def _run_task(self, task: str, context: str) -> None:
        # 当前版本只走 NHY 自己的操作手（action_llm + UIA），不经过 dsh。
        # 外联 dsh 的适配代码保留在 acp_client.py / dsh_bridge.py，
        # 但运行路径不接线，等以后明确要做时再挂。
        from .desktop_operator import run_task

        try:
            result = run_task(self, task, context)
        except Exception as exc:
            import traceback

            from utils.monitor import append_log

            append_log(f"[电脑] 任务异常 {traceback.format_exc()}")
            result = {"ok": False, "summary": f"电脑这边出错了：{exc}", "lines": []}
        with self._lock:
            if self._task is not None:
                self._task.update(
                    status="done" if result.get("ok") else "failed",
                    summary=str(result.get("summary") or ""),
                )
        # 任务结束后清掉“进行中”状态，只留一条最终摘要。
        try:
            from core.apps import APP_CONTEXT

            APP_CONTEXT.remember_state("desktop_progress", "")
        except Exception:
            pass
        self._remember(f"电脑那边结束了：{result.get('summary') or '没有留下说明'}")
        # 任务结束后桌面状态可能变了，刷新概览（覆盖同一个 key）。
        self._push_overview()

    def on_progress(self, text: str) -> None:
        """操作手每步回调；作为“状态”写入，同 key 覆盖。"""
        text = str(text or "").strip()
        if not text:
            return
        now = time.time()
        with self._lock:
            if now - self._last_progress < 3.0:
                return
            self._last_progress = now
            if self._task is not None:
                self._task["step"] = int(self._task.get("step") or 0) + 1
        try:
            from core.apps import APP_CONTEXT

            APP_CONTEXT.remember_state("desktop_progress", text)
        except Exception:
            pass

    @staticmethod
    def _clear_states() -> None:
        try:
            from core.apps import APP_CONTEXT

            APP_CONTEXT.remember_state("desktop_overview", "")
            APP_CONTEXT.remember_state("desktop_progress", "")
        except Exception:
            pass

    @staticmethod
    def _remember(text: str) -> None:
        try:
            from core.apps import APP_CONTEXT

            APP_CONTEXT.remember(text)
        except Exception:
            pass

    # ---------- 思考窗口接口 ----------
    @staticmethod
    def _thought_window() -> int:
        try:
            value = int(config.get("desktop_thought_window", 10))
        except (TypeError, ValueError):
            value = 10
        return max(9, min(11, value))

    def _apply_thought_window(self) -> None:
        try:
            from core.short_term import SHORT_TERM, set_limit_provider

            set_limit_provider(self._thought_window)
            SHORT_TERM.reset_limit()
        except Exception:
            pass

    def _restore_thought_window(self) -> None:
        try:
            from core.short_term import SHORT_TERM, set_limit_provider

            set_limit_provider(None)
            SHORT_TERM.reset_limit()
        except Exception:
            pass

    def status(self) -> dict:
        data = super().status()
        data["running"] = bool(self._running)
        return data


PLUGIN = DesktopPlugin
