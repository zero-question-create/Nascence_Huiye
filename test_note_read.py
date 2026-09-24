"""
test_note_read.py
验证记事本读取功能：
  - read_note 沙箱与文件名校验
  - read_txt 动作
  - 写文件后自动读取（写的过程中顺手读回全文）
  - 读到的内容以「[现在]我打开了我写的“xxx”文件，内容是：xxx」进入下一轮特供记忆
  - 特供记忆「不入库」：不写记忆库、不进对话历史
"""
import asyncio
import json
import os
import shutil
import sys
import unittest
from unittest.mock import patch

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from core import action_layer as L
from core import asset_library as A
from core import cognition as C

TMP = os.path.join(PROJECT_DIR, "data", "test", "_note_test")


class NoteSandboxBase(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        os.makedirs(TMP, exist_ok=True)
        self._orig = (A.ASSET_ROOT, A.INDEX_FILE, A.STAGING_DIR, A.NOTE_DIR)
        A.ASSET_ROOT = os.path.join(TMP, "assets")
        A.INDEX_FILE = os.path.join(A.ASSET_ROOT, "index.json")
        A.STAGING_DIR = os.path.join(TMP, "staging")
        A.NOTE_DIR = os.path.join(TMP, "notes")
        A._index = None
        A._pending.clear()
        C._pending_note_memory = None

    def tearDown(self):
        A.ASSET_ROOT, A.INDEX_FILE, A.STAGING_DIR, A.NOTE_DIR = self._orig
        A._index = None
        A._pending.clear()
        C._pending_note_memory = None
        shutil.rmtree(TMP, ignore_errors=True)


class TestReadNote(NoteSandboxBase):
    def test_read_back_written_content(self):
        A.write_note("心情", "今天有点累")
        A.write_note("心情", "但还是想说话")
        note = A.read_note("心情")
        self.assertIsNotNone(note)
        self.assertEqual(note["name"], "心情")
        self.assertIn("今天有点累", note["text"])
        self.assertIn("但还是想说话", note["text"])
        self.assertFalse(note["truncated"])

    def test_read_missing_file_returns_none(self):
        self.assertIsNone(A.read_note("不存在的笔记"))

    def test_read_rejects_path_traversal(self):
        """读取与写入共用同一套沙箱：路径痕迹一律拒绝"""
        for bad in ["../secret", "..\\secret", "a/b", "a\\b", "", "   ", "bad:name"]:
            self.assertIsNone(A.read_note(bad), f"应拒绝: {bad!r}")

    def test_read_accepts_explicit_txt_suffix(self):
        A.write_note("随笔", "内容一")
        self.assertIsNotNone(A.read_note("随笔.txt"))

    def test_truncation_flag(self):
        """写入有单次上限，读取超出上限时标记 truncated"""
        os.makedirs(A.NOTE_DIR, exist_ok=True)
        with open(os.path.join(A.NOTE_DIR, "长文.txt"), "w", encoding="utf-8") as f:
            f.write("字" * (A.MAX_NOTE_READ_CHARS + 500))
        note = A.read_note("长文")
        self.assertTrue(note["truncated"])
        self.assertEqual(len(note["text"]), A.MAX_NOTE_READ_CHARS)

    def test_list_notes(self):
        A.write_note("甲", "内容")
        A.write_note("乙", "内容")
        self.assertEqual(sorted(A.list_notes()), ["乙", "甲"])

    def test_list_notes_ignores_empty_and_other_ext(self):
        os.makedirs(A.NOTE_DIR, exist_ok=True)
        open(os.path.join(A.NOTE_DIR, "空.txt"), "w", encoding="utf-8").close()
        with open(os.path.join(A.NOTE_DIR, "别的.md"), "w", encoding="utf-8") as f:
            f.write("内容")
        self.assertEqual(A.list_notes(), [])


class TestReadAction(NoteSandboxBase):
    def test_read_txt_action(self):
        A.write_note("日记", "外面的雨下了一整个下午")
        with patch.object(L, "call_api_thinking", return_value=json.dumps({"action": "read_txt", "name": "日记"})):
            out = L.decide_action("念头", False, "", [], "")
        self.assertEqual(out["action"], "read_txt")
        self.assertEqual(out["note_name"], "日记")
        self.assertIn("外面的雨下了一整个下午", out["note_text"])
        self.assertTrue(out.get("read_only"))

    def test_read_missing_note_is_noop(self):
        A.write_note("有的", "内容")
        with patch.object(L, "call_api_thinking", return_value=json.dumps({"action": "read_txt", "name": "没的"})):
            out = L.decide_action("念头", False, "", [], "")
        self.assertEqual(out["action"], "none")

    def test_write_then_auto_read_full_text(self):
        """写入后自动读回该文件全文，供下一轮回看"""
        replies = [
            json.dumps({"action": "write_txt", "name": "杂记", "text": "第一句"}),
            json.dumps({"action": "write_txt", "name": "杂记", "text": "第二句"}),
        ]
        with patch.object(L, "call_api_thinking", side_effect=replies):
            first = L.decide_action("念头", False, "", [], "")
            out = L.decide_action("念头", False, "", [], "")
        self.assertEqual(first["action"], "write_txt")
        self.assertEqual(out["action"], "write_txt")
        # 第二次回看时自然包含前面写过的内容，而非只有本次这一句
        self.assertIn("第一句", out["note_text"])
        self.assertIn("第二句", out["note_text"])
        self.assertEqual(out["written"], "第二句")

    def test_notes_listed_in_prompt(self):
        A.write_note("随笔", "内容")
        usr = L._user_prompt("念头", False, "", [], "")
        self.assertIn("随笔", usr)

    def test_no_energy_in_note_prompts(self):
        A.write_note("随笔", "内容")
        usr = L._user_prompt("念头", False, "", [], "")
        for banned in ("精力", "energy", "睡眠压力"):
            self.assertNotIn(banned, usr)


class TestNoteMemoryChannel(NoteSandboxBase):
    """特供记忆通道：内容进下一轮的 pinned，但不入库"""

    def _run(self, action_json):
        from core import asset_library as AL
        A.write_note("随笔", "先把已有的写在这里")
        loop = asyncio.new_event_loop()
        try:
            with patch.object(L, "call_api_thinking", return_value=action_json), \
                 patch.object(C, "create_memory") as cm, \
                 patch.object(C, "add_to_history") if hasattr(C, "add_to_history") else patch("core.llm_interface.add_to_history") as ah:
                res = loop.run_until_complete(
                    C._run_action_decision(loop, "念头", False, True, [], None, "1108285603")
                )
            return res, cm, ah
        finally:
            loop.close()

    def test_read_puts_note_into_pending_channel_without_storing(self):
        A.write_note("随笔", "记录下的旧事")
        loop = asyncio.new_event_loop()
        try:
            with patch.object(L, "call_api_thinking", return_value=json.dumps({"action": "read_txt", "name": "随笔"})), \
                 patch.object(C, "create_memory") as cm, \
                 patch("core.llm_interface.add_to_history") as ah:
                res = loop.run_until_complete(
                    C._run_action_decision(loop, "念头", False, True, [], None, "1108285603")
                )
        finally:
            loop.close()

        self.assertEqual(res, "翻开笔记: 随笔")
        # 进入了特供记忆通道
        self.assertIsNotNone(C._pending_note_memory)
        self.assertIn("[现在]我打开了我写的“随笔”文件，内容是：", C._pending_note_memory)
        self.assertIn("记录下的旧事", C._pending_note_memory)
        # 不入库：既不写记忆库，也不写对话历史
        cm.assert_not_called()
        ah.assert_not_called()

    def test_write_keeps_storing_the_act_but_not_file_body(self):
        """写入行为本身入库，但只记写下的那句，不把整份文件灌进记忆库"""
        A.write_note("随笔", "很早以前写的句子")
        loop = asyncio.new_event_loop()
        try:
            with patch.object(L, "call_api_thinking", return_value=json.dumps(
                    {"action": "write_txt", "name": "随笔", "text": "今天新写的一句"})), \
                 patch.object(C, "create_memory") as cm, \
                 patch("core.llm_interface.add_to_history"):
                loop.run_until_complete(
                    C._run_action_decision(loop, "念头", False, True, [], None, "1108285603")
                )
        finally:
            loop.close()

        self.assertIsNotNone(C._pending_note_memory)
        # 记忆库里只出现本次写下的内容，旧内容只存在于特供通道
        stored = [c.args[0] for c in cm.call_args_list]
        self.assertTrue(any("今天新写的一句" in s for s in stored))
        self.assertFalse(any("很早以前写的句子" in s for s in stored))

    def test_pending_channel_is_one_shot(self):
        """一次性：取走即清，内容只在紧接着的下一轮出现一次"""
        A.write_note("随笔", "只该出现一次的旧句子")
        loop = asyncio.new_event_loop()
        try:
            with patch.object(L, "call_api_thinking", return_value=json.dumps({"action": "read_txt", "name": "随笔"})), \
                 patch.object(C, "create_memory"), \
                 patch("core.llm_interface.add_to_history"):
                loop.run_until_complete(
                    C._run_action_decision(loop, "念头", False, True, [], None, "1108285603")
                )
        finally:
            loop.close()

        first, ts1 = C._consume_note_memory()
        self.assertIsNotNone(first)
        self.assertIsNotNone(ts1)
        self.assertIn("只该出现一次的旧句子", first)
        # 第二次取不到，说明不会在后续每一轮反复出现
        second, ts2 = C._consume_note_memory()
        self.assertIsNone(second)
        self.assertIsNone(ts2)


if __name__ == "__main__":
    unittest.main()
