# utils/dialogue_state.py

DEFAULT_STATE = {
    "参与者": [],
    "最近话题": "无",
    "我的已知信息": []
}

current_state = DEFAULT_STATE.copy()

def get_state():
    """返回当前对话状态"""
    return current_state

def set_state(new_state: dict):
    """更新当前对话状态"""
    global current_state
    current_state = new_state
    # 预埋：限制已知信息列表长度，防止长期对话撑爆LLM上下文
    MAX_KNOWN_INFO = 20
    if len(current_state.get("我的已知信息", [])) > MAX_KNOWN_INFO:
        current_state["我的已知信息"] = current_state["我的已知信息"][-MAX_KNOWN_INFO:]

def reset_state():
    """重置对话状态"""
    global current_state
    current_state = DEFAULT_STATE.copy()