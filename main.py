# main.py
import os
import sys
import logging
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
RUN_DIR = PROJECT_DIR / "run"
LOG_DIR = RUN_DIR / "logs"
for d in [LOG_DIR, PROJECT_DIR / "data" / "test", PROJECT_DIR / "data" / "models"]:
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_DIR / "cli.log"), encoding="utf-8"),
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

from core.memory_engine import memories
from core.memory_engine import get_model
from utils.persistence import load_all_data, save_all_data, load_state, save_state, append_dialogue
from core.cognition import generate_response
from core.cognition import UNDO_FILE
from core.virtual_clock import clock
from utils.monitor import monitor_start

# 批量注入函数
def cold_start_batch_injection():
    """批量注入冷启动记忆，用于初始化记忆库"""
    try:
        from data.cold_start_prompts import COLD_START_DIALOGS
    except (ImportError, ModuleNotFoundError):
        print("[冷启动] 未找到 data/cold_start_prompts.py，跳过冷启动")
        return
    from core.memory_engine import create_memory, add_link
    from utils.dialogue_state import set_state, get_state
    
    for user_text, bot_text in COLD_START_DIALOGS:
        user_mem_id = create_memory(user_text, half_life=7*24*3600)
        bot_mem_id = create_memory(f"我说：{bot_text}", half_life=7*24*3600)
        add_link(user_mem_id, bot_mem_id, 0.8, "causal")

    state = get_state()
    state["参与者"] = ["辉夜", "群友们"]
    state["我的已知信息"] = [
        "我的名字是辉夜",
        "我是群里的成员，和大家都认识",
        "大家是我的朋友，我们关系很好"
    ]
    set_state(state)
    print(f"[冷启动] 已注入 {len(COLD_START_DIALOGS)} 条基础记忆")

def main():
    monitor_start()
    get_model()
    print(f"当前时间倍速：{clock.set_speed(144)}")
    load_all_data()
    load_state()
    global memories
    if not memories:
        cold_start_batch_injection()
        save_all_data()
        save_state()
    print("Nascence辉夜 v0.5.3")
    print("输入 'exit' 退出")
    
    try:
        while True:
            clock.update_state()
            user_input = input("你: ").strip()
            if not user_input:
                continue
            if user_input.lower() == "exit":            # 退出操作
                break
            elif user_input.lower() == "speed":         # 调整时间倍速
                speed = int(input("输入调整后的倍速："))
                if speed > 0:
                    print(f"当前时间倍速：{clock.set_speed(speed)}")
                else:
                    print("调整失败")
                continue
            elif user_input.lower() == "undo":          # 撤销操作
                import json
                import os
                if os.path.exists(UNDO_FILE):
                    with open(UNDO_FILE, "r", encoding="utf-8") as f:
                        snap = json.load(f)
                    # 覆盖内存中的记忆图
                    from core.memory_engine import memories, links
                    from utils.dialogue_state import set_state
                    memories.clear()
                    memories.update(snap["memories"])
                    links.clear()
                    for key_str, val in snap["links"].items():
                        s, t = key_str.split("||")
                        links[(s, t)] = val
                    set_state(snap["state"])
                    # 同步写入主持久化文件
                    from core.memory_engine import _rebuild_faiss_index
                    _rebuild_faiss_index()
                    save_all_data()
                    save_state()
                    print("[系统] 已撤销上一轮对话")
                else:
                    print("[系统] 没有可撤销的快照")
                continue
            elif user_input.lower() == "move":
                from utils.persistence import migrate_json_to_sqlite
                migrate_json_to_sqlite()
            clock.on_user_input()
            reply, user_input = generate_response(user_input)
            append_dialogue(user_input, reply)
            print(f"辉夜: {reply}")
    except (EOFError, KeyboardInterrupt):
        print("发生错误！")
    finally:
        save_all_data()
        save_state()
        print("再见。")

if __name__ == "__main__":
    main()