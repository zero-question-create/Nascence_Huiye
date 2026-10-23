# core/memory_writer_proc.py
# ========================================================================
# 独立原子写进程：唯一持有 memory.db 写连接的进程。
#
# 为什么必须是独立进程（而不是仅仅一个线程）：
#   - SQLite 写入 + 批量事务 + 清理期间的磁盘抖动不会占用主进程的 GIL；
#   - 主进程被 kill -9 时，写进程仍在事务边界内提交或回滚，不会留下半截数据；
#   - 与概念层、外部工具之间通过 SQLite 自身的 WAL + busy_timeout 协调。
#
# 约束：
#   - 本模块只依赖标准库，不 import core.memory_engine / faiss，
#     避免子进程重复初始化向量索引；
#   - 所有命令必须可通过 pickle 传递；向量以 bytes 形式传入；
#   - 每条命令返回 (rid, result)，主进程按 rid 等待 ack。
# ========================================================================

import json
import os
import sqlite3
import time


def _ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            vector BLOB NOT NULL,
            half_life REAL NOT NULL,
            last_accessed REAL NOT NULL,
            creation_time REAL NOT NULL,
            last_strengthen_time REAL NOT NULL,
            concept_tag_ids TEXT DEFAULT '[]'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS links (
            src TEXT NOT NULL,
            tgt TEXT NOT NULL,
            weight REAL NOT NULL,
            type TEXT NOT NULL,
            last_accessed REAL NOT NULL,
            creation_time REAL NOT NULL,
            PRIMARY KEY (src, tgt)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_links_tgt ON links(tgt)")
    conn.commit()


def _handle(conn, kind, payload):
    if kind == "insert_memory":
        rows = payload.get("rows") or []
        conn.executemany(
            """INSERT OR REPLACE INTO memories
               (id, content, vector, half_life, last_accessed, creation_time,
                last_strengthen_time, concept_tag_ids)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return {"written": len(rows)}

    if kind == "update_times":
        rows = payload.get("rows") or []
        if rows:
            conn.executemany(
                "UPDATE memories SET last_accessed=?, half_life=?, last_strengthen_time=? WHERE id=?",
                rows,
            )
            conn.commit()
        return {"written": len(rows)}

    if kind == "upsert_links":
        rows = payload.get("rows") or []
        if rows:
            conn.executemany(
                """INSERT OR REPLACE INTO links
                   (src, tgt, weight, type, last_accessed, creation_time)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )
            conn.commit()
        return {"written": len(rows)}

    if kind == "delete_links":
        rows = payload.get("rows") or []
        if rows:
            conn.executemany("DELETE FROM links WHERE src = ? AND tgt = ?", rows)
            conn.commit()
        return {"deleted": len(rows)}

    if kind == "delete_memories":
        ids = payload.get("ids") or []
        if ids:
            placeholders = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
            conn.execute(
                f"DELETE FROM links WHERE src IN ({placeholders}) OR tgt IN ({placeholders})",
                ids + ids,
            )
            conn.commit()
        return {"deleted": len(ids)}

    if kind == "purge":
        now = float(payload.get("now") or time.time())
        threshold = float(payload.get("threshold", 0.001))
        rows = conn.execute("SELECT id, last_accessed, half_life FROM memories").fetchall()
        expired = []
        for mem_id, last_accessed, half_life in rows:
            delta = max(0.0, now - (last_accessed or 0.0))
            if half_life and half_life > 0:
                decay = 2 ** (-delta / half_life)
                if decay < threshold:
                    expired.append(mem_id)
        if expired:
            placeholders = ",".join("?" * len(expired))
            conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", expired)
            conn.execute(
                f"DELETE FROM links WHERE src IN ({placeholders}) OR tgt IN ({placeholders})",
                expired + expired,
            )
            conn.commit()
        return {"expired": expired}

    if kind == "prune_links":
        now = float(payload.get("now") or time.time())
        half_life = float(payload.get("half_life", 7 * 24 * 3600))
        min_weight = float(payload.get("min_weight", 0.01))
        rows = conn.execute(
            "SELECT src, tgt, weight, last_accessed, creation_time FROM links"
        ).fetchall()
        dead = []
        for src, tgt, weight, last_accessed, creation_time in rows:
            delta = max(0.0, now - (last_accessed or creation_time or now))
            if weight * (2 ** (-delta / half_life)) < min_weight:
                dead.append((src, tgt))
        if dead:
            conn.executemany("DELETE FROM links WHERE src = ? AND tgt = ?", dead)
            conn.commit()
        return {"removed": len(dead)}

    if kind == "snapshot_times":
        rows = conn.execute(
            "SELECT id, last_accessed, half_life, last_strengthen_time FROM memories"
        ).fetchall()
        return {"rows": rows}

    if kind == "noop":
        return {"ok": True}

    return {"error": f"unknown command: {kind}"}


def planner_main(in_queue, out_queue):
    """热集整理进程：接收紧凑快照，返回需要下沉的条目。

    输入 (rid, [(id, last_accessed, last_strengthen_time), ...], max_hot)
    输出 (rid, [(id, last_accessed, last_strengthen_time), ...])

    只做排序与截断，不碰数据库、不碰向量，所以进程崩溃不会影响数据；
    主进程拿到名单后会做 CAS 校验，期间被访问过的条目自动跳过。
    """
    while True:
        item = in_queue.get()
        if item is None:
            break
        rid, compact, max_hot = item
        try:
            compact = list(compact or [])
            compact.sort(key=lambda row: row[1])
            overflow = max(0, len(compact) - int(max_hot))
            result = compact[:overflow]
        except Exception:
            result = []
        out_queue.put((rid, result))


def writer_main(db_path, cmd_queue, res_queue, ready_event=None):
    """写进程主循环。由 MemoryWriter 以 multiprocessing.Process 启动。"""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_schema(conn)
    if ready_event is not None:
        ready_event.set()

    while True:
        cmd = cmd_queue.get()
        if cmd is None:
            break
        rid = cmd.get("rid")
        kind = cmd.get("kind")
        payload = cmd.get("payload") or {}
        try:
            result = _handle(conn, kind, payload)
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            result = {"error": str(e)}
        res_queue.put((rid, result))

    try:
        conn.close()
    except Exception:
        pass
