# -*- coding: utf-8 -*-
"""Breeze TTS 2 客户端（``breeze-server.exe`` 的 HTTP 接口）。

与 GPT-SoVITS 那条链路的关键差异，都是实测/文档确认过的：

1. **输出的是裸 PCM，不是 WAV**：``POST /v1/audio/speech`` 响应体是
   headerless s16le 单声道，采样率在 ``X-Sample-Rate`` 头里（默认 24000）。
   所以不需要 gsv 那套剥 44 字节 WAV 头的逻辑，但**播放器必须按 24k 初始化**。
2. **分块是"若干秒音频"级**：服务端每攒够一段就 flush，首包通常在百毫秒级，
   比等整段快得多；chunk 边界可能落在样本中间，必须自己缓存奇数尾字节。
3. **音色是 ``voice_id``**（``voices/`` 目录里的 ``.breeze`` 文件），
   不再需要参考音频路径与参考文本；想临时克隆才传 ``ref_audio`` + ``ref_text``。
4. **``instruction`` 是"怎么说"**（音色描述/表演指令），不是内容；
   ``cfg_scale`` 控制贴合程度，1.0 等于关闭引导。

本模块只依赖标准库与 aiohttp，不依赖 MaiBot 运行时，可单独测试。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, fields, replace
from typing import Any, Optional

import aiohttp

LOGGER = logging.getLogger("plugin.live_stream_companion.tts.breeze")

#: breeze-tts-2 的输出采样率（服务端可通过 X-Sample-Rate 覆盖）。
#: 注意它和 gsv.cpp 的 48k 不同，播放器必须跟着改。
SAMPLE_RATE = 24000

#: s16le 单声道每样本字节数，用来算音频时长。
BYTES_PER_SAMPLE = 2

#: 小于这个字节数的响应视为失败（正常一句话远超它）。
MIN_AUDIO_BYTES = 200

#: 单次请求最多保留的错误正文长度。
ERROR_BODY_LIMIT = 200

#: 健康检查超时（秒）：本地服务，卡住就是没起来。
HEALTH_TIMEOUT = 5.0


class BreezeError(RuntimeError):
    """breeze-server 返回错误或不可达。"""


@dataclass
class BreezeParams:
    """一次合成所需的全部参数。

    Attributes:
        server: 服务根地址，如 ``http://127.0.0.1:9880``。
        timeout: 单次请求超时（秒）；流式下是"整体"超时。
        voice_id: ``voices/`` 里的音色名，如 ``engu``；留空用服务端默认。
        instruction: 音色描述/表演指令（不是台词内容）。
        cfg_scale: 贴合度引导，1.0 关闭。
        temperature: 采样温度，0 = 用模型默认。
        split_chars: 长文本切分粒度，0 = 用服务端默认。
        seed: 随机种子，-1 = 每次不同（直播里别让音色每句都变）。
    """

    server: str = "http://127.0.0.1:9880"
    timeout: float = 60.0
    voice_id: str = ""
    instruction: str = ""
    cfg_scale: float = 1.0
    temperature: float = 0.0
    split_chars: int = 0
    seed: int = -1


class BreezeClient:
    """``breeze-server`` 的流式客户端。"""

    def __init__(self, params: Optional[BreezeParams] = None) -> None:
        self._params = params or BreezeParams()
        self._session: Optional[aiohttp.ClientSession] = None
        # 服务端在响应头里告诉我们采样率；没拿到之前用已知默认值
        self._sample_rate = SAMPLE_RATE

    @property
    def params(self) -> BreezeParams:
        return self._params

    @property
    def sample_rate(self) -> int:
        """最近一次合成实际使用的采样率（播放器要按它初始化）。"""

        return self._sample_rate

    def update(self, params: Optional[Any] = None) -> None:
        """热更新参数（配置改了不用重建客户端）。"""

        if params is None:
            return
        if isinstance(params, BreezeParams):
            self._params = params
            return
        # 允许传 dict：只覆盖出现过的字段
        known = {f.name for f in fields(BreezeParams)}
        patch = {k: v for k, v in dict(params).items() if k in known}
        if patch:
            self._params = replace(self._params, **patch)

    # ------------------------------------------------------------------ #
    # 合成
    # ------------------------------------------------------------------ #
    async def synthesize(self, text: str) -> bytes:
        """整段合成（调试/兜底用；正常走 :meth:`synthesize_stream`）。"""

        pieces = [chunk async for chunk in self.synthesize_stream(text)]
        data = b"".join(pieces)
        if len(data) < MIN_AUDIO_BYTES:
            raise BreezeError(f"合成结果过短（{len(data)} 字节）")
        return data

    async def synthesize_stream(
        self, text: str, *, instruction_override: Optional[str] = None
    ) -> AsyncIterator[bytes]:
        """逐块产出裸 PCM（s16le/单声道）。

        Args:
            text: 要念的台词。
            instruction_override: 本句的表演指令覆盖（voice direction）；
                None 用配置里的 instruction，空串表示"回到默认自然朗读"。

        Yields:
            bytes: 可直接写入声卡的 PCM 块；长度总是偶数（不会切断样本）。

        Raises:
            BreezeError: 服务不可达或返回错误。
        """

        content = str(text or "").strip()
        if not content:
            return
        session = self._get_session()
        form = aiohttp.FormData()
        form.add_field("text", content)
        params = self._params
        # 每句覆盖优先：AI 用【压低声音】这类记号换语气时，只影响那一句
        instruction = (
            params.instruction if instruction_override is None else str(instruction_override)
        )
        if instruction.strip():
            form.add_field("instruction", instruction.strip())
        if params.voice_id.strip():
            form.add_field("voice_id", params.voice_id.strip())
        if params.cfg_scale:
            form.add_field("cfg_scale", f"{float(params.cfg_scale):g}")
        if params.temperature:
            form.add_field("temperature", f"{float(params.temperature):g}")
        if params.split_chars:
            form.add_field("split_chars", str(int(params.split_chars)))
        if params.seed >= 0:
            form.add_field("seed", str(int(params.seed)))

        try:
            async with session.post(
                self._url("/v1/audio/speech"), data=form, timeout=aiohttp.ClientTimeout(total=params.timeout)
            ) as response:
                if response.status != 200:
                    detail = (await response.text())[:ERROR_BODY_LIMIT]
                    raise BreezeError(f"HTTP {response.status}: {detail}")
                # 服务端自报采样率；它才是播放器该用的值
                raw_rate = response.headers.get("X-Sample-Rate")
                if raw_rate and raw_rate.isdigit():
                    self._sample_rate = int(raw_rate)
                # chunk 边界可能落在样本中间：留一个字节到下一块，绝不切断样本
                tail = b""
                async for piece in response.content.iter_any():
                    if not piece:
                        continue
                    buffer = tail + piece
                    usable = len(buffer) - (len(buffer) % BYTES_PER_SAMPLE)
                    if usable <= 0:
                        tail = buffer
                        continue
                    tail = buffer[usable:]
                    yield buffer[:usable]
        except aiohttp.ClientError as exc:
            raise BreezeError(f"连接失败：{type(exc).__name__}: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise BreezeError(f"合成超时（{params.timeout:.0f}s）") from exc

    async def register_voice(
        self,
        ref_audio_path: str,
        ref_text: str,
        name: str,
        *,
        description: str = "",
    ) -> dict:
        """把一段参考音频注册成「保存音色」，之后用 voice_id 直接合成。

        ``POST /v1/voices`` 会做一次参考音频编码（这是首字延迟里最大的一块），
        然后把结果写进 ``voices/<name>.breeze``。注册一次之后，每次合成都用
        ``voice_id`` 走缓存路径，**省掉这次编码**——首字延迟从约 900ms 降到
        约 280ms（RTX 3060 实测，见 voices.md）。

        Args:
            ref_audio_path: 参考音频文件的本地绝对路径（WAV）。
            ref_text: 参考音频的逐字转写文本（必填，随音色一起保存）。
            name: 音色名，只允许字母/数字/短横/下划线，≤64 字。
            description: 可选描述（存进 .breeze 的元数据，不参与合成）。

        Returns:
            服务端返回的 voice 对象（含 id / seconds / saved 等）。

        Raises:
            BreezeError: 文件读不到、名字非法或服务端返回错误。
        """

        import os

        path = str(ref_audio_path or "").strip()
        text = str(ref_text or "").strip()
        voice_name = str(name or "").strip()
        if not path or not os.path.isfile(path):
            raise BreezeError(f"参考音频不存在：{path or '（空）'}")
        if not text:
            raise BreezeError("注册音色必须提供参考音频的转写文本（ref_text）")
        if not voice_name or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", voice_name):
            raise BreezeError(f"音色名「{voice_name}」非法：只允许字母/数字/短横/下划线，≤64 字")

        session = self._get_session()
        # 参考音频一次读完：FormData 的文件字段用字节更可控，
        # 传文件对象的话 aiohttp 发送后不会帮忙关掉，容易泄漏句柄
        with open(path, "rb") as audio_file:
            audio_bytes = audio_file.read()
        form = aiohttp.FormData()
        form.add_field("name", voice_name)
        form.add_field("ref_text", text)
        if description.strip():
            form.add_field("description", description.strip())
        form.add_field(
            "ref_audio",
            audio_bytes,
            filename=os.path.basename(path),
            content_type="audio/wav",
        )

        try:
            async with session.post(
                self._url("/v1/voices"), data=form,
                timeout=aiohttp.ClientTimeout(total=self._params.timeout),
            ) as response:
                if response.status not in (200, 201):
                    detail = (await response.text())[:ERROR_BODY_LIMIT]
                    raise BreezeError(f"注册音色失败 HTTP {response.status}: {detail}")
                payload = await response.json(content_type=None)
                if not isinstance(payload, dict):
                    raise BreezeError("注册音色返回了无法解析的内容")
                return payload
        except BreezeError:
            raise
        except aiohttp.ClientError as exc:
            raise BreezeError(f"注册音色连接失败：{type(exc).__name__}: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise BreezeError(f"注册音色超时（{self._params.timeout:.0f}s）") from exc

    async def list_voices(self) -> list[dict]:
        """列出服务端所有音色（含缓存的和保存的）。

        Returns:
            ``[{"id": "engu", "seconds": ..., "saved": True, ...}, ...]``。

        Raises:
            BreezeError: 服务不可达或返回错误。
        """

        session = self._get_session()
        try:
            async with session.get(
                self._url("/v1/voices"), timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT)
            ) as response:
                if response.status != 200:
                    detail = (await response.text())[:ERROR_BODY_LIMIT]
                    raise BreezeError(f"列音色失败 HTTP {response.status}: {detail}")
                payload = await response.json(content_type=None)
                if not isinstance(payload, list):
                    raise BreezeError("列音色返回了无法解析的内容")
                return [item for item in payload if isinstance(item, dict)]
        except BreezeError:
            raise
        except aiohttp.ClientError as exc:
            raise BreezeError(f"列音色连接失败：{type(exc).__name__}: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise BreezeError("列音色超时") from exc

    async def health(self) -> tuple[bool, str]:
        """探测服务是否可用。

        Returns:
            (是否可用, 说明)。说明里会带采样率，方便排查"配错端口"。
        """

        session = self._get_session()
        try:
            async with session.get(
                self._url("/health"), timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT)
            ) as response:
                if response.status != 200:
                    return False, f"HTTP {response.status}"
                try:
                    payload = await response.json(content_type=None)
                except Exception:  # noqa: BLE001 - 非 JSON 也当健康
                    return True, "ok"
                if isinstance(payload, dict):
                    rate = payload.get("sample_rate") or payload.get("sampleRate")
                    if isinstance(rate, int) and rate > 0:
                        self._sample_rate = rate
                    return True, f"ok（{rate or self._sample_rate} Hz）"
                return True, "ok"
        except asyncio.TimeoutError:
            return False, "健康检查超时"
        except aiohttp.ClientError as exc:
            return False, f"连接失败：{type(exc).__name__}"
        except Exception as exc:  # noqa: BLE001 - 探测不该抛
            return False, f"{type(exc).__name__}: {exc}"

    async def close(self) -> None:
        """释放连接池。"""

        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _url(self, path: str) -> str:
        base = (self._params.server or "").strip().rstrip("/")
        return f"{base}{path}"
