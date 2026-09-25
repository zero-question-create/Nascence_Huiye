"""
test_bom_handling.py
验证 UTF-8 BOM 兼容：
  - 配置/状态类 JSON 文件带 BOM 时仍能正常读取（Windows 记事本常见行为）
  - 无 BOM 的文件不受影响
  - manifest 语法损坏时降级为空名单并给出可读错误，而不是让 QQ 服务崩溃
"""
import io
import json
import os
import shutil
import sys
import unittest
from unittest.mock import patch

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

TMP = os.path.join(PROJECT_DIR, "data", "test", "_bom_test")

SAMPLE = {
    "whitelist_groups": ["1108285603"],
    "name_mapping": {"1108285603": {"3866314101": "周圻晨"}},
}


def write_json(path, data, bom: bool):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8-sig" if bom else "utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))


class BomReadTest(unittest.TestCase):
    """各模块的读取点应对 BOM 免疫"""

    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        os.makedirs(TMP, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def test_bom_file_fails_with_plain_utf8(self):
        """先确认现象：普通 utf-8 读取 BOM 文件确实会报错（复现用户的问题）"""
        path = os.path.join(TMP, "with_bom.json")
        write_json(path, SAMPLE, bom=True)
        with open(path, "rb") as f:
            self.assertTrue(f.read(3) == b"\xef\xbb\xbf", "文件应带 BOM")
        with self.assertRaises(json.JSONDecodeError):
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)

    def test_bom_file_reads_with_utf8_sig(self):
        path = os.path.join(TMP, "with_bom.json")
        write_json(path, SAMPLE, bom=True)
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        self.assertEqual(data["whitelist_groups"], ["1108285603"])

    def test_plain_file_still_reads(self):
        """无 BOM 文件同样能读，不能因为改用 sig 而坏掉"""
        path = os.path.join(TMP, "no_bom.json")
        write_json(path, SAMPLE, bom=False)
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        self.assertEqual(data["whitelist_groups"], ["1108285603"])

    def test_manifest_with_bom_loads(self):
        """QQManifest 应能加载带 BOM 的配置文件"""
        import qq_bot
        path = os.path.join(TMP, "qq_manifest.json")
        write_json(path, SAMPLE, bom=True)
        orig = qq_bot.CONFIG_PATH
        qq_bot.CONFIG_PATH = path
        try:
            m = qq_bot.QQManifest()
        finally:
            qq_bot.CONFIG_PATH = orig
        self.assertEqual(m.whitelist, {"1108285603"})
        self.assertEqual(m.name_map["1108285603"]["3866314101"], "周圻晨")

    def test_manifest_broken_json_degrades(self):
        """语法损坏时降级为空名单，不抛异常中断服务启动"""
        import qq_bot
        path = os.path.join(TMP, "broken.json")
        os.makedirs(TMP, exist_ok=True)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write('{"whitelist_groups": ["1",],}')  # 多余逗号
        orig = qq_bot.CONFIG_PATH
        qq_bot.CONFIG_PATH = path
        try:
            m = qq_bot.QQManifest()      # 不应抛出
        finally:
            qq_bot.CONFIG_PATH = orig
        self.assertEqual(m.whitelist, set())
        self.assertEqual(m.name_map, {})

    def test_history_state_with_bom_loads(self):
        """对话历史状态文件带 BOM 时也应能加载"""
        import utils.message_history as MH
        path = os.path.join(TMP, "state.json")
        write_json(path, [{"sender": "周圻晨", "content": "你好", "source": "QQ", "time": 1.0}], bom=True)
        orig_file = MH._STATE_FILE
        orig_hist = MH._message_history
        MH._STATE_FILE = path
        MH._message_history = []
        try:
            MH.load_state()
        finally:
            MH._STATE_FILE = orig_file
            hist = MH._message_history
            MH._message_history = orig_hist
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["content"], "你好")


if __name__ == "__main__":
    unittest.main()
