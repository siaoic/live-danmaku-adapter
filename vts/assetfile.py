# -*- coding: utf-8 -*-
"""按文件内容判断「这个表情/热键到底干了什么」，而不是猜名字。

为什么需要这一层
----------------
VTS 的热键与表情都是**模型作者随便起名**的：``竖手指``、``Anim 3``、``动作1``、
``exp07`` 混在一起。任何「名字关键字表」都只是赌作者的中文习惯，换一个日式/英式/
纯编号的模型就整片失效，而且反过来会把名字起得像道具、内容却是换装的键误用。

VTS 的 ``availableHotkeys`` 与 ``expressions`` 都带 ``file`` 字段，指向模型目录里
真实生效的那个文件。把这个文件读出来，就能拿到**与命名无关的客观事实**：

* ``.exp3.json`` —— Cubism 表情文件，``Parameters`` 写明它把哪些标准参数改成了多少。
  → 改的是脸（眼/眉/嘴/颊）还是身体/头发？据此区分「表情」与「换装/摆姿势」，
  并且能进一步算成 FACS AU 来标定情绪（见 :mod:`vts.expression`）。
* ``.motion3.json`` —— 骨骼/参数动画，``Meta.Duration`` + ``Meta.Loop`` +
  ``Curves`` 说明它是「一次性的小动作」还是「循环 idle」，以及动了哪些部位。
  → 一次性动作不能像道具那样长挂（很多 rig 不自动回位，会停在最后一帧）。
* ``.can3.json`` 等 —— 同源动画格式，有 ``Curves``/``Meta`` 就按动作解析，
  没有就退回「动画」这一弱结论。
* 图片 / 视频 / 音频 / ``.model3.json`` —— 不是表情也不是动作：分别是特效素材
  与换模型，一律不能进情绪轮换或自动道具。

这一层拿不到文件（VTS 装在别的机器、模型目录不在默认路径、PNGTuber 没有
exp3/motion3）时，调用方才退回名字语义。也就是说：**文件是主证据，名字只是兜底**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import json
import os

# ---- 资产类型
KIND_EXPRESSION = "expression"
KIND_MOTION = "motion"
KIND_MEDIA = "media"
KIND_MODEL = "model"
KIND_UNKNOWN = "unknown"

# ---- 部位
GROUP_FACE = "face"
GROUP_EYE = "eye"
GROUP_BROW = "brow"
GROUP_MOUTH = "mouth"
GROUP_CHEEK = "cheek"
GROUP_HEAD = "head"
GROUP_BODY = "body"
GROUP_ARM = "arm"
GROUP_HAND = "hand"
GROUP_HAIR = "hair"
GROUP_IDLE = "idle"
GROUP_POSITION = "position"
GROUP_CUSTOM = "custom"

#: 这些部位一动，就说明「脸上有戏」，可以拿去标定情绪
FACE_GROUPS = frozenset({GROUP_FACE, GROUP_EYE, GROUP_BROW, GROUP_MOUTH, GROUP_CHEEK})

#: 「摆姿势」的身体部位：身体/手臂/手/整体位移。
#: 头发单独拎出来——只动头发的循环动画是「脑袋上的小玩意在晃」（呆毛、尾巴草），
#: 不是把模型摆成另一个姿势。
POSTURE_GROUPS = frozenset({GROUP_BODY, GROUP_ARM, GROUP_HAND, GROUP_POSITION})

#: 标准参数前缀 -> 部位。顺序有讲究：ParamEyeBall* 必须先于 ParamEye* 被匹配到
#: 视线（虽然 Eye 组也能覆盖，但视线单独拎出来更利于判断「只动眼睛 = 换视线」）。
_GROUP_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("parameyeball", GROUP_EYE),
    ("parameye", GROUP_EYE),
    ("parambrow", GROUP_BROW),
    ("parammouth", GROUP_MOUTH),
    ("paramcheek", GROUP_CHEEK),
    ("paramangle", GROUP_HEAD),
    ("parambody", GROUP_BODY),
    ("paramarm", GROUP_ARM),
    ("paramhand", GROUP_HAND),
    ("paramshoulder", GROUP_BODY),
    ("parambust", GROUP_BODY),
    ("paramhair", GROUP_HAIR),
    ("parambreath", GROUP_IDLE),
    ("parambase", GROUP_POSITION),
)

#: 自定义参数里带这些词根时，视为「脸上多了一个符号层」（泪/汗/阴影/脸红…）
_FACE_HINTS: Tuple[str, ...] = (
    "tear", "namida", "涙", "泪", "泣", "blush", "cheek", "照れ", "赤面", "脸红", "红晕",
    "sweat", "汗", "あせ", "shadow", "黑脸", "青ざめ", "阴影", "dark", "eye", "mouth",
    "brow", "face", "anger", "怒", "vein", "swirl", "guru", "圈",
)

#: 按扩展名判断资产类型
_SUFFIX_KIND: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    ((".exp3.json",), KIND_EXPRESSION),
    ((".motion3.json", ".can3.json"), KIND_MOTION),
    ((".model3.json",), KIND_MODEL),
    (
        (
            ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".psd",
            ".mp4", ".webm", ".mov", ".wav", ".mp3", ".ogg", ".m4a",
            ".physics3.json", ".pose3.json", ".cdi3.json", ".userdata3.json",
        ),
        KIND_MEDIA,
    ),
)

#: 一次索引最多扫多少个文件（素材目录动辄上万，启动不能卡）
_INDEX_LIMIT = 6000


#: 自定义参数（作者自己起的 ID，如 key1/key9/LeanInput）里带这些词根时，
#: 仍然能判断出它动的是哪个部位——参数名比表情名稳定得多。
_CUSTOM_TOKENS: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("lean", "body", "bend", "sit", "pose"), GROUP_BODY),
    (("arm", "hand", "finger", "shoulder"), GROUP_ARM),
    (("hair", "bangs", "ahoge"), GROUP_HAIR),
    (("head", "angle"), GROUP_HEAD),
    (("breath", "idle"), GROUP_IDLE),
)


def group_of_param(param_id: str) -> str:
    """标准参数 ID -> 部位；认不出来的归 ``custom``。"""

    key = str(param_id or "").strip().lower()
    if not key:
        return GROUP_CUSTOM
    for prefix, group in _GROUP_PREFIXES:
        if key.startswith(prefix):
            return group
    for tokens, group in _CUSTOM_TOKENS:
        if any(token in key for token in tokens):
            return group
    if any(hint in key for hint in _FACE_HINTS):
        return GROUP_FACE
    return GROUP_CUSTOM


def groups_of_params(params: Iterable[str]) -> frozenset:
    """一组参数 ID -> 动到的部位集合。"""

    return frozenset(group_of_param(pid) for pid in params or [] if str(pid or "").strip())


@dataclass(frozen=True)
class AssetInfo:
    """一个资产文件解析出来的客观事实。"""

    name: str
    kind: str = KIND_UNKNOWN
    groups: frozenset = frozenset()
    params: Dict[str, float] = field(default_factory=dict)
    duration: float = 0.0
    loop: bool = False
    curve_count: int = 0
    reason: str = ""

    @property
    def touches_face(self) -> bool:
        return bool(self.groups & FACE_GROUPS)

    @property
    def only_body(self) -> bool:
        """只动身体/头发/位置这类——是换装或摆姿势，不是情绪。

        注意 ``custom``（作者自定义 ID，如 ``key1``/``key9``）**不算**身体：
        实测示例模型的「流泪」「黑脸」就是只翻一个 artmesh 开关参数，按「动了身体」
        处理会把 7 张脸全判成展示型。自定义参数必须回到名字语义去判断。
        """

        known = self.groups - {GROUP_CUSTOM}
        return bool(known) and not self.touches_face

    @property
    def is_posture(self) -> bool:
        """只把身体/手臂/整体挪一挪：换姿势，不是「脑袋上挂着的小玩意」。"""

        return bool(self.groups & POSTURE_GROUPS) and not self.touches_face

    @property
    def is_one_shot(self) -> bool:
        """一次性小动作：有明确时长、不循环。"""

        return self.kind == KIND_MOTION and not self.loop

    def describe(self) -> str:
        if self.kind == KIND_EXPRESSION:
            return f"{self.name} 表情文件（部位 {'/'.join(sorted(self.groups)) or '无'}）"
        if self.kind == KIND_MOTION:
            return "%s 动作文件（%.1fs%s，曲线 %d）" % (
                self.name, self.duration, "，循环" if self.loop else "", self.curve_count
            )
        return f"{self.name} {self.kind}"


def kind_of_file(name: str) -> str:
    """按文件名后缀判断资产类型（不对就返回 unknown，交给内容判定）。"""

    lowered = str(name or "").strip().lower()
    for suffixes, kind in _SUFFIX_KIND:
        if any(lowered.endswith(suffix) for suffix in suffixes):
            return kind
    return KIND_UNKNOWN


def parse_expression_params(doc: Any) -> Dict[str, float]:
    """解析 .exp3.json 的 ``Parameters``（忽略 Multiply 与坏数据）。"""

    if not isinstance(doc, dict):
        return {}
    raw = doc.get("Parameters")
    if not isinstance(raw, list):
        return {}
    params: Dict[str, float] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("Id") or item.get("id") or "").strip()
        if not pid:
            continue
        try:
            value = float(item.get("Value", item.get("value", 0.0)))
        except (TypeError, ValueError):
            continue
        if str(item.get("Blend") or "").lower() == "multiply":
            continue
        if pid not in params or abs(value) > abs(params[pid]):
            params[pid] = value
    return params


def parse_motion_document(doc: Any) -> Tuple[float, bool, Dict[str, float], int]:
    """解析 .motion3.json / .can3.json。

    Returns:
        (时长, 是否循环, 曲线ID -> 该曲线绝对值最大的取值, 曲线数)

    曲线只用来判断「动了哪些部位」，不需要还原贝塞尔，所以只取 ``Segments`` 里
    的极值；段格式是 ``[t0, v0, ...]``（线性）或带贝塞尔控制点，数值前半是时间
    后半是值，这里按位置取偶数位不够稳，改用「所有数值取绝对值最大」——
    对「有没有动到这条参数」这个判断来说足够了。
    """

    if not isinstance(doc, dict):
        return 0.0, False, {}, 0
    meta = doc.get("Meta") if isinstance(doc.get("Meta"), dict) else {}
    try:
        duration = float(meta.get("Duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    loop = bool(meta.get("Loop") or False)

    curves: Dict[str, float] = {}
    raw_curves = doc.get("Curves")
    count = 0
    if isinstance(raw_curves, list):
        for curve in raw_curves:
            if not isinstance(curve, dict):
                continue
            if str(curve.get("Target") or "").lower() not in ("parameter", ""):
                continue
            pid = str(curve.get("Id") or "").strip()
            if not pid:
                continue
            count += 1
            peak = 0.0
            for value in curve.get("Segments") or []:
                try:
                    peak = max(peak, abs(float(value)))
                except (TypeError, ValueError):
                    continue
            curves[pid] = peak
    return duration, loop, curves, count


def analyze_document(name: str, doc: Any) -> AssetInfo:
    """把已读入的 JSON 文档判定成 :class:`AssetInfo`。"""

    kind = kind_of_file(name)
    if isinstance(doc, dict) and "Curves" in doc and kind in (KIND_UNKNOWN, KIND_MOTION):
        kind = KIND_MOTION
    elif isinstance(doc, dict) and "Parameters" in doc and kind == KIND_UNKNOWN:
        kind = KIND_EXPRESSION

    if kind == KIND_EXPRESSION:
        params = parse_expression_params(doc)
        groups = groups_of_params(params)
        return AssetInfo(
            name=name, kind=kind, groups=groups, params=params,
            reason="表情文件：%d 个参数" % len(params),
        )
    if kind == KIND_MOTION:
        duration, loop, curves, count = parse_motion_document(doc)
        groups = groups_of_params(curves)
        return AssetInfo(
            name=name, kind=kind, groups=groups, duration=duration, loop=loop,
            curve_count=count, reason="动作文件：%d 条曲线" % count,
        )
    if kind in (KIND_MEDIA, KIND_MODEL):
        return AssetInfo(name=name, kind=kind, reason="素材/模型文件，不是表情动作")
    return AssetInfo(name=name, kind=KIND_UNKNOWN, reason="无法识别的文件类型")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001 - 坏文件就当读不到
            return None


class FileIndex:
    """模型目录的文件索引：按文件名找资产并解析（带缓存）。

    引擎每次建清单/换皮时建一份，之后查询都是字典查找，不会重复扫盘。
    """

    def __init__(
        self,
        model_name: str = "",
        roots: Optional[Sequence[Path]] = None,
        limit: int = _INDEX_LIMIT,
    ) -> None:
        """初始化。

        Args:
            model_name: 当前模型名，优先在 ``<root>/<model_name>/`` 下索引。
            roots: 模型根目录；None 时用 :func:`default_model_roots`。
            limit: 最多索引多少个文件。
        """

        self.model_name = str(model_name or "").strip()
        self._roots = list(roots) if roots is not None else default_model_roots()
        self._limit = max(1, int(limit))
        self._index: Optional[Dict[str, Path]] = None
        self._cache: Dict[str, Optional[AssetInfo]] = {}
        self._dirs: List[Path] = []

    # ---- 索引
    @property
    def dirs(self) -> List[Path]:
        """实际索引到的模型目录（日志/排查用）。"""

        self._build()
        return list(self._dirs)

    def _build(self) -> None:
        if self._index is not None:
            return
        index: Dict[str, Path] = {}
        dirs: List[Path] = []
        budget = self._limit
        for root in self._roots:
            # 先精确定位「VTS 显示名 -> 实际文件夹」；定位到了就只索引那个目录，
            # 避免在十几个模型的贴图目录里翻几十万个文件。
            resolved = resolve_model_dir(Path(root), self.model_name) if self.model_name else None
            bases: List[Path] = [resolved] if resolved is not None else [Path(root)]
            for base in bases:
                try:
                    if not base.is_dir():
                        continue
                except OSError:
                    continue
                dirs.append(base)
                try:
                    iterator = base.rglob("*")
                except OSError:
                    continue
                for path in iterator:
                    budget -= 1
                    if budget <= 0:
                        break
                    try:
                        if not path.is_file():
                            continue
                    except OSError:
                        continue
                    name = path.name
                    # 同名文件以先索引到的为准（模型专属目录排在根目录前面）
                    index.setdefault(name, path)
                    # 相对路径也进索引：热键的 file 常带子目录（expressions/xxx.exp3.json）
                    try:
                        rel = path.relative_to(base).as_posix()
                    except ValueError:
                        rel = ""
                    if rel:
                        index.setdefault(rel, path)
                        index.setdefault(rel.lower(), path)
        self._index = index
        self._dirs = dirs

    def find(self, name: str) -> Optional[Path]:
        """按「热键里的 file 字段」找文件：先试原样相对路径，再试文件名。"""

        target = str(name or "").strip().replace("\\", "/")
        if not target:
            return None
        self._build()
        assert self._index is not None
        for key in (target, target.lower(), target.rsplit("/", 1)[-1]):
            path = self._index.get(key)
            if path is not None:
                return path
        # 兜底：大小写/分隔符完全不同的情况，按文件名小写再找一次
        base = target.rsplit("/", 1)[-1].lower()
        for key, path in self._index.items():
            if key.lower() == base:
                return path
        return None

    def analyze(self, name: str) -> Optional[AssetInfo]:
        """解析一个文件；找不到或读不动返回 None（调用方退回名字语义）。"""

        target = str(name or "").strip()
        if not target:
            return None
        if target in self._cache:
            return self._cache[target]
        path = self.find(target)
        info: Optional[AssetInfo] = None
        if path is not None:
            doc = _read_json(path)
            if doc is not None:
                info = analyze_document(path.name, doc)
        self._cache[target] = info
        return info

    def analyze_many(self, names: Iterable[str]) -> Dict[str, AssetInfo]:
        """批量解析，键是**传入的原名**（保持调用方的映射关系）。"""

        out: Dict[str, AssetInfo] = {}
        for name in names:
            info = self.analyze(str(name or ""))
            if info is not None:
                out[str(name)] = info
        return out


def default_model_roots() -> List[Path]:
    """VTS 可能存放模型的目录；找不到就返回空列表（安静降级）。

    覆盖三种安装方式：
    * Steam/独立版把数据放在安装目录的 ``StreamingAssets`` 里
      （实测：``.../VTube Studio/VTube Studio_Data/StreamingAssets/Live2DModels``）；
    * 老版本/便携版放在 ``%APPDATA%\\VTube Studio\\Live2DModels``；
    * 用户自定义目录（自己填环境变量 ``VTS_MODEL_DIRS``，多个用 ``os.pathsep`` 分隔）。
    """

    roots: List[Path] = []
    for chunk in str(os.environ.get("VTS_MODEL_DIRS") or "").split(os.pathsep):
        chunk = chunk.strip()
        if chunk:
            roots.append(Path(chunk))
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "VTube Studio" / "Live2DModels")
    home = Path.home()
    roots.append(home / "Library/Application Support/VTube Studio/Live2DModels")
    roots.append(home / "AppData/Roaming/VTube Studio/Live2DModels")
    try:  # 装在非默认盘时，模型在安装目录的 StreamingAssets 里
        from .discovery import find_vts_executable  # 延迟导入，避免拉起重依赖

        exe = find_vts_executable()
        if exe is not None:
            install = Path(exe).parent
            roots.append(install / "StreamingAssets" / "Live2DModels")
            roots.append(install / "VTube Studio_Data" / "StreamingAssets" / "Live2DModels")
            roots.append(install / "Live2DModels")
            roots.append(install.parent / "Live2DModels")
            # Unity 的 <App>_Data 名字随版本变，兜一层通配
            try:
                for data_dir in install.glob("*_Data"):
                    roots.append(data_dir / "StreamingAssets" / "Live2DModels")
            except OSError:
                pass
    except Exception:  # noqa: BLE001 - 探测失败不影响表情层
        pass
    seen: List[Path] = []
    for root in roots:
        try:
            if root.exists() and root not in seen:
                seen.append(root)
        except OSError:
            continue
    return seen


def resolve_model_dir(root: Path, model_name: str) -> Optional[Path]:
    """在模型根目录里定位「VTS 显示名 -> 实际文件夹」。

    VTS 显示的模型名来自 ``*.vtube.json`` 的 ``Name``，和目录名经常不一致
    （实测：显示「示例模型」，目录叫 ``Example_vts``）。只按目录名找会整片落空，
    所以先试同名目录，再读每个子目录里的 ``*.vtube.json`` 比对 ``Name``。
    """

    name = str(model_name or "").strip()
    if not name:
        return None
    direct = Path(root) / name
    try:
        if direct.is_dir():
            return direct
    except OSError:
        pass
    try:
        entries = [p for p in Path(root).iterdir() if p.is_dir()]
    except OSError:
        return None
    for entry in entries:
        try:
            configs = list(entry.glob("*.vtube.json"))
        except OSError:
            continue
        for config in configs:
            doc = _read_json(config)
            if isinstance(doc, dict) and str(doc.get("Name") or "").strip() == name:
                return entry
    return None

