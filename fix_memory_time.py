# fix_memory_time.py
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.memory_engine import _get_db, _rebuild_faiss_index, memories, hot_ids
from utils.persistence import MEMORY_FILE, load_all_data, save_all_data
from core.virtual_clock import clock

OFFSET_SECONDS = 24 * 3600          #！！！！！！目标修复偏移时间，正数为“将记忆所有的时间向更早的时间调整”，反之则反之！！！！！！

def fix_all():
    print("=== 开始修复记忆时间戳 ===")

    # 1. 加载现有数据（从 JSON，因为 JSON 是热数据的主副本）
    if not os.path.exists(MEMORY_FILE):
        print("未找到记忆文件，退出。")
        return
    with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)

    memories_json = data.get("memories", {})
    print(f"JSON 中记忆数量：{len(memories_json)}")

    # 2. 修改 JSON 中每条记忆的时间字段
    for mem_id, mem in memories_json.items():
        mem["creation_time"] = mem.get("creation_time", 0) - OFFSET_SECONDS
        mem["last_accessed"] = mem.get("last_accessed", 0) - OFFSET_SECONDS
        mem["last_strengthen_time"] = mem.get("last_strengthen_time", mem["creation_time"]) - OFFSET_SECONDS

    # 3. 写回 JSON（原子写入，防止损坏）
    tmp_file = MEMORY_FILE + ".tmp"
    with open(tmp_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, MEMORY_FILE)
    print("JSON 文件已更新。")

    # 4. 同步更新 SQLite
    db = _get_db()
    db.execute("""
        UPDATE memories SET
            creation_time = creation_time - ?,
            last_accessed = last_accessed - ?,
            last_strengthen_time = last_strengthen_time - ?
    """, (OFFSET_SECONDS, OFFSET_SECONDS, OFFSET_SECONDS))
    db.commit()
    print("SQLite 数据库已更新。")

    # 5. 刷新内存（重新加载 JSON 到 memories 字典，并重建 faiss 索引）
    global memories, hot_ids
    memories.clear()
    hot_ids.clear()
    load_all_data()  # 这会从刚刚修改的 JSON 重新加载热数据
    _rebuild_faiss_index()
    print("内存和 faiss 索引已重建。")

    # 6. 最后做一次完整保存，确保所有数据一致
    save_all_data()
    from utils.persistence import save_state
    save_state()
    print("=== 修复完成，请重启 Bot ===")

if __name__ == "__main__":
    # 初始化必要的模块（虚拟时钟、Ollama 等可跳过，但我们只需要改数据，不需要模型）
    # 确保 clock 已初始化，以免后续调用出错
    from core.virtual_clock import clock
    import time
    clock._qq_real_offset = time.time() - clock.now()
    clock.save_state()
    clock.enable_qq_mode()  # 使偏移量固定
    fix_all()