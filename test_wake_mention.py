"""
test_wake_mention.py
测试 @ 唤醒功能及各类 CQ 码、纯文本、结构化消息与睡眠状态处理
"""
import sys
import os
import asyncio
import unittest

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from qq_bot import parse_cq_code, handle_group_message, is_sleeping, enter_sleep, wake_up, get_bot_qq, BOT_NAME
from core.biorhythm import BIORHYTHM

class TestWakeAndMention(unittest.TestCase):
    def test_parse_cq_code_formats(self):
        """测试不同 NapCat / OneBot CQ:at 格式与参数顺序的解析"""
        bot_qq = get_bot_qq()

        # 1. 标准 CQ:at
        text1 = f"[CQ:at,qq={bot_qq}]"
        clean, mentions = parse_cq_code(text1)
        self.assertEqual(clean, "")
        self.assertIn(bot_qq, mentions)

        # 2. 带 text 扩展属性（NapCat 常见格式）
        text2 = f"[CQ:at,qq={bot_qq},text=@辉夜]"
        clean, mentions = parse_cq_code(text2)
        self.assertEqual(clean, "")
        self.assertIn(bot_qq, mentions)

        # 3. 参数乱序且带 name 和 text 扩展属性
        text3 = f"[CQ:at,text=@辉夜,name=辉夜,qq={bot_qq}]"
        clean, mentions = parse_cq_code(text3)
        self.assertEqual(clean, "")
        self.assertIn(bot_qq, mentions)

        # 4. @全体成员
        text4 = "[CQ:at,qq=all]"
        clean, mentions = parse_cq_code(text4)
        self.assertEqual(clean, "")
        self.assertIn("all", mentions)

        # 5. 多个 @ 伴随正文
        text5 = f"你好啊 [CQ:at,qq=123456,name=张三] 还有 [CQ:at,qq={bot_qq},text=@辉夜] 早上好"
        clean, mentions = parse_cq_code(text5)
        self.assertEqual(clean, "你好啊  还有  早上好")
        self.assertIn("123456", mentions)
        self.assertIn(bot_qq, mentions)


class TestAsyncSleepWake(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # 记录发送过的群消息
        self.sent_messages = []
        import qq_bot
        self.orig_send_func = qq_bot.send_group_msg
        async def fake_send_group_msg(group_id, text, reply_msg_id=None):
            self.sent_messages.append((group_id, text))
        qq_bot.send_group_msg = fake_send_group_msg

        # Mock sleep_cleanup 加快单测执行速度
        import utils.persistence
        self.orig_sleep_cleanup = utils.persistence.sleep_cleanup
        utils.persistence.sleep_cleanup = lambda: None

    async def asyncTearDown(self):
        import qq_bot
        qq_bot.send_group_msg = self.orig_send_func
        import utils.persistence
        utils.persistence.sleep_cleanup = self.orig_sleep_cleanup
        # 恢复状态
        BIORHYTHM.wake()
        wake_up()

    async def test_sleep_ignores_unmentioned_message(self):
        """睡眠状态下，未 @ 机器人的消息被忽略，不唤醒"""
        BIORHYTHM.force_sleep()
        enter_sleep()
        self.assertTrue(is_sleeping())

        data = {
            "group_id": "1108285603",
            "user_id": "3866314101",
            "raw_message": "大家都在干嘛呢？",
            "message": [{"type": "text", "data": {"text": "大家都在干嘛呢？"}}],
            "sender": {"card": "周圻晨"}
        }

        await handle_group_message(data)

        # 依然在睡眠中，未发送任何消息
        self.assertTrue(is_sleeping())
        self.assertEqual(len(self.sent_messages), 0)

    async def test_sleep_woken_by_cq_at_empty_text(self):
        """睡眠状态下，纯 CQ 码 @ 机器人唤醒并给出自然回应"""
        bot_qq = get_bot_qq()
        BIORHYTHM.force_sleep()
        enter_sleep()
        self.assertTrue(is_sleeping())

        data = {
            "group_id": "1108285603",
            "user_id": "3866314101",
            "raw_message": f"[CQ:at,qq={bot_qq},text=@辉夜]",
            "message": [
                {"type": "at", "data": {"qq": bot_qq, "text": "@辉夜"}}
            ],
            "sender": {"card": "周圻晨"}
        }

        await handle_group_message(data)

        # 生物钟已醒来
        self.assertFalse(BIORHYTHM.is_asleep())
        self.assertFalse(is_sleeping())
        # 发送了叫醒回复
        self.assertEqual(len(self.sent_messages), 1)
        group_id, reply_text = self.sent_messages[0]
        self.assertEqual(group_id, "1108285603")
        self.assertTrue(any(phrase in reply_text for phrase in ["叫醒", "迷迷糊糊", "我在呢", "醒了", "怎么啦", "犯困", "找我"]))

    async def test_sleep_woken_by_text_mention(self):
        """睡眠状态下，纯文本手打 '@辉夜' 成功唤醒"""
        BIORHYTHM.force_sleep()
        enter_sleep()
        self.assertTrue(is_sleeping())

        data = {
            "group_id": "1108285603",
            "user_id": "3866314101",
            "raw_message": "@辉夜",
            "message": [
                {"type": "text", "data": {"text": "@辉夜"}}
            ],
            "sender": {"card": "周圻晨"}
        }

        await handle_group_message(data)

        self.assertFalse(BIORHYTHM.is_asleep())
        self.assertFalse(is_sleeping())
        self.assertEqual(len(self.sent_messages), 1)

    async def test_sleep_woken_by_message_array_at(self):
        """结构化 message 数组中的 at 类型能够识别并唤醒"""
        bot_qq = get_bot_qq()
        BIORHYTHM.force_sleep()
        enter_sleep()
        self.assertTrue(is_sleeping())

        data = {
            "group_id": "1108285603",
            "user_id": "3866314101",
            # raw_message 中没有 CQ 码，但 message 数组里有 at 段
            "raw_message": "",
            "message": [
                {"type": "at", "data": {"qq": bot_qq}}
            ],
            "sender": {"card": "周圻晨"}
        }

        await handle_group_message(data)

        self.assertFalse(BIORHYTHM.is_asleep())
        self.assertFalse(is_sleeping())
        self.assertEqual(len(self.sent_messages), 1)

    async def test_sleep_woken_by_all_mention(self):
        """@全体成员 也能唤醒辉夜"""
        BIORHYTHM.force_sleep()
        enter_sleep()
        self.assertTrue(is_sleeping())

        data = {
            "group_id": "1108285603",
            "user_id": "3866314101",
            "raw_message": "[CQ:at,qq=all]",
            "message": [
                {"type": "at", "data": {"qq": "all"}}
            ],
            "sender": {"card": "周圻晨"}
        }

        await handle_group_message(data)

        self.assertFalse(BIORHYTHM.is_asleep())
        self.assertFalse(is_sleeping())
        self.assertEqual(len(self.sent_messages), 1)

    async def test_sleep_woken_by_mention_with_text(self):
        """带文本的 @ 消息能够唤醒并进入理解层注入关键词"""
        bot_qq = get_bot_qq()
        BIORHYTHM.force_sleep()
        enter_sleep()
        self.assertTrue(is_sleeping())

        injected = []
        import qq_bot
        orig_inject = qq_bot.inject_message_keywords
        qq_bot.inject_message_keywords = lambda kws: injected.extend(kws)

        from unittest.mock import patch
        with patch("core.memory_engine.create_memory", return_value="fake_mem_id"):
            try:
                data = {
                    "group_id": "1108285603",
                    "user_id": "3866314101",
                    "raw_message": f"[CQ:at,qq={bot_qq},text=@辉夜] 早上好呀",
                    "message": [
                        {"type": "at", "data": {"qq": bot_qq, "text": "@辉夜"}},
                        {"type": "text", "data": {"text": " 早上好呀"}}
                    ],
                    "sender": {"card": "周圻晨"}
                }

                await handle_group_message(data)

                self.assertFalse(BIORHYTHM.is_asleep())
                self.assertFalse(is_sleeping())
                # 关键词被提取并注入认知循环
                self.assertTrue(len(injected) > 0)
            finally:
                qq_bot.inject_message_keywords = orig_inject

    def test_biorhythm_wake_saves_to_disk(self):
        """测试 BIORHYTHM.wake() 会即时持久化到磁盘"""
        BIORHYTHM.force_sleep()
        self.assertEqual(BIORHYTHM.state, "asleep")

        BIORHYTHM.wake("test_save")
        self.assertEqual(BIORHYTHM.state, "awake")

        import json
        with open("data/test/biorhythm.json", "r", encoding="utf-8") as f:
            disk_state = json.load(f)
        self.assertEqual(disk_state.get("state"), "awake")
        self.assertEqual(disk_state.get("awake_until"), BIORHYTHM.awake_until)

    def test_control_panel_chat_wake(self):
        """控制面板测试对话在 mentioned=True 时联动唤醒生物钟"""
        BIORHYTHM.force_sleep()
        enter_sleep()
        self.assertTrue(is_sleeping())

        from unittest.mock import patch, AsyncMock
        with patch("core.cognition.process_dialogue", AsyncMock(return_value=("好的", None))):
            from control_panel import Runtime
            runtime = Runtime()
            runtime.initialized = True
            sender, reply = runtime.chat("测试者", "你好", "1108285603", mentioned=True)

        self.assertFalse(BIORHYTHM.is_asleep())
        self.assertFalse(is_sleeping())
        self.assertEqual(reply, "好的")

if __name__ == "__main__":
    unittest.main()
