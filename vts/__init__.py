"""VTube Studio 控制模块。

只保留两件事，都直接服务于有机演出引擎：
  * client.py    —— VTS 官方 WebSocket API 客户端（认证、查询、热键、表情、参数注入）；
  * discovery.py —— 跨平台自动发现 VTS 的 host/port 与安装位置。

口型同步不在这里：TTS 音频直接播到虚拟声卡，由 VTS 内置的 ULipSync 驱动嘴型。
"""

from .client import (
    VTSClient,
    VTSClientError,
    VTSConnectionError,
    VTSResponseError,
    VTSTimeoutError,
    VTSRealtimeClient,
)
from .discovery import VTS_DEFAULT_PORT, VTS_SCAN_PORTS, auto_discover, get_install_info

__all__ = [
    "VTSClient",
    "VTSClientError",
    "VTSConnectionError",
    "VTSRealtimeClient",
    "VTSResponseError",
    "VTSTimeoutError",
    "VTS_DEFAULT_PORT",
    "VTS_SCAN_PORTS",
    "auto_discover",
    "get_install_info",
]
