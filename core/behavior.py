# core/behavior.py
# ========================================================================
# 行为思考层（全局语言模型）。
#
# 分工：
#   1. 本地认知循环（core/cognition.py）只做本地计算：检索、扩散、概念路、
#      反刍抑制、短期对话窗口。它不依赖任何平台，也不调用 LLM 做记忆选择。
#   2. 每轮最后一跳“语言/行为思考”交给本模块：把已经选好的记忆交给全局
#      primary LLM，产出“此刻的想法 + 是否说出口”。插件不能覆盖语言模型。
#
# 提示词约束（人格涌现的前提）：
#   - 行为思考只接收名字（BOT_NAME）与记忆；不注入平台名、插件名、群号、
#     用户 ID、连接方式等外部信息。
#   - 这里的 system prompt 与重构前的 verbalize 逐字一致，属于“最小提示词”：
#     除了名字和记忆，只保留模型判断发言所必需的时间/状态行。
#   - 新增插件不得修改这段提示词，也不得在 user 内容里追加平台元数据。
# ========================================================================

import datetime
import re
from typing import Optional

from config.constants import BOT_NAME
from utils.monitor import append_log
from utils.metrics_probe import PROBE

from .llm_profile import LLMProfile


def behavior_system_prompt() -> str:
    """行为思考的静态 system prompt（保持与重构前一致）。"""
    return (
        f"你是{BOT_NAME}，正在思考。根据下面给出的记忆和情况，输出此刻的想法，并判断是否说出口。\n\n"
        "规则：\n"
        "- 如果有人在对你说什么，且你想回应，say 为 true；只是自然浮现的念头、联想，say 为 false\n"
        "- say 为 true 时，text 是一句简短口语，尽量 20 字以内，只能使用给定记忆中的信息\n"
        "- 不知道的事情不要提及，只能使用记忆或状态里明确给出的信息\n"
        "- 不要复述记忆里已有的同一内容，也不要重复自己刚才说过的话\n"
        "- 每条记忆开头的[刚刚]/[N天前]只是时间标签，表示这件事发生在何时，不是一条新消息\n"
        "- 记忆按时间排序，可以直接引用、补充观点或提问，但不要添加动作、神态或括号补充\n"
        "输出严格只包含 JSON：\n"
        '{"say": true/false, "text": "..."}\n'
    )


def behavior_user_content(points: str, new_state: dict = None, body_state: str = None,
                          now: datetime.datetime = None) -> str:
    """行为思考的动态 user 内容。

    points 已经是渲染好的记忆行（含时间标签）。除此之外不添加任何平台信息。
    """
    now = now or datetime.datetime.now()
    state_hint = f"当前状态：{new_state}" if new_state else ""
    time_hint = f"现在时间为{now:%Y年%m月%d日 %H时%M分}"
    body_hint = f"【身体状态】{body_state}\n\n" if body_state else ""
    return (
        f"{state_hint}\n{time_hint}\n\n"
        f"{body_hint}"
        f"记忆片段（按时间排序）：\n{points}\n\n"
        "请结合这些记忆和对话情况，输出你此刻的想法。"
    )


def _prepare_memories(memories: list, keywords: list = None, pinned: list = None,
                      timestamps: list = None, pinned_timestamps: list = None) -> list:
    """按重构前 verbalize 的规则整理记忆：pinned 优先、关键词过滤、时间排序、去重。"""
    pinned = list(pinned or [])
    pinned_set = set(pinned)
    if pinned_timestamps is None:
        pinned_ts_list = [None] * len(pinned)
    else:
        pinned_ts_list = list(pinned_timestamps) + [None] * max(0, len(pinned) - len(pinned_timestamps))

    ts = list(timestamps) if timestamps is not None else [None] * len(memories)

    # 普通记忆：剔除 pinned 条目（pinned 后续无条件保留）
    normal = []
    normal_ts = []
    for mem, t in zip(memories, ts):
        if mem not in pinned_set:
            normal.append(mem)
            normal_ts.append(t)

    # 关键词过滤：只作用于普通记忆
    if keywords:
        kw_hits = []
        kw_hits_ts = []
        for mem, t in zip(normal, normal_ts):
            if any(kw in mem for kw in keywords):
                kw_hits.append(mem)
                kw_hits_ts.append(t)
        if kw_hits:
            normal = kw_hits
            normal_ts = kw_hits_ts
    normal = normal[:10]
    normal_ts = normal_ts[:10]

    # 合并：pinned（历史对话、上一轮回复）无条件保留，后接普通记忆
    merged = [m for m in pinned] + normal
    merged_ts = [t for t in pinned_ts_list] + normal_ts

    # 排序：越早越靠前（升序）；无时间戳（None）的条目排最后
    if timestamps is not None:
        indexed = sorted(
            range(len(merged)),
            key=lambda i: (merged_ts[i] is None, merged_ts[i] if merged_ts[i] is not None else 0)
        )
        merged = [merged[i] for i in indexed]
    else:
        merged.reverse()

    # 全局内容查重：pinned 条目优先保留；重复内容只保留一条
    def _content_key(m):
        return re.sub(r"^\[[^\]]*\]\s*", "", m)

    seen_content = set()
    deduped = []
    for m in merged:
        key = _content_key(m)
        if key in seen_content:
            if m in pinned_set:
                for i, existing in enumerate(deduped):
                    if _content_key(existing) == key:
                        deduped[i] = m
                        break
            continue
        seen_content.add(key)
        deduped.append(m)
    return deduped


class BehaviorEngine:
    """把“本地认知循环选出的记忆”交给全局 primary LLM 做语言/行为思考。

    profile 为 None 时使用全局 primary 模型（CLI、旧调用方的兼容路径）。
    """

    def __init__(self, profile: Optional[LLMProfile] = None, plugin_id: str = ""):
        self.profile = profile
        self.plugin_id = plugin_id or (profile.plugin_id if profile else "core")

    def think(self, memories: list, keywords: list = None, new_state: dict = None,
              user_input: str = None, pinned: list = None, timestamps: list = None,
              pinned_timestamps: list = None, body_state: str = None,
              include_reasoning: bool = False) -> dict:
        """生成内心独白与发言决策，返回 {"say": bool, "text": str}。

        参数语义与重构前的 core.llm_interface.verbalize 完全一致。
        include_reasoning=True 时额外返回 "reasoning"（语言层的思考过程），
        由插件设置决定是否交给动作模型。
        """
        if not memories and not pinned:
            return {"say": False, "text": ""}

        keywords = list(keywords or [])
        if user_input:
            keywords.append(user_input)

        memories = _prepare_memories(
            memories, keywords=keywords, pinned=pinned,
            timestamps=timestamps, pinned_timestamps=pinned_timestamps,
        )

        points = "\n".join([f"- {m}" for m in memories])

        append_log("=" * 30 + "LLM特供记忆" + "=" * 30)
        append_log(points)

        system_prompt = behavior_system_prompt()
        user_content = behavior_user_content(points, new_state=new_state, body_state=body_state)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        # 延迟导入：llm_interface.verbalize 会在调用时反向引用本模块，
        # 顶层互相 import 会形成循环。
        from .llm_interface import call_api_thinking, fingerprint_prompt, _safe_json_parse, _coerce_bool

        fingerprint_prompt("verbalize", system_prompt, profile=self.profile)

        # 认知循环高频调用：默认只输出一句 JSON 短句，关闭推理以降低 token。
        # 当前软件要求行为模型接收思维链时，才打开推理并捕获 reasoning。
        PROBE.bump("llm.verbalize.calls")
        PROBE.bump(f"behavior.{self.plugin_id}.calls")
        with PROBE.timed("llm.verbalize_ms"):
            raw = call_api_thinking(
                messages,
                max_tokens=600 if include_reasoning else 300,
                thinking=bool(include_reasoning),
                profile=self.profile,
                return_reasoning=bool(include_reasoning),
            )

        reasoning = ""
        if include_reasoning:
            reply, reasoning = raw if isinstance(raw, tuple) else (raw, "")
        else:
            reply = raw

        append_log("=" * 30 + "理解回复" + "=" * 30)
        append_log(reply)
        if reasoning:
            append_log("=" * 30 + "语言层思考过程" + "=" * 30)
            append_log(reasoning[:1500])

        if not reply:
            result = {"say": False, "text": ""}
            if include_reasoning:
                result["reasoning"] = reasoning
            return result

        data = _safe_json_parse(reply)
        if "say" in data or "text" in data:
            result = {
                "say": _coerce_bool(data.get("say", False)),
                "text": str(data.get("text", "")),
            }
            if include_reasoning:
                result["reasoning"] = reasoning
            return result

        # 完全无法解析时降级
        append_log("*" * 30 + "JSON解析失败，降级处理" + "*" * 30)
        result = {"say": True, "text": reply}
        if include_reasoning:
            result["reasoning"] = reasoning
        return result

    def act(self, thought_text: str, should_speak: bool, said_text: str = "",
            keywords: list = None, context_hint: str = "") -> dict:
        """动作抉择（本能冲动）走当前软件的行为模型；提示词由动作层负责。"""
        from .action_layer import decide_action
        return decide_action(
            thought_text=thought_text,
            should_speak=should_speak,
            said_text=said_text,
            keywords=keywords,
            context_hint=context_hint,
            profile=self.profile,
        )
