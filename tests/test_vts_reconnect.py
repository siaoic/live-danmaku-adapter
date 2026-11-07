"""VTS 后台重连任务生命周期测试：不连真实 VTS。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_MODULE_NAME = "live_danmaku_adapter_vts_reconnect_test"

spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[_MODULE_NAME] = module
spec.loader.exec_module(module)

from live_danmaku_adapter_vts_reconnect_test.plugin import LiveDanmakuAdapter  # noqa: E402


def test_reconnect_task_start_and_stop() -> None:
    """_start_vts_reconnect 创建任务，_stop_vts_reconnect 取消任务。"""

    async def _run() -> None:
        plugin = LiveDanmakuAdapter.__new__(LiveDanmakuAdapter)
        plugin._vts_reconnect_task = None
        plugin._vts_client = None
        # 循环第一次进入 sleep(10) 后就被取消，不会走到访问 config 的代码

        plugin._start_vts_reconnect()
        assert plugin._vts_reconnect_task is not None
        assert not plugin._vts_reconnect_task.done()

        await asyncio.sleep(0)  # 让任务跑起来、进入 sleep
        await plugin._stop_vts_reconnect()
        assert plugin._vts_reconnect_task is None

    asyncio.run(_run())
    print("OK VTS 重连任务启停正常")


def test_reconnect_idempotent() -> None:
    """重复调用 _start_vts_reconnect 不会创建多个任务。"""

    async def _run() -> None:
        plugin = LiveDanmakuAdapter.__new__(LiveDanmakuAdapter)
        plugin._vts_reconnect_task = None
        plugin._vts_client = None

        plugin._start_vts_reconnect()
        first = plugin._vts_reconnect_task
        plugin._start_vts_reconnect()  # 已存在，不重复创建
        assert plugin._vts_reconnect_task is first
        await plugin._stop_vts_reconnect()

    asyncio.run(_run())
    print("OK 重连任务幂等")


def main() -> int:
    test_reconnect_task_start_and_stop()
    test_reconnect_idempotent()
    print("VTS 重连回归通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
