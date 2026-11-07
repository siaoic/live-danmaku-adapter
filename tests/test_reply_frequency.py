# -*- coding: utf-8 -*-
"""弹幕回复节流回归：只改直播流的频率倍率，不动全局配置与主动对话。

用假的 capability RPC 替代宿主，验证：目标倍率换算、首次下发、已一致跳过、
宿主重置后的自愈、能力不可用时降级、卸载还原、以及新流触发立即下发。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_MODULE_NAME = "live_danmaku_adapter_reply_frequency_test"

spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[_MODULE_NAME] = module
spec.loader.exec_module(module)

from live_danmaku_adapter_reply_frequency_test.plugin import (  # noqa: E402
    LiveDanmakuAdapter,
    LiveDanmakuConfig,
    ReplyConfig,
    resolve_reply_frequency,
)


class FakeCapabilityCtx:
    """模拟 SDK 归一化后的能力返回值：get_adjust→float，set_adjust→bool。"""

    def __init__(self, values: Dict[str, float] | None = None, *, fail: bool = False,
                 set_noop: bool = False) -> None:
        self.values: Dict[str, float] = dict(values or {})
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.fail = fail
        self.set_noop = set_noop

    async def call_capability(self, capability: str, timeout_ms: int | None = None, **kwargs: Any) -> Any:
        del timeout_ms
        self.calls.append((capability, kwargs))
        if self.fail:
            raise RuntimeError("capability denied")
        stream_id = str(kwargs.get("chat_id") or "")
        if capability == "frequency.get_adjust":
            return self.values.get(stream_id, 1.0)
        if capability == "frequency.set_adjust":
            if not self.set_noop:
                self.values[stream_id] = float(kwargs["value"])
            return True
        return {"success": False, "error": "unknown capability"}

    def set_calls(self) -> List[Dict[str, Any]]:
        return [args for name, args in self.calls if name == "frequency.set_adjust"]


def _make_adapter(
    ctx: FakeCapabilityCtx,
    *,
    enabled: bool = True,
    value: float = 0.3,
    recheck: int = 10,
    streams: Tuple[str, ...] = ("live-all",),
) -> LiveDanmakuAdapter:
    adapter = LiveDanmakuAdapter()
    adapter._plugin_config_instance = LiveDanmakuConfig(
        reply=ReplyConfig(enabled=enabled, danmaku_frequency=value, recheck_seconds=recheck)
    )
    adapter._ctx = ctx
    for stream_id in streams:
        adapter._live_stream_ids[stream_id] = None
    return adapter


def test_resolve_reply_frequency() -> None:
    """倍率换算：关闭节流交还 1.0，越界裁剪，0 不能真给 0。"""

    assert resolve_reply_frequency(True, 0.3) == 0.3
    assert resolve_reply_frequency(False, 0.3) == 1.0
    assert resolve_reply_frequency(True, 0.0) == 0.05
    assert resolve_reply_frequency(True, -1) == 0.05
    assert resolve_reply_frequency(True, 9) == 1.0
    assert resolve_reply_frequency(True, "bad") == 0.3  # type: ignore[arg-type]


def test_first_apply_sets_frequency() -> None:
    """首次下发：读 1.0 → 设 0.3 → 二次读取确认生效。"""

    async def _run() -> None:
        ctx = FakeCapabilityCtx()
        adapter = _make_adapter(ctx)
        assert await adapter._sync_one_stream_frequency("live-all", 0.3) is True
        assert ctx.values["live-all"] == 0.3
        assert ctx.set_calls() == [{"chat_id": "live-all", "value": 0.3}]

    asyncio.run(_run())


def test_already_matching_does_not_set() -> None:
    """倍率已经一致时不重复设置：set_adjust 会额外调度一轮规划，必须省掉。"""

    async def _run() -> None:
        ctx = FakeCapabilityCtx({"live-all": 0.3})
        adapter = _make_adapter(ctx)
        await adapter._apply_reply_frequency(force=True)
        assert ctx.set_calls() == []
        assert len(ctx.calls) == 1

    asyncio.run(_run())


def test_host_reset_is_healed() -> None:
    """宿主重建聊天流把倍率重置成 1.0 后，巡检会补回来。"""

    async def _run() -> None:
        ctx = FakeCapabilityCtx()
        adapter = _make_adapter(ctx)
        await adapter._apply_reply_frequency(force=True)
        assert ctx.values["live-all"] == 0.3

        ctx.values["live-all"] = 1.0  # 模拟宿主重建 heartflow
        await adapter._apply_reply_frequency(force=True)
        assert ctx.values["live-all"] == 0.3
        assert len(ctx.set_calls()) == 2

    asyncio.run(_run())


def test_recheck_throttle() -> None:
    """非强制巡检受 recheck_seconds 节流，不每秒打一次 RPC。"""

    async def _run() -> None:
        ctx = FakeCapabilityCtx()
        adapter = _make_adapter(ctx, recheck=60)
        await adapter._apply_reply_frequency()
        first = len(ctx.calls)
        await adapter._apply_reply_frequency()
        assert len(ctx.calls) == first

    asyncio.run(_run())


def test_disabled_restores_default() -> None:
    """关掉节流后把倍率还给 1.0，不把宿主状态留成低频率。"""

    async def _run() -> None:
        ctx = FakeCapabilityCtx({"live-all": 0.3})
        adapter = _make_adapter(ctx, enabled=False)
        await adapter._apply_reply_frequency(force=True)
        assert ctx.values["live-all"] == 1.0

    asyncio.run(_run())


def test_capability_failure_degrades_quietly() -> None:
    """能力没授权/调用异常时只警告一次，不抛异常也不影响直播。"""

    async def _run() -> None:
        ctx = FakeCapabilityCtx(fail=True)
        adapter = _make_adapter(ctx)
        await adapter._apply_reply_frequency(force=True)
        assert adapter._reply_adjust_warned is True
        assert len(ctx.calls) == 1  # 第一次就失败，警告去重

        await adapter._apply_reply_frequency(force=True)
        assert len(ctx.calls) == 2

    asyncio.run(_run())


def test_set_not_applied_is_retried() -> None:
    """宿主返回 success 但流还没建好：不缓存假成功，下一轮继续重试。"""

    async def _run() -> None:
        ctx = FakeCapabilityCtx(set_noop=True)
        adapter = _make_adapter(ctx)
        assert await adapter._sync_one_stream_frequency("live-all", 0.3) is False
        ctx.set_noop = False
        assert await adapter._sync_one_stream_frequency("live-all", 0.3) is True
        assert ctx.values["live-all"] == 0.3

    asyncio.run(_run())


def test_multiple_streams_independent() -> None:
    """两条直播流分别下发；本地聊天室的流不在集合里，绝不被改动。"""

    async def _run() -> None:
        ctx = FakeCapabilityCtx()
        adapter = _make_adapter(ctx, streams=("live-all", "bilibili:26960792"))
        await adapter._apply_reply_frequency(force=True)
        assert ctx.values == {"live-all": 0.3, "bilibili:26960792": 0.3}
        assert {args["chat_id"] for args in ctx.set_calls()} == {"live-all", "bilibili:26960792"}

    asyncio.run(_run())


def test_unload_restores_frequency() -> None:
    """卸载插件时把直播流倍率还原成 1.0。"""

    async def _run() -> None:
        ctx = FakeCapabilityCtx({"live-all": 0.3})
        adapter = _make_adapter(ctx)
        await adapter._restore_reply_frequency()
        assert ctx.values["live-all"] == 1.0

    asyncio.run(_run())


def test_reply_config_falls_back_to_defaults() -> None:
    """配置还没注入（或旧 schema 没有 reply 段）时用默认 0.08，不让插件加载失败。"""

    adapter = LiveDanmakuAdapter()
    adapter._plugin_config_instance = None
    config = adapter._reply_config()
    assert config.enabled is True
    assert config.danmaku_frequency == 0.08
    assert resolve_reply_frequency(config.enabled, config.danmaku_frequency) == 0.08


def test_new_stream_marks_dirty() -> None:
    """activity_hook 学到新的直播流时，巡检下一轮立刻下发倍率。"""

    async def _run() -> None:
        ctx = FakeCapabilityCtx()
        adapter = _make_adapter(ctx, streams=())
        message = {
            "platform": "live",
            "session_id": "live-all",
            "message_id": "m-1",
            "raw_message": [{"type": "text", "data": "[B站弹幕] 小明: 你好"}],
        }
        assert adapter._reply_adjust_dirty is False
        await adapter.activity_hook(message=message)
        assert adapter._reply_adjust_dirty is True
        assert "live-all" in adapter._live_stream_ids
        # 同一条流再来一次不再标脏
        adapter._reply_adjust_dirty = False
        await adapter.activity_hook(message=message)
        assert adapter._reply_adjust_dirty is False

    asyncio.run(_run())


def main() -> int:
    test_resolve_reply_frequency()
    test_first_apply_sets_frequency()
    test_already_matching_does_not_set()
    test_host_reset_is_healed()
    test_recheck_throttle()
    test_disabled_restores_default()
    test_capability_failure_degrades_quietly()
    test_set_not_applied_is_retried()
    test_multiple_streams_independent()
    test_unload_restores_frequency()
    test_reply_config_falls_back_to_defaults()
    test_new_stream_marks_dirty()
    print("弹幕回复节流回归通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
