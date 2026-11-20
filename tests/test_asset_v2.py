import tempfile
import unittest
from pathlib import Path

from PIL import Image

from core import asset_library as A


class AssetV2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nhy_asset_v2_test_"))
        self.old_root, self.old_index = A.ASSET_ROOT, A.INDEX_FILE
        A.ASSET_ROOT = str(self.tmp)
        A.INDEX_FILE = str(self.tmp / "index.json")
        A._index = None

    def tearDown(self):
        A.ASSET_ROOT, A.INDEX_FILE = self.old_root, self.old_index
        A._index = None
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_same_desc_different_hash_kept(self):
        first = self.tmp / "a.png"
        second = self.tmp / "b.png"
        Image.new("RGB", (16, 16), (255, 0, 0)).save(first)
        Image.new("RGB", (16, 16), (0, 255, 0)).save(second)
        one = A.add_asset(A.IMAGE, str(first), "同一个描述")
        two = A.add_asset(A.IMAGE, str(second), "同一个描述")
        self.assertTrue(one["added"])
        self.assertTrue(two["added"])
        self.assertNotEqual(one["id"], two["id"])
        self.assertEqual(A.count(A.IMAGE), 2)
        self.assertEqual(len(A.ordered_pool(A.IMAGE)), 2)
        A.mark_sent(A.IMAGE, one["desc"], record_id=one["id"])
        self.assertGreater(A.resolve_token(A.IMAGE, one["id"])["last_sent_at"], 0)


if __name__ == "__main__":
    unittest.main()
