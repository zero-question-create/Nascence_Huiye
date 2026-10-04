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


def _newest_db_last_accessed() -> float:
    """数据库中最新的 last_accessed 时间戳（走索引，微秒级）。

    用于判断"JSON 缓存是否落后于数据库"，替代此前"热集条数 < 全库条数"
    的恒真判据——后者会导致每次启动都做一次昂贵的热记忆全量恢复。
    """
    from core.memory_engine import _get_db
    row = _get_db().execute("SELECT MAX(last_accessed) FROM memories").fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


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
    """从 SQLite 恢复链接与字词网络到内存（调用方需持有 _io_lock）。

    只取最近访问的 MAX_HOT_LINKS 条：热链接本就有硬上限，先把全表读进内存
    再逐条淘汰，在存量库（曾见 96 万条链接）会变成分钟级卡顿；冷链接本就由
    _load_links_for_memory 在记忆访达时按需装入，这里截断不损失可达性。
    """
    from core.memory_engine import _get_db, MAX_HOT_LINKS
    db = _get_db()
    # 按 last_accessed 排序取 TOP-N：本表默认没有该列索引，大表上必须现建，
    # 否则排序要扫全表（96 万行 ≈ 分钟级）。索引仅在真正走恢复路径时才建，
    # 常规启动（JSON 可用）不会付出这份一次性的建索引成本。
    try:
        db.execute("CREATE INDEX IF NOT EXISTS idx_links_last_accessed ON links(last_accessed)")
        db.commit()
    except Exception:
        pass
    links.clear()
    rows = db.execute(
        "SELECT src, tgt, weight, type, last_accessed, creation_time FROM links "
        "ORDER BY last_accessed DESC LIMIT ?",
        (MAX_HOT_LINKS,)
    ).fetchall()
    for src, tgt, weight, ltype, last_accessed, creation_time in rows:
        links[(src, tgt)] = {
            "weight": weight, "type": ltype,
            "last_accessed": last_accessed, "creation_time": creation_time,
        }
    print(f"[持久化] 链接恢复完成：{len(links)} 条（上限 {MAX_HOT_LINKS}）")
    from core.memory_engine import _load_wordweb_from_db
    _load_wordweb_from_db()


def load_all_data():
    """加载记忆与链接。

    **SQLite 是权威数据源**，JSON 仅是"热记忆的可重建缓存"：
      - JSON 不存在（例如已执行过 migrate_json_to_sqlite，它会主动删除 JSON）
        时，从 SQLite 恢复。
      - JSON 存在时先加载它（快），随后按需从 SQLite 补齐。

    注意：热集上限（MAX_HOT_SIZE）天然小于全库总量，因此"热记忆 < 全库"
    是常态而非异常，不能据此判定 JSON 落后而每次全量重取——存量库上这一步
    是分钟级的（实测服务端开机 636s 的主要来源）。JSON 未覆盖的记忆与链接
    由 _load_memory_from_db / _load_links_for_memory 在访达时按需装入，
    可达性不受影响。
    """
    global memories, links
    t_total = time.time()
    with _io_lock:
        from core.memory_engine import hot_ids

        # 1) 先尝试 JSON 缓存（快路径）
        t_json = time.time()
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
                print(f"[持久化] JSON 缓存加载完成：记忆 {len(memories)}，链接 {len(links)}，"
                      f"字词 {len(wordweb)}（{time.time() - t_json:.1f}s）")
            except Exception as e:
                print(f"[持久化] JSON 缓存损坏，将从 SQLite 恢复：{e}")
                memories.clear()
                hot_ids.clear()

        # 2) 判断是否需要从 SQLite 补载热记忆。
        #
        # 旧判据是 "len(memories) < db_total"，但热集上限（MAX_HOT_SIZE）天然
        # 小于全库总量，该条件恒为真 —— 于是每次启动都做一次全表扫描 + 排序
        # 的昂贵恢复（服务端 5 万级库实测数百秒），绝大多数时候纯属浪费。
        #
        # 现改为：JSON 不可用时才恢复；JSON 可用时，仅当数据库里存在比 JSON
        # 更新的记忆（上次未及保存就退出）才补载。判据用 last_accessed 最大值
        # （有索引，微秒级）与 JSON 中的最大值比较。
        db_total = _db_memory_count()
        need_restore = not memories
        if not need_restore:
            try:
                db_newest = _newest_db_last_accessed()
                json_newest = max(
                    (float(m.get("last_accessed", 0.0) or 0.0) for m in memories.values()),
                    default=0.0,
                )
                if db_newest > json_newest + 1.0:
                    need_restore = True
                    print(f"[持久化] 数据库有比 JSON 更新的记忆"
                          f"（新 {db_newest - json_newest:.0f}s），补载热记忆")
            except Exception:
                need_restore = False
        if need_restore:
            if not memories:
                print(f"[持久化] 从 SQLite 恢复记忆（JSON 缺失或损坏），共 {db_total} 条")
            _restore_hot_memories_from_db()
        if not links:
            _restore_links_from_db()

        t_word = time.time()
        from core.memory_engine import _build_word_to_memories
        _build_word_to_memories()
        print(f"[持久化] 词网重建耗时：{time.time() - t_word:.1f}s")

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
    from core.memory_engine import EMBED_DIM
    if not isinstance(mapping, list):
        return False
    if len(mapping) != index.ntotal:
        return False
    if len(mapping) != db_total:
        return False
    if getattr(index, "d", EMBED_DIM) != EMBED_DIM:
        return False
    # 逐条 EXISTS 校验：只为确认映射里的 ID 仍存在于库中。
    # 此前一次性拉全表 ID 建 Python 集合，在 5 万条规模的库存上要数百毫秒到
    # 数秒；这里只查映射自身（通常几千条）并提前短路。
    try:
        from core.memory_engine import _get_db
        db = _get_db()
        missing = 0
        for mem_id in mapping:
            if db.execute("SELECT 1 FROM memories WHERE id = ? LIMIT 1", (mem_id,)).fetchone() is None:
                missing += 1
                if missing > 0:
                    return False
        return True
    except Exception:
        return False

# ========== 对话状态持久化 ==========

def save_state():
    """保存对话状态到 JSON 文件"""
    with _io_lock:
        os.makedirs("data/test", exist_ok=True)
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            from utils.dialogue_state import current_state
            json.dump(current_state, f, ensure_ascii=False, indent=2)

def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, 'r', encoding='utf-8-sig') as f:
            loaded = json.load(f)
        # 标准化键名，仅保留在用的状态键；历史遗留的"已知信息"类字段直接丢弃
        normalized = {}
        for key, value in loaded.items():
            if key in ("参与者", "最近话题"):
                normalized[key] = value
        # 更新状态，不清空，防止空覆盖
        if normalized:
            from utils.dialogue_state import current_state
            current_state.update(normalized)
        print(f"[状态加载] 加载内容：{normalized}")
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