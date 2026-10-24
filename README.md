<div align="center">

<img src="https://raw.githubusercontent.com/insolitude610/astrbot_plugin_text_voice_lang_split/main/assets/astrbot_mute.gif" width="150" alt="astrbot娘静音">

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
- **可选注入人格与最近对话上下文**：翻译时可带上人格和/或最近 5 轮对话作为只读参考，让译文更贴人物语气与情绪；默认关闭，保持最快路径
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
| `emotion_intensity` | `universal_safe` | 情绪/表演标记策略，**面板里是下拉选择**。⚠️ 只有「无标记」对任意 TTS 供应商都安全，其余档位都会向译文写入标记，**只有确认你的 TTS 能解释这些标记时才可选**。<br>「无标记」`universal_safe`（默认）：纯文本，任意供应商<br>「Drama 3 导演」`drama3`：自由导演 + 音效 + 成对标签（需 `drama-3-preview`）<br>「标准情绪标签」`auto`：情绪句加一个 `[标签]`（Fish Audio S2 系）<br>「强化情绪标签」`expressive`：情绪句必加标签（Fish Audio S2 系）<br>「保守情绪标签」`restrained`：通常不加标签（Fish Audio S2 系）<br>格式错误或不在列表中的值按「无标记」处理 |
| `translate_provider` | (空) | 翻译用 LLM Provider，留空则复用当前聊天的 Provider |
| `translate_timeout` | `30` | 翻译请求超时(秒)，超时后自动重试一次（间隔 0.5s），设为 0 关闭超时 |
| `translate_context_mode` | `off` | 翻译参考注入模式，**面板里是下拉选择**。`off` 关闭（默认，最快）；`persona` 仅注入人格；`persona_context` 人格 + 最近 5 轮上下文（最贴情绪，token/延迟最高）。格式错误按「关闭」处理 |
| `translate_context_rounds` | `5` | `persona_context` 模式下注入的对话轮数（1 条用户消息及其回复算 1 轮，最多 20） |
| `translate_context_max_chars` | `2000` | 注入上下文的字符上限，超出先丢最旧内容，最后兜底截尾；0 表示不限制 |
| `tts_max_chars` | `0` | 语音朗读的是**本回合用户实际收到的**可朗读文本，超过此值则跳过语音并记 WARNING（多步工具回合会累加）。0 表示不限制 |
| `remove_patterns` | (见默认值) | TTS 文本过滤正则规则。匹配到的内容在翻译前删除，用于过滤颜文字、装饰符号等视觉噪声 |
| `streaming_follow_up_delay` | `1.5` | 语音跟进的延迟(秒)：流式发送完毕后、以及工具模式下最后一条文字发出后，等待此时间再发语音 |
| `enable_llm_voice_tool` | `false` | LLM 自主判断是否发送语音。开启后注册 `tvls_send_voice` 工具；语音在**本回合最后一条文字真正发出后**作为跟进消息发送 |
| `hold_text_for_voice_timeout` | `20` | 工具模式下，**先压住本回合的文字等语音就绪**，再「先文字、后语音」连着发（把 8.6–12.2s 的文字→语音间隔压到 1.5–3.3s）。本机实测音频就绪 4.97–6.12s，默认 20 ≈ 3 倍余量。**命中超时比不压住更差**：文字被压到超时才发，且回合末会**再生成一次**翻译+TTS（多一次调用）。0 = 关闭（文字立即发）。代价：被压住的那条不经过排在其后的装饰插件与 t2i/`use_markdown_`；它们的 `after_message_sent` 钩子仍会被补发 |

**Provider 优先级：** `translate_provider`（手动指定）> 聊天 `/provider` 切换 > 默认 Provider。

**失败回退：** 翻译超时或报错 → 不发语音；过滤后文本超 `tts_max_chars` → 不发语音；过滤后无内容 → 不发语音。三种情况都会在日志里留下记录（超限与工具请求未送达是 `WARNING`），不再静默。

### 语音契约与消息条数

工具模式（`enable_llm_voice_tool=true`）下的行为，请按这份契约理解：

1. **工具不承诺送达。** `tvls_send_voice` 只记录"本轮想要语音"；它运行在回复产生**之前**，无法预知最终长度。工具只在两种确定的情况下拒绝：语音环境不可用（会话开关关闭 / 无 TTS Provider / 全局 TTS 关闭 / 会话 TTS 被关），或**已经送达**的文本已超上限。其余情况返回"请求已记录"。
2. **只有一个语音来源。** 装饰阶段只发文字，并设 `GENERAL_RESULT` 阻断内置 TTS，所以同一句话不会被读两遍。语音在回合最后一条文字真正投递后（`after_message_sent`）才生成，朗读的是**本回合累计投递的文本**（多步工具回合会把前后几条文字合成一次朗读，cap 也按累计计），超限则跳过并记 `WARNING`。服务的前提是**确实有装饰投递**（`_tvls_pipeline_delivered` 非空）：没有投递（含最终响应为空、文字还在核心缓冲区里）时请求**保持待发**并记一条 `WARNING`，等下一次投递再服务；`send_message_to_user` 发出去的内容不算回复，不会被朗读。若工具返回后模型重复了同一句，且语音可以正常生成，重复的那条会被抑制，语音改从已投递的那条立即服务（仍然只读一遍）。
3. **只有完全重复的 post-tool 文本会被抑制。** 默认配置（`buffer_intermediate_messages=false`）下，AstrBot 会把工具调用前的回复和工具后的消息投递成两条独立消息。工具模式 + 已请求语音时，若第二条是**纯文本**、且与最后一条已投递文本**完全相同**、并且语音环境与长度 gate 都允许，插件会清空第二条（不发），由已投递文本服务语音；其它情况（不同文本、带图片/媒体、语音不可用、不是最终步骤的重复、`reply_prefix` 非空、转发/分段、`send_message_to_user` 代发导致首条被跳过等）保持原有行为。历史里仍会保存模型重复的 assistant 记录，插件不改历史。被抑制的消息不会再触发 `after_message_sent`，因此其它插件在该钩子里为这条重复消息排队的内容（以及它们的临时文件清理）也不会执行。已知边界：AstrBot 的首条发送异常仍会触发 `after_message_sent`，此时记录里算已投递，抑制可能生效（插件拿不到真实的送达信号）。工具未开启或未请求语音时完全不介入。**合并模式（下一条）下同一件事换成另一种形态**：模型的两次重复会被并进同一条消息，而且 `meme_manager` 之类先于本插件运行的装饰插件会把相邻纯文本段规范化成一段，所以插件改为按**文本**折叠——只折叠「完整一句话原样重复一遍」（两个半段逐字符相同、每半段 ≥8 字、接缝前那个字符是 `。？！?!~…`），「哈哈哈…」「好的好的」这类不会被误伤；折叠同样要求工具模式 + 已请求语音 + 语音可生成，否则原样保留并记 `INFO`。历史里仍保存模型重复的 assistant 记录，插件不改历史。
4. **想要「一回合一条文字 + 至多一条语音」= 打开核心的中间消息合并。** 在**当前生效的配置档**里同时设置三个键（缺一无效）：
   - `provider_settings.buffer_intermediate_messages: true`（合并 Agent 中间消息）
   - `provider_settings.streaming_response: false`（合并只在**非流式**生效；同时避免在没有流式实现的适配器上走 `STREAMING_RESULT`）
   - `provider_settings.unsupported_streaming_strategy: "realtime_segmenting"`（若仍是 `turn_off`，加上 aiocqhttp 这类 `support_streaming_message=False` 的平台会让 `stream_to_general=True`，**合并直接不生效**）

   改文件后**必须重启** AstrBot（`_load_all_configs` 只在启动时执行；WebUI 保存才会热生效，但流式关闭时那个策略下拉被 schema 的 `condition` 隐藏，面板上点不出来）。生效范围是所有走该配置档、且适配器没有自行塞 `enable_streaming` extra 的平台（`webchat` 会强制流式，因此不受影响、也不会合并）。副作用：该档下**非语音回合**的文字改为回合结束一次性发出；**语音回合**若开了 `hold_text_for_voice_timeout`，则由本插件在装饰阶段末尾等音频就绪后自行发出（不再由管线发）。合并后的多段文本首尾相接，插件不加分隔符，也不会去重（v1.13.1 的抑制只处理跨消息重复；工具模式下的同句重复由 v1.13.2 的文本折叠处理）。
5. **已知排除项**（插件看不到或不会朗读的文本）：`tool_direct_result` 直发的消息、平台侧丢弃的消息、`platform_settings.forward_threshold` 过长触发的**合并转发**（链里没有纯文本）、以及开启「输出工具调用信息」时核心直发的工具状态消息。
6. **内联路径（工具关闭）** 仍在装饰阶段取文本，读到的是其它插件清洗**之前**的内容。若你同时使用思考标签清理类插件，建议改用工具模式，或把对应正则补进 `remove_patterns`。

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

### 把人格与上下文注入翻译（可选，v1.12.0）

纯直译只看到当前这一句中文，拿不到角色口吻和上一轮的情绪。打开 `translate_context_mode` 后，插件会在翻译提示词里附上**只读参考块**，让译文沿用当前的称谓、敬语等级、自称、口癖和情绪强度：

| 档位 | 注入内容 | 适用场景 |
|------|----------|----------|
| `off`（默认） | 不注入 | 追求最快、最省 token，或不想把聊天内容发给翻译 Provider |
| `persona` | 当前人格提示词 | 只想让人物的语气更稳定 |
| `persona_context` | 当前人格 + 最近 5 轮对话 | 长对话、情绪起伏大，希望译文承接上一轮的语境 |

- **「5 轮」的定义**：最近 5 条用户消息以及它们对应的回复（1 条用户消息 + 它的回复 = 1 轮）。历史在 **LLM 请求发出前** 快照，不包含本轮正在翻译的回复；人格开场白（`begin_dialogs`）会被排除，不会被当成真实聊天记录。
- **预算**：`translate_context_max_chars`（默认 2000，非零值最小按 200 生效）先丢最旧内容，仍超限则保留尾部，避免把 `translate_timeout` 打爆。上下文很长或翻译经常超时，可调小轮数/预算，或调大 `translate_timeout`。
- **隐私**：人格与聊天内容会随翻译请求发给你配置的翻译 Provider。若 `translate_provider` 与聊天 Provider 不是同一个，请确认你可以接受这些内容到达该服务商。
- **适用范围**：只有本地 Agent Runner 才有本地对话历史，也才会注入参考内容；dify / coze / dashscope 这类第三方 runner 不提供本地上下文与 conversation，此时**不会注入任何参考**（按纯翻译处理）。人格注入还需要 AstrBot 支持 `PersonaManager.resolve_selected_persona`：不支持的旧版本会打印一次警告，此时 `persona_context` 只注入上下文，`persona` 模式不注入任何内容。
- **防泄漏只是提示词约束**：参考块明确要求模型「不得翻译/引用/回答/续写参考内容」，并在 `Source text:` 之前再重复一次；但这是提示词层面的约束，无法离线自动验证，建议先观察几条回复。WebChat 的特殊默认人格本身带有「主动追问/总结」这类行为指令，插件的规则会把它标为仅作参考；若在 WebChat 里发现译文多出追问，建议关闭本插件或改用显式人格（另见上文不建议 Live Chat 的说明）。
- **默认零变化**：`off` 时翻译提示词与 system prompt 与旧版本**逐字节一致**，行为、速度、token 都不变。

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

**非流式模式（工具关闭）：** 在消息装饰阶段（`on_decorating_result`）拦截 LLM 的文本结果 → 过滤颜文字/代码块/URL 等视觉噪声 → 调用 LLM 翻译清洁文本（按 `emotion_intensity` 档位决定是否写入情绪/表演标记）→ 剥离翻译输出中的思考/推理产物 → 调用 TTS Provider 生成语音 → 重组消息链为 `[原文文本] + [目标语言语音]`，阻止内置 TTS 重复触发。

**流式模式：** 在 `on_llm_response` 捕获 LLM 完整响应文本，并用 async generator wrapper 注入流式发送 → 流式文本全部发送完毕后自动翻译 + TTS 生成语音跟进。不依赖 `after_message_sent`（AstrBot 的 RespondStage 对 `STREAMING_RESULT` 提前 return，该钩子不会触发）。文本流本身失败时不会朗读（用户没收到那段文字），并记 `WARNING`。

**LLM 自主判断语音（工具模式）：** 开启 `enable_llm_voice_tool` 后注册 `tvls_send_voice`。装饰阶段**只发文字**并阻断内置 TTS（同一句话不会被读两遍）；工具只记录请求；`on_llm_response` 标记"最终响应已产生"，随后那条消息的 `after_message_sent` 里按**已实际投递的整回合文本**做闸门判定并生成语音跟进。LLM 未调用工具则整轮不发语音。详见上文「语音契约与消息条数」。

## 作者的话

本来我是想先找找看有没有现成的轮子来符合我的需求，所以就先让gemini老师帮我去搜搜找找，我的需求就是很简单的：bot接了我自己搞好的fish audio提供商，但是模型说的是日语，强行说中文很怪，所以我需要一个让bot既能发中文又能说日语的TTS插件。同时最好可以配置超过某个字符数量之后bot就不发语音了，不然太长太卡了。于是gemini老师给我找到了一个科尔的tts_sanitizer（ https://github.com/Luna-channel/astrbot_plugin_tts_sanitizer ）还有基于这个插件的fork版（ https://github.com/chenluQwQ/astrbot_plugin_tts_sanitizer_bilingual ）。这两个一个是专门TTS过滤的，还有一个加上了我要的双语翻译。我都下载了两位的插件使用，受益匪浅！非常感谢两位大佬！虽然我用下来还行但我还是希望可以更简单一点，所以这才搞了一个比较轻量化的更能直接满足我需求的插件。


## 许可证

MIT License
