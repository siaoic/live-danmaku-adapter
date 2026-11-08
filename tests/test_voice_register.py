# -*- coding: utf-8 -*-
"""启动时自动注册 Breeze 音色的行为测试（假客户端，不连服务）。

三条契约：
  1. 音色已存在 → 跳过注册，但要把配置里的 voice_id 同步成这个名字；
  2. 音色不存在 → 用参考音频注册，成功后 voice_id 同步；
  3. 没填参考音频 / 注册失败 → 不注册也不崩溃，合成继续用原 voice_id。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tomllib
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR))

_MODULE_NAME = "live_danmaku_voice_register_test"
_spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
plugin = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = plugin
_spec.loader.exec_module(plugin)

from tts.breeze import BreezeClient, BreezeError, BreezeParams  # noqa: E402


class _FakeBreeze(BreezeClient):
    """替身：方法签名与真客户端一致，行为可控。"""

    def __init__(self, voices, *, fail_register: bool = False, fail_health: bool = False) -> None:
        super().__init__(BreezeParams())
        self._voices = voices
        self.fail_register = fail_register
        self.fail_health = fail_health
        self.register_calls: list[dict] = []

    async def health(self):
        if self.fail_health:
            return False, "连接失败"
        return True, "ok（24000 Hz）"

    async def list_voices(self):
        return self._voices

    async def register_voice(self, ref_audio_path, ref_text, name, *, description=""):
        if self.fail_register:
            raise BreezeError("服务端拒绝")
        self.register_calls.append(
            {"path": ref_audio_path, "text": ref_text, "name": name, "description": description}
        )
        return {"id": name, "saved": True, "seconds": 5.4}


def _adapter(client, *, ref_path="", ref_text="参考文本", voice_name="live_voice",
             voice_id="engu"):
    data = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    adapter = plugin.LiveDanmakuAdapter()
    adapter._plugin_config_instance = plugin.LiveDanmakuConfig.model_validate(data)
    adapter._tts_client = client
    cfg = adapter.config.tts
    cfg.ref_audio_path = ref_path
    cfg.ref_text = ref_text
    cfg.voice_name = voice_name
    cfg.voice_id = voice_id
    return adapter


def test_existing_voice_skips_registration_but_syncs() -> None:
    """目标音色已存在：不重复注册，voice_id 要跟上来。"""

    client = _FakeBreeze([{"id": "live_voice", "saved": True}])
    adapter = _adapter(client, voice_id="engu")
    asyncio.run(adapter._ensure_breeze_voice())

    assert client.register_calls == [], "已存在的音色不该再注册"
    assert adapter.config.tts.voice_id == "live_voice", "voice_id 没同步到已存在的音色"


def test_missing_voice_registers_and_syncs(tmp_path) -> None:
    """音色不存在：用参考音频注册，成功后 voice_id 同步。"""

    wav = tmp_path / "ref.wav"
    wav.write_bytes(b"RIFF fake")
    client = _FakeBreeze([{"id": "engu", "saved": True}])
    adapter = _adapter(client, ref_path=str(wav))
    asyncio.run(adapter._ensure_breeze_voice())

    assert len(client.register_calls) == 1
    call = client.register_calls[0]
    assert call["name"] == "live_voice"
    assert call["text"] == "参考文本"
    assert adapter.config.tts.voice_id == "live_voice"
    assert client.params.voice_id == "live_voice", "客户端参数没跟着换"


def test_no_ref_audio_is_noop() -> None:
    """没填参考音频：什么都不做，voice_id 保持原样。"""

    client = _FakeBreeze([])
    adapter = _adapter(client, ref_path="")
    asyncio.run(adapter._ensure_breeze_voice())

    assert client.register_calls == []
    assert adapter.config.tts.voice_id == "engu"


def test_register_failure_keeps_original_voice() -> None:
    """注册失败：不崩溃，合成继续用原 voice_id。"""

    wav = Path(_PLUGIN_DIR / "config.toml")  # 任意存在的文件，让文件检查通过
    client = _FakeBreeze([], fail_register=True)
    adapter = _adapter(client, ref_path=str(wav))
    asyncio.run(adapter._ensure_breeze_voice())

    assert adapter.config.tts.voice_id == "engu", "失败后不应改音色"


def test_disabled_or_unreachable_is_noop(monkeypatch) -> None:
    """服务不可达：不折腾，注册是锦上添花。"""

    async def _nosleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _nosleep)

    client = _FakeBreeze([], fail_health=True)
    adapter = _adapter(client, ref_path="E:/x/ref.wav")
    asyncio.run(adapter._ensure_breeze_voice())
    assert client.register_calls == []



def main() -> int:
    test_existing_voice_skips_registration_but_syncs()
    test_missing_voice_registers_and_syncs(tmp_path=Path("_tmp_voice_test").resolve())
    test_no_ref_audio_is_noop()
    test_register_failure_keeps_original_voice()
    test_disabled_or_unreachable_is_noop()
    print("Breeze 音色自动注册通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
