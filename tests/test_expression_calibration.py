# -*- coding: utf-8 -*-
"""表情标定回归：换一个「名字没有任何语义」的模型，也必须能认出哪张脸是什么情绪。

覆盖四层证据：
  1. .exp3.json 的 Cubism 参数 → FACS AU → 情绪原型（与名字语言无关）；
  2. 中日英名字语义（照れ / blush / 黑脸）；
  3. 结构排除法（换装、部件开关、成对热键不进情绪池）；
  4. 证据不足时**不贴标签**（unknown），绝不瞎给脸。

另含道具行为分类（toy / gesture / look / effect）的回归。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vts.expression import (  # noqa: E402
    ExpressionCatalog,
    canonical_emotion,
    face_units_from_params,
    name_evidence,
    parse_expression_document,
    read_expression_parameters,
    score_face_units,
)
from vts.props import (  # noqa: E402
    PROP_CLASS_EFFECT,
    PROP_CLASS_GESTURE,
    PROP_CLASS_LOOK,
    PROP_CLASS_TOY,
    PropCandidate,
    classify_name,
    item_candidates,
    prop_candidates,
)

# --------------------------------------------------------------------- #
# 一个「名字毫无意义」的模型：exp01..exp06，只有表情文件里的参数能说明它是什么
# --------------------------------------------------------------------- #
OPAQUE_EXPRESSIONS = ["exp01", "exp02", "exp03", "exp04", "exp05", "exp06"]
OPAQUE_FILES = {name: f"{name}.exp3.json" for name in OPAQUE_EXPRESSIONS}
OPAQUE_PARAMS = {
    # 笑：笑眼 + 嘴角上扬 + 微张嘴（AU6 + AU12 + AU26）
    "exp01.exp3.json": {
        "ParamEyeLSmile": 1.0, "ParamEyeRSmile": 1.0,
        "ParamMouthForm": 0.8, "ParamMouthOpenY": 0.3,
    },
    # 惊讶：抬眉 + 睁大眼 + 张嘴（AU1/AU2 + AU5 + AU26）
    "exp02.exp3.json": {
        "ParamBrowLY": 1.0, "ParamBrowRY": 1.0,
        "ParamEyeLOpen": 1.4, "ParamEyeROpen": 1.4, "ParamMouthOpenY": 0.8,
    },
    # 哭：泪参数 + 嘴角下垮 + 压眉（TEAR + AU15 + AU4）
    "exp03.exp3.json": {
        "ParamTear": 1.0, "ParamMouthForm": -0.8,
        "ParamBrowLY": -0.5, "ParamBrowRY": -0.5,
    },
    # 害羞：脸红 + 笑眼 + 小笑（BLUSH + AU6 + AU12）
    "exp04.exp3.json": {
        "ParamCheek": 1.0, "ParamEyeLSmile": 0.4, "ParamEyeRSmile": 0.4,
        "ParamMouthForm": 0.3,
    },
    # 只动视线/头发这类非表情参数：与情绪无关，不贴标签
    "exp05.exp3.json": {
        "ParamEyeBallX": 0.8, "ParamEyeBallY": -0.3, "ParamHairFront": 0.5,
    },
    # 换装：只改身体/服装参数，跟脸无关
    "exp06.exp3.json": {"ParamBodyAngleX": 10.0, "ParamArmLA": 30.0},
}


def _opaque_catalog() -> ExpressionCatalog:
    return ExpressionCatalog(
        model_name="opaque-model",
        expressions=OPAQUE_EXPRESSIONS,
        hotkeys=[],
        expression_files=OPAQUE_FILES,
        expression_params=OPAQUE_PARAMS,
    )


def test_calibrate_without_any_name_semantics() -> None:
    """exp01 这类名字没有任何语义，只能靠表情文件参数标定。"""

    catalog = _opaque_catalog()
    labeled = catalog.labeled
    assert labeled.get("开心") == "exp01", labeled
    assert labeled.get("惊讶") == "exp02", labeled
    assert labeled.get("哭") == "exp03", labeled
    assert labeled.get("害羞") == "exp04", labeled
    print("OK 纯参数标定：", labeled)


def test_non_face_params_stay_unlabeled() -> None:
    """只动视线/头发/身体的表情与情绪无关：不贴标签，也不进情绪轮换。"""

    catalog = _opaque_catalog()
    assert "exp05" in catalog.unknown_names
    assert "exp06" in catalog.unknown_names
    assert catalog.match("无语") == ""


def test_match_uses_calibration_not_name_guess() -> None:
    """情绪词能落到标定结果上；没标定的情绪不能硬塞一张脸。"""

    catalog = _opaque_catalog()
    assert catalog.match("开心") == "exp01"
    assert catalog.match("happy") == "exp01"          # 英文别名同义
    assert catalog.match("哭") == "exp03"
    assert catalog.match("生气") == "", "没有生气脸就不能瞎给一张"


def test_japanese_and_english_names() -> None:
    """日式/英式皮套的命名同样要认，靠同一套多语言词表。"""

    catalog = ExpressionCatalog(
        model_name="yumi",
        expressions=["照れ", "怒り", "泣き", "ジト目", "Blush", "Smug"],
    )
    labeled = catalog.labeled
    assert labeled.get("害羞") in ("照れ", "Blush")
    assert labeled.get("生气") == "怒り"
    assert labeled.get("哭") == "泣き"
    assert labeled.get("无语") == "ジト目"
    assert labeled.get("得意") == "Smug"


def test_manual_map_wins_over_evidence() -> None:
    """人工映射优先级最高：作者起了个怪名也能救。"""

    catalog = ExpressionCatalog(
        model_name="weird",
        expressions=["脸A", "脸B"],
        asset_map={"脸B": "无语"},
    )
    assert catalog.match("无语") == "脸B"
    assert canonical_emotion("无语") == "deadpan"


def test_expression_document_parsing() -> None:
    """解析 .exp3.json：只认 Add/Override 的 Value，Multiply 与坏数据跳过。"""

    doc = {
        "Type": "Live2D Expression",
        "Parameters": [
            {"Id": "ParamMouthForm", "Value": 0.9, "Blend": "Add"},
            {"Id": "ParamEyeLOpen", "Value": 1.2, "Blend": "Add"},
            {"Id": "ParamScale", "Value": 2.0, "Blend": "Multiply"},
            {"Id": "", "Value": 1.0},
            {"Id": "ParamCheek", "Value": "bad"},
        ],
    }
    params = parse_expression_document(doc)
    assert params == {"ParamMouthForm": 0.9, "ParamEyeLOpen": 1.2}
    assert parse_expression_document({"Parameters": "oops"}) == {}


def test_face_units_follow_cubism_semantics() -> None:
    """参数到 AU 的映射要符合 Live2D 官方语义（+笑 / −怒 / 睁大 / 眯眼）。"""

    units = face_units_from_params({
        "ParamMouthForm": 1.0,          # + 笑 -> AU12
        "ParamEyeLSmile": 1.0,          # 笑眼 -> AU6
        "ParamEyeROpen": 1.5,           # 睁大 -> AU5
        "ParamBrowLY": -1.0,            # 压眉 -> AU4
        "ParamCheek": 1.0,              # 脸红
        "ParamAngleX": 30.0,            # 头部角度：不是表情，忽略
    })
    assert units["AU12"] == 1.0 and units["AU6"] == 1.0
    assert units["AU5"] > 0 and units["AU4"] > 0
    assert units["BLUSH"] == 1.0
    assert "AU26" not in units

    scores = score_face_units(units)
    assert max(scores, key=scores.get) in ("happy", "shy")


def test_artmesh_toggle_params_fall_back_to_name() -> None:
    """真机回归：示例模型的表情文件只翻一个 artmesh 开关参数（key1/key2/key9）。

    这种文件在参数层没有任何可用的面部信息，**不能**因此判成「换装」把脸丢掉；
    必须回到名字语义，否则 7 张脸会全变成展示型（实测踩过）。
    """

    files = {name: f"Exp3/{name}.exp3.json" for name in ("流泪", "黑脸", "爱心眼", "兔牙")}
    params = {
        "Exp3/流泪.exp3.json": {"key2": 1.0},
        "Exp3/黑脸.exp3.json": {"key1": 1.0},
        "Exp3/爱心眼.exp3.json": {"key4": 1.0},
        "Exp3/兔牙.exp3.json": {"key9": 1.0},
    }
    catalog = ExpressionCatalog(
        model_name="示例模型",
        expressions=list(files),
        expression_files=files,
        expression_params=params,
    )
    assert catalog.match("委屈") == "流泪"
    assert catalog.match("无语") == "黑脸"
    assert catalog.match("开心") == "爱心眼"
    assert "兔牙" not in catalog.emotion_names, "兔牙是外观件，名字表里明确排除"


def test_custom_param_with_body_token_is_posture() -> None:
    """自定义 ID 里带 lean/body 这类词根时仍能判出部位（实测参数名就叫 LeanInput）。"""

    from vts.assetfile import GROUP_BODY, group_of_param

    assert group_of_param("LeanInput") == GROUP_BODY
    assert group_of_param("key2") == "custom"
    assert group_of_param("ParamMouthForm") == "mouth"


def test_display_and_pairs_excluded_from_rotation() -> None:
    """换装、体型、成对开关不能进情绪池（否则 AI 演着演着换了套衣服）。"""

    hotkeys = [
        {"name": "衣服切换", "hotkeyID": "h1", "type": "ToggleExpression"},
        {"name": "体型2", "hotkeyID": "h2", "type": "ToggleExpression"},
    ]
    catalog = ExpressionCatalog(
        model_name="m",
        expressions=["衣服切换", "体型2", "尾巴开", "尾巴关", "照れ"],
        hotkeys=hotkeys,
    )
    assert catalog.emotion_names == ["照れ"]
    assert catalog.match("害羞") == "照れ"


def test_unknown_model_degrades_quietly() -> None:
    """完全不认识的模型：不报错、不给脸，但清单照常可用。"""

    catalog = ExpressionCatalog(model_name="pngtuber", expressions=["exp01", "exp02"])
    assert catalog.match("无语") == ""
    assert catalog.unknown_names == ["exp01", "exp02"]
    assert "表情 2 个" in catalog.describe()


def test_read_parameters_without_roots_is_safe() -> None:
    """模型目录不存在时安静返回空字典（PNGTuber / 非默认安装路径）。"""

    assert read_expression_parameters({"a": "a.exp3.json"}, "x", roots=[]) == {}


# --------------------------------------------------------------------- #
# 道具：行为分类
# --------------------------------------------------------------------- #
def test_prop_classification_is_multilingual() -> None:
    assert classify_name("狗尾巴草") == PROP_CLASS_TOY
    assert classify_name("outfit change") == PROP_CLASS_LOOK
    assert classify_name("服装切换") == PROP_CLASS_LOOK
    assert classify_name("樱花背景", item_type="Effect") == PROP_CLASS_EFFECT
    assert classify_name("Anim Shake", kind="oneshot") == PROP_CLASS_GESTURE
    assert classify_name("点头", kind="oneshot") == PROP_CLASS_GESTURE


def test_look_and_effect_items_are_dropped() -> None:
    """换装物品与背景特效不进自动道具候选。"""

    items = [
        {"name": "Halo", "instanceID": "i1", "type": "PNGTuber", "canLoad": True},
        {"name": "女仆装", "instanceID": "i2", "type": "Live2D", "canLoad": True},
        {"name": "樱花飘落", "instanceID": "i3", "type": "Effect", "canLoad": True},
        {"name": "sakura effect", "instanceID": "i4", "type": "Live2D", "canLoad": True},
    ]
    names = [c.name for c in item_candidates(items)]
    assert names == ["Halo"]


def test_gesture_holds_briefly_toy_holds_longer() -> None:
    """一次性动作挂 1~3 秒就收，道具可以挂十几秒。"""

    toy = PropCandidate("狗尾巴草", "hotkey", "on", "off", cls=PROP_CLASS_TOY)
    gesture = PropCandidate("Anim Shake", "oneshot", "hk", cls=PROP_CLASS_GESTURE)
    assert gesture.hold_seconds is not None and gesture.hold_seconds <= 3.0
    assert toy.hold_seconds is None
    # 类权重：道具比动作更常被选中
    assert toy.cute > gesture.cute


def test_prop_candidates_union_still_works() -> None:
    items = [{"name": "狗尾巴草", "instanceID": "i1", "type": "Live2D", "canLoad": True}]
    pairs = {"竖手指": ("on", "off")}
    hotkeys = [
        {"name": "竖手指打开", "hotkeyID": "on", "type": "TriggerAnimation"},
        {"name": "竖手指关闭", "hotkeyID": "off", "type": "TriggerAnimation"},
        {"name": "Anim Shake", "hotkeyID": "hk", "type": "TriggerAnimation"},
    ]
    merged = prop_candidates(items, pairs, hotkeys)
    assert [c.name for c in merged] == ["狗尾巴草", "竖手指", "Anim Shake"]
    assert [c.cls for c in merged] == [PROP_CLASS_TOY, PROP_CLASS_TOY, PROP_CLASS_GESTURE]


def test_name_evidence_prefers_longer_token() -> None:
    """长词优先：流冷汗不能被「汗」抢成别的情绪。"""

    evidence = name_evidence("流冷汗")
    assert evidence["awkward"] == 1.0
    assert "泪" not in name_evidence("流冷汗")
