"""直播一轮回复的延迟分解：弹幕入站 → 首段出站。

宿主链路是「入站消息 →（调度等待）→ Planner →（工具）→ Replyer → 断句发送」，
四段各管各的：

* **等待**：弹幕已经在聊天流里，但宿主还没起 Planner。宿主的防抖、以及
  ``no_action_backoff``（连续几次决定不说话就等 15 秒起）都在这一段。
* **Planner**：一次完整的 prefill + 工具调用决策。
* **Replyer**：再一次 prefill + 整段回复的 decode（宿主非流式，要等整段）。

混在一起只能得出「慢」，拆开才知道该改哪一段：等待段拼的是别把宿主逼进退避，
Planner 段拼输入长度与缓存命中，Replyer 段拼输出长度。

本模块只观测：不发送请求、不改写消息、不读网络。时钟默认取 ``time.monotonic``
（NTP 校时会让 wall clock 跳变，而这里量的是几百毫秒级的差值）。
"""

from __future__ import annotations

import statistics
import time
from typing import Callable, Dict, List, Optional

#: 段名 → 报告里的中文标签（顺序即时间顺序）
_SEGMENT_LABELS = {"wait": "等待", "planner": "Planner", "replyer": "Replyer"}

#: 中位数窗口：只留最近若干轮，早期的慢轮不该一直污染当前观感
_WINDOW = 10

#: 每多少轮打一次中位数汇总
SUMMARY_TURNS = 10


class TurnLatency:
    """把一轮回复拆成「等待 / Planner / Replyer」三段并输出一行可读报告。

    同一轮里重复 mark 只保留第一次：一轮可能因为工具循环跑多次 Planner、
    因为校验重试跑多次 Replyer，取首个里程碑才是观众感知到的那一段，
    后面的重试只会把统计越拉越长。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._marks: Dict[str, float] = {}
        self._reported = True
        self._origin = "danmaku"
        self._samples: List[float] = []
        self._since_summary = 0

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def begin(self, origin: str = "danmaku") -> bool:
        """一条入站消息到达，开一轮新的计时。

        已经有未结算的一轮时**不重置**：观众感知的是「我发的第一条弹幕
        等多久才听到回应」，而不是「最后一条弹幕等多久」。中途插进来的
        新消息只算加料，不能把起点往后挪。
        """

        if not self._reported:
            return False
        self._marks = {"receive": self._clock()}
        self._origin = origin
        self._reported = False
        return True

    def mark(self, stage: str) -> bool:
        """记录一个里程碑；返回是否真的写入了。"""

        if self._reported:
            # 上一轮已结算（或还没开始）：这条里程碑属于新的一轮。
            # 主动对话轮没有入站消息，等待段天然为 0。
            self._marks = {"receive": self._clock()}
            self._origin = "auto"
            self._reported = False
        if stage in self._marks:
            return False
        self._marks[stage] = self._clock()
        return True

    def report(self) -> Optional[str]:
        """结算本轮并返回一行分解；没有可用起点时返回 ``None``。

        以「首段出站」为终点：这是观众能看到/听到第一句的时刻。它之后
        还有 TTS 合成首包（几百毫秒），由播报侧单独打点。
        """

        if self._reported:
            return None
        self._marks.setdefault("first_send", self._clock())
        self._reported = True

        marks = self._marks
        start = marks.get("receive")
        if start is None:
            return None
        total = marks["first_send"] - start
        self._samples.append(total)
        if len(self._samples) > _WINDOW:
            del self._samples[0]

        # 里程碑不是每轮都齐：Planner 直接 wait 的轮次没有 Replyer，
        # 缺的阶段并进上一段，保证三段之和等于总时长（不会出现"丢失"的秒数）。
        planner_at = marks.get("planner_start")
        replyer_at = marks.get("replyer_start")
        wait = (planner_at - start) if planner_at is not None else 0.0
        planner = (
            (replyer_at - planner_at)
            if planner_at is not None and replyer_at is not None
            else (marks["first_send"] - planner_at)
            if planner_at is not None
            else 0.0
        )
        replyer = (marks["first_send"] - replyer_at) if replyer_at is not None else 0.0

        parts = "｜".join(
            f"{_SEGMENT_LABELS[name]} {value:.2f}s" for name, value in
            (("wait", wait), ("planner", planner), ("replyer", replyer))
        )
        label = "弹幕→首段" if self._origin == "danmaku" else "触发→首段"
        return f"{label} {total:.2f}s｜{parts}"

    def summary(self) -> Optional[str]:
        """最近若干轮的中位数；不足 3 轮不报（样本太少没有意义）。"""

        if len(self._samples) < 3:
            return None
        values = sorted(self._samples)
        median = statistics.median(values)
        return (
            f"近 {len(values)} 轮中位数 {median:.2f}s，最慢 {values[-1]:.2f}s，"
            f"最快 {values[0]:.2f}s"
        )

    def take_summary(self) -> Optional[str]:
        """每满 :data:`SUMMARY_TURNS` 轮返回一次中位数，其余时候返回 ``None``。"""

        self._since_summary += 1
        if self._since_summary < SUMMARY_TURNS:
            return None
        self._since_summary = 0
        return self.summary()

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    @property
    def first_send_at(self) -> Optional[float]:
        """本轮首段出站的单调时刻（给 TTS 首包算「出站→出声」用）。"""

        return self._marks.get("first_send")

    @property
    def active(self) -> bool:
        """是否有一轮正在计时。"""

        return not self._reported

    @property
    def count(self) -> int:
        """已结算的轮数（窗口内），用于决定多久打一次汇总。"""

        return len(self._samples)
