# core/constants.py
# 全局常量：角色名等硬编码值统一集中于此，避免散落各处。

# 机器人的名字（取自 api_config.json 的 bot_name，可在「配置」页修改，重启后生效）
from config.api_config import config
BOT_NAME = config.get("bot_name", "辉夜")
