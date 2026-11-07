# -*- coding: utf-8 -*-
"""情绪联动（表情 + 面部参数 + 身体姿态）的单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR / "engine"))

from emotion_pose import (  # noqa: E402
    DEFAULT_SUSTAIN_SECONDS,
    EMOTION_POSES,
    decayed,
    pose_for,
    pose_names,
)


def test_every_emotion_has_full_pose() -> None:
    """每个情绪都必须同时具备表情、持续面部偏置和身体姿态。

    只有表情没有身体 = 脸和身体各演各的，这正是要修的问题。
    """

    for emotion in pose_names():
        pose = EMOTION_POSES[emotion]
        assert pose.get("expression"), f"{emotion} 缺少表情"
        assert pose.get("sustain"), f"{emotion} 缺少持续面部偏置"
        assert pose.get("intro"), f"{emotion} 缺少入场动作"
        assert pose.get("body"), f"{emotion} 缺少身体姿态"
    print("OK 全部情绪都有表情+面部+身体：" + "、".join(pose_names()))


def test_body_bias_uses_head_channels() -> None:
    """身体姿态必须走 FaceAngleX/Z 这类实际能带动身体的通道。

    实测：FaceAngleX 20° → ParamBodyAngleX 19.6（几乎 1:1），
    FaceAngleZ 20° → ParamBodyAngleZ -14.4；而 Mocopi 身体通道几乎没反应。
    所以身体偏置不能塞 Mocopi 参数。
    """

    for emotion in pose_names():
        body = EMOTION_POSES[emotion]["body"]
        for key in body:
            assert key in ("FaceAngleX", "FaceAngleY", "FaceAngleZ"), (
                f"{emotion} 的身体姿态用了 {key}，实测这个通道带不动身体")
        # 幅度不能大到像甩头
        for key, value in body.items():
            assert abs(value) <= 8.0, f"{emotion} 的 {key}={value} 太大，会像抽搐"
    print("OK 身体姿态全部走 FaceAngle 通道且幅度合理")


def test_pose_lookup() -> None:
    """能按情绪词取到配方，未知情绪返回 None。"""

    assert pose_for("无语") is not None
    assert pose_for("不存在的情绪") is None
    assert pose_for("") is None
    print("OK 配方查找正确")


def test_decay_holds_then_releases() -> None:
    """情绪姿态应该"保持一段再平滑收回"，不能一刀两断。"""

    # 前 70% 保持满值
    assert decayed(6.0, 0.0, 4.0) == 6.0
    assert decayed(6.0, 2.0, 4.0) == 6.0
    # 后 30% 平滑衰减
    mid = decayed(6.0, 3.5, 4.0)
    assert 0.0 < mid < 6.0
    # 过期归零
    assert decayed(6.0, 4.0, 4.0) == 0.0
    assert decayed(6.0, 9.9, 4.0) == 0.0
    print("OK 情绪姿态先保持后平滑收回")


def test_gaze_bias_present() -> None:
    """情绪要带视线偏置：无语飘走、委屈低头、害羞躲开。"""

    assert EMOTION_POSES["无语"]["gaze_bias"][0] < 0
    assert EMOTION_POSES["委屈"]["gaze_bias"][1] < 0     # 往下看
    assert EMOTION_POSES["害羞"]["gaze_bias"][0] > 0     # 躲开
    for emotion in pose_names():
        bx, by = EMOTION_POSES[emotion]["gaze_bias"]
        assert abs(bx) <= 1.0 and abs(by) <= 1.0, f"{emotion} 视线偏置越界"
    print("OK 视线偏置正确")


def test_engine_filters_after_emotion_blend() -> None:
    """情绪/手势叠加后必须再过滤一次，否则未知参数会让整批注入被拒。"""
    source = (PLUGIN_DIR / "engine" / "performance_engine.py").read_text(encoding="utf-8")
    # 找到叠加那两段，确认后面还有一次 profile.valid 过滤
    idx = source.find("self._blend_face(inputs, {k:")
    assert idx > 0, "找不到手势/情绪叠加"
    tail = source[idx:idx + 2000]
    assert "k in self.profile.valid" in tail, "情绪叠加后缺少参数过滤（会导致 453 整批丢弃）"
    print("OK 情绪叠加后有二次过滤")


def test_emotion_reaches_face_without_expression_file() -> None:
    """模型没有对应 .exp3.json 时，情绪也必须落在面部参数上。

    表情文件只是可选层：没有它不能连"脸色"一起没了，否则只剩身体在演。
    """

    source = (PLUGIN_DIR / "engine" / "performance_engine.py").read_text(encoding="utf-8")
    idx = source.find("def set_expression(self, emotion")
    assert idx > 0
    body = source[idx:idx + 3200]
    assert "posed = self.apply_emotion_pose(emotion, hold" in body, (
        "set_expression 必须无条件应用情绪姿态（且把时序一起传下去）")
    assert "timing=timing" in body, "参数层没有拿到三段时序，会和表情文件脱节"
    assert "if not name:" in body and "posed" in body, (
        "表情匹配失败时应回落到参数表情，而不是直接返回")
    assert "def _emotion_face_layer" in source, "缺少情绪面部层"
    # 面部偏置必须全额落到眉毛上（半幅会被 idle 噪声吃掉）
    assert '"BrowLeftY": 1.0' in source
    print("OK 无表情文件的模型也能把情绪演到脸上")


def test_au_faced_emotions_have_face_deltas() -> None:
    """无语/紧张/害怕/惊讶 必须按 AU 组合给出 face 增量（优先于 sustain clip）。"""

    poses = {emotion: EMOTION_POSES[emotion] for emotion in ("无语", "紧张", "害怕", "惊讶")}
    allowed = {"MouthSmile", "MouthOpen", "BrowLeftY", "BrowRightY",
               "EyeOpenLeft", "EyeOpenRight", "CheekPuff",
               "EyeLeftX", "EyeRightX", "EyeLeftY", "EyeRightY",
               "FaceAngleX", "FaceAngleY", "FaceAngleZ"}
    for emotion, pose in poses.items():
        assert pose.get("face"), f"{emotion} 缺少 face 增量"
        for key in pose["face"]:
            assert key in allowed, f"{emotion} 的 {key} 不是可注入通道"
    # 无语 = 厌恶/轻蔑（AU15 嘴角下压）；睁眼类情绪 = AU5
    assert poses["无语"]["face"]["MouthSmile"] < -0.3
    assert poses["害怕"]["face"]["EyeOpenLeft"] > 0.1
    assert poses["惊讶"]["face"]["EyeOpenLeft"] > 0.1
    # 惊讶的嘴保持中性：它不是笑，也不是撇嘴（那是悲伤）
    assert abs(poses["惊讶"]["face"]["MouthSmile"]) <= 0.05
    # 恐惧靠"瞪眼 + 后缩 + 抖"，不靠嘴角垮；嘴角幅度必须小于无语
    assert abs(poses["害怕"]["face"]["MouthSmile"]) < abs(poses["无语"]["face"]["MouthSmile"])
    print("OK 四个情绪有 AU 落点且方向正确")


def test_emotion_blink_and_gaze_style() -> None:
    """眨眼与视线节奏必须跟着情绪走（拟人化的关键细节）。"""

    for emotion in pose_names():
        pose = EMOTION_POSES[emotion]
        blink = pose.get("blink", "random")
        assert blink in ("random", "slow", "fast", "none"), f"{emotion} 的 blink={blink} 非法"
        rate = float(pose.get("gaze_restless", 1.0))
        assert 0.3 <= rate <= 3.0, f"{emotion} 的 gaze_restless={rate} 越界"
    assert EMOTION_POSES["尴尬"]["blink"] == "fast"      # 窘迫：急促连眨
    assert EMOTION_POSES["无语"]["blink"] == "slow"      # 无语：慢眨/闭眼停一拍
    assert EMOTION_POSES["害怕"]["blink"] == "none"      # 恐惧：瞪眼不眨
    assert EMOTION_POSES["尴尬"]["gaze_restless"] > 1.5  # 眼神乱瞟
    assert EMOTION_POSES["害怕"]["gaze_restless"] < 1.0  # 吓到定住
    print("OK 眨眼/视线节奏按情绪变化")


def test_emotion_rise_and_delay() -> None:
    """每个情绪有自己的上场时长，且眼睛先到、嘴角后到、左右脸有差。"""

    from emotion_pose import RISE_SECONDS

    for emotion in pose_names():
        assert emotion in RISE_SECONDS, f"{emotion} 缺少 rise 时长"
        assert 0.4 <= RISE_SECONDS[emotion] <= 1.2, f"{emotion} 的 rise 越界"
    # Hoffmann et al. (2010)：惊讶最快、悲伤最慢
    assert RISE_SECONDS["惊讶"] < RISE_SECONDS["无语"] < RISE_SECONDS["委屈"]
    assert pose_for("惊讶")["rise"] == RISE_SECONDS["惊讶"]
    assert pose_for("轻蔑")["rise"] == RISE_SECONDS["轻蔑"]

    source = (PLUGIN_DIR / "engine" / "performance_engine.py").read_text(encoding="utf-8")
    assert "FACE_DELAY" in source, "缺少通道上场延迟表"
    assert '"MouthSmile": 0.06' in source, "嘴角没有晚于眼睛到位"
    assert "asym = 1.03" in source and "asym = 0.97" in source, "缺少左右脸不对称"
    print("OK 表情时序：上场时长 + 通道先后 + 左右不对称")


def test_emotion_duration_and_offset() -> None:
    """表情总时长必须在真人区间（0.5~4s），且退场不短于上场。"""

    from emotion_pose import HOLD_SECONDS, OFFSET_SECONDS

    for emotion in pose_names():
        assert emotion in HOLD_SECONDS, f"{emotion} 缺少总时长"
        assert 0.5 <= HOLD_SECONDS[emotion] <= 4.0, (
            f"{emotion} 的 hold={HOLD_SECONDS[emotion]}s 不像真人表情（Ekman: 0.5~4s）")
        assert emotion in OFFSET_SECONDS and OFFSET_SECONDS[emotion] > 0
        assert OFFSET_SECONDS[emotion] <= HOLD_SECONDS[emotion]
    # 惊讶来得快去得快；委屈（悲伤）与开心（微笑）停留更久
    assert HOLD_SECONDS["惊讶"] == min(HOLD_SECONDS.values())
    assert HOLD_SECONDS["委屈"] >= 3.0 and HOLD_SECONDS["开心"] >= 3.0
    assert pose_for("惊讶")["hold"] == HOLD_SECONDS["惊讶"]
    assert pose_for("委屈")["offset"] == OFFSET_SECONDS["委屈"]

    source = (PLUGIN_DIR / "engine" / "performance_engine.py").read_text(encoding="utf-8")
    # 时长必须由 vts/timing.py 按台词计划，而不是查一个固定表就走
    assert "plan_for_text(" in source, "set_expression 没有按时台词计划三段时序"
    assert "min(hold, 8.0)" in source, "缺少总时长上限（防止又挂回十几秒）"
    assert "fade_in=timing.fade_in" in source, "表情淡入没有跟着 onset 走"
    print("OK 表情时长：真人区间 + 按台词计划 + 上限兜底")


if __name__ == "__main__":
    test_every_emotion_has_full_pose()
    test_body_bias_uses_head_channels()
    test_pose_lookup()
    test_decay_holds_then_releases()
    test_gaze_bias_present()
    test_engine_filters_after_emotion_blend()
    test_emotion_reaches_face_without_expression_file()
    test_au_faced_emotions_have_face_deltas()
    test_emotion_blink_and_gaze_style()
    test_emotion_rise_and_delay()
    test_emotion_duration_and_offset()
