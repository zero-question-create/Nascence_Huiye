# utils/persistence.py
import json
import os
import threading
import time
from core.memory_engine import memories, links, pending_deletion, wordweb, hot_ids
from core.memory_engine import check_and_handle_expired, _evict_cold_memories
from core.virtual_clock import clock
from utils.atomic_io import atomic_write_json, atomic_write_text
from utils.metrics_probe import PROBE

_io_lock = threading.Lock()

# 保存节流：关停路径上 QQ 服务与面板会各保存一次，短时间内的重复调用只是重做同样的落盘。
# 小于该间隔的重复保存直接跳过；关停时用 save_all_data(force=True) 保证最后一次一定落盘。
_SAVE_MIN_INTERVAL = 3.0
_last_save_time = 0.0
_save_count = 0                # 统计实际执行的保存次数（供测试与观测）

MEMORY_FILE = "data/test/memory.json"
MEMORY_BAK_FILE = "data/test/memory.json.migrated.bak"
MEMORY_MIGRATED_FLAG = "data/test/memory_json_migrated.flag"
WORDWEB_FILE = "data/test/wordweb.json"
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


def _memory_json_disabled() -> bool:
    """memory.json 是否已完成迁移（迁移后不再整份写这个 36MB 级文件）。"""
    return os.path.exists(MEMORY_MIGRATED_FLAG)


def _snapshot_hot_state():
    """在短锁内取一份热数据快照，供锁外写盘使用。

    此前 dump JSON、写 faiss、同步 links 全在 _data_lock 内完成，
    36MB 序列化 + 磁盘抖动会把认知循环整段卡住。
    """
    from core.memory_engine import _data_lock, _faiss_index, _faiss_to_mem
    with _data_lock:
        mems = {mid: memories[mid] for mid in list(hot_ids) if mid in memories}
        lks = dict(links)
        words = dict(wordweb)
        faiss_index = _faiss_index
        faiss_mapping = list(_faiss_to_mem)
    return mems, lks, words, faiss_index, faiss_mapping


def _do_save_all_data():
    """实际执行保存（调用方需持有 _io_lock）。I/O 全部在 _data_lock 之外。"""
    import faiss

    # 1) 热记忆时间字段 + 热链接增量：交给单写者（内部自行加短锁、写库）
    from core.memory_writer import WRITER
    WRITER.submit("sync_links", wait=True)

    # 2) 锁外取快照并写派生文件
    mems, lks, words, faiss_index, faiss_mapping = _snapshot_hot_state()
    os.makedirs("data/test", exist_ok=True)

    if not _memory_json_disabled():
        # 未迁移的旧库仍写 memory.json，保持兼容；迁移后不再产生这个大文件。
        serializable_sentence_links = {f"{src}||{tgt}": val for (src, tgt), val in lks.items()}
        serializable_word_links = {f"{a}||{b}": val for (a, b), val in words.items()}
        atomic_write_json(MEMORY_FILE, {
            "memories": mems,
            "links": serializable_sentence_links,
            "wordweb": serializable_word_links,
        })

    # 字词网络单独落盘（memory.json 停写后它是唯一副本）
    serializable_word_links = {f"{a}||{b}": val for (a, b), val in words.items()}
    atomic_write_json(WORDWEB_FILE, serializable_word_links)

    # 3) 热链接下沉已在上面由写者完成

    clock.save_state()

    # faiss：先写临时文件再替换，避免主进程读到半截索引
    if faiss_index is not None:
        tmp_index = FAISS_INDEX_FILE + ".tmp"
        try:
            faiss.write_index(faiss_index, tmp_index)
            os.replace(tmp_index, FAISS_INDEX_FILE)
        finally:
            try:
                if os.path.exists(tmp_index):
                    os.remove(tmp_index)
            except OSError:
                pass
    atomic_write_json(FAISS_MAPPING_FILE, faiss_mapping)

    # 保存每日指标计数器快照
    from core.memory_engine import _export_metrics_counters
    atomic_write_json(METRICS_COUNTERS_FILE, _export_metrics_counters())

    # 保存生物钟状态（精力/睡眠压力/睡眠债）
    try:
        from core.biorhythm import BIORHYTHM
        BIORHYTHM.save()
    except Exception:
        pass

    PROBE.gauge("memory.hot", len(mems))
    PROBE.gauge("links.hot", len(lks))
    PROBE.gauge("wordweb.size", len(words))

def migrate_memory_json() -> dict:
    """启动时把旧的 memory.json 数据并入 SQLite，然后停用该缓存文件。

    顺序必须是“先恢复、再删除”：
      1. 读 memory.json（存在时），把 memories / links 逐条写入 SQLite；
      2. wordweb 没有 SQLite 表，单独写到 wordweb.json；
      3. 提交事务并 fsync；
      4. 成功后把 memory.json 改名为 .migrated.bak，并写下迁移标记；
      5. 下一次启动看到标记 + .bak 时才真正删除 .bak。

    返回 {"migrated": bool, "memories": n, "links": n, "wordweb": n, "reason": str}
    """
    result = {"migrated": False, "memories": 0, "links": 0, "wordweb": 0, "reason": ""}

    # 已经迁移过：清理上一轮留下的备份即可
    if _memory_json_disabled():
        if os.path.exists(MEMORY_BAK_FILE):
            try:
                os.remove(MEMORY_BAK_FILE)
                result["reason"] = "已迁移，清理备份"
            except OSError as e:
                result["reason"] = f"已迁移，备份清理失败: {e}"
        else:
            result["reason"] = "已迁移"
        return result

    if not os.path.exists(MEMORY_FILE):
        # 从来没有 memory.json：直接打标记，之后不再产生它
        atomic_write_text(MEMORY_MIGRATED_FLAG, json.dumps({
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "source": "none",
        }, ensure_ascii=False))
        result["reason"] = "无 memory.json，跳过迁移"
        return result

    try:
        with open(MEMORY_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        result["reason"] = f"memory.json 无法解析，保持原状: {e}"
        return result

    from core.memory_engine import _get_db
    import numpy as np

    db = _get_db()
    mem_count = link_count = 0
    try:
        db.execute("BEGIN IMMEDIATE")
        for mem_id, mem in (data.get("memories") or {}).items():
            if not isinstance(mem, dict) or not mem.get("content"):
                continue
            vector = mem.get("vector") or []
            try:
                vec = np.asarray(vector, dtype=np.float32).reshape(-1)
            except (TypeError, ValueError):
                continue
            if vec.size == 0:
                continue
            now = float(mem.get("last_accessed") or time.time())
            db.execute(
                """INSERT OR IGNORE INTO memories
                   (id, content, vector, half_life, last_accessed, creation_time,
                    last_strengthen_time, concept_tag_ids)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mem_id, str(mem["content"]), vec.tobytes(),
                    float(mem.get("half_life") or 2 * 24 * 3600),
                    now,
                    float(mem.get("creation_time") or now),
                    float(mem.get("last_strengthen_time") or now),
                    json.dumps(mem.get("concept_tag_ids") or [], ensure_ascii=False),
                ),
            )
            mem_count += 1

        for key_str, val in (data.get("links") or {}).items():
            if not isinstance(val, dict) or "||" not in str(key_str):
                continue
            src, tgt = str(key_str).split("||", 1)
            db.execute(
                """INSERT OR REPLACE INTO links
                   (src, tgt, weight, type, last_accessed, creation_time)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    src, tgt,
                    float(val.get("weight", 0.0)),
                    str(val.get("type", "semantic")),
                    float(val.get("last_accessed") or val.get("creation_time") or 0.0),
                    float(val.get("creation_time") or 0.0),
                ),
            )
            link_count += 1
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        result["reason"] = f"写入 SQLite 失败，保持 memory.json 原状: {e}"
        return result

    # wordweb：单独落盘（SQLite 无对应表）
    wordweb_data = data.get("wordweb") or {}
    if wordweb_data:
        atomic_write_json(WORDWEB_FILE, wordweb_data)
        result["wordweb"] = len(wordweb_data)

    # 提交成功后才停用 source of truth
    try:
        os.replace(MEMORY_FILE, MEMORY_BAK_FILE)
    except OSError as e:
        result["reason"] = f"数据已入库，但重命名 memory.json 失败: {e}"
        return result

    atomic_write_text(MEMORY_MIGRATED_FLAG, json.dumps({
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "memories": mem_count,
        "links": link_count,
        "wordweb": result["wordweb"],
    }, ensure_ascii=False))
    result.update({"migrated": True, "memories": mem_count, "links": link_count,
                   "reason": "迁移完成"})
    return result


def _db_memory_count() -> int:
    """SQLite 中的记忆条数（判断是否需要冷启动注入的依据）。"""
    try:
        from core.memory_engine import _get_db
        return _get_db().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    except Exception:
        return 0


def _newest_db_last_accessed() -> float:
    """数据库中最新的 last_accessed 时间戳。

    走 MAX(rowid) 主键 B 树定位最新一行再取值（O(1)），而不是
    `SELECT MAX(last_accessed)`：后者在没有该列索引时是全表扫描
    （存量库上数百毫秒到数秒），而我们已决定不在启动路径建任何新索引。
    记忆按插入顺序累积，rowid 最大 ≈ 最后写入，语义等价。
    """
    from core.memory_engine import _get_db
    row = _get_db().execute(
        "SELECT last_accessed FROM memories ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def _newest_db_rowid() -> int:
    """数据库中最新的 rowid（O(1)）。0 表示空库。"""
    from core.memory_engine import _get_db
    row = _get_db().execute("SELECT MAX(rowid) FROM memories").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _restore_hot_memories_from_db(limit: int = None) -> int:
    """从 SQLite 恢复热记忆到内存（调用方需持有 _io_lock）。

    取最近写入的若干条装入内存，其余保持冷态（按需加载）。
    **按 rowid 倒序**取 TOP-N：rowid 是隐式主键 B 树，反向游走只读 N 行；
    若按 last_accessed 排序，在该列无索引时是全表扫描 + 临时 B 树排序
    （存量库上实测十几秒到分钟级，服务端 5 万级库正是启动 636s 的主因）。
    记忆按时间顺序写入，rowid 序与写入序一致，取出的就是最新一批。
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
        "ORDER BY rowid DESC LIMIT ?",
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

    只取最近写入的 MAX_HOT_LINKS 条：热链接本就有硬上限，先把全表读进内存
    再逐条淘汰，在存量库（曾见 96 万条链接）会变成分钟级卡顿；冷链接本就由
    _load_links_for_memory 在记忆访达时按需装入，这里截断不损失可达性。

    按 rowid 倒序取 TOP-N（隐式主键 B 树反向游走，只读 N 行）。
    **不要**按 last_accessed 排序，也**不要**为此建索引：在 96 万行的表上，
    建索引或全表排序都是分钟级的启动开销。
    """
    from core.memory_engine import _get_db, MAX_HOT_LINKS
    db = _get_db()
    links.clear()
    rows = db.execute(
        "SELECT src, tgt, weight, type, last_accessed, creation_time FROM links "
        "ORDER BY rowid DESC LIMIT ?",
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

    顺序（按确认的方案）：
      1. 先尝试从 memory.json 恢复并迁入 SQLite，成功后停用该文件；
      2. 没有 memory.json（或已迁移）时，从 SQLite 恢复热记忆与链接；
      3. wordweb 从 wordweb.json 恢复（memory.json 停写后它是唯一副本）。

    **SQLite 是权威数据源**，JSON 只是历史遗留的热缓存。
    热集上限（MAX_HOT_SIZE）天然小于全库总量，因此"热记忆 < 全库"
    是常态而非异常；未覆盖的记忆由 _load_memory_from_db 在访达时按需装入。
    """
    global memories, links
    t_total = time.time()
    with _io_lock:
        from core.memory_engine import hot_ids

        # 0) memory.json → SQLite 迁移（只做一次；先恢复、再停用文件）
        t_migrate = time.time()
        try:
            migration = migrate_memory_json()
            print(f"[持久化] memory.json 迁移：{migration['reason']}"
                  f"（记忆 {migration['memories']}，链接 {migration['links']}，"
                  f"字词 {migration['wordweb']}，{time.time() - t_migrate:.1f}s）")
        except Exception as e:
            print(f"[持久化] memory.json 迁移失败，保留原文件并继续：{e}")

        # 1) 兼容：迁移失败时 memory.json 仍存在，按旧路径先读它（快路径）
        t_json = time.time()
        legacy_wordweb = None
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
                legacy_wordweb = data.get("wordweb") or None
                print(f"[持久化] JSON 缓存加载完成：记忆 {len(memories)}，链接 {len(links)}"
                      f"（{time.time() - t_json:.1f}s）")
            except Exception as e:
                print(f"[持久化] JSON 缓存损坏，将从 SQLite 恢复：{e}")
                memories.clear()
                hot_ids.clear()

        # 1b) 字词网络：优先 wordweb.json；迁移未完成时回退到 memory.json 里的副本
        t_word = time.time()
        if os.path.exists(WORDWEB_FILE) and not wordweb:
            try:
                with open(WORDWEB_FILE, "r", encoding="utf-8-sig") as f:
                    raw = json.load(f)
                wordweb.clear()
                for key_str, val in (raw or {}).items():
                    if "||" not in str(key_str):
                        continue
                    a, b = str(key_str).split("||", 1)
                    wordweb[(a, b)] = val
                print(f"[持久化] 字词网络恢复 {len(wordweb)} 条（{time.time() - t_word:.1f}s）")
            except Exception as e:
                print(f"[持久化] wordweb.json 读取失败，保持为空：{e}")
        elif not wordweb and legacy_wordweb:
            wordweb.clear()
            for key_str, val in legacy_wordweb.items():
                if "||" not in str(key_str):
                    continue
                a, b = str(key_str).split("||", 1)
                wordweb[(a, b)] = val
            print(f"[持久化] 字词网络从 memory.json 恢复 {len(wordweb)} 条（迁移尚未完成）")

        # 2) 判断是否需要从 SQLite 补载热记忆。
        t_check = time.time()
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
                    print(f"[持久化] 数据库有比内存更新的记忆"
                          f"（新 {db_newest - json_newest:.0f}s），补载热记忆")
            except Exception:
                need_restore = False
        print(f"[持久化] 落后判定完成：{time.time() - t_check:.2f}s（不补载）"
              if not need_restore else
              f"[持久化] 落后判定完成：{time.time() - t_check:.2f}s（将补载）")
        if need_restore:
            if not memories:
                print(f"[持久化] 从 SQLite 恢复记忆（JSON 缺失或已迁移），共 {db_total} 条")
            t_restore = time.time()
            _restore_hot_memories_from_db()
            print(f"[持久化] 热记忆恢复完成：{len(memories)} 条"
                  f"（{time.time() - t_restore:.1f}s）")
        if not links:
            t_links = time.time()
            _restore_links_from_db()
            print(f"[持久化] 链接恢复段耗时：{time.time() - t_links:.1f}s")

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
    from core.memory_engine import _get_db, EMBED_DIM
    if not isinstance(mapping, list):
        return False
    if len(mapping) != index.ntotal:
        return False
    if len(mapping) != db_total:
        return False
    if getattr(index, "d", EMBED_DIM) != EMBED_DIM:
        return False
    # 一次扫描取全表 ID（id 是主键，走覆盖索引，5 万条实测约 0.1s）。
    # 不要改成"逐条 EXISTS"：那是 N 次数据库往返，映射上万条时反而慢出量级
    # （服务端 5 万条映射实测 43s），本地小库测不出差距。
    try:
        db_ids = {r[0] for r in _get_db().execute("SELECT id FROM memories").fetchall()}
    except Exception:
        return False
    return set(mapping).issubset(db_ids)

# ========== 对话状态持久化 ==========

def save_state():
    """保存对话状态到 JSON 文件（原子写，避免被读到半截）"""
    with _io_lock:
        os.makedirs("data/test", exist_ok=True)
        from utils.dialogue_state import current_state
        atomic_write_json(STATE_FILE, current_state)

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
    睡眠巩固阶段的全量维护：
    1. 剪枝权重过低的链接
    2. 整理字词网络（衰减 + 剪枝）
    3. 物理删除衰变到极致的记忆
    4. 全量持久化（含重建 faiss 索引）

    锁顺序：**先 _io_lock，再取 _data_lock 做内存改动，随后释放 _data_lock 再落盘**。
    此前整段维护（含全表扫描、faiss 重建、36MB JSON dump）都压在 _data_lock 内，
    认知循环与消息处理会被整段卡住。
    """
    with _io_lock:                    # 先 io
        stats = _do_sleep_cleanup_locked()
        _do_save_all_data()           # 锁外 I/O（_do_save_all_data 自己只短锁取快照）
        _write_daily_metrics()
    print(f"[睡眠维护] 链接剪枝 {stats['links']}，字词清理 {stats['words']}，"
          f"物理删除 {stats['expired']} 条记忆")


def _do_sleep_cleanup_locked() -> dict:
    """睡眠维护的内存改动部分（内部自行取/放 _data_lock）。

    返回 {"links","words","expired"} 供上层打日志。
    """
    from core.memory_engine import (
        _data_lock, _get_db, _bump_link_deleted, _deleted_links, _dirty_links,
        _purge_expired_memories, _rebuild_faiss_index,
        _evict_cold_memories, _evict_cold_links,
    )
    now = clock.now()
    removed_links = 0
    removed_wordweb = 0

    with _data_lock:
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

    # 3. 物理删除衰变记忆（内部自行加锁，并同步 SQLite）
    expired = _purge_expired_memories()
    if expired:
        _rebuild_faiss_index()          # faiss 全量重建（从 SQLite 剩余记忆重新构建）
        print(f"[睡眠维护] 物理删除 {len(expired)} 条衰变记忆，faiss 索引已重建")

    # 3.5 冷记忆淘汰：挑名单在短锁内，写库在锁外
    _evict_cold_memories()
    _evict_cold_links()

    # 4. 剪枝 SQLite 中已下沉的冷链接（不在内存中的）
    #    这一步是全表扫描 + 逐条 DELETE，放在锁外执行。
    _cold_db = _get_db()
    _cold_rows = _cold_db.execute(
        "SELECT src, tgt, weight, last_accessed, creation_time FROM links"
    ).fetchall()
    hot_keys = set(links.keys())
    to_delete = []
    for src, tgt, weight, last_accessed, creation_time in _cold_rows:
        if (src, tgt) in hot_keys:
            continue  # 热链接跳过，交给内存管理
        _delta = now - (last_accessed or creation_time or now)
        if weight * (2 ** (-_delta / (7 * 24 * 3600))) < 0.01:
            to_delete.append((src, tgt))
    if to_delete:
        _cold_db.executemany("DELETE FROM links WHERE src = ? AND tgt = ?", to_delete)
        _cold_db.commit()
        for _ in to_delete:
            removed_links += 1
            _bump_link_deleted()

    return {"links": removed_links, "words": removed_wordweb, "expired": len(expired)}

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