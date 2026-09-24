"""
test_action_layer.py
验证动作抉择层：素材库字典、去重（不设冷却）、翻页、收藏、txt 沙箱、
精力不进提示词、历史窗口排除自己的消息。
"""
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


class AssetLibraryTestBase(unittest.TestCase):
    """每个用例前把素材库指向独立临时目录，避免污染真实数据。"""

    def setUp(self):
        self.tmp = os.path.join(PROJECT_DIR, "data", "test", "_asset_test")
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp, exist_ok=True)
        self._orig = (A.ASSET_ROOT, A.INDEX_FILE, A.STAGING_DIR, A.NOTE_DIR)
        A.ASSET_ROOT = os.path.join(self.tmp, "assets")
        A.INDEX_FILE = os.path.join(A.ASSET_ROOT, "index.json")
        A.STAGING_DIR = os.path.join(self.tmp, "staging")
        A.NOTE_DIR = os.path.join(self.tmp, "notes")
        A._index = None
        A._pending.clear()

        # 造两个假素材文件
        self.src_img = os.path.join(self.tmp, "src.png")
        self.src_gif = os.path.join(self.tmp, "src.gif")
        for p in (self.src_img, self.src_gif):
            with open(p, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    def tearDown(self):
        A.ASSET_ROOT, A.INDEX_FILE, A.STAGING_DIR, A.NOTE_DIR = self._orig
        A._index = None
        A._pending.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestAssetLibrary(AssetLibraryTestBase):
    def test_index_is_desc_to_filename_dict(self):
        """索引就是「描述 → 文件名」的持久化字典"""
        A.add_asset(A.IMAGE, self.src_img, "一只橘猫在窗台上睡着")
        assert os.path.exists(A.INDEX_FILE)
        with open(A.INDEX_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.assertIn("一只橘猫在窗台上睡着", raw["image"])
        fn = raw["image"]["一只橘猫在窗台上睡着"]
        self.assertTrue(os.path.exists(A.asset_path(A.IMAGE, fn)))

    def test_duplicate_desc_is_same_asset(self):
        """同一描述重复收藏不产生第二个文件"""
        first = A.add_asset(A.IMAGE, self.src_img, "同一张图")
        second = A.add_asset(A.IMAGE, self.src_img, "同一张图")
        self.assertTrue(first["added"])
        self.assertFalse(second["added"])
        self.assertEqual(first["file"], second["file"])
        self.assertEqual(A.count(A.IMAGE), 1)

    def test_pool_excludes_last_sent(self):
        """去重：候选池排除上一条发送的描述（不设时间冷却）"""
        A.add_asset(A.IMAGE, self.src_img, "图A")
        A.add_asset(A.IMAGE, self.src_img, "图B")
        self.assertEqual(len(A.ordered_pool(A.IMAGE)), 2)
        A.mark_sent(A.IMAGE, "图A")
        pool = A.ordered_pool(A.IMAGE)
        self.assertEqual([e["desc"] for e in pool], ["图B"])
        # 换一条发送后，原条目应重新可用
        A.mark_sent(A.IMAGE, "图B")
        self.assertEqual([e["desc"] for e in A.ordered_pool(A.IMAGE)], ["图A"])

    def test_resolve_token_by_id_and_desc(self):
        A.add_asset(A.STICKER, self.src_gif, "猫猫点头.gif")
        aid = A._asset_id(A.STICKER, "猫猫点头.gif")
        self.assertIsNotNone(A.resolve_token(A.STICKER, aid))
        self.assertIsNotNone(A.resolve_token(A.STICKER, "猫猫点头.gif"))
        self.assertIsNone(A.resolve_token(A.STICKER, "不存在的编号"))

    def test_staging_lifecycle(self):
        ref = A.register_pending(A.IMAGE, self.src_img, "刚收到的一张图")
        self.assertTrue(ref)
        items = A.list_pending()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["ref"], ref)
        # 收藏后暂存副本仍在，但素材已入库
        saved = A.add_asset(A.IMAGE, A.get_pending(ref)["path"], "刚收到的一张图")
        self.assertTrue(saved["added"])
        # 过期清理会删除副本
        A._pending[ref]["ts"] = 0
        A.cleanup_pending()
        self.assertEqual(A.list_pending(), [])
        self.assertFalse(os.path.exists(saved["path"]) is False)  # 收藏本体保留


class TestTxtSandbox(AssetLibraryTestBase):
    def test_write_and_append(self):
        p1 = A.write_note("心情", "今天有点累")
        self.assertIsNotNone(p1)
        p2 = A.write_note("心情", "但还是想说话")
        self.assertEqual(p1, p2)
        with open(p1, "r", encoding="utf-8") as f:
            body = f.read()
        self.assertIn("今天有点累", body)
        self.assertIn("但还是想说话", body)

    def test_rejects_unsafe_names_and_paths(self):
        for bad in ["../escape", "..\\escape", "a/b", "a\\b", "", "   ", "x" * 40, "bad:name"]:
            self.assertIsNone(A.write_note(bad, "内容"), f"应拒绝: {bad!r}")

    def test_rejects_empty_text(self):
        self.assertIsNone(A.write_note("笔记", "   "))

    def test_only_txt_extension_written(self):
        path = A.write_note("记录.md", "内容")
        self.assertTrue(path.endswith(".txt"), path)


class TestActionParsing(AssetLibraryTestBase):
    def test_parse_json_variants(self):
        self.assertEqual(L._parse_action('{"action":"none"}')["action"], "none")
        self.assertEqual(L._parse_action('```json\n{"action":"image","id":"Iabc"}\n```')["action"], "image")
        self.assertEqual(L._parse_action('好的，我的选择是 {"action":"sticker","id":"S1"} 就这样')["action"], "sticker")

    def test_parse_rejects_free_text(self):
        """正文里出现'image'这类词不应被当成有效动作"""
        self.assertEqual(L._parse_action("我想发一张 image 给她"), {})

    def test_no_energy_in_prompts(self):
        """精力等内部数值不应出现在提示词中"""
        sys_p = L._system_prompt()
        usr_p = L._user_prompt("念头", True, "说出口的话", ["关键词"], "情况")
        for text in (sys_p, usr_p):
            for banned in ("精力", "energy", "睡眠压力", "压力仍", "0."):
                self.assertNotIn(banned, text, f"提示词不应包含内部数值 {banned!r}")


class TestActionExecution(AssetLibraryTestBase):
    def _seed(self):
        A.add_asset(A.IMAGE, self.src_img, "一只橘猫在窗台上睡着")
        A.add_asset(A.IMAGE, self.src_img, "一片暴雨前的乌云")
        A.add_asset(A.STICKER, self.src_gif, "猫猫点头")

    def test_unknown_id_falls_back_to_none(self):
        self._seed()
        res = L.decide_action.__wrapped__ if hasattr(L.decide_action, "__wrapped__") else None
        with patch.object(L, "call_api_thinking", return_value='{"action":"image","id":"不存在的编号"}'):
            out = L.decide_action("念头", True, "话", [])
        self.assertEqual(out["action"], "none")

    def test_send_action_returns_entry(self):
        self._seed()
        target = A.ordered_pool(A.IMAGE)[0]
        with patch.object(L, "call_api_thinking", return_value=json.dumps({"action": "image", "id": target["id"]})):
            out = L.decide_action("念头", True, "话", [])
        self.assertEqual(out["action"], "image")
        self.assertEqual(out["detail"], target["desc"])
        self.assertTrue(os.path.exists(out["entry"]["path"]))

    def test_dedup_blocks_repeat_send(self):
        """上一条发过的内容不会被再次选中（无冷却，仅去重）"""
        self._seed()
        with patch.object(L, "call_api_thinking", return_value=json.dumps({"action": "image", "id": A.ordered_pool(A.IMAGE)[0]["id"]})):
            first = L.decide_action("念头", True, "话", [])
        A.mark_sent(A.IMAGE, first["detail"])
        # 让模型再次指定同一条
        with patch.object(L, "call_api_thinking", return_value=json.dumps({"action": "image", "id": A._asset_id(A.IMAGE, first["detail"])})):
            second = L.decide_action("念头", True, "话", [])
        self.assertEqual(second["action"], "none")

    def test_next_page_appends_and_does_not_repeat_loop(self):
        """翻页：在子对话里追加询问，不回到认知循环；末页后要求做决定"""
        for i in range(30):
            A.add_asset(A.IMAGE, self.src_img, f"图片描述第{i}号")
        calls = {"n": 0}

        def fake(messages, **kwargs):
            calls["n"] += 1
            # 记录每次询问时最后一条 user 内容，确认是追加而非重开
            if calls["n"] == 1:
                return '{"action":"image_next_page"}'
            return '{"action":"none"}'

        with patch.object(L, "call_api_thinking", side_effect=fake):
            out = L.decide_action("念头", True, "话", [])
        self.assertEqual(out["action"], "none")
        self.assertEqual(calls["n"], 2, "翻页应只追加一次询问")

    def test_page_limit_forces_decision(self):
        """反复翻页会在上限处被强制要求决定，防止无限翻页"""
        for i in range(200):
            A.add_asset(A.IMAGE, self.src_img, f"图片描述第{i}号")
        seen = {"n": 0, "forced": False}

        def fake(messages, **kwargs):
            seen["n"] += 1
            last = messages[-1]["content"]
            if "最后一页" in last:
                seen["forced"] = True
                return '{"action":"none"}'
            return '{"action":"image_next_page"}'

        with patch.object(L, "call_api_thinking", side_effect=fake):
            L.decide_action("念头", True, "话", [])
        self.assertTrue(seen["forced"], "达到翻页上限时应提示已到末页")
        self.assertLessEqual(seen["n"], L._max_pages() + 2)

    def test_save_action_marks_added(self):
        self._seed()
        ref = A.register_pending(A.IMAGE, self.src_img, "刚收到的新图")
        with patch.object(L, "call_api_thinking", return_value=json.dumps({"action": "save_image", "ref": ref})):
            out = L.decide_action("念头", True, "话", [])
        self.assertEqual(out["action"], "save_image")
        self.assertTrue(out["added"])
        self.assertEqual(A.count(A.IMAGE), 3)

    def test_save_of_unknown_ref_is_noop(self):
        self._seed()
        with patch.object(L, "call_api_thinking", return_value='{"action":"save_image","ref":"m999"}'):
            out = L.decide_action("念头", True, "话", [])
        self.assertEqual(out["action"], "none")

    def test_write_txt_action(self):
        self._seed()
        with patch.object(L, "call_api_thinking", return_value=json.dumps(
                {"action": "write_txt", "name": "随笔", "text": "今天天色很暗"})):
            out = L.decide_action("念头", True, "话", [])
        self.assertEqual(out["action"], "write_txt")
        with open(out["path"], "r", encoding="utf-8") as f:
            self.assertIn("今天天色很暗", f.read())

    def test_empty_library_short_circuits_when_notes_disabled(self):
        """收藏、暂存皆空且记事本关闭时，不应发起 LLM 调用"""
        with patch.object(L, "note_enabled", return_value=False), \
             patch.object(L, "call_api_thinking") as m:
            out = L.decide_action("念头", True, "话", [])
        self.assertEqual(out["action"], "none")
        m.assert_not_called()

    def test_empty_library_still_allows_notes(self):
        """记事本开启时，即便收藏为空也应询问一次（允许从零开始写）"""
        with patch.object(L, "note_enabled", return_value=True), \
             patch.object(L, "call_api_thinking", return_value='{"action":"none"}') as m:
            out = L.decide_action("念头", True, "话", [])
        self.assertEqual(out["action"], "none")
        m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
