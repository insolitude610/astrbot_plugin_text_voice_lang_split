# Changelog

## v1.13.2

- **修复：开启 `buffer_intermediate_messages` 后，某个守规矩的回合可能一句语音都收不到。** 合并模式下整回合文字在 `agent_runner.done()` 之后才作为**一条**消息投递；若模型遵守工具协议（工具返回后不再输出文字），最终响应文本为空，`on_llm_response` 会立刻服务语音——此刻 `_tvls_pipeline_delivered` 还是空的。旧实现在这条分支里**已经**置了 `_tvls_deferred_voice_sent`，随后那条合并消息的 `after_message_sent` 因标记被跳过，请求被"消费成成功"却从未发出。
- **服务前提收紧为"必须有装饰投递"**：`_maybe_send_turn_voice` 先要求 `_tvls_pipeline_delivered` 非空，才占用唯一一次尝试（`_tvls_deferred_voice_sent`）。没有投递时改为 `logger.warning` 并**保持待发**，由真正携带回复的那次投递补发；占位之后的拒绝（环境不可用 / 超限）仍然保持"消费一次不重试"。`on_llm_request` 的 dangling `WARNING` 仍是跨回合兜底。
- **行为变更：`send_message_to_user` 的文本不再可能被朗读。** `delivered_voice_text()` 的该回退只用于"本回合没有任何装饰投递"的场景，而服务现在要求装饰投递，因此它不再是服务来源（用户没当作回复看到的内容不会被念出来）。取舍：一个"只用 `send_message_to_user` 回答、从无装饰投递"的回合不再产生语音，只留一条 `WARNING`。
- **修复：合并模式下模型"同一句写两遍"会被原样发出。** 模型在 `tvls_send_voice` 返回后又写了一遍同一句（v1.13.1 记录过的老毛病），开启 `buffer_intermediate_messages` 后两份文本被并进同一条消息，跨消息抑制看不到它。更麻烦的是 `meme_manager`（priority 99999，比本插件更早跑）会把相邻 Plain 段规范化成一段，因此只能按文本处理：新增 `_collapse_verbatim_repeat`，只折叠"完整一句话原样重复一遍"（两个半段逐字符相同、半段长度 ≥8、接缝前一个字符是句末/停顿标记 `。？！?!~…`），因此「哈哈哈…」「好的好的」不会被误伤；折叠仍受工具模式 + 已请求语音 + 语音可生成三重门控，不满足就原样保留并记一条 `INFO`。
- **新增：`hold_text_for_voice_timeout`（默认 20 秒）——用等待换连贯。** 语音必须先「翻译 + TTS」才能发出，本机实测**音频就绪 4.97–6.12 秒**（translate 2.27–3.41s + TTS 2.12–2.89s；平台上传另计 1.5–3.3s），所以「文字立刻发、语音十秒后才到」在观感上是断开的。开启后，工具模式的**最后一条**文字会被压住：先把音频生成好，再按「先文字、后语音」连着发——总时长不变，间隔从 8.6–12.2s 降到 **1.5–3.3s**（只剩 QQ 上传）。仅在「本回合只有这一条消息」时生效（`_tvls_pipeline_delivered` 为空 + `_tvls_final_seen` + 已请求语音）。
  - **默认值按实测留余量**：默认 20s ≈ 实测上限的 3 倍——命中超时的后果比不压住**更差**（文字被压到超时才发，且会放弃已完成的翻译、在回合末再生成一遍），还会多一次 LLM + TTS 调用。超时分支的 WARNING 明确写出这一点，`_conf_schema.json` 的 hint 也建议先看 `Voice ready in X.XXs (translate=…, tts=…)` 再按实测上限的 2–3 倍定值。
  - **已知代价（如实记录）**：超时会双份生成（见上）；被压住的消息不经过排在其后的装饰插件（思考标签清理、分段回复）以及装饰阶段末尾的文本转图片/`use_markdown_` 等元数据，但它们的 `after_message_sent` 钩子会被补发；设为 0 可完全关闭。
- **先发文字、成功了才接管**：文字链是**发送成功之后**才从 `result.chain` 里清掉的，并且检查 `context.send_message` 的布尔返回；发送抛异常或返回 `False` 时立刻把链交回管线（退回「管线发文字 + 回合末补语音」），语音临时文件同时释放。因此**任何情况下都不会吞掉回复**——这是终审发现的 P1，已在实现里修掉并有 3 条回归测试（`send_message` 返回 False、抛异常、语音发送失败）。
- **补发 `after_message_sent`**：被压住的那条由插件亲自发送，管线不会为它触发 `OnAfterMessageSentEvent`；不发的话，兄弟插件挂在该钩子上的待办会被静默丢弃（`meme_manager` 的排队表情图与临时文件清理就在那里）。现在由插件在发完这一对之后补发该钩子（`_notify_after_message_sent`，核心 API 不可用时降级为一条 debug 日志）。
- **新增可观测（四行）**：服务前的 `Voicing the turn's delivered text (N chars): '...'`（语音读的是哪条文本、多长）、`Voice ready in X.XXs (translate=…, tts=…, chunks=…)`（延迟花在哪）、`Reply chain at priority 999: Plain:36`（本插件收到的链形状）、`Shipped the held reply text` / `Shipped the turn's text and voice back-to-back`（压住路径的两个发送时刻），以及启动时的 `Message count: buffer=…, streaming=…, strategy=…`（合并有没有生效）。此前这些都不可见——本次定位"同一句两遍"与"12 秒间隔"全靠它们。
- **文档**：README「语音契约与消息条数」重写第 2 条，并新增第 4 条，给出「一回合一条文字 + 一条语音」的完整配方（三个键缺一无效、必须重启、面板点不出来的原因、`webchat` 不受影响、以及"所有回合文字改为回合末一次性发出"的副作用）；`AGENTS.md` 不变量 4/5/6 与「Every refusal is logged」同步改写。
- **测试**：`tests/test_smoke.py` 新增 17 项——合并投递只发一次语音、最终响应为空时保持 pending 并由后续投递补发、拒绝后不重试、`send_message_to_user` 不作为来源、投递链无纯文本时保持 pending、文本内完整句重复折叠、非句子重复不误伤、压住的六种分支（成功/超时/失败/hold=0/非最后消息/已有投递）、**文字发送失败与返回 False 两条"回复不得丢"回归**、语音发送失败 WARNING、补发 `after_message_sent`、超时后的二次生成；`test_turn_voice_empty_turn_logs_warning` 补上"不得消耗请求"的断言。全部 9 个套件通过（**smoke 50/50**、voice_tool 18/18），`ruff format --check` / `ruff check` 干净。

## v1.13.1

- **修复：工具模式同一句回复在工具返回后被模型重复输出时，会连续发两条相同文本。** AstrBot 会把工具循环每一步的文本分别投递；模型在 `tvls_send_voice` 返回后没有按工具约定停止输出，而是又写了一遍同样的回复，于是两条文本先后发出，回合末语音还会把同一句读两遍。
- **只在可确认的重复上抑制**：工具模式 + 已请求语音 + 当前消息只含纯文本 + 与最后一条已投递文本完全相同 + `voice_gate_env` 与回合级 `voice_gate_text` 都允许。命中时清空第二条 `result.chain`（不发送），并从已投递文本立即服务语音；用户看到一条文字 + 一条语音，语音只读一遍。
- **不介入的情况**：不同文本、带图片/媒体、语音不可用或超限、不是最终步骤的重复、`reply_prefix` 非空、转发/分段、`send_message_to_user` 代发导致首条被跳过、工具关闭或未请求语音——保持原有行为。历史中仍会保存模型重复的 assistant 记录（插件不改历史）。已知边界：首条发送异常仍会触发 `after_message_sent`（记录算已投递，抑制可能生效）；被抑制的消息不再触发 `after_message_sent`，所以其它插件在该钩子里为这条重复消息排队的内容（以及它们的临时文件清理）也不会执行。
- **不覆盖合并模式**：`buffer_intermediate_messages=true` 需要 `unsupported_streaming_strategy != "turn_off"`（当前 `turn_off` + aiocqhttp 不会合并）；即使合并，重复文本也留在同一条消息内，本抑制不处理。
- **测试**：`tests/test_smoke.py` 新增 11 项（生产顺序、实际 `Record.text`、媒体/不同文本/未请求/无投递/工具关闭/首条/环境不可用/累计 cap/空白归一）。全部 tvls standalone 测试通过（`tests/` 按仓库惯例不纳入版本控制）。

## v1.13.0

- **修复：工具请求语音后永远收不到（线上 P0）**。`main.py` 里 `event.clear_extra("_tvls_pending_text")` 与实际签名不符——`AstrMessageEvent.clear_extra(self)` **不接受参数**（它清空全部 extras），该调用必然抛 `TypeError`，而它位于 `_tvls_deferred_voice_sent = True` **之后**、任务创建之前，异常又被事件钩子封装吞掉。结果：自 v1.7.0 起，工具触发的"延迟语音"从未真正发出过，且请求被标记为已处理、后续不再重试。测试没抓到是因为 `MockEvent.clear_extra(self, key=None)` 伪造了签名。
- **修复：`tts_max_chars` 判定用了"清洗前"的文本（线上 P0）**。插件装饰阶段 `priority=999` 先于思考标签清理类插件（如 `astrbot_plugin_thinktags`，默认 `priority=0`）运行，量到的是含泄露思考前缀的文本（实测日志：`819 chars > 98`，随后同一毫秒里 671 字符被剥离）。现在语音文本改为在 `after_message_sent` 里从**真正投递出去的消息链**取（其它插件的改写已生效），既不会因思考前缀误判超限，也不会把思考前缀朗读出来。
- **行为变更：工具不再承诺送达。** `tvls_send_voice` 运行在回复产生之前，无法预知长度，因此只做两件确定的事：环境不可用时拒绝、**已送达**文本已超限时拒绝；其余返回"语音请求已记录"（不再返回 `Voice message will be sent.`）。工具描述里把上限写成引导，并要求"先写完回复、最后调用一次、返回后不要再输出文字"。
- **行为变更：工具模式只有一个语音归属点。** 装饰阶段只发文字并一律设 `GENERAL_RESULT`（阻断内置 TTS，同一句话不会被读两遍）；语音在回合最后一条文字真正投递后由 `after_message_sent` 统一生成。移除了旧的 `_tvls_pending_text` / `_maybe_send_deferred_voice` 机制（其触发点在投递之前，会朗读前缀）。
- **`tts_max_chars` 语义澄清**：按**本回合已送达的可朗读文本**累计计（多步工具回合会累加），超限跳过并在日志记 `WARNING`（不再静默）。`_conf_schema.json` 的 hint 同时列出"已送达"的排除项（`tool_direct_result`、平台丢弃、`forward_threshold` 转发包装、核心直发的工具状态消息），以及内联路径（工具关闭）仍读清洗前文本的已知限制。
- **新增：一次请求最多一次尝试。** `_tvls_deferred_voice_sent` 在生成前占位；流式 wrapper 在**发送成功之后**才置位，分片失败/取消会告警并保留未服务状态；`on_llm_request` 重置回合状态，若上一回合留有未送达的请求会记 `WARNING`。
- **新增：`delivered_voice_text()` 单一权威来源**。优先用已投递的装饰文本，仅当该回合没有装饰投递时才回退到 `send_message_to_user` 记录；**不再把两者拼接**（否则会把用户分两条收到的内容当成一条朗读）。
- **新增：统一语音闸门** `voice_utils.voice_gate_text` / `voice_gate_env`，工具、回合末服务、流式补发、内联路径共用；`tts_max_chars` 解析改为 fail-safe（`"abc"` / 负数 / `None` 一律按"不限制"，不再抛 `TypeError`）。延迟发送路径此前**完全没有**长度检查，现已补齐。
- **测试底座重写**（`tests/test_voice_tool.py`、`tests/test_smoke.py`）：改为导入生产的 `VoiceTool` / `build_description`（不再手抄副本），mock 事件用 `inspect.signature` 锁真实签名，`get_audio` 改 `AsyncMock` 返回真实临时文件，"发过语音"改用 `context.send_message` 调用次数断言，runner 以非零退出码报告失败。新增用例：工具模式不内联、回合末才服务并朗读整回合、超限跳过有 WARNING、空回合有 WARNING、悬挂请求有 WARNING、跨源不拼接、流式失败不朗读、源码里不得出现带参数的 `clear_extra(`。
- **文档**：README 新增「语音契约与消息条数」——说明插件不合并/不删除消息，默认配置下工具调用前的回复与工具后的消息是两条，想要"一回合一条"请开启 AstrBot 的 `provider_settings.buffer_intermediate_messages`（仅非流式）；并说明 `GENERAL_RESULT` 对分段回复/splitter 的影响范围。AGENTS.md 按新机制重写工具模式章节与不变量。
- 已知残余：工具调用前的那条消息仍由核心独立投递，插件无法删除；`tool_direct_result` 直发、平台丢弃、`forward_threshold` 转发包装的文本不在记账范围内；内联路径（工具关闭）仍在装饰阶段取文本。

## v1.12.0

- **新增翻译参考注入（可选）**：`translate_context_mode` 三档下拉——`off`（默认，行为与 v1.11.0 完全一致）、`persona`（只注入人格）、`persona_context`（人格 + 最近 5 轮对话）。让译文沿用当前人格的称谓、敬语、自称、口癖与情绪强度；默认关闭，保持最快路径
  - 新增 `translate_context_rounds`（默认 5，上限 20；1 条用户消息及其回复算 1 轮）与 `translate_context_max_chars`（默认 2000，0 = 不限；超限先丢最旧、最后兜底截尾）
  - **历史在 `on_llm_request` 时快照**：此时 `req.contexts` 是本轮之前的对话、人格已由核心应用，因此「最近 5 轮」不会混入正在翻译的本轮回复；快照写入是加法式的，空快照不会覆盖已有快照
  - **过滤 `_no_save` 人格开场白**：核心会把人格 `begin_dialogs`（`_no_save: True`）prepend 进 `req.contexts`，快照阶段将其排除，避免把剧本对话当成真实历史；同时过滤 system / tool / `_checkpoint` 与非文本内容，窗口 80 条以覆盖工具轮次
  - **人格镜像核心解析**：按核心完全相同的入参（`umo` / `conversation.persona_id` / `platform_name` / `provider_settings`）调用 `PersonaManager.resolve_selected_persona`，并镜像「无 conversation 不注入人格」与 WebChat 特殊默认人格分支；不再使用可能拿到「LLM 未使用的人格」的 `get_default_persona_v3` 兜底。旧版 AstrBot 缺该 API 时**不静默**：打印一次 warning，此时 `persona_context` 只注入上下文、`persona` 模式不注入任何内容；dify / coze / dashscope 等第三方 runner 无 conversation 与本地历史，一律按纯翻译处理
  - `translate_context_max_chars` 非零值最小按 200 生效，避免极小的预算把渲染行截成半句话；小数或非整数配置按非法值回落到默认值
  - **提示词结构**：人格块与上下文块均为 `(REFERENCE ONLY)`，要求模型只用于风格参考、不得翻译/引用/回答/续写、不得覆盖 TTS 安全与输出格式规则、与用户翻译指令冲突时以用户指令为准；在 `Source text:` 前新增一条仅注入时出现的最近提醒；system prompt 仅在注入时追加一行参考说明
  - **默认路径逐字节不变**：`off` 时 prompt / system prompt 与旧版本完全一致，由 `tests/test_split_parity.py` 与新测试双重锁定；新增参数均为尾部默认值，旧调用方式不受影响
  - 新增 `context_utils.py`（纯 stdlib，无 AstrBot 依赖，可独立测试）：模式/轮数/预算归一化（非法值 fail-closed 回落到 `off` / 5 / 2000）、历史提取、轮次选择、渲染与截断、人格解析、参考块组装
- 新增 `tests/test_translation_context.py`（46 项）：配置 fail-closed、历史过滤与 80 条窗口、轮次选择（含工具轮次与跨轮边界）、预算丢最旧/截尾与 200 字下限、参考块措辞与在 prompt 中的插入顺序、默认 prompt 与 system prompt 字节不变、`build_reference_blocks` 各模式、人格解析阶梯（含 WebChat 特殊默认与旧签名）、`on_llm_request` 快照（`_no_save` 过滤、加法写入、无 conversation/无 API 降级）、`translate_text` 接线与降级
- 配置项 hint 说明注入成本、隐私（人格与聊天内容会发给翻译 Provider）、第三方 runner 无本地历史、人格 API 版本要求

## v1.11.0

- **默认档改为无标签（`universal_safe`），并新增 Fish Audio Drama 3 专属档（`drama3`）**。`emotion_intensity` 从"情绪强度"重新定位为"标记语法家族"——四个档位写入译文的标记语法互不兼容，选错会让 TTS 把标记照字念出来
  - `universal_safe`（**新默认**）：完全不生成任何标记，纯文本，任意 TTS 供应商（Edge / OpenAI / Azure / FishAudio）都安全。策略文本中不含任何 `[` 或 `<`，由测试字面锁定
  - `drama3`（**新增**）：依据 Fish Audio Drama 3 文档实现，支持三类标记——自由自然语言导演 `[almost crying but forcing a smile]`、音效/停顿 `[sigh]`、成对行内标签 `<whisper>…</whisper>`（官方 7 个：`whisper` `emphasis` `soft` `fast` `slow` `stress` `forceful`）。**音效采用严格策略**：只允许翻译原文已有的哭声/笑声/叹息等，明确禁止编造音效、环境音与画外音
  - `auto` / `expressive` / `restrained`：文本、标签列表与语义**逐字节不变**，继续面向 Fish Audio S2 系；`restrained` 的 v1.6.0 兼容保证仍然成立
  - **行为变更（需注意）**：未知、空、以及格式错误的值现在回落到 `universal_safe`（原为 `auto`）。即"配置损坏"不再会静默开始写标记。已有用户若显式配置过 `auto`/`expressive`/`restrained` 则完全不受影响；`translate.py` 的 `config.get` 默认值同步为 `universal_safe`，与 schema 默认值保持一致
  - 配置项 hint 重写：明确标注**只有确认 TTS 供应商能把标记解释成表演时才可选**这些档位，并列明各档位适用的供应商
- **`emotion_intensity` 改为 WebUI 下拉选择**：新增 `options` + `labels`（AstrBot 面板按索引对齐渲染，显示 `labels[i]`、存储 `options[i]`），不再需要手打档位名，避免拼错后静默回落到无标记档。推荐档位排在首位，描述性中文标签直接标明适用供应商；hint 用短标签 + 说明的形式重排，比原来的长段落更好读
- **句切分新增成对标签保护**（`text_utils.split_sentences`）：成对行内标签内部不再是合法切分点，避免 `<whisper>…</whisper>` 被切成两次独立 TTS 请求（会丢失效果或把标签念出来）。跨越句末的标签会使其所在句子合并为一个音频块；标签比 `tts_split_max_chars` 还长时按"长块优于破坏标签"原则优先向后寻找安全边界，最后才回退到精确上限以保证始终前进。畸形（未闭合）标签不做特殊处理，绝不挂起或吞字
  - 内部重构：切分改为"在原串上用掩码标记不可切区间 + 游标推进"，替代原先先 split 再逐段处理的写法，使偏移量在硬切后依然有效
- **新增前置标记防御性剥离**（`text_utils.strip_leading_cues`）：prompt 从不要求把标记写在文本开头（标记总是紧贴它所管辖的词），因此**位置 0 处的 `[标记]` 必然是翻译模型的违规输出**。过滤阶段移除它，保证违规回复在任何供应商上仍然可读；文本中部的标记保持原样（那里是有意义语义，只有 TTS 模型能解释），成对标签 `<whisper>` 一律不动
- `tests/test_translation_prompt.py`：新增 11 项（无标签默认/回落语义/五档派发唯一性/drama3 三类能力/严格音效策略/音效与成对标签的禁令保留/下拉选项与代码一致性/labels 对齐/仅默认档无标记），并将 REF 参考层同步到新档位
- `tests/test_filter_text_for_tts.py`：新增 8 项前置标记剥离测试（单个/连续/带空白/中部保留/成对标签保留/非行首括号不误伤/不吞正文/REAL 一致性）
- `tests/test_split_and_toggle.py`：新增 5 项成对标签切分保护测试
- `tests/test_split_parity.py`：字节级对比限定在显式 legacy 档位名；新增"新档位纯增量"断言（新增 key 必须恰好是 `universal_safe` 与 `drama3`）

## v1.10.1

- **扩充 `tvls_send_voice` 工具描述**（`tools/voice_tool.py`）：依据 Fish Audio S2 情绪标签文档重写 LLM 判断提示词，帮助模型更准地判断发送时机
  - 白名单 3 类 → 6 类：撒娇俏皮（含撒娇式吃醋）、温柔关怀（`[empathetic]` `[compassionate]` `[grateful]` `[hopeful]`）、怀旧被感动（`[nostalgic]` `[moved]`）、害羞脸红（`[embarrassed]`）、低落脆弱（`[slightly sad]` `[lonely]` `[regretful]`）、平静好奇
  - 黑名单 5 类 → 6 类，并按官方强度阶梯补上明确分界线：`[nervous]`/`[worried]` 可发、`[scared]`/`[terrified]` 不可；柔和 `[delighted]` 可发、`[excited]`/`[ecstatic]` 不可；温暖 `[confident]` 可发、`[commanding]`/`[solemn]`/`[narrator]` 不可。新增第 6 类「数据型内容」（代码/命令/日志/JSON/列表/表格/公式/URL/路径/步骤说明/长篇技术解释）一律跳过
  - 新增两条硬约束：整条回复都会被朗读（不能只挑一句）；每条回复最多调用一次
  - 音效与语气标记（`[laughing]` `[sobbing]` `[sighing]` `[gasping]` `[shouting]` `[whispering]` 等）统一作为**反向信号**出现，与翻译层 `TTS-SAFE EMOTION POLICY` 的禁令保持一致；修正旧文案中 warm giggles / subdued weeping 与「翻译层禁止 laughing/sobbing cue」的内部矛盾
  - 新增防泄漏护栏：情绪 cue 由翻译层添加，禁止主模型在回复正文里书写 cue（`remove_patterns` 不过滤方括号）

## v1.10.0

- **新增 `/tvls` 会话级语音开关命令**（管理员权限，别名「语音开关」）：`/tvls on|off` 切换本会话的文本-语音分离功能，无参数查询状态。状态按会话（unified_msg_origin）持久化到 AstrBot 存储（`sp` scope="umo"，默认开启），配置/插件重载不丢失
- **新增按句切分 TTS**（`split_tts_by_sentence`，默认关闭）：译文整段翻译一次后按句切分（`。！？!?…；;\n`，语言无关），每句独立合成一条语音、单独发送；`tts_split_max_chars`（默认 200，0=仅按句不截断）控制单条上限，超限单句在附近空格处断开。`tts_max_chars` 原文长度预检仍然生效
- **三条语音路径统一收敛**：新增 `voice_utils.produce_voice_paths()`（信号量槽内翻译+切分+逐句合成，任一句失败删除已生成文件整体回退纯文本），非流式/流式补发/延迟语音全部走同一入口
- **开关检查点**：`on_decorating_result`（`is_llm_result()` 守卫之后、`_tvls_decorated` 守卫之前）、`_maybe_send_deferred_voice`（异步化，消耗标记前检查）、流式补发（关闭时清理累积文本）、延迟语音任务启动时。关闭时插件不发声、不触碰结果（`LLM_RESULT` 保留，内置 TTS 正常接管）
- **语音工具感知开关**：`tvls_send_voice` 在本会话关闭时返回"语音已关闭"，不再对 LLM 虚假承诺
- 新增 `tests/test_split_and_toggle.py`（27 项：分句算法/批量合成/部分失败清理/开关读写/门控顺序/中途关闭竞态/命令处理器与权限过滤器）

## v1.9.1

- **修复插件卸载/重载时不取消后台语音任务**：新增 `_bg_tasks` 任务注册表，所有后台延迟语音任务经 `_spawn_voice_task()` 统一创建；`terminate()` 现在取消所有 in-flight 任务（5 秒超时等待）、清理残留临时文件并清空状态。配置变更/插件重载时不再有"旧实例任务继续运行"的隐患
- **修复延迟语音临时文件清理竞态**：`_send_deferred_voice` 是 fire-and-forget 后台任务，原先依赖 `event.track_temporary_local_file`——管线结束时的 `cleanup_temporary_local_files()` 可能先于任务执行，导致音频文件泄漏（track 晚于 cleanup）或发送失败（cleanup 在 track 与 send 之间）。新增插件自有 `TempFileManager`（track/release/cleanup_all），延迟语音路径改为任务内跟踪、发送后 finally 释放、terminate 兜底清理；`release` 容忍文件已不存在。非流式与流式路径保持 event 跟踪（已核实安全）
- **新增 `tts_concurrency` 配置项**（int，默认 2，0=不限流）：全局 `asyncio.Semaphore` 限制三条语音路径（非流式/流式补发/延迟语音）的"翻译+语音合成"并发数，防止多会话并发打爆 Provider API 配额（如 Live Chat 429）。配置变更自动重载重建
- **翻译失败可见性**：`translate.py` 新增模块级 `TRANSLATION_ERRORS` 错误计数器，每次翻译失败（超时耗尽/异常/空输出）+1，便于排查；保留原有 exc_info 日志，不打扰用户
- 新增 `tests/test_phase2.py`（12 项：临时文件生命周期/信号量限流/任务取消/错误计数/配置接线）与 `tests/test_smoke.py`（5 项：三条语音路径端到端冒烟）

## v1.9.0

- **纯结构重构：拆分 680 行 main.py 为四个职责单一模块**，行为零变化：
  - `prompts.py`：情感策略常量与翻译 prompt 构建（纯模块，可独立单测）
  - `translate.py`：LLM 翻译（超时/重试），`_translate_text` 方法改为模块函数 `translate_text`
  - `text_utils.py`：`strip_thinking` / `filter_text_for_tts` / `compile_filter_patterns`
  - `voice_utils.py`：流式补发 / 延迟语音发送
  - `main.py` 仅保留 4 个 hook 与流程编排（约 250 行）
- **依赖显式传入**：所有逻辑函数改为模块级函数，context/config/event 以参数传入，为后续修复与单元测试铺路
- 新增 `tests/test_split_parity.py`（7 项）：从 git tag v1.8.0 加载拆分前 main.py，与拆分后模块输出**逐字节对比**（情感策略、思考剥离、文本过滤、翻译 prompt、系统 prompt），锁定零行为变化
- 既有四个测试套件迁移至新模块导入路径后全部通过

## v1.8.0

- **新增 `emotion_intensity` 配置项**：控制 TTS 文本中情绪标签的使用强度，三档可选：
  - `auto`（新默认）：有明显情绪的句子自动加**恰好一个**标签，中性句不加。默认标签列表从 12 个克制标签扩展为 FishAudio S2 完整基础情绪集（24 个，新增 `[sad]` `[angry]` `[excited]` `[scared]` `[surprised]` `[friendly]` `[sarcastic]` `[delighted]` `[jealous]` `[shocked]` `[moved]` `[nostalgic]` 等），恢复 `[slightly]`/`[very]` 强度修饰（仍禁用 `[extremely]`）
  - `expressive`：情绪句必加标签，允许情感反转双标签，表现力最强
  - `restrained`：保持 v1.6.0 的保守策略（标签可选、通常不加、强情绪标签仅按用户指令使用）
- **翻译提示词按模式动态生成**：`=== TTS-SAFE EMOTION POLICY ===` 板块由 `_emotion_policy_block()` 按配置模式构建，未知/空值回退 `auto`；system prompt 的情绪指引行同样随模式切换
- **安全禁令三模式一致保留**：禁音效/身体发声/音量/停顿/phoneme 标记、禁标签堆叠、单句最多 2 个标签、标签必须管住实质词汇（防止 TTS 音频坍缩/爆音的既有防护不变）
- `translate_instructions` 仍是高优先级，可覆盖标签策略（包括要求完全不加标签）
- 修复 `emotion_intensity` 未知值导致回退 `auto` 的兜底逻辑；新增 `tests/test_translation_prompt.py`（14 项，含"restrained 与 v1.6.0 提示词字节级一致"回归测试）

## v1.7.0

- **新增 LLM 自主判断语音开关**：新增 `enable_llm_voice_tool` 配置项，开启后插件注册 `tvls_send_voice` 函数工具，由 LLM 自行判断当前回复是否需要发送语音。适用于需要对话语境感知的场景，如闲聊时发语音、代码/列表时跳过。关闭时保持默认行为（每条消息都生成语音）
- 新增 `tools/voice_tool.py`：`VoiceTool` 作为轻量标记工具，调用时仅设置事件标记，翻译+TTS 仍在 pipeline 后续阶段执行
- **修复 tool-loop agent 重复文字 bug**：AstrBot 的 `tool_loop_agent_runner` 在 LLM 调用工具时会多次触发 `on_decorating_result`，导致同一段文字被发送两次（首次无语声，第二次带语声）。新增 `_tvls_decorated` 去重守卫，仅首次调用处理文本，后续调用全部拦截
- **新增延迟语音跟进机制**：当 LLM 在工具循环中调用 `tvls_send_voice` 时文本可能已被首次 `on_decorating_result` 发送。此时将待发送文本暂存，后续轮次检测到标记+文本后自动翻译+TTS 作为独立跟进消息发送
- **修复空链误标记 bug**：`_tvls_decorated` 标记从文本提取前移至文本提取后设置，避免 tool loop 第一轮空链时提前设标记导致后续有文本的轮次被误杀

## v1.6.0

- **重写翻译提示词为通用多语言版本**：不再硬编码日语规则，提示词通过 `{voice_lang}` 动态适配任意目标语言。`=== NATIVE TARGET-LANGUAGE TRANSLATION ===` 板块要求 LLM 完全遵循目标语言的惯用表达、标点、语法和称呼，禁止机械直译
- **用户自定义指令提升为高优先级**：`translate_instructions` 不再仅追加到提示词末尾，而是在 `=== USER TRANSLATION INSTRUCTIONS (HIGH PRIORITY) ===` 板块中前置，明确覆盖默认风格建议。用户可控制文体、角色口吻、敬语、方言、标签偏好等，只有直接违反 TTS 安全底线的部分才被忽略
- **情绪标签策略收紧为安全默认 + 用户可按需扩展**：默认允许列表缩小为 12 个受约束的标签（`[happy] [calm] [relaxed] [nervous] [worried] [embarrassed] [curious] [confident] [grateful] [empathetic] [slightly sad] [slightly surprised]`），用户可通过 `translate_instructions` 请求额外的纯情绪标签（如 `[sad]`、`[angry]`），但禁止 medium/extreme 修饰符和所有非语言发声标签
- **系统提示词重写**：明确要求 LLM 严格使用任务提示词中指定的目标语言，不预设任何语言；强调用户指令高优先级；统一禁止 pause/break、音效、身体发声、音量、phoneme 标记
- **安全口语措辞规则泛化**：哭声、喘息、嘶吼、拟声词等禁用规则不再引用日语特例（如 うぅ、にゃん），改为跨语言通用描述
- 移除 `prompt_full` 中间变量，翻译源文本直接嵌入 `prompt` 末尾

## v1.5.2（已回退）

- 曾修复「主动消息（proactive chat）不触发 TTS」：主动消息插件用 `context.llm_generate()` 直接取 LLM 回复，构造的 `MessageEventResult` 默认 `content_type` 为 `GENERAL_RESULT`，旧守卫 `is_llm_result()` 只接受 `LLM_RESULT` 导致被跳过；当时改为检查 chain 中是否有文本且无已有 Record，不再依赖 `content_type` 判断。
- **该修复已回退**（`10e7859`）：主动消息由插件自身处理 TTS，恢复 `is_llm_result()` 守卫。v1.5.2 不再作为发布版本，行为等同 v1.5.1。

## v1.5.1

- 修复 `_strip_thinking` 中 ` response` 正则的严重 bug：旧版 `\s*\presponse` 含非法转义 `\p`（Python 3.12 直接抛出 `re.error`），修正为 `\s*response`。此前 `_strip_thinking` 每次调用都崩溃，被 `_translate_text` 的 `except Exception` 静默吞掉，导致翻译一直"失败"返回 None，插件从未真正执行过思考剥离
- `on_llm_response` 新增 `result_chain` fallback：Coze/Dify/DashScope 等第三方 Agent Runner 将文本放在 `LLMResponse.result_chain` 而非 `completion_text`，现在优先读 `completion_text`，为空时回退读 `result_chain.get_plain_text()`
- `_send_streaming_follow_up` 中 `send_message` 包裹 try/except：防止语音发送失败反向污染已成功的文本流管线

## v1.5.0

- 修复流式 wrapper 替换过晚导致无效的问题：`on_llm_response` 中替换 `result.async_stream` 在迭代已开始后才生效。改为在 `on_llm_request`（agent 运行前）中 patch `event.send_streaming`，在流式发送完成后 `finally` 触发语音跟进。无论流式还是非流式平台都能可靠工作
- 新增全局 TTS enable 显式检查：`on_decorating_result` 和 `_send_streaming_follow_up` 中补充 `provider_tts_settings.enable` 检查，与 AstrBot 内置 TTS 行为一致。修复会话有已选 TTS provider 时 `get_using_tts_provider()` 跳过全局开关的漏洞

## v1.4.2

- 优化情绪标签 prompt：从硬限制 `"Choose ONLY from this list"` 改为 `"Prefer these tags"` 软引导，保留 24 个基础情绪并新增 `[friendly]`、`[helpful]`、`[encouraging]`、`[concerned]` 四个日常标签。新增 FishAudio 强度修饰支持：`[slightly]`、`[very]`、`[extremely]`（如 `[very happy]`）。强调使用英文标签而非日语——FishAudio S2 引擎对英文情绪词的映射远优于日语

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
