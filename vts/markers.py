# -*- coding: utf-8 -*-
"""台词里的动作记号：把「说的话」和「做的动作」分开。

为什么要在台词里写记号
----------------------
模型自带的表情/动作是**几十个只有作者知道的名字**（兔牙、钢盆、前倾、狗尾巴草…），
让 LLM 自己挑不可能的：它既不知道有哪些，也猜不出该在什么时候用。所以把
「当前这个皮套到底能演什么」写进提示词，让它在台词里打记号，插件再把记号摘掉
交给演出引擎——这是唯一能让「说」和「演」同时准确的路径。

记号语法沿用 Cortico VTuber World 的台本约定：

* ``【兔牙】`` 阻断式动作：语义上是「先做这个，再说后面的」；
* ``<点头>`` 非阻断动作：按它在句子里的位置演，不切分语音。

本插件不切分 TTS（一句话一次合成），两种记号都会**在摘掉后立刻派发**，
差别只在提示词里给模型的语感——``【】`` 用于「换外观/掏道具」，``<>`` 用于
「顺手做个小动作」。

为什么不全摘
------------
``<>`` 在中英文里也用作手写符号（``<3``、``<笑>``、``<https://…>``），
所以只有在**命中当前模型的真实动作名**时才摘；``【】`` 中文正文基本不用，
出现就按记号处理，避免「【歪头】」被 TTS 念出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import re

#: 记号本体：``【x】`` 或 ``<x>``；不允许跨行、不允许嵌套，长度上限挡掉误匹配
#: 记号名上限放宽到 120：容纳自然语言语气描述（【语气:委屈难过，声音低落…】）
MARKER_RE = re.compile(r"【([^【】\n]{1,120})】|<([^<>\n]{1,120})>")

#: 中日文字符：用来判断记号是不是夹在正文中间（决定摘掉后要不要补停顿）
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")

#: 记号后面紧跟这些字时不能补逗号，否则会断出「我点头，了」这种病句
_PARTICLES = frozenset("了着的地得呢啊吧呀嘛哦啦过")

#: 动作类别 -> 提示词里的分组标题
KIND_TITLES: Tuple[Tuple[str, str], ...] = (
    ("tag", "小动作"),
    ("pose", "外观/姿势"),
    ("expression", "表情"),
    ("prop", "小道具"),
    ("direction", "语气"),
)

#: 动作类别 -> 模板占位符名。占位符是给用户改文案用的，值永远来自当前模型。
PLACEHOLDER_BY_KIND: Dict[str, str] = {
    "tag": "small_actions",
    "pose": "poses",
    "expression": "expressions",
    "prop": "props",
    "direction": "directions",
}

#: 内置提示词模板。插件配置里填了自定义模板就以用户为准；
#: 动作清单永远是这些占位符，所以换模型后自制模板照样有效。
DEFAULT_PROMPT_TEMPLATE = (
    "【动作记号】你可以在回复里插入动作记号，它们不会念出来，观众也看不到：\n"
    "- 小动作：{small_actions}\n"
    "- 外观/姿势：{poses}\n"
    "- 表情：{expressions}\n"
    "- 小道具：{props}\n"
    "- 语气：{directions}\n"
    "写法：表情、外观、道具都用方括号，如 `【流泪】`、`【兔牙】`、`【狗尾巴草】`"
    "（同一个再写一次就收起）；小动作用尖括号，如 `<点头>`。\n"
    "组合：一个括号里可以用逗号写多个动作，如 `【微笑,看向屏幕】`、`<点头,眨眼>`，"
    "效果等同按顺序逐个写。\n"
    "`【重置】` 会收起身上所有表情/道具/姿势并把视线转回镜头，换话题时用。\n"
    "语气：在某句话的开头写 `【语气:…】`，那句话就会真的用那种状态说出来。"
    "描述用自然语言，情绪、声音质感、语速都可以写，例如：\n"
    "`【语气:发现真相后委屈难过，声音低落柔弱，带轻微哽咽，语速稍慢】`、\n"
    "`【语气:热恋中自然撒娇，声音柔软亲近，带着俏皮笑意】`。\n"
    "图快也可以用快捷词：`【压低声音】`、`【兴奋语气】`、`【带哭腔】`、"
    "`【正常语气】`(恢复默认)。\n"
    "音效：台词里可以写**半角方括号**的音效标签，会被演绎成真实的非语音声音"
    "（不念出来），如 `[laugh]` 笑、`[叹气]`、`[哭]`、`[gasp]` 倒吸气。"
    "可用标签（只能用这些，写别的会被念出来）：\n"
    "[笑] [大笑] [轻笑] [窃笑] [偷笑] [吸气] [呼气] [深呼吸] [喘气] [叹气] [咳嗽] "
    "[清嗓子] [吞咽] [咂嘴] [哭] [抽泣] [哽咽] [尖叫] [惊呼] [打哈欠] [打喷嚏] [哼] [停顿]\n"
    "[laugh] [giggle] [chuckle] [sigh] [cough] [clears throat] [sniff] [gasp] [breath] [cry] [yawn]\n"
    "**注意区分**：全角【】是给皮套的动作记号（观众看不到），半角[]是给声音的"
    "音效标签（观众听得到），两者别写混。\n"
    "示例：`这题我会<眨眼>` 是边说边眨一下眼；`呜……你别凶我【流泪】` 是边说边哭。\n"
    "表情必须自己写记号才会上脸：想哭就写 `【流泪】` 或 `【委屈】`，"
    "只在台词里说「呜呜」是不会出现表情的。\n"
    "用法：一回合最多 1~3 个动作记号，语气记号只在情绪转变时用；"
    "不要每句都加，也不要把记号写进括号或引号里；列表里没有的动作不要编，宁可不用。"
)

#: 模板里的占位符：只认小写字母和下划线，避免误吃正文里的 `{}`
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def render_prompt_template(template: str, values: Dict[str, str]) -> str:
    """把占位符替换成当前模型的动作清单。

    两条规则是刻意的：

    1. **占位符没有对应动作时，整行删掉**。否则换到一个小皮套会剩下
       「- 小道具：」这种空标题，模型会以为这类动作存在但自己没记住。
    2. **不认识的占位符原样保留**。用户把 ``{expressions}`` 打成
       ``{expression}`` 时，让他看见这个括号，而不是悄悄变成空字符串。

    Args:
        template: 用户模板或内置默认模板。
        values: 占位符名 -> 已经拼好的清单文本（没有动作时为空串）。

    Returns:
        渲染后的提示词；全空时返回空串。
    """

    lines: List[str] = []
    for line in str(template or "").splitlines():
        tokens = _PLACEHOLDER_RE.findall(line)
        known = [token for token in tokens if token in values]
        # 这行引用的动作类别里只要有一个是空的，整行就不要了
        if known and any(not str(values[token]).strip() for token in known):
            continue
        rendered = line
        for token in tokens:
            if token in values:
                rendered = rendered.replace("{" + token + "}", str(values[token]))
        lines.append(rendered)
    # 删行会留下连续空行，压一下但保留段落间距
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

#: 一次回复里最多派发几个记号：多了像抽搐，也会拖慢播报
MAX_ACTIONS_PER_REPLY = 3


@dataclass(frozen=True)
class ActionMarker:
    """台词里摘下来的一个动作记号。"""

    name: str
    blocking: bool          # 【】True / <>False
    raw: str = ""

    def __str__(self) -> str:
        return self.raw or (f"【{self.name}】" if self.blocking else f"<{self.name}>")


def split_actions(
    text: str,
    known: Optional[Iterable[str]] = None,
) -> Tuple[str, List[ActionMarker]]:
    """把台词里的动作记号摘出来。

    Args:
        text: 模型输出的正文（可能含记号）。
        known: 当前模型真实可用的动作名/别名集合；``<>`` 只有命中才摘。

    Returns:
        (干净台词, 记号列表)，按出现顺序。干净台词已清理多余空格。
    """

    body = str(text or "")
    if not body:
        return "", []
    pool = {_norm(name) for name in (known or ()) if str(name or "").strip()}
    markers: List[ActionMarker] = []

    def _replace(match: "re.Match[str]") -> str:
        block, inline = match.group(1), match.group(2)
        name = (block if block is not None else inline or "").strip()
        blocking = block is not None
        if not name:
            return "" if blocking else match.group(0)
        # 组合记号（cortico 台本写法）：【微笑,看向屏幕】一个括号里逗号分隔
        # 多个动作，拆成多个独立记号处理。语气记号（【语气:委屈难过，声音
        # 低落…】）的描述里天然有逗号，必须整体保留，不参与拆分。
        # **<> 与【】一样无条件摘**：端到端实测 AI 会编造词表外的记号
        # （<摸摸脸>、<双手合十在胸前，用力点头>），保留原文的话 TTS 会把
        # "摸摸脸"三个字念出来——那比少演一个动作严重得多。未知名在
        # _run_actions 里静默跳过（不执行），但绝不会出现在观众耳朵里。
        raw_name = name
        parts = [p.strip() for p in re.split("[,，/]", name) if p.strip()]
        if len(parts) > 1 and not raw_name.startswith(DIRECTION_PREFIXES):
            for part in parts:
                markers.append(ActionMarker(name=part, blocking=blocking, raw=match.group(0)))
        else:
            markers.append(ActionMarker(name=name, blocking=blocking, raw=match.group(0)))
        # 夹在正文中间时补一个逗号：直接抹掉会让「骂我【流泪】我明明很努力」
        # 变成「骂我我明明」，TTS 连读听起来像口吃。前后已有标点的不用补。
        before = body[match.start() - 1] if match.start() > 0 else ""
        after = body[match.end()] if match.end() < len(body) else ""
        # 后面紧跟助词时直接贴合：「我点头<点头>了」不能变成「我点头，了」
        if after and after in _PARTICLES:
            return ""
        if CJK_RE.search(before or "") and CJK_RE.search(after or ""):
            return "，"
        return " "

    clean = MARKER_RE.sub(_replace, body)
    if markers:
        clean = _tidy(clean)
    return clean, markers


def _tidy(text: str) -> str:
    """摘掉记号后收拾残留空格/标点。"""

    out = re.sub(r"[ \t]{2,}", " ", text)
    # 中文里「我点头 了」这种断开的空格：两边都是中文就贴合上
    out = re.sub(
        r"(?<=[\u4e00-\u9fff，。！？、；：）】」』…])\s+(?=[\u4e00-\u9fff，。！？、；：…])",
        "",
        out,
    )
    # 中文标点前面的空格也删掉
    out = re.sub(r"\s+([，。！？、；：）】」』…])", r"\1", out)
    out = re.sub(r"([（【「『])\s+", r"\1", out)
    return out.strip()


#: 内置视线动作：不依赖模型资产（任何皮套都有眼睛）。
#: 数值严格参考 cortico-world-vtuber 的 clips.json（eyeX/eyeY）：
#: 每个目标是**语义位置**而不是裸方向——AI 说"看看弹幕"比"向左看 30 度"自然。
#: x/y 是引擎 /gaze 的注视目标；设置后一直保持，由插件在 4~6 秒（随机档，
#: 同 cortico 的 gaze 超时）后滑回默认注视「看向镜头」。
GAZE_TARGETS: Tuple[Tuple[str, Tuple[str, ...], float, float], ...] = (
    # (key, aliases, eye_x, eye_y)
    ("看向镜头", ("看镜头", "看回来", "看观众"), 0.0, 0.0),      # camera：待机相
    ("看向屏幕", ("看屏幕", "看画面"), -0.5, -0.35),             # screen：游戏画面
    ("看向弹幕", ("看弹幕", "看一眼弹幕"), 0.75, -0.15),         # chat：弹幕墙
    ("看向下面", ("看下面", "低头看"), 0.0, -0.85),              # down
    ("看向上面", ("看上面", "看斜上方"), 0.35, 0.75),            # up
    ("看向左边", ("看左边", "看左", "瞟向左边"), -1.0, 0.0),     # 方向补充（屏幕/弹幕的粗粒度版）
    ("看向右边", ("看右边", "看右", "瞟向右边"), 1.0, 0.0),
)


#: 内置场景重置记号（cortico 台本的【Reset】）：收起表情/道具/姿势，视线回镜头。
#: 和视线一样是内置能力，不依赖模型资产。
RESET_ACTIONS: Tuple[str, ...] = ("重置", "复位", "Reset")

#: 内置表演指令记号（breeze 的 voice direction）：名字 → 该句的英文 instruction。
#: AI 在某句话里写【压低声音】，那句话就真的会压低声音合成——breeze 的
#: instruction 是"同人不同状态"的旋钮，和音色（voice_id）互相独立。
#: 命名刻意带"语气/声音"后缀，避免和模型表情名（惊讶/生气）撞车。
DIRECTION_ACTIONS: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    ("压低声音", ("小声说", "悄悄话"), "Speak in a low, quiet voice, like sharing a secret."),
    ("兴奋语气", ("激动地说",), "Speak fast and excited, bursting with energy."),
    ("带哭腔", ("哭腔",), "Speak with a trembling, tearful voice."),
    ("惊讶语气", ("吃惊地说",), "Speak with surprise, voice rising mid-sentence."),
    ("生气语气", ("气鼓鼓地说",), "Speak with an irritated, puffed-up tone."),
    ("疲惫语气", ("有气无力",), "Speak slowly with a tired, drained voice."),
    ("正常语气", ("恢复语气",), ""),  # 空串 = 回到配置里的默认表演指令
)


def _norm(text: str) -> str:
    return str(text or "").strip().lower().replace(" ", "")


def build_action_prompt(
    actions: Sequence[Dict[str, object]],
    *,
    max_items: int = 18,
    template: str = "",
) -> str:
    """把动作词表写成给模型的提示词。

    默认文案内置在 :data:`DEFAULT_PROMPT_TEMPLATE`，也可以在插件配置里
    整个替换掉——用户改的是**说话方式**，动作清单仍然来自当前模型的真实资产，
    所以换皮之后自制模板不会失效。

    模板里的占位符（值来自模型，不是用户填的）：

    ==================  =====================================
    ``{small_actions}`` 小动作，如 nod（点头）、wink（眨眼）
    ``{poses}``         外观/姿势，如 兔牙、钢盆、前倾
    ``{expressions}``   已标定的表情，如 流泪（委屈/难过）
    ``{props}``         可开合的小道具，如 狗尾巴草
    ==================  =====================================

    某个占位符没有对应动作时，**整行会被删掉**，不会留下「- 小道具：」这种空标题。

    Args:
        actions: ``[{"key": "兔牙", "kind": "pose", "aliases": ["兔牙"]}, …]``，
            来自演出引擎按**当前模型真实资产**生成的快照。
        max_items: 每类最多列几个，避免提示词过长（小模型会开始照抄列表）。
        template: 自定义模板；留空用内置默认。

    Returns:
        提示词片段；没有可用动作时返回空字符串（此时不该告诉模型有记号）。
    """

    grouped: Dict[str, List[str]] = {}
    # 内置视线动作排在最前：它们不依赖模型资产，任何皮套都可用（参考
    # cortico-world-vtuber——视线是五通道之一，词表永远包含"看向…"）。
    # 【重置】与语气记号同样内置（语气驱动 breeze 的 voice direction）。
    builtin: List[Dict[str, object]] = [
        {"key": key, "kind": "tag", "aliases": list(aliases)}
        for key, aliases, _gx, _gy in GAZE_TARGETS
    ] + [
        {"key": "重置", "kind": "tag", "aliases": ["Reset", "复位"]},
        *[
            {"key": key, "kind": "direction", "aliases": list(aliases)}
            for key, aliases, _ins in DIRECTION_ACTIONS
        ],
    ]
    for item in [*builtin, *(actions or [])]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        key = str(item.get("key") or "").strip()
        if not kind or not key:
            continue
        aliases = [str(a).strip() for a in (item.get("aliases") or []) if str(a or "").strip()]
        # 展示给模型的用「key（别名1/别名2）」的紧凑写法，省 token
        label = key if not aliases else f"{key}（{'/'.join(aliases[:2])}）"
        bucket = grouped.setdefault(kind, [])
        if label not in bucket and key not in bucket:
            bucket.append(label)

    builtin_keys = {key for key, _a, *_ in GAZE_TARGETS} | {"重置"} | {key for key, _a, *_ in DIRECTION_ACTIONS}
    for kind, labels in grouped.items():
        # 内置记号（视线/语气/重置）保持在前；皮套资产按 key 排序——引擎返回
        # 顺序不稳定（重启/VTS 资产顺序抖动），排序保证同一批资产永远生成
        # 逐字节相同的提示词。这段文本进 system 区域，顺序抖一次整段
        # 前缀缓存就报废。
        labels.sort(key=lambda label: (0 if label.split("（", 1)[0] in builtin_keys else 1, label))

    if not grouped:
        return ""

    values: Dict[str, str] = {}
    for kind, _title in KIND_TITLES:
        items = grouped.get(kind) or []
        if not items:
            values[PLACEHOLDER_BY_KIND[kind]] = ""
            continue
        shown = items[:max_items]
        more = "" if len(items) <= max_items else f" 等 {len(items)} 个"
        values[PLACEHOLDER_BY_KIND[kind]] = "、".join(shown) + more

    return render_prompt_template(template or DEFAULT_PROMPT_TEMPLATE, values)


def _gaze_lookup(name: str) -> Optional[Tuple[str, float, float]]:
    """按名字/别名查内置视线动作，返回 (规范 key, eye_x, eye_y)；查不到返回 None。"""

    target = _norm(name)
    if not target:
        return None
    for key, aliases, gx, gy in GAZE_TARGETS:
        if _norm(key) == target:
            return key, gx, gy
        for alias in aliases:
            if _norm(alias) == target:
                return key, gx, gy
    return None


def gaze_vector_of(name: str) -> Optional[Tuple[float, float]]:
    """视线动作名/别名 → 引擎 /gaze 的 (x, y)；不是视线动作返回 None。"""

    found = _gaze_lookup(name)
    return None if found is None else (found[1], found[2])


def _reset_lookup(name: str) -> bool:
    """名字是否是场景重置记号（重置/复位/Reset）。"""

    return _norm(name) in {_norm(r) for r in RESET_ACTIONS}


def _direction_lookup(name: str) -> Optional[Tuple[str, str]]:
    """按名字/别名查内置表演指令，返回 (规范 key, 英文 instruction)；查不到返回 None。"""

    target = _norm(name)
    if not target:
        return None
    for key, aliases, instruction in DIRECTION_ACTIONS:
        if _norm(key) == target:
            return key, instruction
        for alias in aliases:
            if _norm(alias) == target:
                return key, instruction
    return None


#: 语气记号前缀：`【语气:…】` 后面跟**自然语言表演描述**（发给 breeze 原样生效）。
#: 实测同一句台词，中文描述能让语速/停顿随之变化（3.68s → 4.00s），API 200。
DIRECTION_PREFIXES = ("语气:", "语气：")


def _is_direction_name(name: str) -> bool:
    """是否语气记号：自由语法 `【语气:…】` 或快捷词（压低声音/兴奋语气…）。"""

    raw = str(name or "").strip()
    if raw.startswith(DIRECTION_PREFIXES):
        return True
    return _direction_lookup(raw) is not None


def direction_instruction_of(name: str) -> Optional[str]:
    """语气记号 → 本句的 instruction（voice direction）；不是语气记号返回 None。

    两种写法：
      自由语法 【语气:发现真相后委屈难过，声音低落柔弱，带轻微哽咽，语速稍慢】——
        中文描述**原样**发给 breeze（它训练时见过自然语言指导，实测有效）；
      快捷词 【压低声音】——预置英文描述，图快时用。
      `【正常语气】`/`【语气:】`(空) = 空串，回到配置里的默认表演指令。
    """

    raw = str(name or "").strip()
    for prefix in DIRECTION_PREFIXES:
        if raw.startswith(prefix):
            return raw[len(prefix):].strip() or ""
    found = _direction_lookup(raw)
    return None if found is None else found[1]


#: breeze 的 vocal events（内嵌非语音声音）：台词里写这些标签，breeze 会
#: 演绎成真实的音效而不是念出来。标签词表是自由格式，这里列出常见的做
#: 字幕清理用（音效不该出现在字幕上）。
#: breeze 的 vocal events（内嵌非语音声音）：**半角方括号 + 固定词表**（用户确认）。
#: 千万别和 VTS 记号搞混——全角【】是动作记号（摘掉后驱动皮套），半角 [] 是
#: 音效（原样进 TTS，被演绎成真实声音）。词表封闭：不在这个清单里的标签会被
#: 念出来，所以提示词里要列全。
BREEZE_VOCAL_EVENTS: Tuple[str, ...] = (
    # 中文
    "笑", "大笑", "轻笑", "窃笑", "偷笑", "吸气", "呼气", "深呼吸", "喘气",
    "叹气", "咳嗽", "清嗓子", "吞咽", "咂嘴", "哭", "抽泣", "哽咽", "尖叫",
    "惊呼", "打哈欠", "打喷嚏", "哼", "停顿",
    # 英文
    "laugh", "giggle", "chuckle", "sigh", "cough", "clears throat", "sniff",
    "gasp", "breath", "cry", "yawn",
)


def strip_vocal_events(text: str) -> str:
    """字幕用：摘掉**所有**半角方括号标签。

    已识别的音效会被 breeze 演成声音（字幕不该显示字样）；词表外的（AI 编造
    的 `[吸鼻子]` 这类）会被 TTS 念出来，更是垃圾。字幕上都不该出现。
    """

    cleaned = re.sub(r"\[[^\[\]\n]{0,24}\]", "", str(text or ""))
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def strip_unknown_bracket_tags(text: str) -> str:
    """TTS 文本用：只摘**词表外**的半角方括号标签。

    词表内的（如 `[叹气]`）要原样留给 breeze 演绎成音效；AI 编造的词表外
    标签（`[吸鼻子]`）breeze 不认识，会把字念出来，必须摘掉。
    """

    known = "|".join(re.escape(e) for e in BREEZE_VOCAL_EVENTS)
    cleaned = re.sub(r"\[(?!" + known + r"\])[^\[\]\n]{0,24}\]", "", str(text or ""))
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def action_kind_of(name: str, actions: Sequence[Dict[str, object]]) -> str:
    """按名字/别名反查动作类别（tag/pose/expression/prop/gaze/reset/direction），
    查不到返回空串。

    视线、重置与语气是内置动作（不来自模型资产表），所以要先查内置表。
    语气查在模型资产表之前——命名带后缀（惊讶语气 vs 惊讶）就是为了不抢表情。
    """

    if _gaze_lookup(name) is not None:
        return "gaze"
    if _reset_lookup(name):
        return "reset"
    if _is_direction_name(name):
        return "direction"
    target = _norm(name)
    if not target:
        return ""
    for item in actions or []:
        if not isinstance(item, dict):
            continue
        key = _norm(item.get("key"))
        if key == target:
            return str(item.get("kind") or "").strip()
        for alias in item.get("aliases") or []:
            if _norm(alias) == target:
                return str(item.get("kind") or "").strip()
    return ""


def canonical_action_of(name: str, actions: Sequence[Dict[str, object]]) -> str:
    """按名字/别名取回动作的 key（落库用的规范名）；内置动作返回内置规范名。"""

    found = _gaze_lookup(name)
    if found is not None:
        return found[0]
    if _reset_lookup(name):
        return "重置"
    if _is_direction_name(name):
        # 自由语气返回带前缀的原名（direction_instruction_of 靠它取描述原文）
        return str(name or "").strip()
    direction = _direction_lookup(name)
    if direction is not None:
        return direction[0]
    target = _norm(name)
    if not target:
        return ""
    for item in actions or []:
        if not isinstance(item, dict):
            continue
        if _norm(item.get("key")) == target:
            return str(item.get("key") or "").strip()
        for alias in item.get("aliases") or []:
            if _norm(alias) == target:
                return str(item.get("key") or "").strip()
    return ""
