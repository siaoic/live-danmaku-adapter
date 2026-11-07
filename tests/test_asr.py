# -*- coding: utf-8 -*-
"""ASR 模块回归：wav 打包、事件来源、通道直通入口、语音弹幕前缀解析。"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import sys
import wave
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_MODULE_NAME = "live_danmaku_adapter_asr_test"

spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[_MODULE_NAME] = module
spec.loader.exec_module(module)

from live_danmaku_adapter_asr_test.asr import pcm_to_wav  # noqa: E402
from live_danmaku_adapter_asr_test.core.channel import LiveChannel  # noqa: E402
from live_danmaku_adapter_asr_test.core.events import SOURCE_ASR, LiveEvent  # noqa: E402
from live_danmaku_adapter_asr_test.core.mapping import (  # noqa: E402
    CHANNEL_ACCOUNT_ID,
    CHANNEL_PLATFORM,
    ChannelSettings,
    build_message,
)
from live_danmaku_adapter_asr_test.danmaku import parse_inbound  # noqa: E402


def test_pcm_to_wav_frames() -> None:
    """裸 PCM 打包成 wav：头字段与帧数都要对。"""

    pcm = b"\x00\x00" * 160  # 0.01s @16k 单声道
    wav = pcm_to_wav(pcm, 16000)
    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getframerate() == 16000
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getnframes() == 160


def test_asr_source_label() -> None:
    """ASR 来源在消息文本里的标签是「语音」。"""

    event = LiveEvent(
        source=SOURCE_ASR, room_id="local", user_id="host", user_name="主人",
        kind="danmaku", text="你好",
    )
    assert event.source_label == "语音"


def test_submit_voice_enqueues_bypassing_filter() -> None:
    """submit_voice 直通入队：空文本拒绝、队列满拒绝、字段完整。"""

    channel = LiveChannel(ctx=None, gateway_name="test")
    channel._queue = asyncio.Queue(maxsize=1)
    assert channel.submit_voice("你好", "主人")
    event = channel._queue.get_nowait()
    assert event.source == "asr"
    assert event.user_id == "host"
    assert event.user_name == "主人"
    assert event.kind == "danmaku"
    assert event.text == "你好"
    assert event.event_id.startswith("asr-")
    assert event.mention == 1.0
    # 空文本拒绝
    assert not channel.submit_voice("   ", "主人")
    # 队列满拒绝（不阻塞、不抛异常）
    assert channel.submit_voice("占位")
    assert not channel.submit_voice("进不来")


def test_voice_message_forces_reply_and_not_danmaku() -> None:
    """主播语音的消息必须带数值型 is_mentioned（强制回复），且打上 live_asr 标记。"""

    channel = LiveChannel(ctx=None, gateway_name="test")
    channel._queue = asyncio.Queue(maxsize=4)
    assert channel.submit_voice("今天天气怎么样", "主播")
    event = channel._queue.get_nowait()
    message = build_message(event, 1, ChannelSettings())
    extra = message["message_info"]["additional_config"]
    # 数值型 is_mentioned >= 1.0：宿主 is_mentioned_bot_in_message 强制下一轮回复
    assert extra.get("is_mentioned") == 1.0
    assert extra.get("live_asr") == "1"
    assert message["is_mentioned"] is True
    # 普通弹幕不受影响：不带强制回复标记
    plain = LiveEvent(source="bilibili", room_id="1", user_id="u", user_name="观众", kind="danmaku", text="你好")
    plain_msg = build_message(plain, 2, ChannelSettings())
    assert "is_mentioned" not in plain_msg["message_info"]["additional_config"]
    assert plain_msg["is_mentioned"] is False


def test_parse_inbound_voice_prefix() -> None:
    """弹幕墙解析：[语音弹幕] 前缀被剥掉，昵称与正文分列。"""

    message = {
        "message_info": {
            "user_info": {"user_nickname": "主人"},
            "additional_config": {"live_event_kind": "danmaku"},
        },
        "processed_plain_text": "[语音弹幕] 主人: 今天天气怎么样",
        "raw_message": [{"type": "text", "data": "[语音弹幕] 主人: 今天天气怎么样"}],
    }
    row = parse_inbound(message)
    assert row["user"] == "主人"
    assert row["text"] == "今天天气怎么样"


def main() -> int:
    test_pcm_to_wav_frames()
    test_asr_source_label()
    test_submit_voice_enqueues_bypassing_filter()
    test_voice_message_forces_reply_and_not_danmaku()
    test_parse_inbound_voice_prefix()
    print("ASR 模块回归通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
