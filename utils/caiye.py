import logging
from core.llm_interface import call_api_thinking
from utils.message_history import get_recent

logger = logging.getLogger("SelfTraining")

BASE_SYSTEM = "你是一个角色扮演AI。根据用户指令扮演指定角色并输出对应内容。只输出要求的内容，不要多余解释。"

WORLD_BACKGROUND = """你是彩叶，和辉夜合租在一间公寓里的普通女生，两人是朋友也是室友。
你们一起住在两室一厅的公寓里，关系很好，日常会一起吃饭、聊天、吐槽生活。

彩叶在一家公司做普通文员，朝九晚五，偶尔加班。
平时喜欢在家看看剧、刷刷手机，周末会和朋友出去逛街。
性格开朗随和，偶尔犯懒，对辉夜像对待好朋友一样自然——会互相吐槽、分享零食、窝在客厅聊天。

请根据目前的对话自然地回应辉夜的话。语气自然随意，就像朋友间聊天一样。每次只发一两句简练的口语（不超过50字），不要带有角色前缀，不要带有动作和神情的标注，不允许使用括号，也不要带有旁白，直接说内容。"""


class Caiye:
    def __init__(self):
        pass

    def reset(self):
        pass

    def respond(self, huiye_text, snapshot):
        if not huiye_text or huiye_text == "无":
            return None
        context_parts = []
        context_parts.append(f"现在辉夜在{snapshot['location']}，{snapshot['location_desc']}")
        if snapshot["prev_utterance"]:
            context_parts.append(f"辉夜之前对我说：{snapshot['prev_utterance']}")
        if snapshot["prev_action"]:
            context_parts.append(f"辉夜刚才{snapshot['prev_action']}")
        if snapshot["prev_caiye"]:
            context_parts.append(f"我刚才说：{snapshot['prev_caiye']}")
        context_str = "；".join(context_parts)

        history_block = get_recent(12)
        messages = [{"role": "system", "content": BASE_SYSTEM}]
        user_content = f"""【角色设定】
{WORLD_BACKGROUND}

【最近对话】
{history_block}
【当前场景】
{context_str}

辉夜对我说：{huiye_text}

【指令】你扮演彩叶，自然地回应辉夜。每次只发一两句简练的口语（不超过50字），不要带有角色前缀，不要带有动作和神情的标注，不允许使用括号，也不要带有旁白，直接说内容。只根据以上提供的信息输出，不得添加未给出的内容。"""
        messages.append({"role": "user", "content": user_content})

        reply = call_api_thinking(messages)
        if not reply:
            return None
        reply = reply.strip()
        if len(reply) > 80:
            reply = reply[:80]

        return reply


CAIYE = Caiye()
