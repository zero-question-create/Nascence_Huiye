import os, json, threading, time
import uuid

_message_history = []
_max_history = 200
_lock = threading.Lock()
# 长期日志游标：记录**已写入日志的最大序号**（seq），而不是"列表长度"。
#
# 原实现用 _flushed_count = len(_message_history) 当下标切片，一旦列表被
# _max_history 截断到 200，_message_history[200:] 永远为空——日志从此不再
# 追加；且裁剪是从头部删除，下标会与新窗口错位，可能重复或漏写（F12）。
# 改用单调自增的 seq 定位"哪些还没落盘"，与列表长度解耦。
#
# 序号从 1 开始（不是 0）：判定条件是 `seq > _flushed_seq`，
# 若首条为 0 则 0 > 0 为假，第一条永远写不出去。
_next_seq = 1
_flushed_seq = 0
# 落盘节流：调用方可频繁调用 flush，真正写盘最多每 FLUSH_MIN_INTERVAL 秒一次
FLUSH_MIN_INTERVAL = 30.0
_last_flush_time = 0.0
_HISTORY_FILE = "data/test/message_history.log"
_STATE_FILE = "data/test/message_state.json"


def add_message(sender, content, source, quote=None):
    """追加一条历史消息，返回其序号（seq）。

    quote: 可选的引用信息 {"sender": 被引用者, "text": 被引用的原话}。
    长期记忆侧由 decompose_input 把引用解析成记忆片段；
    这里的 quote 字段服务于短期上下文——让历史窗口能看到"这句话在回应什么"。
    """
    from core.virtual_clock import clock
    global _next_seq
    with _lock:
        record = {
            "sender": sender,
            "content": content,
            "source": source,
            "time": clock.now(),
            "seq": _next_seq,
        }
        _next_seq += 1
        if quote and quote.get("text"):
            record["quote"] = {
                "sender": quote.get("sender") or "",
                "text": quote.get("text") or "",
            }
        _message_history.append(record)
        if len(_message_history) > _max_history:
            del _message_history[:len(_message_history) - _max_history]
        seq = record["seq"]
    save_state()
    # 顺带按节流周期落盘长期日志：此前 flush 只在控制面板关停时调用，
    # 进程被强杀会丢掉全部历史。这里让运行期也能定期持久化。
    try:
        flush_to_file()
    except Exception:
        pass
    return seq


def mark_delivery_failed(seq) -> bool:
    """给指定序号的记录追加"（消息发送失败）"后缀（F20 用）。

    发送失败的内容不应被后续轮次当成"已经说出口的话"，
    因此在历史里显式留痕。返回是否命中。

    注意：改完必须**先释放锁再保存**——save_state() 自己会取 _lock，
    而 threading.Lock 不可重入，在锁内调用会直接死锁（实测会静默挂住进程）。
    """
    if seq is None:
        return False
    hit = False
    with _lock:
        for msg in reversed(_message_history):
            if msg.get("seq") == seq:
                if "（消息发送失败）" not in msg["content"]:
                    msg["content"] = f"{msg['content']}（消息发送失败）"
                hit = True
                break
    if hit:
        save_state()
    return hit


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
    """原子保存短期历史快照。

    两点修复（F13）：
    1. 临时文件名带唯一后缀：此前所有调用共用同一个 `.tmp`，而写入在锁外，
       两个线程同时保存会在同一文件上竞争，实测可抛 FileNotFoundError，
       也可能让较旧的快照覆盖较新的内容。
    2. Windows 上 os.replace 偶发被杀毒/索引服务瞬态占用（WinError 5/32），
       短重试即可；重试仍失败则放弃本次保存（记录已在内存中，不应因此抛错
       打断调用方的写入流程）。
    """
    global _flushed_seq
    with _lock:
        data = list(_message_history)
        flushed = _flushed_seq
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    tmp_file = f"{_STATE_FILE}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:6]}.tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            # 顺带把日志游标一并持久化：重启后才知道哪些号已经写过日志
            json.dump({"messages": data, "flushed_seq": flushed}, f, ensure_ascii=False, indent=2)
        for attempt in range(5):
            try:
                os.replace(tmp_file, _STATE_FILE)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except OSError:
        try:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
        except OSError:
            pass
        # 保存失败不向上抛：记录已在内存，落盘下次再试，不该打断写入流程
        return


def load_state():
    global _message_history, _flushed_seq, _next_seq, _last_flush_time
    if not os.path.exists(_STATE_FILE):
        return
    try:
        with open(_STATE_FILE, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(f"[消息历史] 状态文件损坏，已重建：{_STATE_FILE}")
        with _lock:
            _message_history, _flushed_seq, _next_seq = [], 0, 1
        return

    # 兼容旧格式（顶层直接是消息数组，没有游标信息）
    if isinstance(raw, dict):
        data = raw.get("messages", [])
        flushed = int(raw.get("flushed_seq", 0))
    else:
        data = raw
        flushed = 0   # 旧格式无从得知，视为都未落盘（宁可重复也不丢）

    with _lock:
        _message_history = data[-_max_history:]
        # 旧记录可能没有 seq，先按 1..n 补齐（序号从 1 开始）
        for i, m in enumerate(_message_history):
            m.setdefault("seq", i + 1)
        # 序号以历史里出现过的最大值为准，避免与新进程的 _next_seq 撞号
        max_seq = max((m.get("seq", 0) for m in _message_history), default=0)
        _next_seq = max_seq + 1
        _flushed_seq = max(0, min(flushed, max_seq))
    # 重启后给节流一个基准，避免启动瞬间立刻写盘
    _last_flush_time = time.time()


def flush_to_file(force: bool = False):
    """把尚未落盘的短期历史追加写入长期日志。

    以 seq > _flushed_seq 判定待写条目，与列表是否被截断无关；
    且**全部写入成功后**才推进游标，避免写一半失败导致丢日志。

    进程内自动节流：调用方可频繁调用（例如每次写入历史时），
    但真正落盘最多每 FLUSH_MIN_INTERVAL 秒一次，避免高频 IO。
    关停等关键路径可传 force=True 强制落盘。
    """
    global _flushed_seq, _last_flush_time
    import time as _time
    now = _time.time()
    if force is False and (now - _last_flush_time) < FLUSH_MIN_INTERVAL:
        return
    with _lock:
        pending = [m for m in _message_history if m.get("seq", -1) > _flushed_seq]
        if not pending:
            _last_flush_time = now
            return
        new_flushed = max(m["seq"] for m in pending)
    os.makedirs(os.path.dirname(_HISTORY_FILE), exist_ok=True)
    with open(_HISTORY_FILE, "a", encoding="utf-8") as f:
        for msg in pending:
            # 与历史窗口保持一致的渲染，引用不落丢
            f.write(render_message(msg) + "\n")
    with _lock:
        _flushed_seq = max(_flushed_seq, new_flushed)
        _last_flush_time = now
    save_state()
