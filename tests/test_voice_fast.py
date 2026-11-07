# -*- coding: utf-8 -*-
"""语音快速通道：绕过 Planner 的直答链路。

这里不连宿主，用假上下文验证四件事：
  1. 生成用的是 Replyer（``maisaka.reply.generate``），不是 Planner；
  2. 主播的话和她的回答都会写回上下文，且 ``replyable=False``（不能反过来触发规划）；
  3. 台词里的动作记号会被摘掉再播报（这条链路不过 send 钩子）；
  4. 生成失败/空文本时不播报，也不污染历史。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import tomllib
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR))

_MODULE_NAME = "live_danmaku_voice_fast_test"
_spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
plugin = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = plugin
_spec.loader.exec_module(plugin)

SID = "live-all"


class _FakeReply:
    """假的 Replyer：记录调用参数，按预设返回。"""

    def __init__(self, text: str = "嗯，我在呢", success: bool = True) -> None:
        self.text = text
        self.success = success
        self.calls: list = []

    async def generate(self, stream_id, reference, **kwargs):
        self.calls.append({"stream_id": stream_id, "reference": reference, **kwargs})
        if not self.success:
            return {"success": False, "error": "模型没响应"}
        return {"success": True, "text": self.text, "selected_expression_ids": []}


class _FakeContext:
    """假的上下文写入：记录每一条。"""

    def __init__(self) -> None:
        self.appended: list = []

    async def append(self, stream_id, segments, **kwargs):
        self.appended.append({"stream_id": stream_id, "segments": segments, **kwargs})
        return {"success": True}


class _FakeMaisaka:
    def __init__(self, reply: _FakeReply) -> None:
        self.reply = reply
        self.context = _FakeContext()

    async def append_context(self, stream_id, segments, **kwargs):
        return await self.context.append(stream_id, segments, **kwargs)


class _FakeCtx:
    def __init__(self, reply: _FakeReply) -> None:
        self.maisaka = _FakeMaisaka(reply)
        self.logger = logging.getLogger("test.voice")


def _adapter(reply: _FakeReply | None = None, *, actions=None):
    """构造一个只跑快速通道的适配器，其余外部依赖全部打桩。"""

    data = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    adapter = plugin.LiveDanmakuAdapter()
    adapter._plugin_config_instance = plugin.LiveDanmakuConfig.model_validate(data)
    fake_reply = reply or _FakeReply()
    adapter._ctx = _FakeCtx(fake_reply)  # type: ignore[assignment]
    adapter._latest_live_stream_id = lambda: SID  # type: ignore[method-assign]
    adapter._action_names = lambda: set(actions or ())  # type: ignore[method-assign]

    spoken: list = []

    async def _speak(text):
        spoken.append(text)
        return True

    async def _noop(*a, **kw):
        return None

    adapter.speak = _speak  # type: ignore[method-assign]
    adapter.react_expression = _noop  # type: ignore[method-assign]
    adapter._run_actions = _noop  # type: ignore[method-assign]
    return adapter, fake_reply, spoken


def test_bypass_uses_replyer_not_planner() -> None:
    """走的是 maisaka.reply.generate，材料里带着刚说的那句话。"""

    adapter, reply, spoken = _adapter()
    asyncio.run(adapter._voice_reply_fast("今天直播有点累", "主人"))

    assert len(reply.calls) == 1
    call = reply.calls[0]
    assert call["stream_id"] == SID
    assert "今天直播有点累" in call["reference"], "刚说的那句没进写作材料"
    assert call["format_instruction"], "必须给写作要求，否则 Replyer 会按「长回复」写一大段"
    assert spoken == ["嗯，我在呢"]


def test_both_sides_written_back_but_not_replyable() -> None:
    """主播的话和她的回答都写回上下文，且都不触发回复。"""

    adapter, _reply, _spoken = _adapter()
    asyncio.run(adapter._voice_reply_fast("在吗", "主人"))

    appended = adapter._ctx.maisaka.context.appended
    assert len(appended) == 2, "应该写入「主播说的」和「她答的」两条"
    assert appended[0]["replyable"] is False
    assert appended[1]["replyable"] is False
    assert "在吗" in appended[0]["segments"][0]["data"]
    assert appended[1]["segments"][0]["data"].endswith("嗯，我在呢")


def test_markers_stripped_before_speaking() -> None:
    """快速通道不过 send 钩子，记号必须自己摘，否则会被念出来。"""

    adapter, _reply, spoken = _adapter(
        _FakeReply("呜……你别凶我【流泪】"), actions={"流泪", "委屈"}
    )
    asyncio.run(adapter._voice_reply_fast("你怎么哭了", "主人"))

    assert spoken == ["呜……你别凶我"], f"记号没摘干净：{spoken}"


def test_failure_does_not_speak_or_pollute_history() -> None:
    """生成失败时不播报，也不该把半截对话留在历史里。"""

    adapter, _reply, spoken = _adapter(_FakeReply(success=False))
    asyncio.run(adapter._voice_reply_fast("喂喂喂", "主人"))

    assert spoken == []
    appended = adapter._ctx.maisaka.context.appended
    # 只写了主播那一句（作为上下文），没有把失败的空回复写进去
    assert len(appended) == 1
    assert not any("我：" in item["segments"][0]["data"] for item in appended)


def test_history_is_carried_into_next_reference() -> None:
    """第二轮要把第一轮带进去，否则她每句话都像失忆重来。"""

    adapter, reply, _spoken = _adapter()
    asyncio.run(adapter._voice_reply_fast("我叫小明", "主人"))
    asyncio.run(adapter._voice_reply_fast("我叫什么", "主人"))

    second_reference = reply.calls[1]["reference"]
    assert "我叫小明" in second_reference, "上一轮的话没带上"
    assert "我叫什么" in second_reference


def main() -> int:
    test_bypass_uses_replyer_not_planner()
    test_both_sides_written_back_but_not_replyable()
    test_markers_stripped_before_speaking()
    test_failure_does_not_speak_or_pollute_history()
    test_history_is_carried_into_next_reference()
    print("语音快速通道通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
