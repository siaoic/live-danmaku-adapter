"""「错别字纠正碎片」回归测试：宿主后处理不该污染直播 TTS / 字幕。

对应现场：宿主 ``[chinese_typo]`` 把「叫」打成「教」后，又发了一条
正文只有「叫」、且引用自己上一条消息的独立回复（截图里的第三条）。
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_MODULE_NAME = "live_danmaku_adapter_correction_test"

spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[_MODULE_NAME] = module
spec.loader.exec_module(module)

from live_danmaku_adapter_correction_test.plugin import LiveDanmakuAdapter  # noqa: E402

_is_fragment = LiveDanmakuAdapter._is_correction_fragment


def test_real_correction_fragment() -> None:
    """正文一个字 + 引用自己发的消息 -> 判为纠正碎片。"""

    msg = {"reply_to": "send_api_1793865300577", "processed_plain_text": "叫"}
    assert _is_fragment(msg, "叫") is True
    assert _is_fragment(msg, " 叫。 ") is True  # 带标点/空白
    assert _is_fragment(msg, "重塑") is True  # 纠正一个词也算


def test_normal_short_reply_not_filtered() -> None:
    """正常短回复引用的是观众弹幕，不该被误伤。"""

    assert _is_fragment({"reply_to": "Live-douyin-1793865288-2"}, "好") is False
    assert _is_fragment({"reply_to": "live-bilibili-1"}, "嗯") is False
    assert _is_fragment({}, "叫") is False  # 没引用自己，不能拦


def test_long_reply_not_filtered() -> None:
    """即使引用了自己上一条，正常长回复也必须照常播报。"""

    msg = {"reply_to": "send_api_1793865300577"}
    long_text = "晚上换巨魔，直接让对面感受什么叫“重塑”。都下播了还搁这儿刷弹幕，我吃饭去了。"
    assert _is_fragment(msg, long_text) is False
    assert _is_fragment(msg, "") is False


def test_host_config_disables_typo() -> None:
    """根治开关：宿主错字注入关闭（回复断句由 test_speech_gate.py 单独守）。"""

    config_path = _PLUGIN_DIR.parent.parent / "config" / "bot_config.toml"
    with config_path.open("rb") as fh:
        cfg = tomllib.load(fh)
    assert cfg["chinese_typo"]["enable"] is False
    assert cfg["chinese_typo"]["enable_correction_quote"] is False


def main() -> int:
    test_real_correction_fragment()
    test_normal_short_reply_not_filtered()
    test_long_reply_not_filtered()
    test_host_config_disables_typo()
    print("错别字纠正碎片回归通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
