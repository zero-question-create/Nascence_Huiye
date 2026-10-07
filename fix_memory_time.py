# fix_memory_time.py
# ========================================================================
# Nascence 辉夜 - 离线记忆时间戳平移与修复工具
#
# 支持当前架构完整体系（v0.6.5+ / v0.7+）：
#   1. 核心记忆节点（SQLite memories 表 & memory.json 热缓存）
#   2. 图链接网络（SQLite links 表 & memory.json 中的关联边）
#   3. 字词共现网（memory.json 中的 wordweb last_updated）
#   4. 概念层体系（SQLite concepts 表 & concept_events 事件段起止时间）
#   5. 可选对话历史（message_state.json 短期对话记录时间戳）
#
# 安全特性：
#   - 进程互斥保护：自动检测单实例锁，防止在 Bot/控制面板运行期间并发修改导致数据冲突
#   - 自动数据备份：执行前在 data/test/ 下自动生成时间戳备份目录，支持一键恢复
#   - 事务与原子写入：SQLite 使用单事务整体提交，JSON 采用临时文件原子替换，出错自动回滚
#   - 时钟基准免疫：不篡改 virtual_clock 全局基准偏移，时间短语自然映射
#   - 向量特征免疫：不破坏 768 维特征向量，无需耗时重建 FAISS 索引
#
# 用法：
#   python fix_memory_time.py                  # 默认向过去调整 24 小时（1 天）
#   python fix_memory_time.py --offset-days 3  # 向过去调整 3 天
#   python fix_memory_time.py --offset-hours 2 # 向过去调整 2 小时
#   python fix_memory_time.py --offset-seconds -3600  # 负数表示向未来方向调整 1 小时
#   python fix_memory_time.py --include-messages      # 连同短期对话记录一并平移
# ========================================================================

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
os.chdir(PROJECT_DIR)

# 默认目标修复偏移时间（秒）：正数表示将记忆向更早（过去）调整，负数表示向更晚（未来）调整。
# 保留模块级常量，兼容习惯直接修改本行代码的用户。
OFFSET_SECONDS = 24 * 3600

DATA_DIR = PROJECT_DIR / "data" / "test"
DB_FILE = DATA_DIR / "memory.db"
MEMORY_FILE = DATA_DIR / "memory.json"
MESSAGE_STATE_FILE = DATA_DIR / "message_state.json"
CLOCK_STATE_FILE = DATA_DIR / "clock_state.txt"


def make_backup(data_dir: Path) -> Path | None:
    """执行前在数据目录下创建时间戳备份文件夹。"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = data_dir / f"backup_before_timefix_{timestamp}"
    files_to_backup = ["memory.db", "memory.json", "message_state.json", "clock_state.txt"]
    backed = []
    for fname in files_to_backup:
        fpath = data_dir / fname
        if fpath.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fpath, backup_dir / fname)
            backed.append(fname)
    if backed:
        print(f"[备份] 已创建数据备份目录: {backup_dir.name} (包含: {', '.join(backed)})")
        return backup_dir
    return None


def table_exists(db: sqlite3.Connection, table_name: str) -> bool:
    cur = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    return cur.fetchone() is not None


def update_sqlite(db: sqlite3.Connection, offset_seconds: float) -> dict:
    """在单个事务中整体平移 SQLite 数据库中所有时间戳字段。"""
    counts = {}
    with db:
        # 1. memories 表: creation_time, last_accessed, last_strengthen_time
        if table_exists(db, "memories"):
            cur = db.execute("""
                UPDATE memories SET
                    creation_time = creation_time - ?,
                    last_accessed = last_accessed - ?,
                    last_strengthen_time = last_strengthen_time - ?
            """, (offset_seconds, offset_seconds, offset_seconds))
            counts["memories"] = cur.rowcount

        # 2. links 表: creation_time, last_accessed
        if table_exists(db, "links"):
            cur = db.execute("""
                UPDATE links SET
                    creation_time = creation_time - ?,
                    last_accessed = last_accessed - ?
            """, (offset_seconds, offset_seconds))
            counts["links"] = cur.rowcount

        # 3. concepts 表: creation_time, last_accessed
        if table_exists(db, "concepts"):
            cur = db.execute("""
                UPDATE concepts SET
                    creation_time = creation_time - ?,
                    last_accessed = last_accessed - ?
            """, (offset_seconds, offset_seconds))
            counts["concepts"] = cur.rowcount

        # 4. concept_events 表: start_time, end_time
        if table_exists(db, "concept_events"):
            cur = db.execute("""
                UPDATE concept_events SET
                    start_time = start_time - ?,
                    end_time = end_time - ?
            """, (offset_seconds, offset_seconds))
            counts["concept_events"] = cur.rowcount

    return counts


def update_memory_json(json_path: Path, offset_seconds: float) -> dict:
    """原子更新 memory.json 热缓存文件（含记忆、链接边和字词共现网）。"""
    if not json_path.exists():
        return {}

    with open(json_path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)

    counts = {"memories": 0, "links": 0, "wordweb": 0}

    # 1. 记忆热节点（注意避免原版本双重扣减的 bug）
    memories_dict = data.get("memories", {})
    for mid, mem in memories_dict.items():
        if isinstance(mem, dict):
            orig_creation = float(mem.get("creation_time", 0.0) or 0.0)
            orig_accessed = float(mem.get("last_accessed", orig_creation) or orig_creation)
            orig_strengthen = float(mem.get("last_strengthen_time", orig_creation) or orig_creation)
            mem["creation_time"] = orig_creation - offset_seconds
            mem["last_accessed"] = orig_accessed - offset_seconds
            mem["last_strengthen_time"] = orig_strengthen - offset_seconds
            counts["memories"] += 1

    # 2. 热链接边
    links_dict = data.get("links", {})
    for k, link in links_dict.items():
        if isinstance(link, dict):
            orig_c = float(link.get("creation_time", 0.0) or 0.0)
            orig_a = float(link.get("last_accessed", orig_c) or orig_c)
            link["creation_time"] = orig_c - offset_seconds
            link["last_accessed"] = orig_a - offset_seconds
            counts["links"] += 1

    # 3. 字词共现网络更新时间戳
    wordweb_dict = data.get("wordweb", {})
    for k, wentry in wordweb_dict.items():
        if isinstance(wentry, dict) and "last_updated" in wentry:
            orig_u = float(wentry.get("last_updated", 0.0) or 0.0)
            wentry["last_updated"] = orig_u - offset_seconds
            counts["wordweb"] += 1

    tmp_file = json_path.with_suffix(".tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, json_path)

    return counts


def update_message_state(state_path: Path, offset_seconds: float) -> int:
    """可选：同步平移短期对话历史记录的时间戳。"""
    if not state_path.exists():
        return 0

    with open(state_path, "r", encoding="utf-8-sig") as f:
        raw = json.load(f)

    messages = raw.get("messages", []) if isinstance(raw, dict) else raw
    if not isinstance(messages, list):
        return 0

    count = 0
    for msg in messages:
        if isinstance(msg, dict) and "time" in msg and msg["time"] is not None:
            msg["time"] = float(msg["time"]) - offset_seconds
            count += 1

    tmp_file = state_path.with_suffix(".tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, state_path)

    return count


def fix_all(
    offset_seconds: float = OFFSET_SECONDS,
    include_messages: bool = False,
    no_backup: bool = False,
    force: bool = False,
) -> bool:
    """执行完整记忆时间戳修复流程。"""
    direction = "更早（过去）" if offset_seconds >= 0 else "更晚（未来）"
    abs_hours = abs(offset_seconds) / 3600
    abs_days = abs_hours / 24

    print("==================================================")
    print("  Nascence 辉夜 · 离线记忆时间戳平移与修复")
    print(f"  调整偏移: {offset_seconds:+.1f} 秒 ({abs_days:.2f} 天 / {abs_hours:.2f} 小时，向{direction})")
    print("==================================================")

    # 1. 进程互斥锁校验：防止服务运行中并发读写导致数据损坏
    lock_file = None
    if not force:
        try:
            from panel_runtime import acquire_panel_lock
            lock_file = acquire_panel_lock()
            if lock_file is None:
                print("[错误] 检测到控制面板或 QQ Bot 服务正在运行！")
                print("       请先停止运行中的面板/服务后再执行时间戳修复，以免并发冲突导致数据损坏。")
                print("       (如确信无进程运行，可加 --force 参数强制跳过)")
                return False
        except Exception:
            pass

    try:
        if not DB_FILE.exists() and not MEMORY_FILE.exists():
            print("[错误] 未找到记忆数据库或缓存文件 (data/test/)，退出。")
            return False

        # 2. 自动安全备份
        if not no_backup:
            make_backup(DATA_DIR)

        # 3. 记录修改前效果示例
        sample_before_text = None
        if DB_FILE.exists():
            try:
                db_probe = sqlite3.connect(DB_FILE)
                row = db_probe.execute(
                    "SELECT creation_time, content FROM memories ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                if row:
                    from core.virtual_clock import clock
                    from utils.time_phrases import get_relative_time_phrase
                    c_time, content = row
                    phrase = get_relative_time_phrase(clock.to_real_time(c_time))
                    sample_before_text = (phrase, content[:30])
                db_probe.close()
            except Exception:
                pass

        # 4. 同步更新 SQLite 数据库
        if DB_FILE.exists():
            db = sqlite3.connect(DB_FILE)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=NORMAL")
                counts = update_sqlite(db, offset_seconds)
                print(f"[SQLite] 数据库已更新: memories={counts.get('memories', 0)} 条, "
                      f"links={counts.get('links', 0)} 条, "
                      f"concepts={counts.get('concepts', 0)} 个, "
                      f"events={counts.get('concept_events', 0)} 段")

                # 完整性自检
                check = db.execute("PRAGMA integrity_check").fetchone()
                if check and check[0] != "ok":
                    raise RuntimeError(f"数据库完整性检查异常: {check[0]}")
            finally:
                db.close()

        # 5. 原子更新 memory.json 热缓存
        if MEMORY_FILE.exists():
            jcounts = update_memory_json(MEMORY_FILE, offset_seconds)
            print(f"[JSON] 缓存文件已更新: memories={jcounts.get('memories', 0)} 条, "
                  f"links={jcounts.get('links', 0)} 条, "
                  f"wordweb={jcounts.get('wordweb', 0)} 条")

        # 6. 可选：更新短期对话历史
        if include_messages and MESSAGE_STATE_FILE.exists():
            mcount = update_message_state(MESSAGE_STATE_FILE, offset_seconds)
            print(f"[对话历史] message_state.json 已平移: {mcount} 条消息")

        # 7. 刷新概念层内存索引
        try:
            from core.concept_store import reload_from_db
            reload_from_db()
        except Exception:
            pass

        # 8. 展示示例对比
        if sample_before_text:
            try:
                db_probe = sqlite3.connect(DB_FILE)
                row = db_probe.execute(
                    "SELECT creation_time FROM memories ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                if row:
                    from core.virtual_clock import clock
                    from utils.time_phrases import get_relative_time_phrase
                    new_phrase = get_relative_time_phrase(clock.to_real_time(row[0]))
                    print(f"[效果预览] 最新一条记忆「{sample_before_text[1]}...」")
                    print(f"           相对时间短语从「{sample_before_text[0]}」更新为「{new_phrase}」")
                db_probe.close()
            except Exception:
                pass

        print("==================================================")
        print("  [完成] 记忆体系时间戳已成功同步平移！")
        print("  提示: FAISS 向量索引与特征向量无需重建，现在可以正常启动 Bot。")
        print("==================================================")
        return True

    finally:
        if lock_file:
            try:
                from panel_runtime import release_panel_lock
                release_panel_lock(lock_file)
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Nascence 辉夜 · 离线记忆时间戳平移与修复工具")
    parser.add_argument("--offset-seconds", type=float, default=None,
                        help="平移时间（秒），正数表示向更早（过去）调整，负数向未来")
    parser.add_argument("--offset-hours", type=float, default=None,
                        help="平移时间（小时），指定时优先于 offset-seconds")
    parser.add_argument("--offset-days", type=float, default=None,
                        help="平移时间（天数），指定时优先于 offset-hours")
    parser.add_argument("--include-messages", action="store_true",
                        help="是否连同短期对话记录的时间戳一并平移")
    parser.add_argument("--no-backup", action="store_true",
                        help="跳过执行前自动创建数据备份")
    parser.add_argument("--force", action="store_true",
                        help="跳过单实例运行锁检查（请确保无并发进程）")

    args = parser.parse_args()

    # 计算有效平移秒数
    if args.offset_days is not None:
        offset = args.offset_days * 86400.0
    elif args.offset_hours is not None:
        offset = args.offset_hours * 3600.0
    elif args.offset_seconds is not None:
        offset = float(args.offset_seconds)
    else:
        offset = float(OFFSET_SECONDS)

    success = fix_all(
        offset_seconds=offset,
        include_messages=args.include_messages,
        no_backup=args.no_backup,
        force=args.force,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
