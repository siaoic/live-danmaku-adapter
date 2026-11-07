"""直播平台接入层：每个平台一个链路实现。"""

from .bilibili import BilibiliLiveLink
from .douyin import DouyinLiveLink

__all__ = ["BilibiliLiveLink", "DouyinLiveLink"]
