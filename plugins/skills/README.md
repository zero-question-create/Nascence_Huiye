# 技能插件

把 `core/skill_registry.py` 中已启用的技能动作暴露给辉夜的动作层。

- 技能默认关闭，只有启用后才会出现在动作提示词中。
- 只读技能启用即授权；写入/执行/安装类权限必须在控制面板确认。
- 技能设置只存在面板，不会进入日常认知提示词。

内置技能：

- `screenshot`：截屏并交给视觉模型分析。
- `bilibili_video`：B站视频信息/字幕/低清关键帧分析。
- `read_media`：读取照片/表情包的完整 LLM 描述与 OCR。
- `plugin_forge`：调用 ZCode/DeepSeek harness 生成插件包。
