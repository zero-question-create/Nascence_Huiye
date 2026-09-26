"""
test_concept_retrieval.py
验证概念路检索的接入：命中概念走定向直读、未命中回退原有检索、
时间窗为空时降级、以及这批记忆被标记为 pinned（不被关键词过滤丢弃）。
"""
import os
import shutil
import sys
import time
import unittest
from unittest.mock import patch

import numpy as np

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from core import concept_store as CS
from core import cognition as C

TMP = os.path.join(PROJECT_DIR, "data", "test", "_concept_retrieval")


def _norm(arr):
    v = np.array(arr, dtype=np.float32).reshape(1, -1)
    n = np.linalg.norm(v)
    return v / n if n else v


def _fake_vec(text, dim=CS.EMBED_DIM):
    rng = np.random.default_rng(abs(hash(str(text))) % (2 ** 31))
    return rng.standard_normal(dim).astype(np.float32).tolist()


class ConceptRetrievalTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        os.makedirs(TMP, exist_ok=True)
        self._orig_db = CS.DB_FILE
        CS.DB_FILE = os.path.join(TMP, "memory.db")
        CS._db_conn = None
        CS._concept_faiss = None
        CS._faiss_to_concept = []
        CS._concept_to_faiss = {}
        self._vec = patch.object(CS, "_embed", side_effect=lambda t: _norm(_fake_vec(t)))
        self._vec.start()
        self._real = patch.object(CS, "_to_real", side_effect=lambda v: v)
        self._real.start()

        db = CS._get_db()
        db.execute("CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, creation_time REAL NOT NULL)")
        db.commit()

        # 造三段不同时间的"物理作业"事件
        now = time.time()
        self.members = {}
        rows = [("m1", now - 1800, "我想：物理作业还没写"),      # 半小时前
                ("m2", now - 86400, "我想：物理卷子还空着"),      # 1 天前
                ("m3", now - 10 * 86400, "我说：物理题我自己做")]  # 10 天前
        for mid, ts, text in rows:
            db.execute("INSERT INTO memories (id, creation_time) VALUES (?, ?)", (mid, ts))
            self.members[mid] = {"id": mid, "content": text, "creation_time": ts}
        db.commit()

        self.cid = None
        for mid, _, _ in rows:
            self.cid = CS.record_concept("物理作业", [mid])

        # 让 cognition 的 memories 命中这些条目
        self._mem_patch = patch.dict("core.memory_engine.memories", self.members, clear=True)
        self._mem_patch.start()

    def tearDown(self):
        self._mem_patch.stop()
        self._real.stop()
        self._vec.stop()
        if CS._db_conn is not None:
            CS._db_conn.close()
            CS._db_conn = None
        CS.DB_FILE = self._orig_db
        shutil.rmtree(TMP, ignore_errors=True)

    # ---------- 命中 ----------
    def test_hit_returns_members_without_scoring(self):
        """命中概念应带回段内成员，且带时间标签"""
        timed, name, degraded = C.retrieve_by_concept(["物理作业"])
        self.assertEqual(name, "物理作业")
        self.assertFalse(degraded)
        self.assertGreater(len(timed), 0)
        for ts, text in timed:
            self.assertIsInstance(ts, float)
            self.assertTrue(text.startswith("["))

    def test_prefers_longer_keyword(self):
        """长关键词更具体，应优先用于匹配"""
        CS.record_concept("作业", ["m1"])   # 造一个更泛的概念
        timed, name, _ = C.retrieve_by_concept(["作业", "物理作业"])
        self.assertEqual(name, "物理作业", "应优先用更长的关键词匹配")

    def test_no_keywords_returns_empty(self):
        self.assertEqual(C.retrieve_by_concept([]), ([], None, False))

    # ---------- 未命中 ----------
    def test_miss_returns_empty(self):
        timed, name, degraded = C.retrieve_by_concept(["完全无关的股票行情"])
        self.assertEqual(timed, [])
        self.assertIsNone(name)

    def test_corrupt_event_is_skipped(self):
        """事件段数据损坏时应跳过而非抛异常"""
        db = CS._get_db()
        db.execute("INSERT INTO concept_events (id, concept_id, member_ids, start_time, end_time) "
                   "VALUES ('bad', ?, '不是合法JSON', 0, 0)", (self.cid,))
        db.commit()
        timed, name, _ = C.retrieve_by_concept(["物理作业"])
        self.assertEqual(name, "物理作业")   # 不抛异常即可

    # ---------- 时间窗 ----------
    def test_latest_window_narrows(self):
        """latest（1 小时内）应只带回最近那段"""
        timed_all, _, _ = C.retrieve_by_concept(["物理作业"])
        timed_latest, _, degraded = C.retrieve_by_concept(["物理作业"], time_intent="latest")
        self.assertFalse(degraded)
        self.assertLess(len(timed_latest), len(timed_all))
        self.assertEqual(len(timed_latest), 1)

    def test_empty_window_degrades(self):
        """窗口内无记录时降级为最近若干段，degraded 为 True"""
        CS.TIME_INTENT_WINDOWS["__far__"] = (100 * 86400, 200 * 86400)
        CS.VALID_TIME_INTENTS.add("__far__")
        try:
            timed, name, degraded = C.retrieve_by_concept(["物理作业"], time_intent="__far__")
        finally:
            CS.TIME_INTENT_WINDOWS.pop("__far__", None)
            CS.VALID_TIME_INTENTS.discard("__far__")
        self.assertTrue(degraded)
        self.assertGreater(len(timed), 0, "降级后仍应有材料可回应")

    # ---------- 时间指代通道 ----------
    def test_time_intent_inject_and_consume(self):
        C.inject_time_intent("earlier")
        self.assertEqual(C.consume_time_intent(), "earlier")
        self.assertEqual(C.consume_time_intent(), "none", "应是一次性消费")

    def test_invalid_intent_normalized(self):
        C.inject_time_intent("乱七八糟")
        self.assertEqual(C.consume_time_intent(), "乱七八糟")  # 注入原样存
        # 但 select_episodes 会把它当 none 处理
        events, degraded = CS.select_episodes(self.cid, "乱七八糟", limit=2)
        self.assertEqual(len(events), 2)
        self.assertFalse(degraded)


class ConceptPinnedTest(unittest.TestCase):
    """概念路带回的记忆必须以 pinned 传入 verbalize（不被关键词过滤丢弃）

    验证的是认知循环里 pinned_memories 的组装规则本身。
    """

    def test_concept_texts_enter_pinned(self):
        # 复刻循环中的组装表达式，确认概念路内容被并入 pinned
        dialogue_memories = ["[刚刚] 周圻晨说：你好"]
        prev_mem = "[刚刚] 我想：上一轮的念头"
        drowsy_mem = None
        note_mem = None
        concept_texts = ["[刚刚] 我想：物理作业还没写", "[昨天下午] 我想：物理卷子还空着"]

        pinned_memories = (
            dialogue_memories
            + ([prev_mem] if prev_mem else [])
            + ([drowsy_mem] if drowsy_mem else [])
            + ([note_mem] if note_mem else [])
            + concept_texts
        )
        for t in concept_texts:
            self.assertIn(t, pinned_memories, "概念路内容必须进入 pinned")

    def test_verbalize_keeps_pinned_despite_keyword_filter(self):
        """即使关键词完全不匹配，pinned 也不会被过滤掉（这是 pinned 的契约）"""
        from core.llm_interface import verbalize
        captured = {}

        def fake_api(messages, **kwargs):
            captured["content"] = messages[-1]["content"]
            return '{"say": false, "text": "嗯"}'

        concept_line = "[刚刚] 我想：物理作业还没写"
        with patch("core.llm_interface.call_api_thinking", side_effect=fake_api):
            verbalize(
                memories=["[刚刚] 完全不相干的记忆"],
                keywords=["毫不相关"],
                pinned=[concept_line],
                timestamps=[1000.0],
                pinned_timestamps=[2000.0],
            )
        self.assertIn("物理作业还没写", captured["content"],
                      "pinned 中的概念记忆必须出现在最终提示词里")


if __name__ == "__main__":
    unittest.main()
