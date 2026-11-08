"""端到端验证：用 MaiBot 真实的消息转换与路由键逻辑跑一遍入站链路。

不启动完整 MaiBot，只把 Host 侧最关键的两段代码拉进来：
1. RouteKeyFactory.from_message_dict —— 校验归属字段；
2. PluginMessageUtils._build_session_message_from_dict —— 校验消息字典结构。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(r"E:/AI/直播/MaiBot-1.3.19")
sys.path.insert(0, str(ROOT))

PLUGIN_DIR = Path(__file__).resolve().parent.parent
MODULE_NAME = "live_danmaku_adapter_e2e"

spec = importlib.util.spec_from_file_location(
    MODULE_NAME, PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(PLUGIN_DIR)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[MODULE_NAME] = module
spec.loader.exec_module(module)

from src.platform_io.route_key_factory import RouteKeyFactory
from src.platform_io.types import RouteKey
from src.plugin_runtime.host.message_utils import PluginMessageUtils

from live_danmaku_adapter_e2e.core.events import (
    KIND_DANMAKU, KIND_GIFT, KIND_SUPER_CHAT, SOURCE_BILIBILI, SOURCE_DOUYIN, LiveEvent,
)
from live_danmaku_adapter_e2e.core.mapping import CHANNEL_ACCOUNT_ID, CHANNEL_PLATFORM, ChannelSettings, build_message

DECLARED = RouteKey(platform=CHANNEL_PLATFORM, account_id=CHANNEL_ACCOUNT_ID, scope=None)
settings = ChannelSettings(merge_platforms=True)

cases = [
    LiveEvent(SOURCE_BILIBILI, "21696950", "10086", "阿岚", KIND_DANMAKU, "麦麦在吗"),
    LiveEvent(SOURCE_DOUYIN, "7699088628957350706", "20001", "小明", KIND_DANMAKU, "主播好"),
    LiveEvent(SOURCE_BILIBILI, "21696950", "9", "老板", KIND_SUPER_CHAT, "加油", price=30.0),
    LiveEvent(SOURCE_DOUYIN, "7699088628957350706", "8", "小明", KIND_GIFT, "玫瑰",
              gift_name="玫瑰", gift_count=3, price=0.3),
]

for index, event in enumerate(cases, start=1):
    message = build_message(event, index, settings)
    route_key = RouteKeyFactory.from_message_dict(message)
    assert route_key == DECLARED, f"归属与网关声明不一致: {route_key} != {DECLARED}"
    session_message = PluginMessageUtils._build_session_message_from_dict(message)
    assert session_message.platform == "live"
    assert session_message.account_id == CHANNEL_ACCOUNT_ID
    text = session_message.message_info
    print(f"OK {event.source}/{event.kind}: group={session_message.message_id[:32]} "
          f"nick={text.user_info.user_nickname} text={message['processed_plain_text'][:40]}")

print("端到端入站校验通过（4 个用例，归属与结构均被 Host 逻辑接受）")
