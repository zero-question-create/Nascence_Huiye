# utils/persistence.py
import json
import os
import threading
import time
from core.memory_engine import memories, links, pending_deletion, wordweb, hot_ids
from core.memory_engine import check_and_handle_expired, _evict_cold_memories
from core.virtual_clock import clock

_io_lock = threading.Lock()

MEMORY_FILE = "data/test/memory.json"
STATE_FILE = "data/test/dialogue_state.json"
DIALOGUE_LOG_FILE = "data/test/dialogue_log.jsonl"
FAISS_INDEX_FILE = "data/test/faiss.index"
FAISS_MAPPING_FILE = "data/test/faiss_mapping.json"
METRICS_COUNTERS_FILE = "data/test/metrics_counters.json"

# ========== 记忆持久化 ==========

def save_all_data():
    """保存记忆和链接到文件（原子写入，避免截断）"""
    with _io_lock:
        from core.memory_engine import _get_db, hot_ids, memories
        db = _get_db()
        for mid in hot_ids:
            mem = memories.get(mid)
            if mem:
                db.execute(
                    "UPDATE memories SET last_accessed=?, half_life=?, last_strengthen_time=? WHERE id=?",
                    (mem["last_accessed"], mem["half_life"], mem.get("last_strengthen_time", mem["creation_time"]), mid)
                )
        db.commit()

        os.makedirs("data/test", exist_ok=True)
        serializable_sentence_links = {f"{src}||{tgt}": val for (src, tgt), val in links.items()}
        serializable_word_links = {f"{a}||{b}": val for (a, b), val in wordweb.items()}
        data = {"memories": {mid: memories[mid] for mid in hot_ids}, "links": serializable_sentence_links, "wordweb": serializable_word_links}
        tmp_file = MEMORY_FILE + ".tmp"
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, MEMORY_FILE)

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

def load_all_data():
    """从文件加载记忆和链接"""
    global memories, links
    with _io_lock:
        if not os.path.exists(MEMORY_FILE):
            return
        with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        memories.clear()
        from core.memory_engine import hot_ids
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
        from core.memory_engine import _build_word_to_memories
        _build_word_to_memories()

        # faiss持久化
        from core.memory_engine import (
            _faiss_index, _faiss_to_mem, _mem_to_faiss,
            _init_faiss_index, _rebuild_faiss_index
        )
        import faiss
        if os.path.exists(FAISS_INDEX_FILE) and os.path.exists(FAISS_MAPPING_FILE):
            # 读取索引
            _faiss_index_global = faiss.read_index(FAISS_INDEX_FILE)
            # 读取映射表
            with open(FAISS_MAPPING_FILE, 'r', encoding='utf-8') as f:
                loaded_mapping = json.load(f)
            # 验证长度一致性
            if len(loaded_mapping) == _faiss_index_global.ntotal:
                # 赋值给全局变量
                import core.memory_engine as me
                me._faiss_index = _faiss_index_global
                me._faiss_to_mem = loaded_mapping
                # 重建反向映射
                me._mem_to_faiss = {mem_id: idx for idx, mem_id in enumerate(loaded_mapping)}
            else:
                print("[持久化] faiss 索引与映射表不一致，将全量重建索引")
                _rebuild_faiss_index()
        else:
            # 没有 faiss 文件，可能是首次运行或旧版本，全量重建
            print("[持久化] 未找到 faiss 文件，全量重建索引")
            _rebuild_faiss_index()
        
        if os.path.exists(METRICS_COUNTERS_FILE):
            with open(METRICS_COUNTERS_FILE, 'r', encoding='utf-8') as f:
                metrics = json.load(f)
            from core.memory_engine import _import_metrics_counters
            _import_metrics_counters(metrics)

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
    """
    now = clock.now()
    removed_links = 0
    removed_wordweb = 0

    # 1. 链接剪枝
    dead_links = []
    for key, data in links.items():
        delta = now - data.get("last_accessed", data.get("creation_time", now))
        decay = 2 ** (-delta / (7 * 24 * 3600))  # LINK_HALF_LIFE
        if data["weight"] * decay < 0.01:
            dead_links.append(key)
    for key in dead_links:
        del links[key]
        removed_links += 1
        from core.memory_engine import _count_link_deleted
        _count_link_deleted += 1

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
    db.commit()
    # 重建 faiss 索引（确保与 SQLite 一致）
    from core.memory_engine import _rebuild_faiss_index
    _rebuild_faiss_index()
    # 删除旧 JSON 文件
    os.remove(MEMORY_FILE)
    print("[迁移] JSON 数据已全部迁移至 SQLite，faiss 索引已重建。")