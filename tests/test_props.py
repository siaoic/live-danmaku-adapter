"""自动道具调度：候选筛选、开合节奏、情绪门控（不连 VTS）。"""

from __future__ import annotations

import random

from vts.props import (
    PropCandidate,
    PropScheduler,
    hotkey_candidates,
    item_candidates,
    oneshot_candidates,
    prop_candidates,
)


def test_item_candidates_filter() -> None:
    """只保留「能加载、没挂上、不是背景特效」的物品。"""

    items = [
        {"name": "狗尾巴草", "instanceID": "i1", "type": "Live2D", "canLoad": True, "loaded": False},
        {"name": "已经挂着的", "instanceID": "i2", "type": "Live2D", "canLoad": True, "loaded": True},
        {"name": "不允许加载", "instanceID": "i3", "type": "Live2D", "canLoad": False},
        {"name": "背景特效", "instanceID": "i4", "type": "Effect", "canLoad": True},
        {"name": "没有实例ID", "type": "Live2D", "canLoad": True},
        "坏数据",
    ]
    got = item_candidates(items)
    assert [c.name for c in got] == ["狗尾巴草"]
    assert got[0].kind == "item" and got[0].open_ref == "i1"


def test_hotkey_candidates_and_union() -> None:
    """配对热键也能当道具；同名时物品优先。"""

    pairs = {"狗尾巴草": ("on-dog", "off-dog"), "竖手指": ("on-finger", "off-finger")}
    hotkeys = hotkey_candidates(pairs)
    assert {c.name for c in hotkeys} == {"狗尾巴草", "竖手指"}
    assert all(c.kind == "hotkey" for c in hotkeys)

    items = [{"name": "狗尾巴草", "instanceID": "i1", "type": "Live2D", "canLoad": True}]
    merged = prop_candidates(items, pairs)
    assert merged[0].name == "狗尾巴草" and merged[0].kind == "item"
    assert len(merged) == 2


def test_oneshot_hotkeys_as_gestures() -> None:
    """只有单边 TriggerAnimation 热键的模型也能当小动作；展示型/非动画热键跳过。"""

    hotkeys = [
        {"name": "Anim Shake", "hotkeyID": "hk-shake", "type": "TriggerAnimation"},
        {"name": "兔牙", "hotkeyID": "hk-tooth", "type": "TriggerAnimation"},       # 展示型
        {"name": "Heart Eyes", "hotkeyID": "hk-eye", "type": "ToggleExpression"},  # 非动画
        {"name": "", "hotkeyID": "hk-empty", "type": "TriggerAnimation"},          # 无名
    ]
    got = oneshot_candidates(hotkeys, {"狗尾巴草": ("on", "off")})
    assert [c.name for c in got] == ["Anim Shake"]
    assert got[0].kind == "oneshot" and got[0].close_ref == ""


def test_paired_halves_are_not_oneshot() -> None:
    """「xx打开 / xx关闭」不能因为名字带后缀就被当成两条新的一次性动作。

    真机踩过：示例模型的三对道具各多出一条「打开」「关闭」单发候选，
    自动道具会拿它们当互不相干的小动作乱触发，道具就会挂住不收回。
    """

    pairs = {"狗尾巴草": ("on-dog", "off-dog"), "竖手指": ("on-f", "off-f")}
    hotkeys = [
        {"name": "狗尾巴草打开", "hotkeyID": "on-dog", "type": "TriggerAnimation"},
        {"name": "狗尾巴草关闭", "hotkeyID": "off-dog", "type": "TriggerAnimation"},
        {"name": "竖手指打开", "hotkeyID": "on-f", "type": "TriggerAnimation"},
        {"name": "竖手指关闭", "hotkeyID": "off-f", "type": "TriggerAnimation"},
        {"name": "Anim Shake", "hotkeyID": "hk-shake", "type": "TriggerAnimation"},
    ]
    got = oneshot_candidates(hotkeys, pairs)
    assert [c.name for c in got] == ["Anim Shake"]


def test_prop_union_is_item_then_pair_then_oneshot() -> None:
    """三类来源合并：物品 > 配对热键 > 单发动；同名去重。"""

    items = [{"name": "Halo", "instanceID": "Halo.png", "type": "PNGTuber", "canLoad": True}]
    pairs = {"竖手指": ("a", "b")}
    hotkeys = [
        {"name": "竖手指打开", "hotkeyID": "a", "type": "TriggerAnimation"},
        {"name": "竖手指关闭", "hotkeyID": "b", "type": "TriggerAnimation"},
        {"name": "Anim Shake", "hotkeyID": "c", "type": "TriggerAnimation"},
    ]
    merged = prop_candidates(items, pairs, hotkeys)
    assert [c.name for c in merged] == ["Halo", "竖手指", "Anim Shake"]


def test_scheduler_opens_holds_and_closes() -> None:
    """到点开道具，到点自己收回，然后再重新排队。"""

    scheduler = PropScheduler(
        enabled=True, first_delay=(10.0, 10.0), interval=(100.0, 100.0), hold=(5.0, 5.0),
        rng=random.Random(1),
    )
    scheduler.set_candidates([PropCandidate("狗尾巴草", "hotkey", "on", "off")])

    assert scheduler.tick(0.0) == []
    assert scheduler.tick(5.0) == []
    opened = scheduler.tick(10.0)
    assert [a.kind for a in opened] == ["open"]
    assert scheduler.active_name == "狗尾巴草"

    assert scheduler.tick(12.0) == []
    closed = scheduler.tick(15.0)
    assert [a.kind for a in closed] == ["close"]
    assert scheduler.active_name == ""
    assert scheduler.tick(16.0) == []


def test_manual_prop_holds_off_auto_schedule() -> None:
    """AI 自己点了道具后，自动调度要让路，不能再顺手掏一个。

    否则就是两只手抢同一个画面：她刚收起狗尾巴草，自动调度又掏出竖手指。
    """

    scheduler = PropScheduler(
        enabled=True, first_delay=(0.0, 0.0), interval=(10.0, 10.0), hold=(5.0, 5.0),
        rng=random.Random(3),
    )
    scheduler.set_candidates([PropCandidate("狗尾巴草", "hotkey", "on", "off")])
    scheduler.notify_manual(0.0, cooldown=60.0)
    # 让路期间不掏
    assert scheduler.tick(0.0) == []
    assert scheduler.tick(30.0) == []
    # 让路结束后照常
    opened = scheduler.tick(61.0)
    assert [a.kind for a in opened] == ["open"]


def test_scheduler_skips_negative_emotion() -> None:
    """生气/害怕/委屈/紧张时不掏道具。"""

    scheduler = PropScheduler(
        enabled=True, first_delay=(0.0, 0.0), interval=(1.0, 1.0), hold=(2.0, 2.0),
        rng=random.Random(2),
    )
    scheduler.set_candidates([PropCandidate("草", "item", "i1")])

    assert scheduler.tick(0.0, "生气") == []
    assert scheduler.tick(1.0, "") == []          # 第一次平静的 tick 才开始计时
    assert [a.kind for a in scheduler.tick(2.0, "")] == ["open"]


def test_cute_emotion_shortens_next_prop() -> None:
    """开心/得意/害羞/疑惑时，下一次道具提前到 15~45 秒。"""

    scheduler = PropScheduler(
        enabled=True, first_delay=(0.0, 0.0), interval=(999.0, 999.0), hold=(1.0, 1.0),
        rng=random.Random(6),
    )
    scheduler.set_candidates([PropCandidate("草", "item", "i1")])

    scheduler.tick(0.0)
    assert [a.kind for a in scheduler.tick(1.0, "开心")] == ["open"]
    assert [a.kind for a in scheduler.tick(2.0, "开心")] == ["close"]
    # 常规间隔是 999 秒；可爱情绪下 45 秒内必然再掏一个
    assert [a.kind for a in scheduler.tick(50.0, "开心")] == ["open"]


def test_neutral_emotion_keeps_normal_interval() -> None:
    """没有可爱情绪时，回到常规的长间隔。"""

    scheduler = PropScheduler(
        enabled=True, first_delay=(0.0, 0.0), interval=(999.0, 999.0), hold=(1.0, 1.0),
        rng=random.Random(7),
    )
    scheduler.set_candidates([PropCandidate("草", "item", "i1")])

    scheduler.tick(0.0)
    assert [a.kind for a in scheduler.tick(1.0, "")] == ["open"]
    assert [a.kind for a in scheduler.tick(2.0, "")] == ["close"]
    assert scheduler.tick(500.0, "") == []


def test_disabled_never_opens() -> None:
    """总开关关掉就永远不动道具。"""

    scheduler = PropScheduler(enabled=False, first_delay=(0.0, 0.0), rng=random.Random(3))
    scheduler.set_candidates([PropCandidate("草", "item", "i1")])
    assert scheduler.tick(0.0) == []
    assert scheduler.tick(1000.0) == []


def test_drop_removes_bad_candidate() -> None:
    """装不上的物品从清单里剔除，不再反复试。"""

    scheduler = PropScheduler(
        enabled=True, first_delay=(0.0, 0.0), interval=(1.0, 1.0), hold=(100.0, 100.0),
        rng=random.Random(4),
    )
    candidate = PropCandidate("草", "item", "i1")
    scheduler.set_candidates([candidate])
    scheduler.tick(0.0)
    assert [a.kind for a in scheduler.tick(1.0)] == ["open"]

    scheduler.drop(candidate)
    assert scheduler.candidates() == []
    assert scheduler.active_name == ""


def test_no_immediate_repeat() -> None:
    """还有别的选择时，不连着掏同一个道具。"""

    scheduler = PropScheduler(
        enabled=True, first_delay=(0.0, 0.0), interval=(0.0, 0.0), hold=(1.0, 1.0),
        rng=random.Random(5),
    )
    scheduler.set_candidates([
        PropCandidate("A", "item", "ia"),
        PropCandidate("B", "item", "ib"),
    ])

    scheduler.tick(0.0)
    first = scheduler.tick(1.0)[0].candidate
    scheduler.tick(2.0)                            # 收回
    second = scheduler.tick(3.0)[0].candidate
    assert first.name != second.name
