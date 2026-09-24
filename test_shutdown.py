"""
test_shutdown.py
验证关停优化：
  - links 增量写入的正确性（新增/更新/删除/下沉都不会丢失或残留）
  - save_all_data 的节流与 force 语义
  - 增量写入相对全量写入的耗时优势
所有用例都在临时数据库副本上运行，不触碰真实数据。
"""
import os
import shutil
import sqlite3
import sys
import time
import unittest

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

TMP_DIR = os.path.join(PROJECT_DIR, "data", "test", "_shutdown_test")
REAL_DB = os.path.join(PROJECT_DIR, "data", "test", "memory.db")


class IncrementalSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(REAL_DB):
            raise unittest.SkipTest("没有可用的 memory.db")
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        os.makedirs(TMP_DIR, exist_ok=True)
        cls.db_path = os.path.join(TMP_DIR, "memory.db")
        shutil.copy2(REAL_DB, cls.db_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    def setUp(self):
        import core.memory_engine as me
        import utils.persistence as P
        # 每次用例都从干净副本开始，避免相互干扰
        shutil.copy2(REAL_DB, self.db_path)
        me._db_conn = None
        me.DB_FILE = self.db_path
        me.links.clear()
        me._dirty_links.clear()
        me._deleted_links.clear()
        me.hot_ids.clear()
        me.memories.clear()
        P.MEMORY_FILE = os.path.join(TMP_DIR, "memory.json")
        P.FAISS_INDEX_FILE = os.path.join(TMP_DIR, "faiss.index")
        P.FAISS_MAPPING_FILE = os.path.join(TMP_DIR, "faiss_mapping.json")
        P.METRICS_COUNTERS_FILE = os.path.join(TMP_DIR, "metrics.json")
        self.me = me
        self.P = P

    def tearDown(self):
        if self.me._db_conn is not None:
            self.me._db_conn.close()
            self.me._db_conn = None

    def _db(self):
        return self.me._get_db()

    def _row(self, src, tgt):
        return self._db().execute(
            "SELECT weight, type FROM links WHERE src=? AND tgt=?", (src, tgt)
        ).fetchone()

    def _count(self):
        return self._db().execute("SELECT COUNT(*) FROM links").fetchone()[0]

    def test_add_link_persists(self):
        before = self._count()
        self.me.add_link("aaa-1", "bbb-1", 0.5, "semantic")
        self.P.save_all_data(force=True)
        self.assertEqual(self._count(), before + 1)
        self.assertIsNotNone(self._row("aaa-1", "bbb-1"))

    def test_remove_dirty_after_save(self):
        """保存后脏标记应清空，重复保存不应重复写"""
        self.me.add_link("aaa-2", "bbb-2", 0.5, "semantic")
        self.P.save_all_data(force=True)
        self.assertEqual(len(self.me._dirty_links), 0)
        self.assertEqual(len(self.me._deleted_links), 0)
        n = self._count()
        self.P.save_all_data(force=True)
        self.assertEqual(self._count(), n, "无变更的保存不应改变数据")

    def test_weight_update_overwrites(self):
        self.me.add_link("aaa-3", "bbb-3", 0.5, "semantic")
        self.P.save_all_data(force=True)
        self.me.add_link("aaa-3", "bbb-3", 0.9, "causal")
        self.P.save_all_data(force=True)
        row = self._row("aaa-3", "bbb-3")
        self.assertAlmostEqual(row[0], 0.59, places=6)  # 0.5 + 0.1*0.9
        self.assertEqual(row[1], "causal")

    def test_decay_below_threshold_removes_from_db(self):
        """衰减到阈值以下的链接应从 SQLite 一并删除"""
        self.me.add_link("aaa-4", "bbb-4", 0.5, "semantic")
        self.P.save_all_data(force=True)
        self.assertIsNotNone(self._row("aaa-4", "bbb-4"))
        # 强制衰减到 0 权重，触发删除分支
        self.me.links[("aaa-4", "bbb-4")]["weight"] = 0.0
        self.me.decay_link("aaa-4", "bbb-4")
        self.P.save_all_data(force=True)
        self.assertIsNone(self._row("aaa-4", "bbb-4"), "衰减删除的链接不应残留在库中")

    def test_save_throttle_skips_repeat(self):
        """节流：短时间内重复保存被跳过，force 可强制写入"""
        self.P.load_all_data()
        self.me._dirty_links.clear()
        self.P.save_all_data(force=True)
        first = self.P.save_all_data()
        self.assertFalse(first, "紧接的重复保存应被节流跳过")
        forced = self.P.save_all_data(force=True)
        self.assertTrue(forced, "force=True 应无视节流")

    def test_incremental_is_far_cheaper_than_full(self):
        """增量写入应显著快于全量重写（这是关停提速的核心）"""
        before = self._count()
        self.me.add_link("aaa-9", "bbb-9", 0.5, "semantic")

        t0 = time.time()
        self.me._sync_all_links_to_sqlite()
        inc = time.time() - t0

        t0 = time.time()
        self.me._sync_all_links_to_sqlite(full=True)
        full = time.time() - t0

        self.assertEqual(self._count(), before + 1)
        # 9 万行量级下增量应是毫秒级；这里放宽到全量的 1/5 作为稳健断言
        self.assertLess(inc, max(full / 5, 0.5), f"增量 {inc:.3f}s 未显著快于全量 {full:.3f}s")

    def test_full_sync_still_consistent(self):
        """全量模式作为兜底仍可用，且结果与增量一致"""
        before = self._count()
        self.me.add_link("aaa-11", "bbb-11", 0.4, "temporal")
        self.me._sync_all_links_to_sqlite(full=True)
        self.assertEqual(self._count(), before + 1)
        self.assertIsNotNone(self._row("aaa-11", "bbb-11"))

    def test_redundant_src_index_dropped(self):
        """冗余的 idx_links_src 应已被移除，而 idx_links_tgt 保留"""
        names = [r[0] for r in self._db().execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='links'"
        ).fetchall()]
        self.assertNotIn("idx_links_src", names)
        self.assertIn("idx_links_tgt", names)

    def test_synchronous_pragma_is_normal(self):
        mode = self._db().execute("PRAGMA synchronous").fetchone()[0]
        self.assertEqual(mode, 1, "应设为 NORMAL(1) 以加快批量写入")


if __name__ == "__main__":
    unittest.main()
