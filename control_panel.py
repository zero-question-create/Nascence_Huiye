# control_panel.py
# ========================================================================
# 【已迁移】原 PyQt5 桌面控制面板已被 Web 控制面板（webui.py）取代。
#
# 本文件保留为兼容入口：运行 `python control_panel.py` 将直接启动
# Web 控制面板（http://127.0.0.1:8787），避免旧启动脚本失效。
#
# 新版 WebUI 提供与原面板一致的能力：
#   - 总览 / QQ 服务 / 对话测试 / 日志 / 配置 / 维护
#   - 新增「模型」页：三个后端（文本 / 语义向量 / 多模态图片）的
#     本地默认模型开关与外部 API 配置
# ========================================================================

import subprocess
import sys
from pathlib import Path


def main():
    project_dir = Path(__file__).resolve().parent
    py = project_dir / "venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = sys.executable
    print("控制面板已迁移为 WebUI，正在启动 http://127.0.0.1:8787 ...")
    code = subprocess.call([str(py), str(project_dir / "webui.py")] + sys.argv[1:])
    sys.exit(code)


if __name__ == "__main__":
    main()
