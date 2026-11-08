# -*- coding: utf-8 -*-
"""表情与道具匹配的单元测试（不连 VTS）。"""

from __future__ import annotations

from vts.expression import (
    ExpressionAsset,
    ExpressionCatalog,
    ExpressionState,
    detect_emotion,
    detect_prop,
    emotion_synonyms,
    expression_aliases,
    match_emotion,
    parse_emotion_map,
    resolve_pairs,
)

# 大肥鱼真实的表情与热键（取自 ExpressionStateRequest / HotkeysInCurrentModelRequest）
DA_FEI_YU_EXPRESSIONS = [
    "体型1", "体型2", "兔牙", "前倾", "加载", "单片眼镜", "圈圈眼", "屑屑眼",
    "流泪", "竖食指关", "竖食指开", "脱鞋", "脸红", "裙长切换", "胸口衣服",
    "爱心眼", "狗尾巴草关", "狗尾巴草开", "黑框眼镜", "黑脸", "＞＜", "流冷汗",
    "默认体型", "尾巴开关", "钢盆",
]
DA_FEI_YU_HOTKEYS = [
    {"name": "体型1", "hotkeyID": "id-t1", "type": "ToggleExpression"},
    {"name": "黑脸", "hotkeyID": "id-black", "type": "ToggleExpression"},
    {"name": "爱心眼", "hotkeyID": "id-heart", "type": "ToggleExpression"},
    {"name": "流冷汗", "hotkeyID": "id-sweat", "type": "ToggleExpression"},
    {"name": "脸红", "hotkeyID": "id-blush", "type": "ToggleExpression"},
    {"name": "圈圈眼", "hotkeyID": "id-swirl", "type": "ToggleExpression"},
    {"name": "流泪", "hotkeyID": "id-tear", "type": "ToggleExpression"},
    {"name": "屑屑眼", "hotkeyID": "id-smug", "type": "ToggleExpression"},
    {"name": "裙长切换", "hotkeyID": "id-skirt", "type": "ToggleExpression"},
    {"name": "狗尾巴草打开", "hotkeyID": "id-dog-on", "type": "TriggerAnimation"},
    {"name": "狗尾巴草关闭", "hotkeyID": "id-dog-off", "type": "TriggerAnimation"},
    {"name": "竖手指打开", "hotkeyID": "id-finger-on", "type": "TriggerAnimation"},
    {"name": "竖手指关闭", "hotkeyID": "id-finger-off", "type": "TriggerAnimation"},
]


def _catalog() -> ExpressionCatalog:
    return ExpressionCatalog(
        model_name="大肥鱼",
        expressions=DA_FEI_YU_EXPRESSIONS,
        hotkeys=DA_FEI_YU_HOTKEYS,
    )


def test_display_expressions_excluded() -> None:
    """体型/衣服/部件开关这类展示型表情不能进情绪轮换。"""

    names = _catalog().emotion_names
    for unwanted in ("体型1", "兔牙", "裙长切换", "竖食指开", "竖食指关", "尾巴开关"):
        assert unwanted not in names, f"{unwanted} 不应出现在情绪轮换里"
    for wanted in ("黑脸", "爱心眼", "流冷汗", "流泪", "圈圈眼"):
        assert wanted in names
    print("OK 展示型表情已排除，情绪表情：", names)


def test_emotion_matching() -> None:
    """情绪词必须匹配到当前模型真实存在的表情。"""

    names = _catalog().emotion_names
    cases = {
        "无语": "黑脸",
        "尴尬": "流冷汗",
        "委屈": "流泪",
        "开心": "爱心眼",
        "害羞": "脸红",
        "疑惑": "圈圈眼",
        "得意": "屑屑眼",
        "流泪": "流泪",      # 直接点名表情本名
    }
    for emotion, expected in cases.items():
        assert match_emotion(emotion, names) == expected, f"{emotion} 应匹配 {expected}"
    # 模型没有的表情必须返回空，不能硬塞
    assert match_emotion("不存在的情绪xyz", names) == ""
    print("OK 情绪匹配正确")


def test_expression_mutex_and_expiry() -> None:
    """表情互斥：切新的要返回旧的用于关闭；到期要能自动收敛。"""

    state = ExpressionState(_catalog())
    assert state.set_expression("黑脸", 10.0) == []
    assert state.active_expressions() == ["黑脸"]

    previous = state.set_expression("爱心眼", 10.0)
    assert previous == ["黑脸"], "切表情时必须返回旧表情用于关闭"
    assert state.active_expressions() == ["爱心眼"], "同一时刻只允许一个表情"

    # hold <= 0 表示"一直挂着"，不能被自动摘掉
    state.set_expression("流泪", -1)
    assert state.expired_events() == []
    assert state.active_expressions() == ["流泪"]

    # 想收起就用 clear()
    assert state.clear() == ["流泪"]
    assert state.active_expressions() == []

    # 到期后 expired_events 能把表情摘出来（供引擎下发 active=False）
    state.set_expression("黑脸", 0.05)
    import time as _time

    _time.sleep(0.08)
    assert state.expired_events() == ["黑脸"]
    assert state.active_expressions() == []
    print("OK 表情互斥与到期回落")


def test_expression_activation_payload() -> None:
    """下发格式必须是这个 VTS 版本认的 expressionFile 结构。

    实测：传 activations 数组会报 APIError 650 并整条丢弃，只有单个
    {"expressionFile": "xxx.exp3.json", "active": bool} 有效。
    """

    state = ExpressionState(_catalog())
    state.set_expression("黑脸", 5.0)
    payload = state.to_inject()
    assert payload and payload[0]["expressionFile"] == "黑脸.exp3.json"
    assert payload[0]["active"] is True
    assert "fadeTime" in payload[0]
    assert "name" not in payload[0], "不能用 activations 的 name 字段"
    print("OK expressionFile 结构正确")


def test_prop_pairs() -> None:
    """动画道具必须成对解析出打开/关闭热键。"""

    pairs = resolve_pairs(DA_FEI_YU_HOTKEYS)
    assert pairs["狗尾巴草"] == ("id-dog-on", "id-dog-off")
    assert pairs["竖手指"] == ("id-finger-on", "id-finger-off")
    # 单边热键不能当道具用（没法收回）
    only_open = [{"name": "某道具打开", "hotkeyID": "x", "type": "TriggerAnimation"}]
    assert resolve_pairs(only_open) == {}
    print("OK 道具开/关配对正确")


def test_prop_state() -> None:
    """同一时间只挂一个道具，开新的要返回旧的用于收回。"""

    state = ExpressionState(_catalog())
    assert state.open_prop("狗尾巴草", 30) == []
    assert state.active_props() == ["狗尾巴草"]
    assert state.open_prop("竖手指", 30) == ["狗尾巴草"]
    assert state.active_props() == ["竖手指"]
    assert state.close_prop("竖手指") is True
    assert state.active_props() == []
    print("OK 道具互斥正确")


def test_detect_emotion_from_text() -> None:
    """AI 回复文本要能推断出表情。"""

    cases = {
        "……行吧，你开心就好": "无语",
        "哈哈哈哈哈笑死我了": "开心",
        "呜……有点难过": "委屈",
        "人家不好意思啦": "害羞",
        "我厉害吧，轻轻松松": "得意",
        "？？你说什么": "疑惑",
    }
    for text, expected in cases.items():
        assert detect_emotion(text) == expected, f"「{text}」应判为 {expected}"
    # 中性文本不触发（不能无脑乱给表情）
    assert detect_emotion("今天的天气是多云转晴") == ""
    assert detect_emotion("") == ""
    print("OK 回复文本情绪推断正确")


def test_crying_words_reach_the_tear_expression() -> None:
    """哭/眼泪这族词必须能判出「委屈」。

    回归用例：之前词表里只有「呜呜」没有「呜」「哭」「眼泪」，
    「眼泪都要下来了」这种明确到不能再明确的句子竟然一片空白，
    流泪那张脸永远不会被用到。
    """

    for text in (
        "眼泪都要下来了",
        "呜……你干嘛这样说我",
        "呜呜呜别骂了",
        "眼眶有点湿，抽泣了一声",
        "有点委屈巴巴的",
        # 自嘲不是骂人：她自己的台词里说「就是废物」是在示弱
        "你说得对，我就是废物",
    ):
        assert detect_emotion(text) == "委屈", f"「{text}」应判为委屈"
    print("OK 哭/眼泪词能落到委屈表情")


def test_expression_aliases_cover_synonyms() -> None:
    """标定出的表情要把同一情绪的中文叫法一起交给记号层。

    记号解析只认词表里的词：只给「委屈」的话，AI 写【哭】时记号会被
    当普通文字摘掉，什么也不会发生——看起来就是"说了哭却没有眼泪"。
    """

    cry = ExpressionAsset(name="流泪", kind="emotion", label="cry")
    words = expression_aliases(cry)
    assert "哭" in words and "流泪" in words, words
    # sad ↔ cry 是相邻情绪：标成 sad 的泪脸也该听得懂「哭」
    assert "委屈" in words, words
    # 日文别名不进中文提示词（泣き/照れ 只会让模型以为要写日语）
    assert not any(any("\u3040" <= ch <= "\u30ff" for ch in w) for w in words), words
    assert emotion_synonyms("") == []


def test_detect_emotion_custom_map() -> None:
    """用户自定义映射优先级最高。"""

    extra = parse_emotion_map("无语=＞＜, 开心=爱心眼，测试=黑脸")
    assert extra == {"无语": "＞＜", "开心": "爱心眼", "测试": "黑脸"}
    assert detect_emotion("这个测试真无语", extra) == "＞＜"
    print("OK 自定义映射优先")


def test_detect_prop_from_text() -> None:
    """道具触发词。"""

    assert detect_prop("给你一根狗尾巴草") == "狗尾巴草"
    assert detect_prop("给你点个赞") == "竖手指"
    assert detect_prop("今天天气不错") == ""
    print("OK 道具触发词正确")


if __name__ == "__main__":
    test_display_expressions_excluded()
    test_emotion_matching()
    test_expression_mutex_and_expiry()
    test_expression_activation_payload()
    test_prop_pairs()
    test_prop_state()
    test_detect_emotion_from_text()
    test_crying_words_reach_the_tear_expression()
    test_detect_emotion_custom_map()
    test_detect_prop_from_text()
