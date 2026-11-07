# -*- coding: utf-8 -*-
"""情绪联动：让同一个情绪在「表情 / 面部参数 / 身体姿态」上同时体现。

为什么需要这一层：

  表情（.exp3.json）只是"贴"在脸上的图层，它不会带动眉毛的连续起伏，
  更不会让头歪一下、身体前倾。反过来，参数层也做不出黑脸这种离散表情。
  之前两者各做各的 —— 切了黑脸，头还在那儿匀速摇 —— 看着就像"脸和身体
  是两个人在演"。

  这一层就是把它们绑在一起：**一个情绪 = 一个表情 + 一组持续面部偏置
  + 一组持续身体姿态 + 一个入场小动作**。

三层各自负责什么：

  expression  离散表情文件（黑脸、爱心眼、＞＜），作者画的，参数层做不出来
  sustain     持续参数偏置（眉毛/嘴/眼睑/头角度），cortico sustain clip，
              带 noiseAmp 的会自己微微起伏，不会僵住
  入场动作    切换瞬间放一个 pulse clip（比如无语配 eyeroll + 歪头），
              让情绪是"发生的"，而不是"突然出现的"

身体姿态的落点很关键：实测这套模型的身/头比恒定 3.4:1（FaceAngleX 带动
身体最高效，FaceAngleY 几乎只动头），所以情绪的"身体感"主要靠 FaceAngleX/Z
和点头，而不是指望 Mocopi 通道。

面部配方的依据（按 AU 组合，而不是凭感觉调数值）：

  * Ekman & Friesen 的 FACS / EMFACS 给出每个情绪的原型动作单元组合：
      恐惧 AU1+2+4+5+20+25/26    惊讶 AU1+2+5+26
      愤怒 AU4+5+7+23            厌恶 AU9(+15/16)
      悲伤 AU1+4+15              快乐 AU6+12
    （见 Basic Emotion Theory 的 AU 表、iMotions 的 Duchenne smile 综述）
  * MPEG-4 FAP 的六种基本表情（ISO/IEC 14496）用同一套分区描述：
      恐惧=眉上扬并靠拢、内眉上弯、眼紧张警觉；惊讶=眉上扬、上眼睑全开、下颌张开；
      厌恶=上唇上提；快乐=嘴角向耳侧拉开；悲伤=内眉上弯、眼略闭、嘴放松。
  * ARKit 52 blendshape 到 AU 的映射（Aldenhoven et al., Sensors 2026, Table 1）：
      browDown=AU4、browInnerUp=AU1、browOuterUp=AU2、eyeWide=AU5、
      eyeSquint=AU7、mouthSmile=AU12、mouthFrown=AU15、
      mouthLowerDown=AU16、mouthStretch=AU20、mouthPucker=AU23、jawOpen=AU26。

  这台引擎注入的就是 ARKit 那套通道的低维投影，所以配方按 AU 优先级落地：
      有效（实测）            AU5 → EyeOpen，AU12/AU15 → MouthSmile（连带 AU6 笑眼）
      该模型无映射（保留）    AU1/2/4 → Brow 上下，AU26 → MouthOpen，AU9/16 → 无
  这样同一个表情在通道齐全的模型上是完整 AU 组合，在低维模型上自动退化成
  "嘴 + 眼 + 视线 + 头姿"，不会因为缺通道就整段失效。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

try:  # 正常运行时插件根在 sys.path 上
    from vts.timing import decay_weight
except ImportError:  # 单文件调试（cwd 就在 engine/）时退化成等价实现
    import math as _math

    def decay_weight(q: float, sharpness: float = 3.0) -> float:  # type: ignore[misc]
        q = max(0.0, min(float(q), 1.0))
        k = max(0.5, float(sharpness))
        tail = _math.exp(-k)
        return (_math.exp(-k * q) - tail) / max(1e-9, 1.0 - tail)

# 情绪 -> 联动配方
#   expression  跟它最搭的表情关键字（按 vts/expression.py 的匹配规则）
#   sustain     cortico 的 emotional sustain clip（持续参数偏置）
#   face        直接给输入空间的面部增量（优先于 sustain，可按 AU 组合精确配比）
#   intro       切换瞬间播一次的 pulse clip
#   gaze_bias   视线偏移（x, y），负 y 是往下看
#   gaze_hold   视线偏置保持多久
#   tremble     情绪自带的轻微抖动（紧张/害怕"绷不住"那点颤）
#   blink       情绪驱动的眨眼节奏：slow（慢眨/闭眼停一拍）/ fast（连眨）/ none（瞪眼不眨）
#   gaze_restless 视线扫视频率倍率：>1 眼神乱瞟（尴尬），<1 看定一处（害怕/轻蔑）
EMOTION_POSES: Dict[str, Dict[str, Any]] = {
    "无语": {
        # 无语 = 轻蔑/厌恶的低强度版（EMFACS：厌恶 AU9 + 嘴角下压 AU15）。
        # 这台模型没有皱鼻（AU9），所以"轻蔑"就落在嘴角下压 + 微眯眼 + 眼神飘。
        "expression": "无语",
        "sustain": "angry",          # 兜底：没有 face 字段时用这条 clip
        "sustain_intensity": 0.72,
        "face": {
            "MouthSmile": -0.45,     # AU15 嘴角下压（主力，实测 Param -0.68）
            "EyeOpenLeft": -0.12,    # 轻微眯眼，替代 AU7 的"看不上"
            "EyeOpenRight": -0.12,
            "BrowLeftY": -0.20,      # AU4 眉压低；该模型无映射，留给别的模型
            "BrowRightY": -0.20,
        },
        "intro": "eyeroll",
        "gaze_bias": (-0.55, 0.35),
        "gaze_hold": 6.0,
        "gaze_restless": 1.15,       # 眼神飘，但还没到"乱瞟"
        "blink": "slow",             # 慢眨 + 偶尔闭眼停一拍：无语最像人的地方
        # 身体：后仰 + 歪一下（身体后仰比单纯歪头更有"无语"的体感）
        "body": {"FaceAngleX": 3.5, "FaceAngleZ": 3.0},
    },
    "轻蔑": {
        # 轻蔑/不屑 = EMFACS 的 contempt：单侧嘴角上提（AU12/14）+ 半眯眼 + 抬下巴。
        # 低维模型只能整体提嘴角，所以用"冷笑 + 眯眼 + 抬下巴 + 视线往下压"来演。
        # 和"无语"正相反：无语是嘴角垮下去，轻蔑是嘴角挑起来。
        "expression": "轻蔑",
        "sustain": "chin_up",        # 抬下巴是轻蔑的核心体态
        "sustain_intensity": 0.8,
        "face": {
            "MouthSmile": 0.16,      # AU12 单侧上提的低维近似（冷笑）
            "EyeOpenLeft": -0.10,    # 半眯眼：看不上
            "EyeOpenRight": -0.10,
            "BrowLeftY": -0.12,      # AU4；该模型无映射
            "BrowRightY": -0.12,
        },
        "intro": "smug",
        "gaze_bias": (0.15, -0.40),  # 头抬高、视线从上往下看人
        "gaze_hold": 6.0,
        "gaze_restless": 0.65,       # 看定一处，不屑于到处看
        "blink": "slow",             # 慢条斯理地眨，轻蔑的"不把你放眼里"
        "body": {"FaceAngleY": 3.5, "FaceAngleZ": 1.5},
    },
    "尴尬": {
        # 尴尬 = 社交窘迫：视线躲、急速连眨、嘴角抿住、头微微低下。
        # 它和害怕的区别：不后缩得太狠、不发抖，脸也不要垮下来。
        "expression": "尴尬",
        "sustain": "fearful",
        "sustain_intensity": 0.25,   # 兜底用，有 face 时以 face 为准
        "face": {
            "MouthSmile": -0.10,     # AU23/AU20：嘴角抿紧（尴尬得笑不出来）
            "EyeOpenLeft": 0.02,     # 略睁，配下面的连眨
            "EyeOpenRight": 0.02,
            "BrowLeftY": 0.10,       # AU1+2 微抬；该模型无映射
            "BrowRightY": 0.10,
        },
        "intro": "scare_back",       # 往后一缩（"缩一下"的窘迫感）
        "gaze_bias": (0.45, -0.25),  # 视线躲开
        "gaze_hold": 5.0,
        "gaze_restless": 2.2,        # 眼神乱瞟：尴尬最直观的信号
        "blink": "fast",             # 急促连眨
        "body": {"FaceAngleY": -3.0, "FaceAngleX": -3.0},
    },
    "委屈": {
        "expression": "委屈",
        "sustain": "pitiful",
        "sustain_intensity": 0.9,
        "intro": "dejected",
        "gaze_bias": (0.0, -0.55),   # 低头
        "gaze_hold": 8.0,
        # 身体：整个塌下去，缩成一团
        "body": {"FaceAngleY": -4.0, "FaceAngleZ": -2.5},
    },
    "生气": {
        "expression": "生气",
        "sustain": "angry",
        "sustain_intensity": 1.0,
        "intro": "shake",            # 摇头
        "gaze_bias": (0.0, -0.1),
        "gaze_hold": 4.0,
        "body": {"FaceAngleY": -2.5, "FaceAngleX": 2.0},
    },
    "害羞": {
        "expression": "害羞",
        "sustain": "moved",          # 眼睑微垂 + 浅笑
        "sustain_intensity": 0.7,
        "intro": "tilt_curious",     # 歪头
        "gaze_bias": (0.6, -0.3),    # 视线躲开
        "gaze_hold": 5.0,
        "body": {"FaceAngleZ": 4.0, "FaceAngleY": -2.0},
    },
    "开心": {
        "expression": "开心",
        "sustain": "smile",
        "sustain_intensity": 0.8,
        "intro": "excited_bounce",
        "gaze_bias": (0.0, 0.15),    # 微微抬眼，精神
        "gaze_hold": 6.0,
        "body": {"FaceAngleY": 3.0, "FaceAngleX": 2.0},
    },
    "疑惑": {
        "expression": "疑惑",
        "sustain": "surprised",
        "sustain_intensity": 0.5,
        "intro": "tilt_curious",     # 歪头是疑惑最经典的信号
        "gaze_bias": (0.0, 0.35),
        "gaze_hold": 5.0,
        "body": {"FaceAngleZ": 5.0},
    },
    "惊讶": {
        # 惊讶 = AU1+2（眉上扬）+ AU5（上眼睑全开）+ AU26（下颌张开）。
        # 该模型眉毛/张嘴无映射，能看见的只有"瞪眼 + 嘴拉平 + 抬头"，
        # 所以这里把睁眼给到顶、嘴角收回中性（不要下压，那是悲伤）。
        "expression": "惊讶",
        "sustain": "surprised",
        "sustain_intensity": 0.95,
        "face": {
            "EyeOpenLeft": 0.15,     # AU5 睁到满（常态留了余量，这里刚好顶到 1.0）
            "EyeOpenRight": 0.15,
            "MouthSmile": 0.0,       # 嘴不动：惊讶不是笑也不是撇嘴
            "MouthOpen": 0.30,       # AU26；该模型无映射，留给别的模型
            "BrowLeftY": 0.45,       # AU1+2；该模型无映射
            "BrowRightY": 0.45,
        },
        "intro": "eyes_wide",
        "gaze_bias": (0.0, 0.2),
        "gaze_hold": 3.0,            # 惊讶来得快去得快
        "body": {"FaceAngleY": 3.5},
    },
    "得意": {
        "expression": "得意",
        "sustain": "chin_up",        # 抬下巴
        "sustain_intensity": 0.9,
        "intro": "smug",
        "gaze_bias": (0.2, 0.45),
        "gaze_hold": 6.0,
        # 身体：抬下巴 + 微微侧身，是"得意"的体态
        "body": {"FaceAngleY": 4.0, "FaceAngleX": 2.5, "FaceAngleZ": 2.0},
    },
    "紧张": {
        # 紧张 = 恐惧的低强度版（AU1+2+4+5+20，强度约 0.6）。
        # 与害怕的区别在"僵"：睁眼到 80%、嘴角抿平（不是垮）、视线乱飘、身体前绷。
        "expression": "紧张",
        "sustain": "fearful",
        "sustain_intensity": 0.7,
        "face": {
            "EyeOpenLeft": 0.09,     # AU5 睁大，但没到顶（0.85 → 0.94）
            "EyeOpenRight": 0.09,
            "MouthSmile": -0.20,     # AU20/AU23：嘴角拉紧、抿住（不是下垂）
            "BrowLeftY": 0.30,       # AU1+2；该模型无映射
            "BrowRightY": 0.30,
        },
        "intro": "freeze",           # 先僵一下，符合"心里一紧"
        "gaze_bias": (0.55, -0.2),    # 视线躲闪
        "gaze_hold": 4.0,
        "body": {"FaceAngleX": 2.0, "FaceAngleZ": 2.2},
        "tremble": 0.5,              # 轻微发抖：绷不住的感觉
        "gaze_restless": 1.6,        # 眼神飘忽
        "blink": "fast",             # 急促连眨
    },
    "害怕": {
        # 害怕 = 恐惧原型的满强度（AU1+2+4+5+20+25/26）。
        # 注意别做成"悲伤"：恐惧的嘴是横向拉紧、微张，不是嘴角垮到底。
        # 所以嘴角只收一点，把幅度全给"瞪眼 + 后缩 + 视线躲 + 抖"。
        "expression": "害怕",
        "sustain": "fearful",
        "sustain_intensity": 1.0,
        "face": {
            "EyeOpenLeft": 0.15,     # AU5 顶满
            "EyeOpenRight": 0.15,
            "MouthSmile": -0.04,     # 嘴基本平：恐惧不是哭
            "MouthOpen": 0.25,       # AU26；该模型无映射
            "BrowLeftY": 0.45,       # AU1+2；该模型无映射
            "BrowRightY": 0.45,
        },
        "intro": "scare_back",       # 往后一缩
        "gaze_bias": (0.7, -0.35),    # 视线彻底躲开、往下
        "gaze_hold": 5.0,
        # 身体：后仰 + 侧缩，和紧张的前倾正好相反
        "body": {"FaceAngleX": -3.5, "FaceAngleZ": -2.5},
        "tremble": 0.85,
        "gaze_restless": 0.6,        # 吓到定住：盯着一个方向不敢乱看
        "blink": "none",             # 瞪着眼几乎不眨（恐惧会抑制眨眼）
    },
}

# 表情自带的身体倾向：即使模型没有对应表情，身体也应该有反应
# （表情是模型给的，姿态是我们给的，两者解耦）
DEFAULT_SUSTAIN_SECONDS = 2.6


#: 表情"上场"时长（秒）：真人表情不是一帧到位，不同情绪的快慢也不一样。
#: 依据 Hoffmann et al. (2010) 的感知真实感数据——最真实的 onset 时长：
#: 悲伤 860~1120ms（最慢）、惊讶 500~530ms 与恐惧 480~650ms（最快），
#: 愤怒/厌恶/快乐 670~890ms。数值取区间中值并留一点余量。
RISE_SECONDS: Dict[str, float] = {
    "无语": 0.85, "轻蔑": 0.90, "尴尬": 0.75, "委屈": 1.05, "生气": 0.70,
    "害羞": 0.90, "开心": 0.80, "疑惑": 0.70, "惊讶": 0.50, "得意": 0.80,
    "紧张": 0.60, "害怕": 0.55,
}

#: 表情总时长（秒，含峰值与回落）。
#: 依据：Ekman & Friesen (1982) 真实微笑总时长 500~4000ms；自发社交微笑平均 3~4 秒
#: （Frank et al. 1993）；普通面部表情 0.5~6 秒（PMC6305322 的操作化区间）。
#: 摆拍表情的典型特征恰恰是"保持过久"——原来挂 12 秒就是标准的假笑。
HOLD_SECONDS: Dict[str, float] = {
    "无语": 2.6, "轻蔑": 2.2, "尴尬": 2.4, "委屈": 3.5, "生气": 2.4,
    "害羞": 3.0, "开心": 3.2, "疑惑": 2.2, "惊讶": 1.2, "得意": 2.6,
    "紧张": 2.4, "害怕": 2.6,
}

#: 回落（offset）时长（秒）：真人退场比上场慢，且刻意表情的 offset 更快/更突兀。
#: 这里按"自然表情"给，约等于 rise 的 1.1~1.4 倍。
OFFSET_SECONDS: Dict[str, float] = {
    "无语": 0.70, "轻蔑": 0.60, "尴尬": 0.60, "委屈": 0.90, "生气": 0.55,
    "害羞": 0.70, "开心": 0.70, "疑惑": 0.55, "惊讶": 0.35, "得意": 0.55,
    "紧张": 0.65, "害怕": 0.70,
}


def pose_for(emotion: str) -> Optional[Dict[str, Any]]:
    """取某个情绪的联动配方。

    Args:
        emotion: 情绪词。

    Returns:
        配方字典（带 rise / hold / offset 时序参数）；没有对应配方返回 None。
    """

    key = str(emotion or "").strip()
    pose = EMOTION_POSES.get(key)
    if pose is None:
        return None
    # 补上时序参数，不改原表（原始配方只描述"演什么"，时序单独几张表）
    filled = dict(pose)
    filled.setdefault("rise", RISE_SECONDS.get(key, 0.6))
    filled.setdefault("hold", HOLD_SECONDS.get(key, 3.0))
    filled.setdefault("offset", OFFSET_SECONDS.get(key, 0.6))
    return filled


def pose_names() -> List[str]:
    """所有有联动配方的情绪词。"""

    return sorted(EMOTION_POSES)


def decayed(value: float, age: float, seconds: float, release: float = 0.0) -> float:
    """姿态偏置随时间的衰减（线性到 0），用于让情绪自然消退而不是硬切。

    Args:
        value: 初始值。
        age: 已经过去多少秒。
        seconds: 总保持时长。
        release: 回落时长（秒）；0 表示按总时长的 30%。

    Returns:
        float: 衰减后的值。
    """

    if seconds <= 0 or age >= seconds:
        return 0.0
    hold = seconds - (release if release > 0 else seconds * 0.3)
    if age <= hold:
        return value
    q = (age - hold) / max(1e-6, seconds - hold)
    # 指数衰退（Kuppens, Oravecz & Tuerlinckx 2010 的 DynAffect / OU 过程）：
    # 刚离开峰值时掉得快、尾巴拖得长。线性或 smoothstep 会在最后一段还硬撑着，
    # 观感像"表情被人一把抹掉"，指数衰减更像情绪自己散掉。
    return value * decay_weight(q)