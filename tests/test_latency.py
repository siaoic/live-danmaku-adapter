"""TurnLatency：把一轮回复拆成等待 / Planner / Replyer 三段的观测器。

这些测试只关心「数字算得对不对、边界会不会把总时长算丢」，不碰任何宿主
行为——埋点坏掉的代价是看不到数据，绝不能让它变成回复链路的故障点。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from core.latency import SUMMARY_TURNS, TurnLatency  # noqa: E402


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


def test_reports_three_segments(clock: FakeClock) -> None:
    latency = TurnLatency(clock=clock)
    latency.begin()
    clock.advance(0.25)
    latency.mark("planner_start")
    clock.advance(0.75)
    latency.mark("replyer_start")
    clock.advance(1.5)
    line = latency.report()

    assert line is not None
    assert "弹幕→首段 2.50s" in line
    assert "等待 0.25s" in line
    assert "Planner 0.75s" in line
    assert "Replyer 1.50s" in line


def test_missing_replyer_folds_into_planner(clock: FakeClock) -> None:
    """Planner 直接 wait、或 Replyer 未触发的轮次：不能凭空少掉时间。"""

    latency = TurnLatency(clock=clock)
    latency.begin()
    clock.advance(0.5)
    latency.mark("planner_start")
    clock.advance(1.0)
    line = latency.report()

    assert line is not None
    assert "弹幕→首段 1.50s" in line
    assert "Planner 1.00s" in line
    assert "Replyer 0.00s" in line


def test_new_message_does_not_move_the_start(clock: FakeClock) -> None:
    """一轮还没结算时又来弹幕：起点仍是第一条，否则等待段会被算短。"""

    latency = TurnLatency(clock=clock)
    assert latency.begin() is True
    clock.advance(1.0)
    assert latency.begin() is False
    clock.advance(1.0)
    latency.mark("planner_start")
    clock.advance(1.0)
    line = latency.report()

    assert line is not None
    assert "弹幕→首段 3.00s" in line
    assert "等待 2.00s" in line


def test_report_is_idempotent(clock: FakeClock) -> None:
    latency = TurnLatency(clock=clock)
    latency.begin()
    latency.mark("planner_start")
    assert latency.report() is not None
    assert latency.report() is None


def test_mark_opens_new_turn_after_report(clock: FakeClock) -> None:
    """主动轮没有入站消息：第一个里程碑自己开一轮，等待段为 0。"""

    latency = TurnLatency(clock=clock)
    latency.begin()
    latency.mark("planner_start")
    latency.report()

    clock.advance(5.0)
    assert latency.mark("planner_start") is True
    clock.advance(1.0)
    line = latency.report()

    assert line is not None
    assert "触发→首段 1.00s" in line
    assert "等待 0.00s" in line


def test_duplicate_marks_keep_first(clock: FakeClock) -> None:
    """工具循环会让 Planner 跑多次，只认第一次，否则统计越拖越长。"""

    latency = TurnLatency(clock=clock)
    latency.begin()
    latency.mark("planner_start")
    clock.advance(1.0)
    assert latency.mark("planner_start") is False
    clock.advance(1.0)
    line = latency.report()

    assert line is not None
    # 第二次 mark 没有覆盖起点：整段 2 秒都记在 Planner 上
    assert "等待 0.00s" in line
    assert "Planner 2.00s" in line


def test_first_send_at_exposed(clock: FakeClock) -> None:
    latency = TurnLatency(clock=clock)
    latency.begin()
    stamp = clock.now
    latency.report()
    assert latency.first_send_at == pytest.approx(stamp)


def test_summary_needs_three_samples(clock: FakeClock) -> None:
    latency = TurnLatency(clock=clock)
    for _ in range(2):
        latency.begin()
        latency.report()
    assert latency.summary() is None

    latency.begin()
    latency.report()
    summary = latency.summary()
    assert summary is not None
    assert "近 3 轮中位数" in summary


def test_take_summary_fires_once_per_window(clock: FakeClock) -> None:
    latency = TurnLatency(clock=clock)
    fired = 0
    for _ in range(SUMMARY_TURNS * 2):
        latency.begin()
        latency.report()
        if latency.take_summary():
            fired += 1

    assert fired == 2


def test_window_is_bounded(clock: FakeClock) -> None:
    """只留最近一窗，早期的慢轮不该一直把中位数钉在高位。"""

    latency = TurnLatency(clock=clock)
    for _ in range(30):
        latency.begin()
        clock.advance(10.0)
        latency.report()
    for _ in range(30):
        latency.begin()
        clock.advance(1.0)
        latency.report()

    summary = latency.summary()
    assert summary is not None
    assert "近 10 轮中位数 1.00s" in summary
