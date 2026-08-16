# utils/persistence.py
import json
import os
import time
from core.memory_engine import memories, links, pending_deletion, wordweb, hot_ids
from core.memory_engine import check_and_handle_expired, _evict_cold_memories
from core.memory_engine import _data_lock as _io_lock  # 与 memory_engine 共用同一把锁
from core.virtual_clock import clock
from core import runtime_paths

MEMORY_FILE = "data/test/memory.json"
STATE_FILE = "data/test/dialogue_state.json"
DIALOGUE_LOG_FILE = "data/test/dialogue_log.jsonl"
FAISS_INDEX_FILE = "data/test/faiss.index"
FAISS_MAPPING_FILE = "data/test/faiss_mapping.json"
METRICS_COUNTERS_FILE = "data/test/metrics_counters.json"


def _refresh_paths():
    """按当前运行时数据目录重算路径常量。"""
    global MEMORY_FILE, STATE_FILE, DIALOGUE_LOG_FILE
    global FAISS_INDEX_FILE, FAISS_MAPPING_FILE, METRICS_COUNTERS_FILE
    MEMORY_FILE = runtime_paths.file("memory.json")
    STATE_FILE = runtime_paths.file("dialogue_state.json")
    DIALOGUE_LOG_FILE = runtime_paths.file("dialogue_log.jsonl")
    FAISS_INDEX_FILE = runtime_paths.file("faiss.index")
    FAISS_MAPPING_FILE = runtime_paths.file("faiss_mapping.json")
    METRICS_COUNTERS_FILE = runtime_paths.file("metrics_counters.json")


runtime_paths.register(_refresh_paths)

# ========== 记忆持久化 ==========

def save_all_data():
    """保存记忆和链接到文件（原子写入，避免截断）"""
    with _io_lock:
        from core.memory_engine import _get_db, hot_ids, memories
        db = _get_db()
        hot_ids_snapshot = list(hot_ids)  # 快照拷贝：杜绝遍历中被并发修改
        for mid in hot_ids_snapshot:
            mem = memories.get(mid)
            if mem:
                db.execute(
                    "UPDATE memories SET last_accessed=?, half_life=?, last_strengthen_time=? WHERE id=?",
                    (mem["last_accessed"], mem["half_life"], mem.get("last_strengthen_time", mem["creation_time"]), mid)
                )
        db.commit()

        os.makedirs(runtime_paths.get_data_dir(), exist_ok=True)
        serializable_sentence_links = {f"{src}||{tgt}": val for (src, tgt), val in links.items()}
        serializable_word_links = {f"{a}||{b}": val for (a, b), val in wordweb.items()}
        data = {"memories": {mid: memories[mid] for mid in hot_ids_snapshot}, "links": serializable_sentence_links, "wordweb": serializable_word_links}
        tmp_file = MEMORY_FILE + ".tmp"
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, MEMORY_FILE)

        # 热链接下沉到 SQLite + 清理孤儿（全量对齐，冷链接保留）
        from core.memory_engine import _sync_all_links_to_sqlite
        _sync_all_links_to_sqlite()

        clock.save_state()

        from core.memory_engine import _faiss_index, _faiss_to_mem
        if _faiss_index is not None:
            import faiss
            faiss.write_index(_faiss_index, FAISS_INDEX_FILE)
            with open(FAISS_MAPPING_FILE, 'w', encoding='utf-8') as f:
                json.dump(_faiss_to_mem, f, ensure_ascii=False)

        # 保存每日指标计数器快照
        from core.memory_engine import _export_metrics_counters
        metrics = _export_metrics_counters()
        with open(METRICS_COUNTERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(metrics, f, ensure_ascii=False)

def load_all_data():
    """从持久化加载记忆和链接。

    冷热分离设计下，SQLite 保存全量记忆，memory.json 仅为热快照。
    启动加载策略：
      1. 若 memory.json 有热记忆快照，优先用它恢复热区（兼容旧版）
      2. 从 SQLite 加载最近活跃的 MAX_HOT_SIZE 条记忆到热区
         （覆盖 memory.json 为空/旧的情况，保证热区有数据）
      3. faiss 索引始终从 SQLite 全量重建，保证检索覆盖所有记忆
    """
    global memories, links
    with _io_lock:
        from core.memory_engine import _get_db, hot_ids
        db = _get_db()

        # 1. 若 memory.json 存在，读取热快照（可能有旧数据）
        snap = {}
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
                    snap = json.load(f)
            except (json.JSONDecodeError, OSError):
                snap = {}

        # 2. 从 SQLite 查询最近活跃的记忆 ID（按 last_accessed 降序取 MAX_HOT_SIZE 条）
        hot_limit = _get_hot_limit()
        rows = db.execute(
            "SELECT id FROM memories ORDER BY last_accessed DESC LIMIT ?",
            (hot_limit,)
        ).fetchall()
        recent_ids = [r[0] for r in rows]

        memories.clear()
        hot_ids.clear()
        links.clear()          # 先清空旧链接，再通过记忆加载重新填充
        wordweb.clear()

        # 先恢复 memory.json 里的热记忆（若有）
        from core.memory_engine import _load_memory_from_db
        for mem_id in list(snap.get("memories", {}).keys()):
            if mem_id in recent_ids or mem_id not in [r[0] for r in rows]:
                loaded = _load_memory_from_db(mem_id)
                if loaded is None:
                    # 快照里的记忆已不在 SQLite，用快照数据兜底
                    mem = snap["memories"][mem_id]
                    memories[mem_id] = mem
                    hot_ids.add(mem_id)
        # 再从 SQLite 补齐最近活跃的记忆（确保热区达到上限）
        from core.memory_engine import _batch_load_memories
        need = [mid for mid in recent_ids if mid not in hot_ids]
        _batch_load_memories(need[:hot_limit - len(hot_ids)])

        # 链接与词网：memory.json 快照中的链接补充（batch 加载已拉取 SQLite 链接）
        for key_str, val in snap.get("links", {}).items():
            src, tgt = key_str.split("||")
            if (src, tgt) not in links:
                links[(src, tgt)] = val
        for key_str, val in snap.get("wordweb", {}).items():
            a, b = key_str.split("||")
            wordweb[(a, b)] = val

        from core.memory_engine import _build_word_to_memories, _rebuild_wordweb
        _build_word_to_memories()
        # 重建词网（共现网络）：memory.json 快照为空时，从热记忆内容重建，
        # 保证词网规模不为 0、字词共现联想可用
        _rebuild_wordweb()

        # 3. faiss 索引始终从 SQLite 全量重建（保证检索覆盖全部记忆）
        from core.memory_engine import _rebuild_faiss_index
        _rebuild_faiss_index()

        if os.path.exists(METRICS_COUNTERS_FILE):
            with open(METRICS_COUNTERS_FILE, 'r', encoding='utf-8') as f:
                metrics = json.load(f)
            from core.memory_engine import _import_metrics_counters
            _import_metrics_counters(metrics)

        # 限制热记忆数量到硬上限（MAX_HOT_SIZE），把最久未访问的记忆移出内存
        from core.memory_engine import _evict_cold_memories, _evict_cold_links
        _evict_cold_memories()
        _evict_cold_links()

        print(f"[持久化] 加载完成：热记忆 {len(hot_ids)} 条，faiss 全量 {_get_faiss_count()} 条")


def _get_hot_limit():
    """获取热记忆上限（MAX_HOT_SIZE）。"""
    from core.memory_engine import MAX_HOT_SIZE
    return MAX_HOT_SIZE


def _get_faiss_count():
    """返回当前 faiss 索引向量数（用于日志）。"""
    try:
        from core.memory_engine import _faiss_index
        return _faiss_index.ntotal if _faiss_index is not None else 0
    except Exception:
        return 0

# ========== 对话状态持久化 ==========

def save_state():
    """保存对话状态到 JSON 文件"""
    with _io_lock:
        os.makedirs(runtime_paths.get_data_dir(), exist_ok=True)
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            from utils.dialogue_state import current_state
            json.dump(current_state, f, ensure_ascii=False, indent=2)

def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
        # 标准化键名，将可能的变体统一到标准键
        normalized = {}
        for key, value in loaded.items():
            if key == "我的已知信息" or key == "辉夜的已知信息" or key == "“我”的已知信息" or key == "你的已知信息":
                normalized["我的已知信息"] = value
            elif key in ("参与者", "最近话题"):
                normalized[key] = value
        # 更新状态，不清空，防止空覆盖
        if normalized:
            from utils.dialogue_state import current_state
            current_state.update(normalized)
        print(f"[状态加载] 加载内容: {normalized}")
    except Exception as e:
        print(f"[状态加载] 加载失败: {e}")

def append_dialogue(user_input: str, bot_reply: str):
    """追加一轮对话到日志文件（不读入内存）"""
    record = {
        "time": time.time(),          # 真实时间戳
        "virtual_time": clock.now(),  # 虚拟时间戳
        "user": user_input,
        "bot": bot_reply
    }
    with _io_lock:
        os.makedirs(runtime_paths.get_data_dir(), exist_ok=True)
        with open(DIALOGUE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

# ========== 睡眠清理 ==========
def sleep_cleanup():
    """
    睡眠巩固阶段的全量维护（当前版本已恢复物理删除）：
    1. 剪枝权重过低的链接
    2. 整理字词网络（衰减 + 剪枝）
    3. 物理删除衰变到极致的记忆
    4. 全量持久化（含重建 faiss 索引）
    """
    now = clock.now()
    removed_links = 0
    removed_wordweb = 0

    # 1. 链接剪枝（内存热链接）
    dead_links = []
    for key, data in links.items():
        delta = now - data.get("last_accessed", data.get("creation_time", now))
        decay = 2 ** (-delta / (7 * 24 * 3600))  # LINK_HALF_LIFE
        if data["weight"] * decay < 0.01:
            dead_links.append(key)
    for key in dead_links:
        del links[key]
        removed_links += 1
        from core.memory_engine import _bump_link_deleted
        _bump_link_deleted()
    # 剪枝 SQLite 中已下沉的冷链接（不在内存中的）
    from core.memory_engine import _get_db
    _cold_db = _get_db()
    _cold_rows = _cold_db.execute(
        "SELECT src, tgt, weight, last_accessed, creation_time FROM links"
    ).fetchall()
    _cold_dead = []
    for src, tgt, weight, last_accessed, creation_time in _cold_rows:
        if (src, tgt) in links:
            continue  # 热链接跳过，交给内存管理
        _delta = now - (last_accessed or creation_time or now)
        if weight * (2 ** (-_delta / (7 * 24 * 3600))) < 0.01:
            _cold_dead.append((src, tgt))
    for src, tgt in _cold_dead:
        _cold_db.execute("DELETE FROM links WHERE src = ? AND tgt = ?", (src, tgt))
        removed_links += 1
        from core.memory_engine import _bump_link_deleted
        _bump_link_deleted()
    _cold_db.commit()

    # 2. 字词网络整理
    dead_words = []
    for (a, b), wdata in wordweb.items():
        delta = now - wdata.get("last_updated", now)
        decay = 2 ** (-delta / (30 * 24 * 3600))
        if wdata["forward_count"] * decay < 0.5:
            dead_words.append((a, b))
    for key in dead_words:
        del wordweb[key]
        removed_wordweb += 1

    # 3. 物理删除衰变记忆，并重建 faiss 索引
    from core.memory_engine import _purge_expired_memories, _rebuild_faiss_index
    expired = _purge_expired_memories()
    if expired:
        _rebuild_faiss_index()          # faiss 全量重建（从 SQLite 剩余记忆重新构建）
        print(f"[睡眠维护] 物理删除 {len(expired)} 条衰变记忆，faiss 索引已重建")

    # 3.5 冷记忆淘汰：将超出热记忆上限的最久未访问记忆移出内存（写入 SQLite）
    from core.memory_engine import _evict_cold_memories, _evict_cold_links
    _evict_cold_memories()
    _evict_cold_links()

    # 4. 全量持久化（含热数据写入 JSON、faiss 索引、时钟状态）
    save_all_data()
    print(f"[睡眠维护] 链接剪枝 {removed_links}，字词清理 {removed_wordweb}，物理删除 {len(expired)} 条记忆")

    from core.memory_engine import _write_daily_metrics
    _write_daily_metrics()

# ========== 临时数据迁移（将json导入SQLite） ==========
def migrate_json_to_sqlite():
    """将旧的 memory.json 中的所有记忆导入 SQLite，然后删除 JSON 文件"""
    if not os.path.exists(MEMORY_FILE):
        return
    from core.memory_engine import _get_db, _faiss_index, _faiss_to_mem, _mem_to_faiss
    import faiss, numpy as np
    with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    db = _get_db()
    for mem_id, mem in data.get("memories", {}).items():
        # 插入 SQLite
        db.execute(
            """INSERT OR REPLACE INTO memories (id, content, vector, half_life, last_accessed,
               creation_time, last_strengthen_time, concept_tag_ids)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (mem_id, mem["content"], np.array(mem["vector"], dtype=np.float32).tobytes(),
             mem["half_life"], mem["last_accessed"], mem["creation_time"],
             mem.get("last_strengthen_time", mem["creation_time"]),
             json.dumps(mem.get("concept_tag_ids", [])))
        )
    # 迁移链接到 SQLite links 表
    for key_str, val in data.get("links", {}).items():
        src, tgt = key_str.split("||")
        db.execute(
            """INSERT OR REPLACE INTO links (src, tgt, weight, type, last_accessed, creation_time)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (src, tgt, val.get("weight", 0.0), val.get("type", "semantic"),
             val.get("last_accessed", val.get("creation_time", 0)),
             val.get("creation_time", 0))
        )
    db.commit()
    # 重建 faiss 索引（确保与 SQLite 一致）
    from core.memory_engine import _rebuild_faiss_index
    _rebuild_faiss_index()
    # 删除旧 JSON 文件
    os.remove(MEMORY_FILE)
    print("[迁移] JSON 数据已全部迁移至 SQLite，faiss 索引已重建。")