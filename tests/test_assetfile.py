# -*- coding: utf-8 -*-
"""按文件内容判断热键/表情的作用：不写死任何皮套的名字。

核心回归：
  * 名字完全无意义（``动作1`` / ``exp07``）时，靠 .motion3/.exp3 的内容分类；
  * 名字像道具、内容其实是换装（只动身体/头发）→ 判为 look，不进自动道具；
  * 名字像换装、内容其实在改脸 → 仍然算表情，不能被名字一票否决；
  * 模型目录找不到时安静降级（返回 None，让调用方退回名字语义）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vts.assetfile import (  # noqa: E402
    GROUP_BODY,
    GROUP_EYE,
    GROUP_HAIR,
    GROUP_MOUTH,
    KIND_EXPRESSION,
    KIND_MOTION,
    FileIndex,
    analyze_document,
    group_of_param,
    groups_of_params,
    parse_motion_document,
)
from vts.expression import ExpressionCatalog  # noqa: E402
from vts.props import (  # noqa: E402
    PROP_CLASS_GESTURE,
    PROP_CLASS_LOOK,
    PROP_CLASS_TOY,
    oneshot_candidates,
    prop_candidates,
)


def _write(root: Path, rel: str, payload: dict) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _motion(duration: float, loop: bool, curves: dict) -> dict:
    return {
        "Version": 3,
        "Meta": {"Duration": duration, "Fps": 30.0, "Loop": loop, "CurveCount": len(curves)},
        "Curves": [
            {"Target": "Parameter", "Id": pid, "Segments": [0.0, 0.0, duration, float(value)]}
            for pid, value in curves.items()
        ],
    }


def test_group_of_param_covers_standard_and_custom() -> None:
    """标准参数按前缀归部位；自定义参数带「泪/汗/脸红」这类词根算脸。"""

    assert group_of_param("ParamMouthForm") == GROUP_MOUTH
    assert group_of_param("ParamEyeLSmile") == GROUP_EYE
    assert group_of_param("ParamBodyAngleX") == GROUP_BODY
    assert group_of_param("ParamHairFront") == GROUP_HAIR
    assert group_of_param("ParamTear") == "face"
    assert group_of_param("ParamWhatever") == "custom"
    assert groups_of_params(["ParamMouthForm", "ParamCheek"]) >= {GROUP_MOUTH}


def test_motion_document_parsing() -> None:
    """动作文件要能读出时长、循环标记与动到的部位。"""

    doc = _motion(2.4, False, {"ParamAngleX": 18, "ParamBodyAngleZ": -6})
    duration, loop, curves, count = parse_motion_document(doc)
    assert abs(duration - 2.4) < 1e-6 and loop is False
    assert set(curves) == {"ParamAngleX", "ParamBodyAngleZ"} and count == 2
    info = analyze_document("bounce.motion3.json", doc)
    assert info.kind == KIND_MOTION and info.is_one_shot
    # 头/身体角度不是「脸上的戏」，不能拿去标定情绪
    assert not info.touches_face
    assert {"head", "body"} <= set(info.groups)

    face_doc = _motion(1.0, False, {"ParamMouthOpenY": 0.9, "ParamEyeLSmile": 1.0})
    assert analyze_document("talk.motion3.json", face_doc).touches_face


def test_body_only_motion_is_look(tmp_path: Path) -> None:
    """只动身体/头发的一对热键，本质是换姿势，不能当道具乱开。"""

    root = tmp_path / "models"
    model = root / "yumi"
    _write(model, "motions/lean_on.motion3.json", _motion(3.0, True, {"ParamBodyAngleX": 8}))
    _write(model, "motions/lean_off.motion3.json", _motion(3.0, True, {"ParamBodyAngleX": 0}))
    index = FileIndex(model_name="yumi", roots=[root])
    info = index.analyze("motions/lean_on.motion3.json")
    assert info is not None and info.only_body

    hotkeys = [
        {"name": "动作1打开", "hotkeyID": "hk-on", "type": "TriggerAnimation",
         "file": "motions/lean_on.motion3.json"},
        {"name": "动作1关闭", "hotkeyID": "hk-off", "type": "TriggerAnimation",
         "file": "motions/lean_off.motion3.json"},
    ]
    pairs = {"动作1": ("hk-on", "hk-off")}
    assets = {str(item["hotkeyID"]): index.analyze(str(item["file"])) for item in hotkeys}
    assert all(v is not None for v in assets.values())
    assert prop_candidates([], pairs, hotkeys, assets) == [], "换姿势不该进自动道具"


def test_oneshot_motion_becomes_gesture(tmp_path: Path) -> None:
    """没有配对键的一次性动画：名字叫「动作3」也要靠文件判成 gesture。"""

    root = tmp_path / "models"
    model = root / "yumi"
    _write(model, "motions/shake.motion3.json", _motion(1.1, False, {"ParamAngleZ": 12}))
    index = FileIndex(model_name="yumi", roots=[root])
    info = index.analyze("motions/shake.motion3.json")
    assert info is not None and info.is_one_shot

    hotkeys = [{"name": "动作3", "hotkeyID": "hk-3", "type": "TriggerAnimation",
                "file": "motions/shake.motion3.json"}]
    got = oneshot_candidates(hotkeys, {}, {"hk-3": info})
    assert [c.name for c in got] == ["动作3"]
    assert got[0].cls == PROP_CLASS_GESTURE


def test_hotkey_named_like_prop_but_content_is_face(tmp_path: Path) -> None:
    """名字像道具、内容是表情：文件证据优先，当可挂的小表情（toy）。"""

    root = tmp_path / "models"
    model = root / "yumi"
    _write(model, "expressions/wink.exp3.json", {
        "Parameters": [
            {"Id": "ParamEyeLSmile", "Value": 1.0, "Blend": "Add"},
            {"Id": "ParamMouthForm", "Value": 0.6, "Blend": "Add"},
        ]
    })
    index = FileIndex(model_name="yumi", roots=[root])
    info = index.analyze("expressions/wink.exp3.json")
    assert info is not None and info.kind == KIND_EXPRESSION and info.touches_face

    pairs = {"狗尾巴草": ("hk-a", "hk-b")}
    got = prop_candidates([], pairs, [], {"hk-a": info, "hk-b": info})
    assert [c.cls for c in got] == [PROP_CLASS_TOY], "文件名不叫狗尾巴草也照用文件内容判定"


def test_expression_catalog_prefers_file_over_name(tmp_path: Path) -> None:
    """名字里带「衣服」但文件在改脸：仍算表情，不能被关键字一票否决。"""

    root = tmp_path / "models"
    model = root / "weird"
    _write(model, "expressions/a.exp3.json", {
        "Parameters": [
            {"Id": "ParamEyeLSmile", "Value": 1.0},
            {"Id": "ParamEyeRSmile", "Value": 1.0},
            {"Id": "ParamMouthForm", "Value": 0.9},
        ]
    })
    _write(model, "expressions/b.exp3.json", {
        "Parameters": [{"Id": "ParamBodyAngleX", "Value": 10.0}]
    })
    index = FileIndex(model_name="weird", roots=[root])
    files = {"衣服切换（笑）": "expressions/a.exp3.json", "衣服切换2": "expressions/b.exp3.json"}
    assets = {name: index.analyze(path) for name, path in files.items()}
    assert all(info is not None for info in assets.values())

    catalog = ExpressionCatalog(
        model_name="weird",
        expressions=list(files),
        expression_files=files,
        expression_assets=assets,
    )
    assert catalog.match("开心") == "衣服切换（笑）"
    assert "衣服切换2" not in catalog.emotion_names, "只动身体的文件仍是展示型"


def test_missing_model_dir_degrades(tmp_path: Path) -> None:
    """模型目录不存在：解析返回 None，调用方自然退回名字语义。"""

    index = FileIndex(model_name="ghost", roots=[tmp_path / "nowhere"])
    assert index.analyze("expressions/a.exp3.json") is None
    assert index.analyze("") is None


def test_index_finds_by_subdir_and_basename(tmp_path: Path) -> None:
    """热键里的 file 常带子目录，也要能按文件名兜底找到。"""

    root = tmp_path / "models"
    model = root / "m"
    _write(model, "expressions/deep/x.exp3.json", {"Parameters": [{"Id": "ParamCheek", "Value": 1.0}]})
    index = FileIndex(model_name="m", roots=[root])
    assert index.find("expressions/deep/x.exp3.json") is not None
    info = index.analyze("x.exp3.json")
    assert info is not None and info.touches_face


def test_media_file_is_not_a_prop(tmp_path: Path) -> None:
    """图片/视频类热键（特效素材）不能当可循环道具。"""

    root = tmp_path / "models"
    model = root / "m"
    media = model / "effects" / "sakura.png"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"\x89PNG\r\n\x1a\n")
    index = FileIndex(model_name="m", roots=[root])
    info = index.analyze("effects/sakura.png")
    # 图片没有 JSON 可解析：读不到就降级（不进候选由名字兜底判 effect）
    assert info is None or info.kind != KIND_EXPRESSION
