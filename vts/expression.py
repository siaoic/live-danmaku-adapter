# -*- coding: utf-8 -*-
"""VTS 表情层：把「情绪」标定到当前模型**真实存在**的表情上。

为什么重写
----------
旧实现是一张「情绪词 → 中文表情名关键字」的硬表（无语→黑脸、委屈→流泪…）。
它只对作者用中文起名的模型成立：日文皮套（照れ／困り／怒り）、英文皮套
（Blush / Angry / Teary）、或者干脆叫 ``exp01.exp3.json`` 的模型，全部匹配不上，
最后表现为「AI 情绪很丰富，脸一动不动」。

现在的做法是**标定（calibration）而不是查表**。每个模型启动时，从 VTS 真实返回的
表情清单出发，用三层证据给每个表情打分，得出「这个表情是什么情绪、有多确定」：

1. **表情文件参数（最强证据）**
   ``.exp3.json`` 的 ``Parameters`` 直接写明它把哪些 Cubism **标准参数**改成了多少。
   标准参数的语义是官方固定下来的（``ParamMouthForm`` +是笑 −是怒、
   ``ParamBrowLForm`` −是生气、``ParamEyeLSmile`` +是笑眼、``ParamCheek`` +是脸红），
   于是可以先把参数增量翻译成 FACS 的 **AU（Action Unit）向量**，再和 EMFACS
   的 AU 组合原型做余弦相似度分类。这条路径与表情叫什么叫什么语言无关，
   连 ``exp03.exp3.json`` 也能标定出来。

   依据：
   * Ekman & Friesen, *Facial Action Coding System*（1978；2002 修订）——
     AU 是面部肌肉的解剖学编码，AU 组合与情绪的对应是跨文化的；
   * Friesen & Ekman 的 **EMFACS** 精简版给出了六种基本情绪的原型 AU 组合
     （anger 4+5/4+7/4+5+7、disgust 9/10、fear 1+2+4/20、happiness 12/6+12、
     sadness 1/1+4/15、surprise 1+2+5/1+2+26）；
   * *Real-Time Emotion Recognition … Using Apple's ARKit*, Sensors 26(3):1060 (2026)
     —— 用 blendshape 向量与情绪原型做**余弦相似度**即可得到有竞争力的分类，
     不需要训练；本文档的参数→AU→余弦就是这条路径的 Live2D 版本；
   * Live2D 官方《标准参数列表》—— 每个标准参数的取值范围与语义。

2. **名字语义（兜底）**
   中／日／英三语词表 + 颜文字（``＞＜``／``TT``／``QAQ``／``orz``），
   按命中词长度加权。同名同义（黑脸=deadpan、照れ=shy、Blush=shy）会给出同一标签。
   只在**读不到文件**时生效（PNGTuber、模型目录不在默认路径、热键没有 ``file``）：
   再全的词表也只是赌作者的命名习惯，不能当主证据。

3. **结构排除法（负证据）**
   热键类型与命名结构决定「能不能参与情绪轮换」：体型／衣服／部件开关／
   开合成对的装饰件，一律不进情绪池，避免「AI 说无语却换了条裙子」。

文件解析（``.exp3.json`` / ``.motion3.json`` / 素材）统一在 :mod:`vts.assetfile`，
道具热键的分类（玩具/一次性动作/换装/特效）也走同一套事实，见 :mod:`vts.props`。

三层都没有证据的表情标为 ``unknown``：**不参与情绪轮换、也不瞎给脸**，
只在日志里提示可以在 ``vts/emotion_assets.json`` 里手动映射。

契约（改这里要同步 engine/performance_engine.py）
------------------------------------------------
* 对外情绪词仍是 ``engine/emotion_pose.py`` 认识的中文标签（无语/委屈/害羞…），
  由 :data:`EMOTIONS` 统一映射到内部 canonical key；
* :class:`ExpressionCatalog` 保留 ``emotion_names`` / ``pairs`` / ``file_of`` /
  ``toggle_id`` 等旧属性，新增 ``match()`` 与 ``assets``；
* VTS 只认 ``{"expressionFile": "xxx.exp3.json", "active": bool}`` 单条下发，
  传 activations 数组会报 APIError 650（实测）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import json
import logging
import math
import re
import time
from pathlib import Path

from .assetfile import (
    KIND_MEDIA,
    KIND_MODEL,
    AssetInfo,
    FileIndex,
    default_model_roots,
    parse_expression_params,
)

logger = logging.getLogger("plugin.live_stream_companion.expression")

# 表情默认保持时长（秒）。真实表情总时长约 0.5~4 秒（Ekman & Friesen 1982），
# 挂十几秒会被看成摆拍；引擎会按情绪用更精确的时长覆盖这个兜底值。
DEFAULT_HOLD_SECONDS = 3.2

HOTKEY_TOGGLE = "ToggleExpression"      # 互斥：一次一个
HOTKEY_ANIM = "TriggerAnimation"        # 开/关配对
HOTKEY_REMOVE_ALL = "RemoveAllExpressions"

# 以「开/关」结尾、成对出现的表情是部件开关，不是情绪（如「竖食指开 / 竖食指关」）
PAIR_SUFFIXES: Tuple[str, ...] = ("开", "关", "On", "Off", "on", "off")

# ---------------------------------------------------------------------- #
# 1. 情绪本体：canonical key（英文） -> 对外中文标签
#
# 中文标签沿用 engine/emotion_pose.py 的键，参数姿态库按它查配方，
# 所以这里只能加别名，不能改名。
# ---------------------------------------------------------------------- #
EMOTIONS: Dict[str, str] = {
    "deadpan": "无语",
    "contempt": "轻蔑",
    "awkward": "尴尬",
    "sad": "委屈",
    "cry": "哭",
    "angry": "生气",
    "fear": "害怕",
    "tense": "紧张",
    "surprise": "惊讶",
    "confused": "疑惑",
    "shy": "害羞",
    "happy": "开心",
    "smug": "得意",
}

#: 外部词 -> canonical key。中文、英文、常见同义词都收，多个词指向同一情绪时
#: 表情匹配结果一致，模型换语言也不会换一套逻辑。
EMOTION_ALIASES: Dict[str, str] = {
    # 无语/黑脸
    "无语": "deadpan", "黑脸": "deadpan", "面无表情": "deadpan", "呆滞": "deadpan",
    "deadpan": "deadpan", "blank": "deadpan", "speechless": "deadpan",
    # 轻蔑
    "轻蔑": "contempt", "不屑": "contempt", "嫌弃": "contempt", "鄙视": "contempt",
    "contempt": "contempt", "disgust": "contempt", "smug_eye": "contempt",
    # 尴尬/冷汗
    "尴尬": "awkward", "冷汗": "awkward", "流汗": "awkward", "汗": "awkward",
    "awkward": "awkward", "sweat": "awkward",
    # 委屈/难过
    "委屈": "sad", "难过": "sad", "伤心": "sad", "低落": "sad", "悲": "sad",
    "sad": "sad", "sadness": "sad",
    # 哭
    "哭": "cry", "流泪": "cry", "泪": "cry", "cry": "cry", "tear": "cry", "teary": "cry",
    # 生气
    "生气": "angry", "愤怒": "angry", "怒": "angry",
    "angry": "angry", "anger": "angry",
    # 害怕
    "害怕": "fear", "惊恐": "fear", "恐惧": "fear", "fear": "fear", "scared": "fear",
    # 紧张
    "紧张": "tense", "tense": "tense", "nervous": "tense",
    # 惊讶
    "惊讶": "surprise", "震惊": "surprise", "吃惊": "surprise",
    "surprise": "surprise", "surprised": "surprise", "shock": "surprise",
    # 疑惑
    "疑惑": "confused", "困惑": "confused", "晕": "confused", "问号": "confused",
    "confused": "confused", "puzzled": "confused", "dizzy": "confused",
    # 害羞
    "害羞": "shy", "脸红": "shy", "羞": "shy", "blush": "shy",
    "shy": "shy", "embarrassed": "shy",
    # 开心
    "开心": "happy", "高兴": "happy", "笑": "happy", "爱心眼": "happy", "喜欢": "happy",
    "happy": "happy", "joy": "happy", "smile": "happy", "love": "happy",
    # 得意
    "得意": "smug", "坏笑": "smug", "屑": "smug", "得瑟": "smug",
    "smug": "smug",
    # 日文（日式皮套最常见的命名）
    "笑い": "happy", "照れ": "shy", "怒り": "angry", "泣き": "cry", "悲しみ": "sad",
    "驚き": "surprise", "困り": "confused", "照れ笑い": "shy", "ジト目": "deadpan",
    "あせり": "awkward", "ドヤ": "smug",
}


def canonical_emotion(label: str) -> str:
    """把任意情绪词（中文/英文/日文/同义词）归一到 canonical key。

    Args:
        label: 情绪词，如「无语」「deadpan」「照れ」。

    Returns:
        canonical key（见 :data:`EMOTIONS`）；无法识别返回空字符串。
    """

    text = str(label or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in EMOTION_ALIASES:
        return EMOTION_ALIASES[lowered]
    if text in EMOTION_ALIASES:
        return EMOTION_ALIASES[text]
    # 中文标签本身也可能直接就是 canonical（"无语" 已在别名表里）
    if lowered in EMOTIONS:
        return lowered
    return ""


def emotion_label(canonical: str) -> str:
    """canonical key -> 对外中文标签（engine/emotion_pose 认的键）。"""

    return EMOTIONS.get(str(canonical or ""), "")


def emotion_synonyms(label: str, limit: int = 4) -> List[str]:
    """某个情绪的全部中文叫法，供台词记号使用。

    为什么需要：模型标定出的表情名可能是「流泪」，AI 想哭的时候更可能写
    ``【哭】``、``【难过】``、``【伤心】``。记号解析只认同义词表里的词，
    不在这里列出来，``【哭】`` 就会被当成普通文字摘掉，什么也不会发生。
    所以同一个情绪的中文别名要一起给到记号层。

    Args:
        label: 任意情绪词（表情名、中文标签或 canonical key）。
        limit: 最多返回几个，避免提示词被同义词撑爆。

    Returns:
        中文别名列表（标签本身排第一）；识别不出返回空列表。
    """

    key = canonical_emotion(label)
    if not key:
        return []
    primary = emotion_label(key)
    words: List[str] = [primary] if primary else []
    for word, mapped in EMOTION_ALIASES.items():
        if mapped != key or word in words:
            continue
        # 只收纯中文：提示词是中文的，英文别名对模型没帮助，日文（泣き/照れ）
        # 混在括号里也只是噪音，还会让模型以为要写日语。
        if not re.search(r"[\u4e00-\u9fff]", word):
            continue
        if re.search(r"[\u3040-\u30ff]", word):
            continue
        words.append(word)
    return words[: max(1, int(limit))]


# ---------------------------------------------------------------------- #
# 2. FACS：参数增量 -> AU 向量 -> 情绪原型
#
# 只处理「会改变面部表情」的标准参数；角度/身体/视线/手臂/头发这些是姿态层
# （engine/emotion_pose.py）负责的，混进来只会稀释相似度。
# ---------------------------------------------------------------------- #
#: 除标准参数外，作者自定义参数里常见的「情绪符号」，按名字给伪 AU。
#: 这些不是猜测情绪，而是识别「脸上多了一个符号层」（阴影/泪/汗/怒符/晕圈）。
PARAM_SYMBOL_HINTS: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("tear", "namida", "涙", "泪", "泣"), "TEAR"),
    (("blush", "cheek", "照れ", "赤面", "脸红", "红晕"), "BLUSH"),
    (("sweat", "汗", "あせ"), "SWEAT"),
    (("shadow", "黑脸", "青ざめ", "阴影", "dark"), "SHADOW"),
    (("anger", "怒", "vein", "十字", "怒りマーク"), "ANGER_MARK"),
    (("swirl", "guru", "圈", "ぐるぐる", "dizzy"), "SWIRL"),
    (("sparkle", "kira", "キラ", "star", "星"), "SPARKLE"),
)

#: EMFACS 原型 + 日式动画里高频的四种「非基本情绪」。
#:
#: 基本六情绪取自 EMFACS 的常见 AU 组合；deadpan/awkward/confused/smug 是
#: 二次元皮套的惯用脸（黑脸/流冷汗/圈圈眼/屑笑），没有直接论文，用同一套
#: AU 语汇描述，权重按「这张脸的标志性动作」给。
EMOTION_PROTOTYPES: Dict[str, Dict[str, float]] = {
    # happiness: AU6+AU12（Duchenne 微笑）
    "happy": {"AU6": 1.0, "AU12": 1.0, "AU25": 0.2},
    # sadness: AU1+AU4+AU15
    "sad": {"AU1": 0.9, "AU4": 0.7, "AU15": 1.0},
    # anger: AU4+AU5+AU7+AU23
    "angry": {"AU4": 1.0, "AU5": 0.5, "AU7": 0.7, "AU23": 0.6, "ANGER_MARK": 1.0},
    # surprise: AU1+AU2+AU5+AU26
    "surprise": {"AU1": 1.0, "AU2": 0.9, "AU5": 0.9, "AU26": 0.8},
    # fear: AU1+AU2+AU4+AU5+AU20
    "fear": {"AU1": 0.9, "AU2": 0.6, "AU4": 0.6, "AU5": 0.9, "AU20": 0.7, "SWEAT": 0.5},
    # disgust/contempt: AU9+AU10+AU14（单侧嘴角收紧）
    "contempt": {"AU9": 0.8, "AU10": 0.6, "AU14": 1.0, "AU15": 0.4},
    # 无语/黑脸：压眉 + 眯眼 + 紧唇 + 阴影（AU24 唇部按压，FACS 里就是「无语」那种抿嘴）
    "deadpan": {"AU4": 0.7, "AU7": 0.8, "AU24": 1.0, "SHADOW": 1.0},
    # 尴尬：冷汗 + 眯眼 + 嘴角下拉
    "awkward": {"SWEAT": 1.0, "AU7": 0.6, "AU15": 0.5, "AU4": 0.3},
    # 疑惑：单侧压眉（AU1+AU4 不对称）+ 圈圈眼
    "confused": {"AU1": 0.5, "AU4": 0.8, "SWIRL": 1.0},
    # 害羞：脸红 + 笑眼 + 抿嘴
    "shy": {"BLUSH": 1.0, "AU6": 0.5, "AU12": 0.5, "AU24": 0.3},
    # 得意：单侧嘴角（AU14）+ 挑眉
    "smug": {"AU14": 1.0, "AU12": 0.5, "AU4": 0.3, "SPARKLE": 0.3},
    # 哭：泪 + AU1+AU4+AU15
    "cry": {"TEAR": 1.0, "AU1": 0.8, "AU4": 0.6, "AU15": 0.8},
    # 紧张：压眉 + 眯眼 + 冷汗（绷不住那种）
    "tense": {"AU4": 0.8, "AU7": 0.9, "SWEAT": 0.7, "AU20": 0.4},
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def face_units_from_params(params: Dict[str, float]) -> Dict[str, float]:
    """把一组 Cubism 参数增量翻译成 FACS AU 向量。

    Args:
        params: 参数 ID -> 该表情把参数改成的值（不是增量；标准参数有固定默认值）。

    Returns:
        AU/伪AU -> 激活强度（0~1）。没有面部含义的参数（角度、身体、视线）忽略。
    """

    units: Dict[str, float] = {}

    def add(name: str, value: float) -> None:
        if value <= 0:
            return
        units[name] = max(units.get(name, 0.0), _clamp(value))

    for raw_id, raw_value in (params or {}).items():
        pid = str(raw_id or "").strip()
        if not pid:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        key = pid.lower()

        # --- 眉毛：+ 是抬起（AU1/AU2），− 是压低/聚拢（AU4）
        if key in ("parambrowly", "parambrowry"):
            if value > 0:
                add("AU1", value)
                add("AU2", value * 0.6)
            elif value < 0:
                add("AU4", -value * 0.9)
            continue
        if key in ("parambrowlform", "parambrowrform"):
            if value < 0:
                add("AU4", -value)
            continue
        if key in ("parambrowlangle", "parambrowrangle",
                   "parambrowlx", "parambrowrx"):
            if value < 0:
                add("AU4", -value * 0.8)
            continue

        # --- 眼：笑眼 = AU6；睁大 = AU5；眯/闭 = AU7
        if key in ("parameyelsmile", "parameyersmile"):
            add("AU6", value)
            continue
        if key in ("parameyelopen", "parameyeropen"):
            delta = value - 1.0          # 标准默认 1.0 = 正常睁开
            if delta > 0.05:
                add("AU5", delta * 1.2)
            elif delta < -0.05:
                add("AU7", -delta)
            continue
        # --- 嘴：+ 笑（AU12），− 垮（AU15）；张开 = AU25/AU26；轻微压下 = 抿嘴（AU24）
        if key == "parammouthform":
            if value > 0.05:
                add("AU12", value)
            elif value < -0.05:
                add("AU15", -value)
                add("AU24", -value * 0.6)
            continue
        if key == "parammouthopeny":
            add("AU25", value * 0.6)
            add("AU26", value * 0.9)
            continue
        # --- 脸颊：泛红（AU6 也会被带起来一点）
        if key == "paramcheek":
            add("BLUSH", value)
            add("AU6", value * 0.3)
            continue

        # --- 非标准参数：按名字认「脸上的符号层」
        for tokens, symbol in PARAM_SYMBOL_HINTS:
            if any(token in key for token in tokens):
                add(symbol, abs(value) if value else 1.0)
                break

    return units


def _cosine(units: Dict[str, float], proto: Dict[str, float]) -> float:
    """两个稀疏向量的余弦相似度（任一维为负按 0 处理，AU 只有激活没有负激活）。"""

    if not units or not proto:
        return 0.0
    dot = 0.0
    for key, weight in proto.items():
        dot += max(0.0, units.get(key, 0.0)) * weight
    if dot <= 0:
        return 0.0
    norm_u = math.sqrt(sum(max(0.0, v) ** 2 for v in units.values()))
    norm_p = math.sqrt(sum(w * w for w in proto.values()))
    if norm_u <= 0 or norm_p <= 0:
        return 0.0
    return dot / (norm_u * norm_p)


def score_face_units(units: Dict[str, float]) -> Dict[str, float]:
    """AU 向量 -> 每个情绪的相似度得分（0~1）。"""

    if not units:
        return {}
    return {key: _cosine(units, proto) for key, proto in EMOTION_PROTOTYPES.items()}


# ---------------------------------------------------------------------- #
# 3. 名字语义：中日英 + 颜文字
#
# 同一张脸在不同作者手里叫法完全不同，所以词表按「同一情绪的多语言说法」组织；
# 匹配时长词优先（「流冷汗」先于「汗」，「屑屑眼」先于「屑」）。
# ---------------------------------------------------------------------- #
NAME_LEXICON: Dict[str, Dict[str, float]] = {
    # ---- 无语 / 黑脸
    "黑脸": {"deadpan": 1.0},
    "无语": {"deadpan": 1.0},
    "阴沉": {"deadpan": 0.8, "angry": 0.3},
    "黑线": {"deadpan": 0.7, "awkward": 0.4},
    "汗颜": {"awkward": 0.9, "deadpan": 0.3},
    "阴影": {"deadpan": 0.8},
    "ジト目": {"deadpan": 1.0},
    "无表情": {"deadpan": 0.9},
    "deadpan": {"deadpan": 1.0},
    "blank": {"deadpan": 0.8},
    "＞＜": {"deadpan": 0.5, "sad": 0.4},
    "><": {"deadpan": 0.5, "sad": 0.4},
    # ---- 尴尬 / 冷汗
    "流冷汗": {"awkward": 1.0, "tense": 0.3},
    "冷汗": {"awkward": 0.9},
    "尴尬": {"awkward": 1.0},
    "汗": {"awkward": 0.7},
    "sweat": {"awkward": 0.9},
    "あせ": {"awkward": 0.9},
    # ---- 委屈 / 难过 / 哭
    "流泪": {"sad": 1.0, "cry": 0.9},
    "哭": {"cry": 1.0, "sad": 0.7},
    "泪": {"cry": 0.8, "sad": 0.6},
    "难过": {"sad": 1.0},
    "委屈": {"sad": 1.0},
    "泣": {"cry": 1.0, "sad": 0.6},
    "tear": {"cry": 0.9, "sad": 0.5},
    "cry": {"cry": 1.0},
    "sad": {"sad": 1.0},
    # ---- 生气
    "怒": {"angry": 1.0},
    "生气": {"angry": 1.0},
    "angry": {"angry": 1.0},
    "anger": {"angry": 1.0},
    "怒り": {"angry": 1.0},
    # ---- 害羞 / 脸红
    "脸红": {"shy": 1.0},
    "羞": {"shy": 0.9},
    "红脸": {"shy": 0.9},
    "照れ": {"shy": 1.0},
    "blush": {"shy": 1.0},
    "embarrass": {"shy": 0.8},
    # ---- 开心
    "爱心眼": {"happy": 1.0},
    "笑眼": {"happy": 0.9},
    "开心": {"happy": 1.0},
    "高兴": {"happy": 1.0},
    "微笑": {"happy": 0.8},
    "笑": {"happy": 0.7},
    "happy": {"happy": 1.0},
    "smile": {"happy": 0.9},
    "joy": {"happy": 1.0},
    # ---- 惊讶 / 疑惑
    "圈圈眼": {"confused": 1.0},
    "问号": {"confused": 1.0},
    "疑惑": {"confused": 1.0},
    "困惑": {"confused": 0.8},
    "晕": {"confused": 0.7},
    "confus": {"confused": 0.9},
    "puzzle": {"confused": 0.9},
    "惊": {"surprise": 0.8},
    "吃": {"surprise": 0.5},
    "surprise": {"surprise": 1.0},
    "shock": {"surprise": 0.9},
    # ---- 得意 / 轻蔑
    "屑屑眼": {"smug": 1.0},
    "屑": {"smug": 0.8},
    "得意": {"smug": 1.0},
    "坏笑": {"smug": 0.9},
    "ドヤ": {"smug": 1.0},
    "smug": {"smug": 1.0},
    "轻蔑": {"contempt": 1.0},
    "嫌弃": {"contempt": 0.9},
    "不屑": {"contempt": 0.9},
}

#: 明确是「换装 / 部件 / 展示」的词。命中即不参与情绪轮换。
DISPLAY_KEYWORDS: Tuple[str, ...] = (
    "体型", "表情", "衣服", "胸口衣服", "裙", "裙子", "眼镜", "兔牙",
    "单片眼镜", "钢盆", "加载", "开关", "切换", "打开", "关闭", "前倾", "脱鞋",
    "outfit", "hair", "body", "size", "toggle",
)


def _normalize(text: str) -> str:
    """名字归一化：小写、去空格与常见分隔符。"""

    return str(text or "").strip().lower().replace(" ", "").replace("　", "")


def name_evidence(name: str) -> Dict[str, float]:
    """从名字里读出情绪证据。

    Args:
        name: 表情名（可能是中文/日文/英文/颜文字）。

    Returns:
        情绪 key -> 得分（0~1，取该情绪命中的最强词）。
    """

    body = _normalize(name)
    if not body:
        return {}
    scores: Dict[str, float] = {}
    # 长词优先：「流冷汗」要先于「汗」命中，「屑屑眼」先于「屑」
    for token in sorted(NAME_LEXICON, key=len, reverse=True):
        needle = _normalize(token)
        if not needle or needle not in body:
            continue
        for emotion, weight in NAME_LEXICON[token].items():
            if not emotion:                    # 空 key 表示「这是装饰，不是情绪」
                continue
            if weight > scores.get(emotion, 0.0):
                scores[emotion] = weight
    return scores


# ---------------------------------------------------------------------- #
# 4. 表情文件：定位并解析 .exp3.json
# ---------------------------------------------------------------------- #
def parse_expression_document(doc: Any) -> Dict[str, float]:
    """解析一个 .exp3.json 文档，返回 {参数ID: 目标值}。

    只取 Add / Override 的绝对值；Multiply 语义（缩放）对表情标定没有额外信息量。
    实现委托给 :mod:`vts.assetfile`，两边共用同一套解析，避免格式漂移。
    """

    return parse_expression_params(doc)


def default_expression_roots() -> List[Path]:
    """VTS 可能存放模型的目录（找不到就返回空列表，安静降级）。

    可用环境变量 ``VTS_MODEL_DIRS`` 手工追加（多个目录用 ``os.pathsep`` 分隔），
    适合 VTS 装在不常见盘符、或者模型被软链到别处的机器。
    """

    return default_model_roots()


def read_expression_parameters(
    files: Dict[str, str],
    model_name: str = "",
    roots: Optional[Sequence[Path]] = None,
    limit: int = 400,
) -> Dict[str, Dict[str, float]]:
    """按「表情名 -> 文件名」去磁盘上找 .exp3.json 并解析出参数。

    找不到模型目录（VTS 装在别的机器/别的盘、或模型不在默认路径）时返回空字典，
    调用方会退回名字语义，不影响功能。

    Args:
        files: 表情名 -> 文件名（VTS ExpressionStateRequest 返回的 file）。
        model_name: 当前模型名，优先在 ``Live2DModels/<model_name>/`` 下精确找。
        roots: 模型根目录列表，默认用 :func:`default_expression_roots`。
        limit: 最多索引多少个文件，防止在超大素材目录里卡住启动。

    Returns:
        文件名 -> 参数表。
    """

    wanted = {str(v).strip(): k for k, v in (files or {}).items() if str(v or "").strip()}
    if not wanted:
        return {}
    index = FileIndex(model_name=model_name, roots=roots, limit=limit)
    out: Dict[str, Dict[str, float]] = {}
    for name in wanted:
        info = index.analyze(name)
        if info is not None and info.params:
            out[name] = dict(info.params)
    return out


def read_asset_infos(
    files: Iterable[str],
    model_name: str = "",
    index: Optional[FileIndex] = None,
) -> Dict[str, AssetInfo]:
    """批量把文件名解析成 :class:`AssetInfo`（拿不到文件的就不出现在结果里）。"""

    names = [str(n).strip() for n in files if str(n or "").strip()]
    if not names:
        return {}
    idx = index if index is not None else FileIndex(model_name=model_name)
    return idx.analyze_many(names)


# ---------------------------------------------------------------------- #
# 5. 资产标定
# ---------------------------------------------------------------------- #
#: 语义相邻的情绪：标定只给出一个 key，但同一张脸可以被相邻情绪借用。
#: 例：带泪的脸色既是「哭」也是「委屈」，模型只标了 cry 时，委屈也该能用它；
#: 否则遇到「文件在流泪、名字叫 exp03」的模型，sad 会白白落空。
SIBLING_EMOTIONS: Dict[str, Tuple[str, ...]] = {
    "sad": ("cry",),
    "cry": ("sad",),
    "tense": ("fear",),
    "fear": ("tense",),
}

#: 情绪标签的接受阈值：证据不足就不贴标签，宁可走参数层也不瞎给一张脸。
LABEL_MIN_SCORE = 0.55
LABEL_MIN_MARGIN = 0.08


@dataclass
class ExpressionAsset:
    """一个表情文件的标定结果。"""

    name: str
    file: str = ""
    kind: str = "unknown"          # emotion / unknown / display
    label: str = ""                # canonical key（kind == emotion 时有效）
    confidence: float = 0.0
    reason: str = ""               # 人看的证据摘要
    units: Dict[str, float] = field(default_factory=dict)

    @property
    def emotion(self) -> str:
        """对外中文情绪词（没有标签返回空串）。"""

        return emotion_label(self.label)

    def describe(self) -> str:
        tag = f"{self.emotion or '未标注'} {self.confidence:.2f}"
        if self.kind == "display":
            return f"{self.name} → 展示型（{self.reason or '换装/部件'}）"
        return f"{self.name} → {tag}" + (f"（{self.reason}）" if self.reason else "")


def _is_display_name(name: str, siblings: Sequence[str]) -> bool:
    """换装/部件/成对开关不是情绪，不能参与轮换。"""

    if any(keyword in name for keyword in DISPLAY_KEYWORDS):
        return True
    if name.endswith(PAIR_SUFFIXES) and len(name) > 2:
        stem = name[:-1]
        if any(other != name and other.startswith(stem) for other in siblings):
            return True
    return False


def _evidence_reason(units: Dict[str, float], face: Dict[str, float], name: Dict[str, float]) -> str:
    """把证据拼成一句日志，出问题时一眼能看出是哪一层判的。"""

    parts: List[str] = []
    if units:
        top = sorted(units.items(), key=lambda item: item[1], reverse=True)[:3]
        parts.append("AU " + "/".join(f"{key}={value:.2f}" for key, value in top))
    if face:
        best = max(face.items(), key=lambda item: item[1])
        parts.append(f"参数层 {best[0]}={best[1]:.2f}")
    if name:
        best = max(name.items(), key=lambda item: item[1])
        parts.append(f"名字层 {best[0]}={best[1]:.2f}")
    return "，".join(parts)


def _combine(face: Dict[str, float], naming: Dict[str, float]) -> Dict[str, float]:
    """融合两层证据。

    参数层是客观几何事实（表情文件到底改了哪些参数），权重给高；名字层是
    作者的命名习惯，权重给低但足够在「AU 无法区分黑脸和生气」时打破平局。
    单层缺失时另一层直接占满，不会因为缺一张文件就整体失效。
    """

    if face and naming:
        keys = set(face) | set(naming)
        return {key: 0.72 * face.get(key, 0.0) + 0.28 * naming.get(key, 0.0) for key in keys}
    if face:
        return dict(face)
    return dict(naming)


def calibrate_asset(
    name: str,
    *,
    file: str = "",
    params: Optional[Dict[str, float]] = None,
    info: Optional[AssetInfo] = None,
    siblings: Sequence[str] = (),
    display_override: bool = False,
) -> ExpressionAsset:
    """给单个表情定标签。

    判定顺序是「文件内容 > 名字」：

    1. 文件是图片/视频/模型 → 展示型（换装/特效）；
    2. 文件只改身体/头发/位置（``ParamBodyAngle``/``ParamArm*``/``ParamHair*``）→
       展示型。它可能叫「表情3」，但本质上是在换姿势，进情绪轮换会变成
       「AI 说无语却换了条裙子」；
    3. 文件确实改了脸 → 用参数标定情绪，**名字里的「衣服/眼镜」不再一票否决**；
    4. 没有文件（PNGTuber / 模型目录不可见）→ 才退回名字语义。

    Args:
        name: 表情名。
        file: 文件名（.exp3.json）。
        params: 参数表；没有就是 None。
        info: 该文件的解析结果（含部位与类型）。
        siblings: 同一模型的其他表情名（判断是不是成对开关用）。
        display_override: 热键层判定为展示型（仅在无文件证据时生效）。

    Returns:
        ExpressionAsset。
    """

    asset = ExpressionAsset(name=name, file=file or f"{name}.exp3.json")
    if info is not None and info.kind in (KIND_MEDIA, KIND_MODEL):
        asset.kind = "display"
        asset.reason = info.describe()
        return asset
    if info is not None and info.only_body:
        asset.kind = "display"
        asset.reason = "文件只动 %s（换装/摆姿势）" % "/".join(sorted(info.groups))
        return asset
    if info is None or not info.touches_face:
        if display_override or _is_display_name(name, list(siblings)):
            asset.kind = "display"
            asset.reason = "换装/部件/成对开关（按命名判断）"
            return asset

    if params is None and info is not None and info.params:
        params = info.params
    units = face_units_from_params(params or {})
    face = score_face_units(units)
    naming = name_evidence(name)
    combined = _combine(face, naming)
    asset.units = units
    asset.reason = _evidence_reason(units, face, naming)
    if not combined:
        asset.kind = "unknown"
        return asset

    ranked = sorted(combined.items(), key=lambda item: item[1], reverse=True)
    best_key, best_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_score >= LABEL_MIN_SCORE and (best_score - second) >= LABEL_MIN_MARGIN:
        asset.kind = "emotion"
        asset.label = best_key
        asset.confidence = round(best_score, 3)
    else:
        asset.kind = "unknown"
        asset.confidence = round(best_score, 3)
        if len(ranked) > 1:
            asset.reason += f"（top2 差距过小：{best_key}/{ranked[1][0]}）"
    return asset


# ---------------------------------------------------------------------- #
# 6. 清单
# ---------------------------------------------------------------------- #
def expression_aliases(asset: "ExpressionAsset", limit: int = 6) -> List[str]:
    """一个已标定表情在台词里可以被叫成什么。

    「流泪」会得到 ``委屈/难过/伤心/哭/泪``：同一张脸既是"委屈"也是"哭"，
    模型写哪个词都该落到它身上。相邻情绪（sad↔cry）一并收，否则标成
    sad 的泪脸会听不懂 ``【哭】``。

    Args:
        asset: 已标定的表情资产。
        limit: 最多几个别名（提示词长度要克制）。

    Returns:
        中文别名列表；未标定返回空列表。
    """

    key = canonical_emotion(asset.label or asset.emotion)
    if not key:
        return []
    words: List[str] = []
    for candidate in (key,) + tuple(SIBLING_EMOTIONS.get(key, ())):
        for word in emotion_synonyms(candidate, limit=4):
            if word not in words:
                words.append(word)
    return words[: max(1, int(limit))]


class ExpressionCatalog:
    """当前模型的表情资产清单（标定结果 + 热键 + 动画配对）。

    引擎启动/换皮时用 VTS 真实返回的数据建一份，所有匹配都查它，
    所以换模型只要重启引擎（或等换皮检测）就自动适配。
    """

    def __init__(
        self,
        *,
        model_name: str = "",
        expressions: Optional[List[str]] = None,
        hotkeys: Optional[List[Dict[str, Any]]] = None,
        expression_files: Optional[Dict[str, str]] = None,
        expression_params: Optional[Dict[str, Dict[str, float]]] = None,
        expression_assets: Optional[Dict[str, AssetInfo]] = None,
        hotkey_assets: Optional[Dict[str, AssetInfo]] = None,
        asset_map: Optional[Dict[str, str]] = None,
    ) -> None:
        """建立清单。

        Args:
            model_name: 当前模型名（用于日志与目录匹配）。
            expressions: 表情名列表（ExpressionStateRequest 的 expressions）。
            hotkeys: 热键列表（HotkeysInCurrentModelRequest）。
            expression_files: 表情名 -> .exp3.json 文件名。
            expression_params: 文件名 -> 参数表；没有就只靠名字语义。
            expression_assets: 文件名 -> 文件解析结果（部位/类型），优先于名字。
            hotkey_assets: 热键 ID -> 文件解析结果，供道具层判断「这键到底干嘛」。
            asset_map: 人工映射（表情名 -> 情绪词），优先级最高。
        """

        self.model_name = str(model_name or "")
        self.expressions: List[str] = [str(n) for n in (expressions or []) if str(n or "").strip()]
        # 表情名 -> .exp3.json 文件名。VTS 的 ExpressionActivationRequest 认的是文件名，
        # 而且**必须**带 .exp3.json 后缀，否则报 APIError 650。
        self._files: Dict[str, str] = dict(expression_files or {})
        self.hotkeys: List[Dict[str, Any]] = [h for h in (hotkeys or []) if isinstance(h, dict)]
        self._params: Dict[str, Dict[str, float]] = dict(expression_params or {})
        # 文件名 -> 文件解析结果：这是判断「换装还是表情」的主证据
        self._assets_by_file: Dict[str, AssetInfo] = dict(expression_assets or {})
        # 热键 ID -> 文件解析结果：道具层据此区分玩具/动作/换装/特效
        self.hotkey_assets: Dict[str, AssetInfo] = dict(hotkey_assets or {})
        self._asset_map: Dict[str, str] = {
            str(k).strip(): str(v).strip()
            for k, v in (asset_map or {}).items()
            if str(k).strip() and str(v).strip()
        }
        # 表情名 -> 它的切换热键 ID（ToggleExpression 型）
        self._toggle_ids: Dict[str, str] = {}
        # 展示型热键（体型/衣服这类）不参与表情轮换，避免把模型换装掉
        self._display_names = set()
        for item in self.hotkeys:
            name = str(item.get("name") or "").strip()
            hotkey_id = str(item.get("hotkeyID") or "").strip()
            if not name or not hotkey_id:
                continue
            if str(item.get("type") or "") != HOTKEY_TOGGLE:
                continue
            if any(keyword in name for keyword in DISPLAY_KEYWORDS):
                self._display_names.add(name)
            self._toggle_ids[name] = hotkey_id
        self.pairs = resolve_pairs(self.hotkeys)
        # 逐个标定
        self.assets: List[ExpressionAsset] = []
        for name in self.expressions:
            file = self._files.get(name, "")
            params = self._params.get(file) if file else None
            # 文件解析结果可以按「文件名」或「表情名」索引，两种都认
            info = None
            if file:
                info = self._assets_by_file.get(file)
            if info is None:
                info = self._assets_by_file.get(name)
            asset = calibrate_asset(
                name,
                file=file,
                params=params,
                info=info,
                siblings=self.expressions,
                display_override=name in self._display_names,
            )
            mapped = self._asset_map.get(name)
            if mapped:
                key = canonical_emotion(mapped)
                if key:
                    asset.kind = "emotion"
                    asset.label = key
                    asset.confidence = 1.0
                    asset.reason = f"人工映射 → {mapped}"
            self.assets.append(asset)
        self._by_name: Dict[str, ExpressionAsset] = {asset.name: asset for asset in self.assets}

    # ---- 查询
    @property
    def emotion_names(self) -> List[str]:
        """可以参与情绪轮换的表情名（排除了换装/部件/成对开关）。"""

        return [asset.name for asset in self.assets if asset.kind != "display"]

    @property
    def labeled(self) -> Dict[str, str]:
        """已标定出情绪的表情：情绪中文词 -> 表情名（同情绪取置信度最高）。"""

        best: Dict[str, ExpressionAsset] = {}
        for asset in self.assets:
            if asset.kind != "emotion":
                continue
            current = best.get(asset.label)
            if current is None or asset.confidence > current.confidence:
                best[asset.label] = asset
        return {emotion_label(key): asset.name for key, asset in best.items()}

    @property
    def unknown_names(self) -> List[str]:
        """没标定出语义的表情名（不会被拿去演情绪）。"""

        return [asset.name for asset in self.assets if asset.kind == "unknown"]

    @property
    def display_names(self) -> List[str]:
        """换装/外观类表情名：不进情绪轮换，但可以被台词点名触发。"""

        return [asset.name for asset in self.assets if asset.kind == "display"]

    def vocabulary(self) -> List[Dict[str, Any]]:
        """给提示词用的动作词表（只按当前模型的真实资产生成）。

        Returns:
            ``[{"key": 名字, "kind": "pose"/"expression", "aliases": [...]}]``
        """

        items: List[Dict[str, Any]] = []
        for asset in self.assets:
            if asset.kind == "display":
                # 换装类：名字本身就是记号，没有别名（作者起什么就叫什么）
                items.append({"key": asset.name, "kind": "pose", "aliases": []})
            elif asset.kind == "emotion":
                # 别名要把同一情绪的中文叫法都给出去：标定出「流泪」时，
                # AI 写【哭】【难过】【伤心】也该落到这张脸上，否则记号会被
                # 当成普通文字摘掉，看起来就是"说了哭却什么都没发生"。
                items.append({
                    "key": asset.name,
                    "kind": "expression",
                    "aliases": expression_aliases(asset),
                })
        return items

    def match(self, emotion: str) -> str:
        """按情绪词取当前模型的表情名（本名 > 标定结果 > 相邻情绪 > 名字语义）。"""

        target = str(emotion or "").strip()
        if not target:
            return ""
        pool = self.emotion_names
        if target in pool:
            return target
        key = canonical_emotion(target)
        if key:
            hit = self._labeled_name(key, pool)
            if hit:
                return hit
            # 相邻情绪：模型只标了「哭」，说「委屈」也该能用那张带泪的脸
            for sibling in SIBLING_EMOTIONS.get(key, ()):
                hit = self._labeled_name(sibling, pool)
                if hit:
                    return hit
        return match_emotion(target, pool)

    def _labeled_name(self, key: str, pool: Sequence[str]) -> str:
        """取某个 canonical key 下置信度最高的表情名（必须在可轮换池里）。"""

        if not key:
            return ""
        candidates = [
            asset for asset in self.assets
            if asset.kind == "emotion" and asset.label == key and asset.name in pool
        ]
        if not candidates:
            return ""
        return max(candidates, key=lambda asset: asset.confidence).name

    def toggle_id(self, name: str) -> str:
        """取某个表情的切换热键 ID（没有就返回空）。"""

        return self._toggle_ids.get(name, "")

    def asset(self, name: str) -> Optional[ExpressionAsset]:
        return self._by_name.get(str(name or ""))

    def file_of(self, name: str) -> str:
        """取表情对应的 .exp3.json 文件名（VTS 激活接口要的就是这个）。"""

        known = self._files.get(name)
        if known:
            return known
        asset = self._by_name.get(str(name or ""))
        if asset is not None and asset.file:
            return asset.file
        # 兜底：VTS 的表情文件名默认就是「表情名.exp3.json」
        return f"{name}.exp3.json" if name else ""

    def describe(self) -> str:
        """日志用的一行摘要。"""

        labeled = sum(1 for asset in self.assets if asset.kind == "emotion")
        return "模型「%s」表情 %d 个（已标定 %d、未识别 %d、展示型 %d）、动画道具 %d 组" % (
            self.model_name or "未知",
            len(self.expressions),
            labeled,
            len(self.unknown_names),
            sum(1 for asset in self.assets if asset.kind == "display"),
            len(self.pairs),
        )

    def calibration_report(self) -> List[str]:
        """逐个表情的标定说明（引擎启动时打日志，也方便排查「为什么不动脸」）。"""

        return [asset.describe() for asset in self.assets]


def match_emotion(
    emotion: str,
    names: Iterable[str],
    profile: Optional[ExpressionCatalog] = None,
) -> str:
    """把一个情绪词匹配到当前模型真实存在的表情名。

    匹配顺序：清单标定结果（含表情文件参数证据） > 表情本名 > 名字语义。
    找不到就返回空字符串——**绝不**拿别的模型的常见名字硬套。

    Args:
        emotion: 情绪词（中文/英文/日文均可）或表情本名。
        names: 当前模型可用的表情名集合。
        profile: 已标定的清单；不传则只做名字语义匹配（单测/旧调用）。

    Returns:
        匹配到的表情名；匹配不上返回空字符串。
    """

    pool = [str(n) for n in names if str(n or "").strip()]
    if not pool:
        return ""
    if profile is not None:
        hit = profile.match(emotion)
        if hit:
            return hit

    target = str(emotion or "").strip()
    if not target:
        return ""
    # 1) 情绪词就是表情本名（AI 直接点名某个表情）
    for name in pool:
        if _normalize(name) == _normalize(target):
            return name

    # 2) 名字语义：同情绪取分最高，同分取列表里靠前的（模型作者的排序通常有意图）
    key = canonical_emotion(target)
    if key:
        best_name, best_score = "", 0.0
        for name in pool:
            score = name_evidence(name).get(key, 0.0)
            if score > best_score:
                best_name, best_score = name, score
        if best_name:
            return best_name

    # 3) 用户/作者直接用中文词命名时，允许字面包含
    for name in pool:
        if _normalize(target) and _normalize(target) in _normalize(name):
            return name
    return ""


def match_prop(prop: str, names: Iterable[str]) -> str:
    """把道具名匹配到当前模型的动画热键名（开合由 resolve_pairs 处理）。"""

    pool = [str(n) for n in names if str(n or "").strip()]
    if not pool:
        return ""
    target = _normalize(prop)
    for name in pool:
        if _normalize(name) == target:
            return name
    for name in pool:
        if target and target in _normalize(name):
            return name
    return ""


def resolve_pairs(hotkeys: Iterable[Dict[str, Any]]) -> Dict[str, Tuple[str, str]]:
    """把动画热键拆成「基础名 -> (打开ID, 关闭ID)」。

    VTS 里同类动画热键靠名字后缀区分，常见写法：
      「狗尾巴草打开 / 狗尾巴草关闭」「xxx打开 / xxx关闭」「xxx On / xxx Off」
    没有成对出现的就跳过——单边触发没法回收，会一直挂着。
    """

    opened: Dict[str, str] = {}
    closed: Dict[str, str] = {}
    for item in hotkeys or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != HOTKEY_ANIM:
            continue
        name = str(item.get("name") or "").strip()
        hotkey_id = str(item.get("hotkeyID") or "").strip()
        if not name or not hotkey_id:
            continue
        for suffix in ("打开", "开启", "On", "ON", "on", "出来", "表示"):
            if name.endswith(suffix):
                opened.setdefault(name[: -len(suffix)].strip(), hotkey_id)
                break
        else:
            for suffix in ("关闭", "收回", "Off", "OFF", "off", "切回", "默认", "隐藏"):
                if name.endswith(suffix):
                    closed.setdefault(name[: -len(suffix)].strip(), hotkey_id)
                    break
    pairs: Dict[str, Tuple[str, str]] = {}
    for base, open_id in opened.items():
        close_id = closed.get(base)
        if not close_id:
            # 后缀不完全对称时（如「狗尾巴草关闭」对「狗尾草切换出来」），
            # 退一步按包含关系找
            for closed_base, candidate in closed.items():
                if base and (base in closed_base or closed_base in base):
                    close_id = candidate
                    break
        if close_id:
            pairs[base] = (open_id, close_id)
    return pairs


def load_asset_map(path: Optional[Path] = None) -> Dict[str, Dict[str, str]]:
    """读取人工映射文件（``vts/emotion_assets.json``）。

    格式：``{"*": {"exp01": "无语"}, "模型名": {"黑脸": "无语"}}``，
    ``*`` 是全局兜底。文件不存在就返回空字典。
    """

    target = Path(path) if path is not None else Path(__file__).with_name("emotion_assets.json")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for model, mapping in data.items():
        if isinstance(mapping, dict):
            out[str(model)] = {str(k): str(v) for k, v in mapping.items()}
    return out


def asset_map_for(model_name: str, table: Optional[Dict[str, Dict[str, str]]] = None) -> Dict[str, str]:
    """取某个模型的人工映射（模型专属覆盖全局 ``*``）。"""

    data = table if table is not None else load_asset_map()
    merged: Dict[str, str] = dict(data.get("*", {}))
    merged.update(data.get(str(model_name or ""), {}))
    return merged


# ---------------------------------------------------------------------- #
# 7. 文本 -> 情绪
#
# 这一层仍然是关键词表，但它判断的是「这句话是什么情绪」，属于语言层面，
# 与皮套无关，所以不参与模型自适应。顺序即优先级：先判强信号（轻蔑/无语），
# 再判开心，最后才是中性；宁可漏判也不要把开心念成生气。
# ---------------------------------------------------------------------- #
TEXT_EMOTION_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "轻蔑",
        ("轻蔑", "不屑", "瞧不起", "看不上", "鄙视", "切～", "切~"),
    ),
    (
        "无语",
        ("无语", "服了", "醉了", "emmm", "额…", "汗", "离谱", "绝了", "好吧", "行吧",
         "什么鬼", "你礼貌吗", "难绷", "绷不住", "就这", "不至于"),
    ),
    (
        "害羞",
        ("害羞", "脸红", "别夸", "人家", "羞", "不好意思", "夸我", "被夸"),
    ),
    (
        "尴尬",
        ("尴尬", "社死", "抱歉", "失误", "翻车", "搞砸", "忘了", "不小心"),
    ),
    (
        # 哭与委屈同组：标定层的 cry/sad 本来就会互相借用，这里给同一族词，
        # 让「眼泪都要下来了」这种明确到不能再明确的句子，能真的落到流泪那张脸上。
        # 「呜」「嘤」收单字：中文回复里这两个字几乎只用于抽泣（呜……/呜呜/嘤嘤），
        # 「呜呼」「呜咽」也都还是哭，误伤面极小。
        "委屈",
        ("委屈", "难过", "想哭", "哭", "眼泪", "泪目", "泪崩", "抽泣", "哽咽", "鼻酸",
         "破防", "低落", "心累", "伤心", "难受", "心痛", "可怜", "舍不得", "惨",
         "呜呜", "呜", "嘤", "委屈巴巴", "可怜巴巴", "憋不住", "忍不住了",
         # 她自己的台词里说「我就是废物」是自嘲，不是骂人——放进委屈比留在空转好
         "废物"),
    ),
    (
        "生气",
        ("生气", "愤怒", "气死", "讨厌", "可恶", "过分", "别闹", "哼", "不理你"),
    ),
    (
        "疑惑",
        ("疑惑", "不懂", "不明白", "奇怪", "为什么", "咋回事", "确定吗", "真的假的", "？"),
    ),
    (
        "惊讶",
        ("惊讶", "天哪", "天啊", "居然", "竟然", "没想到", "哇", "不会吧", "震惊"),
    ),
    (
        "得意",
        ("得意", "厉害吧", "佩服我", "当然啦", "小意思", "轻松", "必须的", "哼哼"),
    ),
    (
        "开心",
        ("开心", "高兴", "哈哈", "嘻嘻", "笑死", "太好了", "棒", "喜欢", "可爱",
         "有趣", "好玩", "谢谢", "爱了", "赞"),
    ),
)

# 道具/动作触发词 -> 道具名。只在配置允许道具时生效。
TEXT_PROP_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("狗尾巴草", ("狗尾巴草", "狗尾草", "挠", "草")),
    ("竖手指", ("竖手指", "点赞", "点个赞", "给你点", "比个", "竖个大拇指", "打勾", "竖起大拇指")),
    ("抚胸手", ("抚胸", "捂胸", "捂着胸口", "心跳")),
)


def detect_emotion(text: str, extra_map: Optional[Dict[str, str]] = None) -> str:
    """从一句回复文本里推断该做什么表情。

    Args:
        text: 要朗读/发送的回复正文。
        extra_map: 用户自定义映射（关键词 -> 情绪词），优先级最高。

    Returns:
        情绪词（如「无语」）；判断不出返回空字符串，调用方就不切表情。
    """

    body = str(text or "")
    if not body.strip():
        return ""
    lowered = body.lower()

    # 1) 用户自定义映射优先：填了就按用户的来
    for keyword, emotion in (extra_map or {}).items():
        needle = str(keyword or "").strip().lower()
        if needle and needle in lowered:
            return str(emotion or "").strip()

    # 2) 内置关键词，按优先级顺序
    for emotion, keywords in TEXT_EMOTION_RULES:
        for keyword in keywords:
            if keyword.lower() in lowered:
                return emotion
    return ""


def detect_prop(text: str) -> str:
    """从回复文本里推断要不要掏出某个道具（狗尾巴草这类）。"""

    lowered = str(text or "").lower()
    for prop, keywords in TEXT_PROP_RULES:
        for keyword in keywords:
            if keyword.lower() in lowered:
                return prop
    return ""


def detect_reaction(text: str) -> Tuple[str, str]:
    """观众说的一句话 -> 她该给什么脸色。

    注意这不是"镜像观众的情绪"，而是"人听到这句话会有什么反应"：
      观众在难过 / 冲她发火 / 说讨厌她   -> 委屈（受伤或共情）
      观众在吐槽别人（说讨厌"她/他"）    -> 尴尬（讪笑、吃瓜）
      观众自己在发火                      -> 紧张（怕被波及）
      观众在夸她 / 表白                   -> 害羞
      观众先无语了                        -> 无语（跟着翻白眼）
      观众在发问                          -> 疑惑

    Args:
        text: 直播间里观众说的话（弹幕或主播语音）。

    Returns:
        (情绪词, 理由)；判断不出返回 ("", "")。
    """

    body = str(text or "").strip()
    if not body:
        return "", ""
    lowered = body.lower()
    # 直播间的弹幕默认是冲她说的：观众发「好丑啊」「傻逼」根本不会带「你」，
    # 但那显然是在说她。所以默认 at_me=True，只有明确指向第三者才降级成
    # 「观众在吐槽别人」——否则骂她的话会被判成讪笑吃瓜，她一脸尴尬地笑。
    third_party = any(word in body for word in
                      ("他", "她", "别人", "某人", "那人", "隔壁", "室友", "同事"))
    at_me = (
        not third_party
        or any(word in body for word in ("你", "主播", "直播间"))
        or "@" in body
    )

    # 1) 观众在讲自己的情绪：先共情，别急着接话
    if any(word in lowered for word in
           ("难过", "想哭", "要哭", "哭了", "心累", "emo", "委屈", "伤心")):
        return "委屈", "观众在难过"
    # 2) 夸她 / 表白
    if any(word in lowered for word in
           ("喜欢你", "爱你", "好可爱", "漂亮", "好看", "厉害")):
        return "害羞", "被夸"
    # 3) 负面：冲她来的是"受伤"，说别人的是"吃瓜讪笑"
    if any(word in lowered for word in
           ("讨厌", "滚", "闭嘴", "丑", "傻", "笨", "蠢", "菜", "垃圾", "废物",
            "弱智", "有病", "去死", "不喜欢", "难看", "恶心")):
        return ("委屈", "被观众针对") if at_me else ("尴尬", "观众在吐槽别人")
    # 4) 观众自己在发火：她会紧张
    if any(word in lowered for word in ("气死", "烦死", "过分", "别闹", "忍不了")):
        return "紧张", "观众在发火"
    # 5) 观众先无语了：跟着无语
    if any(word in lowered for word in
           ("无语", "离谱", "服了", "醉了", "难绷", "绷不住")):
        return "无语", "观众在无语"
    # 6) 观众在发问 / 不敢信
    if any(word in lowered for word in
           ("为什么", "咋回事", "真的假的", "不会吧", "？", "?")):
        return "疑惑", "观众在发问"
    return "", ""


def parse_emotion_map(raw: str) -> Dict[str, str]:
    """解析配置里的自定义情绪映射（形如 "无语=黑脸,开心=爱心眼"）。

    Args:
        raw: 配置原文。

    Returns:
        Dict[str, str]: 关键词 -> 情绪词。
    """

    result: Dict[str, str] = {}
    for chunk in str(raw or "").replace("，", ",").split(","):
        item = chunk.strip()
        if not item:
            continue
        for sep in ("=", ":", "："):
            if sep in item:
                keyword, _, emotion = item.partition(sep)
                keyword, emotion = keyword.strip(), emotion.strip()
                if keyword and emotion:
                    result[keyword] = emotion
                break
    return result


# ---------------------------------------------------------------------- #
# 8. 运行期状态：互斥切换 + 到期回落
# ---------------------------------------------------------------------- #
class ExpressionState:
    """当前挂着的表情/道具，负责互斥切换与超时回落。

    纯逻辑，不碰网络：真正注入由引擎每一帧查询 to_inject() 完成。
    """

    def __init__(self, catalog: ExpressionCatalog) -> None:
        """初始化。

        Args:
            catalog: 当前模型的表情清单。
        """

        self.catalog = catalog
        # 表情名 -> 到期时间
        self._active: Dict[str, float] = {}
        # 动画道具基础名 -> 到期时间
        self._props: Dict[str, float] = {}
        # 表情名 -> (淡入, 淡出) 秒。每条表情的时序由 vts/timing.py 按
        # 强度与语境算出来，不再让所有表情共用一个 0.22 秒的淡变。
        self._fades: Dict[str, tuple] = {}

    # ---- 查询
    def has_pending(self) -> bool:
        """有没有正挂着、还等着到期的表情或道具（供引擎决定是否每帧检查）。"""

        return bool(self._active) or bool(self._props)

    def active_expressions(self) -> List[str]:
        """当前生效中的表情名（已剔除过期）。"""

        now = time.time()
        return [n for n, expire in self._active.items() if expire > now]

    def active_props(self) -> List[str]:
        """当前生效中的动画道具（已剔除过期）。"""

        now = time.time()
        return [n for n, expire in self._props.items() if expire > now]

    def to_inject(self) -> List[Dict[str, Any]]:
        """生成当前生效的表情指令列表。

        注意：实测这个 VTS 版本**不支持** activations 数组形式
        （ExpressionActivationRequest 传 activations 会报 APIError 650
        "The given expression filename is invalid"，而且整条请求被丢弃），
        只有单条 {"expressionFile": "xxx.exp3.json", "active": bool} 有效。
        所以这里每条都是独立的一次请求。
        """

        now = time.time()
        values: List[Dict[str, Any]] = []
        for name, expire in sorted(self._active.items()):
            file_name = self.catalog.file_of(name)
            if not file_name:
                continue
            fade_in, fade_out = self._fades.get(name, (0.22, 0.22))
            active = expire > now
            values.append({
                "expressionFile": file_name,
                "active": active,
                # 上场用 onset 折出的淡入，收回用 offset 折出的淡出：
                # 同一个 0.22 会让"惊讶"和"委屈"看起来一样快慢。
                "fadeTime": fade_in if active else fade_out,
            })
        return values

    def expired_events(self) -> List[str]:
        """把已经过期的表情摘掉，返回需要显式关闭的名字。"""

        now = time.time()
        gone = [n for n, expire in self._active.items() if expire <= now]
        for name in gone:
            self._active.pop(name, None)
            self._fades.pop(name, None)
        return gone

    def expired_props(self) -> List[str]:
        """把已经过期的道具摘掉，返回需要触发热键收回的基础名。"""

        now = time.time()
        gone = [n for n, expire in self._props.items() if expire <= now]
        for name in gone:
            self._props.pop(name, None)
        return gone

    # ---- 变更
    def set_expression(
        self,
        name: str,
        hold_seconds: float = DEFAULT_HOLD_SECONDS,
        fade_in: float = 0.22,
        fade_out: float = 0.22,
    ) -> List[str]:
        """切到某个表情，返回需要显式关闭的旧表情。

        VTS 里 ToggleExpression 型表情是互斥的：同时挂两个会出现怪脸，
        所以这里一次只保留一个，并且把其余的显式关掉。

        Args:
            name: 目标表情名。
            hold_seconds: 保持多少秒后自动收起；<=0 表示一直挂着。
            fade_in: 淡入时长（秒）；由三段时序的 onset 折算。
            fade_out: 淡出时长（秒）；由 offset 折算。

        Returns:
            需要显式 deactivate 的旧表情名列表（不含 name 自己）。
        """

        now = time.time()
        # hold_seconds <= 0 表示"一直挂着"，用无穷远保证不会被 expired_events 摘掉；
        # 想立刻收起要用 clear()，两者语义不能混
        expire = now + hold_seconds if hold_seconds > 0 else float("inf")
        previous = [n for n, e in self._active.items() if e > now and n != name]
        self._active = {name: expire}
        self._fades[name] = (max(0.02, float(fade_in)), max(0.02, float(fade_out)))
        return previous

    def clear(self) -> List[str]:
        """收起所有表情，返回需要关闭的名字。"""

        gone = list(self._active)
        self._active.clear()
        self._fades.clear()
        return gone

    def clear_props(self) -> List[str]:
        """收起所有动画道具，返回需要发关闭热键的基础名列表。"""

        gone = list(self._props)
        self._props.clear()
        return gone

    def open_prop(self, base: str, hold_seconds: float = 60.0) -> List[str]:
        """打开一个动画道具，返回需要收回的旧道具。

        Args:
            base: 道具基础名（狗尾巴草 这类）。
            hold_seconds: 保持多少秒。

        Returns:
            需要收回的旧道具基础名列表。
        """

        expire = time.time() + hold_seconds if hold_seconds > 0 else float("inf")
        previous = [n for n in self._props if n != base]
        # 同一时间只允许一个道具：旧的要真的摘掉，否则会一直挂到过期
        self._props = {base: expire}
        return previous

    def close_prop(self, base: str) -> bool:
        """收回一个道具。"""

        return self._props.pop(base, None) is not None
