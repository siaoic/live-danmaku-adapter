# -*- coding: utf-8 -*-
"""台词动作记号：解析、提示词生成、出站钩子摘记号并派发。

三条契约：
  1. ``【兔牙】``／``<点头>`` 必须能从台词里摘干净（TTS 和字幕不能念出括号）；
  2. ``<3``、``<笑>`` 这种手写符号不能被误摘；
  3. 摘出来的记号要按**当前模型的真实动作表**落到 pose/tag/expression/prop 上。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tomllib
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR))

from vts.markers import (  # noqa: E402
    DEFAULT_PROMPT_TEMPLATE,
    MAX_ACTIONS_PER_REPLY,
    action_kind_of,
    build_action_prompt,
    canonical_action_of,
    direction_instruction_of,
    split_actions,
)

ACTIONS = [
    {"key": "兔牙", "kind": "pose", "aliases": []},
    {"key": "钢盆", "kind": "pose", "aliases": []},
    {"key": "黑脸", "kind": "expression", "aliases": ["无语"]},
    {"key": "狗尾巴草", "kind": "prop", "aliases": []},
    {"key": "nod", "kind": "tag", "aliases": ["点头"]},
    {"key": "shake", "kind": "tag", "aliases": ["摇头"]},
]


# --------------------------------------------------------------------- #
# 1. 解析
# --------------------------------------------------------------------- #
def test_blocking_marker_stripped_from_line() -> None:
    clean, markers = split_actions("【兔牙】好看吗", {"兔牙"})
    assert clean == "好看吗"
    assert [m.name for m in markers] == ["兔牙"]
    assert markers[0].blocking is True


def test_inline_marker_needs_known_name() -> None:
    """``<>`` 只有命中真实动作才摘，否则可能是 <3 / <笑> 这类手写符号。"""

    clean, markers = split_actions("我点头<点头>了", {"点头"})
    assert clean == "我点头了"
    assert [m.name for m in markers] == ["点头"]
    assert markers[0].blocking is False

    keep, none = split_actions("给你比个<3", {"点头"})
    assert keep == "给你比个<3" and none == []


def test_unknown_blocking_marker_still_stripped() -> None:
    """中文正文几乎不用【】，出现就当记号摘掉，不能让它被念出来。"""

    clean, markers = split_actions("【不存在】算了", {"兔牙"})
    assert clean == "算了"
    assert [m.name for m in markers] == ["不存在"]


def test_multiple_markers_and_space_cleanup() -> None:
    text = "【兔牙】 你说 <点头> 这个怎么样？<摇头>"
    clean, markers = split_actions(text, {"兔牙", "点头", "摇头"})
    assert [m.name for m in markers] == ["兔牙", "点头", "摇头"]
    assert "【" not in clean and "<" not in clean
    assert clean.startswith("你说")
    assert "  " not in clean


def test_empty_and_plain_text() -> None:
    assert split_actions("") == ("", [])
    clean, markers = split_actions("今天天气不错", {"点头"})
    assert clean == "今天天气不错" and markers == []


# --------------------------------------------------------------------- #
# 2. 词表 -> 提示词
# --------------------------------------------------------------------- #
def test_prompt_groups_by_kind_and_mentions_syntax() -> None:
    prompt = build_action_prompt(ACTIONS)
    assert "外观/姿势" in prompt and "小动作" in prompt
    assert "兔牙" in prompt and "点头" in prompt and "狗尾巴草" in prompt
    assert "<点头>" in prompt and "【兔牙】" in prompt
    assert "不要编" in prompt


def test_custom_template_fills_placeholders() -> None:
    """配置里改成自己的文案后，动作清单仍由当前模型自动填。"""

    custom = "小动作：{small_actions}\n表情：{expressions}"
    text = build_action_prompt(ACTIONS, template=custom)
    assert "nod（点头）" in text
    assert "看向镜头" in text, "视线动作是内置的，任何模板都要带上"
    assert "无语" in text, "表情清单没有被填进自定义模板"
    assert "兔牙" not in text, "模板里没写 {poses}，就不该出现外观件"


def test_empty_category_drops_its_line() -> None:
    """小皮套没有道具时，不能留下「道具：」这种空标题。

    留着空标题，模型会以为这类动作存在、只是自己没记住，然后开始瞎编。
    """

    only_tags = [{"key": "nod", "kind": "tag", "aliases": ["点头"]}]
    text = build_action_prompt(only_tags, template="小动作：{small_actions}\n道具：{props}")
    # 视线动作内置在小动作组最前面；nod 依然在列表里
    assert "小动作：看向" in text
    assert "nod" in text
    assert "道具" not in text


def test_unknown_placeholder_is_visible() -> None:
    """占位符打错时要看得见，而不是被静默换成空。"""

    text = build_action_prompt(ACTIONS, template="表情：{expression}（少了个 s）")
    assert "{expression}" in text


def test_config_ships_with_builtin_prompt() -> None:
    """配置里预填的就是内置模板，UI 打开能看到全文而不是一个空框。

    这条同时防漂移：以后改了 DEFAULT_PROMPT_TEMPLATE 却忘了同步 config.toml，
    界面上会显示旧文案，用户改完还以为生效了。
    """

    raw = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    shipped = str(raw["performance"]["action_prompt"] or "")
    assert shipped.strip(), "配置里的提示词是空的，UI 会显示空框"
    assert shipped.strip() == DEFAULT_PROMPT_TEMPLATE.strip(), (
        "config.toml 的 action_prompt 与内置模板不一致")


def test_clearing_prompt_falls_back_to_builtin() -> None:
    """用户在 UI 里清空后要回退到内置默认，而不是变成「不说话也不演」。"""

    text = build_action_prompt(ACTIONS, template="")
    assert "【动作记号】" in text
    assert "nod（点头）" in text
    assert "看向弹幕" in text


def test_prompt_field_renders_as_tall_textarea() -> None:
    """提示词有几百字，必须渲染成多行框——单行输入框根本没法编辑。

    插件配置的控件类型来自 ``x-widget``、行数来自 ``rows``（不是宿主配置用的
    ``x-textarea-rows``），漏掉任何一个就会悄悄退化成单行框。
    """

    from maibot_sdk.config import generate_plugin_config_schema

    schema = generate_plugin_config_schema(plugin.LiveDanmakuConfig)
    found = {}

    def walk(node):
        if isinstance(node, dict):
            name = node.get("name")
            if name in ("action_prompt", "voice_reply_hint"):
                found[name] = node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    prompt = found.get("action_prompt") or {}
    assert prompt.get("ui_type") == "textarea", "提示词退化成单行框了"
    assert int(prompt.get("rows") or 0) >= 10, "提示词框太矮，看不全"
    assert len(str(prompt.get("default") or "")) > 200, "默认值没带全文"


def test_prompt_empty_when_no_actions() -> None:
    """模型没有任何动作资产时，提示词只剩视线目标——视线不依赖皮套资产。

    参考 cortico-world-vtuber：视线（gaze）是独立通道，词表永远包含"看向…"。
    """

    text = build_action_prompt([])
    assert "看向镜头" in text and "看向弹幕" in text
    # 清单里只有视线（模板示例文字里提到【兔牙】，所以用清单格式断言）
    assert "小动作：看向" in text
    assert "（兔牙）" not in text and "nod（" not in text
    assert build_action_prompt([{"key": "", "kind": "pose"}]) == text


def test_prompt_truncates_long_lists() -> None:
    many = [{"key": f"外观{i}", "kind": "pose", "aliases": []} for i in range(40)]
    prompt = build_action_prompt(many, max_items=5)
    assert "外观0" in prompt and "外观9" not in prompt
    assert "等 40 个" in prompt


def test_lookup_handles_aliases() -> None:
    assert action_kind_of("点头", ACTIONS) == "tag"
    assert canonical_action_of("点头", ACTIONS) == "nod"
    assert action_kind_of("无语", ACTIONS) == "expression"
    assert canonical_action_of("无语", ACTIONS) == "黑脸"
    assert action_kind_of("不存在", ACTIONS) == ""


# --------------------------------------------------------------------- #
# 3. 出站钩子：摘记号 + 派发
# --------------------------------------------------------------------- #
_MODULE_NAME = "live_danmaku_adapter_markers_test"
_spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
plugin = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = plugin
_spec.loader.exec_module(plugin)

SID = "live-all"


def _adapter() -> "plugin.LiveDanmakuAdapter":
    data = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    adapter = plugin.LiveDanmakuAdapter()
    adapter._plugin_config_instance = plugin.LiveDanmakuConfig.model_validate(data)
    adapter._live_stream_ids[SID] = None
    adapter._actions_snapshot = lambda: {"model": "测试皮套", "actions": ACTIONS}  # type: ignore[method-assign]
    return adapter


class _FakeEngine:
    running = True

    def __init__(self) -> None:
        self.calls: list = []

    async def set_pose(self, name, hold_seconds: float = 45.0):
        self.calls.append(("pose", name))
        return {"ok": True, "name": f"戴上 {name}"}

    async def set_expression(self, emotion, hold_seconds: float = 0.0, text: str = ""):
        self.calls.append(("expression", emotion))
        return {"ok": True, "name": emotion}

    async def set_emotion(self, emotion, hold_seconds: float = 0.0, text: str = ""):
        self.calls.append(("emotion", emotion))
        return True

    async def set_prop(self, prop, hold_seconds: float = 60.0):
        self.calls.append(("prop", prop))
        return {"ok": True, "name": prop}

    async def play_tag(self, tag, intensity: float = 1.0):
        self.calls.append(("tag", tag))
        return True

    async def set_gaze(self, x: float, y: float):
        self.calls.append(("gaze", x, y))
        return True


def test_hook_strips_markers_and_dispatches() -> None:
    adapter = _adapter()
    engine = _FakeEngine()
    adapter._engine = engine  # type: ignore[assignment]
    text = "【兔牙】这样是不是好看多了<点头>？"
    message = {
        "platform": "live",
        "processed_plain_text": text,
        "raw_message": [{"type": "text", "data": text}],
    }

    async def _run():
        result = await adapter.action_marker_hook(message=message, stream_id=SID)
        for _ in range(10):                     # 让派发任务跑完
            await asyncio.sleep(0)
        return result

    result = asyncio.run(_run())
    assert result is not None, "有记号时必须回传改写后的消息"
    cleaned = result["modified_kwargs"]["message"]
    assert cleaned["processed_plain_text"] == "这样是不是好看多了？"
    assert cleaned["raw_message"][0]["data"] == "这样是不是好看多了？"
    assert engine.calls == [("pose", "兔牙"), ("tag", "nod")]


def test_marker_expression_beats_keyword_guess() -> None:
    """AI 写了【流泪】，就不能因为后半句有个「哈哈」又被判成开心。

    这是最隐蔽的一条：记号钩子是 NORMAL、播报钩子是 LATE，AI 选好的表情
    会被后跑的关键词推断重新猜一遍盖掉——写了哭却给出笑脸。
    """

    adapter = _adapter()
    actions = [{"key": "流泪", "kind": "expression", "aliases": ["哭", "委屈"]}]
    adapter._actions_snapshot = lambda: {"model": "测试皮套", "actions": actions}  # type: ignore[method-assign]
    engine = _FakeEngine()
    adapter._engine = engine  # type: ignore[assignment]

    _, markers = split_actions("我好难过【流泪】才怪哈哈", {"流泪", "哭", "委屈"})
    assert [str(m) for m in markers] == ["【流泪】"]
    adapter._remember_ai_actions(markers)

    applied = asyncio.run(adapter.react_expression("哈哈哈哈笑死我了"))
    assert applied == "流泪", "记号选的表情应原样保留"
    assert engine.calls == [], "关键词层不该再切一次表情"


def test_keyword_guess_still_works_without_marker() -> None:
    """AI 没写记号时，关键词兜底照旧生效（不能为了记号优先把兜底废掉）。"""

    adapter = _adapter()
    engine = _FakeEngine()
    adapter._engine = engine  # type: ignore[assignment]
    applied = asyncio.run(adapter.react_expression("呜……眼泪都要下来了"))
    assert applied == "委屈"
    assert engine.calls and engine.calls[0][0] == "expression"


def test_hook_noop_without_markers() -> None:
    adapter = _adapter()
    adapter._engine = _FakeEngine()  # type: ignore[assignment]
    message = {"platform": "live", "processed_plain_text": "今天好困", "raw_message": [
        {"type": "text", "data": "今天好困"}]}
    assert asyncio.run(adapter.action_marker_hook(message=message, stream_id=SID)) is None


def test_hook_strips_markers_even_with_empty_vocabulary() -> None:
    """模型词表为空时：台词照常摘干净，未知动作静默跳过、视线照常执行。

    记号永远不该漏给观众（旧行为在词表空时会把【兔牙】原样打出去）。
    视线目标是内置的，与模型词表无关。
    """

    adapter = _adapter()
    adapter._actions_snapshot = lambda: {"actions": []}  # type: ignore[method-assign]
    engine = _FakeEngine()
    adapter._engine = engine  # type: ignore[assignment]
    message = {"platform": "live", "processed_plain_text": "【兔牙】好看吗<看向弹幕>", "raw_message": [
        {"type": "text", "data": "【兔牙】好看吗<看向弹幕>"}]}

    async def scenario():
        result = await adapter.action_marker_hook(message=message, stream_id=SID)
        # 派发是 create_task：等 live-actions 跑完；视线回正任务（4~6s sleep）
        # 对本测试无意义，直接取消避免挂住。
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task() and task.get_name() != "live-actions":
                task.cancel()
        pending = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and t.get_name() == "live-actions"
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return result

    result = asyncio.run(scenario())
    assert result is not None, "视线记号永远可用，钩子不该整体 noop"
    cleaned = result["modified_kwargs"]["message"]["processed_plain_text"]
    assert "兔牙" not in cleaned and "好看吗" in cleaned
    gaze_calls = [c for c in engine.calls if c[0] == "gaze"]
    assert gaze_calls == [("gaze", 0.75, -0.15)], f"视线没派发或数值不对: {engine.calls}"
    assert not any(c[0] in ("pose", "expression", "prop", "tag") for c in engine.calls), \
        "空词表时不应派发模型资产动作"


def test_hook_caps_actions_per_reply() -> None:
    adapter = _adapter()
    engine = _FakeEngine()
    adapter._engine = engine  # type: ignore[assignment]
    text = "".join("<点头>" for _ in range(8))
    message = {"platform": "live", "processed_plain_text": text, "raw_message": [
        {"type": "text", "data": text}]}

    async def _run():
        await adapter.action_marker_hook(message=message, stream_id=SID)
        for _ in range(10):
            await asyncio.sleep(0)

    asyncio.run(_run())
    assert len(engine.calls) == MAX_ACTIONS_PER_REPLY


# --------------------------------------------------------------------- #
# 组合记号与场景重置（参考 cortico-world-vtuber）
# --------------------------------------------------------------------- #
def test_combined_markers_split_on_comma() -> None:
    """【微笑,看向屏幕】拆成两个独立记号（cortico 台本写法）。"""

    clean, markers = split_actions("这波打得漂亮【微笑,看向屏幕】再来一局", set())
    assert [m.name for m in markers] == ["微笑", "看向屏幕"]
    # 夹在两个中文之间：摘掉后自动补一个逗号防止连读
    assert clean == "这波打得漂亮，再来一局"


def test_combined_inline_markers_strip_unknown_too() -> None:
    """<> 组合无条件摘（与【】对称）：词表外子名不执行，但绝不能念出来。

    端到端实测：AI 会编造 <双手合十在胸前，用力点头> 这类词表外记号，
    保留原文的话 TTS 会把"双手合十在胸前"念出来——事故级别。
    """

    known = {"点头", "眨眼", "看向弹幕"}
    clean, markers = split_actions("嘿<点头,眨眼>", known)
    assert [m.name for m in markers] == ["点头", "眨眼"]
    # 「挥手」不在池里：照样摘（观众不该听到"挥手"两个字），执行层再跳过
    clean2, markers2 = split_actions("嘿<点头,挥手>", known)
    assert [m.name for m in markers2] == ["点头", "挥手"]
    assert "挥手" not in clean2 and "嘿" in clean2
    # 中文逗号也支持
    clean3, markers3 = split_actions("【微笑，看向镜头】", set())
    assert [m.name for m in markers3] == ["微笑", "看向镜头"]


def test_reset_marker_is_builtin() -> None:
    """【重置】是内置动作：不依赖模型词表，kind=reset。"""

    assert action_kind_of("重置", []) == "reset"
    assert action_kind_of("Reset", []) == "reset"
    assert action_kind_of("复位", []) == "reset"
    assert canonical_action_of("Reset", []) == "重置"
    # 重置也进提示词词表
    assert "重置" in build_action_prompt([])


def test_hook_dispatches_reset(tmp_path=None) -> None:
    adapter = _adapter()
    engine = _FakeEngine()
    adapter._engine = engine  # type: ignore[assignment]
    adapter.reset_stage_called = False

    async def _fake_reset():
        adapter.reset_stage_called = True
        return True

    engine.reset_stage = _fake_reset  # type: ignore[method-assign]
    text = "好，调整好了【重置】继续继续"
    message = {"platform": "live", "processed_plain_text": text, "raw_message": [
        {"type": "text", "data": text}]}

    async def _run():
        await adapter.action_marker_hook(message=message, stream_id=SID)
        for _ in range(10):
            await asyncio.sleep(0)

    asyncio.run(_run())
    assert adapter.reset_stage_called, "Reset 记号没有派发到引擎"
    # 视线超时任务也会被挂上（reset 内部含视线回正，钩子侧不需要再挂）


# --------------------------------------------------------------------- #
# 语气记号（voice direction）与音效标签
# --------------------------------------------------------------------- #
def test_direction_marker_is_builtin() -> None:
    """【压低声音】是内置语气记号：kind=direction，带英文指令；不抢表情名。"""

    assert action_kind_of("压低声音", []) == "direction"
    assert action_kind_of("悄悄话", []) == "direction"
    ins = direction_instruction_of("压低声音")
    assert ins and "low" in ins, "语气记号必须带英文 instruction（发给 breeze）"
    # 「正常语气」= 空串（回到配置默认），但仍是 direction 记号
    assert direction_instruction_of("正常语气") == ""
    # 命名带后缀就是为了不抢模型表情：【惊讶】是表情位，【惊讶语气】才是语气
    assert action_kind_of("惊讶", []) != "direction"
    assert action_kind_of("惊讶语气", []) == "direction"


def test_hook_sets_pending_direction() -> None:
    """语气记号在钩子里同步生效：auto_speak 合成前 _pending_direction 已就位。"""

    adapter = _adapter()
    text = "【压低声音】我跟你们说个秘密哦"
    message = {"platform": "live", "processed_plain_text": text, "raw_message": [
        {"type": "text", "data": text}]}
    result = asyncio.run(adapter.action_marker_hook(message=message, stream_id=SID))
    assert result is not None
    # 台词里的记号被摘掉（不念出来）
    cleaned = result["modified_kwargs"]["message"]["processed_plain_text"]
    assert "压低声音" not in cleaned and "秘密" in cleaned
    ins = adapter._pending_direction
    assert ins is not None and "low" in ins, "钩子返回时语气指令还没就位"


# --------------------------------------------------------------------- #
# 语气记号（voice direction）与音效标签
# --------------------------------------------------------------------- #
def test_direction_marker_is_builtin() -> None:
    """【压低声音】是内置语气记号：kind=direction，带英文指令；不抢表情名。"""

    assert action_kind_of("压低声音", []) == "direction"
    assert action_kind_of("悄悄话", []) == "direction"
    ins = direction_instruction_of("压低声音")
    assert ins and "low" in ins, "语气记号必须带英文 instruction（发给 breeze）"
    # 「正常语气」= 空串（回到配置默认），但仍是 direction 记号
    assert direction_instruction_of("正常语气") == ""
    # 命名带后缀就是为了不抢模型表情：【惊讶】是表情位，【惊讶语气】才是语气
    assert action_kind_of("惊讶", []) != "direction"
    assert action_kind_of("惊讶语气", []) == "direction"


def test_hook_sets_pending_direction() -> None:
    """语气记号在钩子里同步生效：auto_speak 合成前 _pending_direction 已就位。"""

    adapter = _adapter()
    text = "【压低声音】我跟你们说个秘密哦"
    message = {"platform": "live", "processed_plain_text": text, "raw_message": [
        {"type": "text", "data": text}]}
    result = asyncio.run(adapter.action_marker_hook(message=message, stream_id=SID))
    assert result is not None
    # 台词里的记号被摘掉（不念出来）
    cleaned = result["modified_kwargs"]["message"]["processed_plain_text"]
    assert "压低声音" not in cleaned and "秘密" in cleaned
    ins = adapter._pending_direction
    assert ins is not None and "low" in ins, "钩子返回时语气指令还没就位"


def test_direction_free_form_natural_language() -> None:
    """自由语气语法：【语气:自然语言描述】原样发给 breeze，描述里的逗号不拆分。"""

    desc = "语气:发现真相后委屈难过，声音低落柔弱，带轻微哽咽，语速稍慢"
    assert action_kind_of(desc, []) == "direction"
    # 描述原文（含中文逗号）原样下发——breeze 实测中文自然语言指导有效
    assert direction_instruction_of(desc) == "发现真相后委屈难过，声音低落柔弱，带轻微哽咽，语速稍慢"
    # 组合拆分必须跳过语气记号：描述里的逗号不是动作分隔符
    clean, markers = split_actions(f"【{desc}】原来是这样啊……", set())
    assert len(markers) == 1, "语气描述被逗号拆散了"
    assert markers[0].name == desc
    # 空描述 = 回默认
    assert direction_instruction_of("语气:") == ""
    assert direction_instruction_of("语气：") == ""


def test_direction_free_form_natural_language() -> None:
    """自由语气语法：【语气:自然语言描述】原样发给 breeze，描述里的逗号不拆分。"""

    desc = "语气:发现真相后委屈难过，声音低落柔弱，带轻微哽咽，语速稍慢"
    assert action_kind_of(desc, []) == "direction"
    # 描述原文（含中文逗号）原样下发——breeze 实测中文自然语言指导有效
    assert direction_instruction_of(desc) == "发现真相后委屈难过，声音低落柔弱，带轻微哽咽，语速稍慢"
    # 组合拆分必须跳过语气记号：描述里的逗号不是动作分隔符
    clean, markers = split_actions(f"【{desc}】原来是这样啊……", set())
    assert len(markers) == 1, "语气描述被逗号拆散了"
    assert markers[0].name == desc
    # 空描述 = 回默认
    assert direction_instruction_of("语气:") == ""
    assert direction_instruction_of("语气：") == ""


def test_strip_vocal_events() -> None:
    """字幕用：**所有**半角[]标签都摘（已识别的是声音、编造的是垃圾，都不上字幕）。"""

    from vts.markers import strip_vocal_events

    assert strip_vocal_events("[laugh] 哈哈[叹气]太累了") == "哈哈太累了"
    assert strip_vocal_events("[深呼吸]……[哭]呜呜") == "……呜呜"
    # AI 编造的词表外标签（端到端实测它写了 [吸鼻子]）也不上字幕
    assert strip_vocal_events("[吸鼻子] 谢谢你们") == "谢谢你们"
    # 圆括号不是音效标签，正文里的不能误删
    assert strip_vocal_events("这是(一个普通括号)") == "这是(一个普通括号)"


def test_strip_unknown_bracket_tags() -> None:
    """TTS 用：只摘**词表外**的[]标签；词表内的原样留给 breeze 演绎成音效。"""

    from vts.markers import strip_unknown_bracket_tags

    # 词表内：保留（breeze 会演成叹气声）
    assert strip_unknown_bracket_tags("[laugh] 哈哈[叹气]太累了") == "[laugh] 哈哈[叹气]太累了"
    # 词表外（AI 编造的）：摘掉，否则 TTS 会念"吸鼻子"三个字
    assert strip_unknown_bracket_tags("[吸鼻子] 谢谢你们") == " 谢谢你们".strip()
    assert strip_unknown_bracket_tags("[laugh] 好[吸鼻子]吧") == "[laugh] 好吧"
    # 圆括号不碰
    assert strip_unknown_bracket_tags("这是(一个普通括号)") == "这是(一个普通括号)"


# --------------------------------------------------------------------- #
# AI 工具（Planner 自主的演出控制）
# --------------------------------------------------------------------- #
def test_live_tools_dispatch_to_engine() -> None:
    """四个 live_* 工具都要正确落到引擎调用；视线工具带超时滑回。"""

    adapter = _adapter()
    engine = _FakeEngine()
    adapter._engine = engine  # type: ignore[assignment]
    adapter.reset_stage_called = False

    async def _fake_reset():
        adapter.reset_stage_called = True
        return True

    engine.reset_stage = _fake_reset  # type: ignore[method-assign]

    async def run_all():
        gaze = await adapter.tool_live_gaze(target="看向弹幕")
        gesture = await adapter.tool_live_gesture(name="翻白眼")
        expression = await adapter.tool_live_expression(emotion="无语")
        reset = await adapter.tool_live_reset_stage()
        # 视线超时任务对测试无意义，收掉
        task = adapter._gaze_reset_task
        if task is not None and not task.done():
            task.cancel()
        return gaze, gesture, expression, reset

    gaze, gesture, expression, reset = asyncio.run(run_all())
    assert gaze["ok"] is True and gaze["target"] == "看向弹幕"
    assert ("gaze", 0.75, -0.15) in engine.calls, "视线工具没走到引擎"
    assert gesture["ok"] is True and ("tag", "翻白眼") in engine.calls
    assert expression["ok"] is True and ("expression", "无语") in engine.calls
    assert reset["ok"] is True and adapter.reset_stage_called


def test_live_tools_fail_gracefully_without_engine() -> None:
    """引擎没起：工具返回 ok=False + 原因，不抛异常。"""

    adapter = _adapter()
    adapter._engine = None  # type: ignore[assignment]
    for coro in (
        adapter.tool_live_gaze(target="看向弹幕"),
        adapter.tool_live_gesture(name="点头"),
        adapter.tool_live_expression(emotion="开心"),
        adapter.tool_live_reset_stage(),
    ):
        result = asyncio.run(coro)
        assert result["ok"] is False and "演出引擎未运行" in result["error"]


def test_live_gaze_rejects_unknown_target() -> None:
    adapter = _adapter()
    adapter._engine = _FakeEngine()  # type: ignore[assignment]
    result = asyncio.run(adapter.tool_live_gaze(target="看火星"))
    assert result["ok"] is False and "火星" in result["error"]
    assert "targets" in result, "应该把可用目标列出来帮 AI 自纠"
