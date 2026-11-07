"""音频输出设备枚举与选择。

sounddevice 为可选依赖：未安装或加载 PortAudio 失败时所有接口安全降级。
"""

from typing import Any, Optional

import logging

LOGGER = logging.getLogger("plugin.live_stream_companion.tts")

try:
    import sounddevice as sd
except Exception as exc:  # 未安装 sounddevice 或 PortAudio 缺失时保持模块可用
    sd = None
    IMPORT_ERROR: Optional[Exception] = exc
else:
    IMPORT_ERROR = None


def devices_available() -> bool:
    """sounddevice 是否可用（已安装且 PortAudio 可加载）。"""

    return sd is not None


def list_output_devices() -> list[dict[str, Any]]:
    """枚举可输出的音频设备。

    每个元素形如 {"index": int, "name": str, "channels": int,
    "default_rate": int, "is_default": bool}；sounddevice 不可用时返回空列表。
    """

    if sd is None:
        LOGGER.debug("sounddevice 未安装，无法枚举音频输出设备")
        return []

    try:
        raw_devices = sd.query_devices()
    except Exception as exc:
        LOGGER.warning("枚举音频设备失败: %s", exc)
        return []

    default_index = _default_output_index()
    devices: list[dict[str, Any]] = []
    for index, device in enumerate(raw_devices):
        try:
            channels = int(device["max_output_channels"])
        except (KeyError, TypeError, ValueError):
            continue
        if channels <= 0:
            continue
        devices.append(
            {
                "index": index,
                "name": str(device.get("name", "") or ""),
                "channels": channels,
                "default_rate": int(device.get("default_samplerate") or 0),
                "is_default": index == default_index,
            }
        )
    return devices


def resolve_device_index(selector: str) -> Optional[int]:
    """把设备选择器解析为输出设备索引，解析不到返回 None。

    支持三种写法：输出设备索引（如 "3"）、设备名精确匹配、设备名包含匹配
    （后两者忽略大小写）。
    """

    text = str(selector or "").strip()
    if not text:
        return None

    devices = list_output_devices()
    if not devices:
        return None

    if text.lstrip("-").isdigit():
        index = int(text)
        for device in devices:
            if device["index"] == index:
                return index
        LOGGER.warning("音频输出设备索引不存在: %s", text)
        return None

    lowered = text.lower()
    for device in devices:
        if str(device["name"]).strip().lower() == lowered:
            return int(device["index"])
    for device in devices:
        if lowered in str(device["name"]).lower():
            return int(device["index"])

    LOGGER.warning("找不到匹配的音频输出设备: %s", text)
    return None


def _default_output_index() -> Optional[int]:
    """读取 sounddevice 的默认输出设备索引。"""

    if sd is None:
        return None
    try:
        default = sd.default.device
    except Exception as exc:
        LOGGER.debug("读取默认音频设备失败: %s", exc)
        return None

    if isinstance(default, (list, tuple)):
        if len(default) < 2:
            return None
        value = default[1]
    else:
        value = default
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if index >= 0 else None

def find_virtual_cable_output(needle: str = "CABLE Input") -> str:
    """找虚拟声卡（VB-Audio Virtual Cable）的输出设备，返回可用于播放的设备选择器。

    VTS 的内置口型同步（ULipSync）监听的是麦克风；虚拟声卡的模型是
    「程序播到 CABLE Input → VTS 从 CABLE Output 录到音频」，所以这里找的是
    名字类似 CABLE Input 的**输出**设备。

    注意：PortAudio 会把设备名截断到 32 个字符，所以匹配用前缀，返回值用**索引**
    （索引在本次枚举内唯一且可直接交给播放器，不受截断影响）。

    Args:
        needle: 设备名关键字（不区分大小写）。

    Returns:
        str: 匹配到的设备索引（字符串形式）；找不到返回空串。
    """

    text = str(needle or "").strip().lower()
    if not text:
        return ""
    stem = text.split()[0] if text.split() else text
    devices = list_output_devices()
    # 一轮精确包含，一轮只按首词匹配（覆盖 CABLE In 16ch 之类的变体）
    for device in devices:
        if text in str(device["name"]).lower():
            return str(device["index"])
    for device in devices:
        if stem in str(device["name"]).lower():
            return str(device["index"])
    return ""
