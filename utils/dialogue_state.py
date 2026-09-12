# utils/dialogue_state.py

DEFAULT_STATE = {
    "参与者": [],
    "最近话题": "无"
}

current_state = DEFAULT_STATE.copy()

def get_state():
    """返回当前对话状态"""
    return current_state

def set_state(new_state: dict):
    """更新当前对话状态"""
    global current_state
    current_state = new_state

def reset_state():
    """重置对话状态"""
    global current_state
    current_state = DEFAULT_STATE.copy()