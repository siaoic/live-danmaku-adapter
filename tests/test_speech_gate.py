# -*- coding: utf-8 -*-
"""整段 LLM 回复播报期间的 Planner 门控回归。

覆盖：分段打字等待的消除、整段门控的开合时机、分批兜底窗口、ASR 打断、
以及宿主的 modified_kwargs「整体替换」语义（不能只回传单个字段）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_MODULE_NAME = "live_danmaku_adapter_speech_gate_test"

spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
plugin = importlib.util.module_from_spec(spec)
sys.modules[_MODULE_NAME] = plugin
spec.loader.exec_module(plugin)

LiveDanmakuAdapter = plugin.LiveDanmakuAdapter
SID = "live-all"


def _adapter() -> LiveDanmakuAdapter:
    data = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    adapter = LiveDanmakuAdapter()
    adapter._plugin_config_instance = plugin.LiveDanmakuConfig.model_validate(data)
    adapter._live_stream_ids[SID] = None
    return adapter


def _build_kwargs() -> dict:
    """模拟 send_service.after_build_message 的完整入参。"""

    return {
        "message": {"platform": "live", "processed_plain_text": "第二句短文本"},
        "stream_id": SID,
        "processed_plain_text": "第二句短文本",
        "typing": True,
        "set_reply": False,
        "storage_message": True,
        "show_log": False,
    }


# --------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------- #

def test_config_exposes_gate_settings() -> None:
    data = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    assert data["tts"]["speech_gate_hold_seconds"] == 2.0
    cfg = plugin.LiveDanmakuConfig.model_validate(data)
    # 取消分段打字等待已写死进代码（不再是配置项）：钩子必须始终生效
    assert not hasattr(cfg.tts, "no_typing_delay")
    assert cfg.tts.speech_gate_hold_seconds == 2.0


def test_host_splitter_is_enabled() -> None:
    """这个门控是为「打开断句」配套的；顺带守住 WebUI 把配置写回的漂移。"""

    data = tomllib.loads(
        (_PLUGIN_DIR.parent.parent / "config" / "bot_config.toml").read_text(encoding="utf-8")
    )
    splitter = data["response_splitter"]
    assert splitter["enable"] is True
    # 句子数上限太小会命中 overflow 分支，返回随机预设话术吞掉 AI 的话
    assert splitter["max_sentence_num"] >= 12
    assert splitter["max_split_num"] >= 3
    assert splitter["enable_overflow_return_all"] is True


# --------------------------------------------------------------------- #
# 取消分段模拟打字等待
# --------------------------------------------------------------------- #

def test_typing_hook_disables_delay_and_keeps_all_kwargs() -> None:
    """宿主 modified_kwargs 是整体替换，必须回传完整 kwargs。"""

    kwargs = _build_kwargs()
    result = asyncio.run(_adapter().typing_delay_hook(**kwargs))
    assert result is not None
    changed = result["modified_kwargs"]
    assert changed["typing"] is False
    for key, value in kwargs.items():
        if key != "typing":
            assert key in changed, f"丢了参数 {key}"
            assert changed[key] == value


def test_typing_hook_skips_when_not_typing() -> None:
    assert asyncio.run(_adapter().typing_delay_hook(**{**_build_kwargs(), "typing": False})) is None


def test_typing_hook_is_unconditional() -> None:
    """取消打字等待写死进代码：没有开关，直播流永远走这条。"""

    adapter = _adapter()
    result = asyncio.run(adapter.typing_delay_hook(**_build_kwargs()))
    assert result is not None and result["modified_kwargs"]["typing"] is False


def test_typing_hook_skips_local_chat() -> None:
    kwargs = _build_kwargs()
    kwargs["stream_id"] = "private:qq:123456"
    kwargs["message"] = {"platform": "qq", "processed_plain_text": "本地聊天室"}
    assert asyncio.run(_adapter().typing_delay_hook(**kwargs)) is None


def test_typing_hook_uses_platform_fallback() -> None:
    """stream_id 还没学到时，靠 platform 兜底也要取消等待。"""

    kwargs = _build_kwargs()
    kwargs["stream_id"] = "some-unknown-stream"
    result = asyncio.run(_adapter().typing_delay_hook(**kwargs))
    assert result is not None and result["modified_kwargs"]["typing"] is False


def test_typing_delay_would_exceed_short_audio() -> None:
    """用宿主自己的函数确认：20 字短句的打字等待远长于一句短音频。"""

    from src.chat.utils.utils import calculate_typing_time  # type: ignore

    delay = calculate_typing_time("这是一句用来测试断句间隔的二十字左右的短句子")
    assert delay > 5.0, delay


# --------------------------------------------------------------------- #
# 整段门控状态机
# --------------------------------------------------------------------- #

def test_planner_blocked_idle() -> None:
    assert _adapter()._planner_blocked() is False


def test_planner_blocked_while_segment_pending_or_playing() -> None:
    adapter = _adapter()
    adapter._speech_pending = 3
    assert adapter._planner_blocked() is True
    adapter._speech_pending = 0
    adapter._speaking.set()
    assert adapter._planner_blocked() is True
    adapter._speaking.clear()
    assert adapter._planner_blocked() is False


def test_planner_blocked_during_hold_window() -> None:
    """最后一句播完、下一句还没到达的空窗，兜底窗口必须继续挡住。"""

    adapter = _adapter()
    now = time.monotonic()
    adapter._speech_gate_until = now + 2.0
    assert adapter._planner_blocked(now + 1.9) is True
    assert adapter._planner_blocked(now + 2.1) is False


def test_speaking_gate_aborts_live_danmaku_during_batch() -> None:
    adapter = _adapter()
    adapter._speech_gate_until = time.monotonic() + 5.0
    result = asyncio.run(
        adapter.speaking_gate_hook(message={"platform": "live", "is_command": False})
    )
    assert result == {"action": "abort"}


def test_speaking_gate_allows_commands_non_live_and_idle() -> None:
    adapter = _adapter()
    adapter._speech_gate_until = time.monotonic() + 5.0
    assert asyncio.run(
        adapter.speaking_gate_hook(message={"platform": "live", "is_command": True})
    ) is None
    assert asyncio.run(
        adapter.speaking_gate_hook(message={"platform": "qq", "is_command": False})
    ) is None
    adapter._speech_gate_until = 0.0
    assert asyncio.run(
        adapter.speaking_gate_hook(message={"platform": "live", "is_command": False})
    ) is None


def test_auto_speak_hook_arms_gate_for_each_segment() -> None:
    """每条短句入队都续期兜底窗口，整段期间门控不断。"""

    adapter = _adapter()
    adapter._tts_client = object()
    adapter._player = object()
    adapter._proactive = None

    async def _fake_perform(text: str, generation: int = 0) -> None:
        # 模拟「还在播」，不递减 _speech_pending
        await asyncio.sleep(10)

    adapter._perform_reply = _fake_perform  # type: ignore[method-assign]

    async def _run() -> None:
        for i in range(3):
            await adapter.auto_speak_hook(
                message={"platform": "live", "processed_plain_text": f"第{i + 1}句短文本。"}
            )
        await asyncio.sleep(0)
        assert adapter._speech_pending == 3
        assert adapter._planner_blocked() is True
        tasks = list(adapter._reply_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(_run())
    assert adapter._reply_tasks == set()


def test_last_segment_clears_gate_immediately() -> None:
    """整段最后一句播完立即放行，不额外拖延。"""

    adapter = _adapter()
    adapter._speech_pending = 1
    adapter._speech_gate_until = time.monotonic() + 30.0
    adapter._proactive = None

    async def _fake_react(text: str):
        return None

    async def _fake_speak(text: str) -> bool:
        return False

    adapter.react_expression = _fake_react  # type: ignore[method-assign]
    adapter.speak = _fake_speak  # type: ignore[method-assign]

    asyncio.run(adapter._perform_reply("最后一句。", adapter._speech_generation))
    assert adapter._speech_pending == 0
    assert adapter._speech_gate_until == 0.0
    assert adapter._planner_blocked() is False


def test_reply_segment_grants_followup_budget() -> None:
    """回应观众的一整段话开头：发「回完话接着聊」额度，播完才有 Planner 可触发。"""

    adapter = _adapter()
    adapter._tts_client = object()
    adapter._player = object()
    adapter._proactive = None

    async def _fake_perform(text: str, generation: int = 0) -> None:
        await asyncio.sleep(10)

    adapter._perform_reply = _fake_perform  # type: ignore[method-assign]

    async def _run() -> None:
        assert adapter._followup_turns_left == 0
        await adapter.auto_speak_hook(
            message={"platform": "live", "processed_plain_text": "第一句短文本。"}, stream_id=SID
        )
        await asyncio.sleep(0)
        assert adapter._followup_turns_left == adapter._followup_budget()
        assert adapter._followup_turns_left >= 1
        # 同一段的第二条短句不能再加成倍额度
        await adapter.auto_speak_hook(
            message={"platform": "live", "processed_plain_text": "第二句短文本。"}, stream_id=SID
        )
        await asyncio.sleep(0)
        assert adapter._followup_turns_left == adapter._followup_budget()
        tasks = list(adapter._reply_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(_run())


def test_proactive_segment_does_not_grant_budget() -> None:
    """自语段不续额度：否则又退化成「音频多长、话就多密」的自循环。"""

    adapter = _adapter()
    adapter._tts_client = object()
    adapter._player = object()
    adapter._proactive = plugin.ProactiveDirector()
    adapter._arm_proactive_isolation(SID)

    async def _fake_perform(text: str, generation: int = 0) -> None:
        await asyncio.sleep(10)

    adapter._perform_reply = _fake_perform  # type: ignore[method-assign]

    async def _run() -> None:
        await adapter.auto_speak_hook(
            message={"platform": "live", "processed_plain_text": "自己接着说的话。"}, stream_id=SID
        )
        await asyncio.sleep(0)
        assert adapter._followup_turns_left == 0
        tasks = list(adapter._reply_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(_run())


def test_last_segment_asks_planner_to_continue() -> None:
    """回完观众的整段播完：必须真的排一次主动开口，Planner 才会被叫醒。"""

    adapter = _adapter()
    adapter._speech_pending = 1
    adapter._followup_turns_left = 1
    adapter._proactive = plugin.ProactiveDirector()
    adapter.scheduled: list = []
    adapter._schedule_proactive = (  # type: ignore[method-assign]
        lambda reason, **kwargs: adapter.scheduled.append((reason, bool(kwargs.get("followup"))))
    )

    async def _fake_react(text: str):
        return None

    async def _fake_speak(text: str) -> bool:
        return True

    adapter.react_expression = _fake_react  # type: ignore[method-assign]
    adapter.speak = _fake_speak  # type: ignore[method-assign]

    asyncio.run(adapter._perform_reply("最后一句。", adapter._speech_generation))
    assert adapter.scheduled == [("回复完接着聊", True)]
    assert adapter._followup_turns_left == 0


def test_reply_finish_wakes_planner_with_followup() -> None:
    """真机 bug 回归：回完弹幕、整段播完后必须真的下发主动任务。

    宿主侧 ``enqueue_proactive_task`` 是唯一能唤醒 Planner 的入口；不下发就
    等于「回复完弹幕不会主动对话」，日志里只会看到「保持安静」。
    """

    adapter = _adapter()
    adapter._tts_client = object()
    adapter._player = object()
    adapter._proactive = plugin.ProactiveDirector()
    adapter._latest_live_stream_id = lambda: SID
    calls: list[tuple[str, str]] = []

    class _Maisaka:
        async def trigger_proactive(self, stream_id: str, intent: str, **kwargs: object) -> dict:
            calls.append((stream_id, intent))
            return {"success": True, "task_id": "proactive:test:1"}

    adapter._ctx = SimpleNamespace(maisaka=_Maisaka())

    async def _fake_react(text: str):
        return None

    async def _fake_speak(text: str) -> bool:
        return True

    adapter.react_expression = _fake_react  # type: ignore[method-assign]
    adapter.speak = _fake_speak  # type: ignore[method-assign]

    async def _run() -> None:
        await adapter.auto_speak_hook(
            message={"platform": "live", "processed_plain_text": "晚安都去睡吧。"}, stream_id=SID
        )
        for _ in range(20):
            await asyncio.sleep(0)
            if calls:
                break

    asyncio.run(_run())
    assert calls, "播完后没有下发主动任务：宿主 Planner 永远不会被唤醒"
    assert calls[0][0] == SID
    assert "接着说" in calls[0][1]


def test_proactive_waits_for_entire_batch() -> None:
    """主动开口本身也是 Planner 轮次：分批空窗期内同样不许插队。"""

    adapter = _adapter()
    calls: list[int] = []

    class _Director:
        def should_speak(self):
            calls.append(1)
            return True, "ok"

    adapter._proactive = _Director()
    adapter._speech_gate_until = time.monotonic() + 10.0
    asyncio.run(adapter._maybe_speak_proactively())
    assert calls == []

    adapter._speech_gate_until = 0.0
    adapter._live_stream_ids.clear()  # 没有已知直播流时会在 should_speak 后安全返回
    asyncio.run(adapter._maybe_speak_proactively())
    assert calls == [1]


def test_asr_interrupt_releases_gate() -> None:
    """主播按热键打断时，门控必须让路，否则他的话进不了 Planner。"""

    adapter = _adapter()
    adapter._mic = SimpleNamespace(recording=False, start=lambda: True)
    adapter._speech_pending = 3
    adapter._speech_gate_until = time.monotonic() + 30.0

    asyncio.run(adapter._asr_press())
    assert adapter._speech_generation == 1
    assert adapter._speech_pending == 0
    assert adapter._speech_gate_until == 0.0
    assert adapter._speech_interrupt.is_set()
    assert adapter._planner_blocked() is False


# --------------------------------------------------------------------- #
# 宿主 modified_kwargs 整体替换语义
# --------------------------------------------------------------------- #

def test_planner_hook_returns_complete_kwargs() -> None:
    adapter = _adapter()
    kwargs = {
        "session_id": SID,
        "items": [{"item_type": "SystemMessageItem", "parts": []}],
        "item_schema_version": 1,
        "tool_definitions": [
            {"type": "function", "function": {"name": "send_emoji"}},
            {"type": "function", "function": {"name": "reply"}},
        ],
        "selected_history_count": 3,
        "built_message_count": 4,
        "selection_reason": "recent",
    }
    result = asyncio.run(adapter.planner_tool_filter_hook(**kwargs))
    assert result is not None
    changed = result["modified_kwargs"]
    assert len(changed["tool_definitions"]) == 1
    assert changed["items"] is kwargs["items"]
    assert changed["item_schema_version"] == 1
    assert changed["session_id"] == SID
    assert changed["selection_reason"] == "recent"


def test_replyer_hooks_return_complete_kwargs() -> None:
    adapter = _adapter()
    adapter._arm_proactive_isolation(SID, time.time())

    hint = asyncio.run(
        adapter.proactive_reply_hint_hook(session_id=SID, extra_prompt="原本的提示", marker="keep")
    )
    assert hint is not None
    assert hint["modified_kwargs"]["marker"] == "keep"
    assert "原本的提示" in hint["modified_kwargs"]["extra_prompt"]

    items = [
        {
            "item_type": "UserMessageItem",
            "meta": {"item_id": "u1", "logical_turn_id": None, "timestamp": "2026-11-05T22:00:00"},
            "parts": [{"type": "text", "text": "[B站弹幕] 观众A: 讲个笑话"}],
        }
    ]
    ctx = asyncio.run(
        adapter.proactive_reply_context_hook(session_id=SID, items=items, marker="keep")
    )
    assert ctx is not None
    assert ctx["modified_kwargs"]["marker"] == "keep"
    assert ctx["modified_kwargs"]["session_id"] == SID
    assert "弹幕" not in ctx["modified_kwargs"]["items"][0]["parts"][0]["text"]


# --------------------------------------------------------------------- #
# 说完即收口：从源头不让宿主续跑下一轮 Planner
# --------------------------------------------------------------------- #

_TURN_ID = "turn-live-1"
_TS = "2026-11-05T23:00:00"


def _reply_output_items() -> list:
    """模拟 Planner 返回「思考正文 + reply 工具调用」的输出 Items。"""

    return [
        {
            "item_type": "AssistantMessageItem",
            "meta": {"item_id": "a" * 32, "logical_turn_id": _TURN_ID, "timestamp": _TS},
            "parts": [{"type": "text", "text": "我来讲个段子"}],
        },
        {
            "item_type": "FunctionCallItem",
            "meta": {"item_id": "b" * 32, "logical_turn_id": _TURN_ID, "timestamp": _TS},
            "tool_call": {
                "call_id": "call_reply_1",
                "func_name": "reply",
                "args": {"msg_id": "m1"},
                "extra_content": {"tool_call_source": "response"},
            },
        },
    ]


def _after_response_kwargs() -> dict:
    return {
        "session_id": SID,
        "output_items": _reply_output_items(),
        "item_schema_version": 1,
        "selected_history_count": 3,
        "built_message_count": 4,
        "selection_reason": "recent",
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "total_tokens": 110,
    }


def test_config_exposes_end_turn_switch() -> None:
    data = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    assert data["reply"]["end_turn_after_reply"] is True
    cfg = plugin.LiveDanmakuConfig.model_validate(data)
    assert cfg.reply.end_turn_after_reply is True


def test_append_stop_after_reply() -> None:
    items = _reply_output_items()
    new_items, added = plugin.append_stop_tool_call(items)
    assert added is True
    assert len(new_items) == len(items) + 1
    stop = new_items[-1]
    assert stop["item_type"] == "FunctionCallItem"
    assert stop["tool_call"]["func_name"] == "stop"
    assert stop["tool_call"]["args"] == {}
    # logical_turn_id 必须沿用本轮，否则宿主协议校验会报 missing_turn_call_ids
    assert stop["meta"]["logical_turn_id"] == _TURN_ID
    assert stop["meta"]["item_id"] not in {x["meta"]["item_id"] for x in items}
    assert stop["tool_call"]["call_id"] != items[1]["tool_call"]["call_id"]
    # 纯函数，不改原列表
    assert len(items) == 2


def test_append_stop_skips_when_no_reply() -> None:
    items = _reply_output_items()
    items[1]["tool_call"]["func_name"] = "tool_search"
    new_items, added = plugin.append_stop_tool_call(items)
    assert added is False and new_items is items
    assert plugin.append_stop_tool_call(None) == ([], False)


def test_retarget_proactive_reply_uses_current_task_id() -> None:
    """主动轮没有可回复的观众消息：把 reply 目标换成本轮 task_id。

    真机表现是「未找到要回复的目标消息，msg_id=maxeeau」→ 不发送 → 不播报，
    看起来像「回复工具未生成可见消息」，其实话已经生成过了。
    """

    items = _reply_output_items()
    items[1]["tool_call"]["args"]["reply_reference"] = "讲数独手感"
    new_items, fixed = plugin.retarget_proactive_reply(items, "proactive:test:9")
    assert fixed == 1
    args = new_items[1]["tool_call"]["args"]
    assert args["msg_id"] == "proactive:test:9"
    # 其它参数原样保留，别把模型写的回复方向弄丢
    assert args["reply_reference"] == "讲数独手感"
    # 正文 Item 不受影响，原列表也不被就地修改
    assert new_items[0] is items[0]
    assert items[1]["tool_call"]["args"]["msg_id"] == "m1"


def test_retarget_proactive_reply_is_noop_when_already_matched() -> None:
    items = _reply_output_items()
    items[1]["tool_call"]["args"]["msg_id"] = "proactive:test:9"
    new_items, fixed = plugin.retarget_proactive_reply(items, "proactive:test:9")
    assert fixed == 0 and new_items is items
    assert plugin.retarget_proactive_reply(items, "") == (items, 0)
    assert plugin.retarget_proactive_reply(None, "proactive:test:9") == ([], 0)


def test_append_stop_skips_when_model_already_paused() -> None:
    for index, pause_name in enumerate(("stop", "wait")):
        pause = {
            "item_type": "FunctionCallItem",
            "meta": {
                "item_id": f"pause{index}" * 4,
                "logical_turn_id": _TURN_ID,
                "timestamp": _TS,
            },
            "tool_call": {"call_id": f"call_{pause_name}", "func_name": pause_name, "args": {}},
        }
        new_items, added = plugin.append_stop_tool_call([*_reply_output_items(), pause])
        assert added is False and len(new_items) == 3


def test_appended_stop_passes_host_context_protocol() -> None:
    """宿主会把 hook 回来的 Items 反序列化并校验，构造必须完全合规。"""

    from src.llm_models.payload_content.context_item import (
        CONTEXT_ITEM_SCHEMA_VERSION,
        get_response_tool_calls,
    )
    from src.llm_models.payload_content.context_protocol import ContextProtocolMode
    from src.plugin_runtime.hook_payloads import deserialize_prompt_items

    new_items, added = plugin.append_stop_tool_call(_reply_output_items())
    assert added is True
    restored = deserialize_prompt_items(
        new_items,
        item_schema_version=CONTEXT_ITEM_SCHEMA_VERSION,
        mode=ContextProtocolMode.MODEL_OUTPUT,
        original_items=(),
    )
    # reply 先执行、stop 后执行 → 宿主 break 出内部循环，下一轮 Planner 不触发
    assert [call.func_name for call in get_response_tool_calls(restored)] == ["reply", "stop"]


def test_end_turn_hook_appends_stop_and_keeps_all_kwargs() -> None:
    kwargs = _after_response_kwargs()
    result = asyncio.run(_adapter().end_turn_after_reply_hook(**kwargs))
    assert result is not None
    changed = result["modified_kwargs"]
    for key, value in kwargs.items():
        if key != "output_items":
            assert changed[key] == value, f"丢了参数 {key}"
    assert len(changed["output_items"]) == 3
    assert changed["output_items"][-1]["tool_call"]["func_name"] == "stop"


def test_end_turn_hook_noop_on_non_live_disabled_or_no_reply() -> None:
    adapter = _adapter()
    kwargs = _after_response_kwargs()
    # 本地聊天室不受影响
    assert asyncio.run(
        adapter.end_turn_after_reply_hook(**{**kwargs, "session_id": "private:qq:123456"})
    ) is None
    # 开关关闭
    adapter.config.reply.end_turn_after_reply = False
    assert asyncio.run(adapter.end_turn_after_reply_hook(**kwargs)) is None
    adapter.config.reply.end_turn_after_reply = True
    # 本轮没有 reply（模型自己 wait/stop，或只查工具）
    no_reply = _reply_output_items()
    no_reply[1]["tool_call"]["func_name"] = "wait"
    assert asyncio.run(
        adapter.end_turn_after_reply_hook(**{**kwargs, "output_items": no_reply})
    ) is None


def main() -> int:
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
    print("整段回复 Planner 门控回归通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
