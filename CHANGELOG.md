# Changelog

## v1.2.0

- `tts_max_chars` 改为检查**原文**长度而非译文长度，避免翻译后才截断造成的 LLM 浪费
- 新增 `remove_patterns` 配置项：TTS 合成前使用正则过滤颜文字、emoji、代码块、URL、Markdown 格式等不适合朗读的视觉噪声，翻译 LLM 只看到清洁文本
- 翻译超时/失败不再回退原文 TTS，直接静默发送纯文本，与配置描述一致
- 更新 AGENTS.md 文档与代码行为同步

## v1.1.1

- 修复翻译超时/失败后中文原文 TTS 兜底不生效的问题（改为显式调用 TTS 而非依赖内置管道）

## v1.1.0

- 翻译时自动插入情绪标签（`[嬉しい]`、`[悲しい]` 等），配合 FishAudio 等 TTS 实现情感语音
- 新增 `tts_max_chars` 配置项，译文超长时自动跳过 TTS，适合长回复场景
- 新增 `translate_timeout` 配置项，翻译 LLM 请求超时自动回退，避免阻塞管线
- 新增 `translate_provider` 配置项，支持为翻译指定独立 LLM Provider
- 修复翻译 Provider 未跟随聊天 `/provider` 切换的问题（现在优先读取 event selected_provider）
- 修复翻译失败/超长/TTS 失败等提前 return 路径未阻断内置 TTS 的 bug
- 翻译失败时回退为中文原文 TTS 兜底，不再静默

## v1.0.0

- 初次发布
- 文本与语音使用不同语言，如显示中文 + 朗读日语
- 支持任意 TTS 提供商
- 兼容流式与非流式输出模式
- 翻译/TTS 失败自动回退，不中断正常对话
- WebUI 可视化配置
