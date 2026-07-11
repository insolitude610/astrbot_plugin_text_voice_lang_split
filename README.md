# 文本语音语言分离 (Text-Voice Lang Split)

让 Bot 展示中文文本的同时，用其他语言（如日语）合成语音。支持任意 TTS 提供商，兼容流式/非流式模式，跨平台通用。

## 特性

- 文本与语音使用不同语言，如显示中文 + 朗读日语
- 翻译时自动插入情绪标签（`[嬉しい]`、`[悲しい]` 等），配合 FishAudio 等 TTS 实现情感语音
- 支持任意 TTS 提供商（Edge TTS、OpenAI TTS、Azure TTS、FishAudio 等）
- 兼容流式和非流式输出
- 跨平台通用（Windows / macOS / Linux）
- WebUI 可视化配置
- 翻译/TTS 失败自动回退，翻译超时保护，不中断正常对话

## 安装

**方式一：WebUI 插件市场**

在 AstrBot WebUI 的插件管理页面搜索 `text_voice_lang_split` 一键安装。

**方式二：手动安装**

```bash
cd AstrBot/data/plugins
git clone https://github.com/insolitude610/astrbot_plugin_text_voice_lang_split
```

然后重启 AstrBot 或在 WebUI 中重载插件。

## 配置

在 WebUI 插件配置页面中设置：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `voice_language` | `日语` | 语音目标语言，支持模型能处理的任意语言 |
| `translate_instructions` | (空) | 自定义翻译指令，如"使用可爱的语气翻译"。填写后会追加到默认 prompt 后 |
| `translate_provider` | (空) | 翻译用 LLM Provider，留空则复用当前聊天的 Provider |
| `translate_timeout` | `30` | 翻译请求超时(秒)，设为 0 关闭超时 |
| `tts_max_chars` | `0` | TTS 最大字符数限制，译文超长则跳过语音。设为 0 不限制 |
| `streaming_follow_up_delay` | `1.5` | 流式模式下语音跟进的延迟(秒) |

## 要求

- AstrBot >= 4.5.7
- 已配置 LLM Provider（用于翻译）
- 已配置 TTS Provider（用于合成语音）

## 原理

**非流式模式：** 在消息装饰阶段（`on_decorating_result`）拦截 LLM 的文本结果 → 调用 LLM 翻译（同时自动插入情绪标签如 `[嬉しい]`，支持 FishAudio 等 TTS 的情感语音）→ 调用 TTS Provider 生成语音 → 重组消息链为 `[原文文本] + [目标语言语音]`，阻止内置 TTS 重复触发。

**流式模式：** 捕获 LLM 完整响应文本（`on_llm_response`）→ 文本正常流式发送 → 发送完成后翻译 + TTS 生成语音跟进（`after_message_sent`）。

## 许可证

MIT License
