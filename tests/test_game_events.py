# -*- coding: utf-8 -*-
"""游戏事件素材回归：事件接收、素材整理、意图分叉、推送端口。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_MODULE_NAME = "live_danmaku_adapter_game_events_test"

spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
plugin = importlib.util.module_from_spec(spec)
sys.modules[_MODULE_NAME] = plugin
spec.loader.exec_module(plugin)

GameFeed = plugin.GameFeed
GameEventInbox = plugin.GameEventInbox
ProactiveDirector = plugin.ProactiveDirector


def _payload() -> dict:
    return {
        "ok": True,
        "active": "sudoku",
        "title": "数独",
        "mode": "ai",
        "playing": True,
        "finished": False,
        "progress": 42.0,
        "step": 18,
        "difficulty": "easy",
        "difficulty_label": "简单",
        "remaining": 40,
        "recent": [
            {"step": 16, "value": 5, "reason": "only-candidate", "progress": 40.0, "finished": False},
            {"step": 17, "value": 3, "reason": "hidden-single", "progress": 41.0, "finished": False},
        ],
    }


def test_summary_empty_before_any_event() -> None:
    """一条事件都没收到：不能编造游戏内容。"""

    assert GameFeed().summary() == ""


def test_summary_empty_when_idle() -> None:
    """收到「没在玩」的事件时也不该给素材。"""

    feed = GameFeed()
    feed.update({"ok": True, "active": None, "playing": False, "finished": False})
    assert feed.summary() == ""


def test_summary_is_only_a_scene() -> None:
    """只交代「在玩什么」，不报进度/步数/技巧，避免模型照着念成数据播报。"""

    feed = GameFeed()
    feed.update(_payload())
    text = feed.summary()
    assert "数独" in text
    assert "简单" in text
    # 数字与技巧名一律不进提示
    assert "42%" not in text
    assert "18 步" not in text
    assert "填入" not in text
    assert "唯一候选" not in text
    assert "最近的动作" not in text
    assert "message" not in text


def test_summary_for_finished_game() -> None:
    """解完时只说「玩完了」，不报总步数。"""

    feed = GameFeed()
    payload = _payload()
    payload.update({"playing": False, "finished": True, "progress": 100.0, "step": 45})
    feed.update(payload)
    text = feed.summary()
    assert "玩完" in text
    assert "正在直播玩" not in text
    assert "45" not in text
    # 结束语只给一次，之后不再重复
    feed.mark_announced()
    assert feed.summary() == ""


def test_update_reports_new_activity_only_on_change() -> None:
    """同一步重复推送不算新事件，版本号只在真有变化时增加。"""

    feed = GameFeed()
    payload = _payload()
    assert feed.update(payload) is True
    assert feed.version == 1
    assert feed.update(dict(payload)) is False
    assert feed.version == 1
    payload["step"] = 19
    assert feed.update(payload) is True
    assert feed.version == 2


def test_intent_without_game_keeps_freeform_story() -> None:
    """没游戏时保持原来的自由发挥，不出现游戏字样。"""

    text = ProactiveDirector().build_intent("")
    assert "抽象" in text
    assert "解说" not in text


def test_intent_with_game_does_not_coach() -> None:
    """有游戏时只补一句场景，不给解说角度、不给二选一，避免像提词器。"""

    feed = GameFeed()
    feed.update(_payload())
    text = ProactiveDirector().build_intent(feed.summary())
    assert "数独" in text
    assert "必须说话" in text
    assert "解说" not in text
    assert "聊点别的内容" not in text
    assert "两条路" not in text


def test_intent_with_schedule_demands_real_action() -> None:
    """日程档必须变成「先动手用工具」的意图，而不是继续讲小故事。"""

    text = ProactiveDirector().build_intent("", schedule_focus="去b站看木几萌")
    assert "去b站看木几萌" in text
    assert "tool_search" in text
    assert "打开网页" in text
    assert "browser_snapshot" in text
    assert "别反复导航" in text
    assert "这一轮必须说出一句话" in text
    assert "别凭空编" in text
    # 不能退化成自由发挥的话术，也不能自相矛盾地要求直接 stop
    assert "抽象搞笑" not in text
    assert "这一轮不要直接 stop" in text


def test_schedule_intent_wins_over_game_scene() -> None:
    """日程优先于游戏：有安排时不要又把它带回去解说数独。"""

    feed = GameFeed()
    feed.update(_payload())
    text = ProactiveDirector().build_intent(feed.summary(), schedule_focus="去b站看名侦探柯南")
    assert "去b站看名侦探柯南" in text
    assert "数独" not in text


def test_schedule_rule_requires_tool_use() -> None:
    """浏览器可用时，主动轮尾部规则要写清「先调工具」，否则模型只会直接 stop。"""

    rule = plugin.proactive_schedule_rule("去b站看木几萌", browser_available=True)
    assert "去b站看木几萌" in rule
    assert "browser_navigate" in rule
    assert "browser_snapshot" in rule
    assert "stop" in rule


def test_schedule_rule_drops_browser_guidance_when_unavailable() -> None:
    """浏览器工具本轮被摘掉时，绝不能再让模型去搜它（否则会无限 tool_search）。"""

    rule = plugin.proactive_schedule_rule("去b站看木几萌", browser_available=False)
    assert "去b站看木几萌" in rule
    # 关键：不能出现任何「去用浏览器 / 去搜工具」的指令
    assert "tool_search" not in rule
    assert "browser_navigate" not in rule
    assert "browser_snapshot" not in rule
    # 必须明确要求这一轮开口
    assert "开口" in rule


def test_strip_deferred_tools_reminder_removes_reminder_only() -> None:
    """宿主那条「可以用 tool_search 发现工具」的提示要能被整段摘掉。"""

    items = [
        {"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "正常 system"}]},
        {
            "item_type": "UserMessageItem",
            "parts": [
                {
                    "type": "text",
                    "text": "<system-reminder>\n以下工具当前未直接暴露给你，可以通过 tool_search 发现…\n</system-reminder>",
                }
            ],
        },
        {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "普通观众消息"}]},
    ]
    stripped_items, count = plugin.strip_deferred_tools_reminder(items)
    assert count == 1
    assert len(stripped_items) == 2
    joined = str(stripped_items)
    assert "system-reminder" not in joined
    assert "普通观众消息" in joined
    assert "正常 system" in joined


def test_strip_deferred_tools_reminder_keeps_other_parts() -> None:
    """同一条消息里若还有别的文本 part，只删提示、保留其余。"""

    items = [
        {
            "item_type": "UserMessageItem",
            "parts": [
                {"type": "text", "text": "重要上下文"},
                {"type": "text", "text": "<system-reminder>tool_search…</system-reminder>"},
            ],
        }
    ]
    stripped_items, count = plugin.strip_deferred_tools_reminder(items)
    assert count == 1
    assert len(stripped_items) == 1
    parts = stripped_items[0]["parts"]
    assert len(parts) == 1
    assert parts[0]["text"] == "重要上下文"


def test_event_inbox_receives_push() -> None:
    """收件箱能启动、能收 POST、并把事件交给回调。"""

    received: list = []

    async def main() -> None:
        inbox = GameEventInbox(port=0, on_event=received.append)
        port = await inbox.start()
        body = json.dumps(_payload(), ensure_ascii=False).encode("utf-8")

        def post() -> None:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/game-event",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=3).read()

        await asyncio.to_thread(post)
        await inbox.stop()

    asyncio.run(main())
    assert len(received) == 1
    assert received[0]["step"] == 18


def test_event_inbox_rejects_bad_payload() -> None:
    """坏 JSON 返回 400，不会把回调打崩。"""

    received: list = []

    async def main() -> None:
        inbox = GameEventInbox(port=0, on_event=received.append)
        port = await inbox.start()
        assert port > 0

        def post() -> int:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/game-event",
                data=b"not-json",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=3)
                return 200
            except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
                return int(exc.code)

        status = await asyncio.to_thread(post)
        await inbox.stop()
        assert status == 400

    asyncio.run(main())
    assert received == []


def test_config_exposes_event_fields() -> None:
    """配置项必须可被 WebUI 看到：事件推送开启、端口非零。"""

    raw = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    proactive = raw["proactive"]
    assert proactive["game_events"] is True
    assert int(proactive["event_port"]) >= 1024


def test_config_exposes_followup_turns() -> None:
    """「回完话后自说几轮」必须落在配置里，且能通过 schema 校验。"""

    raw = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    proactive = raw["proactive"]
    assert int(proactive["followup_turns"]) >= 0
    cfg = plugin.LiveDanmakuConfig.model_validate(raw)
    assert cfg.proactive.followup_turns == int(proactive["followup_turns"])


def main() -> int:
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
    print("游戏事件素材回归通过")
    return 0


def _quiet_adapter() -> object:
    """只测「播完要不要开口」的判定，绕开完整初始化。"""

    adapter = plugin.LiveDanmakuAdapter.__new__(plugin.LiveDanmakuAdapter)
    adapter._proactive = ProactiveDirector()
    adapter._game_feed = None
    adapter._schedule_focus = ""
    adapter._consumed_game_version = 0
    adapter._followup_turns_left = 0
    adapter.scheduled: list = []  # type: ignore[attr-defined]
    adapter.followup_flags: list = []  # type: ignore[attr-defined]

    def _schedule(reason: str, **kwargs: object) -> None:
        adapter.scheduled.append(reason)
        adapter.followup_flags.append(bool(kwargs.get("followup", False)))

    adapter._schedule_proactive = _schedule  # type: ignore[attr-defined]
    return adapter


def test_finish_speech_stays_quiet_without_event() -> None:
    """既没新事件、也没续聊额度时，播完就安静——不能靠「说完」永久自续。"""

    adapter = _quiet_adapter()
    adapter._mark_speech_finished()
    assert adapter.scheduled == []


def test_finish_speech_follows_up_after_answering_audience() -> None:
    """回完观众的话：播完必须把 Planner 再叫起来，让她自己接着找话题。

    这就是真机上的 bug：整段回复播完后没有任何触发点，trigger_proactive
    一次都没下发，宿主 Planner 永远不会被唤醒。
    """

    adapter = _quiet_adapter()
    adapter._followup_turns_left = 1
    adapter._mark_speech_finished()
    assert adapter.scheduled == ["回复完接着聊"]
    assert adapter.followup_flags == [True]
    # 额度已消费：自语段播完不会自己给自己续下一轮
    assert adapter._followup_turns_left == 0


def test_followup_budget_only_lasts_one_turn() -> None:
    """自语段播完不再续额度，链长有上限，不会变成「音频多长、话就多密」。"""

    adapter = _quiet_adapter()
    adapter._followup_turns_left = 1
    adapter._mark_speech_finished()  # 回应观众的段播完 -> 续聊一轮
    adapter._mark_speech_finished()  # 续聊段的语音播完 -> 必须收口
    assert adapter.scheduled == ["回复完接着聊"]


def test_game_event_takes_priority_over_followup() -> None:
    """有游戏进展时优先讲进展，续聊额度留到下一次再用。"""

    adapter = _quiet_adapter()
    feed = GameFeed()
    feed.update(_payload())
    adapter._game_feed = feed
    adapter._followup_turns_left = 1
    adapter._mark_speech_finished()
    assert adapter.scheduled and "播完" in adapter.scheduled[0]
    assert adapter._followup_turns_left == 1


def test_finish_speech_triggers_when_game_moved() -> None:
    """播报期间游戏推进了：播完可以接着说，因为确实有新内容。"""

    adapter = _quiet_adapter()
    feed = GameFeed()
    feed.update(_payload())
    adapter._game_feed = feed
    adapter._mark_speech_finished()
    assert adapter.scheduled and "播完" in adapter.scheduled[0]


def test_consumed_version_prevents_repeat() -> None:
    """同一批进展讲过一次就消费掉，再播完不会拿它重复开口。"""

    adapter = _quiet_adapter()
    feed = GameFeed()
    feed.update(_payload())
    adapter._game_feed = feed
    adapter._consumed_game_version = feed.version
    adapter._mark_speech_finished()
    assert adapter.scheduled == []

    # 游戏又走一步 -> 新事件 -> 再次允许开口
    payload = _payload()
    payload["step"] = 19
    feed.update(payload)
    adapter._mark_speech_finished()
    assert adapter.scheduled and "播完" in adapter.scheduled[0]


# --------------------------------------------------------------------- #
# 日程换挡事件：也走事件流触发主动开口，但不污染游戏素材
# --------------------------------------------------------------------- #


def test_note_external_event_keeps_game_scene() -> None:
    """日程事件只推版本号，不能把正在解说的游戏素材冲掉。"""

    feed = GameFeed()
    feed.update(_payload())
    before = feed.summary()
    version = feed.version
    feed.note_external_event()
    assert feed.version == version + 1
    assert feed.summary() == before


def test_schedule_event_triggers_proactive_without_overwriting_game() -> None:
    """收到日程换挡事件：排一次开口、版本前进、游戏素材不变。"""

    adapter = _quiet_adapter()
    feed = GameFeed()
    feed.update(_payload())
    adapter._game_feed = feed
    before = feed.summary()
    version = feed.version

    adapter._on_game_event(
        {
            "kind": "schedule",
            "name": "娱乐档",
            "focus": "去b站看木几萌",
            "step": 123,
        }
    )

    assert adapter.scheduled == ["日程换挡"]
    assert feed.version == version + 1
    assert feed.summary() == before


def test_schedule_event_survives_playback_gate() -> None:
    """换挡时正在播报被挡下：播完仍能补触发，事件不会被吞掉。"""

    adapter = _quiet_adapter()
    feed = GameFeed()
    feed.update(_payload())
    adapter._game_feed = feed
    adapter._consumed_game_version = feed.version
    adapter.scheduled.clear()

    adapter._on_game_event({"kind": "schedule", "focus": "去b站看木几萌"})
    adapter.scheduled.clear()  # 模拟当次被「等说完再想」挡下，没能真正开口
    adapter._mark_speech_finished()
    assert adapter.scheduled and "播完" in adapter.scheduled[0]


def test_schedule_event_keeps_focus_for_the_intent() -> None:
    """focus 不能像以前那样被丢掉：主动轮要靠它知道该去做什么。"""

    adapter = _quiet_adapter()
    adapter._on_game_event(
        {
            "kind": "schedule",
            "name": "娱乐档",
            "focus": "去b站看名侦探柯南",
        }
    )
    assert adapter._schedule_focus == "去b站看名侦探柯南"
    intent = adapter._proactive.build_intent("", schedule_focus=adapter._schedule_focus)
    assert "去b站看名侦探柯南" in intent
    assert "tool_search" in intent


def test_schedule_event_without_game_feed_still_arrives() -> None:
    """关掉游戏素材时事件端口依然要收日程，不能因为 feed=None 直接 return。"""

    adapter = _quiet_adapter()
    adapter._game_feed = None
    adapter._on_game_event({"kind": "schedule", "focus": "去b站看木几萌"})
    assert adapter._schedule_focus == "去b站看木几萌"
    assert adapter.scheduled == ["日程换挡"]


def test_schedule_guidance_targets_browser_mcp_only() -> None:
    """刷B站插件已删除：引导只能指向 playwright MCP 浏览器工具。"""

    text = ProactiveDirector().build_intent("", schedule_focus="去b站看木几萌")
    rule = plugin.proactive_schedule_rule("去b站看木几萌", browser_available=True)
    for block in (text, rule):
        assert "browser_snapshot" in block
        # 不能再让模型去找一个已经不存在的插件工具
        assert "bilibili_watch" not in block
        assert "bilibili_browse" not in block
        assert "刷B站" not in block
    # 只有 ProactiveDirector 的 intent 还保留「没有就 tool_search 搜」的说法；
    # 插件的尾部规则不再提 tool_search —— 浏览器被摘掉时那条指引会让模型
    # 反复搜索一个自己已经拿不到的工具（见 test_schedule_rule_drops_browser_guidance_when_unavailable）。
    assert "tool_search" in text
    assert "tool_search" not in rule


def test_followup_intent_continues_previous_line() -> None:
    """续聊轮的意图必须写「接着说」，不能又叫她另起一段新故事（会横跳/stop）。"""

    intent = ProactiveDirector().build_intent("", followup=True)
    assert "接着说" in intent
    assert "小故事" not in intent

    with_game = ProactiveDirector().build_intent("正在玩：数独，进度 42%", followup=True)
    assert "数独" in with_game and "接着说" in with_game

    # 日程安排优先级更高：续聊轮也不例外
    scheduled = ProactiveDirector().build_intent("", schedule_focus="去b站看木几萌", followup=True)
    assert "去b站看木几萌" in scheduled


def test_schedule_event_inbox_marks_kind() -> None:
    """专用端点能收日程事件，并自动打上来源标记。"""

    received: list = []

    async def main() -> None:
        inbox = GameEventInbox(port=0, on_event=received.append)
        port = await inbox.start()
        body = json.dumps({"name": "娱乐档", "focus": "去b站看木几萌"}, ensure_ascii=False).encode("utf-8")

        def post() -> None:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/schedule-event",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=3).read()

        await asyncio.to_thread(post)
        await inbox.stop()

    asyncio.run(main())
    assert len(received) == 1
    assert received[0]["kind"] == "schedule"
    assert received[0]["focus"] == "去b站看木几萌"


def test_schedule_hub_config_points_to_event_endpoint() -> None:
    """日程已合并进直播适配器：时段表在本插件 config.toml 里，事件端口仍保留。

    换挡事件已改为直连（不再走 HTTP 自环），但 /schedule-event 端点保留，
    外部脚本仍然可以往它 POST 触发主动开口。
    """

    raw = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    schedule = raw["schedule"]
    assert schedule["trigger_on_change"] is True
    assert schedule["periods"], "合并后 config.toml 必须带着时段表"
    own_ports = raw["proactive"]["event_port"]
    assert own_ports > 0


# --------------------------------------------------------------------- #
# 纯事件驱动：开局没有弹幕时也要能找到直播聊天流
# --------------------------------------------------------------------- #

from types import SimpleNamespace  # noqa: E402


class _FakeChat:
    """模拟宿主会话表；记录查询参数，可指定返回或抛错。"""

    def __init__(self, streams=None, error=None, delay=0.0):
        self.streams = streams if streams is not None else []
        self.error = error
        self.delay = delay
        self.calls: list = []

    async def get_all_streams(self, platform: str = "qq"):
        self.calls.append(platform)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.streams


def _stream_adapter(streams=None, error=None):
    adapter = plugin.LiveDanmakuAdapter.__new__(plugin.LiveDanmakuAdapter)
    adapter._live_stream_ids = {}
    adapter._last_stream_id = ""
    adapter._reply_adjust_dirty = False
    adapter._plugin_config_instance = SimpleNamespace(
        tts=SimpleNamespace(auto_speak_platforms="live"),
    )
    adapter._ctx = SimpleNamespace(chat=_FakeChat(streams, error))
    return adapter


def test_resolve_stream_uses_learned_id_without_query() -> None:
    """已经从弹幕学到流 ID：直接用，不去打扰宿主。"""

    adapter = _stream_adapter([{"stream_id": "host-stream"}])
    adapter._remember_live_stream("learned-stream")
    assert asyncio.run(adapter._resolve_live_stream_id()) == "learned-stream"
    assert adapter.ctx.chat.calls == []


def test_resolve_stream_uses_configured_group_id(monkeypatch) -> None:
    """CHANNEL_GROUP_ID 写死为空（走 live-all）；若哪天改成固定 ID，必须短路宿主查询。"""

    assert plugin.CHANNEL_GROUP_ID == "", "默认应为空（合并通道用 live-all）"
    monkeypatch.setattr(plugin, "CHANNEL_GROUP_ID", "fixed-live")
    adapter = _stream_adapter([{"stream_id": "host-stream"}])
    assert asyncio.run(adapter._resolve_live_stream_id()) == "fixed-live"
    assert "fixed-live" in adapter._live_stream_ids
    assert adapter.ctx.chat.calls == []


def test_resolve_stream_discovers_from_host_by_group_name() -> None:
    """重启后一个弹幕都没有：从宿主会话表按显示名找到直播流并记住。"""

    adapter = _stream_adapter(
        [
            {"stream_id": "qq-local", "group_name": "本地聊天室"},
            {"stream_id": "live-room", "group_name": "直播间弹幕"},
        ]
    )
    assert asyncio.run(adapter._resolve_live_stream_id()) == "live-room"
    assert adapter.ctx.chat.calls == ["live"]
    assert "live-room" in adapter._live_stream_ids
    assert adapter._reply_adjust_dirty is True
    # 第二次不再查宿主
    assert asyncio.run(adapter._resolve_live_stream_id()) == "live-room"
    assert len(adapter.ctx.chat.calls) == 1


def test_resolve_stream_falls_back_to_last_live_stream() -> None:
    """会话表没带 group_name 时也不能卡死：平台已限定 live，取最后一个。"""

    adapter = _stream_adapter([{"session_id": "live-a"}, {"session_id": "live-b"}])
    assert asyncio.run(adapter._resolve_live_stream_id()) == "live-b"


def test_resolve_stream_query_failure_is_quiet() -> None:
    """宿主会话查询失败：安静返回空串，不能把事件回调打崩。"""

    adapter = _stream_adapter(error=RuntimeError("ipc down"))
    assert asyncio.run(adapter._resolve_live_stream_id()) == ""


def test_outbound_message_teaches_live_stream_id() -> None:
    """弹幕源关掉时没有入站消息，AI 每说一次话也要能补回流 ID。"""

    adapter = _stream_adapter()
    adapter._learn_live_stream_from_outbound({"platform": "live"}, "6617d187-live")
    assert "6617d187-live" in adapter._live_stream_ids
    assert adapter._reply_adjust_dirty is True
    # 学到之后主动对话不再需要查宿主（能力可能没授权）
    assert asyncio.run(adapter._resolve_live_stream_id()) == "6617d187-live"
    assert adapter.ctx.chat.calls == []


def test_outbound_message_ignores_other_platforms() -> None:
    """别的平台的出站消息不能被误记成直播流。"""

    adapter = _stream_adapter()
    adapter._learn_live_stream_from_outbound({"platform": "qq"}, "qq-stream")
    assert adapter._live_stream_ids == {}
    adapter._learn_live_stream_from_outbound(None, "no-message")
    assert adapter._live_stream_ids == {}


def test_resolve_stream_lookup_times_out() -> None:
    """IPC 挂住时必须限时退出，否则事件回调链会被一个卡住的任务永久堵死。"""

    original = plugin.STREAM_LOOKUP_TIMEOUT
    plugin.STREAM_LOOKUP_TIMEOUT = 0.02
    try:
        adapter = _stream_adapter()
        adapter._ctx.chat.delay = 1.0
        started = time.monotonic()
        assert asyncio.run(adapter._resolve_live_stream_id()) == ""
        assert time.monotonic() - started < 0.5
    finally:
        plugin.STREAM_LOOKUP_TIMEOUT = original


def _adapter_with_table():
    """构造一个带日程表的适配器（配置读插件 config.toml）。"""

    data = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    adapter = plugin.LiveDanmakuAdapter()
    adapter._plugin_config_instance = plugin.LiveDanmakuConfig.model_validate(data)
    adapter._schedule_table = adapter._build_schedule_table()
    return adapter


def test_schedule_hub_injection_keeps_all_kwargs() -> None:
    """日程注入必须整体替换 kwargs：漏掉 session_id 会打挂后面的 Hook。

    合并前测的是 schedule-hub 插件的钩子；合并后同一个契约由
    adapter 的 ``schedule_planner_inject_hook`` 履行。
    """

    adapter = _adapter_with_table()
    items = [{"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "基础人设"}]}]
    result = asyncio.run(
        adapter.schedule_planner_inject_hook(
            items=items,
            item_schema_version=3,
            tool_definitions=[{"name": "browser_navigate"}],
            session_id="live-stream-1",
        )
    )
    assert result is not None
    changed = result["modified_kwargs"]
    assert changed["session_id"] == "live-stream-1"
    assert changed["tool_definitions"] == [{"name": "browser_navigate"}]
    assert changed["item_schema_version"] == 3
    # 缓存契约（2026-11-08 更改）：插件**完全不碰 system**。
    # 原来这里会把稳定段追加进 system 的 parts 末尾，但宿主在部分路径下
    # items 里没有 SystemMessageItem，注入 fallback 会新建一条「只含日程」的
    # system 顶到第 0 条，于是 prompt 第 0 条在「人格段」/「日程段」之间
    # 反复横跳，最长公共前缀只剩 0.2%~35%，命中率被压在 50%。
    # 现在稳定段也一起走尾部（_append_schedule_tail），system 一个字节不动。
    system_text = "\n".join(
        str(pt.get("text") or "")
        for pt in (changed["items"][0].get("parts") or []) if isinstance(pt, dict)
    )
    assert "日程表（必须遵守）" not in system_text, "system 必须保持原样（缓存前缀）"
    assert "基础人设" in system_text, "人格段不能被替换掉"
    all_text = "\n".join(
        str(pt.get("text") or "")
        for item in changed["items"] if isinstance(item, dict)
        for pt in (item.get("parts") or []) if isinstance(pt, dict)
    )
    assert "日程表（必须遵守）" in all_text, "日程稳定段仍须在本轮上下文里生效"
    assert "当前时段" in all_text, "当前档行在尾部"


def test_typing_delay_hook_is_registered_not_the_helper() -> None:
    """装饰器挂错会让宿主把内部 helper 当 hook 调用并传 hook_name 报错。

    真机日志：`live_stream_no_typing_delay 执行异常:
    _learn_live_stream_from_outbound() got an unexpected keyword argument 'hook_name'`。
    """

    cls = plugin.LiveDanmakuAdapter
    info = getattr(cls.typing_delay_hook, "__maibot_component_info__", None)
    assert info is not None, "typing_delay_hook 没注册，断句打字等待会恢复 0.3 秒/字"
    assert info.hook == "send_service.after_build_message"
    assert getattr(cls._learn_live_stream_from_outbound, "__maibot_component_info__", None) is None


def test_schedule_mode_drops_story_rule() -> None:
    """日程档不能同时注入「讲抽象搞笑小故事」和「按安排打开网页」。

    真机复现：日程事件后 AI 打开了 B 站、读了 depth=4 快照，却因为上下文里
    两套规则打架，最后一轮直接 stop，一句话没说。
    """

    strip = plugin.strip_live_context_items
    items = [
        {
            "item_type": "UserMessageItem",
            "meta": {"item_id": "rule"},
            "parts": [
                {
                    "type": "text",
                    "text": "【弹幕回复规则】回复弹幕时，要先读一遍弹幕，再给出回应。",
                }
            ],
        },
        {
            "item_type": "SystemMessageItem",
            "meta": {"item_id": "sys"},
            "parts": [
                {
                    "type": "text",
                    "text": "你的名字是Evil。【弹幕回复规则】回复弹幕时，要先读一遍弹幕。",
                }
            ],
        },
    ]

    plain, n1 = strip(items)
    assert n1 == 1  # system 不再改写（保住前缀缓存），只清 UserMessage
    assert "抽象搞笑" in json.dumps(plain, ensure_ascii=False)

    scheduled, n2 = strip(items, schedule_mode=True)
    assert n2 == 1
    text = json.dumps(scheduled, ensure_ascii=False)
    assert "抽象搞笑" not in text
    assert "按节目安排" in text
    # 默认参数不变，老的主动自言自语行为不能被改坏
    again, _ = strip(items)
    assert again == plain


def test_proactive_tool_chain_detection() -> None:
    """工具链没跑完时不该提前解除主动隔离。"""

    f = plugin.items_have_proactive_tool_chain
    assert f(None) is False
    assert f([]) is False
    assert f([{"item_type": "UserMessageItem", "parts": []}]) is False

    chain = [
        {
            "item_type": "FunctionCallItem",
            "tool_call": {"func_name": "browser_navigate", "args": {}},
        },
        {
            "item_type": "FunctionCallOutputItem",
            "tool_name": "browser_navigate",
            "output": "ok",
        },
        {
            "item_type": "FunctionCallItem",
            "tool_call": {"func_name": "browser_snapshot", "args": {"depth": 4}},
        },
        {
            "item_type": "FunctionCallOutputItem",
            "tool_name": "browser_snapshot",
            "output": "page",
        },
    ]
    assert f(chain) is True
    # 已经收口成 reply 的轮次不算工具链
    assert f(chain + [{"item_type": "FunctionCallItem", "tool_call": {"func_name": "reply", "args": {}}}]) is False
    assert f([{"item_type": "FunctionCallOutputItem", "tool_name": "stop", "output": ""}]) is False


if __name__ == "__main__":
    raise SystemExit(main())
