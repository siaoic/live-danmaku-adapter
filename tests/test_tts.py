# -*- coding: utf-8 -*-
"""tts 子系统离线测试（设备枚举 + 播放），不联网。

Breeze 客户端的请求/流式测试在 tests/test_breeze.py，这里只测
输出设备解析与 PCM 播放这两个不依赖具体后端的部分。
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import asyncio
import io
import sys
import wave

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import tts.devices as devices_module  # noqa: E402
import tts.playback as playback_module  # noqa: E402
import tts  # noqa: E402
from tts.playback import AudioPlayer  # noqa: E402


# ------------------------------------------------------------------ #
#  测试替身
# ------------------------------------------------------------------ #
def make_wav_bytes(seconds: float = 0.2, rate: int = 8000) -> bytes:
    """生成一段 16 位单声道静音 wav。"""

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(bytes(int(rate * seconds) * 2))
    return buffer.getvalue()


class FakeRawStream:
    """记录写入数据的假 RawOutputStream。"""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.frames = bytearray()

    def __enter__(self) -> "FakeRawStream":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    def write(self, data: bytes) -> None:
        self.frames.extend(data)


class FakeSoundDevice:
    """最小可用的 sounddevice 替身：设备枚举 + 原始输出流。"""

    def __init__(
        self,
        device_list: list[dict[str, Any]],
        default_output: Optional[int] = None,
    ) -> None:
        if default_output is None:
            self.default = SimpleNamespace(device=(-1, -1))
        else:
            self.default = SimpleNamespace(device=(-1, default_output))
        self._device_list = device_list
        self.raw_streams: list[FakeRawStream] = []

    def query_devices(self) -> list[dict[str, Any]]:
        return self._device_list

    def RawOutputStream(self, **kwargs: Any) -> FakeRawStream:
        stream = FakeRawStream(**kwargs)
        self.raw_streams.append(stream)
        return stream


DEVICE_LIST: list[dict[str, Any]] = [
    {
        "name": "扬声器 (Realtek Audio)",
        "max_output_channels": 2,
        "default_samplerate": 48000.0,
    },
    {"name": "麦克风", "max_output_channels": 0, "default_samplerate": 44100.0},
    {
        "name": "CABLE Input (VB-Audio Virtual Cable)",
        "max_output_channels": 2,
        "default_samplerate": 44100.0,
    },
]


async def _false_backend(self: AudioPlayer, path: str) -> bool:
    """把所有播放后端替换为失败，用于验证降级行为。"""

    return False


# ------------------------------------------------------------------ #
#  包导出
# ------------------------------------------------------------------ #
def test_tts_package_exports() -> None:
    for name in (
        "AudioPlayer",
        "BreezeClient",
        "BreezeError",
        "BreezeParams",
        "devices_available",
        "list_output_devices",
        "resolve_device_index",
    ):
        assert hasattr(tts, name), f"tts 包缺少导出 {name}"


# ------------------------------------------------------------------ #
#  设备枚举与解析
# ------------------------------------------------------------------ #
def test_devices_unavailable_without_sounddevice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devices_module, "sd", None)
    assert devices_module.devices_available() is False
    assert devices_module.list_output_devices() == []
    assert devices_module.resolve_device_index("0") is None
    assert devices_module.resolve_device_index("CABLE Input") is None


def test_list_output_devices_and_default(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSoundDevice(DEVICE_LIST, default_output=2)
    monkeypatch.setattr(devices_module, "sd", fake)

    assert devices_module.devices_available() is True
    devices = devices_module.list_output_devices()
    assert [item["index"] for item in devices] == [0, 2]
    assert devices[0]["name"] == "扬声器 (Realtek Audio)"
    assert devices[0]["channels"] == 2
    assert devices[0]["default_rate"] == 48000
    assert devices[0]["is_default"] is False
    assert devices[1]["name"] == "CABLE Input (VB-Audio Virtual Cable)"
    assert devices[1]["is_default"] is True


def test_resolve_device_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devices_module, "sd", FakeSoundDevice(DEVICE_LIST, default_output=0))
    assert devices_module.resolve_device_index("") is None
    assert devices_module.resolve_device_index("   ") is None
    assert devices_module.resolve_device_index("0") == 0
    assert devices_module.resolve_device_index("2") == 2
    assert devices_module.resolve_device_index("1") is None
    assert devices_module.resolve_device_index("9") is None
    assert devices_module.resolve_device_index("扬声器 (Realtek Audio)") == 0
    assert devices_module.resolve_device_index("扬声器") == 0
    assert devices_module.resolve_device_index("cable input (vb-audio virtual cable)") == 2
    assert devices_module.resolve_device_index("CABLE") == 2
    assert devices_module.resolve_device_index("不存在的设备") is None


def test_resolve_device_index_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devices_module, "sd", FakeSoundDevice(DEVICE_LIST))
    devices = devices_module.list_output_devices()
    assert all(item["is_default"] is False for item in devices)


# ------------------------------------------------------------------ #
#  播放
# ------------------------------------------------------------------ #
def test_play_wav_bytes_rejects_empty_data() -> None:
    player = AudioPlayer()
    assert asyncio.run(player.play_wav_bytes(b"")) is False


def test_playback_all_backends_fail_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(playback_module, "sd", None)
    monkeypatch.setattr(AudioPlayer, "_play_with_ffplay", _false_backend)
    monkeypatch.setattr(AudioPlayer, "_play_with_winsound", _false_backend)

    player = AudioPlayer(device="CABLE Input", temp_dir=str(tmp_path))
    assert asyncio.run(player.play_wav_bytes(make_wav_bytes())) is False
    assert list(tmp_path.iterdir()) == []


def test_playback_sounddevice_writes_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSoundDevice(DEVICE_LIST, default_output=2)
    monkeypatch.setattr(playback_module, "sd", fake)
    monkeypatch.setattr(devices_module, "sd", fake)
    monkeypatch.setattr(AudioPlayer, "_play_with_ffplay", _false_backend)
    monkeypatch.setattr(AudioPlayer, "_play_with_winsound", _false_backend)

    player = AudioPlayer(device="CABLE", temp_dir=str(tmp_path))
    assert player.device == "CABLE"
    result = asyncio.run(player.play_wav_bytes(make_wav_bytes()))
    assert result is True
    assert len(fake.raw_streams) == 1
    stream = fake.raw_streams[0]
    assert stream.kwargs == {
        "samplerate": 8000,
        "channels": 1,
        "dtype": "int16",
        "device": 2,
    }
    assert len(stream.frames) == 3200
    assert list(tmp_path.iterdir()) == []

    player.set_device("")
    assert player.device == ""
    assert asyncio.run(player.play_wav_bytes(make_wav_bytes())) is True
    assert fake.raw_streams[1].kwargs["device"] is None
    assert list(tmp_path.iterdir()) == []


def test_playback_falls_back_when_device_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSoundDevice(DEVICE_LIST, default_output=0)
    monkeypatch.setattr(playback_module, "sd", fake)
    monkeypatch.setattr(devices_module, "sd", fake)
    monkeypatch.setattr(AudioPlayer, "_play_with_ffplay", _false_backend)
    monkeypatch.setattr(AudioPlayer, "_play_with_winsound", _false_backend)

    player = AudioPlayer(device="不存在的设备", temp_dir=str(tmp_path))
    assert asyncio.run(player.play_wav_bytes(make_wav_bytes())) is False
    assert fake.raw_streams == []
    assert list(tmp_path.iterdir()) == []


def test_playback_missing_temp_dir_returns_false() -> None:
    player = AudioPlayer(temp_dir=str(Path("no_such_dir") / "deeper" / "lsc"))
    assert asyncio.run(player.play_wav_bytes(make_wav_bytes())) is False


def test_playback_serializes_and_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AudioPlayer, "_play_with_sounddevice", _async_true_backend)

    async def scenario() -> None:
        player = AudioPlayer(temp_dir=str(tmp_path))
        results = await asyncio.gather(
            player.play_wav_bytes(make_wav_bytes()),
            player.play_wav_bytes(make_wav_bytes()),
        )
        assert results == [True, True]
        await player.close()

    asyncio.run(scenario())
    assert list(tmp_path.iterdir()) == []


async def _async_true_backend(self: AudioPlayer, path: str) -> bool:
    """播放后端替身：直接成功。"""

    return True
