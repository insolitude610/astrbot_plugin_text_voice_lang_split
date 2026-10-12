<div align="center">

<img src="assets/astrbot_mute.gif" width="150" alt="astrbot娘静音">

# 文本语音语言分离 (Text-Voice Lang Split)

让 Bot 展示中文文本的同时，用其他语言（如日语）合成语音。支持任意 TTS 提供商，兼容流式/非流式模式，跨平台通用。

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.22.0-orange?style=flat-square)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)

</div>

---

## 特性

- 文本与语音使用不同语言，如显示中文 + 朗读日语
- **默认不写入任何标记**：译文是纯文本，任何 TTS 供应商都可以安全朗读
- 可选开启情绪标签（Fish Audio S2 系）或 Drama 3 的表演导演能力，按 TTS 供应商选择
- 支持任意 TTS 提供商（Edge TTS、OpenAI TTS、Azure TTS、FishAudio 等）
- 兼容流式和非流式输出
- 跨平台通用（Windows / macOS / Linux）
- WebUI 可视化配置
- TTS 前自动过滤不适合朗读的内容（颜文字、代码块、URL、Markdown 格式等），仅剥离视觉噪声，不做机械替换
- 自动剥离翻译 LLM 输出的思考/推理内容（`<think>...</think>` 块、` response 标记等），防止推理模型将内心独白混入语音
- 翻译失败/超时/原文过长自动静默，不中断正常对话
- 自动检测 Agent Live 模式并跳过插件 TTS，避免与内置分句 TTS 冲突导致 429

> **注意：** 不建议在 WebChat 的 Live Chat 界面使用本插件。Live Chat 会触发 AstrBot 的 Agent 分句 TTS，两者同时发送 TTS 请求可能导致 API 速率限制（429）。插件已对这种情况做了自动跳过处理，但最佳实践是在普通聊天场景（QQ、微信等平台）中使用本插件。

> **注意：** 本插件与 AstrBot 的**分段回复**功能互斥，请勿同时开启。原因：插件通过修改消息类型来阻止内置 TTS 重复触发，而分段回复依赖原始消息类型才能拆分文本。两者同时开启会导致分段失效、语音顺序错乱。`平台设置 → 分段回复设置 → 启用分段回复` 请保持关闭。

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

> **重要：** 如果翻译频繁超时，请在 AstrBot WebUI → Provider 设置中，找到你给翻译专用的那个 Provider，将 **`timeout`** 从默认的 `120` 改为 **`15`**。插件本身的超时重试只能缓解问题，根因在于 `asyncio.wait_for` 强制取消 HTTP 请求后 httpx 连接池会残留脏连接。让 httpx 用自己的超时机制断开连接才能彻底避免脏连接产生。**改一个数字即可，零副作用。**

在 WebUI 插件配置页面中设置：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `voice_language` | `日语` | 语音目标语言，支持模型能处理的任意语言 |
| `translate_instructions` | (空) | 自定义翻译指令，如"使用可爱的语气翻译"。填写后会追加到默认 prompt 后 |
| `emotion_intensity` | `universal_safe` | 情绪/表演标记策略。⚠️ 除 `universal_safe` 外，其余档位都会向译文写入标记，**只有确认你的 TTS 供应商能把标记解释成表演（而不是照字念出来）时才可选**。<br>`universal_safe`（默认）：完全不生成任何标记，纯文本，任意 TTS 供应商都安全<br>`drama3`：Drama 3 专属，支持自由自然语言导演、音效/停顿标记与成对行内标签（需 `drama-3-preview` 模型）<br>`auto`：有明显情绪的句子加一个标签，中性句不加（Fish Audio S2 系）<br>`expressive`：情绪句必加标签，表现力最强（Fish Audio S2 系）<br>`restrained`：保守档，通常不加标签，仅保留最克制的 12 个标签（Fish Audio S2 系）<br>未知/空值按 `universal_safe` 处理 |
| `translate_provider` | (空) | 翻译用 LLM Provider，留空则复用当前聊天的 Provider |
| `translate_timeout` | `30` | 翻译请求超时(秒)，超时后自动重试一次（间隔 0.5s），设为 0 关闭超时 |
| `tts_max_chars` | `0` | 过滤后的文本超过此字符数时跳过翻译和 TTS。设为 0 不限制 |
| `remove_patterns` | (见默认值) | TTS 文本过滤正则规则。匹配到的内容在翻译前删除，用于过滤颜文字、装饰符号等视觉噪声 |
| `streaming_follow_up_delay` | `1.5` | 流式模式下语音跟进的延迟(秒) |
| `enable_llm_voice_tool` | `false` | LLM 自主判断是否发送语音。开启后注册 `tvls_send_voice` 工具，LLM 自行决定是否朗读当前回复 |

**Provider 优先级：** `translate_provider`（手动指定）> 聊天 `/provider` 切换 > 默认 Provider。

**失败回退：** 翻译超时或报错 → 静默不发语音；过滤后文本超 `tts_max_chars` → 静默不发语音；过滤后无内容 → 静默不发语音。

### 情绪标记档位怎么选

`emotion_intensity` 的档位**不是"强度"递进，而是不同的标记语法体系**，必须和你的 TTS 供应商匹配。选错时模型会把标记照着念出来。

| 档位 | 写入译文的标记 | 适用 TTS |
|------|---------------|----------|
| `universal_safe`（默认） | 无 | 任意供应商，包括 Edge / OpenAI / Azure TTS |
| `drama3` | `[自由描述]`、`[音效]`、`<whisper>…</whisper>` | Fish Audio **Drama 3** |
| `auto` / `expressive` / `restrained` | `[happy]` 等 24 个封闭情绪词 | Fish Audio **S2 系**（`s2-pro` / `s2.1-pro` / `s2.1-pro-free`） |
| — | — | 其他供应商请保持默认 `universal_safe` |

### 使用 Fish Audio Drama 3

Drama 3 是目前 Fish Audio 最强的表演模型，支持用自然语言"导演"每一句的念法。启用步骤：

1. **AstrBot 面板 → 配置 → 服务提供商 → 你的 `fishaudio` 提供商 → 模型 ID**，把值改成 `drama-3-preview`
   - ⚠️ 必须精确写成这个字符串。AstrBot 把它原样放进 HTTP `model` 头，**写错或留空时 Fish Audio 不会报错，而是静默回落到 `s2.1-pro`**
   - 面板里看到的中文名（如「Drama 3」）不是合法的模型 ID，填了等于没启用
2. **插件配置 → `emotion_intensity` 改为 `drama3`**
3. 验证：发一句带 `[small laugh]` 的话，听到笑声说明 Drama 3 生效；听到它把 "small laugh" 念出来说明还在 S2 系

`drama3` 档下翻译 LLM 会生成三类标记，全部要求**严格来自原文**，不允许自行加戏：

```text
[almost crying but forcing a smile] 私は大丈夫。本当よ。
[sigh] もう行ってくれ。
<whisper>本当に大丈夫だから。</whisper>
```

**注意：**

- `drama3` 档与 `split_tts_by_sentence` 同时使用时，成对标签（`<whisper>…</whisper>`）所在句不会被拆开，以保证标签闭合；单个标签长于 `tts_split_max_chars` 时按最佳努力切分
- 若 `remove_patterns` 里保留了 `\[.*?\]` 这类规则，方括号导演标记会在翻译前被删除，Drama 3 的导演能力会失效，请按需调整
- Drama 3 官方标注为 Preview，行为可能变化

### 减少翻译超时

翻译请求超时可能由网络波动或 Provider API 响应慢导致。推荐以下措施：

1. **为翻译单独创建一个 Provider**（如便宜的轻量模型），并在其 AstrBot 配置中将 `timeout` 设为 **15**（而非默认 120）。这样 httpx 会用自己的超时机制优雅关闭连接，比 `asyncio.wait_for` 强制取消更干净。
2. 插件默认超时 30s，超时后会自动延迟 0.5s 重试一次。如果两次都失败则回退为纯文本发送。
3. 如果网络环境确实不稳定，可以适当调高 `translate_timeout`（如 60s），但建议优先处理 Provider 侧的超时配置。

## 要求

- AstrBot >= 4.22.0
- 已配置 LLM Provider（用于翻译）
- 已配置 TTS Provider（用于合成语音）

## 原理

**非流式模式：** 在消息装饰阶段（`on_decorating_result`）拦截 LLM 的文本结果 → 过滤颜文字/代码块/URL 等视觉噪声 → 调用 LLM 翻译清洁文本（按 `emotion_intensity` 档位决定是否写入情绪/表演标记）→ 剥离翻译输出中的思考/推理产物 → 调用 TTS Provider 生成语音 → 重组消息链为 `[原文文本] + [目标语言语音]`，阻止内置 TTS 重复触发。

**流式模式：** 在 `on_llm_response` 捕获 LLM 完整响应文本，并用 async generator wrapper 注入流式发送 → 流式文本全部发送完毕后自动翻译 + TTS 生成语音跟进。不依赖 `after_message_sent`（AstrBot 的 RespondStage 对 `STREAMING_RESULT` 提前 return，该钩子不会触发）。

**LLM 自主判断语音：** 开启 `enable_llm_voice_tool` 后，插件注册 `tvls_send_voice` 函数工具。LLM 在生成回复时可以调用该工具来标记"此回复需要语音"。工具本身只设置事件标记，实际的翻译+TTS 仍在 pipeline 后续阶段执行。LLM 未调用工具时则跳过语音，仅发送文字。这允许 LLM 根据对话语境自行判断——闲聊、情感交流时发语音，代码、列表、表格时跳过。

## 作者的话

本来我是想先找找看有没有现成的轮子来符合我的需求，所以就先让gemini老师帮我去搜搜找找，我的需求就是很简单的：bot接了我自己搞好的fish audio提供商，但是模型说的是日语，强行说中文很怪，所以我需要一个让bot既能发中文又能说日语的TTS插件。同时最好可以配置超过某个字符数量之后bot就不发语音了，不然太长太卡了。于是gemini老师给我找到了一个科尔的tts_sanitizer（ https://github.com/Luna-channel/astrbot_plugin_tts_sanitizer ）还有基于这个插件的fork版（ https://github.com/chenluQwQ/astrbot_plugin_tts_sanitizer_bilingual ）。这两个一个是专门TTS过滤的，还有一个加上了我要的双语翻译。我都下载了两位的插件使用，受益匪浅！非常感谢两位大佬！虽然我用下来还行但我还是希望可以更简单一点，所以这才搞了一个比较轻量化的更能直接满足我需求的插件。


## 许可证

MIT License
