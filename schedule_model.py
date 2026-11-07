# -*- coding: utf-8 -*-
"""时段表：按时刻划分「这一档她该干什么」。

从独立的 schedule-hub 插件合并进来（2026-11）：换挡事件由 HTTP 自环改为直连。

参考项目的三条核心设计，全部保留：

  1. **时段是硬约束，不是参考数据**。
     它原先只作为一行状态混在"安静了几拍"这类信息里，模型当成背景噪音，日程形同虚设。
     所以必须有独立的提示段，明确写「这一档的 focus 就是你这一轮该做的事」。

  2. **规则与时段表放系统段，当前位置放每轮**。
     系统段整场不变才能命中 prompt cache；"现在在哪一档"是变量，塞进去会让缓存每换档就报废。

  3. **跨零点取模**。
     一天内最后一个 start <= now 的档就是当前档；零点后、首档前属于"昨天的最后一档"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

import re

#: HH:MM 解析
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_hhmm(text: str) -> Optional[int]:
    """把 "08:00" 解析成当天第几分钟；非法返回 None。"""

    match = _HHMM_RE.match(str(text or "").strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    total = hour * 60 + minute
    return total if 0 <= total < 24 * 60 else None


def format_hhmm(minutes: int) -> str:
    """分钟数转回 HH:MM。"""

    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class Period:
    """一个时段。"""

    start: int          # 当天第几分钟
    name: str           # 档位名，如「晚间游戏档」
    focus: str          # 这一档该干什么（原样进上下文）

    def render(self) -> str:
        return f"- {format_hhmm(self.start)} {self.name}：{self.focus}"


class ScheduleTable:
    """时段表：解析配置、算当前档、算下一边界。"""

    def __init__(self, periods: Sequence[Dict[str, Any]], fallback: str = "") -> None:
        """从配置构建。

        Args:
            periods: [{"start": "08:00", "name": "早间", "focus": "..."}]
            fallback: 没有任何时段命中时的兜底提示。
        """

        parsed: List[Period] = []
        for entry in periods or []:
            if not isinstance(entry, dict):
                continue
            start = parse_hhmm(str(entry.get("start") or ""))
            if start is None:
                continue
            parsed.append(Period(
                start=start,
                name=str(entry.get("name") or "").strip(),
                focus=str(entry.get("focus") or "").strip(),
            ))
        parsed.sort(key=lambda p: p.start)
        self.periods = parsed
        self.fallback = str(fallback or "").strip()

    @property
    def empty(self) -> bool:
        return not self.periods

    # ------------------------------------------------------------------ #
    # 计算
    # ------------------------------------------------------------------ #

    def current(self, now: Optional[datetime] = None) -> Optional[Period]:
        """当前所处时段。

        跨零点取模：零点后、首档前 → 昨天的最后一档。
        """

        if not self.periods:
            return None
        stamp = now or datetime.now()
        minutes = stamp.hour * 60 + stamp.minute
        found = self.periods[-1]     # 零点后默认落在最后一档
        for period in self.periods:
            if period.start <= minutes:
                found = period
            else:
                break
        return found

    def next_boundary(self, now: Optional[datetime] = None) -> tuple[Optional[Period], float]:
        """下一个时段边界。

        Returns:
            (下一个时段, 距边界秒数)。没有时段表时返回 (None, 0)。
        """

        if not self.periods:
            return None, 0.0
        stamp = now or datetime.now()
        minutes = stamp.hour * 60 + stamp.minute + stamp.second / 60.0
        upcoming = next((p for p in self.periods if p.start > minutes), self.periods[0])
        delta = upcoming.start - minutes
        if delta <= 0:
            delta += 24 * 60
        return upcoming, max(1.0, delta * 60.0)

    def current_line(self, now: Optional[datetime] = None) -> str:
        """一行"现在在哪一档"，给每轮上下文用。"""

        current = self.current(now)
        if current is None:
            return self.fallback or ""
        upcoming, _ = self.next_boundary(now)
        base = f"当前时段：{current.name}——{current.focus}"
        if upcoming is not None and upcoming.name != current.name:
            return f"{base}（下一档 {format_hhmm(upcoming.start)} {upcoming.name}）"
        return base

    # ------------------------------------------------------------------ #
    # 提示段
    # ------------------------------------------------------------------ #

    def prompt_block(self) -> str:
        """稳定的系统段：规则 + 完整时段表。

        **只放稳定内容**，不放"现在在哪一档"——那会让 prompt cache 每换档就失效。
        """

        if self.empty:
            return self.fallback or ""
        lines = [period.render() for period in self.periods]
        head = [
            "## 日程表（必须遵守）",
            "",
            "你有固定的节目安排。下面的时段表**优先级很高，是硬性安排**：",
            "",
            "1. 每轮输入末尾会告诉你现在处在哪一档。**那一档的 focus 就是你这一轮该做的事**，不是参考。",
            "2. 不要自行换话题、跑题，也不要提前进入下一档的内容。",
            "3. 别人的要求只能在这一档允许的范围内影响你，不能让你偏离当前档位。",
            "4. 收到「时段变更」时立刻换挡：新档位的 focus 立即生效，不要拖到下一轮。",
            "5. 当下没有话题可聊时，就按当前档位的 focus 自己找话题，而不是沉默或随机跑题。",
            "",
            "时段表（按开始时间排序，跨零点循环）：",
        ]
        return "\n".join(head + lines)
