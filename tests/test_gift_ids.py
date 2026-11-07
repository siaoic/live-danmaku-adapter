"""礼物连击去重回归：同一连击的多条礼物必须互相去重不互斥。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_MODULE_NAME = "live_danmaku_adapter_gift_test"

spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[_MODULE_NAME] = module
spec.loader.exec_module(module)

from live_danmaku_adapter_gift_test.platforms import BilibiliLiveLink  # noqa: E402


class _Channel:
    """只记录日志的最小通道替身。"""

    logger = type("L", (), {"info": staticmethod(lambda *a, **k: None), "warning": staticmethod(lambda *a, **k: None), "error": staticmethod(lambda *a, **k: None)})()

    def submit(self, event):
        pass

    def update_source(self, *args, **kwargs):
        pass


def test_combo_ids_are_unique() -> None:
    """一次连击拆出来的多条礼物，事件 ID 必须各不相同。"""

    link = BilibiliLiveLink.__new__(BilibiliLiveLink)
    link._channel = _Channel()
    link._event_seq = 0
    base = "gift-combo-abc-22459-1"
    ids = [link.next_event_id(base) for _ in range(5)]
    assert len(set(ids)) == 5, "同连击的礼物事件 ID 不能重复: %r" % (ids,)
    assert all(item.startswith(base) for item in ids)
    print("OK 礼物连击事件ID唯一:", ids[0], "...")


def main() -> int:
    test_combo_ids_are_unique()
    print("礼物去重回归通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
