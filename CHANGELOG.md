# Changelog

## v1.4.1

- 修复 TTS 朗读情绪标签文本的问题：翻译 prompt 改用 FishAudio 官方支持的英文情绪词（24 个封闭列表），替换之前因语言而异的 `[嬉しい]` 等日语标签。FishAudio S2 模型无法映射非英文情绪词为语音控制，会当作文本朗读出来；统一用英文情绪词 `[happy]`、`[sad]` 等确保正确识别

## v1.4.0

- 修复流式模式下语音不发送的问题：RespondStage 对 `STREAMING_RESULT` 在 `send_streaming` 后直接 `return`，不触发 `after_message_sent`。改为在 `on_llm_response` 中用 async generator wrapper 注入流式跟进逻辑，在流式文本全部发送完毕后自动翻译+TTS+发送语音，不依赖 `after_message_sent`
- 新增 `_send_streaming_follow_up` 方法，抽取原 `after_message_sent` 的流式翻译+TTS 逻辑，被 async_stream wrapper 复用
- 精简 `after_message_sent` 为仅清理 `_streaming_texts`，消除死代码
- 修复 URL 正则会吃掉中文：`https?://\S+` 改为 `https?://[a-zA-Z0-9./?#&=\-+%:!*'();,@[\]~_$]+`，只匹配 ASCII URL 字符，中文标点和汉字不再被误吞
- 新增会话级 TTS 开关检查：调用 `SessionServiceManager.should_process_tts_request()`，与 AstrBot 内置 TTS 保持一致，避免用户关闭会话 TTS 后仍产生语音
- 修正 `astrbot_version` 兼容声明：`>=4.5.7` → `>=4.22.0`（`track_temporary_local_file` 在 v4.22.0 引入）

## v1.3.1

- 强化翻译 prompt：明确要求每个方括号内只能有一个情绪标签（如 `[嬉しい]`），禁止多情绪挤一个括号（如 `[嬉しい 悲しい]`）。多情绪时只选最主要的一个，确保 FishAudio 等 TTS 的情绪控制能正确识别

## v1.3.0

- 防御翻译 LLM 输出思考/推理内容混入 TTS 的问题：在翻译请求中加入 `system_prompt` 明确禁止输出推理过程，同时新增 `_strip_thinking` 后处理剥离 `<think>...</think>` 块和 `<｜end▁of▁thinking｜>` 标记等常见思考产物。针对推理模型（如 DeepSeek-R1、Gemini 等）在翻译时可能输出内心独白、且 API 代理可能丢失原生 `thought` 标志的场景提供兜底
- `_strip_thinking` 剥离后若文本为空则视为翻译失败，走静默回退路径

## v1.2.2

- 修复翻译 LLM 频繁超时问题：超时后增加 0.5s 延迟重试一次，给 httpx 连接池时间清理被 `asyncio.wait_for` 强制取消后残留的半死连接，避免后续请求复用脏连接导致的连锁超时
- `translate_timeout` 默认值保持 30s，推荐翻译专用 provider 配置 `timeout: 15` 以利用 httpx 原生超时取代 asyncio 层取消

## v1.2.1

- 修复 Agent Live 模式下插件 TTS 与内置分句 TTS 同时触发导致 429 的问题（自动检测并跳过）
- 修复 `tts_max_chars` 在文本过滤**前**检查长度的问题：含大量代码块/Markdown 的回复被错误跳过，现改为过滤后检查
- 修复 Markdown 链接 `[text](http://...)` 过滤顺序 bug：URL 先被步骤 3 移除导致步骤 4 无法展开链接，残留 `[text]()` 送入翻译
- 新增 `get_audio()` 返回空路径检查，避免创建无效 Record 导致下游报错
- 更新 README 添加 WebChat Live Chat 兼容性说明

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
