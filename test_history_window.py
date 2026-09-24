"""
test_history_window.py
验证历史对话窗口：按「最近 4~6 条他人消息」定位起点，
再取该起点之后的全部消息（包含自己的），以保证拿到完整的对话脉络。
"""
import os
import sys
import unittest

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from core.cognition import _select_dialogue_window
from config.constants import BOT_NAME


def build(seq):
    """seq: [('o'|'s', 内容)] → 消息历史列表"""
    return [
        {"sender": "群友" if tag == "o" else BOT_NAME, "content": text, "source": "QQ", "time": float(i)}
        for i, (tag, text) in enumerate(seq)
    ]


def alternating(rounds):
    """构造 他人/自己 交替的对话，返回 (历史, 每条他人消息的内容列表)"""
    seq = []
    others = []
    for i in range(rounds):
        seq.append(("o", f"他人{i}"))
        others.append(f"他人{i}")
        seq.append(("s", f"我的{i}"))
    return build(seq), others


class DialogueWindowTest(unittest.TestCase):
    def test_window_keeps_own_messages(self):
        """窗口内应保留自己的消息，形成完整的一问一答"""
        hist, _ = alternating(6)
        sel = _select_dialogue_window(hist, 4)
        contents = [m["content"] for m in sel]
        # 起点是倒数第 4 条他人消息「他人2」
        self.assertEqual(contents[0], "他人2")
        # 自己的回复也在窗口里
        self.assertIn("我的2", contents)
        self.assertIn("我的5", contents)
        self.assertEqual(len(sel), 8)

    def test_window_start_tracks_nth_other_message(self):
        """窗口起点随 N 移动：N 越大，越往前取"""
        hist, _ = alternating(6)
        for n, expected_first in ((4, "他人2"), (5, "他人1"), (6, "他人0")):
            sel = _select_dialogue_window(hist, n)
            self.assertEqual(sel[0]["content"], expected_first, f"窗口={n} 起点错误")

    def test_window_ends_at_latest_message(self):
        """窗口必须一直取到最新一条，不能截断当前对话"""
        hist, _ = alternating(3)
        hist.append({"sender": "群友", "content": "最新一句", "source": "QQ", "time": 99.0})
        sel = _select_dialogue_window(hist, 2)
        self.assertEqual(sel[-1]["content"], "最新一句")

    def test_fewer_others_than_window(self):
        """他人消息不足窗口时，从第一条他人消息起算，不报错"""
        hist = build([("o", "a"), ("s", "b"), ("o", "c")])
        sel = _select_dialogue_window(hist, 6)
        self.assertEqual([m["content"] for m in sel], ["a", "b", "c"])

    def test_no_others_returns_empty(self):
        """还没有任何他人消息时返回空，本轮不并入历史"""
        hist = build([("s", "只有我自己")])
        self.assertEqual(_select_dialogue_window(hist, 4), [])

    def test_empty_history(self):
        self.assertEqual(_select_dialogue_window([], 4), [])

    def test_leading_own_messages_before_first_other_excluded(self):
        """起点之前的自己的消息不应被纳入（窗口从对话起点开始）"""
        hist = build([("s", "开场的自言自语"), ("o", "a"), ("s", "b")])
        sel = _select_dialogue_window(hist, 1)
        contents = [m["content"] for m in sel]
        self.assertNotIn("开场的自言自语", contents)
        self.assertEqual(contents, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
