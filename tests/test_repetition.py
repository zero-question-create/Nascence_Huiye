import unittest

from core.repetition import ActionGuard, RecentOutputGuard, similarity


class RepetitionGuardTest(unittest.TestCase):
    def test_similarity_and_output_guard(self):
        guard = RecentOutputGuard()
        guard.add("桌面没什么要动的，QQ未读不少，先安静待着")
        self.assertTrue(guard.is_repeat("桌面没什么要动的，QQ未读不少，先安静待着"))
        self.assertTrue(guard.is_repeat("桌面没什么要动的，QQ未读不少，先安静待一会儿"))
        self.assertFalse(guard.is_repeat("我去看看B站搜索结果"))
        self.assertGreater(similarity("同一句话换皮", "同一句话换皮了"), 0.7)

    def test_action_guard_blocks_paraphrase(self):
        guard = ActionGuard()
        guard.add("app_do:打开steam把我库里的游戏列出来")
        self.assertTrue(guard.is_duplicate("app_do:打开steam把库里的游戏列出来"))
        self.assertFalse(guard.is_duplicate("app_do:打开QQ查看未读"))


if __name__ == "__main__":
    unittest.main()
