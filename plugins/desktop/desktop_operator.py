# plugins/desktop/operator.py
# ========================================================================
# 电脑操作手：一步一个 JSON 动作，先看树、再按引用动手。
#
# 和核心动作层的区别：
#   - 核心动作层决定“要不要让电脑做事”（app_do）；
#   - 这里只负责把一件事拆成若干步并在电脑上执行，不决定说话。
#
# 感知永远是 UIA 文本树；截图只在动作是 screenshot 时发生（默认提示词不鼓励）。
# ========================================================================

from __future__ import annotations

import html
import json
import os
import re
import time
import traceback
from typing import Optional

from config.api_config import config
from core.llm_interface import call_api_thinking
from utils.monitor import append_log
from . import input as desktop_input
from . import uia

_JSON_RE = re.compile(r"\{[\s\S]*\}")

_SYSTEM = """你是辉夜在电脑上的操作手，只负责动手，不负责说话。每次只输出一个 JSON 动作，不要解释。

可用动作：
{"action":"launch","name":"程序名、exe 路径或快捷方式路径"}
{"action":"snapshot"}
{"action":"click","ref":"w...e...g..."}
{"action":"set_value","ref":"...","text":"要写入的完整文本"}
{"action":"type_text","text":"要输入的文字"}
{"action":"key","keys":"ctrl+a"}
{"action":"copy","ref":"..."}
{"action":"copy_url"}
{"action":"paste","ref":"..."}
{"action":"clipboard_get"}
{"action":"clipboard_set","text":"要放进剪贴板的文字"}
{"action":"focus","ref":"..."}
{"action":"scroll","direction":"down","pages":1,"ref":"..."}
{"action":"click_at","x":640,"y":360,"button":"left","clicks":1}
{"action":"type_at","x":640,"y":360,"text":"要键入的内容"}
{"action":"drag","from":[500,400],"to":[800,400]}
{"action":"scroll_at","x":640,"y":400,"direction":"down","pages":1}
{"action":"move","x":640,"y":400}
{"action":"open_url","url":"https://example.com/"}
{"action":"close_window"}
{"action":"close_app","name":"浏览器名（可选）"}
{"action":"links","limit":20}
{"action":"url"}
{"action":"done","summary":"做完了什么"}
{"action":"fail","summary":"为什么做不到"}

规则：
1. 只输出 JSON；禁止输出解释、对话、自然语言段落——你不负责组织语言。
2. 只能使用【界面】里出现过的引用；引用失效就先用 snapshot 重新拿。
3. 能用 set_value / click(ref) 就不要用坐标；坐标只在语义动作失败时使用。
4. 需要坐标时，只能用【界面】里 @l,t,r,b 给出的屏幕坐标或任务里明确给的坐标，禁止猜；点击一般取矩形内靠近中心的位置。
5. 一次只走一步，动作后我会把界面变化给你。
6. 任务完成用 done，确认做不到用 fail；summary 只写事实，不写对话。
7. 如果上一步已经成功点击/聚焦同一处而界面没有变化，不要重复同一个动作，继续下一步（例如键入文字）。
8. 看到账号、登录、确认、发送等明确按钮/列表项时，直接 click；不要用连续 snapshot(ref) 一层层钻树。
9. 同一窗口最多连续观察 2 次；第 3 次必须执行点击/输入/按键，或者 done/fail。
10. 如果界面只有空 Pane/Document，不要自己找深层引用；系统会深取一次树，等下一轮结果。
11. 同一程序在一个任务里最多 launch 一次；已经打开就直接操作现有窗口，不要为了“刷新”反复启动。
12. 浏览器页面内容在深树里；如果【界面】只有地址栏和工具栏，先执行 {"action":"links"}，不要用坐标猜页面内容。
13. 任务是找视频、网页或链接时，【页面链接】/links 返回的“标题 + URL”就是页面上的结果；能直接回答就直接 done，summary 写“标题 + URL”，不要为了确认反复滚动或点开。
14. 只有需要进入某个结果页时才 click 对应的 Hyperlink；不要重复 launch 浏览器，也不要把空白标签页当成任务页面。
15. 任务点名网站（例如 B站/哔哩哔哩）时，用 open_url 直接打开该站点的搜索页（B站：https://search.bilibili.com/all?keyword=关键词），不要依赖浏览器默认搜索引擎。
16. 页面正在跳转时 UIA 可能短暂报错；系统会自动重试，不要因为一次读取失败就放弃。
17. 任何窗口和程序都可以操作，包括 ZCode、辉夜网页控制面板、系统设置；不要因为名字特殊就跳过。只有确实没有权限或控件不可用时才 fail。
18. “控制面板”默认指辉夜自己的网页控制面板（窗口标题含 Nascence/辉夜，地址见任务里的 panel_url），不是 Windows 的 control.exe；需要打开时用 open_url，不要 launch control。
19. 复制地址栏/输入框内容用 copy(ref)，它会回读剪贴板并返回真实文本；只按 ctrl+c 不算复制成功。
20. 粘贴用 paste(ref)，会返回剪贴板里实际粘贴的文本；任务提到“剪贴板里的链接”时先用 clipboard_get 确认内容，不要凭空说“已经复制好了”。
21. 复制当前网页地址栏链接用 copy_url；copy 只能用于 Edit 输入框/地址栏，不要用 copy 复制页面文字，否则会把剪贴板覆盖成非链接内容。
22. 关闭当前窗口用 close_window；关闭整个浏览器/程序用 close_app。不要用 win+d、win+e 或点击聊天项来代替关闭窗口。
23. 关闭时出现标题含“确认关闭/确认”的弹窗，不要忽略；先看弹窗内容，任务确实要关就点“确定”，只是试探/不能关就点“取消”。选择要由当前任务决定。
"""


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _compact_tree(text: str) -> str:
    keep = []
    for line in (text or "").splitlines():
        if "[窗口 " in line:
            keep.append(line)
            continue
        if re.search(r'\b(Pane|Document|Group)\s+""', line) and "actions=" not in line and "value=" not in line:
            continue
        keep.append(line)
    return "\n".join(keep)


def _compact_browser_web(text: str) -> str:
    """浏览器树去掉工具栏/书签栏，只保留地址栏和网页 Document 之后的内容。

    Chromium 的网页内容前面有大量浏览器自身控件（书签、扩展、菜单），
    很容易把 9000 字预算吃光，导致“总览/插件/关闭面板”这些网页按钮
    根本没进入模型看到的树。
    """
    lines = (text or "").splitlines()
    if not lines:
        return ""
    doc_index = None
    for index, line in enumerate(lines):
        if not re.search(r"\bDocument\s+\"", line):
            continue
        if ('value="http://' in line or 'value="https://' in line
                or 'value="app://' in line):
            doc_index = index
            break
    if doc_index is None:
        return text
    header = [lines[0]] if lines[0].startswith("[窗口 ") else []
    address = [
        line for line in lines[:doc_index]
        if 'Edit "地址和搜索栏"' in line or 'Edit "Address' in line
    ]
    return "\n".join(header + address + lines[doc_index:])


def _has_interactive_nodes(text: str) -> bool:
    return bool(re.search(
        r'\b(Button|Edit|ListItem|MenuItem|TabItem|CheckBox|RadioButton|ComboBox)\s+"',
        text or "",
    ) or "actions=invoke" in (text or "") or "actions=select" in (text or ""))


_LINK_RE = re.compile(
    r'(\S+)\s+Hyperlink\s+"((?:[^"\\]|\\.)*)"\s+value="(https?://[^"]+)"'
)
_NAV_LINK_NAMES = {
    "首页", "番剧", "直播", "游戏中心", "会员购", "漫画", "赛事", "下载客户端",
    "投稿", "消息", "动态", "收藏", "历史", "创作中心", "专栏", "综合", "视频",
    "影视", "用户", "更多", "登录", "注册", "客户端下载", "首页首页",
}


def _extract_links(text: str, limit: int = 30) -> list:
    """从 UIA 树里取 Hyperlink 的标题、URL 和引用。"""
    by_url = {}
    order = []
    for line in str(text or "").splitlines():
        match = _LINK_RE.search(line)
        if not match:
            continue
        ref, name, url = match.group(1), match.group(2).strip(), match.group(3).strip()
        name = name.replace('\\"', '"').strip()
        if not name or name in _NAV_LINK_NAMES or len(name) < 2:
            continue
        if url.lower().startswith("javascript:"):
            continue
        if url not in by_url:
            by_url[url] = {"ref": ref, "title": name, "url": url}
            order.append(url)
        elif len(name) < len(by_url[url]["title"]):
            # 同一个结果通常有两个链接：一个带播放量/时长，一个干净标题。
            # 保留更干净、更短的那个标题。
            by_url[url]["ref"] = ref
            by_url[url]["title"] = name
    result = [by_url[url] for url in order]
    # 视频结果优先，广告/用户主页/导航排在后面。
    result.sort(key=lambda item: 0 if "/video/" in item["url"] else 1)
    return result[: max(1, int(limit))]


def _link_summary(text: str, limit: int = 10) -> str:
    links = _extract_links(text, limit=limit)
    if not links:
        return ""
    lines = ["【页面链接】（标题 + URL，可直接用于回答）"]
    for item in links:
        title = item["title"][:90]
        lines.append(f"- {title}  {item['url']}")
    return "\n".join(lines)


_CONTROL_RE = re.compile(
    r'(\S+)\s+(Button|Edit|ListItem|TabItem|CheckBox|RadioButton|ComboBox|MenuItem)\s+"([^"]*)"'
)
_CONTROL_SKIP = {
    "关闭", "关闭此视图", "新标签页", "标签页搜索", "打开新的标签页",
    "收起标签页", "标签页分组", "最小化", "最大化", "恢复",
}


def _control_summary(text: str, limit: int = 24) -> str:
    """把网页里的按钮/输入框/列表项单独列出来。

    视频页/控制面板的内容很长，播放按钮、配置按钮经常在 9000 字截断之外；
    单独列引用，模型就不需要靠猜坐标。
    """
    seen = set()
    items = []
    for line in str(text or "").splitlines():
        match = _CONTROL_RE.search(line)
        if not match:
            continue
        ref, role, name = match.group(1), match.group(2), match.group(3).strip()
        if not name or name in _CONTROL_SKIP:
            continue
        key = (role, name)
        if key in seen:
            continue
        seen.add(key)
        items.append((role, name, ref))
    if not items:
        return ""
    lines = ["【页面控件】（可 click 的引用）"]
    for role, name, ref in items[: max(1, int(limit))]:
        lines.append(f"- {role} \"{name[:70]}\" → {ref}")
    return "\n".join(lines)


def _has_page_content(text: str) -> bool:
    """浏览器树里是否已经露出网页内容（不仅仅是地址栏和工具栏）。"""
    if "Hyperlink" in (text or "") and 'value="http' in (text or ""):
        return True
    match = re.search(r'\bDocument\s+"([^"]*)"', text or "")
    return bool(match and match.group(1).strip())


def _snapshot(backend, window, **kwargs) -> dict:
    """页面跳转时 Chromium 的可访问性树会短暂不可用，重试几次再放弃。"""
    last = None
    for attempt in range(3):
        try:
            return backend.snapshot(window=window, **kwargs)
        except Exception as exc:
            last = exc
            time.sleep(0.6 * (attempt + 1))
    raise last if last else RuntimeError("读取界面失败")


def _is_browser_window(window: int) -> bool:
    if not window:
        return False
    try:
        image = uia.process_image(uia.window_pid(window))
        return os.path.basename(str(image or "")).lower() in {
            "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
        }
    except Exception:
        return False


def _launch_browser_url(backend, url: str, prefer: str = "") -> dict:
    """用一个独立的新窗口打开网址，避免把控制面板所在的标签页顶掉。"""
    name = "Cent Browser"
    table = {}
    try:
        table = backend.apps()
    except Exception:
        pass
    exe = ""
    for key, spec in table.items():
        if str(key).lower() == name.lower():
            exe = str(spec.get("path") if isinstance(spec, dict) else spec)
            break
    before = {int(item["hwnd"]) for item in uia.list_windows(only_titled=True)}
    try:
        result = backend.launch_app(name, args=["--new-window", url])
    except Exception as exc:
        return {"ok": False, "error": f"启动浏览器失败：{exc}"}
    if not result.get("ok"):
        return result
    hwnd = _find_new_window(
        int(result.get("pid") or 0), before, prefer=prefer, exe=exe
    )
    time.sleep(0.8)
    if not hwnd:
        return {"ok": False, "error": "浏览器窗口没有出现"}
    return {"ok": True, "hwnd": hwnd, "via": "open_url_launch", "url": url}


def _parse(raw: str) -> dict:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.removeprefix("```").removeprefix("json").removesuffix("```").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = _JSON_RE.search(text)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    # 模型常在 summary 里直接写英文双引号（如视频标题），JSON 因此失效；
    # done/fail 是终止动作，不能因为一个引号就继续空转到 12 步。
    fallback = re.search(r'"action"\s*:\s*"(done|fail)"', text)
    if fallback:
        summary = ""
        summary_match = re.search(r'"summary"\s*:\s*"(.*)', text, re.S)
        if summary_match:
            summary = summary_match.group(1).strip()
            if summary.endswith("}"):
                summary = summary[:-1].rstrip()
            if summary.endswith('"'):
                summary = summary[:-1].rstrip()
        return {"action": fallback.group(1), "summary": summary}
    return {}


def _describe(action: dict) -> str:
    kind = str(action.get("action") or "")
    if kind == "launch":
        return f"启动 {action.get('name') or '程序'}"
    if kind == "click":
        return f"点击 {action.get('ref')}"
    if kind == "set_value":
        text = str(action.get("text") or "")
        return f"写入 {text[:20]}"
    if kind == "type_text":
        return f"输入 {str(action.get('text') or '')[:20]}"
    if kind == "key":
        return f"按键 {action.get('keys')}"
    if kind == "copy":
        return f"复制 {action.get('ref')}"
    if kind == "copy_url":
        return "复制当前网址"
    if kind == "paste":
        return f"粘贴到 {action.get('ref')}"
    if kind == "clipboard_get":
        return "读取剪贴板"
    if kind == "clipboard_set":
        return f"写入剪贴板 {str(action.get('text') or '')[:16]}"
    if kind == "focus":
        return f"聚焦 {action.get('ref')}"
    if kind == "scroll":
        return f"滚动 {action.get('direction') or 'down'}"
    if kind == "click_at":
        return f"点击坐标 ({action.get('x')},{action.get('y')})"
    if kind == "type_at":
        return f"在 ({action.get('x')},{action.get('y')}) 键入 {str(action.get('text') or '')[:16]}"
    if kind == "drag":
        return f"拖拽 {action.get('from')} → {action.get('to')}"
    if kind == "scroll_at":
        return f"在 ({action.get('x')},{action.get('y')}) 滚动 {action.get('direction') or 'down'}"
    if kind == "move":
        return f"移动鼠标 ({action.get('x')},{action.get('y')})"
    if kind == "links":
        return "读取页面链接"
    if kind == "url":
        return "读取地址栏"
    if kind == "open_url":
        return f"打开网址 {str(action.get('url') or '')[:60]}"
    if kind == "close_window":
        return "关闭当前窗口"
    if kind == "close_app":
        return f"关闭程序 {action.get('name') or ''}".strip()
    if kind == "snapshot":
        return "重新看界面"
    return kind or "未命名动作"


def _keys_allowed() -> bool:
    return bool(config.get("desktop_allow_synthetic_keys", True))


def _coordinate_allowed(plugin, window: int, x: int, y: int):
    """坐标输入开关 + 矩形围栏。"""
    if not bool(config.get("desktop_allow_coordinate_input", True)):
        return False, "坐标输入已在面板关闭"
    if not bool(config.get("desktop_coordinate_fence", True)):
        return True, ""
    rects = []
    try:
        if window:
            rects.append(uia.window_rect(window))
        host = int((plugin.worker_state() or {}).get("hwnd") or 0)
        if host:
            rects.append(uia.window_rect(host))
    except Exception:
        pass
    rects = [rect for rect in rects if rect and rect[2] > rect[0] and rect[3] > rect[1]]
    if not rects:
        return False, "没有可用的围栏目标窗口，坐标输入被拒绝"
    for left, top, right, bottom in rects:
        if left <= x <= right and top <= y <= bottom:
            return True, ""
    hint = "；".join(f"可用区域 {l},{t},{r},{b}" for l, t, r, b in rects)
    return False, f"坐标不在目标窗口/工作桌面内（{hint}）"


def _coord(action: dict, key: str) -> tuple:
    value = action.get(key)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return int(value[0]), int(value[1])
    return int(action.get("x") or 0), int(action.get("y") or 0)


def _existing_app_window(plugin, name: str, prefer: str = "") -> int:
    """已打开的程序窗口；不再排除控制面板等任何窗口。"""
    try:
        return int(plugin._find_app_window(name, prefer) or 0)
    except Exception:
        return 0


def _find_new_window(pid: int, before=None, prefer: str = "", timeout: float = 6.0,
                     exe: str = "") -> int:
    """找到启动后新出现的窗口。

    浏览器已经在运行时，启动的进程会退出、新窗口属于旧进程；所以不能只按
    启动进程的 pid 找，还要允许同 exe 的进程里“启动前不存在”的新窗口。
    """
    before = set(before or ())
    target_exe = os.path.normcase(os.path.abspath(exe)) if exe else ""
    deadline = time.time() + timeout
    best_hwnd = 0
    best_score = -10 ** 9
    while time.time() < deadline:
        for item in uia.list_windows(only_titled=True):
            hwnd = int(item["hwnd"])
            if hwnd in before:
                continue
            pid_match = bool(pid and item["pid"] == pid)
            exe_match = False
            if target_exe:
                image = uia.process_image(int(item["pid"]))
                exe_match = bool(
                    image and os.path.normcase(os.path.abspath(image)) == target_exe
                )
            if pid and not pid_match and not exe_match:
                continue
            title = str(item.get("title") or "")
            score = 0
            lowered = title.lower()
            for term in re.findall(r"[a-z0-9][a-z0-9._-]{2,}", prefer.lower()):
                if term in lowered:
                    score += 2
            for run in re.findall(r"[\u4e00-\u9fff]{2,}", prefer):
                for index in range(len(run) - 1):
                    if run[index:index + 2] in title:
                        score += 1
            if "新标签页" in title or "new tab" in lowered:
                score -= 5
            if score > best_score:
                best_score = score
                best_hwnd = hwnd
        if best_hwnd:
            return best_hwnd
        time.sleep(0.2)
    return best_hwnd


def _execute(plugin, backend: uia.UiaBackend, action: dict, window: int = 0,
             prefer: str = "") -> dict:
    kind = str(action.get("action") or "").strip().lower()
    if kind == "launch":
        name = str(action.get("name") or "")
        worker = plugin.worker_state()
        if worker:
            # 工作桌面在跑：由它启动并归位，返回窗口句柄作为后续目标。
            result = plugin.worker_launch(name)
            hwnd = int(result.get("hwnd") or 0)
        else:
            before = {int(item["hwnd"]) for item in uia.list_windows(only_titled=True)}
            exe = ""
            try:
                for key, spec in backend.apps().items():
                    if str(key).lower() == name.lower():
                        exe = str(spec.get("path") if isinstance(spec, dict) else spec)
                        break
            except Exception:
                exe = ""
            if not exe and os.path.isfile(name.strip().strip('"')):
                exe = os.path.abspath(name.strip().strip('"'))
            result = backend.launch_app(name)
            hwnd = _find_new_window(
                int(result.get("pid") or 0), before, prefer=prefer, exe=exe
            )
            time.sleep(0.8)
            if not hwnd:
                # 浏览器已经在运行时会复用旧进程，启动的进程没有自己的窗口；
                # 这时选标题最贴近任务的那个已有窗口，而不是枚举到的第一个。
                hwnd = _existing_app_window(plugin, name, prefer)
        if not result.get("ok"):
            return result
        return {**result, "hwnd": hwnd}
    if kind in ("close_window", "close_app"):
        if not window:
            return {"ok": False, "error": "没有目标窗口，先 launch 或 snapshot"}

        def find_prompt_dialog(pid: int):
            """找到“确认关闭”这类模态弹窗，交给模型看，不替它点。"""
            for item in uia.list_windows(only_titled=True):
                if pid and int(item["pid"]) != int(pid):
                    continue
                title = str(item.get("title") or "")
                if ("确认关闭" in title or "confirm" in title.lower()
                        or "确认" in title or "warning" in title.lower()):
                    return item
            return None

        def close_one(hwnd: int):
            """尝试关闭一个窗口；若弹出确认框，返回弹窗而不是替模型点确定。"""
            pid = 0
            try:
                pid = uia.window_pid(hwnd)
            except Exception:
                pass
            prompt = find_prompt_dialog(pid) if pid else None
            if prompt:
                return False, prompt
            try:
                backend.close_window(hwnd)
            except Exception:
                pass
            time.sleep(0.5)
            prompt = find_prompt_dialog(pid) if pid else None
            if prompt:
                return False, prompt
            if not uia.window_exists(hwnd):
                return True, None
            try:
                desktop_input.focus_window(hwnd)
                desktop_input.press_key("alt+f4")
            except Exception:
                pass
            time.sleep(0.6)
            prompt = find_prompt_dialog(pid) if pid else None
            if prompt:
                return False, prompt
            return not uia.window_exists(hwnd), None

        if kind == "close_window":
            hwnd = int(window)
            title = uia.window_title(hwnd)
            closed, prompt = close_one(hwnd)
            if prompt:
                return {
                    "ok": False,
                    "pending_dialog": True,
                    "hwnd": int(prompt["hwnd"]),
                    "dialog_title": str(prompt.get("title") or ""),
                    "error": "出现确认弹窗，已经切到弹窗；请先看弹窗内容，再决定点击“确定”还是“取消”。",
                }
            return {
                "ok": closed,
                "via": "close_window",
                "hwnd": hwnd,
                "title": title,
                "error": "" if closed else "窗口还在（可能有保存/确认提示）",
            }

        name = str(action.get("name") or "").strip()
        image = ""
        try:
            image = uia.process_image(uia.window_pid(int(window)))
        except Exception:
            pass
        targets = []
        for item in uia.list_windows(only_titled=True):
            hwnd = int(item["hwnd"])
            title = str(item.get("title") or "")
            if ("确认关闭" in title or "confirm" in title.lower()
                    or "确认" in title):
                continue
            same_title = bool(name and name.lower() in title.lower())
            same_exe = bool(image and uia.process_image(int(item["pid"])) == image)
            if same_title or same_exe:
                targets.append(hwnd)
        if not targets:
            targets = [int(window)]
        closed = []
        pending = None
        for hwnd in targets:
            ok, prompt = close_one(hwnd)
            if prompt:
                pending = prompt
                break
            if ok:
                closed.append(hwnd)
        if pending:
            return {
                "ok": False,
                "pending_dialog": True,
                "hwnd": int(pending["hwnd"]),
                "dialog_title": str(pending.get("title") or ""),
                "closed": len(closed),
                "error": "出现确认弹窗，已经切到弹窗；请先看弹窗内容，再决定点击“确定”还是“取消”。",
            }
        remaining = [hwnd for hwnd in targets if uia.window_exists(hwnd)]
        return {
            "ok": not remaining,
            "via": "close_app",
            "name": name,
            "closed": len(closed),
            "remaining": remaining,
            "error": "" if not remaining else f"还有 {len(remaining)} 个窗口没关掉",
        }
    if kind == "snapshot":
        return {"ok": True, "via": "snapshot"}
    if kind == "links":
        if not window:
            return {"ok": False, "error": "没有目标窗口，先 launch 或 snapshot"}
        snap = _snapshot(
            backend, window, depth=24, max_nodes=2500, chrome=True
        )
        links = _extract_links(
            snap.get("text") or "", limit=int(action.get("limit") or 20)
        )
        return {
            "ok": bool(links),
            "via": "links",
            "count": len(links),
            "links": links,
            "error": "" if links else "当前界面里没有可读的网页链接",
        }
    if kind == "url":
        if not window:
            return {"ok": False, "error": "没有目标窗口，先 launch 或 snapshot"}
        snap = _snapshot(
            backend, window, depth=24, max_nodes=1200, chrome=True
        )
        match = re.search(
            r'Edit "[^"]*(?:地址|Address|search)[^"]*"\s+value="([^"]*)"',
            snap.get("text") or "",
            re.IGNORECASE,
        )
        url = match.group(1) if match else ""
        return {
            "ok": bool(url),
            "via": "url",
            "url": url,
            "error": "" if url else "没有读到地址栏",
        }
    if kind == "open_url":
        url = str(action.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "没有给网址"}
        title = uia.window_title(window) if window else ""
        if (not window or not _is_browser_window(window)
                or "控制面板" in title):
            # 没有浏览器窗口，或当前窗口是控制面板：新开一个独立窗口，
            # 不要覆盖用户正在看的控制面板页面。
            return _launch_browser_url(backend, url, prefer)
        try:
            snap = _snapshot(backend, window, depth=24, max_nodes=1200, chrome=True)
            text = snap.get("text") or ""
            match = re.search(
                r'(\S+)\s+Edit "[^"]*(?:地址|Address|search)[^"]*"',
                text,
                re.IGNORECASE,
            )
            desktop_input.focus_window(window)
            if match:
                backend.set_value(match.group(1), url)
            else:
                desktop_input.press_key("ctrl+l")
                desktop_input.type_text(url)
            time.sleep(0.2)
            desktop_input.press_key("enter")
            time.sleep(0.6)
            return {"ok": True, "via": "open_url", "url": url, "hwnd": window}
        except Exception as exc:
            return {"ok": False, "error": f"打开网址失败：{exc}"}
    if kind == "click":
        ref = str(action.get("ref") or "")
        info = backend.element_info(ref)
        role = str(info.get("role") or "")
        # 聊天消息、群名片这类 Group/Text 经常没有可用的 Invoke；
        # Legacy default 有时返回成功但实际没打开。对它们直接用坐标点。
        if role in ("Group", "Text", "Image", "Pane"):
            rect = backend.element_rect(ref)
            if rect.get("ok"):
                allowed, why = _coordinate_allowed(plugin, window, rect["x"], rect["y"])
                if allowed:
                    if window:
                        desktop_input.focus_window(window)
                    fallback = desktop_input.click(rect["x"], rect["y"])
                    return {
                        "ok": bool(fallback.get("ok")),
                        "ref": ref,
                        "role": role,
                        "via": "click_at",
                        "fallback": fallback,
                        "error": "" if fallback.get("ok") else (
                            fallback.get("error") or "坐标点击失败"
                        ),
                    }
        result = backend.click(ref)
        if result.get("ok"):
            return result
        rect = backend.element_rect(ref)
        if not rect.get("ok"):
            return result
        allowed, why = _coordinate_allowed(plugin, window, rect["x"], rect["y"])
        if not allowed:
            return {**result, "fallback": "blocked", "detail": why}
        if window:
            desktop_input.focus_window(window)
        fallback = desktop_input.click(rect["x"], rect["y"])
        return {
            **result,
            "ok": bool(fallback.get("ok")),
            "via": "click_at",
            "fallback": fallback,
            "error": "" if fallback.get("ok") else (
                fallback.get("error") or result.get("error") or "坐标点击失败"
            ),
        }
    if kind == "set_value":
        return backend.set_value(str(action.get("ref") or ""), str(action.get("text") or ""))
    if kind == "type_text":
        if not _keys_allowed():
            return {"ok": False, "error": "合成键入已在面板关闭，可改用 set_value"}
        ref = str(action.get("ref") or "")
        if not ref:
            if not window:
                return {"ok": False, "error": "没有目标窗口，先 launch 或 snapshot"}
            desktop_input.focus_window(window)
        return backend.type_text(str(action.get("text") or ""), ref)
    if kind == "key":
        if not _keys_allowed():
            return {"ok": False, "error": "合成按键已在面板关闭"}
        if not window:
            return {"ok": False, "error": "没有目标窗口，先 launch 或 snapshot"}
        desktop_input.focus_window(window)
        return desktop_input.press_key(str(action.get("keys") or ""))
    if kind == "clipboard_get":
        return backend.clipboard_get()
    if kind == "clipboard_set":
        return backend.clipboard_set(str(action.get("text") or ""))
    if kind == "copy_url":
        if not window:
            return {"ok": False, "error": "没有目标窗口，先 launch 或 snapshot"}
        try:
            snap = _snapshot(backend, window, depth=24, max_nodes=1200, chrome=True)
            match = re.search(
                r'Edit "[^"]*(?:地址|Address|search)[^"]*"\s+value="([^"]*)"',
                snap.get("text") or "",
                re.IGNORECASE,
            )
            value = html.unescape(match.group(1)) if match else ""
            if not value:
                return {"ok": False, "error": "没有读到地址栏网址"}
            written = backend.clipboard_set(value)
            if not written.get("ok"):
                return written
            return {
                "ok": True,
                "via": "copy_url",
                "text": value,
                "chars": len(value),
            }
        except Exception as exc:
            return {"ok": False, "error": f"复制网址失败：{exc}"}
    if kind == "copy":
        if not _keys_allowed():
            return {"ok": False, "error": "合成按键已在面板关闭"}
        ref = str(action.get("ref") or "")
        if not ref:
            return {"ok": False, "error": "copy 需要 ref（地址栏/输入框）"}
        if not window:
            return {"ok": False, "error": "没有目标窗口，先 launch 或 snapshot"}
        info = backend.element_info(ref)
        if not info.get("ok"):
            return info
        role = str(info.get("role") or "")
        name = str(info.get("name") or "")
        if role != "Edit" and not any(key in name for key in ("地址", "搜索", "输入")):
            return {
                "ok": False,
                "error": "copy 只能用于 Edit/地址栏/输入框；复制网页链接请用 copy_url",
                "role": role,
                "name": name,
            }
        desktop_input.focus_window(window)
        focused = backend.focus(ref)
        if not focused.get("ok"):
            return focused
        time.sleep(0.1)
        desktop_input.press_key("ctrl+a")
        time.sleep(0.05)
        desktop_input.press_key("ctrl+c")
        time.sleep(0.3)
        clip = backend.clipboard_get()
        text = str(clip.get("text") or "")
        return {
            "ok": bool(clip.get("ok") and text),
            "via": "copy",
            "text": text,
            "chars": len(text),
            "error": "" if text else "复制后剪贴板是空的",
        }
    if kind == "paste":
        if not _keys_allowed():
            return {"ok": False, "error": "合成按键已在面板关闭"}
        ref = str(action.get("ref") or "")
        if not ref:
            return {"ok": False, "error": "paste 需要 ref（目标输入框）"}
        if not window:
            return {"ok": False, "error": "没有目标窗口，先 launch 或 snapshot"}
        clip = backend.clipboard_get()
        text = str(clip.get("text") or "")
        if not text:
            return {"ok": False, "error": "剪贴板为空，先 copy 或 clipboard_set"}
        desktop_input.focus_window(window)
        focused = backend.focus(ref)
        if not focused.get("ok"):
            return focused
        time.sleep(0.1)
        desktop_input.press_key("ctrl+v")
        time.sleep(0.2)
        return {"ok": True, "via": "paste", "text": text, "chars": len(text)}
    if kind == "focus":
        return backend.focus(str(action.get("ref") or ""))
    if kind == "scroll":
        return backend.scroll(
            str(action.get("direction") or "down"),
            float(action.get("pages") or 1),
            str(action.get("ref") or ""),
        )
    if kind in ("click_at", "type_at", "drag", "scroll_at", "move"):
        if kind == "drag":
            x1, y1 = _coord(action, "from")
            x2, y2 = _coord(action, "to")
            points = [(x1, y1), (x2, y2)]
        else:
            x1, y1 = _coord(action, "x")
            points = [(x1, y1)]
        for x, y in points:
            allowed, why = _coordinate_allowed(plugin, window, x, y)
            if not allowed:
                return {"ok": False, "error": why, "x": x, "y": y}
        if window:
            desktop_input.focus_window(window)
        if kind == "move":
            return desktop_input.move(x1, y1)
        if kind == "click_at":
            return desktop_input.click(
                x1, y1,
                button=str(action.get("button") or "left"),
                clicks=int(action.get("clicks") or 1),
                modifiers=action.get("modifiers") or [],
            )
        if kind == "type_at":
            if not _keys_allowed():
                return {"ok": False, "error": "合成键入已在面板关闭"}
            return desktop_input.type_at(x1, y1, str(action.get("text") or ""))
        if kind == "scroll_at":
            return desktop_input.scroll(
                x1, y1,
                direction=str(action.get("direction") or "down"),
                pages=float(action.get("pages") or 1),
            )
        return desktop_input.drag(x1, y1, x2, y2)
    return {"ok": False, "error": f"未知动作：{kind}"}


def _panel_url() -> str:
    try:
        port = int(config.get("panel_port", 32123))
    except (TypeError, ValueError):
        port = 32123
    return f"http://127.0.0.1:{port}/"


def _task_window(task: str, context: str = "") -> int:
    """任务点名了某个窗口时，先把它作为目标，避免模型去启动同名系统程序。"""
    blob = f"{task} {context}".lower()
    try:
        windows = uia.list_windows(only_titled=True)
    except Exception:
        windows = []

    # “点开播放页/网页/视频/链接”是网页任务：优先找浏览器窗口，
    # 不要因为 QQ 在前台就去点聊天窗口里的消息（QQ 消息通常没有 Hyperlink）。
    browser_intent = any(
        key in blob for key in (
            "播放页", "网页", "浏览器", "视频", "链接", "b站", "bilibili",
            "打开网页", "打开页面",
        )
    )
    chat_target = any(
        key in blob for key in (
            "qq里", "qq对", "发给qq", "发到qq", "对话框里", "聊天框",
            "聊天窗口", "微信里", "qq消息",
        )
    )
    if browser_intent and not chat_target:
        candidates = []
        for item in windows:
            title = str(item.get("title") or "")
            lowered = title.lower()
            if ("cent browser" in lowered or "chrome" in lowered
                    or "edge" in lowered or "firefox" in lowered
                    or "哔哩哔哩" in title or "bilibili" in lowered):
                candidates.append(item)
        if candidates:
            terms = [term for term in re.findall(r"[\u4e00-\u9fff]{2,}", blob)]
            best = max(
                candidates,
                key=lambda item: sum(
                    1 for term in terms if term in str(item.get("title") or "")
                ),
            )
            return int(best["hwnd"])

    needles = []
    if "控制面板" in blob:
        needles.append("控制面板")
    if "zcode" in blob:
        needles.append("zcode")
    if not needles:
        return 0
    best = 0
    best_score = -10 ** 9
    for item in windows:
        title = str(item.get("title") or "")
        lowered = title.lower()
        score = sum(3 for needle in needles if needle in lowered)
        if "控制面板" in title and ("辉夜" in title or "Nascence" in title):
            score += 5
        # 用户说“控制面板”时指的是辉夜网页面板，不是 Windows 控制面板。
        if ("控制面板" in title or "control panel" in lowered) and not (
            "辉夜" in title or "nascence" in lowered
        ):
            score -= 10
        if score > best_score:
            best_score = score
            best = int(item["hwnd"])
    return best if best_score > 0 else 0


def _is_arrange_task(task: str) -> bool:
    blob = str(task or "")
    if "窗口" not in blob:
        return False
    if not any(key in blob for key in ("整理", "归置", "排整齐", "排列", "摆整齐", "理一理")):
        return False
    # “看看窗口怎么排列/有哪些窗口”是只读问题，不能真的去挪。
    return not any(key in blob for key in ("哪些", "列出", "看看", "内容", "标题"))


def run_task(plugin, task: str, context: str = "") -> dict:
    """执行一个电脑任务；返回 {"ok", "summary", "lines"}。"""
    backend = uia.backend()
    max_steps = max(1, min(40, _cfg_int("desktop_max_steps", 12)))
    timeout = max(30, _cfg_int("desktop_task_timeout", 180))
    profile = plugin.action_llm()
    started = time.time()
    prefer = f"{task} {context}"

    # “桌面上开着哪些窗口”是全局枚举，不该让操作手盯着某一个窗口的树去猜。
    if (re.search(r"(哪些|列出|查看|看看|告诉我).{0,12}窗口", task)
            and not any(key in task for key in ("窗口里", "窗口内", "这个窗口", "当前窗口"))):
        titles = []
        for item in uia.list_windows(only_titled=True):
            title = str(item.get("title") or "").strip()
            if not title or title in ("Program Manager", "Windows 输入体验"):
                continue
            if title not in titles:
                titles.append(title)
        summary = "当前打开的窗口：" + ("、".join(titles) if titles else "没有可见窗口")
        return {"ok": True, "summary": summary, "lines": []}

    # 窗口整理是确定性的平铺动作，不该让操作手用 win+tab/snapshot 瞎试。
    if _is_arrange_task(task):
        arranged = uia.arrange_windows()
        if arranged.get("ok"):
            names = "、".join(
                str(item.get("title") or "") for item in arranged.get("moved") or []
            )
            return {
                "ok": True,
                "summary": f"已把 {arranged.get('count', 0)} 个窗口平铺整齐：{names}",
                "lines": [],
            }
        return {
            "ok": False,
            "summary": str(arranged.get("detail") or "没有可整理的可见窗口"),
            "lines": [],
        }

    worker = plugin.worker_state()
    window: Optional[int] = int(worker.get("last_hwnd") or 0) if worker else None
    if not window:
        window = int(_task_window(task, context) or 0) or None
    if "控制面板" in f"{task} {context}" or "32123" in f"{task} {context}":
        panel_hint = (
            f"辉夜网页控制面板地址：{_panel_url()}；"
            "这是网页，不是 Windows 的 control.exe。需要打开时用 open_url。"
        )
        context = (context + "\n" if context else "") + panel_hint
    last_result = "刚刚开始，还没有动作。"
    lines = []
    llm_errors = 0
    launched_apps = set()
    snapshot_streak = 0
    # 内置程序名给模型参考；它也可以直接给 exe/lnk 路径，不受白名单限制。
    try:
        seen_paths = set()
        launchable = []
        for name, spec in backend.apps().items():
            path = str(spec.get("path") if isinstance(spec, dict) else spec).lower()
            if path in seen_paths:
                continue
            seen_paths.add(path)
            launchable.append(str(name))
        apps_hint = "、".join(launchable[:16])
    except Exception:
        apps_hint = ""

    # 起步状态由插件 perform_task 的 detail 写入，这里不再重复一条。

    for step in range(1, max_steps + 1):
        if time.time() - started > timeout:
            return {"ok": False, "summary": f"这件事做太久了，我先停下（{task}）", "lines": lines}

        # 工作桌面在跑、但还没有软件窗口时，不读宿主窗口的树：
        # 直接提示模型先 launch，避免把空桌面当成界面。
        tree_text = ""
        if not window and worker:
            tree_text = "（工作桌面已打开，还没有软件窗口。先 launch 一个程序。）"
            snap = {}
        else:
            bounds = bool(config.get("desktop_snapshot_bounds", True))
            try:
                # 浏览器的网页内容是深树：Document 在第 13 层左右，
                # 浅树只能看到地址栏和工具栏，操作手会误以为页面是空的。
                snap = _snapshot(backend, window, diff=(step > 1), max_nodes=1200,
                                 depth=20, with_bounds=bounds)
            except Exception as exc:
                worker = plugin.worker_state()
                fallback = int(worker.get("last_hwnd") or 0) if worker else 0
                if not fallback:
                    # 真实桌面模式：QQ 登录后窗口句柄会换，任务里引用的旧 hwnd 失效。
                    # 按任务/上下文里的程序名重新找一次，找不到再用前台窗口。
                    blob = f"{task} {context}".lower()
                    try:
                        if "qq" in blob and hasattr(plugin, "_find_app_window"):
                            fallback = int(plugin._find_app_window("QQ", prefer) or 0)
                        elif "记事本" in blob and hasattr(plugin, "_find_app_window"):
                            fallback = int(plugin._find_app_window("记事本", prefer) or 0)
                        elif any(key in blob for key in ("浏览器", "b站", "bilibili", "网页", "搜索")):
                            for browser in ("Cent Browser",):
                                fallback = int(
                                    _existing_app_window(plugin, browser, prefer) or 0
                                )
                                if fallback:
                                    break
                        if not fallback:
                            fg = uia.foreground_window()
                            title = uia.window_title(fg).strip()
                            if fg and title:
                                fallback = int(fg)
                    except Exception:
                        fallback = 0
                if fallback and fallback != window:
                    window = fallback
                    try:
                        snap = _snapshot(backend, window, diff=False, max_nodes=1200,
                                         depth=20, with_bounds=bounds)
                    except Exception as exc2:
                        # 页面跳转/窗口切换时的短暂失败不要直接结束整件事；
                        # 把错误交给模型，下一步重新 snapshot 或 links。
                        snap = {}
                        tree_text = (
                            f"（这一轮读界面失败：{exc2}。下一轮请先 snapshot 或 links；"
                            "不要据此判断任务失败。）"
                        )
                else:
                    snap = {}
                    tree_text = (
                        f"（这一轮读界面失败：{exc}。下一轮请先 snapshot 或 links；"
                        "不要据此判断任务失败。）"
                    )
            if not window and snap.get("hwnd"):
                window = int(snap["hwnd"])
            if snap:
                tree_text = snap.get("text") or ""
            tree_text = tree_text or "（没有读到界面；下一轮先 snapshot 或 links。）"
            browser_shell = (
                "地址和搜索栏" in tree_text or "Chrome Legacy Window" in tree_text
            )
            if not _has_interactive_nodes(tree_text) or (
                browser_shell and not _has_page_content(tree_text)
            ):
                # QQ/Electron 的浅树可能全是空 Pane；浏览器在网页加载出来前
                # 也可能只有地址栏。自动深取一次，别让模型自己钻引用。
                try:
                    deep = _snapshot(
                        backend, window, diff=False, max_nodes=2500, depth=24,
                        chrome=True, with_bounds=bounds,
                    )
                    deep_text = deep.get("text") or ""
                    if (_has_interactive_nodes(deep_text) or _has_page_content(deep_text)
                            or len(deep_text) > len(tree_text)):
                        snap = deep
                        tree_text = _compact_tree(deep_text)
                        snap["changed"] = None
                except Exception:
                    pass
        if step > 1 and snap.get("changed") is not None and snap.get("changed") <= 12:
            tree_text = (snap.get("diff") or "") + "\n" + tree_text
        # 去掉浏览器工具栏/书签栏，优先把网页里的按钮和输入框留给模型。
        tree_text = _compact_browser_web(tree_text)
        # 链接和页面控件摘要放在树前面：长页面截断时这些引用仍然可见。
        link_summary = _link_summary(tree_text, limit=10)
        control_summary = _control_summary(tree_text, limit=24)
        prefixes = [part for part in (link_summary, control_summary) if part]
        if prefixes:
            tree_text = "\n".join(prefixes) + "\n" + tree_text
        if len(tree_text) > 9000:
            tree_text = tree_text[:9000] + "\n…（已截断）"

        messages = [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": (
                    f"【任务】{task}\n"
                    f"【此刻的情况】{context or '没有额外背景'}\n"
                    f"【可启动的程序】{apps_hint or '（内置表为空）'}"
                    "（也可以直接给 exe/lnk 路径）\n"
                    f"【上一步结果】{last_result}\n"
                    f"【界面】\n{tree_text}\n\n"
                    f"这是第 {step}/{max_steps} 步。请输出下一个动作 JSON。"
                ),
            },
        ]
        try:
            raw = call_api_thinking(messages, max_tokens=300, thinking=False, profile=profile)
        except Exception as exc:
            llm_errors += 1
            append_log(f"[电脑] 第{step}步模型调用失败 {traceback.format_exc()}")
            if llm_errors >= 2:
                return {
                    "ok": False,
                    "summary": f"电脑这边调用模型失败：{exc}",
                    "lines": lines,
                }
            last_result = f"上一次模型调用失败：{exc}；重新看界面后继续。"
            time.sleep(0.8)
            continue
        llm_errors = 0
        raw = str(raw or "")
        action = _parse(raw)
        kind = str(action.get("action") or "").strip().lower()
        append_log(f"[电脑] 第{step}步 {raw[:120]}")

        if kind == "done":
            summary = str(action.get("summary") or "做完了").strip()
            return {"ok": True, "summary": summary, "lines": lines}
        if kind == "fail":
            summary = str(action.get("summary") or "没做成").strip()
            return {"ok": False, "summary": summary, "lines": lines}
        if not kind:
            last_result = "上一轮输出不是合法 JSON，请只输出一个动作 JSON。"
            continue

        if kind == "snapshot":
            snapshot_streak += 1
            if snapshot_streak >= 4:
                return {
                    "ok": False,
                    "summary": f"连续 {snapshot_streak} 次只看界面没有动作，先停下（{task}）",
                    "lines": lines,
                }
            if snapshot_streak == 3:
                last_result = (
                    "你已经连续两次重新看界面，信息已经够判断了。"
                    "下一步必须 click/set_value/type_text/key/open_url，"
                    "或者直接用 done/fail；不能再 snapshot。"
                )
                lines.append("连续观察过多，已阻止第三次 snapshot")
                continue
        else:
            snapshot_streak = 0

        plugin.on_progress(f"第 {step} 步：{_describe(action)}")
        launch_skip = None
        if kind == "launch":
            name = str(action.get("name") or "").strip()
            key = name.lower()
            existing = _existing_app_window(plugin, name, prefer) if name else 0
            if existing:
                desktop_input.focus_window(existing)
                launch_skip = {
                    "ok": True,
                    "hwnd": existing,
                    "reused": True,
                    "detail": f"{name} 已经打开，直接使用现有窗口",
                }
            elif key and key in launched_apps:
                launch_skip = {
                    "ok": False,
                    "error": f"{name} 在这个任务里已经启动过，不要反复 launch；先看现有窗口",
                }
            elif key:
                launched_apps.add(key)
        try:
            result = launch_skip if launch_skip is not None else _execute(
                plugin, backend, action, int(window or 0), prefer
            )
        except Exception as exc:
            # 单步异常不能带走整件事：记下原因，下一步让模型重新看界面。
            append_log(f"[电脑] 第{step}步异常 {traceback.format_exc()}")
            result = {"ok": False, "error": f"动作执行出错：{exc}"}
        if not isinstance(result, dict):
            result = {"ok": False, "error": "动作没有返回结果"}
        if int(result.get("hwnd") or 0):
            window = int(result["hwnd"])
        last_result = json.dumps(result, ensure_ascii=False)
        lines.append(f"{_describe(action)}：{'成功' if result.get('ok') else result.get('error')}")

    return {
        "ok": False,
        "summary": f"这件事花了 {max_steps} 步还没做完，先停下（{task}）",
        "lines": lines,
    }
