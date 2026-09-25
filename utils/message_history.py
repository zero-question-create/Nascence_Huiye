import os, json, threading

_message_history = []
_max_history = 200
_lock = threading.Lock()
_flushed_count = 0
_HISTORY_FILE = "data/test/message_history.log"
_STATE_FILE = "data/test/message_state.json"


def add_message(sender, content, source, quote=None):
    """追加一条历史消息。

    quote: 可选的引用信息 {"sender": 被引用者, "text": 被引用的原话}。
    长期记忆侧由 decompose_input 把引用解析成记忆片段；
    这里的 quote 字段服务于短期上下文——让历史窗口能看到"这句话在回应什么"。
    """
    from core.virtual_clock import clock
    with _lock:
        record = {"sender": sender, "content": content, "source": source, "time": clock.now()}
        if quote and quote.get("text"):
            record["quote"] = {
                "sender": quote.get("sender") or "",
                "text": quote.get("text") or "",
            }
        _message_history.append(record)
        if len(_message_history) > _max_history:
            del _message_history[:len(_message_history) - _max_history]
    save_state()


def quote_suffix(msg) -> str:
    """把一条消息的引用渲染成后缀；无引用时返回空串。

    用自然语序而非括号补充，避免与提示词里"禁止括号补充"的约束产生歧义。
    """
    quote = msg.get("quote") or {}
    text = quote.get("text")
    if not text:
        return ""
    quoted_sender = quote.get("sender") or "某人"
    return f"，这是在回应{quoted_sender}之前说的：“{text}”"


def render_message(msg) -> str:
    """把一条历史消息渲染成可读文本（含引用）。"""
    return f"{msg['sender']}说：{msg['content']}{quote_suffix(msg)}"


def get_recent(n=10):
    with _lock:
        recent = _message_history[-n:]
    return "\n".join(render_message(m) for m in recent)


def get_all():
    with _lock:
        return list(_message_history)


def remove_last(n=2):
    with _lock:
        del _message_history[-n:]
    save_state()


def save_state():
    with _lock:
        data = list(_message_history)
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    tmp_file = _STATE_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, _STATE_FILE)


def load_state():
    global _message_history, _flushed_count
    if not os.path.exists(_STATE_FILE):
        return
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(f"[消息历史] 状态文件损坏，已重建：{_STATE_FILE}")
        with _lock:
            _message_history, _flushed_count = [], 0
        return
    with _lock:
        _message_history = data[-_max_history:]
        _flushed_count = len(_message_history)


def flush_to_file():
    global _flushed_count
    with _lock:
        pending = _message_history[_flushed_count:]
        if not pending:
            return
        _flushed_count = len(_message_history)
    os.makedirs(os.path.dirname(_HISTORY_FILE), exist_ok=True)
    with open(_HISTORY_FILE, "a", encoding="utf-8") as f:
        for msg in pending:
            # 与历史窗口保持一致的渲染，引用不落丢
            f.write(render_message(msg) + "\n")
