"""音频播放：把合成的 wav 字节写到指定输出设备。

播放后端优先级：sounddevice RawOutputStream（支持指定设备）-> ffplay ->
Windows winsound（不支持指定设备，只能用系统默认输出）。

临时 wav 写入 temp_dir（默认系统临时目录），播放结束或失败后都会删除。
"""

from typing import AsyncIterator, Callable, Optional

import asyncio
import logging
import os
import shutil
import tempfile
import wave

try:
    import sounddevice as sd
except Exception:
    sd = None

from .devices import resolve_device_index

LOGGER = logging.getLogger("plugin.live_stream_companion.tts")

# wav 采样位宽 -> sounddevice 原始流 dtype
RAW_DTYPES: dict[int, str] = {1: "uint8", 2: "int16", 3: "int24", 4: "int32"}
# ffplay 播放超时（秒）；TTS 片段都很短，超时即视为异常
FFPLAY_TIMEOUT = 120.0


class AudioPlayer:
    """把 wav 字节播放到指定输出设备。

    device 为空串表示系统默认输出设备，否则是索引或设备名（见
    tts.devices.resolve_device_index）。
    """

    def __init__(
        self,
        device: str = "",
        logger: Optional[logging.Logger] = None,
        temp_dir: Optional[str] = None,
    ) -> None:
        self._device = str(device or "").strip()
        self._logger = logger if logger is not None else LOGGER
        self._temp_dir = str(temp_dir) if temp_dir else tempfile.gettempdir()
        self._lock = asyncio.Lock()

    @property
    def device(self) -> str:
        """当前设备选择器（空串表示系统默认输出设备）。"""

        return self._device

    def set_device(self, selector: str) -> None:
        """更新设备选择器（下次播放生效）。"""

        self._device = str(selector or "").strip()

    async def play_wav_bytes(self, data: bytes) -> bool:
        """播放 wav 字节：任一后端成功返回 True，全部失败返回 False。"""

        if not data:
            self._logger.warning("播放失败：音频数据为空")
            return False

        path = self._write_temp_wav(data)
        if path is None:
            return False

        try:
            async with self._lock:
                if await self._play_with_sounddevice(path):
                    return True
                if await self._play_with_ffplay(path):
                    return True
                if await self._play_with_winsound(path):
                    return True
            self._logger.warning(
                "所有播放后端均不可用，音频未播放（device=%s, 字节=%d）",
                self._device or "默认设备",
                len(data),
            )
            return False
        finally:
            self._remove_temp_file(path)

    def write_temp_wav(self, data: bytes) -> Optional[str]:
        """把 wav 字节写到临时文件并返回路径（调用方负责删除）。

        播放之外还需要复用同一段音频时用这个接口，例如把音频同时交给
        VTS 嘴型驱动；失败返回 None。
        """

        if not data:
            return None
        return self._write_temp_wav(data)

    def remove_temp_file(self, path: Optional[str]) -> None:
        """删除 write_temp_wav 产生的临时文件。"""

        if path:
            self._remove_temp_file(path)

    async def close(self) -> None:
        """等待进行中的播放结束并释放资源。

        本实现不持有长生命周期句柄，锁只用于串行化对同一设备的播放。
        """

        async with self._lock:
            return

    def _write_temp_wav(self, data: bytes) -> Optional[str]:
        """把音频字节写入临时 wav 文件，失败返回 None。"""

        try:
            handle_fd, path = tempfile.mkstemp(
                prefix="lsc_tts_", suffix=".wav", dir=self._temp_dir
            )
        except OSError as exc:
            self._logger.warning("创建临时音频文件失败: %s", exc)
            return None

        try:
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            self._logger.warning("写入临时音频文件失败: %s", exc)
            self._remove_temp_file(path)
            return None
        return path

    def _remove_temp_file(self, path: str) -> None:
        """删除临时音频文件；删除失败且文件仍在时记警告。"""

        try:
            os.remove(path)
        except OSError as exc:
            if os.path.exists(path):
                self._logger.warning("删除临时音频文件失败: %s", exc)

    def _read_wav(self, path: str) -> Optional[tuple[int, int, int, bytes]]:
        """读取 wav 的 (声道数, 采样率, 采样位宽, 帧字节)。"""

        try:
            with wave.open(path, "rb") as wav_file:
                channels = max(1, wav_file.getnchannels())
                rate = max(1, wav_file.getframerate())
                sample_width = wav_file.getsampwidth()
                frames = wav_file.readframes(wav_file.getnframes())
        except (OSError, wave.Error) as exc:
            self._logger.warning("解析 wav 失败: %s", exc)
            return None
        return channels, rate, sample_width, frames

    async def _play_with_sounddevice(self, path: str) -> bool:
        """用 sounddevice 原始输出流播放；可指定设备，不可用时返回 False。"""

        if sd is None:
            return False

        index: Optional[int] = None
        if self._device:
            index = resolve_device_index(self._device)
            if index is None:
                self._logger.warning(
                    "指定的音频设备无法解析，跳过 sounddevice 播放: %s", self._device
                )
                return False

        parsed = self._read_wav(path)
        if parsed is None:
            return False
        channels, rate, sample_width, frames = parsed

        dtype = RAW_DTYPES.get(sample_width)
        if dtype is None:
            self._logger.warning("不支持的采样位宽: %d 字节", sample_width)
            return False

        return await asyncio.to_thread(
            self._play_sounddevice_sync, frames, rate, channels, dtype, index
        )

    def _play_sounddevice_sync(
        self,
        frames: bytes,
        rate: int,
        channels: int,
        dtype: str,
        index: Optional[int],
    ) -> bool:
        """阻塞播放（在线程池中执行），退出时音频已播完。

        TTS 后端输出的采样率（breeze 是 24kHz）与虚拟声卡/声卡原生采样率
        （多为 44.1k/48kHz）通常不一致，直接按原采样率打开设备会走
        PortAudio 的隐式重采样，VTS 的 uLipSync 拿到的样本序列会失真、
        口型对不上；这里显式按设备采样率重采样。
        """

        target_rate = rate
        play_frames = frames
        try:
            info = sd.query_devices(index, "output") if index is not None else sd.query_devices(kind="output")
            device_rate = int(float(info.get("default_samplerate") or 0))
            if device_rate > 0 and device_rate != rate:
                play_frames = _resample_pcm(frames, rate, device_rate, channels, dtype)
                target_rate = device_rate
        except Exception as exc:
            self._logger.debug("查询设备采样率失败，按原采样率播放: %s", exc)

        try:
            with sd.RawOutputStream(
                samplerate=target_rate, channels=channels, dtype=dtype, device=index
            ) as stream:
                stream.write(play_frames)
            return True
        except Exception as exc:
            self._logger.warning("sounddevice 播放失败: %s", exc)
            return False

    async def play_stream(
        self,
        chunks: "AsyncIterator[bytes]",
        sample_rate: int,
        channels: int = 1,
        sample_width: int = 2,
        on_progress: Optional[Callable[[float], None]] = None,
    ) -> bool:
        """边收边播：把 async 迭代器吐出的 PCM 片段实时推给声卡。

        首包一到就开始出声，不必等整句合成完。这就是「边合成边播放」的落点，
        长句的等待时间从「整句合成时长」降到「首包延迟」。

        Args:
            chunks: async 迭代器，产出裸 PCM（无 wav 头）。
            sample_rate: 片段采样率。
            channels: 声道数，默认单声道。
            sample_width: 采样位宽（字节），默认 2（int16）。
            on_progress: 可选回调，写入声卡后报告「已推进的音频秒数」；
                字幕靠它按句切换，没有它就整段一起显示。

        Returns:
            bool: 是否成功播放。
        """

        if sd is None:
            self._logger.warning("sounddevice 不可用，无法流式播放")
            return False

        dtype = RAW_DTYPES.get(sample_width)
        if dtype is None:
            self._logger.warning("不支持的采样位宽: %d 字节", sample_width)
            return False

        index: Optional[int] = None
        if self._device:
            index = resolve_device_index(self._device)
            if index is None:
                self._logger.warning(
                    "指定的音频设备无法解析，跳过流式播放: %s", self._device
                )
                return False

        # 设备原生采样率与合成采样率不同时，逐段重采样（保持边播边转）
        target_rate = sample_rate
        try:
            info = (
                sd.query_devices(index, "output")
                if index is not None
                else sd.query_devices(kind="output")
            )
            device_rate = int(float(info.get("default_samplerate") or 0))
            if device_rate > 0 and device_rate != sample_rate:
                target_rate = device_rate
        except Exception as exc:
            self._logger.debug("查询设备采样率失败，按原采样率播放: %s", exc)

        async with self._lock:
            stream = None
            try:
                stream = sd.RawOutputStream(
                    samplerate=target_rate,
                    channels=channels,
                    dtype=dtype,
                    device=index,
                )
                stream.start()
                written_bytes = 0
                frame_bytes = max(1, sample_rate * channels * sample_width)
                async for piece in chunks:
                    if not piece:
                        continue
                    data = piece
                    if target_rate != sample_rate:
                        data = await asyncio.to_thread(
                            _resample_pcm, piece, sample_rate, target_rate, channels, dtype
                        )
                    # 写入放到线程池，避免阻塞事件循环
                    await asyncio.to_thread(stream.write, data)
                    written_bytes += len(piece)
                    if on_progress is not None:
                        try:
                            on_progress(written_bytes / float(frame_bytes))
                        except Exception:  # noqa: BLE001 - 进度回调不能打断播放
                            pass
                return True
            except Exception as exc:
                self._logger.warning("流式播放失败: %s", exc)
                return False
            finally:
                if stream is not None:
                    try:
                        stream.stop()
                        stream.close()
                    except Exception:
                        pass

    async def _play_with_ffplay(self, path: str) -> bool:
        """用 ffplay 播放临时文件（不支持指定设备，走系统默认输出）。"""

        executable = shutil.which("ffplay")
        if not executable:
            return False

        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "quiet",
                path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            self._logger.warning("启动 ffplay 失败: %s", exc)
            return False

        try:
            code = await asyncio.wait_for(process.wait(), timeout=FFPLAY_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()
            self._logger.warning("ffplay 播放超时（%s 秒）", FFPLAY_TIMEOUT)
            return False

        if code != 0:
            self._logger.warning("ffplay 播放失败，退出码: %s", code)
            return False
        return True

    async def _play_with_winsound(self, path: str) -> bool:
        """Windows 下用 winsound 兜底播放（只能用系统默认输出设备）。"""

        if os.name != "nt":
            return False
        try:
            import winsound
        except ImportError:
            return False

        def _play() -> bool:
            try:
                winsound.PlaySound(path, winsound.SND_FILENAME)
                return True
            except Exception as exc:
                self._logger.warning("winsound 播放失败: %s", exc)
                return False

        return await asyncio.to_thread(_play)

def _resample_pcm(frames: bytes, src_rate: int, dst_rate: int, channels: int, dtype: str) -> bytes:
    """把交错 PCM 从 src_rate 重采样到 dst_rate（线性插值）。

    只在设备采样率与音频不一致时调用。线性插值对 TTS 语音足够，
    而且不引入额外依赖（numpy 是引擎的依赖，这里不强制要求）。
    """

    if src_rate <= 0 or dst_rate <= 0 or src_rate == dst_rate:
        return frames
    try:
        import numpy as np
    except Exception:
        # 没有 numpy 就退回设备隐式重采样，至少还能出声
        return frames

    np_dtype = {"int16": np.int16, "int32": np.int32, "uint8": np.uint8}.get(dtype, np.int16)
    data = np.frombuffer(frames, dtype=np_dtype)
    if data.size == 0:
        return frames
    if channels > 1 and data.size % channels == 0:
        data = data.reshape(-1, channels)
    else:
        data = data.reshape(-1, 1)

    src_len = data.shape[0]
    dst_len = max(1, int(round(src_len * dst_rate / src_rate)))
    src_idx = np.linspace(0.0, src_len - 1, dst_len)
    lo = np.floor(src_idx).astype(np.int64)
    hi = np.minimum(lo + 1, src_len - 1)
    frac = (src_idx - lo).reshape(-1, 1)
    as_float = data.astype(np.float64)
    mixed = as_float[lo] * (1.0 - frac) + as_float[hi] * frac
    return mixed.astype(np_dtype).tobytes()
