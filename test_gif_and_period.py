"""
test_gif_and_period.py
验证两处改动：
  1. 收藏表情包时统一转成 GIF（让 QQ 自动识别为表情包）
  2. 辉夜发送的文本去掉句末的单个句号（在入库与对话历史之前）
"""
import os
import shutil
import sys
import unittest

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from core import asset_library as A
from core.cognition import _strip_trailing_period

TMP = os.path.join(PROJECT_DIR, "data", "test", "_gif_test")


class PeriodStripTest(unittest.TestCase):
    def test_removes_single_trailing_period(self):
        self.assertEqual(_strip_trailing_period("今天有点累了。"), "今天有点累了")

    def test_keeps_question_and_exclamation(self):
        self.assertEqual(_strip_trailing_period("你在干嘛？"), "你在干嘛？")
        self.assertEqual(_strip_trailing_period("好耶！"), "好耶！")

    def test_removes_only_the_last_one(self):
        """末尾连续多个句号时，只去掉最后那一个"""
        self.assertEqual(_strip_trailing_period("嗯。。"), "嗯。")

    def test_keeps_inner_periods(self):
        self.assertEqual(_strip_trailing_period("我说了。然后呢。"), "我说了。然后呢")

    def test_no_period_unchanged(self):
        self.assertEqual(_strip_trailing_period("就这样吧"), "就这样吧")

    def test_empty_string(self):
        self.assertEqual(_strip_trailing_period(""), "")

    def test_only_period(self):
        self.assertEqual(_strip_trailing_period("。"), "")


class StickerGifTest(unittest.TestCase):
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

    def tearDown(self):
        A.ASSET_ROOT, A.INDEX_FILE, A.STAGING_DIR, A.NOTE_DIR = self._orig
        A._index = None
        A._pending.clear()
        shutil.rmtree(TMP, ignore_errors=True)

    def _make_png(self, name="src.png", size=(32, 32), color=(255, 0, 0)):
        from PIL import Image
        p = os.path.join(TMP, name)
        Image.new("RGBA", size, color).save(p, format="PNG")
        return p

    def _make_animated_gif(self, name="src.gif", frames=3):
        from PIL import Image
        p = os.path.join(TMP, name)
        imgs = [Image.new("RGBA", (24, 24), (i * 40, 0, 0)) for i in range(frames)]
        imgs[0].save(p, format="GIF", save_all=True, append_images=imgs[1:], duration=80, loop=0)
        return p

    def test_transparency_preserved(self):
        """透明背景不能压成不透明黑块（表情素材常有透明底）"""
        from PIL import Image
        src = os.path.join(TMP, "alpha.png")
        im = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        for x in range(10, 30):
            for y in range(10, 30):
                im.putpixel((x, y), (255, 0, 0, 255))
        im.save(src)

        saved = A.add_asset(A.STICKER, src, "透明背景")
        with Image.open(saved["path"]) as g:
            self.assertIn("transparency", g.info, "GIF 应带透明索引")
            rgba = g.convert("RGBA")
            self.assertEqual(rgba.getpixel((0, 0))[3], 0, "原本透明处应仍透明")
            self.assertEqual(rgba.getpixel((20, 20))[:3], (255, 0, 0), "主体颜色应保留")

    def test_save_sticker_converts_even_if_reported_as_image(self):
        """NapCat 常把群里的表情包报成普通 image；模型选 save_sticker 时
        应以意图为准存成表情包并转 GIF，而不是按暂存类型回退成图片。"""
        import json
        from unittest.mock import patch
        from core import action_layer as L

        src = self._make_png()
        ref = A.register_pending(A.IMAGE, src, "群里的表情包")
        with patch.object(L, "call_api_thinking",
                          return_value=json.dumps({"action": "save_sticker", "ref": ref})):
            out = L.decide_action("念头", True, "话", [])
        self.assertEqual(out["action"], "save_sticker")
        self.assertTrue(out["saved"]["file"].endswith(".gif"), out["saved"]["file"])
        from PIL import Image
        with Image.open(out["saved"]["path"]) as im:
            self.assertEqual(im.format, "GIF")
        self.assertEqual(A.count(A.STICKER), 1)
        self.assertEqual(A.count(A.IMAGE), 0)

    def test_save_image_keeps_original_format(self):
        """普通图片仍按原格式存，不强行转 GIF"""
        import json
        from unittest.mock import patch
        from core import action_layer as L

        src = self._make_png()
        ref = A.register_pending(A.IMAGE, src, "一张普通照片")
        with patch.object(L, "call_api_thinking",
                          return_value=json.dumps({"action": "save_image", "ref": ref})):
            out = L.decide_action("念头", True, "话", [])
        self.assertEqual(out["action"], "save_image")
        self.assertTrue(out["saved"]["file"].endswith(".png"), out["saved"]["file"])
        self.assertEqual(A.count(A.IMAGE), 1)

    def test_static_png_becomes_gif(self):
        src = self._make_png()
        saved = A.add_asset(A.STICKER, src, "一张静态图")
        self.assertTrue(saved["added"])
        self.assertTrue(saved["file"].endswith(".gif"), saved["file"])
        self.assertTrue(os.path.isfile(saved["path"]))
        from PIL import Image
        with Image.open(saved["path"]) as im:
            self.assertEqual(im.format, "GIF")

    def test_animated_gif_keeps_animation(self):
        """动图应保留多帧与动画属性，而不是压成单帧"""
        src = self._make_animated_gif(frames=3)
        saved = A.add_asset(A.STICKER, src, "一个会动的表情")
        from PIL import Image
        with Image.open(saved["path"]) as im:
            self.assertEqual(im.format, "GIF")
            self.assertEqual(im.n_frames, 3)
            self.assertTrue(getattr(im, "is_animated", False))

    def test_image_kind_not_converted(self):
        """图片不受影响，仍按原格式存"""
        src = self._make_png()
        saved = A.add_asset(A.IMAGE, src, "一张普通图片")
        self.assertTrue(saved["file"].endswith(".png"), saved["file"])

    def test_sticker_in_index_points_to_gif(self):
        src = self._make_png()
        A.add_asset(A.STICKER, src, "检查索引")
        with open(A.INDEX_FILE, "r", encoding="utf-8") as f:
            import json
            raw = json.load(f)
        self.assertTrue(raw["sticker"]["检查索引"].endswith(".gif"))

    def test_send_path_uses_converted_gif(self):
        """发送时取到的是转好的 GIF 文件"""
        src = self._make_png()
        A.add_asset(A.STICKER, src, "待发送")
        entry = A.ordered_pool(A.STICKER)[0]
        self.assertTrue(entry["path"].endswith(".gif"))
        self.assertTrue(os.path.isfile(entry["path"]))

    def test_conversion_failure_falls_back(self):
        """无法解码的源文件应回落为原格式保存，不丢素材"""
        bad = os.path.join(TMP, "broken.gif")
        with open(bad, "wb") as f:
            f.write(b"not an image at all")
        saved = A.add_asset(A.STICKER, bad, "坏文件")
        self.assertIsNotNone(saved)
        self.assertTrue(os.path.isfile(saved["path"]), "转换失败也必须保留素材")

    def test_duplicate_desc_not_reconverted(self):
        src = self._make_png()
        first = A.add_asset(A.STICKER, src, "同一张")
        second = A.add_asset(A.STICKER, src, "同一张")
        self.assertFalse(second["added"])
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(A.count(A.STICKER), 1)


if __name__ == "__main__":
    unittest.main()
