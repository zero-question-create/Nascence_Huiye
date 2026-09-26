"""
test_concept_store.py
验证概念层存储：概念查重归并、occurrences 截断、事件段双重切分、时间窗筛选与降级。
所有用例在临时数据库上运行，不触碰真实数据。
"""
import json
import os
import shutil
import sqlite3
import sys
import unittest
from unittest.mock import patch

import numpy as np

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from core import concept_store as CS

TMP = os.path.join(PROJECT_DIR, "data", "test", "_concept_test")


def _fake_vec(text, dim=CS.EMBED_DIM):
    """确定性假向量：同样文本给同样向量，便于验证归并。

    用文本的字符哈希铺满，保证不同文本相似度接近 0，相同文本相似度 1。
    """
    seed = abs(hash(str(text))) % (2 ** 31)
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    n = np.linalg.norm(v)
    return (v / n).tolist()


class ConceptStoreBase(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        os.makedirs(TMP, exist_ok=True)
        self.db_path = os.path.join(TMP, "memory.db")
        self._orig_db = CS.DB_FILE
        CS.DB_FILE = self.db_path
        CS._db_conn = None
        CS._concept_faiss = None
        CS._faiss_to_concept = []
        CS._concept_to_faiss = {}
        # 打桩向量化，避免依赖 Ollama
        self._vec_patch = patch.object(CS, "_embed", side_effect=lambda t: _norm(_fake_vec(t)))
        self._vec_patch.start()

        # 造一个最小 memories 表，供事件段时间戳查询
        db = CS._get_db()
        db.execute("CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, creation_time REAL NOT NULL)")
        db.commit()

    def tearDown(self):
        self._vec_patch.stop()
        if CS._db_conn is not None:
            CS._db_conn.close()
            CS._db_conn = None
        CS.DB_FILE = self._orig_db
        shutil.rmtree(TMP, ignore_errors=True)

    def _add_memories(self, ids, base_time=1000.0, step=5.0):
        db = CS._get_db()
        for i, mid in enumerate(ids):
            db.execute("INSERT OR REPLACE INTO memories (id, creation_time) VALUES (?, ?)",
                       (mid, base_time + i * step))
        db.commit()


def _norm(arr):
    v = np.array(arr, dtype=np.float32).reshape(1, -1)
    n = np.linalg.norm(v)
    if n:
        v = v / n
    return v


class TestConceptCreateAndMerge(ConceptStoreBase):
    def test_create_concept(self):
        self._add_memories(["m1", "m2"])
        cid = CS.record_concept("周圻晨在催物理作业", ["m1", "m2"])
        self.assertIsNotNone(cid)
        c = CS.get_concept(cid)
        self.assertEqual(c["name"], "周圻晨在催物理作业")
        self.assertEqual(c["total_count"], 1)
        self.assertEqual(c["member_count"], 2)
        self.assertEqual(len(c["occurrences"]), 1)

    def test_same_name_merges(self):
        """同名概念应归并，而不是重复建"""
        self._add_memories(["m1", "m2"])
        cid1 = CS.record_concept("催作业", ["m1"])
        cid2 = CS.record_concept("催作业", ["m2"])
        self.assertEqual(cid1, cid2, "同名应归并到同一概念")
        c = CS.get_concept(cid1)
        self.assertEqual(c["total_count"], 2)
        self.assertEqual(c["member_count"], 2)

    def test_different_name_creates_new(self):
        self._add_memories(["m1", "m2"])
        cid1 = CS.record_concept("催物理作业", ["m1"])
        cid2 = CS.record_concept("完全无关的天气话题", ["m2"])
        self.assertNotEqual(cid1, cid2)
        self.assertEqual(CS.stats()["concepts"], 2)

    def test_empty_name_skips(self):
        """概念名为空时不建概念（该批记忆不入概念层）"""
        self._add_memories(["m1"])
        self.assertIsNone(CS.record_concept("", ["m1"]))
        self.assertIsNone(CS.record_concept("   ", ["m1"]))
        self.assertEqual(CS.stats()["concepts"], 0)

    def test_empty_members_skips(self):
        self.assertIsNone(CS.record_concept("某概念", []))
        self.assertEqual(CS.stats()["concepts"], 0)

    def test_merge_appends_occurrences(self):
        self._add_memories(["m1", "m2"])
        cid = CS.record_concept("催作业", ["m1"], timestamp=1000.0)
        CS.record_concept("催作业", ["m2"], timestamp=2000.0)
        c = CS.get_concept(cid)
        self.assertEqual(len(c["occurrences"]), 2)
        self.assertIn(2000.0, c["occurrences"])

    def test_occurrences_capped(self):
        """occurrences 只保留最近 MAX_OCCURRENCES 个，总数另记"""
        ids = [f"m{i}" for i in range(CS.MAX_OCCURRENCES + 8)]
        self._add_memories(ids)
        cid = None
        for i, mid in enumerate(ids):
            cid = CS.record_concept("反复出现的事", [mid], timestamp=1000.0 + i)
        c = CS.get_concept(cid)
        self.assertEqual(len(c["occurrences"]), CS.MAX_OCCURRENCES)
        self.assertEqual(c["total_count"], CS.MAX_OCCURRENCES + 8)
        # 保留的应是最新的那些
        self.assertEqual(c["occurrences"][-1], 1000.0 + len(ids) - 1)


class TestEventSegmentation(ConceptStoreBase):
    def test_split_by_gap(self):
        """相邻间隔超过 EPISODE_GAP 应切成两段"""
        ids = ["m1", "m2", "m3"]
        db = CS._get_db()
        # m1,m2 相近；m3 远隔
        for mid, t in zip(ids, [1000.0, 1010.0, 1000.0 + CS.EPISODE_GAP + 100]):
            db.execute("INSERT INTO memories (id, creation_time) VALUES (?, ?)", (mid, t))
        db.commit()
        cid = CS.record_concept("话题", ids)
        c = CS.get_concept(cid)
        self.assertEqual(len(c["event_ids"]), 2, f"应切成 2 段，实际 {len(c['event_ids'])}")

    def test_split_by_cap(self):
        """单段累积到 EPISODE_CAP 应自动断开"""
        n = CS.EPISODE_CAP * 2 + 3
        ids = [f"m{i}" for i in range(n)]
        self._add_memories(ids, base_time=1000.0, step=1.0)   # 间隔很小，只触发条数上限
        cid = CS.record_concept("超长话题", ids)
        c = CS.get_concept(cid)
        self.assertEqual(len(c["event_ids"]), 3, f"{n} 条应切成 3 段，实际 {len(c['event_ids'])}")

    def test_no_split_when_continuous_and_small(self):
        ids = ["a", "b", "c"]
        self._add_memories(ids, base_time=1000.0, step=3.0)
        cid = CS.record_concept("短话题", ids)
        self.assertEqual(len(CS.get_concept(cid)["event_ids"]), 1)

    def test_consecutive_batch_merges_into_last_event(self):
        """同一次交互分两批写入时，应续接到同一段而非新开"""
        self._add_memories(["m1", "m2"], base_time=1000.0, step=5.0)
        cid = CS.record_concept("连续话题", ["m1"])
        self._add_memories(["m3"], base_time=1008.0)   # 距上次 8 秒，远小于 GAP
        CS.record_concept("连续话题", ["m3"])
        c = CS.get_concept(cid)
        self.assertEqual(len(c["event_ids"]), 1, "间隔很短的后续批次应并入同一段")
        ev = CS._load_event(c["event_ids"][0])
        self.assertEqual(sorted(ev["member_ids"]), ["m1", "m3"])


class TestTimeWindowSelection(ConceptStoreBase):
    def setUp(self):
        super().setUp()
        self.ids = ["m1", "m2", "m3"]
        # 三段分别对应"半小时前 / 1 天前 / 10 天前"（用虚拟秒，_to_real 打桩为恒等）
        # 取半小时而非整 1 小时，避免恰好落在 latest 窗口边界上因测试耗时被判出界
        import time as _t
        now = _t.time()
        db = CS._get_db()
        offsets = [1800, 86400, 10 * 86400]
        for mid, off in zip(self.ids, offsets):
            db.execute("INSERT INTO memories (id, creation_time) VALUES (?, ?)", (mid, now - off))
        db.commit()
        # 逐条写入使它们落在不同段（间隔很大）
        self.cid = None
        for mid in self.ids:
            self.cid = CS.record_concept("物理作业", [mid])
        self._patch_real()

    def _patch_real(self):
        import time as _t
        self._real_patch = patch.object(CS, "_to_real", side_effect=lambda v: v)
        self._real_patch.start()

    def tearDown(self):
        self._real_patch.stop()
        super().tearDown()

    def test_none_takes_recent(self):
        events, degraded = CS.select_episodes(self.cid, "none", limit=3)
        self.assertFalse(degraded)
        self.assertEqual(len(events), 3)

    def test_latest_window(self):
        """latest（1 小时内）只应命中最近那段"""
        events, degraded = CS.select_episodes(self.cid, "latest", limit=3)
        self.assertFalse(degraded)
        self.assertEqual(len(events), 1)

    def test_recent_window(self):
        """recent（2 天内）应命中 1 小时前与 1 天前两段"""
        events, degraded = CS.select_episodes(self.cid, "recent", limit=3)
        self.assertFalse(degraded)
        self.assertEqual(len(events), 2)

    def test_earlier_window(self):
        """earlier（3~14 天前）应只命中 10 天前那段"""
        events, degraded = CS.select_episodes(self.cid, "earlier", limit=3)
        self.assertFalse(degraded)
        self.assertEqual(len(events), 1)

    def test_empty_range_degrades_to_recent(self):
        """时间窗内无记录时降级为最近若干段，并标记 degraded"""
        events, degraded = CS.select_episodes(self.cid, "earlier", limit=3)
        # 先消耗掉 10 天前那段：改成只问 3 天内的 earliest 场景不好构造，
        # 这里改用不存在的窗口验证降级逻辑
        CS.TIME_INTENT_WINDOWS["__test_gap__"] = (100 * 86400, 200 * 86400)
        try:
            CS.VALID_TIME_INTENTS.add("__test_gap__")
            events2, degraded2 = CS.select_episodes(self.cid, "__test_gap__", limit=3)
        finally:
            CS.TIME_INTENT_WINDOWS.pop("__test_gap__", None)
            CS.VALID_TIME_INTENTS.discard("__test_gap__")
        self.assertTrue(degraded2, "窗口内无记录应标记降级")
        self.assertGreater(len(events2), 0, "降级后仍应带回最近若干段")

    def test_invalid_intent_falls_back_to_none(self):
        events, degraded = CS.select_episodes(self.cid, "完全不存在的枚举", limit=2)
        self.assertEqual(len(events), 2)


class TestFetchMembers(ConceptStoreBase):
    def test_fetch_orders_newest_first(self):
        """段内与段间都按时间倒序（新→旧）"""
        self._add_memories(["old", "new"], base_time=1000.0, step=500.0)
        cid = CS.record_concept("话题", ["old", "new"])
        events, _ = CS.select_episodes(cid, "none", limit=3)
        # 打桩 memories 查询
        fake = {
            "old": {"id": "old", "content": "旧", "creation_time": 1000.0},
            "new": {"id": "new", "content": "新", "creation_time": 1500.0},
        }
        with patch.dict("core.memory_engine.memories", fake, clear=True), \
             patch.object(CS, "_to_real", side_effect=lambda v: v):
            got = CS.fetch_members(events)
        self.assertEqual([m["id"] for m, _ in got], ["new", "old"])

    def test_fetch_respects_max(self):
        ids = [f"m{i}" for i in range(50)]
        self._add_memories(ids)
        cid = CS.record_concept("大批量", ids)
        events, _ = CS.select_episodes(cid, "none", limit=10)
        fake = {mid: {"id": mid, "content": "x", "creation_time": 1000.0} for mid in ids}
        with patch.dict("core.memory_engine.memories", fake, clear=True), \
             patch.object(CS, "_to_real", side_effect=lambda v: v):
            got = CS.fetch_members(events, max_members=7)
        self.assertEqual(len(got), 7)


class TestMatchConcept(ConceptStoreBase):
    def test_exact_match(self):
        self._add_memories(["m1"])
        cid = CS.record_concept("物理作业", ["m1"])
        got_id, name, how = CS.match_concept("物理作业")
        self.assertEqual(got_id, cid)
        self.assertEqual(how, "exact")

    def test_partial_match(self):
        self._add_memories(["m1"])
        cid = CS.record_concept("周圻晨在催物理作业", ["m1"])
        got_id, name, how = CS.match_concept("物理作业")
        self.assertEqual(got_id, cid)
        self.assertEqual(how, "exact")

    def test_no_match_returns_none(self):
        self._add_memories(["m1"])
        CS.record_concept("物理作业", ["m1"])
        got_id, name, how = CS.match_concept("完全无关的股票行情")
        self.assertIsNone(got_id)

    def test_empty_query(self):
        self.assertEqual(CS.match_concept(""), (None, None, None))


if __name__ == "__main__":
    unittest.main()
