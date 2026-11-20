# plugins/desktop/dsh_bridge.py
# ========================================================================
# 【未接线 · 预留】外联 DeepSeek Harness 的命令模板适配层。
#
# 当前版本不调用本模块：电脑插件只走 NHY 自己的操作手。
# 保留它作为最小预留接口：如果以后只想用一条命令把任务交给外部 agent，
# 在 _run_task 里调用 run_task() 即可，返回值格式与 operator 一致。
#
# 命令模板从 desktop_dsh_command 读取，{task} 会被替换；stdout 最后一行当摘要。
# ========================================================================

from __future__ import annotations

import subprocess

from config.api_config import config


def available() -> bool:
    return bool(str(config.get("desktop_dsh_command") or "").strip())


def run_task(plugin, task: str, context: str = "") -> dict:
    template = str(config.get("desktop_dsh_command") or "").strip()
    if not template:
        return {
            "ok": False,
            "summary": "还没有配置 dsh 命令（desktop_dsh_command），这次先用本机操作",
            "lines": [],
        }
    command = template.replace("{task}", str(task or ""))
    try:
        timeout = max(30, int(config.get("desktop_task_timeout", 180)))
    except (TypeError, ValueError):
        timeout = 180
    plugin.on_progress("把这件事交给 dsh 了")
    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "summary": f"dsh 超时了（{timeout}s）", "lines": []}
    except Exception as exc:
        return {"ok": False, "summary": f"dsh 启动失败：{exc}", "lines": []}

    output = (completed.stdout or "").strip()
    errors = (completed.stderr or "").strip()
    summary = output.splitlines()[-1] if output else ""
    if completed.returncode != 0:
        tail = (errors or output).splitlines()[-3:]
        return {
            "ok": False,
            "summary": "dsh 没做成：" + (" / ".join(tail) or f"退出码 {completed.returncode}"),
            "lines": tail,
        }
    return {
        "ok": True,
        "summary": summary or "dsh 说做完了",
        "lines": output.splitlines()[-4:],
    }
