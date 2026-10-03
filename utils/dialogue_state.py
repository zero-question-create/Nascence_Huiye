# utils/dialogue_state.py
#
# 对话状态按群隔离（F09）：此前是全局单例，多个群的参与者与话题会互相覆盖，
# 导致"群 B 的消息把群 A 的话题冲掉"。现在按 group_id 分别维护。
#
# 群标识通过 contextvar 传递：消息处理线程/任务在入口设置一次，
# 后续所有 get_state / set_state 自动落到对应群，无需改动每一层函数签名。
# 默认群（None）保留给 CLI 等无群上下文的场景。

import contextvars

DEFAULT_STATE = {
    "参与者": [],
    "最近话题": "无"
}

# {group_id: state}；group_id 为 None 表示无群上下文的默认会话
_states = {}

# 当前上下文所属的群；set_group / reset_group 成对使用
_current_group = contextvars.ContextVar("dialogue_group", default=None)


def set_group(group_id):
    """设置当前上下文的群标识，返回 token（供 reset_group 还原）。"""
    return _current_group.set(str(group_id) if group_id is not None else None)


def reset_group(token):
    """还原群标识上下文。"""
    try:
        _current_group.reset(token)
    except (ValueError, LookupError):
        pass


def current_group():
    return _current_group.get()


def _key(group_id=None):
    if group_id is None:
        group_id = _current_group.get()
    return str(group_id) if group_id is not None else None


def get_state(group_id=None):
    """返回当前群（或指定群）的对话状态。"""
    return _states.get(_key(group_id), DEFAULT_STATE.copy())


def set_state(new_state: dict, group_id=None):
    """更新当前群（或指定群）的对话状态。非 dict 输入忽略。"""
    if not isinstance(new_state, dict):
        return
    _states[_key(group_id)] = new_state


def reset_state(group_id=None):
    """重置当前群（或指定群）的对话状态。"""
    _states[_key(group_id)] = DEFAULT_STATE.copy()


def all_states() -> dict:
    """返回全部状态（供持久化）。None 键会写成 "null" 以适配 JSON。"""
    return {(k if k is not None else "null"): v for k, v in _states.items()}


def load_states(data):
    """从持久化数据恢复。兼容旧格式（整个文件是一份状态对象）。"""
    _states.clear()
    if not isinstance(data, dict):
        return
    if "参与者" in data:
        # 旧格式：归入默认会话
        _states[None] = data
        return
    for k, v in data.items():
        if isinstance(v, dict):
            _states[None if k == "null" else k] = v
