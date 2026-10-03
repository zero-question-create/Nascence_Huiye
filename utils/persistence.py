# utils/persistence.py
import json
import os
import threading
import time
from core.memory_engine import memories, links, pending_deletion, wordweb, hot_ids
from core.memory_engine import check_and_handle_expired, _evict_cold_memories
from core.virtual_clock import clock

_io_lock = threading.Lock()

# 保存节流：关停路径上 QQ 服务与面板会各保存一次，短时间内的重复调用只是重做同样的落盘。
# 小于该间隔的重复保存直接跳过；关停时用 save_all_data(force=True) 保证最后一次一定落盘。
_SAVE_MIN_INTERVAL = 3.0
_last_save_time = 0.0
_save_count = 0                # 统计实际执行的保存次数（供测试与观测）

MEMORY_FILE = "data/test/memory.json"
STATE_FILE = "data/test/dialogue_state.json"
DIALOGUE_LOG_FILE = "data/test/dialogue_log.jsonl"
FAISS_INDEX_FILE = "data/test/faiss.index"
FAISS_MAPPING_FILE = "data/test/faiss_mapping.json"
METRICS_COUNTERS_FILE = "data/test/metrics_counters.json"

# ========== 记忆持久化 ==========

def save_all_data(force: bool = False) -> bool:
    """保存记忆和链接到文件（原子写入，双锁保护避免并发下沉冲突）。

    force=False 时做节流：距上次保存不足 _SAVE_MIN_INTERVAL 秒则跳过，
    避免关停等路径上重复执行同一份落盘。

    返回是否真正执行并成功完成保存。

    锁顺序约定：**先 _io_lock，后 _data_lock**（全局唯一顺序，见 sleep_cleanup 注释）。
    """
    global _last_save_time, _save_count
    with _io_lock:
        now = time.time()
        if not force and (now - _last_save_time) < _SAVE_MIN_INTERVAL:
            return False
        # 注意：节流时间与成功计数只在**保存真正成功之后**才推进，
        # 否则一次磁盘故障会让紧随其后的重试被误判为"刚保存过"而跳过。
        _do_save_all_data()
        _last_save_time = time.time()
        _save_count += 1
        return True


def _do_save_all_data():
    """实际执行保存（调用方需持有 _io_lock）。"""
    from core.memory_engine import _data_lock, _flush_hot_memory_timestamps, _get_db, hot_ids, memories
    with _data_lock:
        db = _get_db()
        # 热记忆的最新时间/强化结果落库（与过期判定共用同一段逻辑，避免两处不一致）
        _flush_hot_memory_timestamps(db)

        os.makedirs("data/test", exist_ok=True)
        serializable_sentence_links = {f"{src}||{tgt}": val for (src, tgt), val in list(links.items())}
        serializable_word_links = {f"{a}||{b}": val for (a, b), val in list(wordweb.items())}
        data = {
            "memories": {mid: memories[mid] for mid in list(hot_ids) if mid in memories},
            "links": serializable_sentence_links,
            "wordweb": serializable_word_links,
        }
        tmp_file = MEMORY_FILE + ".tmp"
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, MEMORY_FILE)

        # 热链接增量下沉到 SQLite（只写本轮发生变化的行）
        from core.memory_engine import _sync_all_links_to_sqlite
        _sync_all_links_to_sqlite()

        clock.save_state()

        from core.memory_engine import _faiss_index, _faiss_to_mem
        import faiss
        faiss.write_index(_faiss_index, FAISS_INDEX_FILE)
        with open(FAISS_MAPPING_FILE, 'w', encoding='utf-8') as f:
            json.dump(_faiss_to_mem, f, ensure_ascii=False)

    # 保存每日指标计数器快照
    from core.memory_engine import _export_metrics_counters
    metrics = _export_metrics_counters()
    with open(METRICS_COUNTERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(metrics, f, ensure_ascii=False)

    # 保存生物钟状态（精力/睡眠压力/睡眠债）
    try:
        from core.biorhythm import BIORHYTHM
        BIORHYTHM.save()
    except Exception:
        pass

def _db_memory_count() -> int:
    """SQLite 中的记忆条数（判断是否需要冷启动注入的依据）。"""
    try:
        from core.memory_engine import _get_db
        return _get_db().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    except Exception:
        return 0


def _restore_hot_memories_from_db(limit: int = None) -> int:
    """从 SQLite 恢复热记忆到内存（调用方需持有 _io_lock）。

    取最近访问的若干条装入内存，其余保持冷态（按需加载）。
    返回装入内存的条数。
    """
    from core.memory_engine import (
        _get_db, hot_ids, MAX_HOT_SIZE, _row_to_memory,
    )
    if limit is None:
        limit = MAX_HOT_SIZE
    db = _get_db()
    rows = db.execute(
        "SELECT id, content, vector, half_life, last_accessed, creation_time, "
        "last_strengthen_time, concept_tag_ids FROM memories "
        "ORDER BY last_accessed DESC LIMIT ?",
        (limit,)
    ).fetchall()
    for row in rows:
        mem = _row_to_memory(row)
        if mem:
            memories[mem["id"]] = mem
    hot_ids.update(memories.keys())
    return len(memories)


def _restore_links_from_db():
    """从 SQLite 恢复链接与字词网络到内存（调用方需持有 _io_lock）。"""
    from core.memory_engine import _get_db
    db = _get_db()
    links.clear()
    for src, tgt, weight, ltype, last_accessed, creation_time in db.execute(
        "SELECT src, tgt, weight, type, last_accessed, creation_time FROM links"
    ).fetchall():
        links[(src, tgt)] = {
            "weight": weight, "type": ltype,
            "last_accessed": last_accessed, "creation_time": creation_time,
        }
    from core.memory_engine import _load_wordweb_from_db
    _load_wordweb_from_db()


def load_all_data():
    """加载记忆与链接。

    **SQLite 是权威数据源**，JSON 仅是"热记忆的可重建缓存"：
      - JSON 不存在（例如已执行过 migrate_json_to_sqlite，它会主动删除 JSON）
        时，不再直接返回，而是从 SQLite 恢复。
      - JSON 存在时先加载它（快），再由 SQLite 补齐缺失部分（JSON 可能落后）。
    """
    global memories, links
    with _io_lock:
        from core.memory_engine import hot_ids

        # 1) 先尝试 JSON 缓存（快路径）
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, 'r', encoding='utf-8-sig') as f:
                    data = json.load(f)
                memories.clear()
                hot_ids.clear()
                memories.update(data.get("memories", {}))
                hot_ids.update(memories.keys())
                links.clear()
                for key_str, val in data.get("links", {}).items():
                    src, tgt = key_str.split("||")
                    links[(src, tgt)] = val
                wordweb.clear()
                for key_str, val in data.get("wordweb", {}).items():
                    a, b = key_str.split("||")
                    wordweb[(a, b)] = val
            except Exception as e:
                print(f"[持久化] JSON 缓存损坏，将从 SQLite 恢复：{e}")
                memories.clear()
                hot_ids.clear()

        # 2) 以 SQLite 为准补齐：JSON 缺失、损坏、或落后于数据库时都会走到这里
        db_total = _db_memory_count()
        if len(memories) < db_total:
            if not memories:
                print(f"[持久化] 从 SQLite 恢复记忆（JSON 缺失或损坏），共 {db_total} 条")
            _restore_hot_memories_from_db()
            if not links:
                _restore_links_from_db()

        from core.memory_engine import _build_word_to_memories
        _build_word_to_memories()

        # faiss：快照仅在"与数据库成员一致"时采信，否则全量重建
        from core.memory_engine import (
            _faiss_index, _faiss_to_mem, _mem_to_faiss,
            _init_faiss_index, _rebuild_faiss_index,
        )
        import faiss
        if os.path.exists(FAISS_INDEX_FILE) and os.path.exists(FAISS_MAPPING_FILE):
            try:
                _faiss_index_global = faiss.read_index(FAISS_INDEX_FILE)
                with open(FAISS_MAPPING_FILE, 'r', encoding='utf-8-sig') as f:
                    loaded_mapping = json.load(f)
                if _faiss_snapshot_is_valid(_faiss_index_global, loaded_mapping, db_total):
                    import core.memory_engine as me
                    me._faiss_index = _faiss_index_global
                    me._faiss_to_mem = loaded_mapping
                    me._mem_to_faiss = {mem_id: idx for idx, mem_id in enumerate(loaded_mapping)}
                else:
                    print("[持久化] faiss 快照与数据库不一致，将全量重建索引")
                    _rebuild_faiss_index()
            except Exception as e:
                print(f"[持久化] faiss 快照不可用，将全量重建索引：{e}")
                _rebuild_faiss_index()
        else:
            print("[持久化] 未找到 faiss 文件，全量重建索引")
            _rebuild_faiss_index()
        
        if os.path.exists(METRICS_COUNTERS_FILE):
            with open(METRICS_COUNTERS_FILE, 'r', encoding='utf-8-sig') as f:
                metrics = json.load(f)
            from core.memory_engine import _import_metrics_counters
            _import_metrics_counters(metrics)

        # 限制热记忆数量到硬上限（MAX_HOT_SIZE），把最久未访问的记忆移出内存
        from core.memory_engine import _evict_cold_memories, _evict_cold_links
        _evict_cold_memories()
        _evict_cold_links()

        # 加载生物钟状态（按离线真实时长补算）
        try:
            from core.biorhythm import BIORHYTHM
            BIORHYTHM.load()
        except Exception:
            pass


def _faiss_snapshot_is_valid(index, mapping, db_total: int) -> bool:
    """校验 faiss 快照是否可信。

    除长度相等外，还要求：向量维度正确、成员集合落在数据库当前 ID 集合内、
    且映射条数与数据库条数一致。任一不满足就丢弃快照走全量重建——
    否则会静默接受陈旧索引，导致遗漏的记忆无法被向量检索命中。
    """
    from core.memory_engine import _get_db, EMBED_DIM
    if not isinstance(mapping, list):
        return False
    if len(mapping) != index.ntotal:
        return False
    if len(mapping) != db_total:
        return False
    if getattr(index, "d", EMBED_DIM) != EMBED_DIM:
        return False
    try:
        db_ids = {r[0] for r in _get_db().execute("SELECT id FROM memories").fetchall()}
    except Exception:
        return False
    return set(mapping).issubset(db_ids)

# ========== 对话状态持久化 ==========

def save_state():
    """保存对话状态到 JSON 文件（按群隔离后的全部状态）"""
    with _io_lock:
        os.makedirs("data/test", exist_ok=True)
        from utils.dialogue_state import all_states
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(all_states(), f, ensure_ascii=False, indent=2)

def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, 'r', encoding='utf-8-sig') as f:
            loaded = json.load(f)
        # 标准化键名，仅保留在用的状态键；历史遗留的"已知信息"类字段直接丢弃
        def _normalize(state: dict) -> dict:
            return {k: v for k, v in state.items() if k in ("参与者", "最近话题")}

        from utils.dialogue_state import load_states
        if isinstance(loaded, dict) and "参与者" in loaded:
            # 旧格式：一份全局状态 → 归入默认会话，保证首次升级不丢状态
            normalized = _normalize(loaded)
            load_states(normalized if normalized else None)
            print(f"[状态加载] 旧格式已迁移到默认会话：{normalized}")
            return

        cleaned = {}
        if isinstance(loaded, dict):
            for group_key, state in loaded.items():
                if not isinstance(state, dict):
                    continue
                norm = _normalize(state)
                if norm:
                    # JSON 的键都是字符串，None 会被写成 "null"，还原回来
                    cleaned[None if group_key == "null" else group_key] = norm
        load_states(cleaned)
        print(f"[状态加载] 已加载 {len(cleaned)} 个群的状态")
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
        os.makedirs("data/test", exist_ok=True)
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

    锁顺序：本函数**先取 _io_lock，再在其内部取 _data_lock**，
    与 save_all_data 保持一致（它同样是 io→data）。
    此前本函数是 data→io（在 _data_lock 内调用 save_all_data），
    与 save_all_data 构成 AB-BA 环路，两个线程交错时会互相等待造成死锁。
    """
    from core.memory_engine import _data_lock
    with _io_lock:                    # 先 io
        with _data_lock:              # 后 data
            _do_sleep_cleanup_locked()


def _do_sleep_cleanup_locked():
    """睡眠维护的实际工作。调用方需同时持有 _io_lock 与 _data_lock。"""
    from core.memory_engine import (
        _get_db, _bump_link_deleted, _deleted_links, _dirty_links,
        _purge_expired_memories, _rebuild_faiss_index,
        _evict_cold_memories, _evict_cold_links, _write_daily_metrics,
    )
    now = clock.now()
    removed_links = 0
    removed_wordweb = 0

    # 1. 链接剪枝（内存热链接）
    dead_links = []
    for key, data in list(links.items()):
        delta = now - data.get("last_accessed", data.get("creation_time", now))
        decay = 2 ** (-delta / (7 * 24 * 3600))  # LINK_HALF_LIFE
        if data["weight"] * decay < 0.01:
            dead_links.append(key)
    for key in dead_links:
        del links[key]
        removed_links += 1
        _dirty_links.discard(key)
        _deleted_links.add(key)
        _bump_link_deleted()
    # 剪枝 SQLite 中已下沉的冷链接（不在内存中的）
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
        _bump_link_deleted()
    _cold_db.commit()

    # 2. 字词网络剪枝（衰减后共现次数过低则删除）
    dead_words = []
    for (a, b), wdata in list(wordweb.items()):
        delta = now - wdata.get("last_updated", now)
        decay = 2 ** (-delta / (7 * 24 * 3600))
        if wdata["forward_count"] * decay < 0.5:
            dead_words.append((a, b))
    for key in dead_words:
        del wordweb[key]
        removed_wordweb += 1

    # 3. 物理删除衰变记忆，并重建 faiss 索引
    expired = _purge_expired_memories()
    if expired:
        _rebuild_faiss_index()          # faiss 全量重建（从 SQLite 剩余记忆重新构建）
        print(f"[睡眠维护] 物理删除 {len(expired)} 条衰变记忆，faiss 索引已重建")

    # 3.5 冷记忆淘汰：将超出热记忆上限的最久未访问记忆移出内存（写入 SQLite）
    _evict_cold_memories()
    _evict_cold_links()

    # 4. 全量持久化（含热数据写入 JSON、faiss 索引、时钟状态）
    #    此处已在 _io_lock 内，必须走不再取锁的内部实现，否则重入会死锁。
    _do_save_all_data()
    print(f"[睡眠维护] 链接剪枝 {removed_links}，字词清理 {removed_wordweb}，物理删除 {len(expired)} 条记忆")

    _write_daily_metrics()

# ========== 临时数据迁移（将json导入SQLite） ==========
def migrate_json_to_sqlite():
    """将旧的 memory.json 中的所有记忆导入 SQLite，然后删除 JSON 文件"""
    if not os.path.exists(MEMORY_FILE):
        return
    from core.memory_engine import _get_db, _faiss_index, _faiss_to_mem, _mem_to_faiss
    import faiss, numpy as np
    with open(MEMORY_FILE, 'r', encoding='utf-8-sig') as f:
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