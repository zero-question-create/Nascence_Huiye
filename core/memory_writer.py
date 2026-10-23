# core/memory_writer.py
# ========================================================================
# 记忆写者与热集规划器。
#
# 目标（对齐修复方案阶段 4）：
#   1. 单写者：SQLite 的批量写入（时间戳同步、链接增量、淘汰落库、清理）
#      统一交给一条常驻工作线程，主线程只投递命令、不直接写库。
#   2. 原子写：所有派生文件（wordweb.json / faiss 映射）由写者用
#      “临时文件 + fsync + os.replace”完成，不再由多个线程各自写。
#   3. 热集规划：HotSetPlanner 在独立线程里按紧凑快照计算淘汰名单，
#      主线程拿到名单后做 CAS 校验再应用，避免把“已经被访问过”的条目误淘汰。
#
# 约束：
#   - 写者只做 I/O，不参与认知决策；
#   - 命令带序号，创建记忆这类需要确定性的操作可等待 ack；
#   - 队列有界，满时阻塞投递（背压），不做静默丢弃。
# ========================================================================

import multiprocessing
import os
import queue
import threading
import time
from dataclasses import dataclass, field

from utils.metrics_probe import PROBE
from utils.monitor import append_log


@dataclass
class _Command:
    kind: str
    payload: dict = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None


def _writer_mode() -> str:
    try:
        from config.api_config import config
        mode = str(config.get("memory_writer_mode", "process") or "process").strip().lower()
    except Exception:
        mode = "process"
    return mode if mode in ("process", "thread", "legacy") else "process"


# 这些命令依赖主进程内存里的状态（脏集合、热集、faiss），
# 独立写进程无法执行，必须在主进程内完成；它们的数据库写入再经写者转交。
_MEMORY_LOCAL_KINDS = {
    "flush_timestamps", "sync_links", "noop",
    "evict_memories", "evict_links", "purge", "custom",
}


class MemoryWriter:
    """记忆写者：SQLite 写入的唯一执行者。

    三种模式：
      - process：独立进程持有写连接（默认）。进程起不来时自动退回 thread。
      - thread ：工作线程持有写连接。
      - legacy ：不启用写者，调用方直接同步写（兼容旧行为）。

    使用方式：
        writer.start()
        writer.submit("flush_timestamps")          # 不等待
        writer.submit("sync_links", wait=True)     # 等待完成
        writer.stop()
    """

    def __init__(self, max_queue: int = 2000, batch: int = 100, flush_ms: int = 100):
        self._queue = queue.Queue(maxsize=max(1, int(max_queue)))
        self._batch = max(1, int(batch))
        self._flush_ms = max(0, int(flush_ms)) / 1000.0
        self._thread = None
        self._process = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._rid = 0
        self._mode_actual = None
        self._cmd_queue = None
        self._res_queue = None
        self._ready_event = None
        self._stats = {"executed": 0, "errors": 0, "batches": 0, "ipc_roundtrips": 0}

    # ---------- 生命周期 ----------
    def start(self):
        mode = _writer_mode()
        if mode == "legacy":
            self._mode_actual = "legacy"
            return
        if mode == "process":
            try:
                self._start_process()
                self._mode_actual = "process"
                append_log("[记忆写者] 已启动独立写进程")
                return
            except Exception as e:
                append_log(f"[记忆写者] 写进程启动失败，退回线程模式: {e}")
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._mode_actual = "thread"
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="MemoryWriter", daemon=True)
            self._thread.start()
            self._mode_actual = "thread"
            append_log("[记忆写者] 已启动工作线程")

    def _start_process(self):
        from core.memory_engine import DB_FILE
        from core.memory_writer_proc import writer_main

        ctx = multiprocessing.get_context("spawn")
        self._cmd_queue = ctx.Queue(maxsize=2000)
        self._res_queue = ctx.Queue()
        self._ready_event = ctx.Event()
        self._process = ctx.Process(
            target=writer_main,
            args=(os.path.abspath(DB_FILE), self._cmd_queue, self._res_queue, self._ready_event),
            name="MemoryWriterProc",
            daemon=True,
        )
        self._process.start()
        if not self._ready_event.wait(timeout=20):
            raise RuntimeError("写进程未在 20 秒内就绪")
        if self._process.exitcode is not None:
            raise RuntimeError(f"写进程提前退出（code={self._process.exitcode}）")

    def stop(self, timeout: float = 10.0):
        self._stop.set()
        if self._process is not None:
            try:
                self._cmd_queue.put(None)
            except Exception:
                pass
            self._process.join(timeout=timeout)
            if self._process.is_alive():
                self._process.terminate()
            self._process = None
            self._cmd_queue = None
            self._res_queue = None
            self._ready_event = None
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    @property
    def mode(self) -> str:
        return self._mode_actual or _writer_mode()

    # ---------- 投递 ----------
    def submit(self, kind: str, wait: bool = False, **payload):
        """投递一条命令；wait=True 时等待执行完成并返回结果。

        分三类：
          - 依赖主进程内存的命令 → 在调用线程本地执行，其 DB 写入再转交写者；
          - 纯数据行命令 → 交给写者（进程/线程）串行落库；
          - 若当前**就在写者线程里**（例如 evict/purge 内部又触发落库），
            必须本地执行：队列的唯一消费者就是本线程，等待自己的 ack 必死锁。
        """
        if kind in _MEMORY_LOCAL_KINDS:
            return self._execute(_Command(kind, payload))
        if self._thread is not None and threading.current_thread() is self._thread:
            return self._execute(_Command(kind, payload))
        mode = self._mode_actual or _writer_mode()
        if mode == "legacy":
            return self._execute(_Command(kind, payload))
        if mode == "process":
            return self._submit_process(kind, payload, wait)
        if self._thread is None or not self._thread.is_alive():
            return self._execute(_Command(kind, payload))
        cmd = _Command(kind, payload)
        try:
            self._queue.put(cmd, timeout=5.0)
        except queue.Full:
            PROBE.bump("writer.queue_full")
            append_log("[记忆写者] 队列已满，退化为同步执行")
            return self._execute(cmd)
        if wait:
            cmd.done.wait(timeout=30.0)
            return cmd.result
        return None

    def _submit_process(self, kind: str, payload: dict, wait: bool):
        """进程模式：命令经队列发送，按 rid 等待 ack。

        用 _io_lock 串行化“发送 + 接收”，避免多线程下拿错别人的响应；
        SQLite 写入本就是串行的，这个串行化不损失吞吐。
        """
        if self._process is None or not self._process.is_alive():
            # 进程异常退出：降级为线程/同步执行，保证数据不丢
            PROBE.bump("writer.process_down")
            append_log("[记忆写者] 写进程不可用，本次改由主进程直接写")
            return self._execute(_Command(kind, payload))
        with self._io_lock:
            self._rid += 1
            rid = self._rid
            try:
                self._cmd_queue.put({"rid": rid, "kind": kind, "payload": payload}, timeout=5.0)
            except queue.Full:
                PROBE.bump("writer.queue_full")
                return self._execute(_Command(kind, payload))
            if not wait:
                # 不等待型命令（时间戳同步）仍需读走响应，避免响应队列堆积：
                # 用短超时读，超时就放弃，不影响后续命令。
                try:
                    _, result = self._res_queue.get(timeout=10.0)
                    self._stats["ipc_roundtrips"] += 1
                except queue.Empty:
                    result = None
                return result
            deadline = time.time() + 30.0
            while time.time() < deadline:
                try:
                    got_rid, result = self._res_queue.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    break
                self._stats["ipc_roundtrips"] += 1
                if got_rid == rid:
                    if isinstance(result, dict) and result.get("error"):
                        append_log(f"[记忆写者] 命令 {kind} 失败: {result['error']}")
                    return result
                # 不是本次的响应：丢弃并继续等（前面的不等待命令可能留下回执）
            append_log(f"[记忆写者] 命令 {kind} 等待回执超时")
            return None

    def drain(self, timeout: float = 15.0):
        """等待队列清空（关停/保存前调用）。"""
        if self._thread is None:
            return
        cmd = self.submit("noop", wait=True)
        return cmd

    def stats(self) -> dict:
        return dict(self._stats)

    # ---------- 执行循环 ----------
    def _run(self):
        buffered = []
        last_flush = time.time()
        while not self._stop.is_set():
            timeout = max(0.01, self._flush_ms - (time.time() - last_flush))
            try:
                cmd = self._queue.get(timeout=timeout)
                buffered.append(cmd)
            except queue.Empty:
                pass

            # 同一批里合并“可合并”的命令（时间戳同步、链接同步）
            if len(buffered) < self._batch and not self._should_flush(last_flush):
                continue

            pending, buffered = buffered, []
            last_flush = time.time()
            self._stats["batches"] += 1
            self._run_batch(pending)

        # 退出前清空剩余命令
        leftover = []
        while True:
            try:
                leftover.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if leftover:
            self._run_batch(leftover)

    def _should_flush(self, last_flush: float) -> bool:
        return (time.time() - last_flush) >= self._flush_ms

    def _run_batch(self, commands: list):
        """执行一批命令。

        只合并“同步类”命令（flush_timestamps / sync_links 合成一次 gather）；
        数据行命令逐条执行，保证每条的 ack 都被设置——
        此前只处理同步类，insert_memory 等其他命令的 done 事件永不触发，
        线程模式下 create_memory 会白等 30 秒。
        """
        sync_cmds = []
        for cmd in commands:
            if cmd.kind in ("flush_timestamps", "sync_links"):
                sync_cmds.append(cmd)
            else:
                self._execute(cmd)
        if sync_cmds:
            self._execute(_Command("sync_links"))
            for cmd in sync_cmds:
                cmd.result = True
                cmd.done.set()

    # ---------- 具体写入 ----------
    def _sync_all(self):
        """把主进程内存里的脏数据整理成行，交给写者落库。

        注意：脏集合（_dirty_memory_ids / _dirty_links / _deleted_links）只存在于
        主进程内存，独立写进程看不到。因此这里先在主进程 gather，再把“行”
        作为命令发出去——数据库写入仍然只有写者一个执行者。
        """
        from core.memory_engine import (
            _data_lock, _dirty_links, _deleted_links, _dirty_memory_ids,
            memories, links, DEFAULT_HALF_LIFE,
        )
        ts_rows, link_rows, del_rows = [], [], []
        with _data_lock:
            for mid in list(_dirty_memory_ids):
                mem = memories.get(mid)
                if not mem:
                    _dirty_memory_ids.discard(mid)
                    continue
                creation = mem.get("creation_time", mem.get("last_accessed", 0.0))
                ts_rows.append((
                    mem.get("last_accessed", 0.0),
                    mem.get("half_life", DEFAULT_HALF_LIFE),
                    mem.get("last_strengthen_time", creation),
                    mid,
                ))
            _dirty_memory_ids.clear()

            for key in list(_deleted_links):
                del_rows.append(key)
            _deleted_links.clear()

            for key in list(_dirty_links):
                data = links.get(key)
                if data is None:
                    continue
                src, tgt = key
                link_rows.append((
                    src, tgt, data["weight"], data["type"],
                    data.get("last_accessed", data.get("creation_time", 0)),
                    data.get("creation_time", 0),
                ))
            _dirty_links.clear()

        # 顺序：先删后写，避免同一主键两侧都命中时顺序相反
        ok = True
        ok = self._write_rows("delete_links", del_rows) and ok
        ok = self._write_rows("update_times", ts_rows) and ok
        ok = self._write_rows("upsert_links", link_rows) and ok
        if not ok:
            # 写入失败：把数据放回脏集合，下一次保存重试，绝不静默丢弃
            from core.memory_engine import _dirty_links, _deleted_links, _dirty_memory_ids
            for mid, *_rest in ts_rows:
                _dirty_memory_ids.add(mid)
            for key in del_rows:
                _deleted_links.add(key)
            for row in link_rows:
                _dirty_links.add((row[0], row[1]))
            append_log("[记忆写者] 本轮写入未全部成功，已回填脏标记待下次重试")
        self._stats["executed"] += 1
        PROBE.bump("writer.sync")

    def _write_rows(self, kind: str, rows: list) -> bool:
        """把一批行写入数据库：进程模式发给写进程，线程/legacy 模式本地执行。

        返回是否写入成功（供调用方决定是否回填脏标记重试）。
        如果当前就在写者线程里（例如 evict/purge 命令内部又触发落库），
        必须直接写而不能 re-submit：队列的唯一消费者就是本线程，
        等待自己的 ack 会一直等到超时。
        """
        if not rows:
            return True
        if self._thread is not None and threading.current_thread() is self._thread:
            payload_key = "ids" if kind == "delete_memories" else "rows"
            return self._execute(_Command(kind, {payload_key: rows})) is not False
        if self._mode_actual == "process":
            payload = {"ids": rows} if kind == "delete_memories" else {"rows": rows}
            result = self._submit_process(kind, payload, wait=True)
            return result is not None and not (isinstance(result, dict) and result.get("error"))
        payload_key = "ids" if kind == "delete_memories" else "rows"
        return self._execute(_Command(kind, {payload_key: rows})) is not False

    def _execute(self, cmd: _Command):
        try:
            if cmd.kind in ("flush_timestamps", "sync_links"):
                self._sync_all()
                cmd.result = True
            elif cmd.kind == "noop":
                cmd.result = True
            elif cmd.kind == "insert_memory":
                cmd.result = self._insert_memory(cmd.payload.get("rows") or [])
            elif cmd.kind == "update_times":
                cmd.result = self._update_times(cmd.payload.get("rows") or [])
            elif cmd.kind == "upsert_links":
                cmd.result = self._upsert_links(cmd.payload.get("rows") or [])
            elif cmd.kind == "delete_links":
                cmd.result = self._delete_links(cmd.payload.get("rows") or [])
            elif cmd.kind == "delete_memories":
                cmd.result = self._delete_memories(cmd.payload.get("ids") or [])
            elif cmd.kind == "evict_memories":
                from core.memory_engine import _evict_cold_memories
                _evict_cold_memories()
                cmd.result = True
            elif cmd.kind == "evict_links":
                from core.memory_engine import _evict_cold_links
                _evict_cold_links()
                cmd.result = True
            elif cmd.kind == "purge":
                from core.memory_engine import _purge_expired_memories
                cmd.result = _purge_expired_memories()
            elif cmd.kind == "custom":
                fn = cmd.payload.get("fn")
                cmd.result = fn() if callable(fn) else None
            else:
                cmd.result = False
            if cmd.kind != "noop":
                self._stats["executed"] += 1
        except Exception as e:
            self._stats["errors"] += 1
            append_log(f"[记忆写者] 命令 {cmd.kind} 失败: {e}")
            cmd.result = False
        finally:
            cmd.done.set()
        return cmd.result

    # ---------- 线程/legacy 模式的直接写入 ----------
    @staticmethod
    def _db():
        from core.memory_engine import _get_db
        return _get_db()

    def _insert_memory(self, rows):
        if not rows:
            return {"written": 0}
        db = self._db()
        db.executemany(
            """INSERT OR REPLACE INTO memories
               (id, content, vector, half_life, last_accessed, creation_time,
                last_strengthen_time, concept_tag_ids)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        db.commit()
        return {"written": len(rows)}

    def _update_times(self, rows):
        if not rows:
            return {"written": 0}
        db = self._db()
        db.executemany(
            "UPDATE memories SET last_accessed=?, half_life=?, last_strengthen_time=? WHERE id=?",
            rows,
        )
        db.commit()
        return {"written": len(rows)}

    def _upsert_links(self, rows):
        if not rows:
            return {"written": 0}
        db = self._db()
        db.executemany(
            """INSERT OR REPLACE INTO links
               (src, tgt, weight, type, last_accessed, creation_time)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )
        db.commit()
        return {"written": len(rows)}

    def _delete_links(self, rows):
        if not rows:
            return {"deleted": 0}
        db = self._db()
        db.executemany("DELETE FROM links WHERE src = ? AND tgt = ?", rows)
        db.commit()
        return {"deleted": len(rows)}

    def _delete_memories(self, ids):
        if not ids:
            return {"deleted": 0}
        db = self._db()
        placeholders = ",".join("?" * len(ids))
        db.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
        db.execute(
            f"DELETE FROM links WHERE src IN ({placeholders}) OR tgt IN ({placeholders})",
            ids + ids,
        )
        db.commit()
        return {"deleted": len(ids)}


def _planner_mode() -> str:
    try:
        from config.api_config import config
        mode = str(config.get("hot_planner_mode", "process") or "process").strip().lower()
    except Exception:
        mode = "process"
    return mode if mode in ("process", "thread") else "process"


class HotSetPlanner:
    """热集规划器：在独立进程（或线程）里算淘汰名单，主进程 CAS 应用。

    规划只依赖紧凑快照 (id, last_accessed, last_strengthen_time)，不传向量，
    所以跨进程很轻。主进程应用时会重新核对这两个字段：
    期间被访问过的条目自动跳过，不会误淘汰。

    “另一条进程整理后替换回主进程”的落地方式：
      进程算出 evict 名单 → 主进程校验字段未变 → 原子地从热集移除并落库。
    这样既保证了整理发生在独立进程，又不需要跨进程序列化整个热字典。
    """

    def __init__(self, max_hot_getter=None, interval: float = 60.0):
        self._max_hot_getter = max_hot_getter or (lambda: 1500)
        self._interval = max(5.0, float(interval))
        self._thread = None
        self._stop = threading.Event()
        self._plan = []
        self._plan_lock = threading.Lock()
        self._snapshot_version = 0
        self.last_applied = 0
        self._process = None
        self._in_queue = None
        self._out_queue = None
        self._rid = 0
        self._mode_actual = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        mode = _planner_mode()
        if mode == "process":
            try:
                self._start_process()
                self._mode_actual = "process"
                append_log("[热集规划] 已启动整理进程")
            except Exception as e:
                append_log(f"[热集规划] 整理进程启动失败，改用线程: {e}")
                self._mode_actual = "thread"
        else:
            self._mode_actual = "thread"
        self._thread = threading.Thread(target=self._run, name="HotSetPlanner", daemon=True)
        self._thread.start()

    def _start_process(self):
        from core.memory_writer_proc import planner_main
        ctx = multiprocessing.get_context("spawn")
        self._in_queue = ctx.Queue(maxsize=8)
        self._out_queue = ctx.Queue()
        self._process = ctx.Process(
            target=planner_main,
            args=(self._in_queue, self._out_queue),
            name="HotSetPlannerProc",
            daemon=True,
        )
        self._process.start()

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        if self._process is not None:
            try:
                self._in_queue.put(None)
            except Exception:
                pass
            self._process.join(timeout=timeout)
            if self._process.is_alive():
                self._process.terminate()
            self._process = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    @property
    def mode(self) -> str:
        return self._mode_actual or _planner_mode()

    def _run(self):
        while not self._stop.wait(self._interval):
            try:
                plan = self.compute_plan()
                with self._plan_lock:
                    self._plan = plan
                if plan:
                    PROBE.gauge("hot.planned_evict", len(plan))
            except Exception as e:
                append_log(f"[热集规划] 计算失败: {e}")

    def compute_plan(self) -> list:
        """按最久未访问优先，返回需要下沉的 (id, last_accessed, last_strengthen_time)。"""
        from core.memory_engine import _data_lock, hot_ids, memories
        max_hot = int(self._max_hot_getter())
        with _data_lock:
            if len(hot_ids) <= max_hot:
                return []
            compact = [
                (mid, memories[mid].get("last_accessed", 0.0),
                 memories[mid].get("last_strengthen_time", 0.0))
                for mid in hot_ids if mid in memories
            ]
        self._snapshot_version += 1

        if self._mode_actual == "process" and self._process is not None \
                and self._process.is_alive():
            result = self._compute_in_process(compact, max_hot)
            if result is not None:
                return result
            PROBE.bump("hot.planner_fallback")
            append_log("[热集规划] 进程未返回结果，本次改为主进程内计算")
        compact.sort(key=lambda item: item[1])
        return compact[:max(0, len(compact) - max_hot)]

    def _compute_in_process(self, compact, max_hot):
        """把紧凑快照发给整理进程，等待名单；超时/异常返回 None 由调用方回退。"""
        self._rid += 1
        rid = self._rid
        try:
            self._in_queue.put((rid, compact, max_hot), timeout=2.0)
        except queue.Full:
            return None
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                got_rid, result = self._out_queue.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                return None
            if got_rid == rid:
                return list(result or [])
        return None

    def take_plan(self) -> list:
        with self._plan_lock:
            plan, self._plan = self._plan, []
        return plan

    def apply_plan(self, plan: list) -> int:
        """CAS 应用淘汰计划。字段在期间发生变化（被访问/被强化）的条目跳过。"""
        if not plan:
            return 0
        from core.memory_engine import _data_lock, hot_ids, memories
        applied = []
        with _data_lock:
            for mid, last_accessed, last_strengthen in plan:
                mem = memories.get(mid)
                if not mem:
                    hot_ids.discard(mid)
                    continue
                if mem.get("last_accessed", 0.0) != last_accessed:
                    continue
                if mem.get("last_strengthen_time", 0.0) != last_strengthen:
                    continue
                applied.append((
                    mem.get("last_accessed", 0.0),
                    mem.get("half_life", 0.0),
                    mem.get("last_strengthen_time", 0.0),
                    mid,
                ))
                del memories[mid]
                hot_ids.discard(mid)
        if not applied:
            return 0
        # DB 写交给单写者（避免主进程与写者同时写同一个库）。
        # _write_rows 内部会判断“当前是否就在写者线程”，是则直接写、不再入队。
        try:
            WRITER._write_rows("update_times", applied)
        except Exception as e:
            append_log(f"[热集规划] 落库失败，回填脏标记: {e}")
            from core.memory_engine import _dirty_memory_ids
            for _la, _hl, _ls, mid in applied:
                _dirty_memory_ids.add(mid)
        self.last_applied = len(applied)
        PROBE.bump("hot.planner_applied", len(applied))
        return len(applied)


# 模块级单例（由 persistence / qq_bot 在启动时 start）
WRITER = MemoryWriter()
PLANNER = HotSetPlanner()


def start_default_workers():
    WRITER.start()
    PLANNER.start()


def stop_default_workers():
    PLANNER.stop()
    WRITER.stop()
