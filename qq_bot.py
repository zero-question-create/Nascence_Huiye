# qq_bot.py
# ========================================================================
# 兼容层：QQ 接入实现已迁入 plugins/qq_napcat/runtime.py。
#
# 保留本文件是为了让既有入口继续可用：
#   - `python qq_bot.py`
#   - `import qq_bot`（控制面板、测试）
#   - `from qq_bot import send_group_msg, get_bot_qq, ...`
#
# 导入时直接把 sys.modules 中的 qq_bot 指向插件运行时，因此模块级状态
# （_napcat_websocket、_shutdown_event 等）只有一份，不会出现两份副本。
# ========================================================================

import sys

from plugins.qq_napcat import runtime as _runtime

if __name__ == "__main__":
    _runtime.main()
else:
    sys.modules[__name__] = _runtime
