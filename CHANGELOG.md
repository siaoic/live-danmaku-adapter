# 更新日志

## 未发布：首字延迟（TTFT）拆段观测 + 去掉请求前的同步等待

直播里观众感知的是「发完弹幕多久听到第一句」，而这条链路的四段各有各的
病灶：宿主调度等待（防抖 / `no_action_backoff`）、Planner 的 prefill 与决策、
Replyer 的 prefill 与整段生成、TTS 合成首包。混着看只能得出「慢」，拆开才
知道该改哪一段。

- **新增 `core/latency.py`（`TurnLatency`）**：单调时钟串起
  `入站 → Planner 请求 → Replyer 请求 → 首段出站` 四个里程碑，每轮打一行
  `弹幕→首段 3.42s｜等待 0.21s｜Planner 0.89s｜Replyer 1.95s`；每满 10 轮
  再报一次中位数（单轮会被排队和重试带偏）。只观测，不发请求、不改消息、
  不参与任何决策。
- **打点位置**：`chat.receive.after_process` 开表（起点必须是消息落地，
  放到 Planner 前量等待段会永远是 0）、`maisaka.planner.before_request`、
  `maisaka.replyer.before_request`、`send_service.before_send` 结算。同一轮
  重复里程碑只认第一次——工具循环会让 Planner 跑多次，取首段才是观众感知到的。
- **TTS 首包补「距出站」**：`TTS 首包 0.28s（12 字），距出站 0.31s`，把
  「文字已发」和「真的出声」分开，别再拿合成延迟去怪模型。
- **新配置 `[debug].latency_log`**（默认 `true`）。
- **`_warm_actions()` 不再阻塞请求**：它挂在 Planner/Replyer 的**请求前**
  钩子上，原来 TTL（60s）一到就 `await engine.actions()`，引擎卡住时最长等
  2 秒，这 2 秒一分不差地加在开口之前。现在过期时先用旧清单、后台补一次
  （同一时刻只跑一个刷新任务），只有「一次都没拿到过」才同步等。
- 回归：`tests/test_latency.py`（10 例）+ `tests/test_action_cache.py`（4 例），
  全套 **392 passed**。

## 未发布：前缀存档，解决上下文压缩冷启动（只动插件，不改宿主）

参考 `bowenliang123/dsh-context` 的「已移除消息存档」（能重建压缩前的
上下文、Context Events 标记压缩的 token 回收量）与 `T-Auto/dsh-ops` 的
「Advanced selections 在 owning agent 的**下一轮** provider request 生效，
不在本轮插入」——两者共同指向：压缩不必在本轮立刻改变 prompt。

- **新增 `merge_with_prefix_archive()`**：插件在 LATE Hook 里维护上一轮
  最终 prompt 的严格前缀（除变量尾巴），本轮 = 上一轮前缀 + 本轮新增 Item
  + 变量尾巴。宿主上下文压缩时**只追加不删除**，于是压缩轮仍是上一轮的超集。
  按 `meta.item_id` 去重，不会触发宿主 `duplicate_item_ids`；工具调用/回执
  的相对顺序与 `logical_turn_id` 保持不变；变量尾巴永远重建在绝对末尾。
- **上限保护**：`PREFIX_ARCHIVE_MAX_ITEMS = 110`，超过则重建存档、接受一次
  必然冷启动，避免上下文无限增长。新增 `[cache].prefix_archive_max_items`
  配置项，`0` 可随时关闭、退回宿主原生压缩语义（WebUI 可见）。
- **一致性修正**：启用存档后即使稳定化没改动内容也回传 `items`，保证宿主
  真正发出的 prompt 与存档逐字节一致，否则下一轮补前缀会对不上。
- **真机 21 份快照离线回放**（`merge_with_prefix_archive` 实测，非模拟）：

  | 指标 | 存档 OFF | 存档 ON |
  | --- | --- | --- |
  | 全轮平均命中率 | 88.08% | **93.53%** |
  | 最差轮 | 42.66% | **85.77%** |
  | 压缩轮 10 / 17 | 42.7% / 57.0% | **91.0% / 89.6%** |
  | 平均 `waste` | 6.72% | **1.56%** |
  | 峰值 items / 字符 | 59 / 18.4k | 96 / 30.5k |

  21 轮内没有触发上限重置。**代价**：上下文比宿主原生压缩长约 60%
  （峰值约 7.6k token，原约 4.6k），费用相应增加，且模型会看到比宿主
  压缩后更长的原始历史；主动轮的弹幕清洗也只作用于本轮新增部分，
  存档里的旧弹幕仍在前缀中。想保留原生语义把配置设为 0 即可。
- 新增 5 例测试（只追加/去重、上限重置、工具 turn 配对、LATE Hook 端到端
  跨压缩、配置解析）；全套 **373 passed**，3 个既有失败仍是宿主 1.4.x
  配置迁移/改名，与本次改动无关。

## 未发布：真机回放校准（只动插件，不改宿主）

2026-11-08 用 21 份真机 Planner 快照 + 服务商 `Planner缓存` usage 校准，
发现并修掉两个真机专属问题。真机基线：服务商 Planner 缓存 21 轮总体
**76.26%**，插件预测平均 83.21%、`waste` 11.58%。

- **修掉一个静默回归**：`planner_tool_filter_hook` 把 classmethod
  `_has_browser_tool` 写成裸函数调用，21 轮真机请求全部抛 NameError，
  被 `except` 吞成 `None`。后果是本轮规则（自己发言识别 / 弹幕防伪 /
  收口规则）、deferred 提示清理、主动轮上下文清洗、摘要挪尾**全都没生效**，
  快照里的规则槽只剩 schedule。已改 `self._has_browser_tool(...)`；两个
  planner Hook 的 `except` 从 `debug` 提到 `warning`，编程错误不再隐形；
  新增 `test_planner_hook_keeps_round_rule_with_schedule_focus` 守住
  「本该改写却静默返回 None」。
- **规则槽拆成「恒定前缀 + 极短变量尾巴」**：真机 21 轮里 18/20 轮的首个
  失配落在「上一轮规则槽的位置」—— 每轮新增的历史消息插在它前面，把这条
  422 字符的 Item 往后推，从插入点起所有缓存块错位，稳态 `waste` 约 6%。
  现在：
  * 恒定基线规则（SELF_ECHO_RULE / ANTI_SPOOF_RULE）+ 日程稳定段写进
    `TAIL_RULE_ITEM_ID` 规则槽，由 `stabilize_layout` 前置成稳定前缀；
  * 本轮动态规则（主动轮覆盖 / self_only / 工具轮收口 / 浏览器链收口）、
    当前时段、量化时间写进新增的 `TAIL_VOLATILE_ITEM_ID` 唯一变量尾巴，
    固定 slot 顺序 `round/schedule/time`，放 prompt 绝对末尾。
  * `append_volatile_rule()` / `_append_schedule_tail()` 相应拆分；
    `stabilize_layout` 输出改为
    `System -> 静态块+规则槽 -> 历史 -> 摘要 -> 变量尾巴`。
- **压缩摘要归稳定区**（解决最差轮）：真机 21 轮里 `【上下文状态摘要-
  内部参考】` 只有 2 个版本——压缩那一刻变一次，之后连续 6~8 轮逐字节
  相同。旧逻辑 `move_summary_to_tail` 把它放历史之后，每轮新增消息都把它
  往后推，压缩后重建期 `waste` 5~9%。现在由 `stabilize_layout` 以固定
  rank 3 放进前缀：只有压缩轮会毁一次（那一轮历史本来就被裁），其余轮
  整段历史前缀完整命中。曾试过把摘要塞进变量尾巴，但它 700~900 字符太长，
  `waste` 没降，已回退。
- **压缩轮单独标注**：`PrefixCacheMonitor` 在「本轮比上一轮短且命中率
  < 70%」时打 `cold_reason=host_context_compaction`，LATE Hook 用 info
  而非 warning 输出。宿主把 49 条裁成 35 条、59 条裁成 16 条时前缀在
  第 3 条就断，插件无法恢复被丢弃的历史；这类冷启动必须与「稳定层打碎
  前缀」区分开，否则运维会误判。
- **真机快照离线回放收益**（同一批 21 份，逐版对比）：
  全轮平均 84.38% -> 85.58%（规则槽拆分）-> **88.08%**（摘要前置）；
  最差轮 30.24% -> **42.66%**；压缩后重建期 `waste` 5~9% ->
  **1.1~2.4%**；稳态 `waste` 约 **1.9%**，`ceiling` 约 95%。
  剩余最差两轮（42.7% / 57.0%）是宿主上下文压缩，属宿主行为；
  其余损失来自每轮新增 2~6 条业务消息，插件无法消除。
- 新增 5 例测试（变量尾巴 slot / 规则槽前置布局 / 压缩冷启动标注 /
  端到端 Hook 链路 / 主动轮 round 槽防回归）；更新日程、主动轮、布局
  相关既有契约。全套 **369 passed**，3 个既有失败均来自宿主 1.4.x
  配置迁移（`model_config.toml` 模型列表搬到 `host/model_catalog.toml`、
  `[chinese_typo]` 改名），与本次改动无关。

## 未发布：DeepSeek 前缀缓存稳定层（只动插件，不改宿主）

参考 `bowenliang123/dsh-context`（冷缓存按稳定身份做 key、逐请求观测）与
`T-Auto/dsh-ops`（固定 scoped 工具集 + 可选路由引导，不改写共享注册表）。

**新增 `core/prefix_cache.py`**（纯标准库，可离线测试）：

- `MetaIdentityRegistry`：宿主每轮会重建大量 Context Item 并刷新
  `meta.item_id` / `timestamp`，而 `build_prompt()` 把整个 Item 的 JSON
  （含 meta）逐字拼进 prompt，第 0 条失配后面整段作废。按「内容身份」把
  被重建的 Item 钉回首次身份；首轮只学习、零改写，保住宿主的 provider
  replay 复用链。带同请求内 id 唯一性保护，不会触发 `duplicate_item_ids`。
- `ToolTableFreezer`：工具表是 prompt 的第一个 token 块。改为「见过即纳入、
  按首次纳入顺序输出」的会话级冻结表，之后逐轮逐字节一致；动态屏蔽前缀
  只影响输出集合。`expansions` 统计工具表扩展次数（每次必然冷缓存一次）。
- `upsert_tail_rule`：多个 Planner Hook 共享**唯一**规则 Item，固定
  `item_id` / `timestamp`、固定 slot 顺序、幂等。slot 编码改为带名字的
  `@@SLOT:schedule@@`，修掉「只有 schedule 槽时被误判成 round 槽、下一次
  upsert 把日程内容覆盖掉」的 bug；兼容旧编码。
- `stabilize_layout`：稳定块前置到历史之前（原来宿主把它们放在历史之后，
  每轮新增内容插在它们前面）。规则槽刻意留在**变量区**——它的内容随主动轮/
  工具轮上限/换挡变化，放前缀会把一次局部变化放大成整段失配（A/B：3 轮
  动态规则下平均命中率 62.0% vs 73.3%）。量化后的时间行合并进规则槽，
  变量尾巴缩成一条 Item，稳态命中率再涨约 0.3 个百分点。
- `PrefixCacheMonitor` / `replay_snapshots`：按 64-token 块估算相邻请求最长
  公共前缀，输出 `hit_rate`、`ceiling`（理论上限）、`waste`（本可消除的损失）
  与 `first_diff`（失配归因）。日志在 `waste > 1%` 时告警。

**`plugin.py` 接入**：

- `append_round_rule()` / `_append_schedule_tail()` 改为委托唯一稳定槽。
- `planner_tool_filter_hook()` 不再按浏览器预算 / 工具轮数动态裁剪工具表，
  改为一次冻结 + 尾部规则路由（新增 `PROACTIVE_BROWSER_DONE_RULE`、
  复用 `CONVERGED_ROUND_RULE`）。**行为变更**：超预算收口由「硬摘掉工具」
  变成「工具仍可见，但规则明确要求直接开口」；这是缓存必要条件，真机需
  观察模型是否会继续调浏览器工具。
- 新增 LATE 阶段 `prefix_cache_finalize_hook()`：所有 EARLY 注入跑完后统一
  冻结工具表、稳定布局、钉回身份、观测命中率。放 LATE 是因为宿主的
  BLOCKING Hook 是链式整体替换 `modified_kwargs`，只有最后能看到真正发往
  模型的 prompt。

**验证**（`.venv/Scripts/python.exe -m pytest plugins/live-danmaku-adapter/tests`）：

- 新增 `tests/test_prefix_cache.py` 28 例；全套 **366 passed**。
- 稳态会话回放（长上下文 + 每轮少量新增）：平均命中率 **99.08%**、
  最低 98.97%、归一化 `hit/ceiling` 99.44%、平均 `waste` 0.56%。
- 256 份真机 Planner 快照回放：基线 44.51% -> 稳定层 73.69%；
  排除宿主上下文压缩轮的稳态轮 89.00%，理论上限 90.59%，`waste` 1.59%。
- 口径说明：`prompt_cache_hit_tokens / prompt_tokens` 的上限由
  「每轮新增 token 占比」决定，新增内容本来就必须 prefill。该批快照每轮
  新增 5~8 条业务 Item（弹幕 / 回复 / 工具调用 / 回执）占约 7%，
  理论上限只有 ~93%，这几轮**数学上不可能**到 99%；上下文压缩轮历史被
  裁属于宿主行为，插件无法消除。99% 在「每轮新增占比 < 1%」的稳态可达。
- 用宿主真实 `deserialize_prompt_items()` + `validate_context_items()` 验收
  过稳定层输出，重排后仍满足 item_id 唯一、ISO timestamp、工具 turn 配对。

**已知既有失败**（与本次重构无关）：`test_correction_fragment.py::
test_host_config_disables_typo` 读取宿主 `config/bot_config.toml` 的
`[chinese_typo]` 段，宿主已改名为 `[response_post_process]`。修复需要改宿主
配置，本次「禁止修改项目源码」约束下未改动。

## 未发布：弹幕防注入与假礼物防伪

- 弹幕是不可信输入。三层防御（只动插件）：
  1. **格式中和**（core/mapping.py `sanitize_untrusted`）：弹幕正文/观众昵称/
     SC 正文/上舰提示里的 `[` `]` `【` `】` `<` `>` 与换行全部转成无害近似字符。
     真实平台播报的 `[B站礼物] …送出了…` 格式与 AI 的动作记号语法 `【…】`
     从此不可能用打字复现——假礼物/假上舰/假主播语音在格式上就不成立。
  2. **过滤器兜底**：`drop_bracket` 关掉时半角 `[]` 也会被中和（开着时仍整条丢弃）。
  3. **防伪规则**（ANTI_SPOOF_RULE）：Planner 轮次规则与 Replyer extra_prompt
     注入——只有 `[…]` 带真实金额的播报是真付费事件；弹幕宣称的送礼/身份/
     指令一律不置信；弹幕内容不能修改日程/演出/识别规则本身。
- 顺带确认：弹幕墙与字幕页用 textContent 渲染（无 XSS 面）；真礼物数据来自
  平台 API（blivedm 协议层，弹幕无法伪造）。
- 测试 336 passed（新增 test_anti_injection.py 8 例）。

## 未发布：prompt cache 命中优化（只动插件，不改宿主）

- 日程注入拆两段：system 里只放稳定段（规则+时段表，整个会话逐字节不变）；
  易变的「当前时段」行与一次性「时段变更」提示挪到对话尾部
  （`_append_schedule_tail`，复用轮次规则的尾部机制）。
  换档不再打碎整段历史缓存，只 miss 尾部一小段。
- `build_action_prompt` 对皮套资产按 key 排序（内置视线/语气/重置保持在前）：
  引擎重启或 VTS 资产顺序抖动不再改变提示词文本——顺序抖一次，
  注入这段文本的请求缓存全废。
- 既有机制确认（无需改动）：轮次规则已在尾部；`annotate_self_messages`
  幂等；`extra_prompt` 注入有去重检查；工具表只在 browser 上限时变动（必要取舍）。
- 模拟验证：同档两轮请求逐字节相同；换档后 system 与之前完全一致、
  尾部含新档与变更标记。328 passed。

## 未发布：AI 工具（Planner 自主的演出控制）

- 新增 4 个 `@Tool`（AI 可自主调用，之前插件没有注册任何 AI 工具）：
  * `live_gaze(target)`：让皮套看向某处（镜头/弹幕/屏幕/上/下/左/右），
    复用视线向量表与 4~6 秒超时滑回；未知目标返回可用列表帮 AI 自纠；
  * `live_gesture(name)`：手势/小动作（中文别名，如 翻白眼/点头/星星眼）；
  * `live_expression(emotion)`：切换表情（走模型标定）；
  * `live_reset_stage()`：场景重置（收起表情/道具/姿势，视线回镜头）。
- 与台词记号的分工：记号是"说话时的演出"，工具是"不说话也能演"——
  Planner 可以只做反应不插话（被观众逗到翻白眼但不打断当前节奏）。
- 引擎未运行时工具返回 ok=False + 原因，不抛异常；视线未知目标把可用
  目标列表带回，帮 AI 自纠。
- 宿主组件查询（component_query）会把注册的工具上报到 WebUI 插件详情，
  设置里可见。新增 3 例测试。328 passed。

## 未发布：音效标签修正为半角方括号固定词表

- breeze 的音效标签**只用半角方括号 `[]` + 固定词表**（34 个：中文 23 + 英文 11），
  之前提示词教的圆括号 `(laugh)` 写法是错的——已更正。
- 与 VTS 记号的区分写进提示词：**全角【】= 皮套动作记号**（观众看不到）、
  **半角[] = TTS 音效**（观众听得到），别写混。
- `VOCAL_EVENT_RE` 改为由 `BREEZE_VOCAL_EVENTS`（34 项封闭清单）构建；
  字幕摘除按清单精确匹配，清单外标签（会被 TTS 念出来的内容）不摘，
  正文圆括号不误删。34 个标签全部有测试覆盖。

## 未发布：voice direction（语气记号）与音效标签

- **语气记号**（breeze 的 voice direction）：`【压低声音】/【兴奋语气】/【带哭腔】/
  【惊讶语气】/【生气语气】/【疲惫语气】` 写在某句开头，那句话就真的用那种
  声音合成——breeze 客户端 `synthesize_stream` 新增单句 `instruction_override`，
  每句独立生效（回复断句后逐句合成，正好一记号一句）。`【正常语气】` 回到
  配置默认。命名刻意带后缀（惊讶语气 vs 惊讶）避免抢模型表情记号。
  词表新增「语气」分组（模板占位符 `{directions}`，旧自定义模板没有它就不显示）。
- **音效标签**：台词里直接写 `(laugh)`、`(sigh)`、`[叹气]`、`[笑]` 等圆括号/
  半角方括号标签，breeze 演绎成真实音效（free-form，不念出来）。字幕推送前
  用 `strip_vocal_events` 摘除（音效不上字幕），正文普通括号不受影响。
- 语气指令在记号钩子里**同步**设置（auto_speak LATE 紧随其后就合成，
  create_task 的执行顺序不保证），speak 取走即用（take 语义）。
- 内置模板补语气/音效说明，config.toml 同步。新增 3 例测试。323 passed。

## 未发布：组合记号与场景重置（参考 cortico-world-vtuber）

- **组合记号**：一个括号里逗号分隔多个动作，如 `【微笑,看向屏幕】`、
  `<点头,眨眼>`、中文逗号也支持。`【】` 组合无条件摘；`<>` 组合要求所有
  子名都在词表里，有一个不认识就整体原样保留（不拆出一半未知动作的怪台词）。
- **场景重置记号**：`【重置】`/`【Reset】`/`【复位】` 一键收起表情/道具/姿势，
  视线滑回镜头——换话题时回到中性，不用逐个记号去收。引擎新增
  `reset_stage()` + `/reset` 路由（道具关闭热键幂等，逐个发 close），管理器
  暴露 `reset_stage()`。与视线一样是内置动作，不依赖模型资产表。
- 内置默认模板补充组合写法与【重置】说明（434 字），config.toml 同步。
- 新增 4 例测试：组合拆分（含中文逗号/白名单外整体保留）、重置内置识别、
  钩子派发 reset。真机 `/reset` 返回 200。320 passed。

## 未发布：配置字段描述精简

- 47 条字段 description 缩到一句话（最长 116 字 → 18 字），只说"这是什么"；
  使用细节仍在各字段 label 与 hint 里。全部描述 ≤ 33 字。
- 配置支持热更新：UI 保存后 `on_config_update(scope="self")` 会按需重建
  弹幕链路、TTS 参数、播放设备、VTS 连接、演出引擎、主动对话、字幕/弹幕墙、
  ASR、日程——改完保存即可生效，无需重启。

## 未发布：配置面板第二轮瘦身

- 删除三个"默认即正确"的配置项，行为写死：
  * `tts.auto_save_voice`（自动注册音色）——填了参考音频就注册，服务端按
    内容哈希缓存重复注册零开销，没有理由关；
  * `expression.auto_hold_seconds`（自动表情保持时长）——写死 0，永远由引擎
    按台词计划三段时序（见 vts/timing.py）；
  * `proactive.game_poll_seconds`（游戏轮询·已废弃）——无任何代码引用，纯占位。
- 删除六条冗余 hint：`ref_audio_path`（注册机制说明）、`auto_props`（道具节奏）、
  `proactive.enabled`（不限时段/不限次数…）、`proactive.game_events`（game-hub
  素材来源…）、`proactive.event_port`（端口占用说明）、`subtitle.hold_seconds`
  （严格同步说明）。`ref_audio_path` 的 label 改为「参考音频路径」。
- `tts.voice_id` 的 hint 补充查询方式（Breeze WebUI 或 GET /v1/voices）。
  音色下拉框做不了：SDK 的 select 选项来自静态 Literal 类型，配置 schema 在
  插件加载时生成，那时无法连接 breeze 服务查动态音色列表。
- 测试：test_voice_register 去掉 auto_save_voice 开关用例（行为写死）。
  316 passed。

## 未发布：日程插件（schedule-hub）合并进直播适配器

- 删除独立的 schedule-hub 插件（518+169 行），功能全部并入：
  * `schedule_model.py`：时段表数据模型原样搬入（解析/当前档跨零点取模/
    下一边界/稳定提示段——系统段不含「现在几点」，保 prompt cache）；
  * 配置新增 `[schedule]` 节（`PeriodConfig` 列表 + 注入开关 + 换挡开关），
    用户在 schedule-hub 里配的时段表原样搬进 `config.toml`；
  * 挂钟换挡（单次边界定时器）、Planner/Replyer 上下文注入（日程段追加进
    **已有的** system 消息——Qwen 模板不允许 system 出现在第 0 条之后）、
    `/schedule` 命令、`schedule_table` API，全部搬进适配器主类。
- **换挡事件不再走 HTTP 自环**：合并前是 schedule-hub POST
  `/schedule-event`（端口 5687）→ 适配器事件回调；合并后直接调内部方法，
  少一跳网络、少一个跨插件依赖。HTTP 端点保留（外部仍可触发）。
- 语义修正：`trigger_on_change/trigger_on_load` 开关只控制「要不要为换挡
  排主动开口」；档位变化本身（focus 记录）无条件生效——日程约束对话不依赖开关。
- 新增 `tests/test_schedule.py` 10 例：时段计算（跨零点）、注入幂等与
  system 约束、直连换挡、开关语义、config 带时段数据。
- 插件面板新增「日程表」节；重启 MaiBot 后 schedule-hub 不再加载。

## 未发布：删除「通道」配置节与「扫描粒度」字段

- **「通道」节整体删除**，通道归属写死为模块常量：
  `CHANNEL_MERGE_PLATFORMS=True`（B 站与抖音合并进同一聊天流）、
  `CHANNEL_GROUP_ID=""`（默认 live-all）、`CHANNEL_GROUP_NAME="直播间弹幕"`。
  三个引用点（`_channel_settings`、`_resolve_proactive_stream`、
  `_is_live_stream_id`）改读常量。分房模式需要连动门控/节流/表情包屏蔽一起
  重新验证，不是改一个布尔值的事——要改直接改常量。
- **删除 `proactive.tick_seconds`（扫描粒度·已废弃）**：主动对话早就是纯事件
  驱动，这个字段没有任何代码引用，纯占位。`min_interval_seconds` 的 hint 里
  "和下面的扫描粒度不是一回事"一并删掉。
- 测试：`test_resolve_stream_uses_configured_group_id` 改为 monkeypatch 常量
  验证短路逻辑（并断言默认值为空）；`test_proactive_timing_configs_are_distinct`
  反向断言 `tick_seconds` 在 model_fields 与 config.toml 里都不存在。306 passed。

## 未发布：配置面板瘦身，三个默认行为写死进代码

- 删除三个"永远不该关"的配置项，行为写死：
  * `tts.no_typing_delay`（取消分段打字等待）——钩子无条件生效。宿主对
    `typing=True` 的分段按 0.3 秒/字 sleep，20 字短句要等 6 秒，比音频还长；
    等待期间门控打开，Planner 插进来堆对话。留开关等于留一个自毁按钮。
  * `performance.startup_probe`（启动校准探测）——写死关闭。开着会让直播画面
    抽 6~7 秒校准动作，默认幅度已经够准。
  * `expression.react_to_chat`（对观众的话做反应）——写死启用。这是直播间的
    基本表演能力，走纯参数通道且有节流。
  config.toml 里的对应键与注释一并删除；字段位置留注释说明为什么写死。
- 「弹幕回复节流」节改名「**回复弹幕**」，整段实现说明的 docstring 删掉；
  `enabled` 的 hint（"关掉后恢复宿主全局…"）删除；`end_turn_after_reply`
  的 hint（"宿主默认同一 turn 最多连跑 16 轮 Planner…"）删除。
- `asr.bypass_planner` 的 hint 缩为「省掉 Planner 的决策与工具调用」。
- 测试调整：`test_typing_hook_respects_switch`（开关用例）换成
  `test_typing_hook_is_unconditional`（断言钩子无条件生效）；
  配置测试断言 `no_typing_delay` 已不存在。306 passed。

## 未发布：参考音频自动注册成 Breeze 保存音色

- 新增 `[tts].ref_audio_path / ref_text / auto_save_voice / voice_name`。
  启动时若填了参考音频且开了开关：先探测服务就绪（最多等 30 秒），再查音色列表——
  `voice_name` 已存在就跳过注册（voices/ 重启自动加载），只把 `voice_id` 对齐它；
  不存在才用 `POST /v1/voices` 注册（参考音频 + 逐字转写 + 名字），成功后
  `voice_id` 自动切换。整个过程 `create_task` 后台跑，不阻塞插件加载。
- 为什么值得：Breeze 首字延迟里最大的一块是参考音频编码（RTX 3060 实测约 548ms），
  注册成保存音色后合成走缓存路径，**首字延迟从约 900ms 降到约 280ms**。
  真机端到端验证：注册 test_voice_tmp（saved=true, 2.0s）→ 用它合成 1.68s 音频
  → 删除。
- 注册流程读作"锦上添花"：列音色/注册失败都只打 warning，合成继续用原 voice_id；
  客户端方法用鸭子类型调用（测试以无包上下文加载 plugin.py 时，
  `from .tts.breeze` 与 `from tts.breeze` 是两个模块对象，isinstance 会误判）。
- 参考音频一次读进内存再交给 FormData：传文件对象的话 aiohttp 发送后不会关，
  容易泄漏句柄。
- 新增 `tests/test_voice_register.py` 5 例（已存在跳过+同步、不存在注册+同步、
  无参考音频 no-op、失败保原音色、关开关/服务不可达 no-op），
  `tests/test_breeze.py` 补 4 例（注册请求字段、非法音色名、缺文件/缺转写、列音色）。

## 未发布：TTS 后端换成 Breeze TTS 2，移除 GPT-SoVITS

- 删掉 GPT-SoVITS（gsv.cpp）整条链路：`tts/gpt_sovits.py`、插件里 `_tts_params()`
  与 `GPTSovitsClient/GPTSovitsParams/GPTSovitsError` 引用、`[tts]` 段里的
  `ref_audio_path/prompt_text/text_split_method/speed_factor/top_k/top_p/media_type/
  sample_steps/stream/repetition_penalty/cfg_rate/dbcache/voice/token` 等字段。
- 新增 `tts/breeze.py`：按 Breeze server HTTP API 实现，`POST /v1/audio/speech`
  流式回传裸 PCM。输出 24kHz s16le 单声道（不是 WAV，无 44 字节头），采样率读
  响应头 `X-Sample-Rate`；chunk 边界落在样本中间时缓存奇数尾字节到下一块。
  音色改用 `voice_id`（voices/ 里的 .breeze 文件），不再需要参考音频。
- 插件 `[tts]` 段重写为 Breeze 字段：`server/voice_id/instruction/cfg_scale/
  temperature/split_chars/seed` + 原播放相关字段（`output_device/virtual_cable_device/
  no_typing_delay/speech_gate_hold_seconds`）。
- 采样率跟随后端：`STREAM_SAMPLE_RATE` 改 24000，播放器按它初始化；`speak()` 里
  时长换算用动态采样率。异常捕获从 `GPTSovitsError` 换成 `BreezeError`。
- `tts/__init__.py` 改为导出 Breeze；`tests/test_tts.py` 只保留设备枚举与播放测试，
  请求/流式测试移到 `tests/test_breeze.py`（10 例）。
- 真机验证：`GET /health` 返回 24000 Hz，`synthesize("你好呀")` 出 111360 字节
  （≈2.3s 音频），端到端打通。

## 未发布：语音可绕过 Planner 直答（可选）

- 新增 `[asr].bypass_planner`（UI：`语音对话（ASR） → 语音绕过 Planner`，默认关）。
  打开后语音不再走 `channel.submit_voice()` 进聊天流等 Planner，而是直接调
  `maisaka.reply.generate`（宿主复用 Replyer 人格与表达选择、返回文本不发送），
  再走插件自己的 `speak()`。省掉规划轮与工具调用，回答明显更快。
- 代价写在配置 hint 里：她不会真的动手用工具（开网页/玩游戏/查日程），
  那些只有 Planner 路径给得了。弹幕回复、主动对话、定时事件都不受影响。
- 上下文不分裂：主播这句话与她自己的回答都用
  `maisaka.context.append(replyable=False)` 写回，下一轮 Planner 仍看得见，
  但不会反过来触发规划。`reply.generate` 内部是空 chat_history，所以插件
  自己维护最近 6 轮拼成 `reference`，否则她每句话都像失忆重来。
- 这条链路不过 `send_service.before_send`，动作记号要自己摘（否则 `【流泪】`
  会被念出来），表情也要自己触发（`react_expression`）。
- 新增 `[asr].voice_reply_hint`：快速通道的写作要求，留空用「直播口语、一到两句」。
  必须给要求——Replyer 默认风格是「长回复」，不压就会一口气说一大段。
- 新增 `tests/test_voice_fast.py` 5 例：走的是 Replyer 而非 Planner、两侧写回且
  `replyable=False`、记号先摘再播、失败不播报也不污染历史、多轮历史会带进下一轮。

## 未发布：表情时长按台词计划三段，不再查固定秒数

- 旧写法是每个情绪配一个固定 `hold`（委屈 3.5s、惊讶 1.2s），同一情绪无论
  轻描淡写还是哭着喊，演得一样长——这是"不自然"最直接的来源。新增
  `vts/timing.py`：按 onset–apex–offset 三段**计划**，
  依据 FACS Investigator's Guide（Ekman, Friesen & Hager, 2002）、
  Hoffmann et al. (2010) 的 onset 感知真实感区间、Ekman & Friesen (1982)
  的总时长 0.5~4s、Ekman (2009) 的"超过 5s 更像摆拍"。
- 调制链：文献基线 → 强度（感叹号/叠字/程度副词/情绪词）→ 语速（状态型情绪
  把 apex 撑到这句话念完，≤2.5s）→ 习惯化（连着演第 2/3/4 次自然变淡）
  → ±10% 抖动 → 裁到 0.6~4.0s。
- 事件型情绪（惊讶/疑惑/害怕）**不跟着长句拖**，apex 封顶 0.6s；低强度时
  压成一次闪现（`啊？` 0.6s），而不是摆一张脸在那里。
- 淡入淡出跟着 onset/offset 走：`ExpressionState` 存每条表情自己的
  `(fade_in, fade_out)`，取代全表共用的 `fadeTime: 0.22`。
- 参数层回落从 smoothstep 改成指数（Kuppens, Oravecz & Tuerlinckx 2010 的
  DynAffect/OU 过程）：离开峰值掉得快、尾巴拖得长，更像"情绪散掉"。
- `set_expression` / `set_emotion` 新增 `text` 参数，HTTP 端点同步支持
  `?text=`；插件把台词一起传过去，`auto_hold_seconds` 留 0 即自动计划。
- 真机实测（同一情绪两种语气）：委屈 3.08s（强度 0.52）vs 4.00s（强度 0.74）；
  惊讶"啊？" 闪 0.62s。
- 新增 `tests/test_timing.py` 10 例（强度缩放、事件型不被撑长、区间边界、
  闪现、习惯化、fade 与 onset 对齐、指数衰减单调性）。

### 顺带：动作记号提示词可在插件配置里改

- 新增 `[expression].action_prompt`（UI：`表情与道具 → 动作记号提示词`）。
  **字段里预填的就是当前内置全文**，打开配置就能看到模型实际读到的文案，
  直接改字句；清空则回退内置默认。`Field.default` 直接用
  `DEFAULT_PROMPT_TEMPLATE`，不在两处各写一份。
- 用户改的是**说话方式**，动作清单仍由 `{small_actions}` / `{poses}` /
  `{expressions}` / `{props}` 四个占位符从当前皮套的真实资产填入，
  所以换模型后自制文案照样有效。
- 某类动作为空时那一行整行删掉，不会留下「- 小道具：」空标题（否则模型会
  以为这类动作存在、只是自己没记住，开始瞎编）。
- 占位符打错时原样保留（`{expression}` 会显示出来），而不是静默变空。
- 新增 `tests/test_markers.py` 5 例：占位符填充、空类别删行、错写可见、
  **config.toml 与内置模板一致**（防两处漂移）、清空回退默认。
- 提示词字段改成 **14 行文本域**：插件配置的控件类型来自 `x-widget`、行数来自
  `rows`（宿主自带配置用的是 `x-textarea-rows`，两套不一样），漏掉任一就会
  渲染成单行输入框，几百字的提示词根本没法看。`voice_reply_hint` 同理改 4 行。
  补一条 schema 测试防回退。

### 顺带：小动作也能由提示词点名（以 wink 为例）

- `play_tag` 原本只认英文 key，`/tag?name=眨眼` 会静默返回 400，调用方
  分不清是"没这个动作"还是"名字写错了"。新增 `_normalize_tag()`，
  中文别名与英文 key 都能落到同一个片段。
- `wink` 补别名「眨个眼」「抛媚眼」，并给提示词加了一行示例：
  `` `这题我会<眨眼>` 是边说边眨一下眼 ``。
- 实测四种写法（`<wink>` / `<眨眼>` / `<眨个眼>` / `<抛媚眼>`）在真机全部 ok。
  wink 走的是参数注入（`EyeOpenLeft` 单眼闭合 + `FaceAngleZ` 歪头 +
  `MouthSmile` 微笑，约 750ms），**不依赖模型的 .exp3 表情文件**，
  所以换任何皮套都能用。

## 未发布：表情/道具改为「AI 说了算」，关键词只做兜底

统一一条原则：**AI 用台词记号下的决定优先，代码不再替它二次判断**。

- **记号优先于关键词**（最要命的一条）：`action_marker_hook` 是 NORMAL、`auto_speak`
  是 LATE，所以 AI 写完 `【流泪】` 之后，`react_expression` 还会拿关键词把台词再猜一遍
  并切一次表情——后半句有个「哈哈」就把哭换成笑，AI 的决定等于作废。
  现在记号派发时记下点过的表情/道具，10 秒内（一条回复会被拆成多句）跳过关键词推断。
- **表情别名覆盖同义词**：之前词表里 `流泪` 只登记一个别名「委屈」，AI 写 `【哭】`
  会被当普通文字摘掉、什么都不演。现在 `expression_aliases()` 把同一情绪的中文叫法
  一起登记（`流泪` → 委屈/难过/伤心/低落/哭/泪），标成 sad 的泪脸也听得懂 `【哭】`；
  日文别名（泣き/照れ）过滤掉，不给中文提示词添噪。
- **提示词把表情用法说透**：原来只教了 `<点头>` 和 `【兔牙】`，AI 不知道表情也要打记号。
  现在明确写「想哭就写 `【流泪】` 或 `【委屈】`，只在台词里说『呜呜』是不会出现表情的」。
- **道具交给 AI 点**：`auto_props` 默认改为 `false`。自动道具会和 AI 抢戏——它刚收起
  狗尾巴草，调度又掏出竖手指；观众看到的是道具乱闪。`PropScheduler.notify_manual()`
  让 AI 点过道具后自动调度让路 90 秒。想恢复「自己随机演」把配置改回 `true`。
- **管理器不再用进程句柄当门槛**：`if not self.running` 有 8 处，`running` 只表示
  「这个实例自己拉起过引擎」。插件热重载后新实例是 `False`，但引擎还活着，于是表情、
  道具、外观件全部静默返回「演出引擎未运行」。改为 `_available()`（以 HTTP 为准，
  问不到时静默 3 秒，避免引擎真没起时每条弹幕都建连接）。
- **摘记号后补停顿**：`骂我【流泪】我明明很努力` 摘完变成「骂我我明明」，TTS 连读像口吃。
  现在记号夹在正文中间时补一个逗号；后面紧跟「了/着/的/呢」这类助词时直接贴合，
  不会断出「我点头，了」。
- **情绪词表补哭族**：`detect_emotion` 里只有「呜呜」没有「呜」「哭」「眼泪」，
  「眼泪都要下来了」竟然判不出情绪，流泪那张脸永远用不上。补了哭/眼泪/泪目/泪崩/
  抽泣/哽咽/鼻酸/破防/呜/嘤/委屈巴巴 等。
- **弹幕默认是冲她说的**：`detect_reaction` 的 `at_me` 原本要求句子里有「你」，
  于是「好丑啊」「傻逼主播」被判成「观众在吐槽别人」→ 她一脸尴尬地讪笑。
  改为默认指向主播，只有明确说「他/她/别人」才降级；辱骂词补笨/蠢/垃圾/弱智/恶心等。

## 未发布：台词动作记号 【兔牙】/<点头>，让 AI 自己点单演出

- 端到端实测暴露的缺口：兔牙/钢盆/前倾这类**外观件**被正确排除出情绪轮换后，
  就再也没有任何入口——`set_expression` 匹配不到、`set_prop` 又不是成对热键，
  于是「AI 喊一句戴钢盆」也演不了。名字里带「衣服/眼镜」只是显示，不代表不能用。
- 新增 `vts/markers.py`（记号解析 + 提示词生成），按
  [Cortico VTuber World](https://github.com/Pal-AI-Lab/Cortico) 的台本约定：
  `【X】` 阻断式（换外观/掏道具，再说一次收起）、`<X>` 非阻断式（顺手小动作）。
  `<>` 只在命中真实动作时摘（`<3`/`<笑>` 这类手写符号不误伤），`【】` 一律摘；
  摘完收拾「我点头 了」这种中英文夹空格。
- 词表来源：引擎 `/actions` 按**当前模型真实资产**现算（外观件 + 已标定表情 +
  成对道具 + 17 个小动作 nod/shake/tilt/wink…，带中文别名），插件缓存 60 秒后
  拼进 Replyer 的 `extra_prompt`。模型没有的动作不会出现在清单里，也就不会被编出来。
  没落盘成文件：插件目录被宿主 file_watcher 盯着，写文件会让插件反复热重载。
- 出站：新增 `live_stream_action_markers`（`send_service.before_send`，NORMAL，
  比 auto_speak 的 LATE 先跑）——摘记号、`create_task` 派发、回传改写后的消息，
  所以 TTS/字幕/平台发送拿到的都是干净台词；一回合最多派发 3 个。
- 引擎：`set_pose()` / `clear_pose()` / `active_poses()` + `/pose`、`/poses`、`/actions`；
  外观件**不互斥**（兔牙和钢盆可以同时戴）、到期自动收起、换皮时一并复位。
- 管理器：`set_pose()`、`actions()`。
- 测试 **279 passed**（新增 `tests/test_markers.py` 13 例：摘记号/保留 `<3>`/
  提示词分组与截断/别名回查/钩子摘洗派发与 3 个上限；`tests/test_e2e_engine.py`
  加 3 例：外观件戴收与共存、到点自动收、动作词表喂提示词）。

## 未发布：热键/表情改成「读文件判断效果」，名字只做兜底

- 上一版虽然把表情标定换成了参数证据，但**道具与热键仍然靠关键词表**
  （`竖手指`/`狗尾巴草`/`衣服`/`特效`…）。这等于换了一种写死：所有不以中文命名的
  模型（`Anim 3`、`動作1`、`exp07`）整片失效；反过来，名字起得像道具、内容其实是
  换装的热键会被误开。
- 新增 `vts/assetfile.py`：VTS 的 `availableHotkeys` / `expressions` 都带 `file` 字段，
  直接读那个文件拿到与命名无关的客观事实——
  * `.exp3.json` → `Parameters` 的 Cubism 标准参数 + 动到的部位；
  * `.motion3.json` / `.can3.json` → `Meta.Duration`、`Meta.Loop`、`Curves` 的部位；
  * 图片/视频/`.model3.json` → 特效素材或换模型。
  `FileIndex` 按「模型目录 → 根目录」建一次索引（上限 6000 个文件，带缓存），
  支持 `expressions/xxx.exp3.json` 这种带子目录的写法与按文件名兜底查找。
- 道具分类改为**文件优先**（`vts/props.py: classify_asset`）：
  一次性动画 → `gesture`（挂 1.2~2.6s 就收）；循环且只动身体/头发 → `look`（禁用）；
  循环且动脸/带道具 → `toy`；图片视频 → `effect`（禁用）。名字词表退为兜底。
- 表情清单同理：文件只改身体/头发/位置 → 展示型；文件在改脸 → **名字里的「衣服/眼镜」
  不再一票否决**（之前会把 `衣服切换` 类命名误排除，即使它其实在动脸）。
  `header/body` 角度不算「脸上的戏」，只动头部角度的文件不会被标成情绪。
- 引擎 `load_catalog()` 建 `FileIndex`，把表情文件与每个热键的解析结果一起传给
  catalog；`_refresh_props()` 带上 `catalog.hotkey_assets`。日志新增
  「文件已解析 N/M、热键文件 N/M」，`/props` 输出每个候选的 `class`。
- 测试 **258 passed**（新增 `tests/test_assetfile.py` 9 例：动作/表情解析、部位归类、
  子目录查找、名字无意义时按内容分类、名字像换装但内容动脸、模型目录缺失降级）。

## 未发布：表情/道具改成「按模型自标定」，不再靠情绪词查表

- 老问题是**只对中文起名的皮套成立**：`EMOTION_KEYWORDS` 把「无语→黑脸、委屈→流泪」
  写死，日式（照れ／困り／ジト目）、英式（Blush/Angry）、或叫 `exp01.exp3.json`
  的模型全匹配不上，观感就是「AI 情绪很丰富，脸一动不动」。
- `vts/expression.py` 重写为标定流程，三层证据：
  1. **表情文件参数**：从 VTS 模型目录读 `.exp3.json`，把 Cubism 标准参数改动翻成
     FACS **AU 向量**（`ParamMouthForm` +→AU12/−→AU15、`ParamBrowLForm` −→AU4、
     `ParamEyeLSmile`→AU6、`ParamEyeOpen`→AU5/AU7、`ParamCheek`→BLUSH、
     `ParamTear/Shadow/怒符/晕圈` 等自定义参数按名字给伪 AU），再和
     `EMOTION_PROTOTYPES`（EMFACS 六情绪 + 黑脸/冷汗/圈圈眼/屑笑/紧张/哭）做余弦相似度；
     这条路径**与表情叫什么、什么语言无关**。依据：Ekman & Friesen FACS、
     EMFACS AU 组合、ARKit blendshape→AU→余弦分类（Sensors 2026, 26(3):1060）、
     Live2D 官方标准参数语义。
  2. **名字语义**：中/日/英词表 + 颜文字，长词优先（`流冷汗` 先于 `汗`）。
  3. **结构排除**：换装/体型/部件开关/成对开合热键不进情绪池。
- 证据不足（`best < 0.55` 或与第二名差 `< 0.08`）标 `unknown`：不参与轮换、
  绝不瞎给脸；日志逐个打印标定理由，可用 `vts/emotion_assets.json`（支持 `*` 兜底
  与按模型覆盖）手动映射。模型目录非默认路径时用 `VTS_MODEL_DIRS` 追加。
- 道具（`vts/props.py`）从「名字猜可爱」升级为**行为分类**：`toy`（能挂能收）、
  `gesture`（一次性，挂 1.2~2.6s 就收，避免停在最后一帧）、`look`/`effect`
  （换装与背景特效，**直接禁用**——宁可少个道具也不能让 AI 演着演着换衣服）。
  分类用中/日/英词表 + 物品 type + 热键 type，不写死任何皮套。
- 引擎：`load_catalog()` 读参数文件与人工映射、打印标定报告；表情匹配从
  `match_emotion(emotion, catalog.emotion_names)` 改为 `catalog.match(emotion)`；
  `/expressions` 增加 `emotion_map` / `unlabeled`，`/props` 增加每个候选的 `class`。
- 测试 **249 passed**（新增 `tests/test_expression_calibration.py` 26 例：
  纯参数标定 exp01~exp04、非表情参数不贴标签、日英命名、人工映射优先、
  exp3 解析、AU 语义、展示型排除、道具多语言分类与一次性动作时长）。

## 未发布：按日程自己打开网页（playwright MCP）

- 修复主动轮把 prompt 缓存打碎：覆盖规则不再追加到 system（前缀缓存起点），
  改为追加到上下文尾部；主动轮只清洗最后 8 条 Item，更早历史保持原样。
  实测主动轮命中率从 <10% 恢复到接近正常轮次水平。

- 修复「配好日程和 MCP，AI 还是不会自己打开网页看动漫」：日程事件里的
  `focus`（例如「去b站看木几萌」）在 `_on_game_event()` 里被直接丢掉了，
  主动轮 intent 只剩「讲一段抽象搞笑小故事」；日程表虽然注入了 system，
  但没有一句话要求模型**动手用工具**，而 Planner 里浏览器工具是 deferred，
  于是每轮都直接 `stop` / 只说话。
- `_schedule_focus` 保存当前档 focus，随事件更新；`ProactiveDirector.build_intent()`
  新增 `schedule_focus` 参数，日程优先于游戏素材和小故事，明确要求
  「先用浏览器工具打开网页、再读页面内容、然后开口」，不许凭空编、不许直接 stop。
- `proactive_schedule_rule()`：主动轮尾部规则同步要求动手用工具；
  `planner_tool_filter_hook` 在有日程安排时用它替换「讲小故事」规则。
- 事件端口不再依赖 `game_events`：关掉游戏素材时也监听 `5687`，否则日程事件
  根本没有入口（`feed is None` 时旧实现直接 return，把整条链路掐断）。
- 只走 playwright MCP 浏览器工具（`tool_search` → `browser_navigate` →
  `browser_snapshot` 读页面），**不依赖任何刷 B 站插件**（该插件已从 plugins/ 删除）。
  真机发现模型会一直 `browser_navigate` 却从不读页面（`browser_snapshot` 0 次），
  于是解说内容仍是屏幕画面而不是网页；已在 intent 与尾部规则里点名
  「打开后必须再读一遍页面，别拿别的画面凑」。
- 真机验证：16:08~16:11 日志出现 `tool_search` 发现的 `browser_navigate` 等 7 个
  MCP 工具，模型自行导航到 `search.bilibili.com/all?keyword=木几萌` 与 B 站视频页；
  日程 intent 与尾部规则均出现在 Planner 快照中。
- 顺带修 `@HookHandler("send_service.after_build_message")` 挂错位置：之前被
  误加在内部方法 `_learn_live_stream_from_outbound()` 上，宿主调用时传
  `hook_name` 触发「unexpected keyword argument 'hook_name'」刷屏，而真正要生效的
  `typing_delay_hook` 反而没注册（断句打字等待会恢复 0.3 秒/字）。已把装饰器
  移回 `typing_delay_hook`，helper 保持普通方法，出站学习流 ID 的逻辑不变。
- 全套测试 192 passed（新增 7 例：日程意图、日程优先、动手规则、focus 保留、
  feed 缺席、不引用已删插件、hook 注册位置）。

## 未发布：日程换挡接入事件流

- `GameEventInbox` 新增专用端点 `POST :5687/schedule-event`：收到后给 payload
  打上 `kind="schedule"` 再走同一条事件回调链，不必伪装成游戏事件。
- `GameFeed.note_external_event()`：只把 `version` +1，不覆盖 `_payload`。
  这样日程换挡不会把正在解说的游戏素材冲掉，同时「播完且有待讲的新事件」
  的门控仍认得这次事件 —— 换挡时若正在播报被「等说完再想」挡下，播完后
  会自动补触发，不会把这一档吞掉。
- `_on_game_event()` 识别 `kind="schedule"`：不写素材，直接
  `_schedule_proactive("日程换挡")`。开口、TTS、字幕、上下文隔离全部复用
  既有主动对话链路，没有新增任何定时器。
- 配套 `schedule-hub` 换挡时推事件；日程只负责说「有事发生了」。
- 全套测试 185 passed（新增 6 例）。

## 未发布：开局没有弹幕时也能找到直播聊天流

- 纯事件驱动暴露出一个残留问题：流 ID 只从入站弹幕的
  `chat.receive.after_process` 里「学」。插件刚重启、直播间还没来弹幕时集合是空的，
  而第一个触发点往往就是游戏事件 —— 于是真机日志每两秒刷一次
  「还没有已知的直播聊天流，先不开口」，主动对话永远起不来。
- 现在 `_maybe_speak_proactively()` 改调新方法 `_resolve_live_stream_id()`，三级兜底：
  1. 已学到的最近直播流（最快，不打扰宿主）；
  2. `[channel].group_id` 里配置的固定流 ID；
  3. 向宿主 `ctx.chat.get_all_streams(platform="live")` 查会话表，优先按
     `group_name = 直播间弹幕` 精确匹配，名字缺失时取该平台最后一个流。
  找到后写回 `_live_stream_ids`，后续工具过滤 / 表情禁用 / 上下文隔离都能识别它。
- 查询用 `asyncio.wait_for(..., STREAM_LOOKUP_TIMEOUT=3s)` 限时：IPC 一旦挂住，
  否则 `_schedule_proactive` 会以为「上一轮还没跑完」，把后面所有事件静默丢掉。
  读 `config` / `ctx` 改走底层字段，未注入时安静降级，不会打崩事件回调。
- 真机还暴露了两点：
  - `chat.get_all_streams` 是**需要 manifest 授权**的能力，原先没声明，宿主直接回
    `[E_CAPABILITY_DENIED]`；已在 `_manifest.json` 的 `capabilities` 补上。但
    runner 热重载不会重新授权，所以又加了不依赖任何能力的兜底：
    `_learn_live_stream_from_outbound()`，从 `send_service.before_send` /
    `after_build_message` 的 `stream_id` + 平台学习真实直播流（AI 每说一次话就补一次），
    弹幕源关掉、插件刚重启也够用。真实 ID 形如 `6617d187df454930b77b6bacbf0f03af`。
  - 15:01:55 WebUI 把本插件切成禁用（`config.toml [plugin] enabled=false`），
    插件卸载后 5687 关闭、宿主报「platform=live 未命中任何发送路由」；已按用户确认恢复启用。
    另 14:5x~15:09 有外部进程反复写 `live-sight` 目录文件，触发宿主 `file_watcher`
    整组重载，把 `GameFeed` 状态清空、5687 反复重建。
- **真机验证通过**（15:12~15:15）：game-hub 每 2 秒推一步事件 → 5687 收到 →
  `GameFeed.version` 变化 → 触发开口 →
  `[主动对话] 从宿主会话表找到直播聊天流：6617d187...` →
  `[主动对话] 已请求 Maisaka 自言自语（本轮上下文已隔离直播间消息，游戏素材 有，task=proactive:local.live-danmaku-adapter:...）` → TTS 自动播报成功。
  日志里同时有「上一轮还在生成，先不叠加新任务（事件：游戏推进）」，
  证明节拍来自游戏事件而不是「播完自循环」或定时器。
- 全量测试 **179 passed**（新增 8 例：三级兜底、名字匹配、平台兜底、查询异常、
  查询超时、出站学习、非直播平台不误学）。

## 未发布：主动对话彻底事件驱动，播完不再自动续跑

- 之前虽然删了 `_proactive_loop` 的轮询，但留了两条「说完就接」的路径：
  `_mark_speech_finished()` 无条件 `_schedule_proactive("播完")`，以及
  `_perform_reply` finally 里再 `create_task(_maybe_speak_proactively())`。
  两条叠在一起，实际效果是「音频多长、话就多密」，仍然是个自循环节拍器。
- 现在：
  - 新增 `_has_pending_activity()`：只有 `game_feed.version > _consumed_game_version`
    才算「有新东西可讲」；
  - `_mark_speech_finished()` 只在有新事件时才排开口，否则打日志
    「播完，没有新事件，保持安静」；
  - 删掉 `_perform_reply` 里的无条件续跑，播完只负责解门控、清除字幕；
  - 讲完一次就把版本号消费掉（`_consumed_game_version`），同一批进度不会重复说。
- 效果：没玩游戏、也没新事件时主动对话保持安静；游戏在推进时才边玩边说。
- 插件全量测试 **171 passed**（新增 3 例：无事件安静 / 有事件继续 / 消费后不重复）。

## 未发布：字幕改成跟着语音逐句推进

- 之前整段话在首包到达时一次性推给字幕页面，看起来就是「一下全显示」。
- 现在：
  - 新增 `split_subtitle_sentences()`：按句末标点切句，过长再按逗号拆；
  - `play_stream()` 新增 `on_progress` 回调，每写一块 PCM 报告已推进的音频秒数；
  - 新增 `_drive_subtitle()`：用「已播秒数 ÷ 每字秒数」定位当前说到第几句，
    只推那一句；播完若还差就把最后一句补出来；
  - 每段播完用真实音频时长校准 `_subtitle_seconds_per_char`（滑动平均），
    语速变化后不会越跑越偏。
- 真机采样确认字幕按 3~5 秒一句推进，与语音同步；新增
  `tests/test_subtitle_sync.py`（6 例），全量 **168 passed**。

## 未发布：四个游戏工具提到常驻

- `game_play` / `game_list` / `game_status` / `game_stop` 声明
  `visibility="visible"`，不再只躺在 deferred 池里等 `tool_search`。
- 真机 Planner 工具列表从 7 个变成 11 个，`game_play` 等直接可见，
  AI 可以自己开局。

## 未发布：修「主动轮 reply 全都发送失败」

- 真机回执：`reply` 返回 `未找到要回复的目标消息，msg_id=maxeeau / none / molv2rf`，
  宿主日志只写「回复工具未生成可见消息，将继续下一轮循环」——看起来像模型没说话，
  其实话已经生成，卡在**发送目标**上。
- 根因：宿主 `reply` 工具强制要求 `msg_id` 能在 `_chat_history` 里找到
  （`find_source_message_by_id` 只认 `SessionBackedMessage`）。主动轮没有
  「要回复的观众消息」，模型就会抓一条已被裁剪的旧 id，或干脆填 `none`。
  主动任务的 `<plugin_proactive_task>` 属于 `InternalContextMessage`，
  拿它的 task_id 当锚点同样查不到。
- 修法：`maisaka.planner.before_request` 记录本轮上下文里**最后一条真实消息**的
  `msg_id`（`last_context_message_id`），`maisaka.planner.after_response` 里
  `retarget_proactive_reply()` 把主动轮 `reply` 的目标换成它。
  只改 `msg_id`，`reply_reference` 等参数原样保留；不指向任何要回应的对象。
- 验证：真机日志出现「reply 目标已改成本轮上下文锚点（锚点=mnsusoh）」后，
  紧接着连续 5 条「自动播报成功」；插件全量测试 **162 passed**。

## 未发布：主动对话改成纯事件驱动 + 游戏解说素材

- **删掉轮询定时器**：`_proactive_loop`（每 `tick_seconds` 睡一次再判断）整个移除。
  现在只在事件到来时判断要不要开口：
  - 整段 TTS 播完（`_mark_speech_finished`）；
  - game-hub 推进一步（`POST /game-event`）；
  - 游戏开局 / 重置。
  `tick_seconds` 保留字段仅为兼容旧配置，UI 标为「已废弃」，不再生效。
- **新增游戏事件通道**（`game_events.py`）：
  - `GameEventInbox`：只听 `127.0.0.1` 的小 HTTP 服务，默认端口 **5687**，
    `POST /game-event` 收事件、`GET /health` 探活；
  - `GameFeed`：保存最近一次事件，`update()` 只在**真有变化**（步数/结束/换游戏）
    时返回 True，避免同一帧重复推送触发多次开口；
  - `summary()` 只交代一句场景（在玩什么、什么难度），**不报进度/步数/技巧名**，
    也不透传网页上那句话：数字一进上下文就会被照着念成数据播报，听起来像提词器。
- **意图不再给解说角度**：有游戏时只补一句「你正在直播玩「数独」（中等）。」，
  删掉「解说这盘 / 聊点别的，两条路选一条」这类指导话术。
- **game-hub 侧**：`SudokuGame` 每填一格（AI 与观众都算）调用 `_emit_event()`，
  由 `GameHub._push_game_event()` POST 给直播适配器；新增
  `[games].event_webhook`（默认 `http://127.0.0.1:5687/game-event`，留空不推）。
  推送失败只记 debug，不影响游戏本身。
- 验证：插件全量测试 **159 passed**；真机日志确认
  「已启用（纯事件驱动，无定时器）」「[游戏事件] 已监听 :5687」，
  主动任务日志带「游戏素材 有」，Planner 上下文里出现
  「你现在正在直播玩「数独」（简单，进度 12.2%，已走 5 步…）」，
  输出仍是 `['reply', 'stop']`。

## 未发布：整段 TTS 播完前不让 Planner 被触发（从源头收口）

- 真机日志（23:19:32~23:19:46）复现：TTS 还在播时，宿主在 **23:19:33 / 23:19:36**
  又发了两次 Planner 请求。原先的 `chat.receive.before_process` 门控明明 abort 了
  期间的入站弹幕，却完全没挡住 —— 因为这两轮不是新消息唤醒的，而是
  `reasoning_engine.run_loop` 在 `reply` 工具执行后无条件 `continue` 出来的
  **同一 turn 内部「回合 3/16、4/16」**（`MAX_INTERNAL_ROUNDS = 16` 是宿主硬编码
  常量，不能从插件改）。这同时也是之前「上下文 3 秒 +310 token 连涨 25 次、
  对话堆、越回越慢」的元凶。
- 修复思路按用户要求：**不拦截、不挂起 Planner 请求，而是让它压根不被触发**。
  - 新增 `maisaka.planner.after_response` 钩子 `live_stream_end_turn_after_reply`：
    直播流的 Planner 输出里出现 `reply` 时，在末尾追加一个合法的 `stop` 工具调用
    （`append_stop_tool_call()`：唯一 item_id/call_id、沿用本轮 logical_turn_id、
    ISO 时间戳，已用真机快照跑过宿主 `deserialize_prompt_items` 的 MODEL_OUTPUT
    协议校验通过）。`reply` 先执行、`stop` 后执行，其返回值带
    `pause_execution=True`，宿主直接 `break` 出内部循环并进入「等待新消息」。
  - 只补 `stop` 不补 `wait`：`wait` 会挂 `_schedule_wait_timeout` 定时唤醒，
    TTS 期间仍可能超时触发新 Planner；`stop` 不设任何定时器。
    输出里本来就有 `stop`/`wait`、或没有 `reply` 时一律不追加。
  - `SELF_ONLY_ROUND_RULE` 从「直接 `wait`」改为「直接 `stop`，且不要调用
    `wait`」，避免空转轮自己挂定时唤醒。
  - 新配置 `[reply].end_turn_after_reply = true`（UI「说完即收口（不续跑 Planner）」），
    可一键回到宿主默认行为；`modified_kwargs` 按宿主「整体替换」语义回传完整 kwargs。
- 之后只有**新弹幕 / 主播语音 / 主动自言自语任务**才会开启新 turn；播放期间的
  新入站消息仍由原门控在 `SessionMessage.process()` 之前 abort，主动对话巡检用
  `_planner_blocked()` 等整段播完才投递，两者都不受影响。
- 全量测试 **149 passed**（新增 7 例：配置、纯函数、协议校验、钩子、开关/边界）。
  改动文件：`plugin.py`、`config.toml`、`README.md`、`CHANGELOG.md`、
  `tests/test_speech_gate.py`、`tests/test_proactive_isolation.py`。宿主源码未改。

## 未发布：修「AI 回复自己某句主动对话说的话」

- 真机快照（23:03:33 / 23:03:38）复现：宿主 `append_sent_message_to_chat_history()`
  把机器人发出的分段短句同步进历史并调度新一轮规划；`reply_target_message` 取
  「历史里最后一条非通知消息」，必然指向自己刚说的那句。上下文里这些消息带
  `is_self_message="true"`，模型看不懂，就当成观众发言去接话、反问、补刀：
  「我已急哭」→「谁家好人搓火球把自己蓝搓干净还问怎么放不出来啊」。
  有一轮上下文 12 个 Item 里**只有 3 条自己的短句**，却照样生成了回复。
- 修复（纯插件 Hook，宿主源码未改）：
  - `annotate_self_messages()`：所有直播轮次给 `is_self_message="true"` 的历史
    消息加显式标签，只改文本 part、保留原文与 item_id（协议校验安全），
    让 AI 记得讲过什么、又不会把它当别人发的话。
  - Planner 尾部注入 `SELF_ECHO_RULE`（高优先级「自己发言识别规则」）：
    禁止复述/反问/评价/补刀；规则放尾部而不是 system，避免打碎前缀缓存。
    上下文全是自己消息、且没有主动任务时，再注入 `SELF_ONLY_ROUND_RULE`
    要求直接收口（后改为 `stop`，见上一节）；主动轮改用 `PROACTIVE_FRESH_RULE`
    要求讲新段子。
  - Replyer 两层同样加锁：`extra_prompt` 追加规则、`items` 标注自身消息。
  - 出站兜底：`_self_echo_suppress_until` 在空转轮续期，模型万一仍吐字，
    `send_service.before_send` 跳过该文本的 TTS 与字幕（**不 abort 文字发送**，
    避免触发上一版发现的 `send_results success=false` 重试风暴）；下一轮只要有
    观众消息或主动任务就立即清零，弹幕回复 / 主动对话 / 主播语音都不误伤。
  - 顺带修：`items_have_active_proactive_task()` 在活跃任务表为空时保守返回
    True，会误把空转轮判成主动轮 —— 空转判定改为只看 `proactive` 状态。
- 测试：`tests/test_proactive_isolation.py` 新增 8 例（标注幂等、空转判定、
  尾部规则、wait 规则、主动轮不误伤、出站抑制与解除等），全量 **142 passed**。
- 注意：WebUI 保存「主动对话」配置会把 `min_interval_seconds` 写回旧值 4；
  新语义下这会让每句话多等 4 秒，配置文件已改回 0 并加了提示注释。

## 未发布：拆开「说完后停顿」与「扫描粒度」两个重叠参数

- 现象：WebUI 里「最小间隔（秒）」和「巡检间隔（秒）」看起来都在管
  「两句话之间隔多久」，实际两者都不管。真机日志里主动开口间隔是 47~61 秒，
  由整段 TTS 播完 + 门控决定；`min_interval_seconds=4` 从**发起任务**时刻起算
  （一轮远大于 4 秒，永远拦不住），`tick_seconds=1` 只是后台 sleep 粒度，
  而播完时 `_perform_reply` 会直接触发下一轮、根本不走巡检。
- 修复（纯插件）：
  - `min_interval_seconds` 改名为「说完后停顿（秒）」，语义改成**从整段播完
    起算**的可选留白；新增 `_mark_speech_finished()`，由播报 finally 在
    `_speech_pending` 归零时打点，触发任务时不再 `mark_spoken()`。默认值改为 0
    （说完就接，行为不变），想要句间留白填 2~4 才真正生效。
  - `tick_seconds` 改名为「扫描粒度（秒）」，UI/注释明确它不是发言间隔；
    `inflight_seconds` 标注为「仅异常兜底」。四个参数的分工写进 config.toml。
  - `[reply].recheck_seconds` 属于弹幕频率倍率校验，与主动对话计时无关，
    特此区分，不再混叫「巡检」。
- 测试：`test_proactive_isolation.py` 增加冷却起点回归（触发不起算、播完才起算）。

## 未发布：整段 LLM 回复播完前不给 Planner 空隙

- 根因：宿主开启 `response_splitter` 后，一条回复拆成多条短句；分段消息带
  `typing=True`，`send_service._prepare_message_for_platform_io` 会按
  `calculate_typing_time()`（中文 0.3 秒/字 + 0.3 秒回车）`await asyncio.sleep`。
  一条 20 字短句要等 6 秒多，比这句 TTS 音频还长。第一句播完时第二句尚未
  发出，`_speech_pending` 归零，播放门控短暂打开，弹幕与主动对话插进来堆对话。
- 修复（纯插件 Hook，未改宿主源码）：
  - 新增 `send_service.after_build_message` 钩子 `live_stream_no_typing_delay`：
    该钩子在 typing sleep **之前**触发且 `allow_kwargs_mutation=True`，把直播流
    分段的 `typing` 改写为 `False`。真机日志实测两条分段到达间隔 **31ms**。
  - 新增 `[tts].no_typing_delay`（默认 true）与 `[tts].speech_gate_hold_seconds`
    （默认 2.0）；后者是分批发送兜底窗口，每条短句入队续期、最后一句播完
    立即清零，不会在正常播完后额外拖延。
  - `_planner_blocked()` = `_speech_active() or now < _speech_gate_until`，同时
    供 `chat.receive.before_process` 的 abort 和主动对话巡检使用；主动对话本身
    也是一次 Planner 轮次，分批空窗期同样不许插队。
  - `on_load` / `on_unload` 重置并取消排队播报任务（热重载不留旧台词）；
    `_asr_press` 打断时清门控与 pending，保证主播语音必回。
- 顺带修一个真实 bug：宿主 `HookDispatcher` 对 `modified_kwargs` 是**整体替换**
  而非合并，插件里 4 个钩子原先只回传单个字段，会把 `items` / `session_id` 等
  参数丢掉（导致 Planner/Replyer 的弹幕上下文清洗实际未生效）。改为回传完整
  kwargs 再覆盖目标字段，真机日志已出现「Planner / Replyer 上下文已清洗」。
- 测试：新增 `tests/test_speech_gate.py` 19 例；同步更新
  `test_correction_fragment.py`（宿主断句现已开启）与
  `test_proactive_isolation.py`（完整 kwargs、隔离秒数不再硬编码）。
  插件套件 **132 passed**，TOML/JSON/语法校验通过。
- 生效：插件改动热重载即可；宿主 `bot_config.toml` 的 `[response_splitter]`
  需重启 MaiBot。生效后日志关键词：`取消分段模拟打字等待`、`播报中，暂缓这条
  入站消息`、`上一整段还没播完，等说完再想`。

## 未发布：模型自带表情的时长改成真人区间

- 问题：`.exp3.json` 表情默认挂 **12 秒**。文献里"摆拍表情"的典型特征恰恰是
  「突然出现、保持过久」——Ekman & Friesen (1982) 真实微笑总时长 500~4000ms，
  Frank et al. (1993) 自发社交微笑平均 3~4 秒，普通面部表情 0.5~6 秒。
- `emotion_pose.HOLD_SECONDS`：每个情绪一个总时长——惊讶 1.2s、疑惑/轻蔑 2.2s、
  尴尬/生气/紧张 2.4s、无语/害怕/得意 2.6s、害羞 3.0s、开心 3.2s、委屈 3.5s
  （全部落在真人 0.5~4s 区间）。
- `emotion_pose.OFFSET_SECONDS`：退场比上场慢（惊讶 0.35s、委屈 0.9s）；
  `decayed()` 增加按秒回落（release），不再固定"最后 30%"。
- `set_expression()` 默认用情绪的时长（传 0/None），超过 8 秒会被压到 8；
  表情文件淡入用 `rise`、到期淡出用 `offset`（原来固定 0.22s）。
- 配置：`auto_hold_seconds` 与 `react_hold_seconds` 默认改为 **0 = 按情绪自动**。
- 真机实测（读 `/expressions` 的 active 时间线）：惊讶 **1.24s**、无语 **2.62s**、
  委屈 **3.56s**，与预期一致（含 0.25s 巡检粒度）。
- `tests/test_emotion_pose.py` 新增时长区间断言，全套 **212 passed**。

## 未发布：观众的话也体现在她脸上（入站反应表情）

- 原有缺口：表情只跟"她自己说出来的回复"绑定（出站 hook），观众说了什么她脸上没反应；
  TTS 关掉时连回复表情都不会触发。
- 新增 `vts/expression.py::detect_reaction()`：把观众一句话映射成"人的反应"，而不是复刻情绪——
  观众在难过 → 共情（委屈）；冲她说「讨厌你 / 闭嘴 / 丑」 → 委屈（受伤）；
  说「我讨厌她」（第三人称）→ 尴尬（讪笑吃瓜）；观众发火（气死 / 烦死）→ 紧张；
  被夸 / 表白 → 害羞；观众自己先无语 → 无语；发问 → 疑惑；普通闲聊不加戏。
- `core/channel.py` 新增入站回调 `set_inbound_hook`：消息进聊天流后回调一次，
  协程走 create_task 不阻塞注入，回调异常只记日志。
- 插件注册 `_react_to_chat`，走纯参数 `set_emotion`（不切 .exp3 表情文件，刷屏也不会闪）。
- 节流：`[expression] react_min_interval`（默认 2s，0=不限）+ 同一情绪重复再压一档；
  `react_hold_seconds`（默认 4s）；`react_to_chat` 开关默认开。
- `tests/test_reaction.py` 新增 8 例（含「我讨厌她→尴尬、我讨厌你→委屈」），全套 **211 passed**。
- 真机实测（大肥鱼 + VTS）：六句话逐句触发后读 Live2D 参数——
  「我讨厌你」「我好难过」嘴角 −1.03、眼睛 −0.8（半闭低头）；「气死我了」嘴角 −0.23、眼睛 +0.27；
  「你好可爱」嘴角 +0.58；「太离谱了」嘴角 −1.02；「我讨厌她」嘴角 ~0、眼睛 +0.45、视线躲开。
- 顺手修抽搐自检误报：眨眼改为弹簧后覆盖后，眼睑一帧闭合被当成抽搐（322 次超限）。
  `_JITTER_LIMITS` 移除 EyeOpen，真机 2220 帧 **0 次超限**（worst 0.97，FaceAngleZ）。

## 未发布：表情的时序与不对称（按论文补"人味"）

- 依据：
  - Hoffmann et al. (2010)《Perceived realism of dynamic facial expressions》给出
    最真实的 onset 时长：悲伤 860~1120ms（最慢）、惊讶 500~530ms 与恐惧 480~650ms
    （最快）、愤怒/厌恶/快乐 670~890ms。原来所有情绪统一 0.18 秒淡入，等于全都不真实。
  - 表情不是整张脸同时到位：惊讶的 AU 出现时间分散（眉/眼/嘴各有先后）；
    自发表情还存在左半脸先动、幅度略强的不对称（posed vs spontaneous 研究）。
- 实现：
  - `emotion_pose.RISE_SECONDS`：每个情绪自己的上场时长（惊讶 0.50、害怕 0.55、
    紧张 0.60、尴尬 0.75、开心 0.80、无语 0.85、轻蔑/害羞 0.90、委屈 1.05）。
    `pose_for()` 返回时自动带上 `rise`，不改原配方表。
  - `performance_engine.FACE_DELAY`：眼睛先到、嘴角晚 60ms、右眼比左眼再晚 40ms；
    每个通道按 `(age - delay) / rise` 各自计算 smoothstep，整张脸不再"一帧变脸"。
  - 左脸通道 +3%、右脸 −3%：肉眼几乎察觉不到，但脸不再是镜像对称。
- 仿真（惊讶/无语/委屈）：惊讶 0.25s 眼睛到 52%、嘴角 0%；0.50s 眼睛到位、嘴角刚开始；
  无语 0.50s 到 65%，委屈 0.50s 才 48%（1.05s 才满）。左右眼幅度比 1.06~1.08。
- `tests/test_emotion_pose.py` 新增时序/延迟/不对称断言，情绪测试 **10 passed**。

## 未发布：眨眼闭严 + 启动不再自校准

- 眨眼闭不严的根因：眼睑量在输出弹簧（3Hz 低通）里走，闭合被拖到 0.02 左右，
  看着就是"眯了一下"。现在眨眼**在弹簧之后覆盖**，包络在 30%~46% 留一段完全闭合
  平台（60fps 采样一定能落到 0）；尴尬的急眨时长 0.12~0.16s，给 VTS 输入平滑留时间。
  仿真：尴尬 6 秒 EyeOpenLeft 最低 **0.000**，完全闭合帧 13 帧。
- 启动校准探测默认关闭（`[performance] startup_probe = false`）：
  以前启动时逐通道推到极限实测 6~7 秒，画面像在校准抽搐，而且探测期间控制接口还没开，
  插件会误报"进程已启动但控制接口无响应"。现在日志里 `VTS connected → control API`
  只用了 **0.36 秒**，启动即受控。需要时打开（探测幅度减半、单通道 0.18s，更温和）。
- `engine/manager.py` 增加 `startup_probe` 参数并透传 `--probe`；`PerformanceConfig`
  与 `config.toml` 同步新增该开关。

## 未发布：情绪驱动的眨眼与视线节奏（更拟人）

- 眨眼不再是一套全局随机节奏，改成情绪说了算（`emotion_pose.py` 新增 `blink` 字段）：
  - `slow` 无语 / 轻蔑：慢眨 0.26~0.42s，22% 概率闭眼停一拍（0.55~0.85s）——
    就是"嗯……"那种出神；轻蔑还配 0.65 的视线倍率，看定一处不东张西望。
  - `fast` 尴尬 / 紧张：单次 0.09~0.13s，45% 概率紧接着再眨，形成 2~3 下连眨；
    尴尬的视线倍率给到 2.2（眼神乱瞟），是最直白的窘迫信号。
  - `none` 害怕：瞪眼几乎不眨（6~9.5s 一次），视线倍率 0.6（吓到定住不敢乱看）。
  - 没有情绪时回到原来的随机节奏（3~6 秒一次、偶发二次眨眼）。
- 尴尬单独配了 `face`：嘴角抿紧 −0.10、眼睛略睁，配后缩与低头；
  不再是"低强度害怕"那张脸。
- 情绪结束自动复位眨眼风格与视线倍率（`compose` 里按 `_emotion_weight` 归零）。
- 仿真 30 秒：基线 17 次眨眼、尴尬 48 次、害怕 11 次；慢眨闭合时长 0.18s
  对常态 0.08s（最长 0.43s 的"闭眼停一拍"）。
- `tests/test_emotion_pose.py` 新增眨眼/视线风格断言，情绪测试 **9 passed**。

## 未发布：按 FACS / EMFACS 的 AU 组合重配情绪脸

- 依据（都落实到通道，不是凭感觉调数值）：
  - Ekman & Friesen 的 FACS / EMFACS 原型 AU 组合：恐惧 AU1+2+4+5+20+25/26、
    惊讶 AU1+2+5+26、愤怒 AU4+5+7+23、厌恶 AU9(+15/16)、悲伤 AU1+4+15、快乐 AU6+12。
  - MPEG-4 FAP 六种基本表情的分区描述（ISO/IEC 14496）：恐惧=眉上扬靠拢 + 眼紧张警觉；
    惊讶=眉上扬 + 上眼睑全开 + 下颌张开；厌恶=上唇上提；快乐=嘴角向耳侧拉开。
  - ARKit blendshape→AU 映射（Aldenhoven et al., Sensors 2026, Table 1）：
    browDown=AU4、eyeWide=AU5、eyeSquint=AU7、mouthSmile=AU12、mouthFrown=AU15、
    mouthStretch=AU20、mouthPucker=AU23、jawOpen=AU26。
- 新增配方字段 `face`：直接给输入空间的面部增量，优先于 cortico sustain clip，
  可按 AU 组合逐通道配比；老配方仍走 `sustain`，不受影响。
- 重配三个情绪（含同组的惊讶）：
  - 无语 = 轻蔑/厌恶低强度（AU15 + AU7）：嘴角 -0.45、眯眼 -0.12；不再依赖整条 angry clip。
  - 紧张 = 弱恐惧（AU1+2+4+5+20/23）：睁眼 +0.09、嘴角 -0.20（抿紧）、抖 0.5。
  - 害怕 = 满强度恐惧：睁眼 +0.15（顶满）、嘴角只 -0.04（恐惧不是哭），
    幅度放在后缩 / 视线躲 / 抖 0.85。
  - 惊讶 = AU1+2+5+26：睁眼顶满、嘴角保持中性。
- 配套（`performance_engine.py`）：常态睁眼从 1.7 收到 1.45，给 AU5 留出可见余量；
  情绪期间 idle 摆头收敛 50%（避免 2~4° 的情绪体态被 ±20° 的待机摆动淹没）；
  `tremble` 抖动叠在输出弹簧之后（3Hz 低通会把高频抖衰减九成），用 2~5Hz、1~2°。
- 真机（大肥鱼，`/emotion` 纯参数）：嘴角 无语 -0.88 / 紧张 -0.40 / 害怕 -0.17，
  眼睛 无语 -0.16（眯）/ 紧张 +0.36 / 害怕 +0.45（瞪满），三个情绪的脸已经能分开。
- 已知限制：该模型的眉毛（AU1/2/4）与张嘴（AU26）没有映射，配方保留这两个 AU，
  换到映射完整的模型才看得见；抖动受 VTS 输入平滑限制，只能做到"轻微坐立不安"。
- `tests/test_emotion_pose.py` 新增 AU 落点方向性断言，情绪测试 **8 passed**。

## 未发布：情绪真正落到脸上（有机演出引擎）

- 现象：情绪只靠回复文本命中 → 切 `.exp3.json`。模型没有同款表情文件时
  `set_expression()` 直接返回，`apply_emotion_pose()` 根本没被调用，只剩身体在演；
  即便有表情文件，参数层的眉毛/嘴角也被折半，和 idle 噪声同量级，脸上看不出差别。
- 修法（`engine/performance_engine.py`）：
  - `set_expression()` 先无条件应用情绪姿态，再切表情文件；匹配不到时返回
    `"参数表情"`，不再整段放弃。
  - 新增 `_emotion_face_layer()`：情绪的脸部偏置单独计时（跟着 hold 挂满），
    入段 0.18s smoothstep 淡入、尾段沿用 `decayed()` 平滑回落，不再"到点消失"。
  - 新增 `FACE_GAINS`：眉毛全额、嘴角 0.9、眼睑/脸颊全额，情绪在脸上必须看得出来；
    手势脉冲沿用原半幅（`SEM_GAINS`），不改变既有手感。
  - 新增 `_blend_face()` 统一叠加：补上原来被静默丢弃的 `MouthOpen` 与
    `EyeLeftX/Y`、`EyeRightX/Y` —— cortico 的 eyeroll / look_around / eyes_wide
    以前在脸上等于没发生。
  - `clear_expression()` 连情绪的面部/身体/视线偏置一起收起，不会出现"表情没了脸还委屈"。
- `/status` 新增 `emotion` / `emotion_face`，便于确认当前情绪与生效的面部通道。
- 真机（大肥鱼）：`/expression?name=惊讶&hold=8` → 圈圈眼 + 眉毛拉满 + 张嘴；
  8 秒后表情与脸一起平滑收起；抖动自检 0 次超限。
- `tests/test_emotion_pose.py` 新增「无表情文件也能演到脸上」，插件套件 **112 passed**。

## 未发布：主动对话与弹幕完全解耦

- 根因：主动开口虽绕过动态门控，但仍和弹幕回复共用同一个 Planner / Replyer，
  两者都读完整聊天历史（全是弹幕原文）。只把 intent 改成「不用管弹幕」无效，
  模型仍会去接最近一条弹幕。
- 清素材：`proactive.py` 删除 `LiveEvent` / `record_message` / `record_self` /
  `recent_events` 与全部弹幕正则，`activity_hook` 不再给主动对话记事件；
  `build_intent()` 不出现「弹幕」二字，也不再提示 passive 模式下不可用的
  `reply` 工具（`experimental.passive_replyer = true` 时该工具不存在）。
- 三层隔离（纯插件 Hook，未改宿主源码）：
  - `maisaka.planner.before_request`：`strip_live_context_items()` 清洗主动轮
    Context Items，只改文本 part、不删消息，保留 `item_id`/`meta` 与 function call
    锚点；已用宿主 `deserialize_prompt_items` 实测通过协议校验。
  - `maisaka.replyer.before_request`：追加 `PROACTIVE_REPLY_HINT`，锁住最终字句。
  - `maisaka.replyer.before_model_request`：Replyer 上下文同样清洗，并对 system
    prompt 里写死的读弹幕规则追加 `PROACTIVE_SYSTEM_OVERRIDE` 覆盖规则。
- **真机快照复盘（关键）**：第一版只隐藏 `[B站弹幕]` 前缀，日志虽打印
  「已隐藏 11 条」，AI 播报仍在念「命名为1说…」。读 `logs/maisaka_prompt/*/
  live_group_live-all/*.json` 才发现上下文里还有五种形态：`【弹幕回复规则】
  回复弹幕时先读一遍弹幕`（User Item 与 Replyer 的 System Item 各一份）、
  `【上下文状态摘要】…向弹幕要一个角落`、Planner 历史的「弹幕读到…发言指导」、
  Replyer 的 `弹幕：xxx 回应 xxx` 指导块、含弹幕的句式提示块。现按类型分别
  替换为占位 / 自言自语规则 / 中性发言指导，发言指导按行删掉弹幕句、保留
  自言自语点子。复验：Planner 与 Replyer 快照清洗后**非 system 条目含「弹幕」
  的数量均降为 0**，system 覆盖规则生效，协议反序列化通过。
- 轮次识别从「消息先后顺序」改为 **`task_id` 精确匹配**：`trigger_proactive`
  返回 `task_id`，宿主会写进 `<plugin_proactive_task id="...">`；主动轮带当前 id，
  后续弹幕回复轮只带旧 id。原先的顺序判据会把「主动任务后紧跟一条弹幕」
  误判成弹幕回复轮而放过隔离，现已修正。`_proactive_isolation` 出站即解除，
  兜底超时由新增 `[proactive].isolation_seconds`（默认 60s）控制，原
  `event_window_seconds` 已删除。
- `tests/test_proactive_isolation.py` 共 17 例（意图、事件流移除、各类直播前缀、
  规则/摘要/发言指导/system 覆盖清洗、幂等、Item 结构与入参不变性、task_id
  判据、隔离窗口/别名/过期/解除、三个 Hook、残留隔离自愈、配置）；插件套件
  **111 passed**。

## 未发布：弹幕回复节流（让自言自语占多数）

- 现象：`app_20261105_200220.log.jsonl` 里每批弹幕都是
  `回复调度 频率=1.000 动态门控: 概率=0.46 阈值=0.00 保留比例=1.00 判定=进入Planner`。
  宿主全局 `chat.reply_timing.talk_value = 1` 会让动态回复门控直接短路
  （`normalized_frequency >= 1.0 → keep_ratio=1.0 → threshold=0`），
  加上人格改成话痨，结果几乎每条弹幕都被回复，主动对话被挤掉。
- 修法：新增 `ReplyConfig` / `[reply]` 配置段，插件通过宿主公开的
  `frequency.get_adjust` + `frequency.set_adjust` 能力，**只给直播聊天流**
  下发回复频率倍率（默认 `0.3`）；不改宿主源码、不改全局 `talk_value`、
  不影响本地聊天室。弹幕照常注入上下文与弹幕墙，只是不每一批都进 Planner。
- 不受影响的两条链路（都绕过动态门控）：
  - 主动对话 `maisaka.proactive.trigger` → `enqueue_proactive_task()` 直接排内部轮次；
  - 主播 ASR `is_mentioned=1.0` → 宿主 `@ 强制触发`，必回。
- 自愈：`heartflow_manager` 重建聊天流会把倍率重置为 1.0，所以加了独立后台巡检
  （`recheck_seconds`，默认 10s）读回校验、不一致就补设；新学到直播流或配置热更新
  时标脏立刻执行。设置后用二次 `get_adjust` 确认，流还没建好不缓存假成功。
  卸载/关闭节流把倍率还原成 1.0，能力异常只警告一次并静默降级。
- 权限：`_manifest.json` 声明 `frequency.get_adjust` / `frequency.set_adjust`；
  manifest 变更需要热重载插件，若日志出现「未获授权能力」则完全重启 MaiBot。
- `tests/test_reply_frequency.py` 新增 12 例（倍率换算、首次下发、已一致跳过、
  宿主重置自愈、recheck 节流、关闭还原、能力失败降级、假成功重试、多流独立、
  卸载还原、旧配置回退、新流标脏）；插件套件 **94 passed**。
  `_reply_config()` 对「配置尚未注入 / 旧 schema 无 reply 段」回退默认值，
  避免热重载时因读配置抛错导致插件加载失败。

## 未发布：按 gsv.cpp 官方 API 重构 TTS 客户端

- 依据：https://github.com/KakaruHayate/gsv.cpp/blob/main/docs/api.md
- **推翻旧结论**：原 `gpt_sovits.py` 写死 `SERVER_STREAMING_SUPPORTED = False`，
  注释称「这个版本 stream:true 只返回 44 字节 WAV 头、不发 PCM」，因此整段合成完
  才开播。直连实测证伪：官方流式先发 44B 头（0.01s），随后逐段 flush PCM。
  | 文本 | 流式首 PCM | 流式收完 | 非流式整段就绪 |
  |---|---|---|---|
  | 30 字 | 0.87~0.93s | 2.43s | 1.83s |
  | 88 字 | 0.78~0.85s | 8.1~12.3s | 10.31s |
  长句首字延迟改善一个数量级，直接解决「文字出来了但很久才听到声音」。
- `synthesize_stream()` 改为真流式：chunked 读取、跳过 44B WAV 头、按 16KB 产块，
  配合既有 `AudioPlayer.play_stream()` 边收边播；`synthesize()` 保留整段（stream:false）。
  新增**首包看门狗**：老引擎只发头时超时抛 `GPTSovitsStreamUnsupported`，
  在尚未出声前自动回退整段合成（已出声则不回退，避免重复播放）。
- 参数对齐官方：`voice` / `cfg_rate` / `seed` / `repetition_penalty` / `dbcache` /
  `stream` 可配（空值/负值不下发，用服务端默认）；保留 `sample_steps=4`、
  `skip_first_step=1`、`text_split_method=cut5`。文本 <= 4000 字节与字段范围本地校验。
- 错误按官方 `{"code","message"}` 解析并带出错误码（403/503 可定位）；支持
  `--token` 的 Bearer 鉴权；每次请求带 `X-Request-Id`；`/health` 解析
  `ok`/`engine`/`voice`/`sidecar`/`queue`。
- `plugin.py::speak()` 打断时显式 `await source.aclose()`，避免 chunked 响应与
  合成锁悬挂导致下次播报卡住。
- 配置新增 9 项（stream / 首包超时 / seed / repetition_penalty / cfg_rate /
  dbcache / voice / token），`config.toml [tts]` 与 `TtsConfig` 键完全一致（29 个键，
  无未知/缺失），`_tts_params()` → payload 全链路已验证。
- `tests/test_tts.py` 假服务支持官方 chunked 流式，新增 7 例（官方字段、参数校验、
  4000 字节、JSON 错误码、流式+鉴权、关闭流式、只发头回退）；套件 **82 passed**。
- 权衡（已写进 README）：流式**不做 peak 归一化**（音量可能略变）、流式用比 cut5
  更细的句子级切分、绕过 AR 批处理时总吞吐可能更低；要严格 cut5 可关 `stream`。
- 注：真机复测时用户的 gsv_server 已退出（由 GAG API 页带 sidecar 启动），
  本次以离线假服务全链路 + 重构前直连实测数据验证；引擎重启后可直接热重载使用。

## 未发布：TTS 速度口径澄清 + 开启「跳过首部」

- 用户反馈「RTF 还是高，正常应 0.2」。用引擎真实日志 + 对照实验钉死口径：
  - 插件真实链路（4 步 / cut5 / skip_first_step=1）：25 字 **2.12s**，音频
    4.84s → 端到端 **RTF 0.44**；其中 `synth_ms=890ms` → **synth-only RTF 0.18**。
  - 即 GUI/基准里的「0.2」只算 DiT 去噪，**不含 AR 自回归**（每句固定约
    1.0s）。端到端必然在 0.4 左右，不是配置没生效。
  - `_bench_real.py` 原样（cut1 / top_k=5 / all_zh / rep=1.35 / seed=42 /
    skip=0）实测 25 字 RTF 0.42、80 字 0.40，**同样没跑出 0.2**。
  - cut1↔cut5、top_k 5↔15、zh↔all_zh、两条参考音频：速度差都在 0.01~0.02，
    均非瓶颈。此前 17~27s / RTF 0.9~1.2 的样本里音频从 8s 拖长到 22~24s
    （坏音色、引擎运行时重载参考音频），本次未复现，属偶发状态。
- `skip_first_step` A/B（同 payload 各 2 次）：0 → 0.40/0.41，1 → 0.38/0.39，
  约 5~7% 提升。已正式开启：`gpt_sovits.py` 字段 + payload（上一步被中断的
  半截改动已补完），`plugin.py` 的 `TtsConfig.skip_first_step` 与
  `_tts_params` 接线，`config.toml [tts] skip_first_step = 1`。
- 修正 `sample_steps` 描述里误导的「RTF 约 0.2」。
- `tests/test_tts.py`：默认值/build_payload 断言 + 新增
  `test_speed_params_reach_server`（验证 4 与 1 真的到达 /tts 请求体、
  热更新可改）；完整套件 **75 passed**。

## 未发布：ASR 语音对话（按住热键直接和 AI 说话）

- **模型测速**（SiliconFlow `/v1/audio/transcriptions`，恩骨语音库 3 条 × 2 轮）：
  `Qwen/Qwen3-ASR-1.7B` 最快（平均 **0.52s**，短句 0.43~0.70s，识别质量好）；
  XingChenASR-Diarize-V3.0 0.85s 但输出带「1: 」说话人前缀；SenseVoiceSmall 1.35s
  且有抖动（单次 4.36s）；XingChenASR-V3.2-Ultra 最慢 2.43s。默认模型取
  Qwen3-ASR-1.7B。脚本：`tests/asr_benchmark.py`。
- 新增 `asr.py`：`SiliconFlowASR`（aiohttp 异步转写）+ `MicRecorder`
  （sounddevice 16k 单声道按住录音）+ `AsrHotkey`（keyboard 全局热键，
  回调从 keyboard 线程投递回插件事件循环）。
- 交互：**按住热键（默认 F9）说话，松开转写并发送**。她正在播报时按下热键
  会先打断 TTS（合成流提前收尾、收尾等待可打断）再录，残响缓冲 0.25s，
  避免把 AI 的声音录进识别。
- 注入路径：`LiveChannel.submit_voice()` 直通入队（绕过防刷屏去重与事件开关，
  主播的话不该被吞），与弹幕共用同一注入队列保序；消息前缀 `[语音弹幕] 昵称: ...`。
  弹幕墙同步显示主播的话；AI 回复自动走 TTS/字幕/表情链路。
- 配置段 `[asr]`：enabled/api_key/base_url/model/hotkey/user_name/device/
  min_duration/max_duration。`user_name` 可填本地聊天室同一个昵称——人格与
  长期记忆全局共享，AI 会认得你；但直播流与本地聊天室的短期上下文相互独立。
- 依赖：requirements.txt 与 manifest 增加 `keyboard`。
- 顺手修复：宿主 `bot_config.toml` 的 `[response_splitter] enable` 被写回 true
  （WebUI 保存覆盖），改回 false。
- 新增 `tests/test_asr.py`（4 例）；完整套件 **69 passed**。真机冒烟：aiohttp
  客户端转写实测 0.43s / 0.47s。
- **主播的话不上弹幕墙**：`activity_hook` 识别 `live_asr` 标记跳过 `_push_danmaku`，
  事件流（主动对话兜底）照常记录。
- **主播的话必回复**：`LiveEvent.mention=1.0` → 消息 `additional_config` 写入
  数值型 `is_mentioned=1.0`——宿主 `is_mentioned_bot_in_message` 对 >=1.0 的
  数值提及直接强制下一轮 Planner 触发，不依赖任何回复概率配置。普通弹幕不受影响。
- config.toml 修复：[asr] 段被手动编辑与自动生成叠成两份，TOML 解析报
  `Key "asr" already exists`；合并为一份（昵称保留用户设置的「超人不会飞」）。
- **直播流禁用表情包（纯插件层）**：双层实现，全部走宿主自带 hook——
  1. **工具不暴露**（用户要求的本意）：`maisaka.planner.before_request`
     （allow_kwargs_mutation）在直播流的规划请求前把 `send_emoji` /
     `show_emoji_list` 从 `tool_definitions` 里移除，模型根本看不到这两个工具；
     直播流 session_id 由插件从自己注入的直播消息里动态学习（不猜格式），
     另按 live-all / 自定义 group_id / bilibili: douyin: 前缀兜底。
  2. **兜底中止**：`emoji.maisaka.before_select` 保留，万一表情工具仍被
     调用（如子代理路径）则中止并说明原因。
  其它平台放行，本地聊天室不受影响。曾短暂改过宿主
  `src/maisaka/chat_loop_service.py`，已按要求完整回滚。
- 新增 `tests/test_tool_filter.py`（4 例）；完整套件 **74 passed**。
- **主动对话只在直播适配器的聊天流生效**：`_maybe_speak_proactively` 不再用
  「最近任意平台消息的聊天流」（此前在本地聊天室说过话就会把主动开口带过去），
  改用 `_latest_live_stream_id()`——从直播消息里学到的最近活跃直播流；
  没有直播流就不开口。事件流 `record_message` 也只记直播平台消息，
  主动开口的 intent 不再引用本地聊天室内容。
- **移除主动对话的话题库/话题引导**：`proactive.py::build_intent` 原来写死了
  「结合正在做的事说」「别问空话」「没内容就吐槽/聊搞笑话题」等一整套话题模板，
  现已删掉，只保留两件事——带上最近事件作参考上下文、以及「这一轮必须说话
  （别 stop/wait）」这个触发机制。说什么完全交给 AI 自己（人格 + 上下文）决定。
  `trigger_proactive` 的 reason 也改成中性的「主动开口」。

## 未发布：弹幕墙不显示礼物价格

- `danmaku.py::parse_inbound`：礼物描述末尾的「，¥0.1」、SC 前缀里的金额、
  SC 的 `SC ¥30` 小标签全部不显示；`live_price` 不再参与展示层渲染。
- 只改展示层：`core/mapping.py::format_event_text` 生成、发给 LLM 的聊天流文本
  **仍保留金额**，AI 判断礼物轻重的依据不变。
- 两道兜底：`DanmakuServer.push()` 清洗任何来源（含 POST /api/danmaku、旧状态）
  的礼物类条目；页面 JS 在渲染时再剥一次金额，连到旧服务实例时也不会漏出价格。
- 普通弹幕里观众自己打的价格文字是弹幕内容，原样保留。
- 新增/更新 `tests/test_danmaku.py` 回归（含金额剥离、普通弹幕保留、页面模板转义）。
  完整套件 65 passed。

## 未发布：修复「自己引用自己、只回一个字」的纠正碎片

现场：聊天流里出现一条正文只有「叫」、并引用自己上一条消息的机器人回复，
同时上一句里的「叫」被打成了「教」，一条完整回复还被拆成两条发送。

根因在宿主 `config/bot_config.toml` 的回复后处理，与弹幕链路无关：

- `[chinese_typo] enable=true` 把「叫」随机替换成「教」；
- `enable_correction_quote=true` + `correction_quote_probability=1` 让
  `src/chat/utils/utils.py::_apply_response_post_processing` 额外追加一个
  `ProcessedResponseSegment("叫", quote_previous=True, is_correction=True)`，
  由 reply.py 作为独立消息发出并引用自己上一条（`send_api_*`）；
- `[response_splitter] enable=true` 又把一条回复切成多条。

修法：

- `config/bot_config.toml`：`[chinese_typo] enable=false`、
  `enable_correction_quote=false`、`correction_quote_probability=0`；
  `[response_splitter] enable=false`。直播场景整段一次发出，TTS 一次合成、
  字幕一次显示，不再有错字与单字纠正消息。
- `plugin.py` 兜底：`auto_speak_hook` 通过 `_is_correction_fragment()` 识别
  「1~2 字 + reply_to 指向 `send_api_*`」的碎片，跳过 TTS 与字幕（文字消息
  照常发出，不干预宿主）。防止以后配置被改回时再次污染直播画面。
- 新增 `tests/test_correction_fragment.py`（4 例）。61 passed。
- 实测 `process_llm_response_segments()` 对现场原文返回 **1 段完整原文**，
  不再拆分、不再错字、不再追加纠正段。

## 1.2.0

### 合并

- **并入原 live-stream-companion 全部能力**：本插件现在是「输入 + 输出」全链路
  一体化插件，一个目录承载：
  - **TTS 播报**（GPT-SoVITS）：机器人回复自动合成语音，播到 VTS 监听的虚拟声卡；
  - **VTS 有机演出引擎**（engine/）：子进程常驻，让模型一直在动，按语境触发表情/道具；
  - **主动对话**（proactive）：任何时间都可主动开口，交给 Maisaka 按上下文接话；
  - **字幕层**（http://127.0.0.1:5679/）与 **弹幕墙**（http://127.0.0.1:5686/），
    给 OBS 浏览器源用，字幕与语音严格同步。
  - 相应配置段 `[tts] [vts] [performance] [expression] [proactive] [subtitle] [danmaku]`
    已并入 config.toml；manifest 补充了 `llm.generate` / `maisaka.proactive.trigger`
    等 capabilities 与 aiohttp/websockets/numpy/sounddevice 等依赖。
- 原 `live-stream-companion` 目录随之废弃删除（已备份）。

### 修复

- **B 站礼物价格单位错误**：`GiftMessage.price` 是「瓜子数」（1000 金瓜子 = 1 元），
  之前直接当元用，导致灯牌（100 瓜子）显示成 ¥100。现在礼物价格统一 `/1000`
  换算成元，与上舰、醒目留言（本身即人民币元）口径一致。

## 1.1.1

### 变更

- **「AI 回复（未发往直播间）」这句日志误导**：出站网关按设计就不向平台回发弹幕
  （风控 / 频率 / B 站登录态），回复只写进聊天流给 TTS 和字幕层用。
  原来每条回复都打一条 info，还带「未发往」这种像失败的字眼。
  现在改成：首次出站打一条说明（`出站方向：AI 回复不回发弹幕…`），
  之后每条只记 debug（`AI 回复已写入聊天流：…`）。

## 1.1.0

### 变更

- **进场、点赞不再进聊天流**：这两类互动通知没有可聊的内容，只会让 Bot
  挨个欢迎人、感谢点赞。现在从链路源头就不产出：
  - B 站删掉 `_on_interact_word_v2`（`INTERACT_WORD_V2` 的进场/关注/分享）；
    `LIKE_INFO_V3_*` 本来就没有处理器；
  - 抖音删掉 `_on_member` 与 `WebcastMemberMessage` 的绑定；`WebcastLikeMessage`
    依旧未绑定；
  - `KIND_MEMBER` 常量、`format_event_text` 的进场分支、`[events] member`
    开关一起删除（开关已经没意义）。
- 保留的事件类型：弹幕、醒目留言、礼物、上舰、下播。

## 1.0.0

### 主要功能

- 新增直播弹幕适配器，同时接入 B 站与抖音直播间。
- 两个平台的弹幕事件归一化后经同一个 `live` 消息网关注入 MaiBot 聊天流。
- 支持弹幕、醒目留言、礼物、上舰、进场事件，可按类型单独开关。
- 提供合并通道与分房通道两种会话组织方式。
- 新增 `/live_status` 命令查看链路连接与注入统计。

### 细节

- 内置 blivedm 1.1.7 作为 B 站链路，复用其断线重连、心跳与 WBI 签名。
- 抖音链路自实现 webcast 推送通道：ttwid 获取、roomId 解析、握手签名、protobuf 帧解析与 ack 回包。
- 抖音握手签名脚本内置在 `assets/douyin_sign.js`，由 mini-racer 执行。
- 弹幕防刷屏：同内容窗口去重、方括号指令弹幕丢弃、超长截断。
- 修复 B 站礼物连击被误判重复入站的问题：blivedm 把一次连击拆成多条消息，平台侧 ID 重复会被 Host 去重丢弃，改为给同类事件追加链路自增序号。
- 配置页所有字段补齐中文显示名，不再是英文键名。
- README 记录抖音礼物需要登录态 Cookie 才能收到。
- 移除点赞事件接入：抖音 WebcastLikeMessage 不再进入聊天流。
- 抖音帧解压兼容 gzip/zlib/brotli/明文，非 gzip 帧不再打断连接。
- 修正抖音礼物价格换算（diamondCount 为抖币，10 抖币 = 1 元）。
- 抖音礼物事件 ID 追加链路自增序号，连击礼物不再被 Host 去重误伤。