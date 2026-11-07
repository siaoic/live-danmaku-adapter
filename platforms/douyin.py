"""抖音直播弹幕链路：直连抖音 webcast 推送 WebSocket。

链路分四步：
1. 取 ttwid（访问 live.douyin.com 首页拿到，弹幕 WS 握手要靠它）；
2. 从 live.douyin.com/<web_rid> 页面里解析出真实 roomId；
3. 生成带签名的 wss 地址并连接；
4. 解 protobuf 推送帧，把弹幕/礼物翻译成 LiveEvent。

抖音没有公开的弹幕 API，这里用的是网页版实时推送通道，字段随抖音前端版本
演进，所以解析逻辑对未知消息一律跳过，只取用得到的字段。
"""

from __future__ import annotations

import asyncio
import gzip
import re
import zlib
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from ..core.channel import LiveChannel
from ..core.events import (
    KIND_DANMAKU,
    KIND_GIFT,
    KIND_LIVE_END,
    KIND_SUPER_CHAT,
    SOURCE_DOUYIN,
    LiveEvent,
)
from .douyin_sign import DouyinSigner
from .pb import decode_user, encode_bytes_field, encode_int_field, get_all, get_int, get_str, get_value

_RECONNECT_S = 5  # 链路异常退出后重建连接的间隔（秒）
_STOP_TIMEOUT_S = 8  # 停止时等待链路收尾的上限（秒）
_IDLE_TIMEOUT_S = 120  # 多久没收到任何帧就判定连接已死（抖音平时几秒就有一帧）

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# 抖音网页版推送通道地址；参数与官方前端保持一致，缺项会导致握手被拒
_WS_HOST = "webcast5-ws-web-hl.douyin.com"
_WS_PATH = "/webcast/im/push/v2/"
_DEFAULT_DID = "7319483754668557238"

# 握手被拒时服务端返回的 handshake-status 描述，用于把错误翻译成人话
_HANDSHAKE_ERRORS = {
    "415": "握手签名被拒（DEVICE_BLOCKED），通常是签名脚本过期或设备参数异常",
    "417": "握手 cookie 校验失败（缺少有效的 ttwid）",
}


def _decode_payload(payload: bytes) -> bytes:
    """解出推送帧的正文。

    抖音的帧大部分是 gzip，但个别帧（例如纯心跳/明文包）可能是 deflate、
    brotli 或直接是明文，按 gzip 解会直接抛异常。这里按实际压缩特征依次尝试，
    全部不成立时按原文返回，避免一帧异常拖垮整条连接。

    Args:
        payload: PushFrame 里的 payload 字段。

    Returns:
        bytes: 解压后的 protobuf 正文。
    """

    if payload[:2] == b"\x1f\x8b":
        return gzip.decompress(payload)
    # zlib 头：0x78 开头（deflate 带 zlib 包装）
    if len(payload) > 1 and payload[0] == 0x78:
        try:
            return zlib.decompress(payload)
        except zlib.error:
            pass
    # brotli 没有固定 magic，只能靠尝试；解不出来说明本来就是明文
    try:
        import brotli

        return brotli.decompress(payload)
    except Exception:
        pass
    return payload


def _ms_token(length: int = 107) -> str:
    """生成 msToken（107 位随机串），网页端每次请求都会带上。"""

    import random
    import string

    alphabet = string.ascii_letters + string.digits + "=_"
    return "".join(random.choice(alphabet) for _ in range(length))


class DouyinLiveLink:
    """一个抖音直播间的弹幕链路。"""

    def __init__(self, channel: LiveChannel, web_rid: str, extra_cookie: str = "") -> None:
        """初始化链路。

        Args:
            channel: 统一弹幕通道，事件最终交给它。
            web_rid: 抖音直播间号，即 live.douyin.com/<web_rid> 里的那串数字。
            extra_cookie: 可选的额外 Cookie 串，用于需要登录态的场景。
        """

        self._channel = channel
        self._web_rid = str(web_rid or "").strip()
        self._extra_cookie = (extra_cookie or "").strip()
        self._run_task: Optional[asyncio.Task[None]] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._stopping = True
        self._ttwid = ""
        self._room_id = ""
        self._last_frame_at = 0.0
        # 连击礼物等场景平台侧 ID 会重复，用它保证事件 ID 唯一
        self._event_seq = 0
        self._signer = DouyinSigner()

    @property
    def web_rid(self) -> str:
        """配置里的抖音直播间号。"""

        return self._web_rid

    @property
    def active(self) -> bool:
        """链路协程是否仍在运行。"""

        return self._run_task is not None and not self._run_task.done()

    def submit(self, event: LiveEvent) -> None:
        """把事件交给统一通道。"""

        self._channel.submit(event)

    def _next_event_id(self, base_id: str) -> str:
        """给平台侧可能重复的 ID 追加自增序号。

        Args:
            base_id: 平台侧原始事件标识。

        Returns:
            str: 带本链路自增序号的唯一事件 ID。
        """

        self._event_seq += 1
        return f"{base_id}-{self._event_seq}"

    async def start(self) -> None:
        """启动链路（后台任务，不阻塞插件加载）。"""

        if self._run_task is not None and not self._run_task.done():
            return
        if not self._web_rid:
            self._channel.update_source(SOURCE_DOUYIN, connected=False, error="未配置直播间号")
            self._channel.logger.warning("[live] 未配置抖音直播间号，抖音链路未启动")
            return
        self._stopping = False
        self._run_task = asyncio.create_task(self._run_loop(), name=f"douyin-live-{self._web_rid}")
        self._channel.logger.info(f"[live] 抖音链路已启动：直播间 {self._web_rid}")

    async def stop(self) -> None:
        """停止链路并释放连接资源。"""

        self._stopping = True
        task, self._run_task = self._run_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._cleanup()
        self._signer.release()
        self._channel.update_source(SOURCE_DOUYIN, connected=False)

    async def _run_loop(self) -> None:
        """连接直播间并常驻；异常退出后重建连接。"""

        while not self._stopping:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self._channel.update_source(SOURCE_DOUYIN, connected=False, room=self._web_rid, error=error)
                self._channel.logger.error(f"[live] 抖音弹幕链路异常：{error}")
            await self._cleanup()
            if self._stopping:
                break
            await asyncio.sleep(_RECONNECT_S)
    
    async def _run_once(self) -> None:
        """建立一次连接并持续收帧，直到连接断开或被要求停止。"""

        session = await self._ensure_session()
        await self._refresh_ttwid(session)
        self._room_id = await self._resolve_room_id(session)
        ws_url = self._build_ws_url(self._room_id)
        ws_url = f"{ws_url}&signature={self._signer.sign(ws_url)}"
        headers = {
            "cookie": self._build_cookie(),
            "user-agent": _USER_AGENT,
            "Origin": "https://live.douyin.com",
            "Referer": f"https://live.douyin.com/{self._web_rid}",
        }
        async with session.ws_connect(ws_url, headers=headers, heartbeat=None, max_msg_size=0) as ws:
            self._last_frame_at = time.time()
            self._channel.update_source(SOURCE_DOUYIN, connected=True, room=self._room_id, error="")
            self._channel.logger.info(f"[live] 抖音弹幕连接就绪：直播间 {self._web_rid}（roomId {self._room_id}）")
            while not self._stopping:
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=_IDLE_TIMEOUT_S)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(f"抖音弹幕连接 {_IDLE_TIMEOUT_S} 秒没有数据，判定已断开") from exc
                if message.type == aiohttp.WSMsgType.BINARY:
                    await self._handle_frame(ws, message.data)
                elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                    raise RuntimeError(f"抖音弹幕连接已关闭：{ws.close_code}")

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """复用（或新建）HTTP 会话。"""

        session = self._session
        if session is not None and not session.closed:
            return session
        session = aiohttp.ClientSession(headers={"User-Agent": _USER_AGENT})
        self._session = session
        return session

    async def _refresh_ttwid(self, session: aiohttp.ClientSession) -> None:
        """访问直播间首页取 ttwid；握手没有它会被 417 拒绝。"""

        async with session.get("https://live.douyin.com/") as response:
            cookie = response.cookies.get("ttwid")
        if cookie is None or not cookie.value:
            raise RuntimeError("访问抖音直播首页没有拿到 ttwid cookie")
        self._ttwid = cookie.value

    def _build_cookie(self) -> str:
        """拼出握手用的 Cookie 串。"""

        parts = [f"ttwid={self._ttwid}", f"msToken={_ms_token()}"]
        if self._extra_cookie:
            parts.append(self._extra_cookie)
        return "; ".join(parts)

    async def _resolve_room_id(self, session: aiohttp.ClientSession) -> str:
        """从直播间页面解析真实 roomId。"""

        url = f"https://live.douyin.com/{self._web_rid}"
        async with session.get(url, headers={"cookie": f"ttwid={self._ttwid}"}) as response:
            html = await response.text()
        # 页面里的 JSON 是反斜杠转义过的，先还原成普通引号再匹配
        html = html.replace('\\"', '"')
        match = re.search(r'"roomId":"(\d{8,})"', html)
        if match is None:
            raise RuntimeError(
                f"抖音直播间 {self._web_rid} 页面里没有解析到 roomId，"
                "可能是直播间不存在、已下播或页面结构变化"
            )
        return match.group(1)

    def _build_ws_url(self, room_id: str) -> str:
        """拼出 webcast 推送地址（尚未签名）。"""

        internal_ext = (
            "internal_src:dim"
            f"|wss_push_room_id:{room_id}"
            f"|wss_push_did:{_DEFAULT_DID}"
            "|first_req_ms:1721106114541"
            "|fetch_time:1721106114633"
            "|seq:1"
            "|wss_info:0-1721106114633-0-0"
            "|wrds_v:7392094459690748497"
        )
        params: List[str] = [
            "app_name=douyin_web",
            "version_code=180800",
            "webcast_sdk_version=1.0.14-beta.0",
            "update_version_code=1.0.14-beta.0",
            "compress=gzip",
            "device_platform=web",
            "cookie_enabled=true",
            "screen_width=1536",
            "screen_height=864",
            "browser_language=zh-CN",
            "browser_platform=Win32",
            "browser_name=Mozilla",
            "browser_version=Mozilla%2F5.0",
            "browser_online=true",
            "tz_name=Asia/Shanghai",
            "cursor=d-1_u-1_fh-7392091211001140287_t-1721106114633_r-1",
            f"internal_ext={internal_ext}",
            "host=https://live.douyin.com",
            "aid=6383",
            "live_id=1",
            "did_rule=3",
            "endpoint=live_pc",
            "support_wrds=1",
            f"user_unique_id={_DEFAULT_DID}",
            "im_path=/webcast/im/fetch/",
            "identity=audience",
            "need_persist_msg_count=15",
            "insert_task_id=",
            "live_reason=",
            f"room_id={room_id}",
            "heartbeatDuration=0",
        ]
        return f"wss://{_WS_HOST}{_WS_PATH}?" + "&".join(params)

    async def _handle_frame(self, ws: aiohttp.ClientWebSocketResponse, frame: bytes) -> None:
        """解一帧推送数据并分发到事件处理。"""

        self._last_frame_at = time.time()
        log_id = get_int(frame, 2)
        payload = get_value(frame, 8)
        if not isinstance(payload, bytes) or not payload:
            return
        body = _decode_payload(payload)
        need_ack = get_int(body, 9)
        internal_ext = get_value(body, 5)

        for message in get_all(body, 1):
            if not isinstance(message, bytes):
                continue
            method = get_str(message, 1)
            raw = get_value(message, 2)
            if not isinstance(raw, bytes):
                continue
            self._dispatch(method, raw)

        if need_ack:
            # 服务端要求回 ack，不回会导致它停止推送
            ack = encode_int_field(2, log_id)
            ack += encode_bytes_field(7, b"ack")
            if isinstance(internal_ext, bytes):
                ack += encode_bytes_field(8, internal_ext)
            await ws.send_bytes(ack)

    def _dispatch(self, method: str, raw: bytes) -> None:
        """按消息类型把 protobuf 载荷翻译成 LiveEvent。"""

        handler = self._HANDLERS.get(method)
        if handler is None:
            # 点赞、在线人数、榜单等消息对聊天没有价值，静默跳过
            return
        try:
            handler(self, raw)
        except Exception as exc:
            # 单条消息解析失败不能拖垮整个连接
            self._channel.logger.warning(f"[live] 解析抖音消息 {method} 失败：{type(exc).__name__}: {exc}")

    def _common_event(self, event: LiveEvent) -> None:
        """补上公共字段后提交。"""

        event.room_id = event.room_id or self._room_id or self._web_rid
        self.submit(event)

    def _on_chat(self, raw: bytes) -> None:
        """弹幕消息 WebcastChatMessage：common=1、user=2、content=3。"""

        user = decode_user(get_value(raw, 2) or b"")
        common = get_value(raw, 1) or b""
        self._common_event(
            LiveEvent(
                source=SOURCE_DOUYIN,
                room_id=self._room_id,
                user_id=user["id"] or "0",
                user_name=user["nickname"],
                kind=KIND_DANMAKU,
                text=get_str(raw, 3),
                event_id=f"chat-{get_int(common, 2) or time.time_ns()}",
                avatar=user["avatar"],
                timestamp=int((get_int(common, 4) or 0) // 1000),
            )
        )

    def _on_gift(self, raw: bytes) -> None:
        """礼物 WebcastGiftMessage：user=7、gift=15、repeatCount=5、comboCount=6。"""

        user = decode_user(get_value(raw, 7) or b"")
        common = get_value(raw, 1) or b""
        gift = get_value(raw, 15) or b""
        # gift 结构里 name=16，价格 diamondCount=12（单位抖币，10 抖币 = 1 元）
        gift_name = get_str(gift, 16) or "礼物"
        diamond = get_int(gift, 12)
        count = get_int(raw, 6) or get_int(raw, 5) or 1
        self._common_event(
            LiveEvent(
                source=SOURCE_DOUYIN,
                room_id=self._room_id,
                user_id=user["id"] or "0",
                user_name=user["nickname"],
                kind=KIND_GIFT,
                text=gift_name,
                # 连击礼物会拆成多条消息且 msgId 可能重复，追加序号避免被 Host 去重误伤
                event_id=self._next_event_id(f"gift-{get_int(common, 2) or time.time_ns()}"),
                avatar=user["avatar"],
                price=round(diamond * count / 10, 2),
                gift_name=gift_name,
                gift_count=count,
                timestamp=int((get_int(common, 4) or 0) // 1000),
            )
        )

    def _on_control(self, raw: bytes) -> None:
        """直播状态 WebcastControlMessage：status=3 表示直播结束下播。"""

        if get_int(raw, 2) != 3:
            return
        self._common_event(
            LiveEvent(
                source=SOURCE_DOUYIN,
                room_id=self._room_id,
                user_id="0",
                user_name="",
                kind=KIND_LIVE_END,
                text="直播已结束",
                event_id=f"end-{time.time_ns()}",
            )
        )

    _HANDLERS: Dict[str, Any] = {}

    async def _cleanup(self) -> None:
        """释放当前会话（可重复调用）。"""

        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()


# 消息类型 → 处理函数；类体内直接引用会变成普通函数，所以在类定义后统一绑定。
# 进场（WebcastMemberMessage）、点赞（WebcastLikeMessage）不绑定：
# 它们没有内容可聊，只会在聊天流里刷屏。
DouyinLiveLink._HANDLERS = {
    "WebcastChatMessage": DouyinLiveLink._on_chat,
    "WebcastGiftMessage": DouyinLiveLink._on_gift,
    "WebcastControlMessage": DouyinLiveLink._on_control,
}