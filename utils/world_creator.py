# untils.world_creator.py
# 本工具实现一个由LLM自组织的世界输入生成器
import json
import time
import random
from openai import OpenAI
from utils.monitor import append_log

NOVEL_FILE = "data/超时空辉夜姬！.txt"
test = {"lines": [1, 141, 373, 471, 567, 819, 885, 1001, 1151, 1373, 1415, 1635, 1855, 2063, 2267, 2397, 2895, 3031], "offsets": [0.0, 7200.0, 86400.0, 172800.0, 259200.0, 345600.0, 432000.0, 518400.0, 604800.0, 691200.0, 777600.0, 864000.0, 950400.0, 1036800.0, 1123200.0, 1209600.0, 1296000.0, 1382400.0]}

from config.api_config import config

client = OpenAI(
    api_key=config["primary_api_key"],
    base_url=config["primary_base_url"],
)

MODEL = config["primary_model"]

def _call_api(messages, max_tokens=8192):
    """
    世界生成自用函数，严谨对外使用!!!(避免命名冲突)
    """
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.3,
            extra_body={"thinking": {"type": "disabled"}}
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        err_msg = f"[WorldCreator API Error] {e}"
        print(err_msg)
        append_log(err_msg)
        return None

# ==================== 知识追踪器 ====================
class KnowledgeTracker:
    def __init__(self):
        self.learned = set()
        self.experienced = set()
        self.met_people = set()
        self.last_round_new = {"learned": set(), "experienced": set(), "met_people": set()}

    def update_from_outlines(self, outlines: list):
        old_learned = self.learned.copy()
        old_experienced = self.experienced.copy()
        old_people = self.met_people.copy()

        for item in outlines:
            for kw in item.get("new_knowledge", []):
                self.learned.add(kw)
            for exp in item.get("new_experiences", []):
                self.experienced.add(exp)
            for person in item.get("new_people", []):
                self.met_people.add(person)

        self.last_round_new = {
            "learned": self.learned - old_learned,
            "experienced": self.experienced - old_experienced,
            "met_people": self.met_people - old_people
        }

    def get_new_knowledge_message(self) -> dict:
        """
        返回一条 assistant 消息，内容为本轮新增的知识点。
        用于追加到对话历史中，供后续场景复用。
        """
        parts = []
        if self.last_round_new["learned"]:
            parts.append(f"新学会：{', '.join(sorted(self.last_round_new['learned']))}")
        if self.last_round_new["experienced"]:
            parts.append(f"新经历：{', '.join(sorted(self.last_round_new['experienced']))}")
        if self.last_round_new["met_people"]:
            parts.append(f"新认识：{', '.join(sorted(self.last_round_new['met_people']))}")
        if not parts:
            parts.append("（本场景无新增知识点）")
        return {"role": "assistant", "content": "；".join(parts)}

# ==================== 小说分段 ====================
def number_lines(text: str) -> str:
    """
    给文本的每一行加上行号，格式：行号|内容
    """
    lines = text.splitlines()  # 或 split('\n')，splitlines 会正确处理各种换行符
    numbered = [f"{i+1}|{line}" for i, line in enumerate(lines)]
    return "\n".join(numbered)

def _create_segmentation_prompt(numbered_lines: str) -> str:
    """
    生成小说场景分割的提示词。
    numbered_lines: 一个字符串，每行是 "行号|内容"，例如 "1|“那个——那个——”"。
    """
    prompt = """你是一个文本分割专家。请理解以下带行号的小说内容，按场景切分成独立的叙事块。

【场景定义】
一个“场景”是一段在连续时间、同一地点发生的，围绕核心互动的事件。
当时间跳跃（如“第二天”、“一周后”）、地点变更或话题完全转变时，视为新场景。
每个场景的理想长度为 20-60 行，但以叙事完整为准。

【任务】
1. 找出所有新场景的起始行号。第一个场景总是从第1行开始。
2. 为每个场景估算一个相对于故事起点的**绝对时间偏移量**（秒）。如果文中没有明确时间提示，请根据事件顺序合理推断。

【输出格式】
只输出一个 JSON 对象，包含两个数组。第一个数组是场景起始行号，第二个数组是对应的时间偏移量（秒）。
例如：{"lines": [1, 48, 93, 152], "offsets": [0.0, 7200.0, 86400.0, 172800.0]}
"""
    content = f"""【小说文本（行号|内容）】
{numbered_lines}
    """
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": content}
    ]
    return messages

def print_scenes(scenes):
    """打印每个场景的标题和内容"""
    output_file = "scenes_output.txt"
    with open(output_file, "w", encoding="utf-8") as f:
        for i, scene in enumerate(scenes, 1):
            f.write(f"{'='*20} 场景 {i} {'='*20}\n")
            f.write(scene + "\n\n")

# ==================== 决策层（视角转换 + 大纲生成） ====================
def _world_deciding(scene_text: str) -> list:
    prompt = f"""你是视角转换器。将以下【彩叶视角的小说场景】转换为客观事件大纲。

【转换规则】
1. 剥离彩叶的内心独白和主观评价（如“真是的”、“火大”、“这家伙”等）。
2. 只提取客观事实：谁做了什么、谁说了什么、发生了什么。
3. 那个少女/婴儿指的是”辉夜“，请使用”辉夜“作为名字，不要用代称。
4. 禁止添加情感解读（如“她很开心”），只写客观行为（如“她笑了”）。
5. 如果小说中出现了辉夜已经学会的内容，不需要重复生成“学习”类事件，但可以生成“实践”类事件。
6. 为每个事件分配一个时间偏移量（秒），从0.0开始，精度0.1秒，按事件先后顺序递增。
7. 时间间隔约束：即使是紧密连续的动作，每个事件之间也必须间隔至少1秒。对于吃饭、外出、睡觉等有明显时间跨度的活动，间隔应拉长到 600-3600 秒。禁止多个事件的偏移量完全相同或相差不足1秒。
8. 为每个事件提取新出现的知识点、新经历的场景、新认识的人（如无则留空数组）。

【输出格式】
严格输出JSON数组，无任何解释：
[{{"offset": 0.0, "outline": "辉夜做了什么/看到什么/听到什么", "new_knowledge": ["知识点"], "new_experiences": ["场景"], "new_people": ["人名"]}}]
"""
    return [
        {"role": "system", "content": prompt},
        # 用户消息在调用处拼接
    ]


# ==================== 填充器 ====================
FILLER_PROMPT_TEMPLATE = """你是日常事件生成器。辉夜是一个来自月球的少女，目前和彩叶一起生活。她正在度过一段没有特殊事件的日常时光。

{knowledge_context}

【任务】
请为辉夜生成 {event_count} 个日常事件大纲，均匀分布在从 {start_offset} 到 {end_offset} 的时间范围内。

【约束】
1. 事件必须基于辉夜**已经学会或经历过**的事情。禁止引入任何新知识、新技能或新人物。
2. 每个事件是一个客观陈述，以“辉夜”开头，如“辉夜独自整理了自己的床铺”。
3. 时间偏移量（offset）单位为秒，精度0.1秒。
4. 所有事件的 new_knowledge、new_experiences、new_people 字段全部为空数组。

【输出格式】
严格输出JSON数组，无任何解释：
[
  {{"offset": 3600.0, "outline": "辉夜做了什么事", "new_knowledge": [], "new_experiences": [], "new_people": []}}
]
"""


# ==================== 分散 + 填充 ====================
def _disperse_and_fill(outlines: list, tracker: KnowledgeTracker, scene_start_time: float) -> list:
    GAP_THRESHOLD = 4 * 3600   # 超过4小时视为空白
    EVENTS_PER_HOUR = 0.5      # 每2小时生成1个填充事件

    filled = []
    prev_end_time = scene_start_time

    for event in sorted(outlines, key=lambda x: x["offset"]):
        event_abs_time = scene_start_time + event["offset"]

        gap = event_abs_time - prev_end_time
        if gap > GAP_THRESHOLD:
            fill_count = max(1, int(gap / 3600 * EVENTS_PER_HOUR))
            append_log(f"[WorldCreator] 检测到时间空白 {gap/3600:.1f}小时，生成 {fill_count} 条填充事件")
            filler_messages = [
                {"role": "system", "content": "你是日常事件生成器，只输出JSON数组。"},
                {"role": "user", "content": FILLER_PROMPT_TEMPLATE.format(
                    knowledge_context=tracker.get_state_context(),
                    event_count=fill_count,
                    start_offset=prev_end_time - scene_start_time,
                    end_offset=event_abs_time - scene_start_time
                )}
            ]
            raw = _call_api(filler_messages)
            if raw:
                try:
                    filler_events = json.loads(raw.strip("```").strip("json"))
                    filled.extend(filler_events)
                except:
                    append_log("[WorldCreator] 填充事件解析失败，跳过")

        filled.append(event)
        prev_end_time = event_abs_time + 1

    return filled


# ==================== 后续生成模块（全部返回 messages） ====================
def _world_happening(outline_text: str) -> list:
    prompt = """你是事件扩写器。将以下事件大纲扩展为一段详细的事件描述（100-200字）。

【规则】
1. 用第三人称叙述，保持客观。
2. 只写可被观察到的行为、对话和环境细节。
3. 禁止添加角色的内心想法或情感评价。
"""
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"【事件大纲】\n{outline_text}"}
    ]


def _world_environment(event_detail: str) -> list:
    prompt = """你是环境推断器。根据以下事件描述，推断辉夜(那个少女/婴儿)此刻所处的环境。

【规则】
1. 用一句话描述她周围能感知到的东西：地点、光线、温度、声音、气味等。
2. 只写辉夜能直接感受到的环境信息。
3. 禁止添加情感氛围描写（如“温馨的房间”），只写客观物理环境。
"""
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"【事件描述】\n{event_detail}"}
    ]


def _world_actions(event_detail: str) -> list:
    prompt = """你是动作提取器。从以下事件描述中，提取辉夜(那个少女/婴儿)本人的具体动作。

【规则】
1. 只写辉夜做了什么（包括说了什么、看了什么、去了哪里）。
2. 用简短句子列举，每句一个动作。
3. 禁止添加其他人的动作或内心想法。
4. 如果事件中没有辉夜的动作，返回“无”。
"""
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"【事件描述】\n{event_detail}"}
    ]


def _recepter(environment: str, actions: str) -> list:
    prompt = """你是感官记录器。根据以下环境信息和辉夜(那个少女/婴儿)的动作，描述她此刻的客观身体感受。

【规则】
1. 只写感官输入：看到、听到、闻到、触到、身体感觉（冷/热/痛/痒等）。
2. 禁止写情感（开心/难过/害怕/喜欢）。
3. 如果没有特殊感官输入，写“无特殊感官输入”。
4. 一句话概括。
"""
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"【环境】\n{environment}\n\n【辉夜的动作】\n{actions}"}
    ]


def _saying(environment: str, actions: str, feeling: str, event_detail: str) -> list:
    prompt = """你是辉夜，也就是小说中提到的那个少女/婴儿。请根据以下信息，生成尽可能多的第一人称记忆片段。

【规则】
1. 每条记忆以“我”开头，是一个完整清晰的陈述句。
2. 只写客观事实，不写情感评价（如“我很开心”）。
3. 不添加信息中没有的内容。
4. 如果感受栏是“无特殊感官输入”，不要在记忆中编造感官细节。
5. 每条记忆末尾标注类型：[daily_routine/learning/emotional/default]

【输出格式】（每行一条）
[陈述句] | [类型]
"""
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"【环境】{environment}\n【你的动作】{actions}\n【你的感受】{feeling}\n【事件详述】{event_detail}"}
    ]


# ==================== 记忆注入器 ====================
HALF_LIFE_RULES = {
    "daily_routine": 12 * 3600,
    "learning": 7 * 24 * 3600,
    "emotional": 30 * 24 * 3600,
    "default": 3 * 24 * 3600
}

def _inject_memory(content: str, mem_type: str, absolute_time: float):
    """
    使用 create_memory 创建记忆，然后手动修正时间戳以匹配事件发生的绝对虚拟时间。
    """
    from core.memory_engine import create_memory, semantic_dedup, _get_db
    from core.virtual_clock import clock

    # 先去重（只检查内存，基本够用）
    if semantic_dedup(content):
        append_log(f"[WorldCreator] 记忆重复，跳过: {content[:30]}...")
        return

    half_life = HALF_LIFE_RULES.get(mem_type, HALF_LIFE_RULES["default"])
    mem_id = create_memory(content, half_life=half_life)

    # 修正时间戳：覆盖 creation_time 和 last_accessed
    from core.memory_engine import memories
    mem = memories.get(mem_id)
    if mem:
        mem["creation_time"] = absolute_time
        mem["last_accessed"] = absolute_time
        mem["last_strengthen_time"] = absolute_time
        # 同步到 SQLite
        db = _get_db()
        db.execute(
            "UPDATE memories SET creation_time=?, last_accessed=?, last_strengthen_time=? WHERE id=?",
            (absolute_time, absolute_time, absolute_time, mem_id)
        )
        db.commit()


# ==================== 顶层调度 ====================
def world_training():
    """
    切分整部小说，对每一部分都有：
    决策->时间轴分散->生成事件详情->生成环境&动作->生成主观感受->综上并生成主观叙述
    """
    try:
        print("[WorldCreator] 当前正在使用注入法进行预训练，一经启动，无法中断，请慎重考虑！")
        user_in = input("[WorldCreator] 同意并开始(Y):")
        if user_in == "Y":
            jump_to = 0
        elif user_in == "X":
            jump_to = int(input("[WorldCreator] 跳转模式，跳转至第几个场景（请务必输入自然数）："))
        else:
            user_in = input("[WorldCreator] 同意并开始(Y):")
            if user_in == "Y":
                jump_to = 0
            elif user_in == "X":
                jump_to = int(input("[WorldCreator] 跳转模式，跳转至第几个场景（请务必输入自然数）："))
            else:
                return
        from core.memory_engine import _rebuild_faiss_index
        from core.virtual_clock import clock

        tracker = KnowledgeTracker()
        knowledge_history = []  # 累积的知识消息历史

        with open(NOVEL_FILE, "r", encoding="gbk") as f:
            novel_text = f.read()
        append_log(f"[WorldCreator] 已读取小说，总行数: {len(novel_text.splitlines())}")

        numbered_text = number_lines(novel_text)
        if test:
            raw_result = test
        else:
            raw_result = _call_api(_create_segmentation_prompt(numbered_text))
            print(f"分割结果：{raw_result}")

        try:
            raw_cleaned = raw_result
            if isinstance(raw_result, str):
                # 如果是字符串，清理格式后解析
                raw_cleaned = raw_result.strip("```json").strip("```").strip("json")
                seg_data = json.loads(raw_cleaned)
            else:
                # 如果已经是字典，直接使用
                seg_data = raw_result

            split_lines = seg_data["lines"]
            split_offsets = seg_data["offsets"]
        except Exception as e:
            print(f"[Error] 分割结果解析失败: {e}")
            return

        lines = novel_text.splitlines()
        split_lines.append(len(lines) + 1)  # 终点

        scenes = []
        for i in range(len(split_lines) - 1):
            start_line = split_lines[i]
            end_line = split_lines[i + 1]
            scene_text = '\n'.join(lines[start_line-1:end_line-1]).strip()
            if scene_text:
                scenes.append({
                    "text": scene_text,
                    "absolute_start": split_offsets[i]  # 本场景的绝对起始时间
                })
        # print_scenes(scenes)      # 需要时取消注释以保存分段结果

        print(f"[WorldCreator] 共分割出 {len(scenes)} 个场景，开始处理...")
        append_log(f"[WorldCreator] 共切分为 {len(scenes)} 个场景，开始处理")
        
        idx_debug = 0
        for idx, scene_info in enumerate(scenes, 1):
            if idx < jump_to:
                continue
            idx_debug = idx
            print(f"\n====== 场景 {idx}/{len(scenes)} ======")
            append_log(f"[WorldCreator] --- 开始处理场景 {idx}/{len(scenes)} ---")

            scene_text = scene_info["text"]
            scene_absolute_start = scene_info["absolute_start"]
            # 1. 决策：生成大纲
            decision_messages = _world_deciding(scene_text)  # 返回 [system_msg, user_msg_placeholder]
            # 增加 user 消息为实际场景文本
            decision_messages.append({"role": "user", "content": f"【彩叶视角的小说场景】\n{scene_text}"})
            # 在 system 消息和 user 消息之间插入知识历史
            decision_messages = [decision_messages[0]] + knowledge_history + [decision_messages[1]]
            #print(f"拼接消息：{decision_messages}")

            raw_outlines = _call_api(decision_messages)
            if not raw_outlines:
                continue
            try:
                outlines = json.loads(raw_outlines.strip("```").strip("json"))
            except:
                print(f"[警告] 场景{idx}大纲解析失败，跳过")
                print(f"[警告] 场景{idx}内容为：{raw_outlines}")
                continue

            if outlines:
                append_log(f"[WorldCreator] 场景{idx} 大纲解析成功，生成 {len(outlines)} 条大纲")
            else:
                append_log(f"[WorldCreator] 场景{idx} 大纲解析失败，跳过")

            # 2. 更新知识追踪器
            tracker.update_from_outlines(outlines)

            # 将本轮新增知识点追加到历史中
            knowledge_history.append(tracker.get_new_knowledge_message())

            # 3. 分散 + 填充
            filled_outlines = _disperse_and_fill(outlines, tracker, scene_absolute_start)
            append_log(f"[WorldCreator] 场景{idx} 分散填充后共 {len(filled_outlines)} 条事件")

            # 4. 逐事件生成记忆并注入
            injected_count = 0
            event_count = 1
            for event in filled_outlines:
                append_log(f"[WorldCreator] 处理事件 {event_count}")
                outline_text = event.get("outline", "")
                offset = event.get("offset", 0.0)

                append_log(f"[WorldCreator] 生成事件: {outline_text[:50]}... (offset={offset})")        # 逐事件打印，刷屏注释

                # 生成详细事件
                happen_raw = _call_api(_world_happening(outline_text))
                event_detail = happen_raw or outline_text

                # 生成环境
                env_raw = _call_api(_world_environment(event_detail))
                environment = env_raw or "普通室内环境"

                # 生成动作
                act_raw = _call_api(_world_actions(event_detail))
                actions = act_raw or "无"

                # 生成感受
                feel_raw = _call_api(_recepter(environment, actions))
                feeling = feel_raw or "无特殊感官输入"

                # 生成第一人称自述
                say_raw = _call_api(_saying(environment, actions, feeling, event_detail))
                if not say_raw:
                    continue

                # 解析自述行并注入
                for line in say_raw.strip().split("\n"):
                    if "|" in line:
                        content, mem_type = line.rsplit("|", 1)
                        content = content.strip()
                        mem_type = mem_type.strip()
                        absolute_time = scene_absolute_start + event["offset"]
                        print(f"生成记忆：{content[:50]}......")
                        _inject_memory(content, mem_type, absolute_time)
                        injected_count += 1

                event_count += 1
                # 温和节流，避免API限速
                time.sleep(0.3)
            append_log(f"[WorldCreator] 场景{idx} 共注入 {injected_count} 条记忆")
        append_log("[WorldCreator] 世界生成完成，即将重建faiss索引")

        # 5. 全部完成后重建 faiss 索引
        from utils.persistence import save_all_data, save_state
        save_all_data()
        save_state()
        _rebuild_faiss_index()
        print("[WorldCreator] 世界生成完成，faiss索引已重建。")

    except Exception as e:
        print(f"[警告] 报错：{e}")
        from utils.persistence import save_all_data, save_state
        save_all_data()
        save_state()
        _rebuild_faiss_index()
        print("[WorldCreator] 世界生成出错，已保存完成部分，faiss索引已重建。")
        print(f"[WorldCreator] 世界生成出错，当前注入场景：{idx_debug}场景")

if __name__ == "__main__":
    world_training()