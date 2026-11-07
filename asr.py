# -*- coding: utf-8 -*-
"""ASR 语音对话：按住热键说话，松开后转写并送进聊天流。

链路：全局热键（keyboard 库）→ 麦克风录音（sounddevice）→ SiliconFlow
转写接口 → 构造 LiveEvent 走统一通道注入 MaiBot 聊天流，AI 的回复
自动走 TTS/字幕链路。

AI 正在播报时按下热键会先打断语音（plugin 侧的 _speech_interrupt 事件），
等她闭嘴、留一点残响缓冲后才开始录，避免把 AI 的声音录进识别。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import wave
from typing import Any, Awaitable, Callable, Optional

import aiohttp

try:  # sounddevice 为可选依赖：缺 PortAudio 时录音不可用，但模块仍可导入
    import sounddevice as sd
except Exception:  # pragma: no cover - 环境相关
    sd = None

try:  # keyboard 为可选依赖：全局热键监听
    import keyboard
except Exception:  # pragma: no cover - 环境相关
    keyboard = None

from .tts.devices import resolve_device_index

LOGGER = logging.getLogger("plugin.live_danmaku_adapter.asr")

#: 录音参数：16kHz 单声道 int16，转写接口的标准输入
DEFAULT_SAMPLE_RATE = 16000


class ASRError(Exception):
    """转写接口报错。"""


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1, sample_width: int = 2) -> bytes:
    """把裸 PCM 打包成 wav 字节（转写接口要带文件头的音频）。"""

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(max(1, int(channels)))
        wf.setsampwidth(max(1, int(sample_width)))
        wf.setframerate(max(1, int(sample_rate)))
        wf.writeframes(pcm)
    return buffer.getvalue()


class SiliconFlowASR:
    """SiliconFlow /v1/audio/transcriptions 转写客户端（aiohttp 异步）。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.siliconflow.cn/v1",
        model: str = "Qwen/Qwen3-ASR-1.7B",
        timeout: float = 30.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """初始化客户端。

        Args:
            api_key: SiliconFlow API Key。
            base_url: 接口根地址，默认官方云端。
            model: 转写模型；实测 Qwen3-ASR-1.7B 最快（短句 0.4~0.7s）。
            timeout: 单次转写超时（秒）。
            logger: 插件 logger。
        """

        self._api_key = str(api_key or "").strip()
        self._base_url = str(base_url or "https://api.siliconflow.cn/v1").strip().rstrip("/")
        self._model = str(model or "Qwen/Qwen3-ASR-1.7B").strip()
        self._timeout = max(5.0, float(timeout))
        self._logger = logger if logger is not None else LOGGER

    @property
    def model(self) -> str:
        """当前使用的转写模型名。"""

        return self._model

    async def transcribe(self, wav_bytes: bytes, filename: str = "speech.wav") -> str:
        """转写一段 wav 音频，返回识别文本；接口报错抛 ASRError。"""

        if not wav_bytes:
            return ""
        if not self._api_key:
            raise ASRError("未配置 api_key")
        url = f"{self._base_url}/audio/transcriptions"
        form = aiohttp.FormData()
        form.add_field("file", wav_bytes, filename=filename, content_type="audio/wav")
        form.add_field("model", self._model)
        headers = {"Authorization": f"Bearer {self._api_key}"}
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=form, headers=headers) as resp:
                    body = await resp.text()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ASRError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status != 200:
            raise ASRError(f"HTTP {resp.status}: {body[:200]}")
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise ASRError(f"非 JSON 响应：{body[:200]}") from exc
        return str(data.get("text") or "").strip()


class MicRecorder:
    """按住/松开式麦克风录音（sounddevice InputStream，16k 单声道 int16）。"""

    def __init__(
        self,
        device: str = "",
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """初始化（不占用麦克风，start() 时才打开）。

        Args:
            device: 输入设备名或索引；留空用系统默认麦克风。
            sample_rate: 采样率，默认 16k。
            logger: 插件 logger。
        """

        self._device = str(device or "").strip()
        self.sample_rate = max(8000, int(sample_rate))
        self._logger = logger if logger is not None else LOGGER
        self._stream: Optional[Any] = None
        self._frames: list[bytes] = []
        self._started_at = 0.0

    @property
    def recording(self) -> bool:
        """是否正在录音。"""

        return self._stream is not None

    def elapsed(self) -> float:
        """本次录音已持续多少秒（未在录音时为 0）。"""

        if self._stream is None:
            return 0.0
        return time.monotonic() - self._started_at

    def start(self) -> bool:
        """打开麦克风开始收音；已在录音或设备不可用时返回 False。"""

        if self._stream is not None:
            return False
        if sd is None:
            self._logger.warning("sounddevice 不可用，无法录音")
            return False
        self._frames = []
        index: Optional[int] = None
        if self._device:
            index = resolve_device_index(self._device)
            if index is None:
                self._logger.warning(f"指定的录音设备无法解析，改用系统默认：{self._device}")
                index = None
        try:
            stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                device=index,
                callback=self._on_frame,
            )
            stream.start()
        except Exception as exc:
            self._logger.warning(f"麦克风打开失败：{type(exc).__name__}: {exc}")
            self._stream = None
            return False
        self._stream = stream
        self._started_at = time.monotonic()
        return True

    def stop(self) -> bytes:
        """停止录音并返回原始 PCM（int16 单声道）；没在录音返回空字节。"""

        stream, self._stream = self._stream, None
        if stream is None:
            return b""
        try:
            stream.stop()
            stream.close()
        except Exception as exc:
            self._logger.debug(f"关闭录音流异常：{exc}")
        return b"".join(self._frames)

    def _on_frame(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        """PortAudio 回调线程：把每帧数据拷进列表（bytes() 是拷贝，线程安全）。"""

        del frames, time_info
        if status:
            self._logger.debug(f"录音流状态：{status}")
        if self._stream is not None:
            self._frames.append(bytes(indata))


class AsrHotkey:
    """全局热键：按住开始录音、松开转写发送（keyboard 库）。"""

    def __init__(
        self,
        hotkey: str,
        loop: asyncio.AbstractEventLoop,
        on_press: Callable[[], Awaitable[None]],
        on_release: Callable[[], Awaitable[None]],
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """初始化。

        Args:
            hotkey: 键名，如 f9；keyboard 库的键名（小写字母/数字/f1~f12 等）。
            loop: 插件所在的 asyncio 事件循环，回调从 keyboard 线程投递回它。
            on_press: 按下时执行的协程（开始录音）。
            on_release: 松开时执行的协程（转写发送）。
            logger: 插件 logger。
        """

        self.hotkey = str(hotkey or "f9").strip().lower()
        self._loop = loop
        self._on_press = on_press
        self._on_release = on_release
        self._logger = logger if logger is not None else LOGGER
        self._hooks: list[Any] = []
        self._started = False

    def start(self) -> bool:
        """注册全局按键钩子；keyboard 不可用或键名非法返回 False。"""

        if self._started:
            return True
        if keyboard is None:
            self._log("warning", "keyboard 库未安装，语音热键不可用")
            return False
        try:
            self._hooks = [
                keyboard.on_press_key(self.hotkey, self._handle_press),
                keyboard.on_release_key(self.hotkey, self._handle_release),
            ]
        except Exception as exc:
            self._log("warning", f"热键 {self.hotkey} 注册失败：{exc}")
            return False
        self._started = True
        return True

    def stop(self) -> None:
        """注销钩子。"""

        if not self._started:
            return
        for hook in self._hooks:
            try:
                keyboard.remove_listener(hook)
            except Exception:
                pass
        self._hooks = []
        self._started = False

    def _handle_press(self, event: Any) -> None:
        """keyboard 线程回调：投递回插件事件循环。"""

        del event
        try:
            asyncio.run_coroutine_threadsafe(self._on_press(), self._loop)
        except Exception as exc:
            self._log("debug", f"按下回调投递失败：{exc}")

    def _handle_release(self, event: Any) -> None:
        """keyboard 线程回调：投递回插件事件循环。"""

        del event
        try:
            asyncio.run_coroutine_threadsafe(self._on_release(), self._loop)
        except Exception as exc:
            self._log("debug", f"松开回调投递失败：{exc}")

    def _log(self, level: str, msg: str) -> None:
        getattr(self._logger, level, self._logger.info)(msg)
