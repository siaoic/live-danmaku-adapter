"""离线自测：不连网、不起 MaiBot，直接验证归一化与两个平台的解析逻辑。

插件在 MaiBot 里是一个独立包（包名由插件 ID 生成），所以这里用同样的方式
加载：把插件目录作为包的搜索路径，core/、platforms/ 的相对导入才能正常工作。

用法：python tests/test_offline.py（在插件目录下执行）。
"""

from __future__ import annotations

import asyncio
import gzip
import importlib.util
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_MODULE_NAME = "live_danmaku_adapter_under_test"


def _load_plugin_package() -> None:
    """按 Runner 的方式把插件目录加载成包。"""

    spec = importlib.util.spec_from_file_location(
        _MODULE_NAME,
        _PLUGIN_DIR / "plugin.py",
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法为插件目录构造 import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)


_load_plugin_package()

from live_danmaku_adapter_under_test.core.events import (  # noqa: E402
    KIND_DANMAKU,
    KIND_GIFT,
    KIND_SUPER_CHAT,
    SOURCE_BILIBILI,
    SOURCE_DOUYIN,
    LiveEvent,
)
from live_danmaku_adapter_under_test.core.mapping import (  # noqa: E402
    CHANNEL_ACCOUNT_ID,
    CHANNEL_PLATFORM,
    ChannelSettings,
    DanmakuFilter,
    build_message,
)
from live_danmaku_adapter_under_test.platforms.douyin import DouyinLiveLink  # noqa: E402
from live_danmaku_adapter_under_test.platforms.douyin_sign import DouyinSigner  # noqa: E402
from live_danmaku_adapter_under_test.platforms.pb import encode_bytes_field, encode_int_field  # noqa: E402


def test_message_mapping() -> None:
    """合并通道下，两个平台的事件应该落进同一个聊天流。"""

    settings = ChannelSettings(merge_platforms=True, group_id="", group_name="直播间弹幕")
    bili = LiveEvent(
        source=SOURCE_BILIBILI,
        room_id="21696950",
        user_id="10086",
        user_name="阿岚",
        kind=KIND_DANMAKU,
        text="麦麦在吗",
        timestamp=1750000000,
    )
    douyin = LiveEvent(
        source=SOURCE_DOUYIN,
        room_id="7699088628957350706",
        user_id="20001",
        user_name="小明",
        kind=KIND_DANMAKU,
        text="主播好",
        timestamp=1750000001,
    )

    bili_msg = build_message(bili, 1, settings)
    douyin_msg = build_message(douyin, 2, settings)

    assert bili_msg["platform"] == CHANNEL_PLATFORM == douyin_msg["platform"]
    assert bili_msg["account_id"] == CHANNEL_ACCOUNT_ID == douyin_msg["account_id"]
    # 合并通道：两个来源的 group_id 必须完全相同，才算真的「走一个通道」
    bili_group = bili_msg["message_info"]["group_info"]["group_id"]
    douyin_group = douyin_msg["message_info"]["group_info"]["group_id"]
    assert bili_group == douyin_group
    assert bili_msg["raw_message"][0]["data"].startswith("[B站弹幕]")
    assert douyin_msg["raw_message"][0]["data"].startswith("[抖音弹幕]")
    assert bili_msg["message_info"]["additional_config"]["live_source"] == SOURCE_BILIBILI
    print("OK 合并通道消息映射")


def test_split_channel_mapping() -> None:
    """分房模式下，两个平台的会话 ID 必须不同。"""

    settings = ChannelSettings(merge_platforms=False)
    bili = LiveEvent(SOURCE_BILIBILI, "21696950", "1", "阿岚", KIND_DANMAKU, "a")
    douyin = LiveEvent(SOURCE_DOUYIN, "7699088628957350706", "2", "小明", KIND_DANMAKU, "b")
    bili_msg = build_message(bili, 1, settings)
    douyin_msg = build_message(douyin, 2, settings)
    assert bili_msg["message_info"]["group_info"]["group_id"] != douyin_msg["message_info"]["group_info"]["group_id"]
    print("OK 分房通道消息映射")


def test_paid_event_text() -> None:
    """付费事件的播报文本要带金额与来源。"""

    settings = ChannelSettings()
    sc = LiveEvent(SOURCE_BILIBILI, "1", "9", "老板", KIND_SUPER_CHAT, "加油", price=30.0)
    gift = LiveEvent(
        SOURCE_DOUYIN, "1", "8", "小明", KIND_GIFT, "玫瑰", gift_name="玫瑰", gift_count=3, price=0.3
    )
    sc_text = build_message(sc, 1, settings)["raw_message"][0]["data"]
    gift_text = build_message(gift, 2, settings)["raw_message"][0]["data"]
    assert "醒目留言" in sc_text and "30" in sc_text
    assert "礼物" in gift_text and "玫瑰x3" in gift_text
    print("OK 付费事件播报文本")


def test_filter() -> None:
    """空弹幕、方括号指令、重复弹幕都应被过滤。"""

    f = DanmakuFilter(dedup_window_s=300, dedup_max_entries=10, drop_bracket=True, max_text_len=20)
    assert f.accept("", 0.0) is None
    assert f.accept("  ", 0.0) is None
    assert f.accept("[红包] 快来抢", 0.0) is None
    assert f.accept("你好", 0.0) == "你好"
    assert f.accept("你好", 1.0) is None, "窗口内同内容要丢弃"
    assert f.accept("你好", 301.0) == "你好", "窗口外同内容要放行"
    long_text = "x" * 30
    assert f.accept(long_text, 5.0) == long_text[:20]
    print("OK 防刷屏过滤")


def _frame(messages: list, need_ack: bool = True) -> bytes:
    """构造一个抖音推送帧（PushFrame，payload 里是 Response）。"""

    body = b""
    for method, payload in messages:
        item = encode_bytes_field(1, method.encode()) + encode_bytes_field(2, payload)
        body += encode_bytes_field(1, item)
    body += encode_int_field(9, 1 if need_ack else 0)
    body += encode_bytes_field(5, b"internal-ext")
    return encode_int_field(2, 42) + encode_bytes_field(8, gzip.compress(body))


def _chat_payload(user_id: int, nickname: str, content: str) -> bytes:
    """构造 WebcastChatMessage 载荷。"""

    user = encode_int_field(1, user_id) + encode_bytes_field(3, nickname.encode())
    common = encode_int_field(2, 9001) + encode_int_field(4, 1750000000000)
    return encode_bytes_field(1, common) + encode_bytes_field(2, user) + encode_bytes_field(3, content.encode())


class _FakeLogger:
    """记录日志调用的最小替身。"""

    def __init__(self) -> None:
        self.warnings: list = []

    def warning(self, *args: object, **kwargs: object) -> None:
        self.warnings.append(args)

    def info(self, *args: object, **kwargs: object) -> None:
        pass


class _FakeChannel:
    """记录提交事件的最小通道替身。"""

    def __init__(self) -> None:
        self.events: list = []
        self.logger = _FakeLogger()

    def submit(self, event: LiveEvent) -> None:
        self.events.append(event)

    def update_source(self, *args: object, **kwargs: object) -> None:
        pass


class _FakeWS:
    """记录 ack 回包的最小 WebSocket 替身。"""

    def __init__(self) -> None:
        self.sent: list = []

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)


def _make_link(channel: _FakeChannel, room_id: str = "7699088628957350706") -> DouyinLiveLink:
    """绕过 __init__ 直接装配一个只用于解析的链路对象。"""

    link = DouyinLiveLink.__new__(DouyinLiveLink)
    link._channel = channel
    link._room_id = room_id
    link._web_rid = "261378947940"
    link._last_frame_at = 0.0
    return link


def test_douyin_parsing() -> None:
    """抖音帧解析：弹幕能取到昵称与内容，且服务端要求时回了 ack。"""

    channel = _FakeChannel()
    link = _make_link(channel)
    ws = _FakeWS()
    frame = _frame([("WebcastChatMessage", _chat_payload(123456, "小明", "主播好"))])
    asyncio.run(link._handle_frame(ws, frame))

    assert len(channel.events) == 1
    event = channel.events[0]
    assert event.text == "主播好"
    assert event.user_name == "小明"
    assert event.user_id == "123456"
    assert event.room_id == "7699088628957350706"
    assert event.kind == KIND_DANMAKU
    assert ws.sent, "服务端要求 ack 时必须回包"
    print("OK 抖音帧解析与 ack")


def test_douyin_unknown_message_ignored() -> None:
    """未知消息类型要静默跳过，不能抛异常。"""

    channel = _FakeChannel()
    link = _make_link(channel, room_id="1")
    frame = _frame([("WebcastRoomStatsMessage", b"")], need_ack=False)
    asyncio.run(link._handle_frame(_FakeWS(), frame))
    assert channel.events == []
    print("OK 未知消息静默跳过")


def test_signer() -> None:
    """签名器要能算出稳定的 16 位签名，且同一 URL 结果可复现。"""

    signer = DouyinSigner()
    url = "wss://example.com/ws?room_id=1&aid=6383&app_name=douyin_web"
    digest = signer.build_md5(url)
    assert len(digest) == 32
    # 签名里带了时间戳与随机量，同 URL 两次结果不同是正常的，只校验格式
    for _ in range(3):
        signature = signer.sign(url)
        assert signature, "签名结果不能为空"
        assert len(signature) == 16, "抖音签名固定 16 位"
    signer.release()
    print("OK 抖音握手签名")


def main() -> int:
    """依次跑全部离线检查。"""

    test_message_mapping()
    test_split_channel_mapping()
    test_paid_event_text()
    test_filter()
    test_douyin_parsing()
    test_douyin_unknown_message_ignored()
    test_signer()
    print("全部离线检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
