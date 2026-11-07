# -*- coding: utf-8 -*-
"""表情时序：时长由内容与语境决定，不是查表拿固定秒数。

三条必须成立的契约：
  1. 同一个情绪，语气重/句子长 -> 演得更久；语气轻/一句话 -> 更短；
  2. 事件型情绪（惊讶/疑惑）不会被长句拖成"发呆"，状态型才会撑到话说完；
  3. 所有时长都落在真人区间（0.5~4s），不会出现挂十几秒的雕像表情。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from vts.timing import (  # noqa: E402
    EVENT_APEX_CAP,
    MAX_TOTAL,
    MIN_TOTAL,
    ExpressionTiming,
    decay_weight,
    estimate_intensity,
    estimate_speech_seconds,
    plan_for_text,
    plan_timing,
)


def test_same_emotion_scales_with_intensity() -> None:
    """「呜……有点难过」和「呜呜呜呜你干嘛这样骂我！！」不能一样长。"""

    weak = plan_for_text("委屈", "呜……有点难过。", jitter=False)
    strong = plan_for_text("委屈", "呜呜呜呜你干嘛这样骂我！！我明明很努力了", jitter=False)
    assert strong.intensity > weak.intensity
    assert strong.total > weak.total, "语气更重却演得更短"
    # 强度高时上场更快（更冲动），这是方向性要求
    assert strong.onset < weak.onset


def test_event_emotions_not_stretched_by_long_sentence() -> None:
    """惊讶是瞬间反应，不该跟着长句挂两秒半。"""

    short = plan_for_text("惊讶", "啊？", jitter=False)
    long_one = plan_for_text("惊讶", "什么？！你说她昨天就退播了？！", jitter=False)
    assert long_one.apex <= EVENT_APEX_CAP + 1e-6, "惊讶的峰值被长句拖长了"
    assert short.total < 1.0, "一声「啊？」不该演一整秒"


def test_state_emotion_covers_the_sentence() -> None:
    """状态型情绪要撑到这句话说完，否则会「脸上变了一下就没了」。"""

    text = "……行吧，你开心就好，我懒得说了"
    timing = plan_for_text("无语", text, jitter=False)
    speech = estimate_speech_seconds(text)
    assert timing.apex >= min(speech - timing.onset, 2.5) - 1e-6


def test_total_always_within_human_range() -> None:
    """任何组合都必须落在 0.6~4.0 秒（Ekman: 真笑 0.5~4s，超过 5s 像摆拍）。"""

    rng = random.Random(7)
    texts = ["", "啊？", "嗯。", "呜呜呜", "哈哈哈哈这也太好笑了吧我真的受不了了！！",
             "你再说一遍试试，我今天就要跟你把这件事说清楚，你到底什么意思"]
    for emotion in ("委屈", "开心", "生气", "惊讶", "无语", "害羞", "得意", "紧张"):
        for text in texts:
            for repeat in (0, 1, 3):
                timing = plan_timing(
                    emotion, intensity=rng.uniform(0.15, 1.0),
                    speech_seconds=estimate_speech_seconds(text),
                    repeat=repeat, rng=rng,
                )
                assert MIN_TOTAL - 1e-6 <= timing.total <= MAX_TOTAL + 1e-6, (
                    f"{emotion}/「{text}」总长 {timing.total} 越界")


def test_flash_for_faint_surprise() -> None:
    """低强度的惊讶压成一次闪现，而不是摆一张脸。"""

    timing = plan_for_text("惊讶", "啊？", jitter=False)
    assert timing.flash is True
    assert timing.total <= 0.8
    # 强惊讶不闪，但也不拖长
    strong = plan_for_text("惊讶", "什么？！你说她昨天就退播了？！", jitter=False)
    assert strong.flash is False


def test_repeat_habituates() -> None:
    """同一个情绪连着演，一次比一次淡、一次比一次短。"""

    totals = [plan_for_text("无语", "行吧，你开心就好我懒得说了", repeat=i, jitter=False).total
              for i in range(4)]
    assert totals[0] > totals[1] > totals[2], f"习惯化没有生效：{totals}"
    assert totals[-1] < totals[0] * 0.85


def test_fade_matches_onset_and_offset() -> None:
    """淡入跟着 onset、淡出跟着 offset，不能所有表情共用一个 0.22。"""

    slow = plan_timing("委屈", intensity=1.0, jitter=False)
    fast = plan_timing("惊讶", intensity=0.9, jitter=False)
    assert slow.fade_in > fast.fade_in, "委屈应该比惊讶起得慢"
    assert slow.fade_out > fast.fade_out
    assert 0.08 <= fast.fade_in <= 0.45
    assert 0.10 <= fast.fade_out <= 0.75


def test_decay_weight_is_exponential() -> None:
    """回落用指数曲线：刚离开峰值掉得快，尾巴拖得长，两端必须精确为 1 和 0。"""

    assert abs(decay_weight(0.0) - 1.0) < 1e-9
    assert abs(decay_weight(1.0)) < 1e-9
    previous = 1.0
    for step in range(1, 11):
        value = decay_weight(step / 10.0)
        assert value < previous, "衰减曲线必须单调下降"
        previous = value
    # 指数衰减的特征：前 20% 掉得比后 20% 多
    early = 1.0 - decay_weight(0.2)
    late = decay_weight(0.8) - decay_weight(1.0)
    assert early > late, "不是指数衰减"


def test_estimate_intensity_directions() -> None:
    """强度估计的方向必须对：重语气更高，省略号更低。"""

    assert estimate_intensity("呜呜呜你干嘛这样骂我！！") > estimate_intensity("嗯，知道了。")
    assert estimate_intensity("啊？") < estimate_intensity("你说真的吗")
    assert 0.15 <= estimate_intensity("") <= 1.0
    assert estimate_intensity("非常非常非常难过！！！") <= 1.0


def test_timing_describe_is_readable() -> None:
    """日志里要能一眼看出这次为什么这么长。"""

    text = plan_timing("委屈", intensity=0.8, jitter=False).describe()
    assert "委屈" in text and "起" in text and "峰" in text


def main() -> int:
    test_same_emotion_scales_with_intensity()
    test_event_emotions_not_stretched_by_long_sentence()
    test_state_emotion_covers_the_sentence()
    test_total_always_within_human_range()
    test_flash_for_faint_surprise()
    test_repeat_habituates()
    test_fade_matches_onset_and_offset()
    test_decay_weight_is_exponential()
    test_estimate_intensity_directions()
    test_timing_describe_is_readable()
    print("表情时序计划通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
