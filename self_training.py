import os
import sys
import threading
import logging
import json
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
RUN_DIR = PROJECT_DIR / "run"
LOG_DIR = RUN_DIR / "logs"
for d in [LOG_DIR, PROJECT_DIR / "data" / "test", PROJECT_DIR / "data" / "models"]:
    d.mkdir(parents=True, exist_ok=True)

_log_file = LOG_DIR / "self_training.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(str(_log_file), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

if sys.platform == "linux":
    local_lib = RUN_DIR / "lib" / "usr" / "lib" / "x86_64-linux-gnu"
    if local_lib.is_dir():
        os.environ.setdefault("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = f"{local_lib}:{os.environ['LD_LIBRARY_PATH']}".rstrip(":")
    local_qt = RUN_DIR / "qt5-plugins"
    if (local_qt / "platforminputcontexts").is_dir():
        os.environ["QT_PLUGIN_PATH"] = str(local_qt)
    os.environ.setdefault("QT_IM_MODULE", "fcitx")
    os.environ.setdefault("GTK_IM_MODULE", "fcitx")
    os.environ.setdefault("XMODIFIERS", "@im=fcitx")

from core.virtual_clock import clock
from core.memory_engine import memories, create_memory, add_link, pathfind_activation, retrieve_similar
from core.llm_interface import call_api_thinking, verbalize
from utils.world_layer import WORLD, HOME_ZONES
from utils.caiye import CAIYE
from utils.time_phrases import get_relative_time_phrase
from utils.dialogue_state import get_state, set_state
from utils.persistence import save_state, save_all_data
from utils.event_bus import BUS
from utils.message_history import add_message, get_recent

logger = logging.getLogger("SelfTraining")
world_logger = logging.getLogger("WorldGen")

BASE_SYSTEM = "你是一个角色扮演AI。根据用户指令扮演指定角色并输出对应内容。只输出要求的内容，不要多余解释。"

ACTION_PROMPT = """你是一个动作选择器。根据辉夜当前的冲动和外部环境，输出一个标准化的动作指令。

可选动作指令：
- 移动到[位置]  例：移动到厨房
- 进食
- 休息
- 查看/环顾

【规则】
1. 只输出动作指令本身，不要加"做"字前缀
2. 指代人或物时使用具体名称（如"彩叶""泡面"），不要用"你""我""它"等代词
3. 只能从当前房间移动到相邻房间：当前房间 {location} 的相邻房间是 {connected}
4. 如果当前处于受伤/应激状态（{injury_hint}），输出"无"
5. 只根据以上提供的信息输出，不得添加未给出的内容

输出格式：只输出动作指令文本。如果无法执行任何动作，输出"无"。

当前冲动：{intention}
当前环境：{location_desc}

回忆：{memory_context}
"""


class SelfTraining:
    def __init__(self):
        self.running = False
        self._thread = None
        self._stop = threading.Event()
        self._cycle_done = threading.Event()
        self._cycle_done.set()
        self.cycle_interval = 30
        self._round_num = 0

    def start(self):
        if self.running:
            return
        WORLD.load_scene()
        if hasattr(clock, "now"):
            WORLD.last_tick_virtual = clock.now()
        self._round_num = 0
        self.running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("自训练循环已启动（间隔 %d 秒）", self.cycle_interval)

    def stop(self, block=False):
        if not self.running:
            return
        self.running = False
        self._stop.set()
        if block:
            self._cycle_done.wait(120)
            if self._thread:
                self._thread.join(timeout=5)
            WORLD.save_scene()
            logger.info("自训练循环已停止，场景已持久化")
        else:
            logger.info("自训练循环已请求停止")

    def set_interval(self, seconds):
        self.cycle_interval = max(5, int(seconds))
        logger.info("自训练间隔已设为 %d 秒", self.cycle_interval)

    def _loop(self):
        while not self._stop.is_set():
            self._cycle_done.clear()
            try:
                self._cycle()
            except Exception as e:
                logger.exception("自训练循环异常: %s", e)
            self._cycle_done.set()
            self._stop.wait(self.cycle_interval)
        WORLD.save_scene()
        logger.info("自训练循环已停止，场景已持久化")

    def _calc_stamina(self, snapshot):
        hunger_ratio = snapshot["hunger"] / snapshot["max_hunger"]
        fatigue_ratio = snapshot["fatigue"] / snapshot["max_fatigue"]
        base = 3.0
        penalty = (hunger_ratio + fatigue_ratio) * 1.5
        return max(0.5, base - penalty)

    def _retrieve(self, keywords, stamina, top_k=5):
        seed_ids = []
        for kw in keywords:
            if not kw:
                continue
            try:
                similar = retrieve_similar(kw, k=top_k)
                for score, mem in similar:
                    mem_id = mem["id"]
                    if mem_id not in seed_ids:
                        seed_ids.append(mem_id)
            except Exception:
                pass
        activated = []
        if seed_ids:
            try:
                activated = pathfind_activation(seed_ids, max_stamina=stamina, top_k=top_k, max_steps=1)
            except Exception:
                pass
        seen = set()
        combined = []
        for mem, score in activated:
            mid = mem["id"]
            if mid not in seen:
                seen.add(mid)
                combined.append((score, mem))
        combined.sort(key=lambda x: x[0], reverse=True)
        lines = []
        for score, mem in combined[:top_k]:
            ts = mem.get("creation_time", 0)
            real_ts = clock.to_real_time(ts) if hasattr(clock, "to_real_time") else ts
            try:
                phrase = get_relative_time_phrase(real_ts)
            except Exception:
                phrase = ""
            lines.append(f"[{phrase}] {mem['content']}")
        return lines

    def _cycle(self):
        self._round_num += 1
        WORLD.tick()
        snapshot = WORLD.get_snapshot()
        stamina = self._calc_stamina(snapshot)

        world_logger.info("=== 第 %d 轮 ===", self._round_num)
        world_logger.info("位置: %s | 时段: %s | 天气: %s", snapshot["location"], snapshot["time_of_day"], snapshot["weather"])
        world_logger.info("环境: %s", snapshot["location_desc"])
        world_logger.info("可通行: %s", snapshot["exits"])
        world_logger.info("饥饿: %.1f/%d | 疲劳: %.1f/%d | 体力(行走): %.1f", snapshot["hunger"], snapshot["max_hunger"], snapshot["fatigue"], snapshot["max_fatigue"], stamina)

        vision, v_kws = self._perceive_vision(snapshot)
        hearing, h_kws = self._perceive_hearing(snapshot)
        feeling, f_kws = self._perceive_feeling(snapshot)
        logger.info("[视觉] %s", vision)
        logger.info("[听觉] %s", hearing)
        logger.info("[感觉] %s", feeling)

        sensory_ids = []
        for text in [vision, hearing, feeling]:
            if text and text != "无":
                mid = create_memory(text, half_life=600)
                sensory_ids.append(mid)

        if len(sensory_ids) >= 2:
            for i in range(len(sensory_ids)):
                for j in range(i + 1, len(sensory_ids)):
                    add_link(sensory_ids[i], sensory_ids[j], 1.0, "cotemporal")
                    add_link(sensory_ids[j], sensory_ids[i], 1.0, "cotemporal")

        all_kws = []
        seen_kw = set()
        for kw in v_kws + h_kws + f_kws:
            kw = kw.strip()
            if kw and kw not in seen_kw:
                seen_kw.add(kw)
                all_kws.append(kw)
        first_memories = self._retrieve(all_kws, stamina, top_k=5) if all_kws else []

        first_memories_str = "\n".join(first_memories) if first_memories else ""
        intention_text, state_text, decision_kws = self._decide(snapshot, vision, hearing, feeling, first_memories_str, stamina)
        logger.info("[意图] %s", intention_text)
        logger.info("[状态] %s", state_text)
        logger.info("[决策关键词] %s", decision_kws)

        second_memories = self._retrieve(decision_kws, stamina, top_k=5) if decision_kws else []
        merged_set = set()
        merged_lines = []
        for m in first_memories + second_memories:
            m = m.strip()
            if m and m not in merged_set:
                merged_set.add(m)
                merged_lines.append(m)

        from utils.dialogue_state import get_state as get_ds
        lang_result = verbalize(
            memories=merged_lines,
            keywords=decision_kws,
            new_state=get_ds(),
            user_input=intention_text,
            system_prompt="你是辉夜，根据当前的回忆和处境，自然地说出一句话。不要添加任何动作或神态描述，不要用括号补充说明，不要包含任何非语言的标注。只根据以上提供的信息输出，不得添加未给出的内容。"
        )
        merged_context_str = "\n".join(merged_lines)
        action_cmd = self._respond_action(snapshot, intention_text, merged_context_str)
        logger.info("[语言] %s", lang_result)
        logger.info("[动作] %s", action_cmd or "无")
        if lang_result:
            BUS.message.emit("辉夜", lang_result, "自训练")
            add_message("辉夜", lang_result, "自训练")

        caiye_reply = CAIYE.respond(lang_result, snapshot) if lang_result else None
        logger.info("[彩叶] %s", caiye_reply or "（沉默）")
        if caiye_reply:
            BUS.message.emit("彩叶", caiye_reply, "自训练")
            add_message("彩叶", caiye_reply, "自训练")

        move_result = self._apply_movement(action_cmd) if action_cmd else None
        world_logger.info("[移动] %s", move_result or "无动作")

        WORLD.prev_utterance = lang_result or ""
        WORLD.prev_action = move_result or ""
        WORLD.prev_caiye = caiye_reply or ""

        if move_result:
            create_memory(f"我{move_result}", half_life=6*3600)
        if lang_result:
            create_memory(f"我对彩叶说：{lang_result}", half_life=12*3600)
        WORLD.save_scene()
        save_all_data()

    def _extract_keywords(self, text):
        if not text:
            return []
        import re
        results = re.findall(r'\[关键词[:：]\s*(.+?)(?:\n|$)', text)
        if results:
            return [kw.strip() for kw in results[-1].replace("，", ",").split(",") if kw.strip()][:2]
        return []

    def _perceive_vision(self, snapshot):
        prev = ""
        if snapshot["prev_utterance"]:
            prev += f"刚才我说：{snapshot['prev_utterance']}\n"
        if snapshot["prev_action"]:
            prev += f"刚才我：{snapshot['prev_action']}\n"
        prompt = f"""位置：{snapshot['location']}
环境描述：{snapshot['location_desc']}
时段：{snapshot['time_of_day']}
天气：{snapshot['weather']}
{prev}
【指令】你扮演辉夜的视觉感知通道。输出两句：
第一行：一句以"我看到"开头的第一人称视觉陈述，只写客观可见的内容。
第二行：[关键词：<最多2个关键词，用逗号分隔>]
只根据以上提供的信息输出，不得添加未给出的内容。"""
        msg = [{"role": "system", "content": BASE_SYSTEM},
               {"role": "user", "content": prompt}]
        raw = call_api_thinking(msg, max_tokens=8000) or "我看到周围的环境\n[关键词：环境]"
        lines = raw.split("\n", 1)
        text = lines[0].strip() or "我看到周围的环境"
        kws = self._extract_keywords(raw)
        return text, kws

    def _perceive_hearing(self, snapshot):
        prev = ""
        if snapshot["prev_utterance"]:
            prev += f"刚才我说：{snapshot['prev_utterance']}\n"
        if snapshot["prev_action"]:
            prev += f"刚才我：{snapshot['prev_action']}\n"
        prompt = f"""位置：{snapshot['location']}
环境描述：{snapshot['location_desc']}
时段：{snapshot['time_of_day']}
{prev}"""
        if snapshot["prev_caiye"]:
            prompt += f"\n刚才听到的话：{snapshot['prev_caiye']}"
        prompt += '\n【指令】你扮演辉夜的听觉感知通道。输出两句：\n第一行：一句以"我听到"开头的第一人称听觉陈述（如果没有特殊声音则写"无"）。\n第二行：[关键词：<最多2个关键词，用逗号分隔>]\n只根据以上提供的信息输出，不得添加未给出的内容。'
        msg = [{"role": "system", "content": BASE_SYSTEM},
               {"role": "user", "content": prompt}]
        raw = call_api_thinking(msg, max_tokens=8000) or "无\n[关键词：安静]"
        lines = raw.split("\n", 1)
        text = lines[0].strip() or "无"
        kws = self._extract_keywords(raw)
        return text, kws

    def _perceive_feeling(self, snapshot):
        prev = ""
        if snapshot["prev_utterance"]:
            prev += f"刚才我说：{snapshot['prev_utterance']}\n"
        if snapshot["prev_action"]:
            prev += f"刚才我：{snapshot['prev_action']}\n"
        prompt = f"""饥饿值：{snapshot['hunger']}/{snapshot['max_hunger']}（越高越饿）
疲劳值：{snapshot['fatigue']}/{snapshot['max_fatigue']}（越高越累）
{prev}
【指令】你扮演辉夜的身体感觉通道。输出两句：
第一行：一句以"我感觉"开头的第一人称身体感受陈述（饥饿值>50描述饥饿感，疲劳值>50描述疲劳感，都低则写"我感觉身体状态正常"）。
第二行：[关键词：<最多2个关键词，用逗号分隔>]
只根据以上提供的信息输出，不得添加未给出的内容。"""
        msg = [{"role": "system", "content": BASE_SYSTEM},
               {"role": "user", "content": prompt}]
        raw = call_api_thinking(msg, max_tokens=8000) or "我感觉身体状态正常\n[关键词：正常]"
        lines = raw.split("\n", 1)
        text = lines[0].strip() or "我感觉身体状态正常"
        kws = self._extract_keywords(raw)
        return text, kws

    def _build_conversation_history(self, max_turns=4):
        return get_recent(max_turns * 2)

    def _decide(self, snapshot, vision, hearing, feeling, memory_context, stamina):
        cur_state = get_state()
        state_hint = json.dumps({"参与者": cur_state.get("参与者", []), "topic": cur_state.get("最近话题", ""), "info": cur_state.get("我的已知信息", [])}, ensure_ascii=False)
        conv = self._build_conversation_history()
        prompt = f"""当前对话状态：{state_hint}
当前环境：{snapshot['location_desc']}
视觉：{vision}
听觉：{hearing}
身体感觉：{feeling}

最近的对话：
{conv}

你想起的回忆：
{memory_context}
【指令】你扮演辉夜的内部决策机制。输出四行：
第一行：一句以"我想"开头的意图自述。
第二行：一句以"我感到"开头的状态自述。
第三行：[关键词：<2个关键词，用逗号分隔>]
第四行：状态更新 JSON — {{"participants":["辉夜",...],"topic":"...","info":["..."]}}
只根据以上提供的信息输出，不得添加未给出的内容。"""
        msg = [{"role": "system", "content": BASE_SYSTEM},
               {"role": "user", "content": prompt}]
        raw = call_api_thinking(msg, max_tokens=8000) or "我想四处看看\n我感到身体状态正常\n[关键词：环境，探索]\n{\"participants\":[\"辉夜\"],\"topic\":\"\",\"info\":[]}"
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        intention = "我想四处看看"
        state = "我感到身体状态正常"
        kws = []
        state_json = None
        for l in lines:
            if l.startswith("我想") and intention == "我想四处看看":
                intention = l
            elif l.startswith("我感到") and state == "我感到身体状态正常":
                state = l
            elif "关键词" in l or l.startswith("["):
                kws = self._extract_keywords(l)
            elif l.startswith("{"):
                try:
                    state_json = json.loads(l)
                except Exception:
                    pass
        if state_json:
            mapped = {
                "参与者": state_json.get("participants", cur_state.get("参与者", [])),
                "最近话题": state_json.get("topic", cur_state.get("最近话题", "")),
                "我的已知信息": state_json.get("info", cur_state.get("我的已知信息", [])),
            }
            set_state(mapped)
            save_state()
        return intention, state, kws

    def _apply_movement(self, action_cmd):
        action_cmd = action_cmd.strip().lower()
        if action_cmd.startswith("移动"):
            for zone_name in HOME_ZONES:
                if zone_name in action_cmd:
                    current = HOME_ZONES.get(WORLD.location, HOME_ZONES["客厅"])
                    if zone_name in current["connected"]:
                        WORLD.location = zone_name
                        return f"辉夜移动到了{zone_name}"
                    return f"辉夜试图移动到{zone_name}但无法通行"
        return None

    def _respond_action(self, snapshot, intention, memory_context):
        current_zone = HOME_ZONES.get(WORLD.location, HOME_ZONES["客厅"])
        connected = "、".join(current_zone["connected"])
        prompt = ACTION_PROMPT.format(
            intention=intention,
            injury_hint="无",
            location=snapshot["location"],
            connected=connected,
            location_desc=snapshot["location_desc"],
            memory_context=memory_context,
        )
        msg = [{"role": "system", "content": BASE_SYSTEM},
               {"role": "user", "content": prompt}]
        result = call_api_thinking(msg, max_tokens=8000)
        if result and result.strip() != "无":
            return result.strip()
        return None


TRAINER = SelfTraining()

if __name__ == "__main__":
    logging.info("启动自训练（独立模式）")
    TRAINER.start()
    try:
        while TRAINER.running:
            import time
            time.sleep(1)
    except KeyboardInterrupt:
        TRAINER.stop()
        logging.info("自训练已停止")
