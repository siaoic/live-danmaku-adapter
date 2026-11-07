"""B 站礼物价格单位回归：GiftMessage.price 是瓜子数，必须 /1000 才是元。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_MODULE_NAME = "live_danmaku_adapter_gift_price_test"

spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[_MODULE_NAME] = module
spec.loader.exec_module(module)

from live_danmaku_adapter_gift_price_test.platforms.bilibili import _Handler  # noqa: E402


class _FakeGift:
    """模拟 blivedm 的 GiftMessage：灯牌单价 100 瓜子 = 0.1 元。"""

    uid = 12345
    uname = "观众甲"
    gift_name = "粉丝团灯牌"
    gift_id = 1
    num = 1
    price = 100  # 瓜子数
    coin_type = "gold"
    face = ""
    guard_level = 0
    tid = "t-1"
    rnd = "r-1"
    timestamp = 1700000000


class _FakeLink:
    """记录 submit 收到的 LiveEvent，供断言 price。"""

    room_id = 22632424

    def __init__(self):
        self.event = None
        self._seq = 0

    def submit(self, event):
        self.event = event

    def next_event_id(self, base_id):
        self._seq += 1
        return f"{base_id}-{self._seq}"


def test_gift_price_converted_to_yuan() -> None:
    """灯牌 100 瓜子应换算成 0.1 元，而不是 ¥100。"""

    handler = _Handler.__new__(_Handler)
    link = _FakeLink()
    handler._link = link
    handler._on_gift(None, _FakeGift())
    assert link.event is not None
    assert link.event.price == 0.1, f"礼物价格应 /1000 成元，实际 {link.event.price}"
    print(f"OK 灯牌价格换算正确：{link.event.price} 元")


def test_gift_price_zero_gift() -> None:
    """免费礼物 price=0，换算后仍是 0。"""

    handler = _Handler.__new__(_Handler)
    link = _FakeLink()
    handler._link = link
    gift = _FakeGift()
    gift.price = 0
    handler._on_gift(None, gift)
    assert link.event.price == 0.0


def main() -> int:
    test_gift_price_converted_to_yuan()
    test_gift_price_zero_gift()
    print("礼物价格回归通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
