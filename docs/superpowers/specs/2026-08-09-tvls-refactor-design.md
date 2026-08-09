# astrbot_plugin_text_voice_lang_split 重构设计（v1.8.0 → v1.9.1）

日期：2026-08-09
范围：纯拆分 + 四项修复/优化，分两个 Phase 提交

## 背景与目标

- **问题**：main.py 680 行承担全部职责（情感策略常量、翻译、文本清洗、流式补丁、延迟语音、3 个 hook），难以维护与测试。
- **目标**：
  1. 拆分 main.py 为职责单一模块，逻辑零变化（Phase 1）
  2. 修复已证实的缺陷：2.1 终止不取消任务 / 2.3 延迟语音文件清理竞态 / 2.5 翻译失败可见性
  3. 增加并发限流（5.2）
  4. 提升可测试性（依赖显式传入 + 模块级函数）
- **明确不做**：2.2（语音先于文本＝特性保留）、4.2/4.3/4.4（命令开关/触发模式/批量 TTS，暂缓）、跨消息翻译缓存（用户否决）、2.4（代码验证不存在重复翻译 bug，丢弃）

## 约束（AGENTS.md 承重墙，不得触碰）

- `_tvls_decorated` 设置位置保持在"文本提取之后"（当前 main.py:398）
- `is_llm_result()` 守卫（main.py:364）
- `_strip_thinking` 的 `\s*response` 正则不变（`\p` 在 Python 3.12 崩溃）
- 所有 early-return 的 `GENERAL_RESULT` + `use_t2i_=False` + pop `_streaming_texts` 三元组逐个保留
- 情感策略常量（restrained/auto/expressive）逐字节迁移
- 无外部依赖；Python 3.10+；AstrBot >= 4.22.0

## Phase 1：纯拆分（commit #1，版本 → 1.9.0）

### 目标结构

```
astrbot_plugin_text_voice_lang_split/
├── main.py          # TextVoiceLangSplit(Star)：4 个 hook + 编排（~250 行）
├── prompts.py       # 情感策略常量 + 系统提示行 + prompt 构建
├── translate.py     # LLM 翻译（超时/重试/计数）
├── text_utils.py    # strip_thinking / filter_text_for_tts / compile_filter_patterns
├── voice_utils.py   # 语音门控 / 音频合成与跟踪 / 流式补发 / 延迟语音 / 后台任务与临时文件管理
├── tools/voice_tool.py（不变）
```

### 迁移映射（原 main.py 行号）

| 新位置 | 内容 |
|--------|------|
| prompts.py | `_EMOTION_POLICY_RESTRAINED`(15-55)、`_EMOTION_POLICY_AUTO`(57-96)、`_EMOTION_POLICY_EXPRESSIVE`(98-133)、`_EMOTION_SYSTEM_LINES`(135-145)、`emotion_policy_block()`(166-177)、`emotion_system_line()`(180-187)、`build_translation_prompt()`（提取自 212-260 的 prompt 拼接）、`build_translation_system_prompt()`（提取自 281-294） |
| translate.py | `_translate_text` → `translate_text(context, config, text, event)`(189-320)，方法改模块函数，依赖显式传入；模块级 `TRANSLATION_ERRORS` 计数器（Phase 2 用） |
| text_utils.py | `_strip_thinking`(347-356)、`_filter_text_for_tts` → `filter_text_for_tts(text, patterns)`(334-344)、`_compile_filter_patterns` → `compile_filter_patterns(patterns)`(325-332) |
| voice_utils.py | 门控三连查（provider/会话 TTS/全局 enable，3 处重复：376-388、530-541、619-637）；`send_streaming_follow_up(...)`(503-598)；`maybe_send_deferred_voice(...)`(600-616)；`send_deferred_voice(...)`(618-670)；后台任务与临时文件管理（Phase 2 用） |
| main.py | 保留：hook 编排、`initialize`/`terminate`、`__init__` 状态、`_get_session_key` |

### 设计原则

- 所有逻辑函数改为模块级，依赖（context/config/event/状态容器）显式传入 —— 可测试性提升的根本
- main.py 只做编排，不承载逻辑；Phase 2 的改动应尽量落在 voice_utils.py/translate.py 内部
- Phase 1 期间不得引入任何新行为（包括日志文本、错误路径、顺序）

### Phase 1 验收

- A：现有 4 套测试（tests/ 下，gitignored）改 import 新模块路径后全绿
- B：新增字节对比测试（git tag v1.8.0 基线 + 新旧实现输出一致）
- D：git tag v1.8.0
- C（Phase 2 后）：真实环境冒烟

## Phase 2：修复与优化（commit #2，版本 → 1.9.1）

### 2.1 终止取消后台任务

- 插件维护 `self._bg_tasks: set[asyncio.Task]`，统一经 `_spawn_voice_task(coro)` 创建、注册、`done_callback` 移除
- `terminate()`：cancel 全部任务 → `asyncio.gather(..., return_exceptions=True)` 短超时等待 → 清理残留临时文件 → 清空 `_streaming_texts`
- 背景：AstrBot 配置变更/重载会调用 `terminate()`（star_manager.py:273 reload），旧实例的后台延迟语音任务可能继续运行

### 2.3 延迟语音临时文件清理竞态

- **根因**：`_send_deferred_voice` 是 fire-and-forget 任务；管线末尾 `cleanup_temporary_local_files()`（scheduler.py:97）与任务执行竞态——track 晚于 cleanup → 文件泄漏；cleanup 在 track 与 send 之间 → 发送失败
- **方案**：延迟语音路径不再依赖 `event.track_temporary_local_file`，改用插件自有临时文件管理器：
  - `track(path)` 注册；`release(path)`（发送完成后 finally 调用）删除；`cleanup_all()`（terminate 兜底）
- **不变**：非流式路径（on_decorating_result 内同步完成）与流式补发路径（被 send_streaming 包装 await 同步完成，先于 cleanup）保持 event 跟踪 —— 已核实安全

### 2.5 翻译失败可见性

- `translate.py` 模块级 `TRANSLATION_ERRORS: int` 计数器，每次 `translate_text` 返回 None 时 +1
- 保留现有 exc_info 日志；main.py 提供读取方法（供排查/测试）
- 无用户可见消息（避免打扰聊天）

### 5.2 并发限流

- 新增配置 `tts_concurrency`（int，默认 2，0=不限流）
- 插件级全局 `asyncio.Semaphore`，三条语音路径（非流式/流式补发/延迟语音）在"翻译 + TTS 合成"段共用
- 配置变更触发插件 reload（star_manager.py:273），信号量随实例重建
- `_conf_schema.json` 用 `"int"` 类型（非 `"integer"`，否则 TypeError）

## 验证策略（四层）

- A：现有测试迁移后全绿（test_strip_thinking / test_filter_text_for_tts / test_translation_prompt / test_voice_tool）
- B：字节对比 golden 测试（Phase 1）：新旧 `filter_text_for_tts` / `strip_thinking` / 情感策略块 / 翻译 prompt 输出一致
- C：真实环境冒烟：非流式 / 流式 / 工具调用三条路径（Phase 2 后）
- D：git tag v1.8.0 基线

Phase 2 新增单测：任务注册/取消/terminate 清理、临时文件生命周期、翻译错误计数、信号量并发上限。

## 版本与提交

- Phase 1 完成 → `metadata.yaml` 1.9.0 + CHANGELOG + commit
- Phase 2 完成 → `metadata.yaml` 1.9.1 + CHANGELOG + commit
- 每 Phase 后：`ruff format` + `ruff check`（AstrBot 项目根执行）
