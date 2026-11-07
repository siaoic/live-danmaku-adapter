"""直播适配器核心层：事件模型、消息映射与统一通道。"""

from .channel import LiveChannel
from .events import LiveEvent
from .mapping import CHANNEL_ACCOUNT_ID, CHANNEL_PLATFORM, ChannelSettings, DanmakuFilter, build_message

__all__ = [
    "CHANNEL_ACCOUNT_ID",
    "CHANNEL_PLATFORM",
    "ChannelSettings",
    "DanmakuFilter",
    "LiveChannel",
    "LiveEvent",
    "build_message",
]
