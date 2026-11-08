# -*- coding: utf-8 -*-
"""有机演出引擎（不播放 VTS 待机动作，直接合成参数并注入 VTube Studio）。

移植自参考插件 astrbot_plugin_live_stream_companion 的 performance_engine.py ——
这一套才是"让 VTS 模型一直在动"的演出引擎（另一套 vts_client.py 只是官方 API 封装）。

设计目标：参考视频那种"一直在动、但不重复"的活人感。

层结构（全部在语义参数空间相加，再映射到 VTS 追踪输入注入）：
  base    动作库采样：从 de/1/2.motion3 随机挑片段，各自随机速度/相位，
          平滑权重交叉淡化，每 8~15 秒重新挑源 —— 动作"像"作者做的，但不会循环重播
  micro   程序化生命感：呼吸、缓慢摇摆（value noise）、随机眨眼（含二次眨眼）
  react   cortico 手势脉冲（点头/摇头/歪头/眨眼/雀跃…），按标签触发，包络混合
  gaze    鼠标位置 → 眼球 + 少量头部跟随，平滑跟手
  emotion 标签情绪偏置：嘴角、眉毛、眼睑，带起落包络

约束（实测）：
  * VTS 拒绝注入 Live2D 参数（错误 453），只能注入追踪输入；
  * VTS 播放动画时动画值优先，因此本引擎运行时不触发任何 VTS 动作热键；
  * 一个输入驱动多个输出（FaceAngleX→ParamAngleX + ParamBodyAngleX），
    身体角度只能跟头部耦合——这本来就是 VTS 的映射方式，视觉上自然。

相对参考实现的改动（本插件不依赖 AstrBot）：
  * token 由插件通过 --token 传入，不再读 AstrBot 的 sqlite 数据库；
  * VTS 的 host/port 可配（本机 127.0.0.1:8001 可能被别的程序占用）；
  * 日志改 stdlib logging，不再往插件目录写 .log 文件；
  * 动作文件缺哪个就跳过哪个，不强制三个都在。
"""
import json, math, os, random, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import logging

import numpy as np
import websocket

# 引擎以子进程运行，cwd 是 engine/ 目录，插件根目录不在 sys.path 上；
# 手动补进去，才能 import 插件根下的 vts 包（表情匹配逻辑与插件共用一份）。
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from emotion_pose import DEFAULT_SUSTAIN_SECONDS, decayed, pose_for
from vts.expression import (
    DEFAULT_HOLD_SECONDS,
    ExpressionCatalog,
    ExpressionState,
    asset_map_for,
    match_prop,
    read_asset_infos,
)
from vts.assetfile import FileIndex
from vts.props import PropScheduler, prop_candidates
from vts.timing import plan_for_text

logger = logging.getLogger("plugin.live_stream_companion.engine")

# ---- 运行参数（由插件以子进程方式启动时通过命令行传入）----
import argparse as _argparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_ap = _argparse.ArgumentParser(add_help=False)
_ap.add_argument("--port", type=int, default=int(os.environ.get("PERF_ENGINE_PORT", "18766")))
_ap.add_argument("--clips", default=os.environ.get("PERF_ENGINE_CLIPS", os.path.join(_HERE, "cortico_clips.json")))
_ap.add_argument("--vts-host", default=os.environ.get("PERF_ENGINE_VTS_HOST", "127.0.0.1"))
_ap.add_argument("--vts-port", type=int, default=int(os.environ.get("PERF_ENGINE_VTS_PORT", "8001")))
_ap.add_argument("--token", default=os.environ.get("PERF_ENGINE_VTS_TOKEN", ""))
_ap.add_argument("--probe", type=int, default=int(os.environ.get("PERF_ENGINE_PROBE", "0")))
_ap.add_argument("--auto-props", type=int, default=int(os.environ.get("PERF_ENGINE_AUTO_PROPS", "1")))
_ap.add_argument("--plugin-name", default=os.environ.get("PERF_ENGINE_PLUGIN_NAME", "MaiBot Live Stream Companion"))
_ap.add_argument("--developer", default=os.environ.get("PERF_ENGINE_DEVELOPER", "MaiBot"))
_ARGS, _unknown = _ap.parse_known_args()

PACK = os.path.dirname(_ARGS.clips)
PORT = _ARGS.port
FPS = 60
PLUGIN = (_ARGS.plugin_name, _ARGS.developer)

# ---------------------------------------------------------------- 曲线解析
# ---------------------------------------------------------------- 语义量映射
# Live2D 输出 -> VTS 追踪输入（自 yumi.vtube.json 的 ParameterSettings 反解）
def l2d_to_inputs(vals):
    g = lambda k, d=0.0: vals.get(k, d)
    out = {}
    if "ParamAngleX" in vals:
        out["FaceAngleX"] = float(np.clip(g("ParamAngleX"), -30, 30))
    if "ParamAngleY" in vals:
        # /1.2 会把点头压得很小；/1.05 保留更多纵向表达
        out["FaceAngleY"] = float(np.clip(g("ParamAngleY") / 1.05, -28, 28))
    if "ParamAngleZ" in vals:
        # 歪头同理：原来除 0.857 等于又压一次，改为直接放大
        out["FaceAngleZ"] = float(np.clip(g("ParamAngleZ") * 1.15, -38, 38))
    if "ParamMouthForm" in vals:
        out["MouthSmile"] = float(np.clip(g("ParamMouthForm") / 3.6 + 0.5, 0, 1))
    if "ParamEyeOpenL" in vals:
        out["EyeOpenLeft"] = float(np.clip(g("ParamEyeOpenL") / 1.7, 0, 1))
    if "ParamEyeOpenR" in vals:
        out["EyeOpenRight"] = float(np.clip(g("ParamEyeOpenR") / 1.7, 0, 1))
    # 这台模型的映射是 EyeRightX/Y -> ParamEyeBallX/Y（且 X 反向），
    # 注入 EyeLeftX/Y 不会生效（实测眼球行程为 0）
    if "ParamEyeBallX" in vals:
        v = float(np.clip((1.0 - g("ParamEyeBallX")) / 2.0, -1, 1))
        out["EyeRightX"] = v
        out["EyeLeftX"] = v
    if "ParamEyeBallY" in vals:
        v = float(np.clip(g("ParamEyeBallY"), -1, 1))
        out["EyeRightY"] = v
        out["EyeLeftY"] = v
    if "ParamBrowYL" in vals:
        out["BrowLeftY"] = float(np.clip((g("ParamBrowYL") + 1) / 2, 0, 1))
    if "ParamBrowYR" in vals:
        out["BrowRightY"] = float(np.clip((g("ParamBrowYR") + 1) / 2, 0, 1))
    if "ParamCheekpuff" in vals:
        out["CheekPuff"] = float(np.clip(g("ParamCheekpuff") / 1.4, 0, 1))
    if "ParamMouthX" in vals:
        out["MouthX"] = float(np.clip(g("ParamMouthX") / 1.1, -1, 1))
    return out

# 手势/情绪词 -> cortico clip
TAG_TO_CLIP = {
    "nod": "nod", "shake": "shake", "tilt": "tilt_curious", "wink": "wink",
    "bounce": "excited_bounce", "wide": "eyes_wide", "pout": "pout_puff",
    "smug": "smug", "laugh": "laugh", "star": "excited_bounce",
    "surprise": "surprised", "sleepy": "sleepy", "angry": "angry",
    "sad": "pitiful", "love": "smile", "mic": "smile", "dog": "excited_bounce",
    "eyeroll": "eyeroll",
}
# 台本记号里模型可能写中文，这里给出每个 tag 的中文别名（写进提示词词表）
TAG_LABELS = {
    "nod": ("点头",), "shake": ("摇头",), "tilt": ("歪头",), "wink": ("眨眼", "眨个眼", "抛媚眼"),
    "bounce": ("跳一下", "雀跃"), "wide": ("睁大眼",), "pout": ("鼓嘴", "嘟嘴"),
    "smug": ("得意",), "laugh": ("笑", "大笑"), "star": ("星星眼",),
    "surprise": ("惊讶",), "sleepy": ("困", "打哈欠"), "angry": ("生气",),
    "sad": ("难过",), "love": ("比心", "爱心"), "mic": ("拿麦",), "dog": ("小狗",),
    "eyeroll": ("翻白眼", "白眼"),
}
# 情绪保持型（sustain）
TAG_TO_SUSTAIN = {
    "smile": "smile", "laugh": "laugh", "angry": "angry", "sad": "pitiful",
    "surprise": "surprised", "sleepy": "sleepy", "love": "smile",
}

def log(*a):
    logger.info(" ".join(str(x) for x in a))

# ---------------------------------------------------------------- VTS
def token():
    """认证 Token 由插件在自己的配置里保存，启动本进程时通过 --token 传入。"""

    return (_ARGS.token or "").strip() or None


class VTS:
    def __init__(self):
        self.ws = None; self.valid_inputs = set(); self._inputs_at = 0.0
        # 主循环（每帧注入）和 HTTP 控制线程（表情/道具）共用一条连接。
        # VTS 对 InjectParameterDataRequest 每帧回一个 ACK，且响应**不保证**按请求
        # 顺序返回（实测 inj1 会排在 inj4 后面），两个线程各自 send/recv 就会错配：
        # 热键请求拿到的是注入的 ACK，判定为失败，表情也就点不动。
        # 所以把「一问一答」整段锁起来，代价是每帧多几百微秒，接受。
        self._io = threading.Lock()
        self.connect()
    def live2d_snapshot(self):
        """取一次 Live2D 输出参数快照（用于探测影响力/校验效果）。"""

        r = self.call("Live2DParameterListRequest")
        if not r:
            return None
        params = ((r.get("data") or {}).get("parameters")) or []
        return {p["name"]: float(p.get("value", 0) or 0) for p in params if p.get("name")}

    @property
    def profile(self):
        """当前模型的输入参数集合。"""
        return ModelProfile(self.valid_inputs)

    def refresh_inputs(self, force=False):
        now = time.time()
        if not force and self.valid_inputs and now - self._inputs_at < 60:
            return
        r = self.call("InputParameterListRequest")
        d = (r or {}).get("data") or {}
        names = [p.get("name") for p in (d.get("defaultParameters") or []) + (d.get("customParameters") or [])]
        if names:
            self.valid_inputs = {n for n in names if n}
            self._inputs_at = now
            log("valid inputs:", len(self.valid_inputs))
    def connect(self):
        # 认证失败的冷却：VTS 里 token 被吊销时，逐帧重连只会把日志刷爆、模型一动不动，
        # 所以失败后 10 秒内不重试，并把原因记下来供插件查询。
        if time.time() < getattr(self, "_retry_at", 0.0):
            return False
        try:
            url = "ws://%s:%d" % (_ARGS.vts_host, _ARGS.vts_port)
            ws = websocket.create_connection(url, timeout=15)
            ws.send(json.dumps({"apiName":"VTubeStudioPublicAPI","apiVersion":"1.0","requestID":"a",
                "messageType":"AuthenticationRequest",
                "data":{"pluginName":PLUGIN[0],"pluginDeveloper":PLUGIN[1],"authenticationToken":token()}}))
            r = json.loads(ws.recv())
            if not r.get("data",{}).get("authenticated"):
                reason = json.dumps(r.get("data"), ensure_ascii=False)[:120]
                log("auth failed", reason)
                self.last_error = "VTS 认证失败（token 可能已被吊销）：" + reason
                self._retry_at = time.time() + 10.0
                return False
            self.ws = ws
            self.last_error = ""
            self._last_conn_err = ""
            log("VTS connected")
            return True
        except Exception as e:
            self.ws = None
            msg = repr(e)[:90]
            self.last_error = "连不上 VTS：%s" % msg
            # 403 基本都是端口被别的程序抢了：VTS 的 API 端口默认 8001，
            # 而 MaiBot 自己的 HTTP 服务（[http] port）也默认 8001 并显式绑 127.0.0.1，
            # Windows 下「更具体的 127.0.0.1 绑定」优先，于是连接打到了 MaiBot 的 uvicorn 上，
            # 它当然拒绝 WebSocket 升级。这种情况和 token 无关，重连多少次都没用。
            if "403" in msg:
                self.last_error = (
                    "VTS 握手被拒(403)：本机 %s 端口可能被别的程序占用"
                    "（MaiBot 的 [http] port 默认也是 8001，且绑 127.0.0.1 优先于 VTS 的 0.0.0.0）。"
                    "把 MaiBot 的 [http] port 改成 8010 之类再重启，VTS 保持 8001 即可。"
                    % _ARGS.vts_port
                )
            # 冷却：连不上也别逐帧重试，否则日志一秒钟刷几十行
            self._retry_at = time.time() + 10.0
            if msg != getattr(self, "_last_conn_err", ""):
                log("connect err", msg)
                self._last_conn_err = msg
            return False
    def call(self, msg, data=None):
        with self._io:
            if self.ws is None and not self.connect(): return None
            try:
                self.ws.send(json.dumps({"apiName":"VTubeStudioPublicAPI","apiVersion":"1.0","requestID":"x",
                    "messageType":msg,"data":data or {}}))
                r = json.loads(self.ws.recv())
                # 不是自己的响应说明连接状态已经错位：丢掉重连，
                # 否则之后每个请求都会拿到上一条的应答
                if r.get("messageType", "").replace("Response", "") not in (msg.replace("Request", ""), "APIError"):
                    log("call desync", msg, "->", r.get("messageType"))
                    try: self.ws.close()
                    except Exception: pass
                    self.ws = None
                    return None
                return r
            except Exception as e:
                log("call err", repr(e)[:70])
                try: self.ws.close()
                except Exception: pass
                self.ws=None; return None
    def inject(self, values, activations=None):
        # refresh_inputs 内部会走 call()，这里不能重复加锁（普通 Lock 不可重入）
        self.refresh_inputs()
        # 只发 VTS 真的存在的输入；未知名字会让整批注入被拒（引擎看起来"不动"）
        vals = [{"id": k, "value": float(v)} for k, v in values.items()
                if not self.valid_inputs or k in self.valid_inputs]
        if not vals and not activations: return
        if self.ws is None and not self.connect():
            # 连不上就别往下走：call() 会再试一次，白刷日志
            return
        r = self.call("InjectParameterDataRequest", {
            "faceFound": True, "mode": "set", "parameterValues": vals})
        if r and r.get("messageType") == "APIError":
            log("inject rejected:", json.dumps(r.get("data"))[:120])
            self.refresh_inputs(force=True)

    def set_expression_state(self, expression_file, active, fade_time=0.22):
        """开关一个表情。

        实测这个 VTS 版本的 ExpressionActivationRequest **只接受单个
        expressionFile**（必须带 .exp3.json 后缀）；传 activations 数组会报
        APIError 650 并整条丢弃。所以一次只发一个表情。
        """

        if not expression_file:
            return False
        r = self.call("ExpressionActivationRequest", {
            "expressionFile": expression_file,
            "active": bool(active),
            "fadeTime": float(fade_time),
        })
        if r and r.get("messageType") == "APIError":
            log("expression rejected:", json.dumps(r.get("data"), ensure_ascii=False)[:160])
            return False
        return bool(r)

    # ---- 表情清单：换模型后重启引擎会自动重建
    def load_catalog(self):
        """读取当前模型的表情与热键，建一份清单供匹配。"""

        model = self.call("CurrentModelRequest") or {}
        model_name = str(((model.get("data") or {}).get("modelName")) or "")
        expr = self.call("ExpressionStateRequest", {"details": False}) or {}
        items = [e for e in (((expr.get("data") or {}).get("expressions")) or []) if isinstance(e, dict)]
        names = [str(e.get("name")) for e in items if e.get("name")]
        # 激活接口认的是文件名（必须带 .exp3.json），所以要把 name -> file 一起存下来
        files = {str(e.get("name")): str(e.get("file")) for e in items if e.get("name") and e.get("file")}
        hk = self.call("HotkeysInCurrentModelRequest") or {}
        hotkeys = ((hk.get("data") or {}).get("availableHotkeys")) or []
        # 表情文件里的 Parameters 是唯一与「名字叫什么语言」无关的证据：
        # 把 .exp3.json 从 VTS 模型目录读出来，翻译成 FACS AU 向量来标定情绪。
        # 读不到（模型不在默认路径 / 是 PNGTuber）就退回名字语义，不影响功能。
        index = FileIndex(model_name=model_name)
        assets = read_asset_infos(files.values(), model_name, index=index)
        # 热键同理：读它指向的 .exp3/.motion3，看它到底动了哪里，
        # 而不是靠「竖手指」「动作1」这种名字去猜。
        hotkey_assets = {}
        for item in hotkeys:
            if not isinstance(item, dict):
                continue
            hotkey_id = str(item.get("hotkeyID") or "").strip()
            target = str(item.get("file") or "").strip()
            if not hotkey_id or not target:
                continue
            info = index.analyze(target)
            if info is not None:
                hotkey_assets[hotkey_id] = info
        params = {name: info.params for name, info in assets.items() if info.params}
        catalog = ExpressionCatalog(
            model_name=model_name,
            expressions=names,
            hotkeys=hotkeys,
            expression_files=files,
            expression_params=params,
            expression_assets=assets,
            hotkey_assets=hotkey_assets,
            asset_map=asset_map_for(model_name),
        )
        log(
            "expression catalog:", catalog.describe(),
            "文件已解析", len(assets), "/", len(files),
            "热键文件", len(hotkey_assets), "/", len(hotkeys),
        )
        return catalog
        for line in catalog.calibration_report():
            log("  calibration:", line)
        if catalog.unknown_names:
            log("未识别语义的表情（不参与情绪轮换，可在 vts/emotion_assets.json 手动映射）:",
                catalog.unknown_names[:8])
        return catalog

    def trigger_hotkey(self, hotkey_id):
        """触发一个 VTS 热键（只用于动画道具的开/关，不用于表情轮换）。"""

        if not hotkey_id:
            return False
        r = self.call("HotkeyTriggerRequest", {"hotkeyID": hotkey_id})
        return bool(r) and r.get("messageType") == "HotkeyTriggerResponse"

    def current_model(self):
        """当前模型的信息（modelName 用来判断换皮）。"""

        r = self.call("CurrentModelRequest") or {}
        return r.get("data") or {}

    # ---- 物品（道具）：不依赖模型热键，任何皮套只要有可加载物品就能用
    def list_items(self):
        """列出可以加载的物品文件（VTS 的 ItemListRequest 返回的是 availableItemFiles）。"""

        r = self.call("ItemListRequest") or {}
        data = (r.get("data") or {})
        return (data.get("availableItemFiles")) or []

    def load_item(self, file_name_or_id):
        """把一件物品挂到模型上（位置/大小用 VTS 里保存的默认值）。"""

        ref = str(file_name_or_id or "").strip()
        if not ref:
            return False
        r = self.call("ItemLoadRequest", {"fileName": ref, "fadeTime": 1.0})
        if r and r.get("messageType") == "APIError":
            log("item load rejected:", json.dumps(r.get("data"), ensure_ascii=False)[:120])
            return False
        return bool(r) and r.get("messageType") == "ItemLoadResponse"

    def unload_item(self, instance_id):
        """把挂上去的物品收回（instanceID 从加载响应里拿不到时退回用文件名）。"""

        ref = str(instance_id or "").strip()
        if not ref:
            return False
        r = self.call("ItemUnloadRequest", {"itemInstanceIDs": [ref]})
        return bool(r) and r.get("messageType") == "ItemUnloadResponse"

# ---------------------------------------------------------------- 模型能力探测
class ModelProfile:
    """自适应：探测当前模型支持哪些参数，据此调整表现策略。

    实测各模型差异极大（同一台 VTS 上）：
      * DeepSeek_vts  32 个输入、无身体、有眉毛
      * hijiki_vts    10 个输入、无身体、无眉毛
      * wanko_vts      8 个输入、无身体、无眉毛
      * yumi_vts      18 个输入、有身体、有眉毛
    写死一套参数名会在别的模型上全部失效（VTS 对整批无效参数直接报 453），
    所以这里在连接后探测一次，把"这个模型能做什么"记下来。
    """

    #: VTS 的默认输入参数全集（用于判断模型支持哪些）
    ALL_INPUTS = None  # 运行时从 VTS 拉

    def __init__(self, valid_inputs, default_inputs=None):
        self.valid = set(valid_inputs or ())
        self.default = set(default_inputs or ())

    @property
    def empty(self):
        return not self.valid

    def has(self, *names):
        """至少有一个名字可用。"""
        return any(n in self.valid for n in names)

    def first(self, *names):
        """返回第一个可用的名字，都没有则返回 None。"""
        for n in names:
            if n in self.valid:
                return n
        return None

    # ---- 具体能力 ----

    @property
    def can_brow(self):
        return self.has("BrowLeftY", "BrowRightY")

    @property
    def can_eye_ball(self):
        return self.has("EyeLeftX", "EyeRightX")

    @property
    def can_mouth_x(self):
        return self.has("MouthX")

    @property
    def can_cheek(self):
        return self.has("CheekPuff")

    @property
    def can_body(self):
        """模型是否能被 Mocopi 类输入独立驱动身体（多数模型其实不能）。"""
        return self.has("MocopiBodyAngleX", "MocopiBodyAngleZ", "MocopiHipAngleZ")

    @property
    def brow_names(self):
        return tuple(n for n in ("BrowLeftY", "BrowRightY") if n in self.valid)

    def describe(self):
        feats = []
        if self.can_brow:
            feats.append("眉毛")
        if self.can_eye_ball:
            feats.append("眼球")
        if self.can_mouth_x:
            feats.append("嘴形")
        if self.can_cheek:
            feats.append("脸颊")
        if self.can_body:
            feats.append("Mocopi身体")
        return "%d 个输入 / %d 项增强能力（%s）" % (
            len(self.valid), len(feats), "、".join(feats) or "仅基础")

class ModelProbe:
    """自适应：实测每个输入参数对当前模型的实际影响力，据此缩放输出幅度。

    为什么不能只看「参数是否存在」——实测证明这是错的：
      VTS 的 InputParameterListRequest 对任何模型都返回全部 127 个默认参数，
      而且注入未在模型里映射的参数**依然会**通过 VTS 的默认映射与物理引擎产生
      效果（在最简模型 wanko 上，BrowLeftY 仍能造成 1.21 的 Live2D 变化）。
    所以「无效参数」并不存在，存在的只是"效果强弱不同"。

    真正有意义的自适应是：连上后花 ~2 秒实测每个通道的影响力，
    影响力弱的通道就少给幅度（省得白动），影响力强的给足。
    """

    #: 需要探测的通道 -> 探测值。
    #: 幅度只给到合法范围的一半左右：探测本身也会被用户看见，给满就像甩头，
    #: 而且信噪比减半后阈值（0.03）也跟着调，足够分辨"吃得动/吃不动"。
    PROBES = (
        ("FaceAngleX", 10.0), ("FaceAngleY", 8.0), ("FaceAngleZ", 8.0),
        ("EyeLeftX", 0.6), ("EyeRightX", 0.6), ("EyeOpenLeft", 0.3),
        ("MouthSmile", 0.7), ("MouthX", 0.6), ("BrowLeftY", 0.6), ("BrowRightY", 0.6),
        ("CheekPuff", 0.6), ("MocopiBodyAngleX", 10.0), ("MocopiHipAngleZ", 10.0),
    )

    #: 输入名 -> 期望的 Live2D 输出关键片段（用于跨模型命名匹配）
    _LIKE = {
        "FaceAngleX": "anglex", "FaceAngleY": "angley", "FaceAngleZ": "anglez",
        "EyeLeftX": "eyeballx", "EyeRightX": "eyeballx",
        "EyeOpenLeft": "eyelopen", "MouthSmile": "mouthform",
        "MouthX": "mouthx", "BrowLeftY": "browly", "BrowRightY": "browry",
        "CheekPuff": "cheek", "MocopiBodyAngleX": "bodyanglex",
        "MocopiHipAngleZ": "hip",
    }

    @classmethod
    def _match_like(cls, input_name):
        """把输入参数名映射成 Live2D 输出名里应出现的关键片段。"""

        if input_name in cls._LIKE:
            return cls._LIKE[input_name]
        # 兜底：去掉 Face/Mocopi 前缀后小写比较
        base = input_name.lower().replace("mocopi", "")
        return base

    @staticmethod
    def _key_matches(live2d_name, like):
        """大小写/下划线无关的包含判断。"""

        normalized = live2d_name.lower().replace("_", "")
        return like in normalized

    def __init__(self, strengths):
        self.strengths = dict(strengths or {})

    @classmethod
    def measure(cls, vts):
        """实测各通道影响力。返回值越大说明该通道对该模型越"吃得动"。

        Returns:
            ModelProbe: 已测好的档案。
        """

        strengths = {}
        for name, value in cls.PROBES:
            try:
                base = vts.live2d_snapshot()
                if base is None:
                    break
                vts.call("InjectParameterDataRequest", {
                    "faceFound": True, "mode": "set",
                    "parameterValues": [{"id": name, "value": value}],
                })
                time.sleep(0.18)
                after = vts.live2d_snapshot()
                vts.call("InjectParameterDataRequest", {
                    "faceFound": True, "mode": "set",
                    "parameterValues": [{"id": name, "value": 0.0}],
                })
                if not after:
                    continue
                # 各模型命名风格不同：yumi 是 ParamAngleX，wanko 是 PARAM_ANGLE_X，
                # 还有 AngleXbs 这类变体。按"归一化后包含"来匹配，避免因为大小写/
                # 下划线差异把整个通道误判成"完全没反应"。
                like = cls._match_like(name)
                relevant = {k: v for k, v in after.items() if cls._key_matches(k, like)}
                src = relevant or after
                delta = max((abs(src.get(k, 0.0) - base.get(k, 0.0)) for k in src), default=0.0)
                strengths[name] = float(delta)
                time.sleep(0.06)
            except Exception as exc:
                log("probe fail", name, repr(exc)[:60])
        return cls(strengths)

    def gain(self, name, default=1.0, weak=0.35, strong=1.0, threshold=0.03):
        """按实测影响力给出该通道的幅度系数。

        测不到（探测失败/通道不存在）时返回 default，保持原行为。
        """

        if name not in self.strengths:
            return default
        if self.strengths[name] < threshold:
            return weak
        return strong

    def describe(self):
        if not self.strengths:
            return "未探测（按默认幅度）"
        weak = [k for k, v in self.strengths.items() if v < 0.03]
        return "%d 通道已探测，其中 %d 个对该模型影响很弱%s" % (
            len(self.strengths), len(weak),
            ("（" + "、".join(weak[:4]) + "）") if weak else "")

# ---------------------------------------------------------------- 噪声
class ValueNoise:
    """平滑的 1D value noise（C1 连续）。

    x 必须单调递增地调用；内部用游标推进，避免每次线性搜索。
    早期版本每帧重建对象，等价于每帧取一个随机数 —— 那会让高频抖动爆表。
    """
    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.xs = [0.0]
        self.vs = [self.rng.uniform(-1, 1)]
        self.i = 0
    def _grow_to(self, x):
        while self.xs[-1] < x + 1.0:
            self.xs.append(self.xs[-1] + self.rng.uniform(0.6, 1.6))
            self.vs.append(self.rng.uniform(-1, 1))
    def at(self, x):
        self._grow_to(x)
        n = len(self.xs)
        while self.i + 1 < n and self.xs[self.i + 1] < x:
            self.i += 1
        i = max(0, min(self.i, n - 2))
        x0, v0 = self.xs[i], self.vs[i]
        x1, v1 = self.xs[i + 1], self.vs[i + 1]
        if x < x0:
            return v0
        if x > x1:
            return v1
        t = (x - x0) / (x1 - x0)
        s = t * t * t * (t * (t * 6 - 15) + 10)   # smootherstep：二阶连续
        return v0 + (v1 - v0) * s

# ---------------------------------------------------------------- 引擎
class Engine:
    def __init__(self):
        # 不加载任何 motion3 动作：本引擎刻意不播放 VTS 动画/待机动作。
        # 只靠参数合成演活模型，避免"动画值优先"覆盖注入导致抽搐。
        self.clips = json.load(open(_ARGS.clips, encoding="utf-8"))
        self.vts = VTS()
        # 探测当前模型支持哪些输入（用于过滤）
        self.vts.refresh_inputs(force=True)
        self.profile = self.vts.profile
        # 自适应核心：实测每个通道对这个模型的实际影响力，据此缩放输出。
        # 默认（--probe 0）不做：探测要在画面上把每个通道推到极限测六七秒，
        # 模型刚启动时会像在校准一样乱动，而且期间控制接口还没开，
        # 插件会误判"进程启动了但接口无响应"。需要时用 --probe 1 打开。
        self.probe = ModelProbe.measure(self.vts) if _ARGS.probe else ModelProbe({})
        log("model profile:", self.profile.describe())
        log("model probe:", self.probe.describe())
        self.t0 = time.time()
        self.rng = random.Random()
        self.noise_yaw = ValueNoise(1); self.noise_pitch = ValueNoise(2)
        self.noise_roll = ValueNoise(3); self.noise_breath = ValueNoise(4)
        # 多倍频漂移用（代替固定频率正弦，避免像节拍器）
        self.nslow_a = ValueNoise(11); self.nslow_b = ValueNoise(12)
        self.nslow_c = ValueNoise(13); self.nslow_d = ValueNoise(14)
        # 大幅度连续漂移（无停留状态）
        self.nbig_a = ValueNoise(21); self.nbig_b = ValueNoise(22)
        self.nbig_c = ValueNoise(23); self.nbig_d = ValueNoise(24)
        # 短促 beat 列表（只在 __init__ 初始化一次）
        self.beats = []                    # (vec3, start, dur)
        self.next_beat = self.t0 + self.rng.uniform(0.5, 2.0)
        self._prev_inputs = {}            # 输出平滑用
        self.smooth_alpha = 0.75          # 60fps：只削毛刺，不拖慢动作
        # 输出弹簧：给所有通道一点"惯性"，消除线性折返的机械感
        self._spring_v = {}
        self._spring_x = {}
        # 眼球扫视：眼睛不动是最明显的"僵"
        self.gaze_target = (0.0, 0.0)
        self.gaze_pos = (0.0, 0.0)
        self.next_saccade = self.t0 + self.rng.uniform(0.6, 2.0)
        # blink
        self.next_blink = self.t0 + self.rng.uniform(1.5, 4.0)
        # 挑眉动作（表情层用）
        self.next_brow_raise = self.t0 + self.rng.uniform(2.0, 6.0)
        self._brow_raise_at = -100.0
        self._brow_raise_dur = 0.6
        self._brow_raise_amp = 0.0
        self.blink_t0 = -10.0; self.blink_dur = 0.12
        self.pulses = []      # (clip, start, dur_ms, intensity)
        self.sustains = {}    # name -> (clip, expire)
        self.gaze = (0.0, 0.0)
        self.lock = threading.Lock()
        # 环境表演：不定时插一个 cortico 手势脉冲，避免长时间同一张脸。
        # 全部来自 cortico_clips.json 的参数曲线，不触发任何 VTS 热键/动画。
        self.next_ambient = self.t0 + self.rng.uniform(4, 9)
        self.ambient_clips = ["accent_nod", "nod_rapid", "accent_swing", "accent_brow",
                              "tilt_curious", "look_around", "glance_danmaku", "smug",
                              "eyes_wide", "pout_puff"]
        # 表情/道具：清单来自当前模型的真实表情文件与热键，换模型重启即自适应。
        # 只做「按情绪词匹配 + 互斥切换」，不播任何 VTS 动画，所以不会和参数层抢控制权。
        try:
            self.catalog = self.vts.load_catalog()
        except Exception as exc:
            log("expression catalog failed", repr(exc)[:90])
            self.catalog = ExpressionCatalog()
        self.expr = ExpressionState(self.catalog)
        # 自动道具：候选来自当前模型真实的物品与配对热键，不写死任何皮套的名字。
        # 隔几分钟自己掏一个、挂几秒再收回，让画面有点小动作。
        self.props = PropScheduler(enabled=bool(_ARGS.auto_props), rng=self.rng)
        self._next_prop_check = 0.0
        self._refresh_props()
        # 换皮检测：运行时在 VTS 里换模型后，参数集合/表情/道具候选都要重建，
        # 否则还在按旧模型注入（VTS 会整批拒绝无效参数），看起来就是"换了皮不动"
        self._model_name = str(getattr(self.catalog, "model_name", "") or "")
        self._next_model_check = self.t0 + 10.0
        # 展示型表情（兔牙/钢盆/前倾…）的到期时间：由台词记号点名触发。
        # 必须在 _reset_expressions 之前建好——它会顺手把旧外观一起收掉。
        self._poses = {}
        # 启动时把上一次运行残留的情绪表情关掉：VTS 会记住表情状态，
        # 上次没收回的会一直挂在脸上，热重启后看着像"换了模型还带着怪脸"。
        self._reset_expressions()
        # 情绪姿态：让同一个情绪在面部参数、身体角度、视线上一起体现。
        # 没有这一层的话，表情只是"贴在脸上的图层"，头还在匀速摇，
        # 看着就像脸和身体是两个人在演。
        self._pose_emotion = ""
        self._pose_at = 0.0
        self._pose_seconds = 0.0
        # 情绪的面部偏置（眉毛/嘴/眼睑/脸颊）单独计时：脸是情绪的主载体，
        # 要跟着表情文件挂满整个 hold；身体则提前回正，不能脸回来了身子还在歪。
        self._face_hold = {}
        self._face_at = 0.0
        self._face_seconds = 0.0
        # 这个情绪"上场"要走多久（惊讶 0.5s 快、委屈 1.05s 慢）
        self._face_rise = 0.6
        # 这个情绪退场要走多久（回落比上场慢）
        self._face_offset = 0.6
        # 情绪自带的头部抖动幅度（紧张/害怕"绷不住"的那点颤）
        self._tremble = 0.0
        # 情绪驱动的眨眼节奏（slow/fast/none）与视线扫视频率倍率
        self._blink_style = ""
        self._gaze_rate = 1.0
        self._gaze_bias = (0.0, 0.0)
        self._gaze_bias_until = 0.0
        # 需要显式关闭的表情（切换或到期时填进来）
        self._expr_close = []
        # 表情状态是否有变化：只有变化时才下发，避免拖慢 60fps 注入循环
        self._expr_dirty = False
        # 展示型表情（兔牙/钢盆/前倾…）的状态与情绪表情分开管：
        # VTS 里这类 ToggleExpression 可以同时挂好几个，不做互斥。
        self._poses = getattr(self, "_poses", {})

    # ---- 表情
    def _reset_expressions(self):
        """启动/换皮时收起所有情绪表情与已挂的外观，只保留体型这类基础设置。"""

        for name in self.catalog.emotion_names:
            self.vts.set_expression_state(self.catalog.file_of(name), False, 0.0)
        self._poses.clear()

    def _expression_repeat(self, emotion):
        """这个情绪在短时间内已经演过几次（用于习惯化）。

        人不会每次都用同样的力度重复同一个表情——连着第三次「无语」会比第一次
        淡。这里只做一个很短的自适应窗口，让连续同情绪自然递减。
        """

        now = time.time()
        last, count, at = getattr(self, "_expression_history", ("", 0, 0.0))
        if emotion == last and now - at < 15.0:
            count += 1
        else:
            count = 0
        self._expression_history = (emotion, count, now)
        return count

    def set_expression(self, emotion, hold_seconds=None, text=""):
        """按情绪词切换表情（黑脸/流冷汗/爱心眼…），匹配不到就只走参数层。

        表情文件是可选层：模型没有对应 .exp3.json 时，情绪照样由参数层
        体现在脸上（眉毛/嘴角/眼睑/脸颊/身体/视线），不会变成"面无表情"。
        匹配逻辑在 vts/expression.py：只查当前模型真实存在的表情，
        换模型后不会把别的模型的表情名硬塞进来。

        时长不写死：交给 vts/timing.py 按台词计划三段时序（onset/apex/offset）。
        强度看感叹号/叠字/程度副词，apex 还要撑到这句话念完——所以同一个
        「委屈」在「呜……有点难过」里是短促一次，在「呜呜呜呜你干嘛这样骂我！」
        里会更沉更长。hold_seconds 显式传入时仍以调用方为准（调试/接口用）。
        """

        catalog = self.catalog
        # 走清单的标定结果（表情文件参数 → FACS AU → 情绪原型），
        # 名字语义只是兜底；两者都没有就不给表情，只走参数层。
        name = catalog.match(emotion)
        timing = plan_for_text(
            emotion, text, repeat=self._expression_repeat(emotion), rng=self.rng,
        )
        hold = timing.total
        if hold_seconds is not None:
            try:
                manual = float(hold_seconds)
            except (TypeError, ValueError):
                manual = 0.0
            if manual > 0:
                hold = manual
        # 上限再兜一层：外部传了个很长的值也不该演成"雕像"
        hold = max(0.6, min(hold, 8.0))
        if name:
            with self.lock:
                previous = self.expr.set_expression(
                    name, hold, fade_in=timing.fade_in, fade_out=timing.fade_out)
            # 互斥：先把旧的显式关掉，避免出现两个表情叠一起的怪脸
            if previous:
                self._expr_close = list(previous)
            self._expr_dirty = True
            # 立刻下发一次，别等下一帧（HTTP 调用方马上就会去查状态）
            self._apply_expressions()
        # 关键：同一个情绪同时驱动面部参数、身体姿态和视线，
        # 否则表情和身体各演各的，看着像两个人。
        # 这一步和表情文件无关，没表情的模型也要有"脸色"。
        posed = self.apply_emotion_pose(emotion, hold, timing=timing)
        if not name:
            if posed:
                log("expression", emotion, "-> 参数表情（模型没有这张脸）", timing.describe())
                return True, "参数表情"
            known = "、".join(sorted(catalog.labeled)) or "无（表情语义未标定）"
            return False, "当前模型没有匹配「%s」的表情（已标定：%s）" % (emotion, known)
        log("expression", emotion, "->", name, timing.describe())
        return True, name

    def apply_emotion_pose(self, emotion, hold_seconds=None, timing=None):
        """让情绪在面部参数 / 身体姿态 / 视线上一并体现。

        三层配合：
          * sustain   持续参数偏置（眉毛/嘴/眼睑/头角度），带噪声不会僵住；
          * intro     切换瞬间放一个 pulse 小动作，让情绪是"发生的"；
          * gaze      视线偏置（无语看别处、委屈低头、害羞躲开）。

        Args:
            emotion: 情绪词。
            hold_seconds: 总时长；None 时按配方或默认值。
            timing: :class:`vts.timing.ExpressionTiming`。给了就用它的三段
                时序（onset/apex/offset），这样面部参数和表情文件同步上下场，
                不会出现"脸先动完、表情才淡出来"。
        """

        pose = pose_for(emotion)
        if pose is None:
            return False
        if timing is not None:
            hold = timing.total
            rise_seconds = timing.onset
            offset_seconds = timing.offset
        else:
            try:
                hold = float(hold_seconds) if hold_seconds is not None else 0.0
            except (TypeError, ValueError):
                hold = 0.0
            if hold <= 0:
                hold = DEFAULT_HOLD_SECONDS
            rise_seconds = float(pose.get("rise", 0.6))
            offset_seconds = float(pose.get("offset", 0.6))

        now = time.time()
        with self.lock:
            self._pose_emotion = emotion
            self._pose_at = now
            # 姿态比表情略短一点：表情还挂着时身体已经慢慢回正，看起来更自然
            self._pose_seconds = max(0.8, hold * 0.85)
            self._tremble = float(pose.get("tremble", 0.0))
            # 表情的上场时长：不同情绪不一样快，整张脸一瞬间到位最假
            self._face_rise = max(0.12, rise_seconds)
            # 退场时长：真人回落比上场慢（刻意表情反而更快）
            self._face_offset = max(0.14, offset_seconds)
            # 眨眼与视线也属于情绪表演：尴尬会连眨 + 眼神乱瞟，害怕会瞪眼不眨
            self._blink_style = str(pose.get("blink") or "")
            self._gaze_rate = float(pose.get("gaze_restless", 1.0))

            # 入场动作：只在有对应 clip 时播
            intro = pose.get("intro")
            clip = (self.clips.get("pulse") or {}).get(intro) if intro else None
            if clip:
                dur = max([k[-1][0] for k in clip["tracks"].values()] or [800])
                self.pulses.append((clip["tracks"], now, dur, 0.85))

            # 持续面部偏置：换情绪时直接替换，不叠加。
            # face 字段是按 AU 组合精确配好的输入增量，直接用它；
            # 没有 face 的老配方才退回 cortico sustain clip。
            # 脸跟着 hold 走（表情文件挂多久，脸就维持多久），比身体长一点。
            face = pose.get("face")
            sname = pose.get("sustain")
            sclip = (self.clips.get("sustain") or {}).get(sname) if sname else None
            if face:
                self._face_hold = {k: float(v) for k, v in face.items()}
                self._face_at = now
                self._face_seconds = max(0.8, hold)
            elif sclip:
                intensity = float(pose.get("sustain_intensity", 0.8))
                hold_vals = sclip.get("hold") or {}
                self._face_hold = {
                    k: float(v.get("v", 0.0)) * intensity
                    for k, v in hold_vals.items() if isinstance(v, dict)
                }
                self._face_at = now
                self._face_seconds = max(0.8, hold)
            else:
                self._face_hold = {}
                self._face_seconds = 0.0

            # 视线偏置
            bias = pose.get("gaze_bias")
            if bias:
                self._gaze_bias = (float(bias[0]), float(bias[1]))
                self._gaze_bias_until = now + float(pose.get("gaze_hold", 5.0))
        log("pose", emotion, "->", pose.get("sustain"), "intro", pose.get("intro"))
        return True

    def set_emotion(self, emotion, hold_seconds=None, text=""):
        """只走参数层的情绪：不切任何 .exp3.json，脸全靠参数合成。

        用于"纯参数"验证/演算——模型没有对应表情文件、或不想让表情图层盖住
        眉毛和眨眼时，情绪依然由眉毛/嘴角/眼睑/脸颊/头角度/视线体现。
        hold_seconds 传 None 表示按台词计划时序（见 vts/timing.py）。
        """

        timing = plan_for_text(
            emotion, text, repeat=self._expression_repeat(emotion), rng=self.rng,
        )
        hold = timing.total
        if hold_seconds is not None:
            try:
                manual = float(hold_seconds)
            except (TypeError, ValueError):
                manual = 0.0
            if manual > 0:
                hold = manual
        hold = max(0.6, min(hold, 8.0))
        return self.apply_emotion_pose(emotion, hold, timing=timing)

    def _pose_layer(self, now):
        """把当前情绪的身体姿态换算成每帧的语义量偏置（带衰减）。

        为什么用 FaceAngleX/Z：实测这套模型上 FaceAngleX 20° 会让
        ParamBodyAngleX 跟着到 19.63（几乎 1:1），FaceAngleZ 20° 让
        ParamBodyAngleZ 到 -14.39；而 Mocopi 身体通道对这类模型几乎没反应。
        所以"身体随情绪动"只能靠头带动。
        """

        emotion = self._pose_emotion
        if not emotion:
            return {}
        age = now - self._pose_at
        if age > self._pose_seconds:
            self._pose_emotion = ""
            return {}
        pose = pose_for(emotion)
        if pose is None:
            return {}
        body = pose.get("body") or {}
        if not body:
            return {}
        # 前 70% 保持，后 30% 平滑回正：情绪不能一刀两断
        scale = decayed(1.0, age, self._pose_seconds)
        if scale <= 0:
            return {}
        return {k: float(v) * scale for k, v in body.items()}

    #: 情绪面部偏置的落点与权重（输入空间）。
    #: 眉毛给满 —— 情绪最直接的信号是眉，压 0.5 会被 idle 噪声吃掉，看起来"没表情"；
    #: 嘴角留 0.9，拉满 1.0 会变成咧嘴笑；头角度沿用原来的幅度，免得和身体层打架。
    FACE_GAINS = {
        "BrowLeftY": 1.0, "BrowRightY": 1.0,
        "MouthSmile": 0.9, "MouthOpen": 1.0,
        "EyeOpenLeft": 1.0, "EyeOpenRight": 1.0,
        "CheekPuff": 1.0,
        "FaceAngleX": 0.5, "FaceAngleY": 0.5, "FaceAngleZ": 0.4,
    }

    #: 手势脉冲（nod / shake / eyeroll…）的幅度系数，沿用原来的半幅
    SEM_GAINS = {
        "MouthSmile": 0.5, "BrowLeftY": 0.5, "BrowRightY": 0.5,
        "FaceAngleX": 0.5, "FaceAngleY": 0.5, "FaceAngleZ": 0.4,
    }

    def _emotion_weight(self, now):
        """当前情绪的强度 0~1：入段 0.2 秒淡入，到期按 decayed 平滑回落。

        idle 收敛和抖动都靠它连续变化，所以情绪起落时不会突然"定住/放开"。
        """

        if not self._pose_emotion:
            return 0.0
        age = now - self._pose_at
        if age > self._pose_seconds:
            return 0.0
        attack = min(1.0, age / 0.2)
        attack = attack * attack * (3 - 2 * attack)
        return decayed(1.0, age, self._pose_seconds) * attack

    #: 各通道的"上场延迟"（秒）。真人做表情时不是整张脸同一时刻到位：
    #: 眼睛先睁大，嘴角晚半拍；左半脸比右半脸领先一点（自发表情的不对称）。
    #: 依据：Hoffmann et al. (2010) 的 onset 时长数据 + 左脸先动的自发表情研究。
    FACE_DELAY = {
        "EyeOpenLeft": 0.0, "EyeOpenRight": 0.04,
        "EyeLeftX": 0.0, "EyeRightX": 0.04, "EyeLeftY": 0.0, "EyeRightY": 0.04,
        "MouthSmile": 0.06, "MouthOpen": 0.06, "CheekPuff": 0.05,
        "BrowLeftY": 0.02, "BrowRightY": 0.05,
    }

    def _emotion_face_layer(self, now):
        """把当前情绪换算成每帧的面部偏置（带淡入与平滑回落）。

        原来情绪只是往 sustains 里塞一条 clip，到点直接消失；而且幅度被
        折半后和 idle 的眉毛噪声差不多，脸上几乎看不出差别。现在：
          * 眉毛/嘴角/眼睑/脸颊按 FACE_GAINS 全额给，情绪必须看得出来；
          * 每个情绪有自己的 rise 时长（惊讶 0.5s 快、委屈 1.05s 慢），
            而且各通道还有几十毫秒的先后差 —— 整张脸同一帧到位才是最假的；
          * 左半脸幅度 +3%、右半脸 −3%：完全对称的脸会被看成"摆拍"；
          * 尾段沿用 decayed 的平滑回落，情绪退场也是渐变的。
        """

        if not self._face_hold:
            return {}
        age = now - self._face_at
        if age > self._face_seconds:
            self._face_hold = {}
            return {}
        rise = max(0.2, self._face_rise)
        out = {}
        for name, value in self._face_hold.items():
            delay = self.FACE_DELAY.get(name, 0.0)
            a = (age - delay) / rise
            a = 0.0 if a <= 0.0 else min(1.0, a)
            a = a * a * (3 - 2 * a)       # smoothstep：起手快、收尾稳
            if a <= 0.0:
                continue
            scale = decayed(1.0, age, self._face_seconds, self._face_offset) * a
            if scale <= 0:
                continue
            # 左脸略强 / 右脸略弱（3%）：肉眼几乎察觉不到幅度，但整张脸不再"镜像对称"
            asym = 1.0
            if name in ("EyeOpenLeft", "EyeLeftX", "EyeLeftY", "BrowLeftY"):
                asym = 1.03
            elif name in ("EyeOpenRight", "EyeRightX", "EyeRightY", "BrowRightY"):
                asym = 0.97
            out[name] = value * scale * self.FACE_GAINS.get(name, 1.0) * asym
        return out

    def _blend_face(self, inputs, deltas):
        """把输入空间的情绪/手势量叠加到已算好的注入值上。

        各通道按自己的合法区间收敛：归一化量 0~1、眼球 -1~1、头角度按度数。
        未知通道直接丢弃，避免把无关名字塞进注入批次。
        """

        limits = {"MouthSmile": (0.0, 1.0), "MouthOpen": (0.0, 1.0),
                  "CheekPuff": (0.0, 1.0),
                  "BrowLeftY": (0.0, 1.0), "BrowRightY": (0.0, 1.0),
                  "EyeOpenLeft": (0.0, 1.0), "EyeOpenRight": (0.0, 1.0),
                  "EyeLeftX": (-1.0, 1.0), "EyeRightX": (-1.0, 1.0),
                  "EyeLeftY": (-1.0, 1.0), "EyeRightY": (-1.0, 1.0),
                  "FaceAngleX": (-28.0, 28.0), "FaceAngleY": (-10.0, 12.0),
                  "FaceAngleZ": (-13.0, 13.0)}
        for name, value in deltas.items():
            bounds = limits.get(name)
            if bounds is None or not math.isfinite(value):
                continue
            base = inputs.get(name)
            if base is None:
                base = 0.5 if name in ("MouthSmile", "BrowLeftY", "BrowRightY") else (
                    1.0 if name.startswith("EyeOpen") else 0.0)
            inputs[name] = float(np.clip(base + value, bounds[0], bounds[1]))

    def clear_expression(self):
        """收起所有表情（连同情绪带来的面部/身体/视线偏置），外观一并收起。"""

        with self.lock:
            gone = self.expr.clear()
            # 表情收起时情绪也要一起收：不然表情没了，脸还挂着一副委屈
            self._pose_emotion = ""
            self._face_hold = {}
            self._face_seconds = 0.0
            self._gaze_bias = (0.0, 0.0)
            self._gaze_bias_until = 0.0
        poses = self.clear_pose()
        if gone:
            self._expr_close = list(gone)
            self._expr_dirty = True
            self._apply_expressions()
            log("expression cleared", gone)
        if poses:
            log("poses cleared", poses)
        return gone

    # ---- 外观/姿势（展示型表情）：由台词记号点名，不进情绪轮换
    def set_pose(self, name, hold_seconds=45.0, toggle=True):
        """戴上/收起模型自带的外观件（兔牙、钢盆、前倾、眼镜…）。

        这些是 ToggleExpression 型表情，VTS 里可以多个同时挂，所以这里**不做互斥**，
        只按名字记到期时间。``toggle=True`` 时同一个名字再说一次就收起。

        Args:
            name: 表情名（当前模型真实存在的）。
            hold_seconds: 保持多少秒；<=0 表示一直戴着。
            toggle: 已经戴着时是否改为收起。

        Returns:
            (是否成功, 说明)
        """

        target = str(name or "").strip()
        asset = self.catalog.asset(target)
        if asset is None:
            return False, "当前模型没有「%s」（可用：%s）" % (
                target or "空", "、".join(self.catalog.display_names[:8]) or "无外观件")
        if asset.kind == "emotion":
            return False, "「%s」是情绪表情，请走 /expression" % target
        file_name = self.catalog.file_of(target)
        if not file_name:
            return False, "「%s」没有可激活的表情文件" % target

        now = time.time()
        expire = self._poses.get(target)
        if toggle and expire is not None and expire > now:
            self._poses.pop(target, None)
            self.vts.set_expression_state(file_name, False, 0.25)
            log("pose off", target)
            return True, "收起 " + target
        self.vts.set_expression_state(file_name, True, 0.25)
        self._poses[target] = now + float(hold_seconds) if hold_seconds > 0 else float("inf")
        log("pose on", target, "hold", hold_seconds)
        return True, "戴上 " + target

    def clear_pose(self, name=""):
        """收起一个或全部外观件。"""

        target = str(name or "").strip()
        names = [target] if target else list(self._poses)
        gone = []
        for item in names:
            if self._poses.pop(item, None) is None and target:
                continue
            file_name = self.catalog.file_of(item)
            if file_name:
                self.vts.set_expression_state(file_name, False, 0.3)
            gone.append(item)
        return gone

    def active_poses(self):
        """当前戴着的外观件（已剔除过期）。"""

        now = time.time()
        return sorted(n for n, expire in self._poses.items() if expire > now)

    def _expire_poses(self, now):
        """到点的外观件自动收起（挂在 60fps 循环里，每秒最多一次）。"""

        if not self._poses:
            return
        expired = [n for n, expire in self._poses.items() if expire <= now]
        for name in expired:
            self._poses.pop(name, None)
            file_name = self.catalog.file_of(name)
            if file_name:
                self.vts.set_expression_state(file_name, False, 0.3)
            log("pose expired", name)

    def reset_stage(self):
        """场景重置（cortico 台本里的【Reset】）：收起表情/道具/姿势，视线回镜头。

        换话题时 AI 打一个【重置】就能回到中性，不用逐个记号去收。
        道具的关闭热键是幂等的：对每个已知道具发一次 close，已收起的没有副作用。
        """

        with self.lock:
            gone_expr = self.expr.clear()
            gone_props = self.expr.clear_props()
            self.clear_pose("")
        closed = []
        for base, pair in self.catalog.pairs.items():
            if base not in gone_props:
                continue
            try:
                if self.vts.trigger_hotkey(pair[1]):
                    closed.append(base)
            except Exception as exc:  # noqa: BLE001 - 单个道具收不起来不影响其它
                log("prop close failed", base, exc)
        self.set_gaze(0.0, 0.0)
        if gone_expr or gone_props or closed:
            log("reset stage", "expr:", gone_expr, "props:", gone_props)
        return True

    def set_prop(self, prop, hold_seconds=60.0):
        """打开/收起动画道具（狗尾巴草、竖手指这类开/关配对热键）。

        hold_seconds <= 0 表示收起当前道具。
        """

        pairs = self.catalog.pairs
        base = match_prop(prop, pairs.keys()) if pairs else ""
        if not base:
            return False, "当前模型没有匹配「%s」的动画道具（可选：%s）" % (
                prop, "、".join(pairs.keys()) or "无")
        open_id, close_id = pairs[base]
        try:
            hold = float(hold_seconds)
        except (TypeError, ValueError):
            hold = 60.0
        if hold <= 0:
            with self.lock:
                self.expr.close_prop(base)
            ok = self.vts.trigger_hotkey(close_id)
            log("prop closed", base)
            return ok, base
        with self.lock:
            previous = self.expr.open_prop(base, hold)
        # 只允许一个道具同时挂着：先把旧的收回
        for old in previous:
            old_pair = pairs.get(old)
            if old_pair:
                self.vts.trigger_hotkey(old_pair[1])
        ok = self.vts.trigger_hotkey(open_id)
        log("prop opened", base, "hold", hold)
        # AI 自己点了道具：让自动调度让路，别它刚收起狗尾巴草这边又掏一个
        scheduler = getattr(self, "props", None)
        if scheduler is not None:
            scheduler.notify_manual(time.time())
        return ok, base

    # ---- 输入
    def _normalize_tag(self, tag):
        """把中文别名或英文 key 统一成 tag（"眨眼" -> "wink"）。

        插件走的是 canonical key，所以生产链路本来不会出错；但调试接口
        ``/tag?name=眨眼`` 会直接落到这里——不归一就静默返回 False，
        调用方只看到一个 400，很难判断到底是没这个动作还是参数写错了。
        """

        raw = str(tag or "").strip()
        if not raw:
            return ""
        if raw in TAG_TO_CLIP:
            return raw
        for key, labels in TAG_LABELS.items():
            if raw in labels:
                return key
        return raw

    def play_tag(self, tag, intensity=1.0):
        tag = self._normalize_tag(tag)
        clip_name = TAG_TO_CLIP.get(tag)
        played = False
        with self.lock:
            if clip_name and clip_name in (self.clips.get("pulse") or {}):
                clip = self.clips["pulse"][clip_name]
                dur = max([k[-1][0] for k in clip["tracks"].values()] or [800])
                self.pulses.append((clip["tracks"], time.time(), dur, intensity))
                played = True
            sname = TAG_TO_SUSTAIN.get(tag)
            if sname and sname in (self.clips.get("sustain") or {}):
                clip = self.clips["sustain"][sname]
                hold = clip.get("hold") or {}
                vals = {k: v.get("v", 0.0) for k, v in hold.items() if isinstance(v, dict)}
                self.sustains[sname] = (vals, time.time() + 3.5)
                played = True
        if played: log("tag", tag, "->", clip_name or sname)
        return played

    def set_gaze(self, x, y):
        with self.lock: self.gaze = (x, y)

    def _new_waypoint(self):
        """在姿态空间里均匀取一个离当前位置够远的目标。"""
        cur = self.pose_to
        for _ in range(12):
            cand = [self.rng.uniform(-26, 26),      # yaw
                    self.rng.uniform(-12, 16),      # pitch
                    self.rng.uniform(-21, 21)]      # roll
            if math.dist(cand, cur) > 12:
                return cand
        return cand

    def _update_posture(self, now):
        dt = 1.0 / FPS
        t = now - self.t0
        # 3 个时间尺度的噪声叠加：既有几十秒的大幅换姿，也有几秒的小幅变化
        # OU（均值回归）漂移：有界、自然回中，符合 ECA 研究的姿态漂移特性
        if not hasattr(self, "_ou"):
            self._ou = [0.0, 0.0, 0.0]
        ou = self._ou
        theta, sigma = 0.35, 1.9      # 回中速率 & 波动强度
        for i in range(3):
            ou[i] += (-theta * ou[i]) * dt + sigma * math.sqrt(dt) * self.rng.gauss(0, 1)
        yaw_p = ou[0] * 5.5 + self.nslow_a.at(t * 0.019) * 6.0
        pitch_p = ou[1] * 5.2 + self.nslow_c.at(t * 0.016) * 5.5
        roll_p = yaw_p * 0.32 + ou[2] * 2.0
        cur_pose = [yaw_p, pitch_p, roll_p]
        # 随机 beat：短促偏移，让停留时也不死
        if now >= self.next_beat:
            ang = self.rng.uniform(0, 2 * math.pi)
            cy = math.cos(ang)
            vec = [cy, self.rng.uniform(-0.8, 0.8), cy * 0.35 + self.rng.uniform(-0.15, 0.15)]
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            amp = self.rng.uniform(3.0, 8.0)
            dur = self.rng.uniform(0.35, 0.95)
            self.beats.append(([v / norm * amp for v in vec], now, dur))
            self.next_beat = now + self.rng.uniform(0.9, 2.6)
        off = [0.0, 0.0, 0.0]
        alive = []
        for vec, start, dur in self.beats:
            t = now - start
            if 0 <= t <= dur:
                q = t / dur
                # minimum-jerk 轨迹（Hogan 1985）：速度曲线呈钟形，缓入缓出
                env = 10 * q**3 - 15 * q**4 + 6 * q**5 if q <= 0.5 else 1.0
                # 后半段回到 0（beat 是短暂偏移）
                if q > 0.5:
                    q2 = (q - 0.5) / 0.5
                    env = 1.0 - (10 * q2**3 - 15 * q2**4 + 6 * q2**5)
                for i in range(3):
                    off[i] += vec[i] * env
                alive.append((vec, start, dur))
        self.beats = alive
        return (cur_pose[0] + off[0], cur_pose[1] + off[1], cur_pose[2] + off[2])

    def _ambient(self, now):
        """不定时插一个作者写的小动作；偶尔换一个表情。"""
        if now >= self.next_ambient:
            recent = getattr(self, "_ambient_recent", [])
            pool = [c for c in self.ambient_clips if c not in recent[-6:]] or self.ambient_clips
            name = self.rng.choice(pool)
            self._ambient_recent = (recent + [name])[-12:]
            clip = (self.clips.get("pulse") or {}).get(name)
            if clip:
                with self.lock:
                    dur = max([k[-1][0] for k in clip["tracks"].values()] or [800])
                    self.pulses.append((clip["tracks"], now, dur, self.rng.uniform(0.45, 0.8)))
            # 环境小动作（点头/歪头/看别处…）是"灵动"最直接的来源：
            # 原来 6~14 秒一次偏稀，改成 4~9 秒，密度接近真人直播的自然节奏
            self.next_ambient = now + self.rng.uniform(4.0, 9.0)

        # 表情不走 VTS 表情热键：热键会整套覆盖参数、和注入抢通道，切换瞬间就是抽搐。
        # 表情统一由参数层（MouthSmile / BrowLeftY / BrowRightY / EyeOpen）合成。

    # ---- 各层

    def micro_layer(self, now):
        t = now - self.t0
        breath = math.sin(2 * math.pi * 0.22 * t) * 0.5 + 0.5
        # yaw（转头）是驱动身体的最高效通道（实测身/头比 3.2），所以给它更多权重；
        # pitch（点头）几乎不带动身体（0.05），压小一点免得"只有头在动"。
        yaw = self.noise_yaw.at(t * 0.13) * 4.2
        pitch = self.noise_pitch.at(t * 0.11) * 1.5 + (breath - 0.5) * 1.1
        # 高频微颤（0.15~0.3 度）：完全丝滑反而像塑料，人肉总有一点点抖动
        yaw += self.noise_breath.at(t * 2.7) * 0.28
        pitch += self.noise_yaw.at(t * 3.1) * 0.18
        # 姿态基调：40~90 秒尺度的重心/朝向变化，让"整体感觉"也在变
        # 姿态基调：40~90 秒尺度的重心/朝向变化。yaw 权重加大 —— 它带动身体，
        # 所以这段慢漂移是"身体在自然换重心"最主要的来源
        self.bias_yaw = self.nslow_d.at(t * 0.014) * 7.5
        self.bias_pitch = self.nslow_a.at(t * 0.011) * 2.5
        # 多倍频噪声漂移：没有固定周期，不会像节拍器
        # 多倍频漂移：yaw 权重↑（带身体），pitch 权重↓（只动头）
        yaw += self.nslow_a.at(t * 0.085) * 4.6 + self.nslow_b.at(t * 0.031) * 3.2
        pitch += self.nslow_c.at(t * 0.070) * 2.0 + self.nslow_d.at(t * 0.026) * 1.2
        # roll（歪头）身/头比 1.78，也值得给足
        roll = self.noise_roll.at(t * 0.09) * 1.6 + self.noise_pitch.at(t * 2.3) * 0.18
        # 嘴部小变化：让不说话的时候脸也不死板
        jaw = 0.06 + 0.05 * math.sin(2 * math.pi * 0.17 * t) + 0.03 * self.noise_breath.at(t * 0.4)
        mouth_x = 0.22 * self.noise_yaw.at(t * 0.21)
        pucker = 0.05 * self.noise_roll.at(t * 0.17)
        # 嘴型慢漂移：人静下来嘴也不会完全不动
        mouth_form = self.noise_pitch.at(t * 0.07) * 0.55 + (breath - 0.5) * 0.35
        py_, pp_, pr_ = self._update_posture(now)
        yaw += py_
        pitch += pp_
        roll += pr_
        # 情绪权重：情绪上来时真人会先"定住"，而 idle 的摆头有 ±20°，
        # 情绪那 2~4° 的体态本来会被完全淹没。所以情绪期间把 idle 收紧一点，
        # 情绪退了再自然放开（系数随权重连续变化，不会突变）。
        weight = self._emotion_weight(now)
        idle_scale = 1.0 - 0.5 * weight
        yaw *= idle_scale
        pitch *= idle_scale
        roll *= idle_scale
        return {"ParamAngleX": yaw, "ParamAngleY": pitch, "ParamAngleZ": roll,
                "ParamMouthX": mouth_x, "ParamMouthForm": mouth_form}

    def body_layer(self, now):
        """身体层：独立驱动躯干/胯/肩，不再只是"头带着身体晃"。

        这个模型（大肥鱼）没有把 FaceAngle 映射到独立的身体通道，
        实测 FaceAngleX 虽然会顺带带动 ParamBodyAngleX，但幅度被动、
        而且完全是头部的附庸 —— 看起来就是"头在动、身子是块木头"。

        这里用 VTS 的 Mocopi 全身追踪输入独立驱动身体：
          * MocopiBodyAngleX  躯干前倾/后仰（实测能带动 HipAngleX/ShoulderZ）
          * MocopiBodyAngleZ  躯干侧倾
          * MocopiHipAngleZ   胯部扭转
        三者用不同频率的 value noise，所以身体有自己的节奏，不会和头同步。
        """

        t = now - self.t0

        # 实测：这个模型对 Mocopi 输入的响应很弱（单独注入只有 4~10 的幅度），
        # 身体主要还是被 FaceAngleX（转头）带动的。所以这层只做「小幅补个性」，
        # 不能喧宾夺主 —— 幅度给大了会和头部带动的结果互相抵消。
        body_x = self.nslow_b.at(t * 0.028) * 3.0 + self.noise_pitch.at(t * 0.09) * 1.2
        body_z = self.nslow_c.at(t * 0.035) * 2.6 + self.noise_yaw.at(t * 0.12) * 1.0
        hip_z = self.nslow_d.at(t * 0.022) * 2.4 + self.noise_roll.at(t * 0.07) * 0.9

        # 呼吸联动：吸气时躯干微微挺起，这个细节让「活着」的感觉强很多
        breath = math.sin(2 * math.pi * 0.22 * t)
        body_x += breath * 0.9
        body_z += math.sin(2 * math.pi * 0.22 * t + 0.6) * 0.6

        # 自适应：不是所有模型都能被 Mocopi 输入驱动身体（多数只有 FaceAngle 牵连），
        # 没有这些通道就整层跳过。
        if not self.profile.can_body:
            return {}
        out = {}
        if "MocopiBodyAngleX" in self.profile.valid:
            out["MocopiBodyAngleX"] = float(np.clip(body_x, -6.0, 6.0))
        if "MocopiBodyAngleZ" in self.profile.valid:
            out["MocopiBodyAngleZ"] = float(np.clip(body_z, -5.0, 5.0))
        if "MocopiHipAngleZ" in self.profile.valid:
            out["MocopiHipAngleZ"] = float(np.clip(hip_z, -4.5, 4.5))
        return out
    def expression_layer(self, now):
        """表情辅助层：眉毛、脸颊、嘴形的小幅持续变化。

        原来引擎只驱动头部/眼睛/嘴，眉毛完全不动 —— 而眉毛是"有情绪"最直观的
        信号。这里用慢速 value noise 叠加出连续的小起伏，再配合 react 层的手势，
        让整张脸看着是"活的"而不是只有头在转。
        """

        t = now - self.t0

        # 眉毛：两条独立噪声（左右不同步才自然），基线略高于中点显得有精神
        # BrowLeftY/BrowRightY 的注入空间是 0~1，0.5 是中性位置
        # 两条眉毛用**同频率不同源**的噪声：完全独立会像抽搐，完全同步又假。
        # 之前右眉挂的是 noise_roll（本身变化最慢），实测它的变化频率只有左眉的 1/9。
        # 两条眉毛用同一个噪声源、不同相位 —— 既不会像抽搐（同源同相），
        # 也不会左右乱跳（异源异频）。相位差靠采样频率的微小差异制造。
        brow_l = 0.5 + self.noise_pitch.at(t * 0.13) * 0.22
        brow_r = 0.5 + self.noise_pitch.at(t * 0.13 + 2.7) * 0.22

        # 每 6~14 秒来一次"挑一下眉"，这是真人说话的常态
        if now >= self.next_brow_raise:
            self._brow_raise_at = now
            self._brow_raise_dur = self.rng.uniform(0.35, 0.9)
            self._brow_raise_amp = self.rng.uniform(0.18, 0.42)
            self.next_brow_raise = now + self.rng.uniform(6.0, 14.0)

        # 挑眉包络：快速抬起 + 缓慢落下
        phase = (now - getattr(self, "_brow_raise_at", -100.0)) / max(0.01, getattr(self, "_brow_raise_dur", 0.5))
        if 0.0 <= phase <= 1.0:
            amp = getattr(self, "_brow_raise_amp", 0.0)
            if phase < 0.35:
                q = phase / 0.35
                env = q * q * (3 - 2 * q)
            else:
                q = (phase - 0.35) / 0.65
                env = 1.0 - q * q * (3 - 2 * q)
            raise_ = amp * env
            brow_l += raise_
            brow_r += raise_

        # 自适应：模型没有眉毛通道（hijiki/wanko 这类简版模型）就跳过，
        # 不浪费帧预算，也不会让整批注入因为无效名字被 VTS 拒绝。
        if not self.profile.can_brow:
            return {}
        return {
            name: float(np.clip(brow_l if name == "BrowLeftY" else brow_r, 0.0, 1.0))
            for name in self.profile.brow_names
        }
    def blink_ratio(self, now):
        """返回 0..1 的睁眼倍率（1=正常睁眼，0=完全闭合）。

        眨眼节奏由情绪决定（拟人化的关键细节之一）：
          none  害怕/警觉 —— 瞪着眼几乎不眨（恐惧会抑制眨眼）
          slow  无语/轻蔑 —— 慢眨，偶尔闭眼停一拍（"嗯……"的出神感）
          fast  尴尬/紧张 —— 急促连眨 2~3 下，再歇一会儿
        没有情绪时走原来的随机节奏（3~6 秒一次，含偶发二次眨眼）。
        """
        if now >= self.next_blink:
            self.blink_t0 = now
            r = self.rng.random()
            style = self._blink_style
            if style == "none":
                self.blink_dur = self.rng.uniform(0.10, 0.14)
                delay = self.rng.uniform(6.0, 9.5)
            elif style == "slow":
                # 22% 慢到接近"闭眼出神"，其余是比常态更慢的眨眼
                self.blink_dur = (self.rng.uniform(0.55, 0.85) if r < 0.22
                                  else self.rng.uniform(0.26, 0.42))
                delay = self.rng.uniform(2.4, 4.6)
            elif style == "fast":
                # 45% 紧接着再眨一下，形成"连眨"，其余歇 1~2 秒。
                # 时长给到 0.12~0.16：VTS 端还有输入平滑，太快会闭不严。
                self.blink_dur = self.rng.uniform(0.12, 0.16)
                delay = (self.blink_dur + self.rng.uniform(0.07, 0.15) if r < 0.45
                         else self.rng.uniform(1.1, 2.2))
            else:
                if r < 0.10:
                    self.blink_dur = self.rng.uniform(0.24, 0.38)   # 慢眨（走神/困）
                elif r < 0.30:
                    self.blink_dur = self.rng.uniform(0.15, 0.20)   # 普通
                else:
                    self.blink_dur = self.rng.uniform(0.10, 0.14)   # 快眨
                # 真人眨眼 3~6 秒一次；原来 2.0~6.5 偏慢而且有很长一段不动
                delay = self.rng.uniform(1.8, 4.8)
                if self.rng.random() < 0.28:     # 偶发二次眨眼
                    delay = self.blink_dur + self.rng.uniform(0.09, 0.22)
            self.next_blink = now + delay
        phase = (now - self.blink_t0) / self.blink_dur
        if 0.0 <= phase <= 1.0:
            # 不对称包络：闭眼快（前 30%）、完全闭合保持一小段、睁开慢（后 54%）。
            # 闭合段留平台是必须的：60fps 采样不一定正好落在最低点，
            # 没有平台就会出现"眨眼只眯到 0.02"的假闭合（尴尬的急眨尤其明显）。
            if phase < 0.30:
                v = phase / 0.30
                return max(0.0, 1.0 - v * v * (3 - 2 * v))
            if phase < 0.46:
                return 0.0
            v = (phase - 0.46) / 0.54
            return max(0.0, 0.98 * v * v * (3 - 2 * v))
        return 1.0

    def react_layer(self, now):
        add = {}
        with self.lock:
            alive = []
            for tracks, start, dur, inten in self.pulses:
                t_ms = (now - start) * 1000.0
                if 0 <= t_ms <= dur:
                    for param, keys in tracks.items():
                        if not isinstance(keys, list) or len(keys) < 2: continue
                        xs = [k[0] for k in keys]; ys = [k[1] for k in keys]
                        add[param] = add.get(param, 0.0) + float(np.interp(t_ms, xs, ys)) * inten
                    alive.append((tracks, start, dur, inten))
            self.pulses = alive
            for name, (vals, expire) in list(self.sustains.items()):
                if now > expire:
                    self.sustains.pop(name, None); continue
                for k, v in vals.items():
                    add[k] = add.get(k, 0.0) + float(v)
        return add

    def _update_saccade(self, now):
        if now >= self.next_saccade:
            # 扫视幅度：扩大范围让眼球动得更明显（人眼本来就能扫到接近极限）
            nx = self.rng.uniform(-0.95, 0.95)
            ny = self.rng.uniform(-0.65, 0.65)
            # main sequence：扫视时长随幅度增长（幅度越大耗时越久）
            dist = math.dist(self.gaze_target, (nx, ny))
            self._saccade_tau = 0.03 + 0.03 * min(1.0, dist / 0.9)   # 30~60ms
            self.gaze_target = (nx, ny)
            # 扫视间隔长尾分布：均值约 1.2s，偶尔很长（盯着不动）
            u = max(1e-3, self.rng.random())
            # 扫视间隔：真人注视平均 1 秒左右就会跳一次；原来均值偏长、上限 4.5s
            # 会让眼睛长时间发呆。收紧到 3.0s 上限、系数 0.85。
            # 情绪会改这个节奏：尴尬/紧张眼神乱瞟（倍率 >1），害怕/轻蔑看定一处（<1）。
            span = min(3.0, -0.85 * math.log(u) + 0.10)
            self.next_saccade = now + max(0.25, span / max(0.3, self._gaze_rate))
            # 大视线转移时 30% 概率伴随眨眼（Eyes Alive）
            if dist > 0.45 and self.rng.random() < 0.30:
                self.blink_t0 = now
                self.blink_dur = 0.13
        # 注视期间的微扫视（生理眼震）：目标点的微小抖动
        # ValueNoise 的 x 必须用相对时间（now - t0）；绝对时间会让它无限增长卡死引擎
        tn = now - self.t0
        px, py = self.gaze_pos
        tx, ty = self.gaze_target
        jx = self.noise_breath.at(tn * 6.7) * 0.012
        jy = self.noise_yaw.at(tn * 5.9) * 0.012
        tau = getattr(self, "_saccade_tau", 0.045)
        k = 1.0 - math.exp(-(1.0 / FPS) / tau)
        self.gaze_pos = (px + (tx + jx - px) * k, py + (ty + jy - py) * k)
        return self.gaze_pos

    def gaze_layer(self, now):
        with self.lock: gx, gy = self.gaze
        sx, sy = self._update_saccade(now)
        # 情绪视线偏置：无语要飘走、委屈要低头、害羞要躲开。
        # 没有这一段，眼睛永远在匀速扫视，情绪就只剩"贴图"
        bx, by = self._current_gaze_bias(now)
        # 鼠标注视 + 自然扫视 + 情绪偏置叠加；头部只跟一小部分，避免像摇头娃娃
        eye_x = float(np.clip(-gx * 0.55 - sx * 0.85 + bx * 0.85, -0.95, 0.95))
        eye_y = float(np.clip(-gy * 0.45 - sy * 0.55 + by * 0.75, -0.85, 0.85))
        # 情绪偏置也带一点头动，眼睛和头一起转才像"在看别处"
        return {"ParamEyeBallX": eye_x, "ParamEyeBallY": eye_y,
                "ParamAngleX": gx * 2.4 + sx * 1.9 + bx * 4.5,
                "ParamAngleY": -gy * 1.5 - sy * 1.4 + by * 3.0}

    def _current_gaze_bias(self, now):
        """取当前情绪带来的视线偏置（到时间就衰减回 0）。"""

        if not self._gaze_bias or (0.0, 0.0) == self._gaze_bias:
            return (0.0, 0.0)
        if now >= self._gaze_bias_until:
            self._gaze_bias = (0.0, 0.0)
            return (0.0, 0.0)
        age = now - self._pose_at
        bx, by = self._gaze_bias
        scale = decayed(1.0, age, max(1.0, self._pose_seconds))
        return (bx * scale, by * scale)

    # ---- 合成
    def compose(self, now):
        # 情绪结束了：眨眼节奏、视线扫视速度都要回归常态
        if (self._blink_style or self._gaze_rate != 1.0) and self._emotion_weight(now) <= 0.0:
            self._blink_style = ""
            self._gaze_rate = 1.0
        self._ambient(now)
        self._expire_poses(now)
        micro = self.micro_layer(now)
        react = self.react_layer(now)
        gaze = self.gaze_layer(now)
        expr = self.expression_layer(now)
        body = self.body_layer(now)
        # 没有动作层：姿态全部由 micro（呼吸/摇摆/漂移）+ gaze + react 合成
        vals = {}
        for layer in (micro, gaze):
            for k, v in layer.items():
                if k in ("ParamAngleX", "ParamAngleY", "ParamAngleZ",
                         "ParamMouthForm", "ParamMouthX"):
                    vals[k] = vals.get(k, 0.0) + v
                else:
                    vals[k] = v
        # 情绪/手势：语义量直接叠加到语义通道
        sem = {}
        for layer in (react, self._pose_layer(now)):
            for k, v in layer.items():
                sem[k] = sem.get(k, 0.0) + v
        # 表情层（眉毛）与身体层都走独立输入通道，不进语义合成：
        # 它们各自用不同的噪声频率，才能和头部形成"各自的节奏"
        extra_inputs = dict(expr)
        extra_inputs.update(body)
        # 眨眼：作为倍率作用在动作本身给出的睁眼量上，避免眼睛整体跳变
        blink = self.blink_ratio(now)
        # 默认常态微睁（L2D 单位满值 1.7），眨眼系统负责闭合
        base_eye = max(0.0, vals.get("ParamEyeOpenL", 1.45))
        # 常规状态不要长时间保持"眯眼"，但也不能一直顶到满值：
        # 实测这台模型眼睛满值 1.87，常态顶满就没有"瞪眼"的空间，
        # 于是"惊讶/紧张/害怕"睁眼在脸上等于没发生。常态收到 1.45（约 86%），
        # 给情绪留出 15% 左右的可见余量。
        base_eye = max(1.05, min(1.45, base_eye))
        # 这里不乘 blink：眨眼统一在输出弹簧之后施加，否则 3Hz 低通的弹簧
        # 会把闭合拖不到底，看起来就是"眨眼没闭严"。
        vals["ParamEyeOpenL"] = base_eye
        vals["ParamEyeOpenR"] = base_eye
        # 全局头部缩放。0.52 太保守，模型看着像"僵住"；0.85 仍远在人体舒适范围内，
        # 但转头/点头明显可见 —— 这是"灵动"最主要的来源。
        HEAD_GAIN = 1.0
        for hk in ("ParamAngleX", "ParamAngleY"):
            if hk in vals:
                vals[hk] *= HEAD_GAIN
        # 人体工学限幅 + roll 跟随 yaw（避免独立大角度歪头）
        def _knee(v, lo, hi):
            span = hi - lo; knee = 0.85
            if v > hi - span*(1-knee):
                over = v - (hi - span*(1-knee)); lim = hi - span*(1-knee)
                return lim + span*(1-knee)*math.tanh(over/ (span*(1-knee)))
            if v < lo + span*(1-knee):
                over = (lo + span*(1-knee)) - v; lim = lo + span*(1-knee)
                return lim - span*(1-knee)*math.tanh(over/(span*(1-knee)))
            return v
        # 限幅：X（转头）放到 ±30 —— 实测转身/转头的"身/头比"高达 3.2，
        # 是驱动身体最划算的通道；Y（点头）收窄到 -10~+12，因为它几乎不带动身体
        # （身/头比 0.05），只在头上去晃，是"头动身不动"的主要成因。
        # 身/头比恒定 3.4:1，所以"身体僵"的解法只有一个：在自然范围内把转头给足。
        # 真人直播转头常在 ±25°，这里放到 ±28 已经接近上限，再大就会显得抽风。
        if "ParamAngleX" in vals:
            vals["ParamAngleX"] = _knee(vals["ParamAngleX"], -28.0, 28.0)
        if "ParamAngleY" in vals:
            vals["ParamAngleY"] = _knee(vals["ParamAngleY"], -10.0, 12.0)  # 抬头少低头多
        # roll 跟随 yaw + 保留独立成分，限 ±13（歪头是"可爱/灵动"的关键表情）
        yaw_v = vals.get("ParamAngleX", 0.0)
        roll_ind = vals.get("ParamAngleZ", 0.0) * 0.55
        vals["ParamAngleZ"] = _knee(yaw_v * 0.34 + roll_ind, -13.0, 13.0)
        # 渲染 -> 注入
        inputs = l2d_to_inputs(vals)
        # 表情层/身体层直接写输入空间（不走语义映射）
        inputs.update(extra_inputs)
        # 自适应增益：按实测影响力放大/缩小各通道。
        # 对"吃得动"的通道放大，对"吃不动的"收敛 —— 这样同一套代码在
        # 36 参数的模型和 8 参数的模型上都有合适的幅度，不会一个过火一个发呆。
        probe = getattr(self, "probe", None)
        if probe is not None and probe.strengths:
            scaled = {}
            for k, v in inputs.items():
                g = probe.gain(k)
                if g != 1.0:
                    scaled[k] = v * g
                else:
                    scaled[k] = v
            inputs = scaled

        # 最后一道自适应过滤：只保留当前模型真实支持的参数。
        # VTS 对「整批都无效」的注入直接回 453 并整批丢弃，混入一个有效参数才不报错，
        # 所以宁可在这里过滤干净，也不要指望服务端容错。
        if not self.profile.empty:
            dropped = [k for k in inputs if k not in self.profile.valid]
            if dropped:
                if not getattr(self, "_drop_logged", False):
                    log("模型不支持的参数已跳过:", dropped)
                    self._drop_logged = True
            inputs = {k: v for k, v in inputs.items() if k in self.profile.valid}
        if not inputs:
            return {}
        # 手势/入场 pulse：沿用原来的半幅（脉冲短促，给满会像甩脸）。
        # 眼球（EyeLeftX/Y、EyeRightX/Y）与 MouthOpen 也在这里落下去 ——
        # 之前只认嘴角/眉毛/眼睑，cortico 的 eyeroll / look_around / eyes_wide
        # 在脸上等于没发生。
        self._blend_face(inputs, {k: v * self.SEM_GAINS.get(k, 1.0) for k, v in sem.items()})
        # 情绪的面部偏置：眉毛/嘴角/眼睑/脸颊全额给，自带淡入与平滑回落
        self._blend_face(inputs, self._emotion_face_layer(now))
        # 情绪 sustain 可能带来模型没有的通道：上面的 sem 叠加会凭空创建 key，
        # 而 VTS 对含未知参数的整批注入直接回 453 并全部丢弃（模型就"不动了"）。
        # 所以叠加完必须再过滤一次。
        if not self.profile.empty:
            inputs = {k: v for k, v in inputs.items() if k in self.profile.valid}
        # 输出弹簧（二阶临界阻尼）：让运动带惯性，避免线性折返
        dt = 1.0 / FPS
        omega = 2 * math.pi * 3.0        # 约 3Hz；w*dt 必须 <0.5 否则欧拉积分发散
        # 各通道的合法输出范围（超出会被 VTS 拒绝）
        legal = {"FaceAngleX": 30.0, "FaceAngleY": 25.0, "FaceAngleZ": 35.0}
        smoothed = {}
        for k, v in inputs.items():
            if not math.isfinite(v):
                v = self._spring_x.get(k, 0.0)
                if not math.isfinite(v):
                    v = 0.0
            x = self._spring_x.get(k)
            if x is None or not math.isfinite(x) or not math.isfinite(self._spring_v.get(k, 0.0)):
                self._spring_x[k] = v
                self._spring_v[k] = 0.0
                x = v
            vel = self._spring_v.get(k, 0.0)
            delta = abs(v - x)
            # 幅度加大后，大 delta 会把 gain 顶到 1.8、弹簧变硬，反而在换段/beat
            # 时产生尖峰（实测超限 3 次）。上限收到 1.35，既保留"跟得上"的手感，
            # 又不会在大动作时抖一下。
            gain = 1.0 + min(0.35, delta * 0.05)
            w = omega * gain
            acc = w * w * (v - x) - 2.0 * w * vel
            vel += acc * dt
            x += vel * dt
            # 数值防护：超范围就钳制并衰减速度
            lim = legal.get(k)
            if lim is not None and abs(x) > lim:
                x = math.copysign(lim, x)
                vel *= 0.3
            if not math.isfinite(x):
                x, vel = v, 0.0
            self._spring_x[k] = x
            self._spring_v[k] = vel
            smoothed[k] = x
        # 眨眼直接覆盖（绕过输出弹簧）：眼睑闭合是几十毫秒的动作，
        # 被弹簧拖慢就会"闭不严"，尴尬那种急眨尤其明显。
        if blink < 1.0:
            for name in ("EyeOpenLeft", "EyeOpenRight"):
                if name in smoothed:
                    smoothed[name] = float(np.clip(inputs.get(name, 1.0) * blink, 0.0, 1.0))
        # 紧张/害怕的"绷不住"：低频小幅抖动（2~5Hz、1~2°）必须叠在弹簧之后 ——
        # 输出弹簧是 3Hz 低通，10Hz 过了弹簧只剩不到一成；VTS 那边还有输入平滑，
        # 频率再高也留不住，所以这里只做"轻微坐立不安"，不追求高频震颤。
        tremble = self._tremble * self._emotion_weight(now)
        if tremble > 0.0:
            tn = now - self.t0
            jitter_z = self.noise_breath.at(tn * 2.2) * tremble * 2.8
            jitter_y = self.noise_pitch.at(tn * 1.8) * tremble * 1.1
            if "FaceAngleZ" in smoothed:
                smoothed["FaceAngleZ"] = float(np.clip(smoothed["FaceAngleZ"] + jitter_z, -13.0, 13.0))
            if "FaceAngleY" in smoothed:
                smoothed["FaceAngleY"] = float(np.clip(smoothed["FaceAngleY"] + jitter_y, -10.0, 12.0))
        self._prev_inputs = smoothed
        self._track_jitter(smoothed)
        self.vts.inject(smoothed)
        self._expire_expressions(now)
        self._auto_props(now)
        return smoothed

    # ---- 自动道具：时不时把模型自己的道具掏出来演一下
    def _refresh_props(self):
        """重建道具候选：当前模型的物品 + 打开/关闭成对的热键。"""

        items = []
        if _ARGS.auto_props:
            try:
                items = self.vts.list_items()
            except Exception as exc:
                log("item list failed", repr(exc)[:80])
        candidates = prop_candidates(
            items,
            getattr(self.catalog, "pairs", {}) or {},
            getattr(self.catalog, "hotkeys", []) or [],
            getattr(self.catalog, "hotkey_assets", {}) or {},
        )
        self.props.set_candidates(candidates)
        log("prop candidates:",
            [(c.name, c.cls) for c in candidates] or "无")

    # ---- 动作词表：给「台词记号」用的清单，只列当前皮套真有的东西
    def actions_payload(self):
        """当前模型能演的全部动作（外观/表情/道具/小动作）。

        两个消费者都必须看到**真实资产**而不是写死的示例：
        * 提示词：告诉模型「你可以打哪些记号」；
        * 解析器：判断台词里的记号是不是合法动作。
        """

        catalog = getattr(self, "catalog", None)
        items = list(catalog.vocabulary()) if catalog is not None else []
        for base in sorted(getattr(catalog, "pairs", {}) or {}):
            items.append({"key": base, "kind": "prop", "aliases": []})
        for tag, labels in TAG_LABELS.items():
            if tag in TAG_TO_CLIP:
                items.append({"key": tag, "kind": "tag", "aliases": list(labels)})
        return {
            "model": str(getattr(catalog, "model_name", "") or ""),
            "actions": items,
        }

    def _check_model(self, now):
        """换皮检测：模型变了就重建参数集合、表情清单和道具候选。"""

        if now < self._next_model_check:
            return
        self._next_model_check = now + 8.0
        info = self.vts.current_model()
        name = str(info.get("modelName") or "")
        if not name:
            return
        previous = getattr(self, "_model_name", "")
        if not previous:
            self._model_name = name
            return
        if name == previous:
            return
        log("model changed:", previous, "->", name)
        self._model_name = name
        try:
            self.vts.refresh_inputs(force=True)
            self.profile = self.vts.profile
            catalog = self.vts.load_catalog()
            with self.lock:
                self.catalog = catalog
                self.expr = ExpressionState(catalog)
            self._reset_expressions()
            self._refresh_props()
            log("model adapted:", self.profile.describe())
        except Exception as exc:  # noqa: BLE001 - 换皮失败就继续用旧配置，别拖垮引擎
            log("model adapt failed", repr(exc)[:100])

    def _auto_props(self, now):
        """每秒最多检查一次，别让道具调度拖慢 60fps 注入。"""

        scheduler = getattr(self, "props", None)
        if scheduler is None or now < self._next_prop_check:
            return
        self._check_model(now)
        self._next_prop_check = now + 1.0
        emotion = self._pose_emotion
        for action in scheduler.tick(now, emotion):
            candidate = action.candidate
            if candidate.kind == "item":
                ok = self.vts.load_item(candidate.open_ref) if action.kind == "open" \
                    else self.vts.unload_item(candidate.open_ref)
            elif candidate.kind == "oneshot":
                # 单发动小动作：只在开始那一拍触发一次，没有"收回"动作
                ok = self.vts.trigger_hotkey(candidate.open_ref) if action.kind == "open" else True
            else:
                ref = candidate.open_ref if action.kind == "open" else candidate.close_ref
                ok = self.vts.trigger_hotkey(ref)
            log("auto prop", action.kind, candidate.name, "ok" if ok else "fail")
            # 物品装不上（有的物品不允许 API 加载）就从清单里剔掉，不反复试
            if not ok and candidate.kind == "item":
                scheduler.drop(candidate)

    def _expire_expressions(self, now):
        """每帧查一次有没有到期的表情/道具，有才真的下发。

        状态变化（切换/收起）由 set_expression/clear_expression 立即下发；
        这里只负责"时间到了"这种情况，所以每帧只做两个字典比较，
        不会给 60fps 循环增加 VTS 请求。
        """

        state = getattr(self, "expr", None)
        if state is None:
            return
        if not state.has_pending():
            return
        if now < getattr(self, "_next_expr_check", 0.0):
            return
        self._next_expr_check = now + 0.25
        self._apply_expressions()

    def _apply_expressions(self):
        """按当前表情状态下发一次开关。

        只在**状态变化时**调用（切换/到期），不是每帧都发：
        ExpressionActivationRequest 是"设置"语义但 VTS 每个请求都要应答，
        每帧发会把 60fps 的注入循环拖慢。表情本身一旦激活会一直保持。
        """

        state = getattr(self, "expr", None)
        if state is None:
            return
        # 到期回落：主动摘掉，避免表情一直盖住引擎的眉毛/眨眼。
        # 到期用的淡出时长跟情绪的 offset 走（退场比上场慢），切换时才用短淡出。
        gone = state.expired_events()
        if gone:
            fade = max(0.2, float(getattr(self, "_face_offset", 0.22)))
            for name in gone:
                self.vts.set_expression_state(self.catalog.file_of(name), False, fade)
            log("expression expired", gone)
        # 道具到期：触发热键收回（热键没有幂等语义，只能发一次）
        for base_name in state.expired_props():
            pair = self.catalog.pairs.get(base_name)
            if pair:
                self.vts.trigger_hotkey(pair[1])
            log("prop expired", base_name)

        # 先关旧的（互斥：VTS 里同时挂两个表情会叠成怪脸）
        for name in getattr(self, "_expr_close", []):
            self.vts.set_expression_state(self.catalog.file_of(name), False)
        self._expr_close = []
        # 再开当前的：淡入时长跟情绪的 rise 走，别用一刀切的 0.22
        fade_in = max(0.2, float(getattr(self, "_face_rise", 0.22)))
        for item in state.to_inject():
            if item.get("active"):
                self.vts.set_expression_state(
                    item["expressionFile"], True, fade_in)

    # 抽搐自检：单独看"相邻两帧的跳变"，比看范围更能反映人眼感知的突变。
    # 头部角度 >3°/帧、归一化参数 >0.15/帧 就记一次；/status 里能看到计数与最差值。
    #: 抽搐自检的通道阈值。注意不含 EyeOpen：眨眼是"弹簧之后直接覆盖"的瞬变，
    #: 眼睑一帧闭合本来就是真人行为，算进抽搐统计只会把计数刷爆。
    _JITTER_LIMITS = {"FaceAngleX": 3.0, "FaceAngleY": 3.0, "FaceAngleZ": 3.0,
                      "MouthSmile": 0.15,
                      "EyeLeftX": 0.15, "EyeRightX": 0.15, "EyeLeftY": 0.15,
                      "EyeRightY": 0.15, "BrowLeftY": 0.15, "BrowRightY": 0.15}

    def _track_jitter(self, values):
        """统计相邻帧跳变，用于判断是否出现抽搐。"""

        prev = getattr(self, "_jitter_prev", None)
        if prev is None:
            self._jitter_prev = dict(values)
            self._jitter_count = 0
            self._jitter_worst = (0.0, "")
            self._jitter_frames = 0
            return
        self._jitter_frames += 1
        worst = self._jitter_worst
        for name, limit in self._JITTER_LIMITS.items():
            if name not in values or name not in prev:
                continue
            jump = abs(values[name] - prev[name])
            if jump > limit:
                self._jitter_count += 1
            ratio = jump / limit
            if ratio > worst[0]:
                worst = (ratio, name)
        self._jitter_worst = worst
        self._jitter_prev = dict(values)

    def run(self):
        period = 1.0 / FPS
        nxt = time.time()
        while True:
            now = time.time()
            try: self.compose(now)
            except Exception as e: log("tick err", repr(e)[:100])
            nxt += period
            dt = nxt - now
            if dt > 0: time.sleep(dt)
            else: nxt = time.time()

ENGINE = None

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        ok, msg = False, "unknown"
        if u.path == "/tag":
            ok = ENGINE.play_tag(q.get("name", "nod"), float(q.get("intensity", 1.0)))
            msg = "tag " + q.get("name", "nod")
        elif u.path == "/gaze":
            ENGINE.set_gaze(float(q.get("x", 0)), float(q.get("y", 0))); ok, msg = True, "gaze"
        elif u.path == "/expression":
            # 按情绪词切换表情：/expression?name=无语&text=呜……有点难过
            # 传了 text 就按它的强度与语速计划时序；hold 省略=自动
            raw_hold = q.get("hold")
            ok, result = ENGINE.set_expression(
                q.get("name", ""), float(raw_hold) if raw_hold else None,
                text=q.get("text", ""))
            msg = result
        elif u.path == "/emotion":
            # 纯参数情绪：/emotion?name=无语（不切任何 .exp3.json）
            raw_hold = q.get("hold")
            ok = ENGINE.set_emotion(
                q.get("name", ""), float(raw_hold) if raw_hold else None,
                text=q.get("text", ""))
            msg = "emotion " + q.get("name", "")
        elif u.path == "/prop":
            # 动画面具/道具：/prop?name=狗尾巴草&hold=60（hold=0 表示收起）
            ok, result = ENGINE.set_prop(q.get("name", ""), float(q.get("hold", 60)))
            msg = result
        elif u.path == "/pose":
            # 外观/姿势（兔牙、钢盆、前倾…）：/pose?name=兔牙&hold=45&toggle=1
            # 这类不进情绪轮换，只认台词里的记号点单；toggle=1 时再说一次就收起
            ok, result = ENGINE.set_pose(
                q.get("name", ""),
                float(q.get("hold", 45)),
                str(q.get("toggle", "1")).lower() not in ("0", "false", "no"),
            )
            msg = result
        elif u.path == "/poses":
            ok = True; msg = json.dumps({
                "active": ENGINE.active_poses(),
                "all": ENGINE.catalog.display_names,
            }, ensure_ascii=False)
        elif u.path == "/actions":
            # 台词记号能用的动作清单（提示词与解析器共用同一份）
            ok = True; msg = json.dumps(ENGINE.actions_payload(), ensure_ascii=False)
        elif u.path == "/expression/clear":
            ENGINE.clear_expression(); ok, msg = True, "cleared"
        elif u.path == "/reset":
            # 场景重置（cortico 的【Reset】）：表情/道具/姿势全收，视线回镜头
            ok, msg = True, ENGINE.reset_stage()
        elif u.path == "/expressions":
            # 列出当前模型可用的情绪表情与道具，供页面/调试用。
            # emotion_map 是标定结果（情绪 -> 表情名），换模型后一眼能看出哪张脸对应什么情绪。
            ok = True; msg = json.dumps({
                "model": ENGINE.catalog.model_name,
                "emotions": ENGINE.catalog.emotion_names,
                "emotion_map": ENGINE.catalog.labeled,
                "unlabeled": ENGINE.catalog.unknown_names,
                "props": sorted(ENGINE.catalog.pairs),
                "active": ENGINE.expr.active_expressions(),
                "poses": ENGINE.active_poses(),
            }, ensure_ascii=False)
        elif u.path == "/props":
            # 自动道具：能列出现有候选，方便确认"这个皮套到底有什么可用的"
            ok = True; msg = json.dumps({
                "auto": bool(ENGINE.props.enabled),
                "active": ENGINE.props.active_name,
                "candidates": [
                    {"name": c.name, "kind": c.kind, "class": c.cls}
                    for c in ENGINE.props.candidates()
                ],
            }, ensure_ascii=False)
        elif u.path == "/status":
            ok = True; msg = json.dumps({
                "mode": "params-only",
                "expression_model": ENGINE.catalog.model_name,
                "expression_names": len(ENGINE.catalog.expressions),
                "expression_emotions": len(ENGINE.catalog.labeled),
                "expression_unlabeled": len(ENGINE.catalog.unknown_names),
                "expression_props": sorted(ENGINE.catalog.pairs),
                "auto_props": bool(ENGINE.props.enabled),
                "auto_props_active": ENGINE.props.active_name,
                "auto_props_candidates": len(ENGINE.props.candidates()),
                "expression_active": ENGINE.expr.active_expressions(),
                "expression_props_active": ENGINE.expr.active_props(),
                "vts_connected": ENGINE.vts.ws is not None,
                "vts_error": getattr(ENGINE.vts, "last_error", ""),
                "jitter_frames": getattr(ENGINE, "_jitter_frames", 0),
                "jitter_over_limit": getattr(ENGINE, "_jitter_count", 0),
                "jitter_worst_ratio": round(getattr(ENGINE, "_jitter_worst", (0.0, ""))[0], 2),
                "jitter_worst_param": getattr(ENGINE, "_jitter_worst", (0.0, ""))[1],
                "instance_guard": "多个引擎实例同时注入会导致抽搐，启动时会自动清理旧实例",
                "pulses": len(ENGINE.pulses),
                "sustains": list(ENGINE.sustains),
                "emotion": ENGINE._pose_emotion,
                "emotion_face": sorted(ENGINE._face_hold),
                "ambient_clips": len(ENGINE.ambient_clips)})
        b = json.dumps({"ok": ok, "msg": msg}).encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type","application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

def main():
    global ENGINE
    # 作为子进程运行时 stdout 会被插件接管，这里把 logging 接到 stdout
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    ENGINE = Engine()
    log("纯参数模式（不播放任何 VTS 动画/待机动作）| clips:",
        len(ENGINE.clips.get("pulse") or {}), "pulse,",
        len(ENGINE.clips.get("sustain") or {}), "sustain")
    threading.Thread(target=ENGINE.run, daemon=True).start()
    log("control API http://127.0.0.1:%d/" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
