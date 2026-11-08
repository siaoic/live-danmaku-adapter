"""道具调度：从「当前模型真的有什么」里挑道具，时不时掏出来演一下。

自适应原则
----------
不写死任何皮套的道具名，也不假设「道具」长得一样。候选只来自 VTS 当前模型的
真实数据，并先按**行为类型**分类，再决定能不能用、怎么用：

1. **物品**（``ItemListRequest``）：``canLoad`` 且当前没加载的 Live2D/PNGTuber 物品；
2. **成对热键**（``TriggerAnimation`` 且名字带开/关后缀）：能开能收，可以挂着；
3. **单发热键**：只有一次触发、没有收回键，只能当一次性小动作。

分类（:data:`PROP_CLASS_*`，**先读文件再退回名字**）：
  * ``toy``     能挂能收的小玩意（狗尾巴草、尾巴、光环…）——可以「开→停→收」；
  * ``gesture`` 一次性动作（点头、抖动、Anim Shake）——触发即结束，不能长挂；
  * ``look``    换装/外观（衣服、发型、体型）——**禁用**，
                否则 AI 演个道具把模型换了套衣服；
  * ``effect``  背景/特效（樱花、雨、光斑）——**禁用**，会盖住画面。

判定依据来自 `vts/assetfile.py`：热键的 ``file`` 指向哪个 ``.exp3.json`` /
``.motion3.json``，就按它实际动的部位（脸 / 身体 / 头发）与 ``Meta.Loop`` 分类。
名字词表（中/日/英）只在读不到文件时兜底——否则换一个用 ``动作1`` 命名的模型，
整套判断就失效。

为什么禁用而不是低权重：这两类一旦被误当成"道具"，观感上是"主播突然变装/糊成一片"，
比不演更糟。宁可少一个道具，也不能出戏。

节律依据（真人 / 拟人研究）
--------------------------
* 空闲动画是「灵动感」的主要来源，关键是**不能停**，一旦定住，拟人感立刻崩
  （Atxa Landa et al., *Evaluating Idle Animation Believability*, arXiv:2509.05023）。
* 手工做的空闲动作最容易输在「太规律」：动作分布方差小、间隔像节拍器。
  真实动作的间隔是不规则、长尾的，所以这里用对数均匀采样而不是 uniform。
* 坐着的成年人自然小动作约 1 次/分钟量级（Galton 的 fidget 计数），
  15~20 次/小时；太密会显得多动，太稀（2~5 分钟）又会「像断了电」。
* 一次性动作不能像道具那样"挂着"：它本来就是几百毫秒的位移，挂久了模型会
  停在动作最后一帧（很多 rig 不自动回位），看起来像卡住。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import math
import random

from .expression import DISPLAY_KEYWORDS, HOTKEY_ANIM
from .assetfile import KIND_EXPRESSION, KIND_MEDIA, KIND_MODEL, KIND_MOTION, AssetInfo

#: 这些情绪挂着道具会显得没心没肺，直接跳过这一拍
NEGATIVE_EMOTIONS = frozenset({"生气", "害怕", "委屈", "紧张"})

#: 这些情绪本身就在「卖萌」，下一次道具来得更快一点，跟情绪连成一拍
CUTE_EMOTIONS = frozenset({"开心", "得意", "害羞", "疑惑"})

#: 可爱情绪下的下一次道具间隔（秒）
CUTE_INTERVAL = (15.0, 45.0)

#: 名字命中这些词的道具更「可爱」，只用于加权排序，不影响能不能用
CUTE_HINTS = ("草", "花", "猫", "狗", "兔", "熊", "心", "星", "气球", "翅膀", "糖", "尾巴", "耳")

# ---------------------------------------------------------------------- #
# 行为分类
# ---------------------------------------------------------------------- #
PROP_CLASS_TOY = "toy"
PROP_CLASS_GESTURE = "gesture"
PROP_CLASS_LOOK = "look"
PROP_CLASS_EFFECT = "effect"
PROP_CLASS_UNKNOWN = "unknown"

#: 换装/外观类：命中即禁用（中/日/英）
LOOK_HINTS: Tuple[str, ...] = (
    "衣服", "服装", "服饰", "套装", "女仆", "泳装", "校服", "汉服", "裙", "裤", "外套",
    "发型", "头发", "发色", "体型", "身材", "帽子",
    "眼镜", "配饰", "耳朵", "兔牙", "钢盆", "胸",
    "outfit", "cloth", "dress", "skirt", "hair", "body", "size", "hat", "glasses",
    "accesor", "accessor",
    "服", "髪", "衣装", "メガネ",
)

#: 背景/特效类：命中即禁用
EFFECT_HINTS: Tuple[str, ...] = (
    "背景", "特效", "粒子", "樱花", "雨", "雪", "光斑", "烟雾", "闪", "滤镜", "遮罩",
    "background", "effect", "particle", "snow", "rain", "smoke", "glitch", "filter",
    "背景", "エフェクト", "雪", "雨",
)

#: 一次性动作：只影响挂多久，不影响可用性
GESTURE_HINTS: Tuple[str, ...] = (
    "shake", "nod", "wave", "jump", "dance", "clap", "点头", "摇头", "挥手", "招手",
    "跳", "抖", "摇头", "鼓掌", "拍手", "鞠躬", "眨眼", "wink", "动作", "anim",
    "うなず", "手振",
)

#: 物品类型里适合当道具挂上去的；Effect 是背景/特效，挂上去会盖住画面
_PROP_ITEM_TYPES = frozenset({"live2d", "live2ditem", "pngtuber", "vnet", ""})

#: 一次性动作的挂载时长（秒）：动作本身几百毫秒，挂久了会停在最后一帧
_GESTURE_HOLD = (1.2, 2.6)

#: 各行为类的挑选权重（越大越常被选中）
_CLASS_WEIGHT: Dict[str, float] = {
    PROP_CLASS_TOY: 1.0,
    PROP_CLASS_GESTURE: 0.7,
    PROP_CLASS_UNKNOWN: 0.45,
}


def _norm(text: str) -> str:
    return str(text or "").strip().lower()


def classify_asset(asset: Optional[AssetInfo], kind: str = "") -> Optional[str]:
    """按文件内容判断候选类型；判断不了返回 None（交给名字兜底）。

    这是主路径：不管热键叫「竖手指」「Anim 3」还是「动作1」，读它指向的文件就知道
    它动了哪些部位、是不是循环动画。
    """

    if asset is None:
        return None
    if kind == "item":
        # 物品是独立挂在屏幕上的角色/贴纸，本身就是道具
        return PROP_CLASS_TOY
    if asset.kind in (KIND_MEDIA, KIND_MODEL):
        # 图片/视频/换模型：不是可演的小动作，要么是特效要么是换皮
        return PROP_CLASS_EFFECT
    if asset.kind == KIND_MOTION:
        # 一次性动画（点头/抖动）只能触发一次；循环动画可以开着挂着。
        # 但「循环 + 只挪身体/手臂」是摆姿势（前倾、换坐姿），挂上去等于换了个造型；
        # 只动头发/头部/自定义参数的循环动画才是脑袋上挂着的小玩意（呆毛、尾巴草）。
        if not asset.loop:
            return PROP_CLASS_GESTURE
        return PROP_CLASS_LOOK if asset.is_posture else PROP_CLASS_TOY
    if asset.kind == KIND_EXPRESSION:
        # 只改身体/头发/位置的表情文件 = 换装或摆姿势
        return PROP_CLASS_LOOK if asset.only_body else PROP_CLASS_TOY
    if asset.only_body:
        return PROP_CLASS_LOOK
    return None


def classify_name(
    name: str,
    *,
    kind: str = "",
    item_type: str = "",
    asset: Optional[AssetInfo] = None,
) -> str:
    """判断这个候选项属于哪一类。

    Args:
        name: 道具名（任意语言）。
        kind: ``item`` / ``hotkey`` / ``oneshot``。
        item_type: 物品的 VTS type（``Live2D`` / ``PNGTuber`` / ``Effect``…）。
        asset: 热键/物品指向文件的解析结果；有就以它为准。

    Returns:
        :data:`PROP_CLASS_*` 之一。
    """

    by_file = classify_asset(asset, kind)
    if by_file is not None:
        return by_file

    body = _norm(name)
    if not body:
        return PROP_CLASS_UNKNOWN
    if _norm(item_type) == "effect":
        return PROP_CLASS_EFFECT
    if any(word in body for word in EFFECT_HINTS):
        return PROP_CLASS_EFFECT
    if any(word in body for word in LOOK_HINTS):
        return PROP_CLASS_LOOK
    if any(word in body for word in DISPLAY_KEYWORDS):
        return PROP_CLASS_LOOK
    if kind == "item":
        return PROP_CLASS_TOY
    if kind == "oneshot":
        return PROP_CLASS_GESTURE
    if any(word in body for word in GESTURE_HINTS):
        return PROP_CLASS_GESTURE
    # 名字层面看不出问题（没说是换装、也没说是动作）：当可挂道具，
    # 因为「能开能收的热键」绝大多数就是作者做的小玩意。
    return PROP_CLASS_TOY


@dataclass(frozen=True)
class PropCandidate:
    """一个可用道具。

    ``item`` 用 ``instanceID`` 装载 / 卸载，``hotkey`` / ``oneshot`` 用热键触发。
    字段顺序保持兼容：``(name, kind, open_ref, close_ref)``。
    """

    name: str
    kind: str                     # "item" / "hotkey" / "oneshot"
    open_ref: str
    close_ref: str = ""
    cls: str = PROP_CLASS_UNKNOWN
    weight: float = 1.0

    @property
    def cute(self) -> float:
        """名字里的可爱线索 × 行为类权重：只影响挑选概率，不决定可用性。"""

        score = 1.0 + sum(1 for word in CUTE_HINTS if word in self.name)
        return score * max(0.05, float(self.weight)) * _CLASS_WEIGHT.get(self.cls, 0.45)

    @property
    def hold_seconds(self) -> Optional[float]:
        """这一类该挂多久；None 表示用调度器的默认值。"""

        if self.cls == PROP_CLASS_GESTURE:
            return _GESTURE_HOLD[1]
        return None

    @property
    def usable(self) -> bool:
        """换装/特效不参与自动道具。"""

        return self.cls not in (PROP_CLASS_LOOK, PROP_CLASS_EFFECT)


@dataclass(frozen=True)
class PropAction:
    """调度器让引擎执行的一次动作。"""

    kind: str                     # "open" / "close"
    candidate: PropCandidate


def item_candidates(items: Iterable[Dict[str, Any]]) -> List[PropCandidate]:
    """把 VTS 物品列表过滤成「可以临时挂上去」的道具。"""

    out: List[PropCandidate] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        instance = str(item.get("instanceID") or "").strip()
        if not instance or not item.get("canLoad") or item.get("loaded"):
            continue
        kind = str(item.get("type") or "").strip().lower()
        if kind not in _PROP_ITEM_TYPES:
            continue
        name = str(item.get("name") or instance)
        cls = classify_name(name, kind="item", item_type=kind)
        if cls in (PROP_CLASS_LOOK, PROP_CLASS_EFFECT):
            continue
        out.append(PropCandidate(name, "item", instance, cls=cls))
    return out


def hotkey_candidates(
    pairs: Dict[str, Tuple[str, str]],
    hotkey_assets: Optional[Dict[str, AssetInfo]] = None,
) -> List[PropCandidate]:
    """把「基础名 -> (打开ID, 关闭ID)」的配对热键转成候选（能开能收，可挂）。

    Args:
        pairs: ``resolve_pairs()`` 的结果。
        hotkey_assets: 热键 ID -> 文件解析结果；有就以文件内容分类，不看名字。
    """

    assets = hotkey_assets or {}
    out: List[PropCandidate] = []
    for base, (open_id, close_id) in (pairs or {}).items():
        if not open_id or not close_id:
            continue
        cls = classify_name(base, kind="hotkey", asset=assets.get(open_id) or assets.get(close_id))
        if cls in (PROP_CLASS_LOOK, PROP_CLASS_EFFECT):
            continue
        out.append(PropCandidate(base, "hotkey", open_id, close_id, cls=cls))
    return out


#: 成对热键的收/放后缀：单发候选要用它把「xx打开 / xx关闭」认回成已配对的那一对
_PAIR_SUFFIXES = ("打开", "开启", "关闭", "收回", "出来", "切回", "默认", "On", "Off", "on", "off")


def _pair_stem(name: str) -> str:
    """「狗尾巴草打开」-> 「狗尾巴草」；不是成对写法就返回空串。"""

    body = str(name or "").strip()
    for suffix in _PAIR_SUFFIXES:
        if body.endswith(suffix) and len(body) > len(suffix):
            return body[: -len(suffix)].strip()
    return ""


def oneshot_candidates(
    hotkeys: Optional[Iterable[Dict[str, Any]]],
    pairs: Optional[Dict[str, Tuple[str, str]]] = None,
    hotkey_assets: Optional[Dict[str, AssetInfo]] = None,
) -> List[PropCandidate]:
    """没有配对、但确实是动画触发的热键：当一次性小动作使用。

    有些模型只提供一个 TriggerAnimation 热键（如 Akari 的「Anim Shake」），
    没有对应的「关闭」键。这类不做开合，只触发一次当小动作，
    也不会去碰体型/衣服这类展示型热键。

    注意要把「xx打开 / xx关闭」认回成已配对的那一对：配对表里存的是基础名
    （狗尾巴草），而热键名带后缀，只按全名比对会让两端各多出一个单发候选——
    实测大肥鱼因此多出 6 条幽灵候选，会被当成互不相干的小动作乱触发。
    """

    paired = set((pairs or {}).keys())
    assets = hotkey_assets or {}
    out: List[PropCandidate] = []
    for item in hotkeys or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        hotkey_id = str(item.get("hotkeyID") or "").strip()
        if not name or not hotkey_id or name in paired:
            continue
        if _pair_stem(name) in paired:
            continue
        if str(item.get("type") or "") != HOTKEY_ANIM:
            continue
        cls = classify_name(name, kind="oneshot", asset=assets.get(hotkey_id))
        if cls in (PROP_CLASS_LOOK, PROP_CLASS_EFFECT):
            continue
        out.append(PropCandidate(name, "oneshot", hotkey_id, cls=PROP_CLASS_GESTURE))
    return out


def prop_candidates(
    items: Optional[Iterable[Dict[str, Any]]] = None,
    pairs: Optional[Dict[str, Tuple[str, str]]] = None,
    hotkeys: Optional[Iterable[Dict[str, Any]]] = None,
    hotkey_assets: Optional[Dict[str, AssetInfo]] = None,
) -> List[PropCandidate]:
    """物品优先（屏幕上真能看到的道具），再补配对热键，最后是单发动小动作；按名字去重。"""

    out: List[PropCandidate] = []
    seen = set()
    for candidate in (
        list(item_candidates(items or []))
        + list(hotkey_candidates(pairs or {}, hotkey_assets))
        + list(oneshot_candidates(hotkeys, pairs, hotkey_assets))
    ):
        if candidate.name in seen:
            continue
        seen.add(candidate.name)
        out.append(candidate)
    return out


class PropScheduler:
    """按时间挑道具：开 -> 停几秒 -> 收，循环。纯逻辑，不碰 VTS。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        first_delay: Tuple[float, float] = (10.0, 30.0),
        interval: Tuple[float, float] = (35.0, 90.0),
        hold: Tuple[float, float] = (6.0, 12.0),
        rng: Optional[random.Random] = None,
    ) -> None:
        """初始化。

        Args:
            enabled: 总开关。
            first_delay: 引擎启动后第一次掏道具的随机延迟（秒）。
            interval: 两次道具之间的随机间隔（秒）。
            hold: 每个道具挂多久（秒）；一次性动作会用自己的短时长。
            rng: 随机源（单测可注入固定种子）。
        """

        self.enabled = bool(enabled)
        self.first_delay = first_delay
        self.interval = interval
        self.hold = hold
        self._rng = rng or random.Random()
        self._candidates: List[PropCandidate] = []
        self._active: Optional[PropCandidate] = None
        self._close_at = 0.0
        self._next_at: Optional[float] = None
        self._last_name = ""

    # ------------------------------------------------------------------ #
    # 候选
    # ------------------------------------------------------------------ #

    def set_candidates(self, candidates: Iterable[PropCandidate]) -> None:
        """换模型/重新探测后更新候选；当前挂着的道具不在清单里就不再管它。"""

        self._candidates = [item for item in candidates if item.usable]
        if self._active is not None and self._active not in self._candidates:
            self._active = None

    def candidates(self) -> List[PropCandidate]:
        return list(self._candidates)

    def drop(self, candidate: PropCandidate) -> None:
        """某个道具实测用不了：从清单里剔除，别反复试。"""

        self._candidates = [item for item in self._candidates if item != candidate]
        if self._active == candidate:
            self._active = None

    @property
    def active_name(self) -> str:
        return self._active.name if self._active is not None else ""

    def notify_manual(self, now: float, cooldown: float = 90.0) -> None:
        """AI 自己在台词里点了道具：自动调度让路一段时间。

        自动道具原本是为了「别让画面太干」，但 AI 已经明确要演某个道具时，
        再随机掏一个就是互相打架——它刚把狗尾巴草收起来，这边立刻掏出竖手指，
        观众看到的是道具乱闪，而不是"她想演什么"。

        Args:
            now: 当前时间戳（与 :meth:`tick` 同一时钟）。
            cooldown: 让路多少秒；期间不自动掏新道具。
        """

        until = float(now) + max(0.0, float(cooldown))
        current = self._next_at if self._next_at is not None else 0.0
        self._next_at = max(current, until)

    # ------------------------------------------------------------------ #
    # 调度
    # ------------------------------------------------------------------ #

    def tick(self, now: float, emotion: str = "") -> List[PropAction]:
        """推进一拍，返回要让引擎执行的动（可能为空）。"""

        actions: List[PropAction] = []
        if self._active is not None:
            if now >= self._close_at:
                actions.append(PropAction("close", self._active))
                self._active = None
                # 刚卖完萌就接着来一个，跟情绪连成一拍；否则回到常规间隔
                span = CUTE_INTERVAL if str(emotion or "").strip() in CUTE_EMOTIONS else self.interval
                self._next_at = now + self._sample(span)
            return actions

        if not self.enabled or not self._candidates:
            return actions
        if str(emotion or "").strip() in NEGATIVE_EMOTIONS:
            return actions
        if self._next_at is None:
            self._next_at = now + self._sample(self.first_delay)
            return actions
        if now < self._next_at:
            return actions

        chosen = self._choose()
        if chosen is None:
            return actions
        self._active = chosen
        self._close_at = now + self._hold_for(chosen)
        return [PropAction("open", chosen)]

    def _hold_for(self, candidate: PropCandidate) -> float:
        """这一件挂多久：一次性动作用自己的短时长，其余按配置随机。"""

        fixed = candidate.hold_seconds
        if fixed is not None:
            return float(fixed)
        return self._sample(self.hold)

    def _sample(self, span: Tuple[float, float]) -> float:
        """在区间内取对数均匀随机数。

        多数落在靠近下界的位置，偶尔拉长到上界 —— 真人动作的间隔是长尾的，
        uniform 会让每次都是"中间值"，听感/观感上就像节拍器。
        """

        lo, hi = float(span[0]), float(span[1])
        if lo <= 0.0 or hi <= lo:
            return max(0.0, lo)
        return math.exp(self._rng.uniform(math.log(lo), math.log(hi)))

    def _choose(self) -> Optional[PropCandidate]:
        pool = list(self._candidates)
        if not pool:
            return None
        # 同一个道具不连着掏两次：还有别的可选就先把上次的剔掉
        if self._last_name:
            rest = [item for item in pool if item.name != self._last_name]
            if rest:
                pool = rest
        weights = [item.cute for item in pool]
        chosen = self._rng.choices(pool, weights=weights, k=1)[0]
        self._last_name = chosen.name
        return chosen
