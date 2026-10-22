# 文本语音分离插件：`tvls_send_voice` 与 `tts_max_chars` 冲突 —— 诊断、设计 v10 与评审记录

- 版本：v1.12.0（`c77fe57`）为基线，**本文档不改动任何代码**。
- 结论：**两个已确认的线上 P0** 与一个**产品层硬约束**。修复方案（v10）已实现级详细，但方案门禁（跨模型独立评审）连续 9 轮未通过，故未落地。
- 环境：AstrBot 4.29.1；插件配置 `tts_max_chars=98`、`enable_llm_voice_tool=true`、`emotion_intensity=drama3`、`streaming_follow_up_delay=1.5`、`split_tts_by_sentence=false`。

---

## 1. 结论摘要

| 问题 | 定性 | 可修？ |
|---|---|---|
| 工具声称"会发语音"，但 `tts_max_chars` 超限时静默不发 | 线上 P0，可 100% 修复 | ✅ |
| 延迟语音自 v1.7.0 起从未真正发出（`clear_extra` 抛 `TypeError`） | 线上 P0，一行修复 | ✅ |
| 长度判定量到的是 thinktags 清洗**之前**的文本（819 字 vs 98 上限） | 线上 P0，需改判定点 | ✅ |
| "每次调用工具都多一条消息" | **插件无法消除**，只能由 AstrBot 的 `buffer_intermediate_messages` 合并 | ❌ 插件侧 |

---

## 2. 已确认的线上缺陷（含证据）

### P0-1　工具承诺 + 长度闸门静默否决

- `tools/voice_tool.py:81`：只要会话开关打开就 `set_extra("_tvls_voice_requested", True)` 并返回 `"Voice message will be sent."`，**没有任何长度前置条件**。
- `main.py:116-125`：过滤后文本超过 `tts_max_chars` 时 `logger.info` 后直接 `return`，语音不生成。
- 结果：模型被明确告知"会发语音"，用户拿不到语音，且日志里只有一行 `INFO`。

### P0-2　延迟语音路径整体失效（`clear_extra` 签名不符）

```python
# main.py:233-241（现状）
event.set_extra("_tvls_deferred_voice_sent", True)
event.clear_extra("_tvls_pending_text")      # ← 抛 TypeError
self._spawn_voice_task(voice_utils.send_deferred_voice(...))
```

- `AstrMessageEvent.clear_extra(self)` **不接受 key**（`astr_message_event.py:232`，它清空全部 extras）。
- 实测：`TypeError: AstrMessageEvent.clear_extra() takes 1 positional argument but 2 were given`。
- 异常在 `set_extra("_tvls_deferred_voice_sent", True)` **之后**抛出，被 `call_event_hook`（`pipeline/context_utils.py:102-103`）吞掉 → 任务未创建、请求被标记为"已处理"、后续任何触发都会因该标记而跳过。
- 该行由 `0df0e11`（v1.7.0 "deferred voice"）引入。生产日志中 **没有任何** `Deferred voice triggered` 记录；出现过的几条 `pending text: 'hello world...'` 全部来自 `tests/test_split_and_toggle.py`。
- 测试未捕获的原因：`tests/test_voice_tool.py:49-53` 的 `MockEvent.clear_extra(self, key: str = None)` 伪造了真实签名。

### P0-3　长度判定用的是"其它插件清洗前"的文本

- 真实日志（16:14:15）：
  ```
  main:118  Filtered text (819 chars) exceeds max (98), skip TTS
  thinktags main:156  已剥离 3 段英文思考前缀（671 字符）
  respond.stage:233  Prepare to send
  ```
- 装饰处理器按 `-priority` 排序（`star_handler.py:26`，数值大者先跑）：本插件 `priority=999` 先跑，`astrbot_plugin_thinktags`（默认 0）后跑并把 671 字符思考前缀从消息链剥掉。
- 后果：**可见文本很短，插件量到 819 字** → 触发上限 → 不发音；若未超限，则会把泄露的英文思考前缀**朗读出来**。
- 同时说明：`tts_max_chars` 的"超限"在真实使用中大多是思考前缀造成的，不是模型回复本身长。

---

## 3. 产品层硬约束：多余消息无法由插件消除

证据链（AstrBot 4.29.1）：

1. 工具循环每一步的助手文本都会作为独立 `llm_result` 产出：`tool_loop_agent_runner.py:924-948`，随后才执行工具（`:1014`）。
2. `run_agent` 对每个 `llm_result` 都 `set_result` + `yield`（`astr_agent_run_util.py:274-291`）→ 每个 yield 都走一遍装饰+投递（`scheduler.py:52-64`）。
3. 插件只能看到**最终响应**（`on_llm_response` 仅在 `astr_agent_run_util.py` 的 `on_agent_done` 触发，即最终/中止响应），因此能"冲掉"的只有最终那条。
4. 带另一次工具调用的中间消息**没有任何插件钩子能看到**。
5. AstrBot 的 `provider_settings.buffer_intermediate_messages=true` 会把多步中间文本**合并成一条**投递（`astr_agent_run_util.py:275-309`）——这是唯一的"一回合一条"手段，且仅非流式有效。

因此：**"1 条文字 + 1 条语音"的正确做法是开启该核心配置**；插件侧只应负责"不撒谎、该发就发、超限如实拒绝并告警"。

---

## 4. 实施规格 v10（实现级）

> 范围：只修 (a) 假承诺 + 两个 P0；不动消息条数、不引入定时器/状态机/响应改写。

### 4.1 `voice_utils.py` —— 单一判定

```python
VOICE_GATE_OK / EMPTY / TOO_LONG / DISABLED / NO_PROVIDER / SESSION_TTS_OFF / TTS_OFF

@dataclass(frozen=True)
class VoiceGate:
    ok: bool; reason: str; text: str; length: int; max_chars: int

def _coerce_cap(value) -> int            # 非整数/负数 → 0（不限），避免 "abc" > 0 的 TypeError
def voice_gate_text(text, config, filter_patterns) -> VoiceGate
    # filter_text_for_tts → len(filtered.strip()) >= 2 → max_chars>0 and len(filtered) > max_chars
async def voice_gate_env(context, config, event, tts_provider=None) -> VoiceGate
    # 会话开关 → TTS Provider → SessionServiceManager.should_process_tts_request → provider_tts_settings.enable
```

四个消费方必须共用：工具、回合末服务、流式补发、工具关闭时的内联路径。

### 4.2 `tools/voice_tool.py` —— 不做长度承诺

- `build_description(cap)`：保留现有情绪分类正文，前置调用协议：
  - 先写完整回复，再在同一条消息里**最后**调用本工具一次；
  - 语音朗读的是"本回合用户实际收到的文本"；该文本过长（约超过 N 字，按插件过滤后计）时**通常会被跳过**——这是**引导**，不是承诺；
  - 工具返回后不要再输出任何文字。
  - 过滤项必须按 `text_utils.filter_text_for_tts` 的真实行为描述（围栏/行内代码、URL、Markdown 强调、行首 `[cue]` 块、用户配置的 `remove_patterns`）；**不要声称会去掉 emoji**。
- `call()`：
  1. 已请求过 → 拒绝，不改状态；
  2. `voice_gate_env` 不通过 → 按 reason 返回实话，不置标记；
  3. 已送达文本**已**超限（单调，只会更长）→ 返回"不会发送：已送达 N 字，超过 M 字上限"，不置标记（这是**拒绝**，不是承诺）；
  4. 其余（含尚未送达任何文本）→ 置 `_tvls_voice_requested`，返回中性确认："语音请求已记录，不要再输出任何文字。"
- `main.initialize()` 里按配置重建描述并 `logger.info` 打印生效上限。

### 4.3 `main.py` —— 单归属点、确认时记账、无定时器

**回合状态**（event extras，`on_llm_request` 每回合重置一次，`internal.py:307`）：
`_tvls_decorated`、`_tvls_pipeline_delivered`、`_tvls_final_seen`、`_tvls_voice_requested`、`_tvls_deferred_voice_sent`。

**装饰阶段** `on_decorating_result(priority=999)`：
- 现有守卫（`is_llm_result`、会话开关、live）与能力闸门顺序不变；
- 找到 `plain_texts` 后，**工具模式一律** `GENERAL_RESULT` + `use_t2i_=False` + `pop(_streaming_texts)` + `return`（永不内联合成 → 唯一归属点，内置 TTS 不可能重复朗读）；
- 工具关闭时保留现有内联行为（爆炸半径最小）。

**记账** `after_message_sent(priority=999)`：
- 读 `event.get_result()`（此时是 **RespondStage 真正投递的那条链**，`clear_result()` 在钩子之后）；
- 若 `getattr(result, "_tvls_confirmed", False)` 为真则跳过（`MessageEventResult` 无 `slots`，可写该标记；**不要用 `id()`**）；
- 取 Plain 文本 → 过滤 → 追加到 `_tvls_pipeline_delivered`；置标记；
- **仅当 `_tvls_final_seen` 已置位**才执行服务（保证服务发生在回合最后一条消息之后）。

**回合末** `on_llm_response(priority=-1000)`（跑在 thinktags 等之后，看到的是清洗后文本）：
- 置 `_tvls_final_seen`；
- 最终文本为空（不会有投递）→ 立即执行服务；否则等紧接着的那条 `after_message_sent`。

**服务体**（唯一实现，两个触发点共用）：
- 前置：工具模式、会话开关、`_tvls_voice_requested`、`_delivered_voice_text` 非空、`_tvls_deferred_voice_sent` 未置；
- **先置标记再发**（同一事件循环 tick 内完成判定，避免并发双发）；
- `voice_gate_env` → `voice_gate_text`；任一不通过：**`logger.warning`** 带 reason 与字数（绝不静默）；通过则 `_spawn_voice_task(send_deferred_voice(...))`（TempFileManager 生命周期不变），发送前 `sleep(streaming_follow_up_delay)` 保证文字先到。

**语音文本来源（round-9 修订 1）**：`_delivered_voice_text(event)` **必须以一个权威来源为准**：
1. 优先用本回合已确认投递的装饰链文本；
2. **仅当**本回合没有任何装饰链文本时，才回退到 `_send_message_to_user_current_session_plain_texts`；
3. **不得把两者拼接**（否则会把用户分两条收到的内容当成一条朗读，且 cap 也会量错）。
   - 跨源去重仅在回退路径内做。

**重置告警**：`on_llm_request` 重置前若发现 `_tvls_voice_requested` 且未 `_tvls_deferred_voice_sent` → 一条 `logger.warning`（"上一回合请求的语音未送达"）。

**流式**：被 patch 的 `send_streaming` 包装器仍是流式归属方；`_tvls_deferred_voice_sent` **在发送成功后**才置位（不是之前），分片发送失败/取消 → 不置位并 `WARNING`（下一回合重置告警兜底）。`send_streaming_follow_up` 也改用 `voice_gate_text`。

**禁止项**：任何 `clear_extra(key)` 调用；任何消息链清空；任何 `resp.completion_text` 改写；任何 `on_decorating_result` 优先级变更。

### 4.4 配置与文档

- `_conf_schema.json`
  - `tts_max_chars` hint 改为："语音朗读的是本回合**用户实际收到**的可朗读文本；超过此值则跳过并记一条 WARNING。统计单位为整回合累计（多步工具回合会累加）。"
  - 同一 hint 内列出"已送达"的**排除项**：`tool_direct_result` 直发的链、平台丢弃的消息、`platform_settings.forward_threshold`（当前 600 字）触发的**转发包装**（长文本会变成 `Node`，插件侧看不到 Plain 文本）、以及 `show_tool_use_status=true` 时由核心在工具循环内直发的"🔨 调用工具"状态消息。
  - `enable_llm_voice_tool` hint 改为："开启后语音以**跟进消息**的形式在本回合文字之后发送；文字条数由 AstrBot 的『合并 Agent 中间消息』决定，插件不合并也不删除消息。"
- `README.md`
  - 配置表同步；
  - 新增「语音契约与消息条数」小节：(1) 工具模式 = 文字由核心投递 + 至多一条语音跟进；(2) 默认 `buffer_intermediate_messages=false` 时，工具调用前的回复与工具后的消息是**两条**，插件无法合并 —— 想要一条请开该核心项；(3) 超限 = 不发音 + WARNING，不会出现"答应了没发"。
  - 明确 `GENERAL_RESULT` 的副作用边界：**只影响本插件装饰到的那些消息**（这些消息会失去分段回复/splitter 处理，且读 `is_model_result()` 的插件会跳过）。
  - 真·修复"测量点"的问题：内联路径（工具关闭）仍在装饰阶段取文本，会读到其它插件清洗前的文本 —— 作为已知限制写明，并给出建议（把 `remove_patterns` 补上对应正则，或关闭内联路径改用工具模式）。
- `AGENTS.md`：删除 `_tvls_pending_text` 与"三个 `after_message_sent` 触发点"的旧描述（`AGENTS.md:68-79` 会与新实现冲突），改写不变量：① 只有一份判定函数；② 工具模式不得内联合成；③ 记账只在 `after_message_sent`（确认投递）；④ 服务只在"`_tvls_final_seen` 之后的投递"或"最终文本为空"时触发；⑤ 任何未送达的语音请求必须留下 WARNING；⑥ 禁止 `clear_extra(key)`。
- `CHANGELOG.md` v1.13.0、`metadata.yaml` 版本号。

### 4.5 测试矩阵（必须打生产模块）

**先修底座**（否则测不到本次改动）：
- `tests/test_voice_tool.py` 删除 `_tvls_pending_text` 相关用例；
- 集成层改为 `from astrbot_plugin_text_voice_lang_split.tools.voice_tool import VoiceTool, build_description`（现在导入的是手抄的 `VoiceToolReal`）；
- `MockEvent` 的 `set_extra(key, value)` / `get_extra(key=None, default=None)` / `clear_extra(self)` 用 `inspect.signature` 与真实类比对；
- TTS fixture 的 `get_audio` 用 `AsyncMock` 返回真实临时文件（现在是同步 `MagicMock`，导致 `TypeError` 被 `produce_voice_paths` 的 `except` 吞掉后仍打印 PASS）；
- 所有"发了语音"断言改为统计 `context.send_message` 的 await 次数（不要只断言标记）。

**用例**：
1. 超限回复 + 调用工具 → 拒绝文案含 N/M、无语音、文字恰好一条；
2. 未超限 + 调用工具 → 恰好一条语音，且发生在文字之后；
3. 工具先调、后写正文 → 中性受理，最终正文被朗读；
4. 两步回合（40+58 合格 / 40+59 超限）→ 后者在服务时被拒并 WARNING；
5. 三步回合 → 服务只发生在最终消息之后，语音文本 = 整回合已送达文本；
6. 缓冲模式（`buffer_intermediate_messages=true`）→ 合并成一条投递后服务；
7. 回合末零送达 → 恰一条 WARNING；
8. `send_message_to_user` 与装饰回复并存 → 朗读文本 = 已投递的回复（不得拼接）；
9. 流式：wrapper 成功后服务的恰好一次，回合末服务不再发；分片失败 → WARNING 且临时文件释放；
10. **跨回合**：上一回合遗留请求不得在新回合产生语音（重置回归测试）；
11. 静态断言：插件内不存在带参数的 `clear_extra(` 调用；
12. 9 个套件全绿 + `ruff format --check` / `ruff check` 干净。

---

## 5. 九轮独立评审记录（跨模型：提案方 v4.1-flash，评审方 v4-flash，同 provider）

| 轮 | 结论 | 它发现了什么 / 改变了什么 |
|---|---|---|
| 1 | FAIL | 清空 `result.chain` 会连带删掉 `meme_manager`（`priority=99999` 先跑）追加的图片；`_tvls_pending_text` 在多轮工具回合是陈旧的 |
| 2 | FAIL | `clear_extra(key)` 的 `TypeError`（P0-2）；按长度丢弃后续文本会误删真正的短结论；建议单判定函数 |
| 3 | FAIL | 装饰阶段量到的文本 ≠ 真正投递的文本（thinktags）；累积只覆盖第一条；触发覆盖不全 |
| 4 | FAIL | 排队时快照会朗读**引子**而非回复；流式 wrapper 不置"已服务"标记 → 双发；`id()` 令牌不可靠 |
| 5 | FAIL | 内置 TTS 与插件语音可能双读（工具模式某分支未设 `GENERAL_RESULT` —— 该轮**事实有误**，`main.py:134` 本来就设了）；缓冲是"合并"不是"删除" |
| 6 | FAIL | 工具先调会被拒（v6 引入的回归）；送达超时 vs 信号；cap 语义（单条 vs 整回合）未定义；测试底座是副本 |
| 7 | FAIL | 流式标记若前置会吞掉部分失败（真）；cap 判定时点与"整回合"契约矛盾（真，因服务触发点在投递前） |
| 8 | FAIL | 工具不能承诺长度（判定发生在文本产生之前）；服务必须在"最终消息之后"触发；缓冲模式下工具永远看不到已送达文本 |
| 9 | FAIL | `_delivered_voice_text` 跨源拼接（会把两条内容当一条朗读）；"已置标记 = 已发送"把失败伪装成成功（`send_deferred_voice` 失败只记 INFO）；转发包装（`forward_threshold=600`）会让长文本变 `Node` 而记不到；跨回合回归测试缺失 |

**教训（对后续任何重做有效）**：
1. 不要在插件里重建投递/合并语义（AstrBot 已提供 `buffer_intermediate_messages`）。
2. "已请求"与"已送达"必须是两个状态；后者只能在**投递确认点**（`after_message_sent`）获得。
3. 语音文本必须来自**单一权威来源**，且与 cap 判定的对象是同一份文本。
4. 任何"气泡地删除用户可见内容"的设计都会被否 —— 宁可多一条消息。
5. 测试必须驱动生产模块；手抄副本 + 同步 `MagicMock` 会让死路径显示 PASS。

---

## 6. 立即可用的最小热修（若只想先恢复语音）

```diff
--- a/main.py
+++ b/main.py
@@ -233,7 +233,7 @@
         event.set_extra("_tvls_deferred_voice_sent", True)
-        event.clear_extra("_tvls_pending_text")
+        event.set_extra("_tvls_pending_text", None)
         logger.info(
```

- 作用：让延迟语音路径重新可用（`get_extra(..., None)` 的后续判真逻辑不变）。
- **副作用**：延迟语音会**第一次真正发出**，因此你会开始看到"语音跟进消息"；若模型在工具调用后仍写了文字，那些文字也会出现（这是第 3 节的核心约束，需靠 `buffer_intermediate_messages` 合并）。
- 注意：这行改动**没有**解决 P0-1（假承诺）与 P0-3（测量点），只是恢复功能。
- 该改动属于 Level 2，若要入库仍需通过评审；本文件仅给出内容。

---

## 7. 部署相关注意（你的当前配置）

1. **`streaming_response` 双来源冲突**：根 `data/cmd_config.json` 为 `true`，会话档 `data/config/abconf_9caab07a-….json` 为 `false`，DB 内无 per-session 覆盖；实测日志走的是**非流式**分支。实现时必须同时兼容两条路径（流式由 wrapper 归属，非流式由回合末服务归属）。
2. **`forward_threshold=600`**（`platform_settings`）：长于该值的消息在 `result_decorate/stage.py:415-426` 会被包成 `Node` 转发 → `after_message_sent` 读到的链里没有 Plain 文本，记账会漏。你的 `tts_max_chars=98` 使被朗读的回复基本不会触发，但 `cap` 判定在同回合有长文本时会失真，需在 hint/README 写明。
3. **`show_tool_use_status`**：你的配置为 `false`（无"🔨 调用工具"消息）；但代码缺省是 `true`，其它用户会产生由核心直发的状态消息，插件既看不到也不记账。
4. **thinktags 与本插件的顺序**：本插件 `priority=999` 先跑。工具模式下由确认时记账（响应阶段之后）规避了"量到思考前缀"；**内联路径（工具关闭）仍有该问题**。
