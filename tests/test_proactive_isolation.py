# -*- coding: utf-8 -*-
"""主动对话与弹幕解耦回归：意图、事件流、三层上下文隔离与状态机。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_MODULE_NAME = "live_danmaku_adapter_isolation_test"

spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
plugin = importlib.util.module_from_spec(spec)
sys.modules[_MODULE_NAME] = plugin
spec.loader.exec_module(plugin)

ProactiveDirector = plugin.ProactiveDirector
LiveDanmakuAdapter = plugin.LiveDanmakuAdapter
strip_live_context_items = plugin.strip_live_context_items
PLACEHOLDER = plugin.PROACTIVE_CONTEXT_PLACEHOLDER
HINT = plugin.PROACTIVE_REPLY_HINT

SID = "live-all"


def _item(item_id: str, text: str, item_type: str = "UserMessageItem") -> dict:
    return {
        "item_type": item_type,
        "meta": {"item_id": item_id, "logical_turn_id": None, "timestamp": "2026-11-05T21:00:00"},
        "parts": [{"type": "text", "text": text}],
    }


def _items() -> list:
    return [
        _item("sys", "system prompt", "SystemMessageItem"),
        _item("u1", '<message msg_id="1" user="观众A">[B站弹幕] 观众A: 主播讲个笑话</message>'),
        _item("u2", "[抖音礼物] 小明 送出了 小心心x1"),
        _item("u3", "[B站醒目留言 ¥30] 土豪: 点首歌"),
        _item("u4", "[语音弹幕] 主播: 你今天怎么样"),
        _item("u5", "普通的本地聊天室消息，不带任何直播前缀"),
        _item("u6", ProactiveDirector().build_intent()),
        _item(
            "task",
            '<plugin_proactive_task id="proactive:local.live-danmaku-adapter:1">\n插件请求你主动处理一轮聊天：现在轮到你自言自语\n</plugin_proactive_task>',
        ),
    ]


def _adapter() -> LiveDanmakuAdapter:
    adapter = LiveDanmakuAdapter()
    adapter._live_stream_ids[SID] = None
    return adapter


def test_intent_is_pure_monologue() -> None:
    """意图不提弹幕、不引导回应，也不能再提示 passive 模式下不可用的 reply 工具。"""

    intent = ProactiveDirector().build_intent()
    assert "弹幕" not in intent
    assert "reply" not in intent
    assert "观众" not in intent
    assert "小故事" in intent
    assert "必须说话" in intent
    assert "stop" in intent and "wait" in intent


def test_director_has_no_event_stream() -> None:
    """主动对话模块不再保存/读取任何直播间事件。"""

    director = ProactiveDirector(min_interval_seconds=5)
    for name in ("record_message", "record_event", "record_self", "recent_events", "_events"):
        assert not hasattr(director, name), f"事件流残留: {name}"


def test_should_speak_only_depends_on_timer() -> None:
    """开口时机只看计时，与有没有新消息无关。"""

    director = ProactiveDirector(min_interval_seconds=10)
    assert director.should_speak(now=100.0) == (True, "")
    director.mark_spoken(now=100.0)
    ok, reason = director.should_speak(now=105.0)
    assert ok is False and "停顿中" in reason
    assert director.should_speak(now=111.0) == (True, "")
    assert ProactiveDirector(min_interval_seconds=0).should_speak(now=1.0) == (True, "")


def test_strip_masks_every_live_prefix_but_keeps_normal_text() -> None:
    """所有直播间消息前缀都被隐藏，普通文本与意图原文保留。"""

    items = _items()
    new_items, hidden = strip_live_context_items(items)
    assert hidden == 4
    by_id = {item["meta"]["item_id"]: item for item in new_items}
    for item_id in ("u1", "u2", "u3", "u4"):
        assert by_id[item_id]["parts"][0]["text"] == PLACEHOLDER
    assert "普通的本地聊天室消息" in by_id["u5"]["parts"][0]["text"]
    assert "小故事" in by_id["u6"]["parts"][0]["text"]
    assert by_id["sys"]["parts"][0]["text"] == "system prompt"


def test_strip_preserves_structure_and_does_not_mutate_input() -> None:
    """只改文本，保留 item_id/meta/其它字段，且不就地修改入参。"""

    items = _items()
    original_text = items[1]["parts"][0]["text"]
    new_items, _ = strip_live_context_items(items)
    assert items[1]["parts"][0]["text"] == original_text
    assert new_items[1]["meta"] == items[1]["meta"]
    assert new_items[1]["item_type"] == "UserMessageItem"
    assert [i["meta"]["item_id"] for i in new_items] == [i["meta"]["item_id"] for i in items]
    assert strip_live_context_items(None) == ([], 0)
    assert strip_live_context_items([1, "x", None]) == ([1, "x", None], 0)


def test_strip_handles_guard_enter_and_non_text_parts() -> None:
    """上舰/进场前缀同样隐藏；图片 part 与畸形 Item 不炸。"""

    items = [
        _item("g1", "[B站上舰·舰长] 小明 开通了舰长"),
        _item("g2", "[抖音进场] 小红 进入了直播间"),
        {
            "item_type": "UserMessageItem",
            "meta": {"item_id": "img", "logical_turn_id": None, "timestamp": "2026-11-05T21:00:00"},
            "parts": [{"type": "image", "image_base64": "AA==", "image_format": "png"}],
        },
        {"item_type": "FunctionCallItem", "tool_call": {"call_id": "c1", "func_name": "reply"}},
    ]
    new_items, hidden = strip_live_context_items(items)
    assert hidden == 2
    assert new_items[0]["parts"][0]["text"] == PLACEHOLDER
    assert new_items[1]["parts"][0]["text"] == PLACEHOLDER
    assert new_items[2]["parts"][0]["type"] == "image"
    assert new_items[3]["tool_call"]["call_id"] == "c1"


def test_isolation_arm_active_expire_and_disarm() -> None:
    """隔离窗口：置位生效、超时自清理、主动解除。"""

    adapter = _adapter()
    adapter._arm_proactive_isolation(SID, now=100.0)
    assert adapter._proactive_turn_active(SID, now=101.0) is True
    assert adapter._proactive_turn_active(SID, now=1000.0) is False
    assert adapter._proactive_isolation == {}
    adapter._arm_proactive_isolation(SID, now=100.0)
    adapter._disarm_proactive_isolation(SID)
    assert adapter._proactive_turn_active(SID, now=101.0) is False


def test_isolation_alias_for_single_live_stream() -> None:
    """宿主在 planner/replyer 可能用不同形式的会话 ID，单流时按主动轮处理。"""

    adapter = _adapter()
    adapter._arm_proactive_isolation(SID, now=100.0)
    assert adapter._proactive_turn_active("another-session-id", now=101.0) is True
    assert adapter._proactive_turn_active("another-session-id", now=102.0) is True
    adapter._disarm_proactive_isolation()
    assert adapter._proactive_isolation == {}


def test_planner_hook_only_masks_during_proactive_turn() -> None:
    """非主动轮也会标注自己说的话/注入规则；主动轮额外隐藏弹幕。"""

    adapter = _adapter()
    kwargs = {
        "session_id": SID,
        "items": _items(),
        "tool_definitions": [
            {"type": "function", "function": {"name": "send_emoji"}},
            {"name": "tool_search"},
        ],
    }
    result = asyncio.run(adapter.planner_tool_filter_hook(**kwargs))
    assert result is not None and "tool_definitions" in result["modified_kwargs"]
    # 宿主 modified_kwargs 是整体替换，必须把原始 kwargs 完整带回去
    assert result["modified_kwargs"]["session_id"] == SID
    assert result["modified_kwargs"]["items"] is not None
    assert plugin.SELF_ECHO_RULE in result["modified_kwargs"]["items"][-1]["parts"][0]["text"]

    adapter._arm_proactive_isolation(SID)
    result = asyncio.run(adapter.planner_tool_filter_hook(**kwargs))
    assert result is not None
    modified = result["modified_kwargs"]
    assert len(modified["tool_definitions"]) == 1
    hidden_items = modified["items"]
    assert hidden_items[1]["parts"][0]["text"] == PLACEHOLDER
    assert "小故事" in hidden_items[6]["parts"][0]["text"]


def test_strip_replaces_rules_summary_guidance_and_assistant_history() -> None:
    """真机快照里发现的另外四类「把 AI 拉回弹幕」的内容也要清掉。"""

    items = [
        _item("rule", "当前聊天额外注意事项： 【弹幕回复规则】 - 回复弹幕时，要先读一遍弹幕，再给出回应。"),
        _item("summary", "【上下文状态摘要-内部参考】 当前话题: - Bot 反复向弹幕要一个背靠墙的角落。"),
        _item("guide", "弹幕读到观众A说“我不会得狂犬病吧”。 发言指导： - 先读弹幕：观众A说…"),
        _item("assistant", "弹幕读到 bili_123 只发了一个？，友好范畴", "AssistantMessageItem"),
        _item("self", '<message user="Bot" is_self_message="true"> 我给你们算个账啊，硬币有编制'),
        _item("plain", "当前时间：2026-11-05 21:24:11"),
    ]
    new, hidden = strip_live_context_items(items)
    assert hidden == 4
    by_id = {item["meta"]["item_id"]: item["parts"][0]["text"] for item in new}
    assert by_id["rule"] == plugin.PROACTIVE_MONOLOGUE_RULE
    assert by_id["summary"] == PLACEHOLDER
    assert by_id["guide"] == plugin.PROACTIVE_NEUTRAL_GUIDANCE
    assert by_id["assistant"] == PLACEHOLDER
    assert "我给你们算个账" in by_id["self"]
    assert by_id["plain"] == "当前时间：2026-11-05 21:24:11"


def test_strip_handles_bare_danmaku_guidance_and_phrase_hints() -> None:
    """真机里另外两种形态：「弹幕：…回应…」指导块、含弹幕的句式提示块。"""

    items = [
        _item("g", "弹幕：自放鹤人归云烟“还好带耳机了”。\n回应带耳机：明智，不戴耳机算工伤。"),
        _item("h", "弹幕这轮没有新内容，继续自言自语。\n\n发言指导：\n- 接上遥控器电池装反的故事，再补一句抽象发散。"),
        _item("p", '你可以视情况自由使用以下句式：\n- 当"读弹幕"时，可以用"绷不住了"来表达。'),
    ]
    new, hidden = strip_live_context_items(items)
    assert hidden == 3
    by_id = {item["meta"]["item_id"]: item["parts"][0]["text"] for item in new}
    assert by_id["g"] == plugin.PROACTIVE_NEUTRAL_GUIDANCE
    assert "遥控器电池装反" in by_id["h"] and "继续自言自语" not in by_id["h"]
    assert plugin.PROACTIVE_NEUTRAL_GUIDANCE in by_id["h"]
    assert by_id["p"] == PLACEHOLDER


def test_strip_leaves_system_untouched_for_prefix_cache() -> None:
    """主动轮不再改写 system：system 是前缀缓存起点，保持原样才能连续命中。"""

    system = "你的名字是Bot。\n当前聊天额外注意事项：\n【弹幕回复规则】\n- 回复弹幕时，要先读一遍弹幕。"
    items = [_item("sys", system, "SystemMessageItem"), _item("u1", "[B站弹幕] 观众A: 在吗")]
    new, hidden = strip_live_context_items(items)
    assert hidden == 1
    assert new[0]["parts"][0]["text"] == system
    assert plugin.PROACTIVE_SYSTEM_OVERRIDE not in new[0]["parts"][0]["text"]


def test_strip_only_touches_tail_items() -> None:
    """只清洗尾部：历史保持原样，prompt 前缀才稳定。"""

    items = [
        _item("sys", "system prompt", "SystemMessageItem"),
        _item("u1", "[B站弹幕] 观众A: 早"),
        _item("u2", "[B站弹幕] 观众B: 来了"),
        _item("u3", "[B站弹幕] 观众C: 在吗"),
    ]
    new, hidden = strip_live_context_items(items, tail_items=1)
    assert hidden == 1
    assert new[-1]["parts"][0]["text"] == plugin.PROACTIVE_CONTEXT_PLACEHOLDER
    assert new[1]["parts"][0]["text"] == "[B站弹幕] 观众A: 早"
    assert new[2]["parts"][0]["text"] == "[B站弹幕] 观众B: 来了"


def test_active_task_id_distinguishes_proactive_turn() -> None:
    """用本次 task_id 区分主动轮与之后只带旧 task_id 的弹幕回复轮。"""

    def tag(num: int) -> dict:
        return _item(f"t{num}", f'<plugin_proactive_task id="proactive:local.live-danmaku-adapter:{num}">')

    current = {"proactive:local.live-danmaku-adapter:2"}
    assert plugin.items_have_active_proactive_task([tag(1), tag(2)], current) is True
    assert plugin.items_have_active_proactive_task([tag(1)], current) is False
    assert plugin.items_have_active_proactive_task([tag(1)], set()) is True
    assert plugin.items_have_active_proactive_task([_item("u", "普通文本")], current) is True
    assert plugin.items_have_active_proactive_task(None, current) is True


def test_planner_hook_disarms_stale_isolation_for_danmaku_turn() -> None:
    """主动轮 stop 后隔离残留：后续只带旧 task_id 的轮次立即解除，弹幕回复照常。"""

    adapter = _adapter()
    adapter._arm_proactive_isolation(SID)
    adapter._proactive_task_ids.add("proactive:local.live-danmaku-adapter:2")
    items = [
        _item("task", '<plugin_proactive_task id="proactive:local.live-danmaku-adapter:1">'),
        _item("u1", "[B站弹幕] 观众A: 主播在吗"),
    ]
    kwargs = {"session_id": SID, "items": items, "tool_definitions": []}
    asyncio.run(adapter.planner_tool_filter_hook(**kwargs))
    # 通用「自己发言识别」规则对所有直播轮都注入；关键是隔离状态已解除
    assert adapter._proactive_isolation == {}
    assert adapter._proactive_task_ids == set()
    assert asyncio.run(adapter.proactive_reply_context_hook(session_id=SID, items=items)) is None


def test_replyer_hint_hook_guards_all_live_rounds() -> None:
    """Replyer 所有直播轮都禁止接自己的话；主动轮再加自言自语锁，不重复追加。"""

    adapter = _adapter()
    base = {"session_id": SID, "extra_prompt": "原插件要求：语气轻快"}
    result = asyncio.run(adapter.proactive_reply_hint_hook(**base))
    assert result is not None
    extra = result["modified_kwargs"]["extra_prompt"]
    assert "原插件要求：语气轻快" in extra and plugin.SELF_ECHO_RULE in extra
    assert HINT not in extra

    adapter._arm_proactive_isolation(SID)
    result = asyncio.run(adapter.proactive_reply_hint_hook(**base))
    extra = result["modified_kwargs"]["extra_prompt"]
    assert plugin.SELF_ECHO_RULE in extra and HINT in extra
    assert asyncio.run(adapter.proactive_reply_hint_hook(session_id=SID, extra_prompt=extra)) is None

    empty = asyncio.run(adapter.proactive_reply_hint_hook(session_id=SID, extra_prompt=""))
    assert plugin.SELF_ECHO_RULE in empty["modified_kwargs"]["extra_prompt"]
    assert HINT in empty["modified_kwargs"]["extra_prompt"]


def test_replyer_context_hook_masks_only_in_proactive_turn() -> None:
    """Replyer 请求上下文同样隐藏弹幕；非主动轮原样放行。"""

    adapter = _adapter()
    kwargs = {"session_id": SID, "items": _items()}
    assert asyncio.run(adapter.proactive_reply_context_hook(**kwargs)) is None
    adapter._arm_proactive_isolation(SID)
    result = asyncio.run(adapter.proactive_reply_context_hook(**kwargs))
    assert result is not None
    assert result["modified_kwargs"]["items"][1]["parts"][0]["text"] == PLACEHOLDER
    adapter._disarm_proactive_isolation()
    assert asyncio.run(adapter.proactive_reply_context_hook(**kwargs)) is None


def test_cooldown_starts_when_speech_finishes_not_when_task_is_sent() -> None:
    """「说完后停顿」从整段播完起算，发起任务时不能提前打点。

    旧实现在 trigger 之前就 mark_spoken()，一轮 TTS 远大于设定秒数，
    冷却永远拦不住任何一轮，与巡检间隔看起来重复、实际都失效。
    """

    data = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    adapter = _adapter()
    adapter._plugin_config_instance = plugin.LiveDanmakuConfig.model_validate(data)
    director = ProactiveDirector(min_interval_seconds=10)
    adapter._proactive = director
    adapter._latest_live_stream_id = lambda: SID

    class _Maisaka:
        async def trigger_proactive(self, *args: object, **kwargs: object) -> dict:
            return {"success": True, "task_id": "proactive:test:1"}

    adapter._ctx = SimpleNamespace(maisaka=_Maisaka())
    asyncio.run(adapter._maybe_speak_proactively())
    assert director._last_speak_at == 0.0, "发起任务时起了冷却，会和播报门控重叠"

    adapter._mark_speech_finished()
    finished_at = director._last_speak_at
    assert finished_at > 0.0
    ok, reason = director.should_speak(now=finished_at + 5.0)
    assert ok is False and "停顿中" in reason
    assert director.should_speak(now=finished_at + 11.0)[0] is True


def test_proactive_timing_configs_are_distinct() -> None:
    """主动对话是纯事件驱动：扫描粒度字段已彻底删除，不许回潮。"""

    data = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    cfg = plugin.LiveDanmakuConfig.model_validate(data)
    assert 0 <= cfg.proactive.min_interval_seconds <= 120
    assert plugin.ProactiveConfig.model_fields["min_interval_seconds"].json_schema_extra["label"] == "说完后停顿（秒）"
    # 字段必须真的没了：model_fields 里没有、配置文件里也没有
    assert "tick_seconds" not in plugin.ProactiveConfig.model_fields, "扫描粒度字段又回来了"
    assert "tick_seconds" not in data["proactive"]


def test_config_uses_isolation_seconds_and_drops_event_window() -> None:
    """配置文件不再有事件回溯窗口，改为上下文隔离窗口。"""

    data = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    assert "event_window_seconds" not in data["proactive"]
    # 具体秒数允许使用者自行调整，只校验字段存在且是有效的兜底超时
    assert 1 <= data["proactive"]["isolation_seconds"] <= 600
    cfg = plugin.LiveDanmakuConfig.model_validate(data)
    assert cfg.proactive.isolation_seconds == data["proactive"]["isolation_seconds"]
    assert not hasattr(cfg.proactive, "event_window_seconds")


# --------------------------------------------------------------------- #
# 「AI 回复自己说的话」回归（真机快照 23:03:33 的轮次形态）
# --------------------------------------------------------------------- #


def _self_echo_items() -> list:
    """复刻只有自己刚说的 3 条分段短句、没有任何观众消息的一轮。"""

    return [
        _item("sys", "system prompt", "SystemMessageItem"),
        _item(
            "s1",
            '<message msg_id="m6eaj2l" time="23:03:30" user="Bot" is_self_message="true">\n行吧大爷听完当场把蓝条当血条用</message>',
        ),
        _item(
            "s2",
            '<message msg_id="mtcsapr" time="23:03:30" user="Bot" is_self_message="true">\n搓个火球把自己蓝搓干净了还问我怎么放不出来</message>',
        ),
        _item(
            "s3", '<message msg_id="mbp2bkr" time="23:03:30" user="Bot" is_self_message="true">\n我已急哭</message>'
        ),
        _item("tail", "请你进行分析，是不调用工具，进行思考和等待\n还是使用 reply 回复"),
    ]


def _texts(items: list) -> str:
    return "\n".join(
        part["text"]
        for item in items
        for part in item.get("parts", [])
        if isinstance(part, dict) and part.get("type") == "text"
    )


def test_annotate_self_messages_preserves_structure_and_is_idempotent() -> None:
    """只给自身消息加标签：不删 Item、不改 meta、保留原文、不重复标注。"""

    items = _self_echo_items()
    new, count = plugin.annotate_self_messages(items)
    assert count == 3
    assert new[0] is items[0] and new[4] is items[4]
    for index in (1, 2, 3):
        assert new[index]["meta"]["item_id"] == items[index]["meta"]["item_id"]
        assert new[index]["parts"][0]["text"].startswith(plugin.SELF_ECHO_LABEL)
    assert "我已急哭" in new[3]["parts"][0]["text"]
    again, again_count = plugin.annotate_self_messages(new)
    assert again_count == 0 and again == new


def test_self_only_round_ignores_rounds_with_real_viewers() -> None:
    """只有「全是自己消息」的轮次才判定为空转；有弹幕的轮次必须照常回复。"""

    assert plugin.self_only_round(_self_echo_items()) is True
    mixed = _self_echo_items()
    mixed.insert(2, _item("dm", '<message msg_id="mx" user="观众A">[B站弹幕] 观众A: 法术，小子</message>'))
    assert plugin.self_only_round(mixed) is False
    assert plugin.self_only_round([]) is False
    assert plugin.self_only_round("not a list") is False


def test_append_round_rule_targets_tail_not_system() -> None:
    """规则追加在尾部（不动 system 前缀，保住 prompt 缓存），且幂等。"""

    items = _self_echo_items()
    new, added = plugin.append_round_rule(items, "RULE-X")
    assert added is True
    assert new[-1]["parts"][0]["text"].startswith("RULE-X")
    assert new[0] is items[0] and "RULE-X" not in new[0]["parts"][0]["text"]
    again, again_added = plugin.append_round_rule(new, "RULE-X")
    assert again_added is False and again == new


def test_planner_hook_asks_model_to_wait_on_self_only_round() -> None:
    """空转轮：标注自身消息 + 注入「不要说话，直接 stop」，kwargs 完整回传。"""

    adapter = _adapter()
    kwargs = {
        "session_id": SID,
        "items": _self_echo_items(),
        "tool_definitions": [{"name": "reply"}],
        "selection_reason": "chat",
    }
    result = asyncio.run(adapter.planner_tool_filter_hook(**kwargs))
    assert result is not None
    modified = result["modified_kwargs"]
    assert modified["session_id"] == SID and modified["selection_reason"] == "chat"
    assert modified["tool_definitions"] == kwargs["tool_definitions"]
    joined = _texts(modified["items"])
    assert plugin.SELF_ONLY_ROUND_RULE in joined
    assert plugin.SELF_ECHO_RULE in joined
    assert joined.count(plugin.SELF_ECHO_LABEL) == 3


def test_proactive_round_keeps_self_messages_without_self_only_rule() -> None:
    """主动轮仍要说话：用「讲新段子」规则，不能误用 wait；自己说过的话保留并标注。"""

    adapter = _adapter()
    adapter._arm_proactive_isolation(SID)
    adapter._proactive_task_ids.add("proactive:local.live-danmaku-adapter:9")
    items = _self_echo_items()
    items.insert(4, _item("task", '<plugin_proactive_task id="proactive:local.live-danmaku-adapter:9">'))
    items.insert(5, _item("dm", "[B站弹幕] 观众A: 法术，小子"))
    result = asyncio.run(adapter.planner_tool_filter_hook(session_id=SID, items=items, tool_definitions=[]))
    assert result is not None
    joined = _texts(result["modified_kwargs"]["items"])
    assert plugin.PROACTIVE_FRESH_RULE in joined
    assert plugin.SELF_ONLY_ROUND_RULE not in joined
    assert joined.count(plugin.SELF_ECHO_LABEL) == 3
    assert PLACEHOLDER in joined  # 弹幕照常隐藏


def test_self_only_round_suppresses_speech_until_a_real_round() -> None:
    """空转轮抑制播报；下一轮只要出现观众消息就立刻解除，绝不误伤弹幕回复。"""

    adapter = _adapter()
    asyncio.run(adapter.planner_tool_filter_hook(session_id=SID, items=_self_echo_items(), tool_definitions=[]))
    assert adapter._self_echo_suppress_until > time.monotonic()

    mixed = _self_echo_items()
    mixed.insert(2, _item("dm", '<message msg_id="mx" user="观众A">[B站弹幕] 观众A: 法术，小子</message>'))
    asyncio.run(adapter.planner_tool_filter_hook(session_id=SID, items=mixed, tool_definitions=[]))
    assert adapter._self_echo_suppress_until == 0.0


def test_auto_speak_skips_text_from_self_only_round() -> None:
    """抑制期内的出站文本不排队播报；文字仍照常发出（不 abort）。"""

    data = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    adapter = _adapter()
    adapter._plugin_config_instance = plugin.LiveDanmakuConfig.model_validate(data)
    adapter._tts_client = object()
    adapter._player = object()

    async def _fake_perform_reply(*args: object, **kwargs: object) -> None:
        return None

    adapter._perform_reply = _fake_perform_reply  # type: ignore[method-assign]
    message = {"platform": "live", "processed_plain_text": "谁家好人搓火球把自己蓝搓干净还问怎么放不出来啊"}

    adapter._self_echo_suppress_until = time.monotonic() + 30.0
    assert asyncio.run(adapter.auto_speak_hook(message=message)) is None
    assert adapter._speech_pending == 0

    adapter._self_echo_suppress_until = 0.0
    assert asyncio.run(adapter.auto_speak_hook(message=message)) is None
    assert adapter._speech_pending == 1  # 正常轮照常排队播报


def test_replyer_context_annotates_self_in_every_live_round() -> None:
    """生成层也标注自身消息，避免最终字句去接自己上一句。"""

    adapter = _adapter()
    kwargs = {"session_id": SID, "items": _self_echo_items(), "extra_prompt": "语气轻快"}
    result = asyncio.run(adapter.proactive_reply_context_hook(**kwargs))
    assert result is not None
    assert result["modified_kwargs"]["session_id"] == SID
    assert result["modified_kwargs"]["extra_prompt"] == "语气轻快"
    assert _texts(result["modified_kwargs"]["items"]).count(plugin.SELF_ECHO_LABEL) == 3


def main() -> int:
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
    print("主动对话与弹幕解耦回归通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def test_last_context_message_id_picks_latest() -> None:
    """主动轮 reply 的锚点取上下文里最后一条真实消息，不取内部任务 id。"""

    items = [
        {
            "item_type": "UserMessageItem",
            "parts": [{"type": "text", "text": '<message msg_id="m1" user="A">早</message>'}],
        },
        {
            "item_type": "UserMessageItem",
            "parts": [{"type": "text", "text": '<message msg_id="m2" user="B">来了</message>'}],
        },
    ]
    assert plugin.last_context_message_id(items) == "m2"
    # 没有消息 / 结构不对：返回空串，调用方回退到任务 id
    assert plugin.last_context_message_id(None) == ""
    assert plugin.last_context_message_id([{"item_type": "SystemMessageItem"}]) == ""


def _adapter_with_isolation(seconds: int) -> LiveDanmakuAdapter:
    adapter = _adapter()
    adapter._plugin_config_instance = SimpleNamespace(
        proactive=SimpleNamespace(isolation_seconds=seconds)
    )
    return adapter


def test_isolation_ttl_has_floor_for_browser_chain() -> None:
    """WebUI 把 isolation_seconds 写回 10 也不能让工具链半路饿死。"""

    assert _adapter_with_isolation(10)._proactive_isolation_ttl() == 30
    assert _adapter_with_isolation(60)._proactive_isolation_ttl() == 60
    assert _adapter_with_isolation(300)._proactive_isolation_ttl() == 300


def test_tool_chain_renews_isolation_past_fixed_ttl() -> None:
    """复刻真机时间线：导航->快照->查找->再导航 12.7s，隔离必须仍有效。"""

    adapter = _adapter_with_isolation(60)
    t0 = 1000.0
    adapter._arm_proactive_isolation(SID, now=t0)
    # planner hook 在每一轮工具链活跃时都会续期
    for offset in (1.6, 5.8, 7.5, 9.0):
        assert adapter._proactive_turn_active(SID, now=t0 + offset) is True
        adapter._renew_proactive_isolation(SID, now=t0 + offset)
    # 旧的 10s 固定窗口在这里早就过期，AI 会掉回弹幕回复轮直接 stop
    assert adapter._proactive_turn_active(SID, now=t0 + 12.7) is True
    # 工具链停了之后仍会自然超时，不会永久残留挡住后续弹幕回复
    assert adapter._proactive_turn_active(SID, now=t0 + 12.7 + 61) is False


def test_renew_does_not_resurrect_disarmed_isolation() -> None:
    """出站已经解除的隔离不能被续期复活，否则弹幕回复会看不到弹幕。"""

    adapter = _adapter_with_isolation(60)
    adapter._arm_proactive_isolation(SID, now=100.0)
    adapter._disarm_proactive_isolation()
    adapter._renew_proactive_isolation(SID, now=101.0)
    assert adapter._proactive_turn_active(SID, now=102.0) is False


def test_renew_is_noop_without_matching_session() -> None:
    """未知 session 不应凭空创建隔离记录。"""

    adapter = _adapter_with_isolation(60)
    adapter._arm_proactive_isolation(SID, now=100.0)
    adapter._renew_proactive_isolation("some-other-session", now=101.0)
    assert set(adapter._proactive_isolation) == {SID}


def _browser_chain_items() -> list:
    return [
        _item("sys", "system prompt", "SystemMessageItem"),
        {"item_type": "FunctionCallItem", "tool_call": {"func_name": "browser_navigate"}},
        {"item_type": "FunctionCallOutputItem", "tool_name": "browser_navigate", "output": "ok"},
    ]


def test_browser_chain_budget_closes_tools() -> None:
    """工具链跑够就收口：不给浏览器工具，模型只能 reply 开口讲。"""

    adapter = _adapter_with_isolation(60)
    adapter._arm_proactive_isolation(SID)
    adapter._proactive_task_ids.add("proactive:local.live-danmaku-adapter:browser")
    tools = [
        {"function": {"name": "browser_navigate"}},
        {"function": {"name": "browser_snapshot"}},
        {"function": {"name": "reply"}},
        {"function": {"name": "stop"}},
    ]
    kwargs = {"session_id": SID, "items": _browser_chain_items(), "tool_definitions": tools}

    # 预算内：浏览器工具照常可见，每走一步都在续期
    for step in range(plugin.MAX_PROACTIVE_BROWSER_CALLS):
        result = asyncio.run(adapter.planner_tool_filter_hook(**kwargs))
        names = [
            adapter._tool_definition_name(d)
            for d in result["modified_kwargs"].get("tool_definitions", tools)
        ]
        assert "browser_navigate" in names, f"第 {step + 1} 步不该提前收口"
        assert adapter._proactive_browser_calls == step + 1

    # 超预算：拿掉 browser_*，但保留 reply / stop，让她能开口
    result = asyncio.run(adapter.planner_tool_filter_hook(**kwargs))
    names = [
        adapter._tool_definition_name(d)
        for d in result["modified_kwargs"]["tool_definitions"]
    ]
    assert "browser_navigate" not in names
    assert "browser_snapshot" not in names
    assert "reply" in names and "stop" in names


def test_browser_call_counter_resets_on_arm_and_disarm() -> None:
    """新任务和出站后都必须清零，否则下一轮一开口就被收口。"""

    adapter = _adapter_with_isolation(60)
    adapter._proactive_browser_calls = 99
    adapter._arm_proactive_isolation(SID, now=100.0)
    assert adapter._proactive_browser_calls == 0
    adapter._proactive_browser_calls = 99
    adapter._disarm_proactive_isolation(SID)
    assert adapter._proactive_browser_calls == 0
