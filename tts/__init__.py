"""TTS 子系统：Breeze TTS 2 客户端、输出设备枚举与音频播放。"""

from .breeze import BreezeClient, BreezeError, BreezeParams
from .devices import (
    devices_available,
    find_virtual_cable_output,
    list_output_devices,
    resolve_device_index,
)
from .playback import AudioPlayer

__all__ = [
    "AudioPlayer",
    "BreezeClient",
    "BreezeError",
    "BreezeParams",
    "devices_available",
    "find_virtual_cable_output",
    "list_output_devices",
    "resolve_device_index",
]
