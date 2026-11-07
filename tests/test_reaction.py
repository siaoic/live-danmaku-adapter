# -*- coding: utf-8 -*-
"""观众的话 -> 她的反应表情：必须是"人的反应"，不是复读观众情绪。"""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from vts.expression import detect_reaction  # noqa: E402


def test_third_person_complaint_is_awkward() -> None:
    """观众说「我讨厌她」——不是冲她来的，反应是讪笑吃瓜。"""

    emotion, reason = detect_reaction("我讨厌她")
    assert emotion == "尴尬", reason


def test_directed_complaint_hurts() -> None:
    """冲她来的负面 -> 委屈（受伤），而不是尴尬。"""

    assert detect_reaction("我讨厌你")[0] == "委屈"
    assert detect_reaction("主播好丑")[0] == "委屈"
    assert detect_reaction("闭嘴吧你")[0] == "委屈"


def test_viewer_sadness_gets_empathy() -> None:
    """观众在讲自己的情绪 -> 共情（委屈）。"""

    assert detect_reaction("我今天好难过")[0] == "委屈"
    assert detect_reaction("心累了家人们")[0] == "委屈"
    assert detect_reaction("我要哭了")[0] == "委屈"


def test_praise_is_shy() -> None:
    """被夸/被表白 -> 害羞（优先级高于负面词）。"""

    assert detect_reaction("你好可爱")[0] == "害羞"
    assert detect_reaction("我喜欢你")[0] == "害羞"
    assert detect_reaction("主播好漂亮")[0] == "害羞"


def test_viewer_anger_makes_her_nervous() -> None:
    """观众在发火 -> 她紧张（怕被波及）。"""

    assert detect_reaction("气死我了")[0] == "紧张"
    assert detect_reaction("烦死了")[0] == "紧张"


def test_viewer_speechless_is_speechless() -> None:
    """观众先无语了 -> 跟着无语。"""

    assert detect_reaction("太离谱了")[0] == "无语"
    assert detect_reaction("服了")[0] == "无语"


def test_question_is_confused() -> None:
    """观众在发问 -> 疑惑。"""

    assert detect_reaction("为什么啊？")[0] == "疑惑"
    assert detect_reaction("真的假的")[0] == "疑惑"


def test_plain_chat_no_reaction() -> None:
    """普通闲聊不硬加戏。"""

    assert detect_reaction("晚上好")[0] == ""
    assert detect_reaction("")[0] == ""
    assert detect_reaction("   ")[0] == ""


def main() -> int:
    test_third_person_complaint_is_awkward()
    test_directed_complaint_hurts()
    test_viewer_sadness_gets_empathy()
    test_praise_is_shy()
    test_viewer_anger_makes_her_nervous()
    test_viewer_speechless_is_speechless()
    test_question_is_confused()
    test_plain_chat_no_reaction()
    print("观众反应表情映射通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
