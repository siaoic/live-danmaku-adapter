# -*- coding: utf-8 -*-
"""端到端：AI 的一句话 → 情绪 → 表情文件/参数 → VTS 收到的帧。

这条链路里唯一被替换的是「和 VTS 之间的 WebSocket」：`FakeVTS` 继承真实的
``VTS``，只把 ``connect()``/``call()`` 换成回放真实报文结构的假服务端。
因此以下代码全部是生产路径：
``VTS.load_catalog()`` → ``FileIndex`` 读真实 .exp3/.motion3 → ``ExpressionCatalog``
标定 → ``Engine.set_expression()`` → ``ExpressionState`` → ``ExpressionActivationRequest``；
以及参数层的 ``Engine.compose()`` → ``InjectParameterDataRequest``。

断言三件事：
  1. **表情文件能被用上**：说「难过」时真的下发 ``流泪.exp3.json``；
  2. **点赞/换装类不会被误用**：兔牙/钢盆/前倾 只动外观，不进情绪池；
  3. **点头不依赖动画**：引擎用参数（FaceAngleY）自己点头，不触发任何热键。
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_ENGINE_DIR = _PLUGIN_DIR / "engine"

if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))

import performance_engine as pe  # noqa: E402
from vts.expression import detect_emotion  # noqa: E402

MODEL_NAME = "大肥鱼"

#: 表情名 -> 相对路径（VTS ExpressionStateRequest 返回的形状）
EXPRESSION_FILES = {
    "流泪": "expressions/流泪.exp3.json",
    "黑脸": "expressions/黑脸.exp3.json",
    "爱心眼": "expressions/爱心眼.exp3.json",
    "兔牙": "expressions/兔牙.exp3.json",
    "钢盆": "expressions/钢盆.exp3.json",
    "前倾": "expressions/前倾.exp3.json",
    "脸红": "expressions/脸红.exp3.json",
}

#: 表情文件内容（真实大肥鱼的这些文件大致就是这么做出来的）
EXPRESSION_DOCS = {
    # 委屈/哭：泪 + 嘴角垮 + 压眉
    "expressions/流泪.exp3.json": {"Parameters": [
        {"Id": "ParamTear", "Value": 1.0, "Blend": "Add"},
        {"Id": "ParamMouthForm", "Value": -0.8, "Blend": "Add"},
        {"Id": "ParamBrowLY", "Value": -0.5, "Blend": "Add"},
        {"Id": "ParamBrowRY", "Value": -0.5, "Blend": "Add"},
    ]},
    # 无语/黑脸：压眉 + 半闭眼 + 垮嘴 + 阴影
    "expressions/黑脸.exp3.json": {"Parameters": [
        {"Id": "ParamBrowLY", "Value": -1.0},
        {"Id": "ParamBrowRY", "Value": -1.0},
        {"Id": "ParamEyeLOpen", "Value": 0.35},
        {"Id": "ParamEyeROpen", "Value": 0.35},
        {"Id": "ParamMouthForm", "Value": -0.6},
        {"Id": "ParamShadow", "Value": 1.0},
    ]},
    # 开心：笑眼 + 嘴角上扬
    "expressions/爱心眼.exp3.json": {"Parameters": [
        {"Id": "ParamEyeLSmile", "Value": 1.0},
        {"Id": "ParamEyeRSmile", "Value": 1.0},
        {"Id": "ParamMouthForm", "Value": 0.9},
    ]},
    # 害羞：脸红
    "expressions/脸红.exp3.json": {"Parameters": [{"Id": "ParamCheek", "Value": 1.0}]},
    # 兔牙/钢盆：只切换一个自定义外观参数（artmesh 显隐）
    "expressions/兔牙.exp3.json": {"Parameters": [{"Id": "ParamTooth", "Value": 1.0}]},
    "expressions/钢盆.exp3.json": {"Parameters": [{"Id": "ParamBowl", "Value": 1.0}]},
    # 前倾：只动身体和头部角度
    "expressions/前倾.exp3.json": {"Parameters": [
        {"Id": "ParamBodyAngleX", "Value": 8.0},
        {"Id": "ParamAngleX", "Value": 5.0},
    ]},
}

HOTKEYS = [
    *[
        {"name": name, "hotkeyID": f"hk-{index}", "type": "ToggleExpression", "file": path}
        for index, (name, path) in enumerate(EXPRESSION_FILES.items())
    ],
    {"name": "狗尾巴草打开", "hotkeyID": "hk-dog-on", "type": "TriggerAnimation",
     "file": "motions/dog_on.motion3.json"},
    {"name": "狗尾巴草关闭", "hotkeyID": "hk-dog-off", "type": "TriggerAnimation",
     "file": "motions/dog_off.motion3.json"},
]

MOTION_DOG_ON = {"Version": 3, "Meta": {"Duration": 1.0, "Loop": True, "CurveCount": 1},
                 "Curves": [{"Target": "Parameter", "Id": "ParamHairFront",
                             "Segments": [0.0, 0.0, 1.0, 0.5]}]}
MOTION_DOG_OFF = {"Version": 3, "Meta": {"Duration": 1.0, "Loop": True, "CurveCount": 1},
                  "Curves": [{"Target": "Parameter", "Id": "ParamHairFront",
                              "Segments": [0.0, 0.5, 1.0, 0.0]}]}

#: VTS InputParameterListRequest 的默认参数（引擎注入的 Face* 系列）
INPUT_PARAMS = [
    "FaceAngleX", "FaceAngleY", "FaceAngleZ", "FaceEyeOpen", "FaceEyeSmile",
    "FaceMouthOpen", "FaceMouthForm", "FaceBrowY", "FaceBrowForm", "FaceCheek",
    "FaceBreath", "FaceEyeBallX", "FaceEyeBallY",
]


def _build_model_dir(root: Path) -> None:
    model = root / MODEL_NAME
    for rel, payload in EXPRESSION_DOCS.items():
        path = model / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (model / "motions").mkdir(parents=True, exist_ok=True)
    (model / "motions" / "dog_on.motion3.json").write_text(
        json.dumps(MOTION_DOG_ON), encoding="utf-8")
    (model / "motions" / "dog_off.motion3.json").write_text(
        json.dumps(MOTION_DOG_OFF), encoding="utf-8")


class FakeVTS(pe.VTS):
    """只替换传输层：报文结构与真实 VTS 一致。"""

    def __init__(self) -> None:
        self.ws = object()          # 骗过 "if self.ws is None: connect()"
        self.valid_inputs = set()
        self._inputs_at = 0.0
        self._io = threading.Lock()
        self.sent: list = []
        self.injected: list = []
        self.last_error = ""

    def connect(self) -> bool:      # 永远"已连接"
        return True

    def call(self, msg, data=None):
        self.sent.append((msg, dict(data or {})))
        if msg == "CurrentModelRequest":
            return {"messageType": "CurrentModelResponse", "data": {"modelName": MODEL_NAME}}
        if msg == "ExpressionStateRequest":
            return {"messageType": "ExpressionStateResponse", "data": {"expressions": [
                {"name": name, "file": path} for name, path in EXPRESSION_FILES.items()
            ]}}
        if msg == "HotkeysInCurrentModelRequest":
            return {"messageType": "HotkeysInCurrentModelResponse",
                    "data": {"availableHotkeys": HOTKEYS}}
        if msg == "InputParameterListRequest":
            return {"messageType": "InputParameterListResponse", "data": {
                "defaultParameters": [{"name": n} for n in INPUT_PARAMS],
                "customParameters": [],
            }}
        if msg == "ItemListRequest":
            return {"messageType": "ItemListResponse", "data": {"availableItems": []}}
        if msg == "InjectParameterDataRequest":
            self.injected.append({item["id"]: item["value"]
                                  for item in (data or {}).get("parameterValues", [])})
            return {"messageType": "InjectParameterDataResponse", "data": {}}
        return {"messageType": msg.replace("Request", "Response"), "data": {}}

    # ---- 断言用
    def expressions_sent(self) -> list:
        return [
            (data.get("expressionFile"), data.get("active"))
            for msg, data in self.sent if msg == "ExpressionActivationRequest"
        ]

    def hotkeys_triggered(self) -> list:
        return [data.get("hotkeyID") for msg, data in self.sent if msg == "HotkeyTriggerRequest"]


def _engine(tmp_path: Path) -> "pe.Engine":
    import os

    root = tmp_path / "models"
    _build_model_dir(root)
    os.environ["VTS_MODEL_DIRS"] = str(root)
    pe.VTS = FakeVTS            # Engine.__init__ 里 self.vts = VTS()
    engine = pe.Engine()
    assert isinstance(engine.vts, FakeVTS)
    return engine


# --------------------------------------------------------------------- #
# 1. 标定：文件 + 名字两路证据都指向同一张脸
# --------------------------------------------------------------------- #
def test_catalog_calibrates_real_expression_files(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    catalog = engine.catalog
    # 行为断言：情绪词落到哪张脸（而不是标定内部用了哪个 key）
    assert catalog.match("委屈") == "流泪"
    assert catalog.match("哭") == "流泪"
    assert catalog.match("无语") == "黑脸"
    assert catalog.match("开心") == "爱心眼"
    assert catalog.match("害羞") == "脸红"
    # 只动外观/身体的三张脸不能进情绪池
    for name in ("兔牙", "钢盆", "前倾"):
        assert name not in catalog.emotion_names
    print("\n[标定]", catalog.describe())
    print("[情绪→表情]", catalog.labeled)
    for line in catalog.calibration_report():
        print("   ", line)


# --------------------------------------------------------------------- #
# 2. AI 说的那句话 -> 表情文件真的下发
# --------------------------------------------------------------------- #
def test_ai_text_triggers_expression_file(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    fake: FakeVTS = engine.vts  # type: ignore[assignment]

    cases = {
        "呜……有点难过": "流泪",
        "……行吧，你开心就好": "黑脸",
        "哈哈哈哈哈笑死我了": "爱心眼",
        "人家不好意思啦": "脸红",
    }
    for text, expected_name in cases.items():
        emotion = detect_emotion(text)
        assert emotion, f"「{text}」应该判出情绪"
        ok, result = engine.set_expression(emotion)
        assert ok and result == expected_name, f"「{text}」-> {emotion} 应给 {expected_name}，实际 {result}"
        file_name, active = fake.expressions_sent()[-1]
        assert file_name == EXPRESSION_FILES[expected_name] and active is True
        print(f"[表情] 「{text}」-> {emotion} -> {result} -> {file_name}")


# --------------------------------------------------------------------- #
# 3. 展示型不能被当成情绪/道具误用（兔牙、钢盆、前倾）
# --------------------------------------------------------------------- #
def test_display_expressions_not_misused(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    fake: FakeVTS = engine.vts  # type: ignore[assignment]
    before = len(fake.sent)

    for name in ("兔牙", "钢盆", "前倾"):
        ok, _ = engine.set_expression(name)
        assert ok is False, f"{name} 不该被当成情绪表情"
        assert engine.set_prop(name, 10.0)[0] is False, f"{name} 不是成对热键，不该能开"
    # 不应该有任何表情激活帧发出去
    assert not [m for m, _ in fake.sent[before:] if m == "ExpressionActivationRequest"]


# --------------------------------------------------------------------- #
# 4. 点头：不靠动画、不靠热键，纯参数
# --------------------------------------------------------------------- #
def test_nod_is_done_by_parameters(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    fake: FakeVTS = engine.vts  # type: ignore[assignment]

    import time as _time

    now = _time.time()
    for step in range(4):                       # 先跑几帧建立基线
        engine.compose(now + step * 0.02)
    base = [frame.get("FaceAngleY", 0.0) for frame in fake.injected]
    assert base, "引擎没有注入任何参数"

    assert engine.play_tag("nod") is True, "nod 片段应该在 cortico_clips 里"
    for step in range(60):                      # 1.2 秒，覆盖点头片段长度
        engine.compose(now + 0.1 + step * 0.02)
    tail = [frame.get("FaceAngleY", 0.0) for frame in fake.injected[len(base):]]
    span = max(tail) - min(tail)
    assert span > 2.0, f"点头幅度只有 {span:.1f}°，参数层没有真的点头"
    assert not fake.hotkeys_triggered(), "点头不该触发任何 VTS 热键"
    print(f"[点头] FaceAngleY 摆幅 {span:.1f}°（min={min(tail):.1f}, max={max(tail):.1f}）")


# --------------------------------------------------------------------- #
# 5. 道具：成对热键按文件内容判成 toy，可以开合
# --------------------------------------------------------------------- #
def test_paired_hotkey_prop(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    fake: FakeVTS = engine.vts  # type: ignore[assignment]
    assert engine.catalog.pairs.get("狗尾巴草") == ("hk-dog-on", "hk-dog-off")
    candidates = {c.name: c for c in engine.props.candidates()}
    assert "狗尾巴草" in candidates
    assert candidates["狗尾巴草"].cls == "toy"
    ok, name = engine.set_prop("狗尾巴草", 20.0)
    assert ok and name == "狗尾巴草"
    assert "hk-dog-on" in fake.hotkeys_triggered()


# --------------------------------------------------------------------- #
# 6. 外观件（兔牙/钢盆/前倾）：由台词记号点单，可戴可收
# --------------------------------------------------------------------- #
def test_pose_toggle_by_marker(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    fake: FakeVTS = engine.vts  # type: ignore[assignment]

    ok, message = engine.set_pose("兔牙")
    assert ok and "戴上" in message
    assert fake.expressions_sent()[-1] == (EXPRESSION_FILES["兔牙"], True)
    assert engine.active_poses() == ["兔牙"]

    # 再说一次就收起（toggle）
    ok, message = engine.set_pose("兔牙")
    assert ok and "收起" in message
    assert fake.expressions_sent()[-1] == (EXPRESSION_FILES["兔牙"], False)
    assert engine.active_poses() == []

    # 钢盆和兔牙可以同时戴：外观件不做互斥
    engine.set_pose("钢盆")
    engine.set_pose("前倾")
    assert set(engine.active_poses()) == {"钢盆", "前倾"}

    # 情绪表情不能当外观件，未知名字要明确失败
    assert engine.set_pose("黑脸")[0] is False
    assert engine.set_pose("没这个")[0] is False


def test_pose_expires_automatically(tmp_path: Path) -> None:
    """到点自动收起，不会一直戴着。"""

    import time as _time

    engine = _engine(tmp_path)
    fake: FakeVTS = engine.vts  # type: ignore[assignment]
    engine.set_pose("前倾", hold_seconds=0.05)
    _time.sleep(0.08)
    engine._expire_poses(_time.time())
    assert engine.active_poses() == []
    assert fake.expressions_sent()[-1] == (EXPRESSION_FILES["前倾"], False)


def test_actions_payload_feeds_prompt(tmp_path: Path) -> None:
    """动作词表要覆盖四类，且能直接喂给提示词生成器。"""

    from vts.markers import build_action_prompt

    engine = _engine(tmp_path)
    payload = engine.actions_payload()
    entries = {(item["key"], item["kind"]) for item in payload["actions"]}
    assert ("兔牙", "pose") in entries
    assert ("狗尾巴草", "prop") in entries
    assert ("黑脸", "expression") in entries
    assert ("nod", "tag") in entries
    nod = next(item for item in payload["actions"] if item["key"] == "nod")
    assert "点头" in nod["aliases"]

    prompt = build_action_prompt(payload["actions"])
    assert "【兔牙】" in prompt and "<点头>" in prompt
    print("\n[提示词]\n" + prompt)


if __name__ == "__main__":
    import tempfile

    for func in (
        test_catalog_calibrates_real_expression_files,
        test_ai_text_triggers_expression_file,
        test_display_expressions_not_misused,
        test_nod_is_done_by_parameters,
        test_paired_hotkey_prop,
        test_pose_toggle_by_marker,
        test_pose_expires_automatically,
        test_actions_payload_feeds_prompt,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            func(Path(tmp))
    print("\n端到端通过")
