"""抖音 WebSocket 握手签名。

抖音对弹幕 WebSocket 握手做了一层前端混淆签名（X-Bogus），缺签名会被
直接拒绝（关闭前返回 handshake-status 415 / DEVICE_BLOCKED）。签名算法是
混淆过的前端 JS，无法用纯 Python 复现，所以这里内嵌官方前端原样的签名
脚本，交给 JS 运行时执行。

脚本来源：DouyinLiveWebFetcher（AGPL-3.0），assets/douyin_sign.js。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

import urllib.parse

_SIGN_SCRIPT_NAME = "douyin_sign.js"

# 参与签名的查询参数；顺序必须与抖音前端一致，改动会直接导致签名失效
_SIGN_PARAMS = (
    "live_id",
    "aid",
    "version_code",
    "webcast_sdk_version",
    "room_id",
    "sub_room_id",
    "sub_channel_id",
    "did_rule",
    "user_unique_id",
    "device_platform",
    "device_type",
    "ac",
    "identity",
)


class DouyinSigner:
    """抖音握手签名器：加载脚本一次，之后每次握手只调用函数。"""

    def __init__(self, script_path: Optional[Path] = None) -> None:
        """初始化签名器。

        Args:
            script_path: 签名脚本路径；留空时使用插件 assets 目录下的脚本。

        Raises:
            RuntimeError: JS 运行环境不可用或脚本加载失败时抛出。
        """

        self._script_path = script_path or (Path(__file__).resolve().parent.parent / "assets" / _SIGN_SCRIPT_NAME)
        self._context: Optional[object] = None

    def _ensure_context(self) -> object:
        """惰性加载 JS 运行时与签名脚本。"""

        if self._context is not None:
            return self._context
        try:
            from py_mini_racer import MiniRacer
        except ImportError as exc:
            raise RuntimeError(
                "抖音链路需要 JS 运行时来生成握手签名，但未安装 mini-racer，请执行 pip install mini-racer"
            ) from exc
        if not self._script_path.is_file():
            raise RuntimeError(f"抖音签名脚本缺失：{self._script_path}")
        context = MiniRacer()
        context.eval(self._script_path.read_text(encoding="utf-8"))
        self._context = context
        return context

    @staticmethod
    def build_md5(url: str) -> str:
        """按抖音前端的口径，把关键查询参数拼成待签名串再取 MD5。"""

        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        payload = ",".join(f"{name}={query.get(name, '')}" for name in _SIGN_PARAMS)
        return hashlib.md5(payload.encode("utf-8")).hexdigest()

    def sign(self, url: str) -> str:
        """返回该握手 URL 对应的 signature 参数值。"""

        context = self._ensure_context()
        return str(context.call("get_sign", self.build_md5(url)))

    def release(self) -> None:
        """释放 JS 运行时。"""

        self._context = None
