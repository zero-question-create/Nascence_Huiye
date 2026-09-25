"""
test_quote_history.py
验证引用消息并入对话历史：
  - 引用以结构化字段随消息存储（长期记忆侧仍由 decompose_input 解析）
  - 短期上下文（历史窗口 / get_recent / 落盘）能还原引用
  - 无引用的消息不受影响，旧格式记录（无 quote 字段）兼容
"""
import json
import os
import shutil
import sys
import unittest

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import utils.message_history as MH
from core.cognition import _select_dialogue_window
from config.constants import BOT_NAME


class QuoteHistoryTest(unittest.TestCase):
    def setUp(self):
        self._orig = MH._message_history
        MH._message_history = []
        MH._flushed_count = 0
        self.tmp = os.path.join(PROJECT_DIR, "data", "test", "_quote_test")
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp, exist_ok=True)
        self._orig_file = MH._HISTORY_FILE
        MH._HISTORY_FILE = os.path.join(self.tmp, "history.log")

    def tearDown(self):
        MH._message_history = self._orig
        MH._HISTORY_FILE = self._orig_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_quote_stored_as_structured_field(self):
        MH.add_message("周圻晨", "你说得对", "QQ",
                       quote={"sender": "侯歆瑜", "text": "昨天的作业还没交"})
        rec = MH.get_all()[-1]
        self.assertEqual(rec["content"], "你说得对")
        self.assertEqual(rec["quote"]["sender"], "侯歆瑜")
        self.assertEqual(rec["quote"]["text"], "昨天的作业还没交")

    def test_quote_not_in_content(self):
        """引用不污染正文，正文仍是原话"""
        MH.add_message("周圻晨", "你说得对", "QQ",
                       quote={"sender": "侯歆瑜", "text": "作业还没交"})
        rec = MH.get_all()[-1]
        self.assertEqual(rec["content"], "你说得对")
        self.assertNotIn("作业还没交", rec["content"])

    def test_render_includes_quote(self):
        MH.add_message("周圻晨", "你说得对", "QQ",
                       quote={"sender": "侯歆瑜", "text": "昨天的作业还没交"})
        rendered = MH.get_recent(10)
        self.assertIn("你说得对", rendered)
        self.assertIn("昨天的作业还没交", rendered)
        self.assertIn("侯歆瑜", rendered)

    def test_window_renders_quote(self):
        """历史窗口（认知循环用的那条路径）必须带出引用"""
        MH.add_message("侯歆瑜", "作业还没交", "QQ")
        MH.add_message("周圻晨", "你说得对", "QQ",
                       quote={"sender": "侯歆瑜", "text": "作业还没交"})
        win = _select_dialogue_window(MH.get_all(), 4)
        body = "".join(f"{m['content']}{MH.quote_suffix(m)}" for m in win)
        self.assertIn("你说得对", body)
        self.assertIn("作业还没交", body)

    def test_message_without_quote_unaffected(self):
        MH.add_message("周圻晨", "普通消息", "QQ")
        rec = MH.get_all()[-1]
        self.assertNotIn("quote", rec)
        self.assertEqual(MH.quote_suffix(rec), "")
        self.assertEqual(MH.get_recent(10), "周圻晨说：普通消息")

    def test_legacy_record_without_quote_field(self):
        """兼容旧数据：没有 quote 字段的历史记录不应报错"""
        MH._message_history.append({"sender": "老群友", "content": "旧消息", "source": "QQ", "time": 1.0})
        self.assertEqual(MH.quote_suffix(MH._message_history[-1]), "")
        self.assertEqual(MH.get_recent(10), "老群友说：旧消息")

    def test_empty_quote_ignored(self):
        MH.add_message("周圻晨", "消息", "QQ", quote={"sender": "某人", "text": ""})
        self.assertNotIn("quote", MH.get_all()[-1])

    def test_flush_to_file_preserves_quote(self):
        """落盘也不丢引用"""
        MH.add_message("周圻晨", "你说得对", "QQ",
                       quote={"sender": "侯歆瑜", "text": "作业还没交"})
        MH.flush_to_file()
        with open(MH._HISTORY_FILE, "r", encoding="utf-8") as f:
            body = f.read()
        self.assertIn("你说得对", body)
        self.assertIn("作业还没交", body)

    def test_quote_survives_state_roundtrip(self):
        """存盘再读回，引用字段仍在"""
        MH.add_message("周圻晨", "你说得对", "QQ",
                       quote={"sender": "侯歆瑜", "text": "作业还没交"})
        MH._STATE_FILE = os.path.join(self.tmp, "state.json")
        MH.save_state()
        MH._message_history = []
        MH.load_state()
        rec = MH.get_all()[-1]
        self.assertEqual(rec.get("quote", {}).get("text"), "作业还没交")

    def test_bot_message_can_render_quote(self):
        """辉夜自己的消息也能带引用后缀（例如引用了某人）"""
        MH.add_message(BOT_NAME, "我才没说", "QQ",
                       quote={"sender": "周圻晨", "text": "你昨天答应过的"})
        rendered = MH.get_recent(10)
        self.assertIn("我才没说", rendered)
        self.assertIn("你昨天答应过的", rendered)


if __name__ == "__main__":
    unittest.main()
