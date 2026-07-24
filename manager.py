from core.memory_engine import create_memory, add_link
from utils.persistence import load_all_data, save_all_data
if __name__ == "__main__":
    load_all_data()
    while True:
        user_input = input("[管理员作弊插件] 请输入强制植入的记忆：")
        if user_input == "exit":
            break
        user_mem_id = create_memory(user_input)
        bot_mem_id = create_memory(user_input)
        add_link(user_mem_id, bot_mem_id, 0.9, "causal")
    save_all_data()