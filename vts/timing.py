# -*- coding: utf-8 -*-
"""表情/动作的时序计划：时长按内容与语境算，不查固定秒数表。

为什么要重做
------------
原来每个情绪挂一个写死的 ``hold``（委屈 3.5s、惊讶 1.2s），不管她说的是
「呜……有点难过」还是「呜呜呜呜呜你干嘛这样骂我」。这违反两条可验证的规律：

1. **真人表情是 onset–apex–offset 三段，不是一个"亮 N 秒"**。
   FACS 的 Investigator's Guide 把表情切成 onset（到峰值）、apex（峰值保持）、
   offset（回落）三段（Ekman, Friesen & Hager, 2002）。三段的相对长短决定
   观感是否自然，总时长只是结果。
2. **段的长度随情绪强度、情绪类别和是否在说话而变**。
   情绪体验越强，apex 越久（Ekman et al., 2002）；真笑的 onset 是 0.5~0.75s，
   悲伤最慢、惊讶/恐惧最快；单个自发笑容总长 0.5~4s（Ekman & Friesen, 1982），
   超过 5 秒基本是摆拍（Ekman, 2009）。

所以这里不再"给一个数"，而是**计划三段**：先用文献基线，再按强度调制，
再用剩余语速把 apex 撑到话说完，最后加一点抖动（同一个情绪不会每次都演得一样）。

文献与取值的对应
----------------
* onset 基线：Hoffmann, Traue, Bachmayr & Kessler (2010) 的感知真实感实验——
  悲伤 860~1120ms（最慢）、惊讶 500~530ms 与恐惧 480~650ms（最快）、
  愤怒/厌恶/快乐 670~890ms。这里取区间中值。
* 总时长上限：Ekman & Friesen (1982) 真笑 500~4000ms；Ekman (2009) 超过 5s
  更像刻意表情。故把 total 裁到 0.6~4.0s。
* apex 下限/上限：Schmidt, Cohn & Tian (2003) 自发微笑 onset≈0.5s、
  apex≈0.2s、offset≈0.3s；Broekens, Qu & Brinkman (2012) 的计算模型建议
  表情总时长 1~2s。这里让 apex 随强度在 0.3~2.5s 之间移动。
* 微表情：40~200ms（远快于普通表情，见微表情文献综述）。低于阈值强度时
  不给"摆着的脸"，而是压成一次短促闪现。

这一层是纯计算，不碰 VTS，方便单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import math
import random
import re

#: 每个情绪的三段基线 (onset, apex, offset)，单位秒。
#: onset 取 Hoffmann et al. (2010) 的感知真实感区间中值；apex/offset 按
#: Ekman & Friesen (1982) 的总时长区间配比——悲伤/委屈最长，惊讶最短。
BASE_TIMING: Dict[str, Tuple[float, float, float]] = {
    "惊讶": (0.50, 0.30, 0.28),   # 最快也最短：惊讶是一种"泄漏"，不是状态
    "害怕": (0.55, 0.40, 0.45),
    "生气": (0.70, 0.95, 0.55),
    "开心": (0.55, 1.00, 0.60),   # 真笑 onset 0.5~0.75，取中偏快
    "委屈": (0.85, 1.40, 0.90),   # 悲伤是三类里最慢的
    "无语": (0.75, 0.90, 0.70),
    "轻蔑": (0.80, 0.75, 0.60),   # 轻蔑常见单侧、来得慢收得快
    "尴尬": (0.70, 0.85, 0.65),
    "害羞": (0.80, 0.95, 0.70),
    "疑惑": (0.65, 0.75, 0.55),
    "得意": (0.70, 0.95, 0.60),
    "紧张": (0.60, 0.70, 0.60),
}

#: 总时长硬边界（秒）。上限依据 Ekman (2009)：挂着超过 5 秒更像摆拍。
MIN_TOTAL = 0.60
MAX_TOTAL = 4.00

#: 强到弱的分界：低于这个强度，短暂的惊讶/疑惑/轻蔑改成"闪一下"。
FLASH_THRESHOLD = 0.50
#: 适合闪一下的情绪：本身就是瞬时反应，不是持续状态。
FLASH_EMOTIONS = frozenset({"惊讶", "疑惑", "轻蔑"})

#: 能"挂着"的状态型情绪：只有它们会为了让话说完而延长 apex。
#: 惊讶/害怕/疑惑是事件型反应——事情过去了表情就该收，跟着长句一起拖会变成
#: "瞪着你看"，反而假。
SUSTAINABLE_EMOTIONS = frozenset({
    "委屈", "生气", "开心", "害羞", "无语", "轻蔑", "尴尬", "得意", "紧张",
})
#: 事件型情绪的 apex 上限（秒）：再长就不是惊讶，是发呆了。
EVENT_APEX_CAP = 0.60

#: 中文语速（字/秒）：用来估算这句话还要说多久，好让表情撑到话说讲完。
#: gsv.cpp 这类 TTS 在中文直播里大约 4.5~5.5 字/秒，取中值。
CHARS_PER_SECOND = 5.0

#: 程度副词与会增强情绪的词：它们出现时语气更重，apex 该更长。
_INTENSIFIERS = (
    "非常", "特别", "超级", "简直", "真的", "太", "好", "超", "巨", "极",
    "死了", "爆", "疯", "完蛋", "受不了",
)
#: 情绪词本身：直接说"难过/委屈/哭"，强度不该和随口一句一样。
_EMOTION_WORDS = ("难过", "委屈", "想哭", "眼泪", "心疼", "难受", "害怕", "气死")
#: 削弱语气的标记：省略号常表示无力/迟疑，反而该短一点、慢一点。
_SOFTENERS = ("……", "...", "，", "算了", "也许", "大概", "可能")


@dataclass(frozen=True)
class ExpressionTiming:
    """一次表情的三段时序（秒）。"""

    emotion: str = ""
    onset: float = 0.60
    apex: float = 0.90
    offset: float = 0.55
    intensity: float = 0.5
    flash: bool = False

    @property
    def total(self) -> float:
        """从开始动到完全收回的总时长。"""

        return self.onset + self.apex + self.offset

    @property
    def fade_in(self) -> float:
        """给 VTS ``ExpressionActivationRequest.fadeTime`` 的淡入时长。

        取 onset 的 60%：VTS 的淡入是线性插值，如果直接等于 onset，前段
        会显得"抢拍"；留一点余量让参数层接管最开始的加速。
        """

        return round(max(0.08, min(self.onset * 0.6, 0.45)), 3)

    @property
    def fade_out(self) -> float:
        """收回时的淡出时长（对应 offset）。"""

        return round(max(0.10, min(self.offset, 0.75)), 3)

    def describe(self) -> str:
        tag = "闪" if self.flash else "演"
        return (
            f"{self.emotion or '未命名'} {tag} {self.total:.2f}s"
            f"（起{self.onset:.2f}/峰{self.apex:.2f}/落{self.offset:.2f}，强度{self.intensity:.2f}）"
        )


def estimate_intensity(text: str, *, base: float = 0.5) -> float:
    """从一句台词估算情绪强度（0.15~1.0）。

    这不是"理解语义"，只是把明显的语气信号折算成系数：感叹号、叠字、
    程度副词都会让表情更足；省略号和缓和词会让它更淡。

    Args:
        text: 要朗读的台词。
        base: 起评分（默认 0.5，中性偏弱）。

    Returns:
        0.15~1.0 之间的强度。
    """

    body = str(text or "")
    if not body.strip():
        return 0.15
    score = float(base)

    # 感叹号：情绪的显式标记，但连用四个不会比两个更激动，封顶 +0.3
    score += min(body.count("！") + body.count("!"), 3) * 0.10
    # 叠字（呜呜呜/哈哈哈/啊啊啊）：自发情绪的典型形态。
    # 含两字的「哈哈」「呜呜」也算——中文里它们本身就是笑/哭的拟声。
    if re.search(r"(.)\1{2,}", body) or re.search(r"(哈|呜|啊|嘿|嘻|嘤|嗷|哇){2,}", body):
        score += 0.12
    # 程度副词
    hits = sum(1 for word in _INTENSIFIERS if word in body)
    score += min(hits, 2) * 0.10
    # 直接说出情绪词：强度不该和随口一句相同
    if any(word in body for word in _EMOTION_WORDS):
        score += 0.10
    # 省略号/缓和词：语气泄了，别演太满
    if any(word in body for word in _SOFTENERS):
        score -= 0.08
    # 长句通常情绪更铺得开；短促的一句（"啊？"）则更像瞬间反应
    if len(body) >= 18:
        score += 0.05
    elif len(body) <= 6:
        score -= 0.12

    return round(max(0.15, min(score, 1.0)), 3)


def estimate_speech_seconds(text: str) -> float:
    """估算这句话念出来要多久（秒），用于把 apex 撑到话说完。"""

    body = str(text or "")
    if not body.strip():
        return 0.0
    # 标点也会有停顿：每个句读约 0.18 秒
    pauses = sum(body.count(mark) for mark in "。！？，；：")
    seconds = len(body) / CHARS_PER_SECOND + pauses * 0.18
    return round(max(0.4, min(seconds, 15.0)), 2)


def plan_timing(
    emotion: str,
    *,
    intensity: float = 0.5,
    speech_seconds: float = 0.0,
    repeat: int = 0,
    rng: Optional[random.Random] = None,
    jitter: bool = True,
) -> ExpressionTiming:
    """给一次表情算三段时序。

    调制顺序刻意如此：先基线 → 再按强度缩放 → 再撑到话说完 → 最后裁边界。
    顺序反过来会出现"撑完又超上限被硬裁"，把强度的影响裁没了。

    Args:
        emotion: 情绪词（中文标签）。
        intensity: 0.15~1.0 的情绪强度（见 :func:`estimate_intensity`）。
        speech_seconds: 这句话还要念多久；>0 表示她正在说话。
        repeat: 这个情绪在短时间内已经演过几次（习惯化，越演越淡）。
        rng: 随机源（单测注入固定种子）。
        jitter: 是否加抖动；关掉后同一输入必得同一结果，便于测试。

    Returns:
        :class:`ExpressionTiming`。
    """

    key = str(emotion or "").strip()
    onset, apex, offset = BASE_TIMING.get(key, (0.65, 0.95, 0.60))
    level = max(0.15, min(float(intensity), 1.0))

    # 习惯化：同一个情绪连着演，人会一次比一次淡（强度先衰减）
    if repeat > 0:
        level = max(0.15, level * (0.85 ** min(repeat, 3)))

    # 强度高 -> 起得快、峰更久、落得慢。这三条方向一致，才不会出现
    # "很激动但一秒就收"这种矛盾。
    onset *= 1.15 - 0.35 * level       # 0.80x ~ 1.15x
    apex *= 0.55 + 0.85 * level        # 0.64x ~ 1.40x
    offset *= 0.85 + 0.30 * level      # 0.90x ~ 1.15x

    # 正在说话：状态型情绪要撑到这句话讲完，否则会"脸上变了一下就没了"。
    # 事件型（惊讶/疑惑/害怕）不撑——它们是瞬间反应，跟着长句拖就成了发呆。
    if speech_seconds > 0 and key in SUSTAINABLE_EMOTIONS:
        need = max(0.0, float(speech_seconds) - onset)
        # 习惯化后不再硬撑整句：连着演第三次还撑满，观众看得出来是在摆姿势
        if repeat > 0:
            need *= max(0.35, 0.70 ** min(repeat, 3))
        apex = max(apex, min(need, 2.5))
    elif key in FLASH_EMOTIONS:
        apex = min(apex, EVENT_APEX_CAP)

    # 瞬时情绪 + 低强度：不给"摆着的脸"，改成一次短暂闪现
    flash = level < FLASH_THRESHOLD and key in FLASH_EMOTIONS
    if flash:
        onset = max(0.12, min(onset, 0.20))
        apex = max(0.10, min(apex, 0.16))
        offset = max(0.14, min(offset, 0.22))

    if jitter:
        source = rng or random
        onset *= source.uniform(0.90, 1.10)
        apex *= source.uniform(0.88, 1.12)
        offset *= source.uniform(0.90, 1.12)

    # 裁边界：先保证总长在区间内，再保证每段不为 0
    total = onset + apex + offset
    if total < MIN_TOTAL:
        scale = MIN_TOTAL / max(total, 1e-6)
        onset, apex, offset = onset * scale, apex * scale, offset * scale
    elif total > MAX_TOTAL:
        scale = MAX_TOTAL / total
        onset, apex, offset = onset * scale, apex * scale, offset * scale

    onset_r = round(max(0.08, onset), 3)
    apex_r = round(max(0.10, apex), 3)
    offset_r = round(max(0.12, offset), 3)
    # round 会把三段各自截掉或补上一点，总长可能正好越过两端边界。
    # 先补下限再压上限，两步都做完才返回，保证对外承诺的是闭区间。
    shortfall = MIN_TOTAL - (onset_r + apex_r + offset_r)
    if shortfall > 0:
        apex_r = round(apex_r + shortfall + 0.001, 3)
    excess = (onset_r + apex_r + offset_r) - MAX_TOTAL
    if excess > 0:
        apex_r = round(max(0.10, apex_r - excess - 0.001), 3)

    return ExpressionTiming(
        emotion=key,
        onset=onset_r,
        apex=apex_r,
        offset=offset_r,
        intensity=round(level, 3),
        flash=flash,
    )


def plan_for_text(
    emotion: str,
    text: str,
    *,
    repeat: int = 0,
    rng: Optional[random.Random] = None,
    jitter: bool = True,
) -> ExpressionTiming:
    """按台词同时估强度与语速，再计划时序（引擎里的常用入口）。"""

    return plan_timing(
        emotion,
        intensity=estimate_intensity(text),
        speech_seconds=estimate_speech_seconds(text),
        repeat=repeat,
        rng=rng,
        jitter=jitter,
    )


def decay_weight(q: float, sharpness: float = 3.0) -> float:
    """回落段的衰减曲线：指数而不是直线。

    依据 Kuppens, Oravecz & Tuerlinckx (2010) 的 DynAffect 模型——情绪被建模成
    Ornstein-Uhlenbeck 过程，偏离基线的量按 ``exp(-γΔt)`` 衰减，也就是刚离开
    峰值时掉得快、尾巴拖得长。换算成 0~1 的归一化曲线：

        w(q) = (exp(-k·q) - exp(-k)) / (1 - exp(-k))

    这样 q=0 时为 1、q=1 时正好为 0，中间是"先快后慢"的真实衰退，
    不会像线性那样在最后一段还硬撑着。

    Args:
        q: 回落进度 0~1。
        sharpness: 衰减陡度 k；越大前段掉得越快。

    Returns:
        0~1 的权重。
    """

    q = max(0.0, min(float(q), 1.0))
    k = max(0.5, float(sharpness))
    tail = math.exp(-k)
    return (math.exp(-k * q) - tail) / max(1e-9, 1.0 - tail)
