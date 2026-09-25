"""
test_sticker_send.py
验证表情包发送时带上 sub_type：
  NapCat 的 [ze.image] 分支会把消息段的 sub_type 传给 createValidSendPicElement，
  最终写入 QQ 的 picSubType 字段，这才是 QQ 区分"图片"与"表情包"的开关。
  仅把文件转成 .gif 不足以让它显示为表情包。
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

TMP = os.path.join(PROJECT_DIR, "data", "test", "_sticker_send")


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(json.loads(msg))


class StickerSendTest(unittest.TestCase):
    def setUp(self):
        import qq_bot
        self.qq_bot = qq_bot
        shutil.rmtree(TMP, ignore_errors=True)
        os.makedirs(TMP)
        self.gif = os.path.join(TMP, "s.gif")
        with open(self.gif, "wb") as f:
            f.write(b"GIF89a" + b"\x00" * 32)
        self.ws = FakeWS()
        self._orig_ws = qq_bot._napcat_websocket
        self._orig_sleep = qq_bot.is_sleeping
        qq_bot._napcat_websocket = self.ws
        qq_bot.is_sleeping = lambda: False

    def tearDown(self):
        self.qq_bot._napcat_websocket = self._orig_ws
        self.qq_bot.is_sleeping = self._orig_sleep
        shutil.rmtree(TMP, ignore_errors=True)

    def _send(self, as_sticker):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self.qq_bot.send_group_media("1108285603", self.gif, as_sticker=as_sticker)
            )
        finally:
            loop.close()

    def test_sticker_send_sets_sub_type(self):
        ok = self._send(as_sticker=True)
        self.assertTrue(ok)
        seg = self.ws.sent[0]["params"]["message"][0]
        self.assertEqual(seg["type"], "image")
        self.assertEqual(seg["data"].get("sub_type"), 1,
                         "表情包必须带 sub_type=1，否则 QQ 当普通图片显示")

    def test_image_send_has_no_sub_type(self):
        ok = self._send(as_sticker=False)
        self.assertTrue(ok)
        seg = self.ws.sent[0]["params"]["message"][0]
        self.assertNotIn("sub_type", seg["data"], "普通图片不应带 sub_type")

    def test_default_is_image(self):
        """不传 as_sticker 时保持旧行为（按图片发）"""
        loop = asyncio.new_event_loop()
        try:
            ok = loop.run_until_complete(
                self.qq_bot.send_group_media("1108285603", self.gif)
            )
        finally:
            loop.close()
        self.assertTrue(ok)
        seg = self.ws.sent[0]["params"]["message"][0]
        self.assertNotIn("sub_type", seg["data"])

    def test_file_uri_is_absolute(self):
        self._send(as_sticker=True)
        seg = self.ws.sent[0]["params"]["message"][0]
        self.assertTrue(seg["data"]["file"].startswith("file:///"))


class StickerSendWiringTest(unittest.TestCase):
    """认知循环应把 sticker 类型透传给发送函数"""

    def test_cognition_passes_as_sticker_for_sticker_kind(self):
        from core import cognition as C
        from core import asset_library as A

        calls = []

        async def fake_media(gid, path, **kwargs):
            calls.append((gid, path, kwargs.get("as_sticker")))
            return True

        loop = asyncio.new_event_loop()
        try:
            with patch.object(A, "available_kinds", return_value=[A.STICKER]), \
                 patch.object(A, "list_pending", return_value=[]), \
                 patch.object(C, "decide_action") if hasattr(C, "decide_action") else patch("core.action_layer.decide_action") as dec, \
                 patch.object(C, "create_memory", return_value="m1"), \
                 patch("core.llm_interface.add_to_history"):
                dec.return_value = {
                    "action": "sticker", "detail": "猫猫点头",
                    "entry": {"kind": A.STICKER, "path": os.path.join(TMP, "x.gif"),
                              "desc": "猫猫点头"},
                }
                with patch.object(A, "mark_sent"):
                    loop.run_until_complete(
                        C._run_action_decision(loop, "念头", True, True, [], fake_media, "1108285603")
                    )
        finally:
            loop.close()

        self.assertEqual(len(calls), 1, f"应发送一次，实际 {calls}")
        self.assertTrue(calls[0][2], "sticker 类型必须以 as_sticker=True 发送")


if __name__ == "__main__":
    unittest.main()
