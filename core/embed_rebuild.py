# core/embed_rebuild.py
# ========================================================================
# 语义向量维度重建工具
#
# 用途：
#   切换 embedding 模型（或模型输出维度变化）后，SQLite 里已存记忆向量与
#   faiss 索引的维度可能不匹配（例如旧库 768 维、新模型输出 1024 维），
#   会导致检索/建索引崩溃。本工具把全量记忆向量按目标维度重算并重建索引。
#
# 对外接口：
#   - get_stored_dim()         查询 SQLite 中现有向量维度
#   - get_model_dim()          探测当前 embed 模型实际输出维度（已就绪时）
#   - get_memory_count()       记忆总数
#   - rebuild_status()         返回当前重建任务的进度快照
#   - start_rebuild(target_dim) 启动后台重建（target_dim 为空则用模型实际维度）
#
# 使用方：
#   - Web 面板「维护」页（webui.py 的 /api/maintenance/embed_* 接口）
#   - 也可独立命令行调用：python -c "from core.embed_rebuild import start_rebuild; start_rebuild()"
# ========================================================================

import threading
import time

import numpy as np

from config.api_config import config, save_config


_rebuild_lock = threading.Lock()
_rebuild_state = {
    "running": False,
    "done": 0,
    "total": 0,
    "stored_dim": None,
    "model_dim": None,
    "target_dim": None,
    "error": None,
    "log": [],
}


def _log(line: str):
    with _rebuild_lock:
        _rebuild_state["log"].append(str(line))
        if len(_rebuild_state["log"]) > 500:
            del _rebuild_state["log"][:-500]


def get_stored_dim():
    """读取 SQLite 中任一记忆向量的维度；库为空返回 None。"""
    try:
        from core.memory_engine import _get_db
        db = _get_db()
        row = db.execute("SELECT vector FROM memories LIMIT 1").fetchone()
        if row is None or not row[0]:
            return None
        return int(len(np.frombuffer(row[0], dtype=np.float32)))
    except Exception:
        return None


def get_memory_count() -> int:
    try:
        from core.memory_engine import _get_db
        db = _get_db()
        return int(db.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    except Exception:
        return 0


def get_model_dim():
    """探测当前 embed 模型实际输出维度。

    仅当本地/外部 embed 能力可用时探测（会触发本地模型启动，耗时可观），
    失败返回 None。
    """
    try:
        from core.memory_engine import text_to_vector
        return int(len(_probe_retry(text_to_vector)))
    except Exception:
        return None


def _probe_retry(text_to_vector, attempts: int = 10, interval: float = 5.0) -> list:
    """探测向量：llama-server 端口就绪但模型可能仍在加载（返回 503 Loading model），
    这里对 503 做重试，避免误判重建失败。"""
    last_exc = None
    for i in range(attempts):
        try:
            return list(text_to_vector("维度探测"))
        except Exception as e:
            last_exc = e
            msg = str(e)
            if "503" in msg and "Loading model" in msg:
                _log(f"模型仍在加载中，等待 {interval}s 后重试（{i + 1}/{attempts}）…")
                time.sleep(interval)
                continue
            raise
    raise last_exc


def _embed_retry(text_to_vector, content: str, attempts: int = 3, interval: float = 2.0) -> list:
    """单条记忆向量化：对模型暂未就绪（503 Loading model）做少量重试。"""
    last_exc = None
    for i in range(attempts):
        try:
            return list(text_to_vector(content))
        except Exception as e:
            last_exc = e
            msg = str(e)
            if "503" in msg and "Loading model" in msg:
                time.sleep(interval)
                continue
            raise
    raise last_exc


def rebuild_status() -> dict:
    """返回重建任务当前进度快照（线程安全）。"""
    with _rebuild_lock:
        snap = dict(_rebuild_state)
        snap["log"] = list(_rebuild_state["log"])
        snap["stored_dim"] = get_stored_dim()
        snap["memory_count"] = get_memory_count()
        return snap


def start_rebuild(target_dim=None) -> dict:
    """启动后台全量重建。

    target_dim:
      - 传整数：尽量按此维度重算（若与模型实际输出维度不符会被强制修正并提示）
      - 为空：使用模型实际输出维度
    返回 {ok, message}；重复调用时若已有任务运行会拒绝。
    """
    with _rebuild_lock:
        if _rebuild_state["running"]:
            return {"ok": False, "message": "已有重建任务进行中，请稍后再试。"}
        _rebuild_state.update(
            running=True, done=0, total=0, model_dim=None,
            target_dim=target_dim, error=None, log=[],
        )
    threading.Thread(target=_run_rebuild, args=(target_dim,), daemon=True,
                     name="embed-rebuild").start()
    return {"ok": True, "message": "重建已开始，可在「维护」页查看进度。"}


# ------------------------------------------------------------------------
# 后台执行
# ------------------------------------------------------------------------
def _run_rebuild(target_dim):
    try:
        from core import memory_engine as ME
        from core.memory_engine import _get_db, _init_faiss_index, _rebuild_faiss_index, text_to_vector

        # 1. 确保 embed 后端就绪（本地默认模型未加载则尝试拉起）
        from core.model_backend import BACKENDS
        backend = BACKENDS["embed"]
        if backend.use_default() and not backend.is_local_ready():
            _log("正在启动本地 embedding 模型…")
            if not backend.start_local(wait=True):
                raise RuntimeError("本地 embedding 模型启动失败，请检查模型文件或改用外部 API。")
        elif not backend.use_default() and not (backend.api_base_url() and backend.api_model()):
            raise RuntimeError("embed 后端既未配置本地模型，也未填写外部 API，无法生成向量。")

        # 2. 探测模型实际输出维度
        _log("正在探测模型输出维度…")
        probe = _probe_retry(text_to_vector)
        model_dim = int(len(probe))
        with _rebuild_lock:
            _rebuild_state["model_dim"] = model_dim

        # 3. 确定目标维度（模型输出维度是硬约束）
        if target_dim is None:
            target_dim = model_dim
        target_dim = int(target_dim)
        if target_dim != model_dim:
            _log(f"提示：目标维度 {target_dim} 与模型实际输出 {model_dim} 不符，将按 {model_dim} 重建。")
            target_dim = model_dim
        with _rebuild_lock:
            _rebuild_state["target_dim"] = target_dim

        # 4. 全量读取记忆并重算向量
        db = _get_db()
        rows = db.execute("SELECT id, content FROM memories").fetchall()
        total = len(rows)
        with _rebuild_lock:
            _rebuild_state["total"] = total
        _log(f"开始全量重建：{total} 条记忆 → {target_dim} 维")

        for i, (mem_id, content) in enumerate(rows):
            vec = _embed_retry(text_to_vector, content or "")
            blob = np.array(vec, dtype=np.float32).tobytes()
            db.execute("UPDATE memories SET vector=? WHERE id=?", (blob, mem_id))
            if (i + 1) % 50 == 0:
                db.commit()
            with _rebuild_lock:
                _rebuild_state["done"] = i + 1
            if (i + 1) % 100 == 0:
                _log(f"进度 {i + 1}/{total}")
        db.commit()
        _log("所有记忆向量已重算，正在重建 faiss 索引…")

        # 5. 更新配置与内存中的维度常量
        config["embed_dim"] = target_dim
        save_config(config)
        ME.EMBED_DIM = target_dim

        # 6. 重建 faiss（从 SQLite 全量重建，维度已取 config.embed_dim）
        _init_faiss_index()
        _rebuild_faiss_index()

        # 7. 刷新热记忆内存向量（与 SQLite 保持一致，避免新旧维度混用）
        from core.memory_engine import hot_ids, memories
        for mid in list(hot_ids):
            row = db.execute("SELECT vector FROM memories WHERE id=?", (mid,)).fetchone()
            if row and row[0] and mid in memories:
                memories[mid]["vector"] = np.frombuffer(row[0], dtype=np.float32).tolist()

        _log(f"重建完成：{total} 条记忆，维度 {target_dim}。配置已更新，建议重启进程后使用。")
    except Exception as e:
        import traceback
        _log(f"重建失败：{e}")
        _log(traceback.format_exc())
        with _rebuild_lock:
            _rebuild_state["error"] = str(e)
    finally:
        with _rebuild_lock:
            _rebuild_state["running"] = False
