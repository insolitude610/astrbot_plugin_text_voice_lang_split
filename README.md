# 文本语音语言分离 (Text-Voice Lang Split)

让 Bot 展示中文文本的同时，用其他语言（如日语）合成语音。支持任意 TTS 提供商，兼容流式/非流式模式，跨平台通用。

## 特性

- 文本与语音使用不同语言，如显示中文 + 朗读日语
- 翻译时自动插入情绪标签（`[嬉しい]`、`[悲しい]` 等），配合 FishAudio 等 TTS 实现情感语音
- 支持任意 TTS 提供商（Edge TTS、OpenAI TTS、Azure TTS、FishAudio 等）
- 兼容流式和非流式输出
- 跨平台通用（Windows / macOS / Linux）
- WebUI 可视化配置
- TTS 前自动过滤不适合朗读的内容（颜文字、代码块、URL、Markdown 格式等），仅剥离视觉噪声，不做机械替换
- 翻译失败/超时/原文过长自动静默，不中断正常对话
- 自动检测 Agent Live 模式并跳过插件 TTS，避免与内置分句 TTS 冲突导致 429

> **注意：** 不建议在 WebChat 的 Live Chat 界面使用本插件。Live Chat 会触发 AstrBot 的 Agent 分句 TTS，两者同时发送 TTS 请求可能导致 API 速率限制（429）。插件已对这种情况做了自动跳过处理，但最佳实践是在普通聊天场景（QQ、微信等平台）中使用本插件。

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
| `tts_max_chars` | `0` | 过滤后的文本超过此字符数时跳过翻译和 TTS。设为 0 不限制 |
| `remove_patterns` | (见默认值) | TTS 文本过滤正则规则。匹配到的内容在翻译前删除，用于过滤颜文字、装饰符号等视觉噪声 |
| `streaming_follow_up_delay` | `1.5` | 流式模式下语音跟进的延迟(秒) |

**Provider 优先级：** `translate_provider`（手动指定）> 聊天 `/provider` 切换 > 默认 Provider。

**失败回退：** 翻译超时或报错 → 静默不发语音；过滤后文本超 `tts_max_chars` → 静默不发语音；过滤后无内容 → 静默不发语音。

## 要求

- AstrBot >= 4.5.7
- 已配置 LLM Provider（用于翻译）
- 已配置 TTS Provider（用于合成语音）

## 原理

**非流式模式：** 在消息装饰阶段（`on_decorating_result`）拦截 LLM 的文本结果 → 过滤颜文字/代码块/URL 等视觉噪声 → 调用 LLM 翻译清洁文本（同时自动插入情绪标签如 `[嬉しい]`，支持 FishAudio 等 TTS 的情感语音）→ 调用 TTS Provider 生成语音 → 重组消息链为 `[原文文本] + [目标语言语音]`，阻止内置 TTS 重复触发。

**流式模式：** 捕获 LLM 完整响应文本（`on_llm_response`）→ 文本正常流式发送 → 发送完成后翻译 + TTS 生成语音跟进（`after_message_sent`）。

# 作者的话

本来我是想先找找看有没有现成的轮子来符合我的需求，所以就先让gemini老师帮我去搜搜找找，我的需求就是很简单的：bot接了我自己搞好的fish audio提供商，但是模型说的是日语，强行说中文很怪，所以我需要一个让bot既能发中文又能说日语的TTS插件。同时最好可以配置超过某个字符数量之后bot就不发语音了，不然太长太卡了。于是gemini老师给我找到了一个科尔的tts_sanitizer（ https://github.com/Luna-channel/astrbot_plugin_tts_sanitizer ）还有基于这个插件的fork翻译版（ https://github.com/chenluQwQ/astrbot_plugin_tts_sanitizer_bilingual ）。这两个一个是专门TTS过滤的，还有一个加上了我要的双语翻译。我都下载了两位的插件使用，受益匪浅！非常感谢两位大佬！虽然我用下来还行但我还是希望可以更简单一点，所以这才搞了一个比较轻量化的更能直接满足我需求的插件。


## 许可证

MIT License
