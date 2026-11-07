# -*- coding: utf-8 -*-
"""Breeze TTS 2 客户端单元测试（不连真服务，用 aiohttp mock）。"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR))

import pytest  # noqa: E402

from tts.breeze import BreezeClient, BreezeError, BreezeParams, SAMPLE_RATE  # noqa: E402


def test_params_defaults() -> None:
    params = BreezeParams()
    assert params.server == "http://127.0.0.1:9880"
    assert params.voice_id == ""
    assert params.cfg_scale == 1.0
    assert params.temperature == 0.0


def test_update_accepts_dict() -> None:
    client = BreezeClient(BreezeParams())
    client.update({"voice_id": "engu", "cfg_scale": 1.5})
    assert client.params.voice_id == "engu"
    assert client.params.cfg_scale == 1.5
    # 没出现的字段保持不变
    assert client.params.server == "http://127.0.0.1:9880"


def test_update_ignores_unknown_keys() -> None:
    client = BreezeClient(BreezeParams())
    client.update({"voice_id": "engu", "not_a_real_field": 123})
    assert not hasattr(client.params, "not_a_real_field")
    assert client.params.voice_id == "engu"


def test_sample_rate_default() -> None:
    client = BreezeClient()
    assert client.sample_rate == 24000
    assert SAMPLE_RATE == 24000


class _FakeResponse:
    """模拟 aiohttp 流式响应：带 X-Sample-Rate 头，按块产出裸 PCM。"""

    def __init__(self, chunks=None, *, status=200, rate="24000", body=b"err"):
        self.status = status
        self._chunks = chunks if chunks is not None else [b"\x01\x00"] * 200
        self._rate = rate
        self._body = body

    @classmethod
    def chunks_default(cls):
        """一段足够长的默认 PCM（注册接口不会真的读它）。"""

        return [b"\x01\x00"] * 200

    @property
    def headers(self):
        return {"X-Sample-Rate": self._rate}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        if isinstance(self._body, bytes):
            return self._body.decode("utf-8", "replace")
        return str(self._body)

    @property
    def content(self):
        return _FakeContent(self._chunks)


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    def iter_any(self):
        return self


class _FakeSession:
    """拦截 POST，返回预设响应；记录 form 字段供断言。"""

    def __init__(self, response):
        self._response = response
        self.last_form = None
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, *, data, timeout):
        # 从 aiohttp.FormData 里挖出字段值：(MultiDict, dict, value) 三元组
        form = {}
        for headers, _meta, value in data._fields:
            form[headers["name"]] = value
        self.last_form = form
        self.last_url = url
        return self._response

    def get(self, url, timeout=None):
        self.last_get_url = url
        return self._response

    async def close(self):
        self.closed = True


def _make_client(params, response):
    client = BreezeClient(params)
    client._session = _FakeSession(response)
    return client


async def _collect(client, text):
    return [piece async for piece in client.synthesize_stream(text)]


def test_stream_yields_pcm_and_sets_rate() -> None:
    async def run():
        params = BreezeParams(voice_id="engu", instruction="可爱少女")
        chunks = [b"\x01\x00", b"\x02\x00", b"\x03"]
        client = _make_client(params, _FakeResponse(chunks))
        pieces = await _collect(client, "你好")
        # 三块 2+2+1 字节 = 5 字节，最后 1 字节是奇数尾，必须等下一块凑偶
        assert pieces == [b"\x01\x00", b"\x02\x00"]
        assert client.sample_rate == 24000
        # form 字段带上了音色与指令
        form = client._session.last_form
        assert form["voice_id"] == "engu"
        assert form["instruction"] == "可爱少女"
        assert form["text"] == "你好"

    import asyncio
    asyncio.run(run())


def test_odd_tail_byte_carried_to_next_chunk() -> None:
    async def run():
        # 第一块 3 字节（奇），第二块 1 字节：应拼成 4 字节 = 2 个样本
        chunks = [b"\x01\x00\x02", b"\x00"]
        client = _make_client(BreezeParams(), _FakeResponse(chunks))
        pieces = await _collect(client, "测试")
        assert pieces == [b"\x01\x00", b"\x02\x00"]

    import asyncio
    asyncio.run(run())


def test_http_error_raises_breeze_error() -> None:
    async def run():
        client = _make_client(BreezeParams(), _FakeResponse([], status=500, body="boom"))
        with pytest.raises(BreezeError):
            await _collect(client, "测试")

    import asyncio
    asyncio.run(run())


def test_synthesize_short_result_raises() -> None:
    async def run():
        client = _make_client(BreezeParams(), _FakeResponse([b"\x01\x00"]))
        with pytest.raises(BreezeError):
            await client.synthesize("测试")

    import asyncio
    asyncio.run(run())


def test_empty_text_produces_nothing() -> None:
    async def run():
        client = BreezeClient()
        pieces = [piece async for piece in client.synthesize_stream("   ")]
        assert pieces == []

    import asyncio
    asyncio.run(run())


def test_close_releases_session() -> None:
    async def run():
        client = BreezeClient()
        client._session = _FakeSession(_FakeResponse([]))
        await client.close()
        assert client._session is None

    import asyncio
    asyncio.run(run())


# ------------------------------------------------------------------ #
#  音色注册与列表
# ------------------------------------------------------------------ #
class _FakeMultipartResponse(_FakeResponse):
    """注册接口的响应：返回 JSON。"""

    async def json(self, content_type=None):
        return {"id": "live_voice", "saved": True, "seconds": 5.4}


def test_register_voice_sends_name_ref_text_and_file(tmp_path) -> None:
    async def run():
        wav = tmp_path / "ref.wav"
        wav.write_bytes(b"RIFF fake wav data")
        client = _make_client(
            BreezeParams(), _FakeMultipartResponse(_FakeResponse.chunks_default())
        )
        result = await client.register_voice(
            str(wav), "参考音频里说的这句话", "live_voice", description="可爱少女"
        )
        assert result["saved"] is True
        form = client._session.last_form
        assert form["name"] == "live_voice"
        assert form["ref_text"] == "参考音频里说的这句话"
        assert form["description"] == "可爱少女"
        # ref_audio 是 multipart 文件字段：传的是字节，内容即文件内容
        assert form["ref_audio"] == b"RIFF fake wav data"

    import asyncio
    asyncio.run(run())


def test_register_voice_rejects_bad_name(tmp_path) -> None:
    async def run():
        wav = tmp_path / "ref.wav"
        wav.write_bytes(b"x")
        client = _make_client(BreezeParams(), _FakeMultipartResponse(_FakeResponse.chunks_default()))
        for bad in ("", "有中文", "bad name", "a" * 65, "../evil"):
            try:
                await client.register_voice(str(wav), "文本", bad)
            except BreezeError as exc:
                assert "非法" in str(exc) or "必须" in str(exc)
            else:
                raise AssertionError(f"非法名字「{bad}」竟然通过了")

    import asyncio
    asyncio.run(run())


def test_register_voice_rejects_missing_file_or_text(tmp_path) -> None:
    async def run():
        client = _make_client(BreezeParams(), _FakeMultipartResponse(_FakeResponse.chunks_default()))
        # 文件不存在
        try:
            await client.register_voice(str(tmp_path / "no.wav"), "文本", "ok_name")
        except BreezeError as exc:
            assert "不存在" in str(exc)
        else:
            raise AssertionError("不存在的文件竟然通过了")
        # 缺转写文本
        wav = tmp_path / "ref.wav"
        wav.write_bytes(b"x")
        try:
            await client.register_voice(str(wav), "", "ok_name")
        except BreezeError as exc:
            assert "转写" in str(exc)
        else:
            raise AssertionError("缺转写文本竟然通过了")

    import asyncio
    asyncio.run(run())


def test_list_voices_parses_json() -> None:
    class _ListResponse:
        status = 200

        async def json(self, content_type=None):
            return [{"id": "engu", "saved": True, "seconds": 5.36}]

        async def text(self):
            return "[]"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def run():
        client = BreezeClient()
        client._session = _FakeSession(_ListResponse())
        voices = await client.list_voices()
        assert voices[0]["id"] == "engu"

    import asyncio
    asyncio.run(run())


def main() -> int:
    import asyncio

    asyncio.run(asyncio.gather())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
