# -*- coding: utf-8 -*-
"""动作词表缓存：过期时不能在请求前干等。

``_warm_actions`` 挂在 Planner/Replyer 的请求前钩子上，属于「发请求之前」
的同步路径。它曾经在 TTL 到期后直接用 ``await engine.actions()`` 拉一次，
引擎卡住时最长等 2 秒——这 2 秒会一分不差地加到观众听到第一句之前。

现在改成「过期先用旧值、后台补一次」：本文件锁住这三条行为，防止以后
为了「词表更新更及时」把它改回同步等待。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
import tomllib
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_MODULE_NAME = "live_danmaku_adapter_action_cache_test"

spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
plugin = importlib.util.module_from_spec(spec)
sys.modules[_MODULE_NAME] = plugin
spec.loader.exec_module(plugin)


class FakeEngine:
    """记录被调用次数的假引擎。"""

    def __init__(self, delay: float = 0.0) -> None:
        self.calls = 0
        self.delay = delay

    async def actions(self) -> dict:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return {"actions": [{"key": "nod", "kind": "small_action"}]}


def _adapter() -> plugin.LiveDanmakuAdapter:
    data = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    adapter = plugin.LiveDanmakuAdapter()
    adapter._plugin_config_instance = plugin.LiveDanmakuConfig.model_validate(data)
    return adapter


def test_fresh_cache_does_not_touch_engine() -> None:
    adapter = _adapter()
    engine = FakeEngine()
    adapter._engine = engine
    adapter._actions_cache = ({"actions": []}, time.monotonic())

    asyncio.run(adapter._warm_actions())

    assert engine.calls == 0
    assert adapter._actions_refresh_task is None


def test_expired_cache_returns_immediately_and_refreshes_in_background() -> None:
    """引擎慢到 5 秒也必须立刻返回：这 5 秒属于观众的等待。"""

    adapter = _adapter()
    engine = FakeEngine(delay=5.0)
    adapter._engine = engine
    adapter._actions_cache = ({"actions": []}, time.monotonic() - 3600)

    async def run() -> float:
        started = time.monotonic()
        await adapter._warm_actions()
        elapsed = time.monotonic() - started
        task = adapter._actions_refresh_task
        assert task is not None
        task.cancel()
        return elapsed

    elapsed = asyncio.run(run())

    assert elapsed < 0.2
    assert adapter._actions_refresh_task is not None


def test_cold_start_still_fetches_synchronously() -> None:
    """一次都没拿到过时必须等：否则这轮模型手里没有记号清单。"""

    adapter = _adapter()
    engine = FakeEngine()
    adapter._engine = engine
    adapter._actions_cache = None

    asyncio.run(adapter._warm_actions())

    assert engine.calls == 1
    assert adapter._actions_cache is not None
    assert adapter._actions_cache[0]["actions"][0]["key"] == "nod"


def test_failed_fetch_is_not_cached() -> None:
    """拉取失败不写缓存，下一轮还会再试。"""

    class BrokenEngine:
        async def actions(self) -> dict:
            raise RuntimeError("engine down")

    adapter = _adapter()
    adapter._engine = BrokenEngine()
    adapter._actions_cache = None

    asyncio.run(adapter._warm_actions())

    assert adapter._actions_cache is None
