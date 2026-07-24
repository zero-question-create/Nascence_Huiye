import os, json, threading

_message_history = []
_max_history = 200
_lock = threading.Lock()
_flushed_count = 0
_HISTORY_FILE = "data/test/message_history.log"
_STATE_FILE = "data/test/message_state.json"


def add_message(sender, content, source):
    with _lock:
        _message_history.append({"sender": sender, "content": content, "source": source})
        if len(_message_history) > _max_history:
            del _message_history[:len(_message_history) - _max_history]
    save_state()


def get_recent(n=10):
    with _lock:
        recent = _message_history[-n:]
    lines = []
    for msg in recent:
        lines.append(f"{msg['sender']}说：{msg['content']}")
    return "\n".join(lines)


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
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_state():
    global _message_history, _flushed_count
    if not os.path.exists(_STATE_FILE):
        return
    with open(_STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
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
            f.write(f"{msg['sender']}说：{msg['content']}\n")
