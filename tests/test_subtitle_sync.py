# -*- coding: utf-8 -*-
"""字幕逐句同步回归：断句规则 + 按播放进度推进。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_MODULE_NAME = "live_danmaku_adapter_subtitle_sync_test"

spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
plugin = importlib.util.module_from_spec(spec)
sys.modules[_MODULE_NAME] = plugin
spec.loader.exec_module(plugin)

split_sentences = plugin.split_subtitle_sentences
LiveDanmakuAdapter = plugin.LiveDanmakuAdapter


def _adapter(rate: float = 0.1) -> object:
    """只测字幕推进，绕开完整初始化。"""

    adapter = LiveDanmakuAdapter.__new__(LiveDanmakuAdapter)
    adapter._subtitle_seconds_per_char = rate
    adapter.pushed: list = []  # type: ignore[attr-defined]
    adapter._publish_subtitle = lambda text, auto_clear_after=None: adapter.pushed.append(text)  # type: ignore[attr-defined]
    return adapter


def test_split_keeps_every_sentence() -> None:
    """按句末标点切开，句子内容不丢字。"""

    text = "第一句话。第二句话！第三句？"
    assert split_sentences(text) == ["第一句话。", "第二句话！", "第三句？"]


def test_split_splits_long_sentence_at_comma() -> None:
    """超长句再按逗号拆一档，避免一句字幕铺满屏幕。"""

    text = "这是一个特别特别长的句子，里面不止一个停顿，所以要拆开显示。"
    parts = split_sentences(text, max_chars=12)
    assert len(parts) >= 2
    assert all(len(part) <= 24 for part in parts)
    # 拆开后拼起来仍是原文（去掉首尾空白）
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")


def test_split_empty_text() -> None:
    assert split_sentences("   ") == []
    assert split_sentences("就一句") == ["就一句"]


def test_drive_subtitle_follows_playback_progress() -> None:
    """播放推进多少，字幕就换到第几句；不是一口气全显示。"""

    adapter = _adapter(rate=0.1)
    progress = {"seconds": 0.0}
    finished = asyncio.Event()

    async def run() -> None:
        task = asyncio.create_task(adapter._drive_subtitle("甲。乙。丙。", progress, finished))
        await asyncio.sleep(0.12)
        assert adapter.pushed == ["甲。"]

        progress["seconds"] = 0.25  # 约第 2 句
        await asyncio.sleep(0.12)
        assert adapter.pushed[-1] == "乙。"

        progress["seconds"] = 0.45  # 约第 3 句
        await asyncio.sleep(0.12)
        assert adapter.pushed[-1] == "丙。"

        finished.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())
    # 每句只推一次，没有重复覆盖
    assert adapter.pushed == ["甲。", "乙。", "丙。"]


def test_drive_subtitle_shows_last_sentence_on_finish() -> None:
    """语速比估算快时，播完也要把最后一句补出来，不能停在中间。"""

    adapter = _adapter(rate=0.1)
    progress = {"seconds": 0.0}
    finished = asyncio.Event()

    async def run() -> None:
        task = asyncio.create_task(adapter._drive_subtitle("一号。二号。三号。", progress, finished))
        await asyncio.sleep(0.1)
        # 进度几乎没走就直接结束：字幕也应该落到最后一句
        finished.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())
    assert adapter.pushed[-1] == "三号。"


def test_single_sentence_keeps_original_text() -> None:
    """只有一句时原样显示，不做任何切分。"""

    adapter = _adapter()
    progress = {"seconds": 0.0}
    finished = asyncio.Event()

    async def run() -> None:
        task = asyncio.create_task(adapter._drive_subtitle("就一句话", progress, finished))
        await asyncio.sleep(0.05)
        finished.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())
    assert adapter.pushed == ["就一句话"]


def main() -> int:
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
    print("字幕逐句同步回归通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
