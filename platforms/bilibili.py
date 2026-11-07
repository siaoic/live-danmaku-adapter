"""B 站直播弹幕链路：基于本地内置的 blivedm（vendor/blivedm）。

blivedm 自带断线重连、心跳与 WBI 签名，这里只负责：
1. 把 blivedm 的同步回调翻译成归一化的 LiveEvent；
2. 维护房间连接的生命周期；
3. 把链路状态回报给统一通道。
"""

from __future__ import annotations

import asyncio
import time
from http.cookies import SimpleCookie
from typing import Any, Dict, Optional

from aiohttp import ClientSession

from ..core.events import (
    KIND_DANMAKU,
    KIND_GIFT,
    KIND_GUARD,
    KIND_LIVE_END,
    KIND_SUPER_CHAT,
    SOURCE_BILIBILI,
    LiveEvent,
)
from ..core.channel import LiveChannel
from ..vendor.blivedm import BaseHandler, BLiveClient
from ..vendor.blivedm.models import web as web_models

_RECONNECT_S = 5  # 链路异常退出后重建客户端的间隔（秒）
_STOP_TIMEOUT_S = 8  # 停止时等待链路收尾的上限（秒）
_LIVE_SILENCE_S = 90  # 心跳静默多久就判定掉线（blivedm 每 30 秒一次心跳，留 3 个周期）

# B 站对非浏览器 UA 会返回 412，这里固定用一个正常的 Chrome UA
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _clean_toast_msg(toast: str) -> str:
    """上舰提示语里的占位符是 <%名字%> 格式，去掉尖括号保留名字。"""

    return toast.replace("<%", "").replace("%>", "").strip()


class _Handler(BaseHandler):
    """单房间直播事件处理器。

    blivedm 的 handle() 是同步分发（库里刻意不做成协程，防止回调阻塞网络），
    所以这里只取字段、交给 channel.submit 入队，绝不在回调里 await。
    """

    def __init__(self, link: "BilibiliLiveLink") -> None:
        """初始化处理器。

        Args:
            link: 持有本处理器的链路，事件由它投递。
        """

        super().__init__()
        self._link = link

    # ===== 保活信号 =====

    def _on_heartbeat(self, client: Any, message: web_models.HeartbeatMessage) -> None:
        """心跳回包说明弹幕连接活着（blivedm 会在内部悄悄重连）。"""

        self._link.mark_live()

    # ===== 弹幕 =====

    def _on_danmaku(self, client: Any, message: web_models.DanmakuMessage) -> None:
        """普通弹幕：过滤后作为群聊消息注入，交给主程序的回复链路处理。"""

        # 跨房镜像弹幕不是本房间说的话，不能算到当前直播间头上
        if message.is_mirror:
            return
        emoticon_url = ""
        if message.dm_type == 1:
            emoticon_url = str(message.emoticon_options_dict.get("url") or "")
        self._link.submit(
            LiveEvent(
                source=SOURCE_BILIBILI,
                room_id=str(self._link.room_id),
                user_id=str(message.uid or 0),
                user_name=message.uname or "",
                kind=KIND_DANMAKU,
                text=message.msg or "",
                event_id=f"{message.timestamp}-{message.rnd}",
                avatar=message.face or "",
                # 弹幕里的 privilege_type 就是舰队身份：1 总督、2 提督、3 舰长
                guard_level=message.privilege_type or 0,
                timestamp=message.timestamp // 1000 if message.timestamp else 0,
                extra={
                    "live_user_level": str(message.user_level or 0),
                    "live_medal_name": message.medal_name or "",
                    "live_medal_level": str(message.medal_level or 0),
                    "live_emoticon_url": emoticon_url,
                },
            )
        )

    # ===== 礼物 =====

    def _on_gift(self, client: Any, message: web_models.GiftMessage) -> None:
        """礼物：blivedm 会把一次连击拆成多条消息，按平台侧 rnd 去重。"""

        self._link.submit(
            LiveEvent(
                source=SOURCE_BILIBILI,
                room_id=str(self._link.room_id),
                user_id=str(message.uid or 0),
                user_name=message.uname or "",
                kind=KIND_GIFT,
                text=message.gift_name or "",
                # blivedm 把一次连击拆成多条 GiftMessage：同一次连击里 tid/rnd 相同，
                # 只靠它们拼 event_id 会被 Host 当成重复消息丢掉，这里再带上礼物与数量
                event_id=self._link.next_event_id(
                    f"gift-{message.tid or message.rnd or message.timestamp}-{message.gift_id}-{message.num}"
                ),
                avatar=message.face or "",
                guard_level=message.guard_level or 0,
                # GiftMessage.price 是「瓜子数」（1000 金瓜子 = 1 元），要换算成元
                price=float((message.price or 0) / 1000),
                gift_name=message.gift_name or "礼物",
                gift_count=int(message.num or 1),
                timestamp=message.timestamp or 0,
                extra={"live_coin_type": message.coin_type or ""},
            )
        )

    # ===== 上舰 =====

    def _on_buy_guard(self, client: Any, message: web_models.GuardBuyMessage) -> None:
        """上舰（GUARD_BUY）：舰长/提督/总督的开通与续费。"""

        self._link.submit(
            LiveEvent(
                source=SOURCE_BILIBILI,
                room_id=str(self._link.room_id),
                user_id=str(message.uid or 0),
                user_name=message.username or "",
                kind=KIND_GUARD,
                text="",
                event_id=f"guard-{message.uid}-{message.start_time}-{message.guard_level}",
                guard_level=message.guard_level or 0,
                gift_name=message.gift_name or "",
                price=float((message.price or 0) / 1000),
                gift_count=int(message.num or 1),
                timestamp=message.start_time or 0,
            )
        )

    def _on_user_toast_v2(self, client: Any, message: web_models.UserToastV2Message) -> None:
        """上舰播报（USER_TOAST_MSG_V2 带完整提示语，比 GUARD_BUY 更适合朗读）。"""

        # source=2 是系统补发的「赠送」副本，官方评论栏也不显示，跳过以免重复播报
        if message.source == 2:
            return
        toast = _clean_toast_msg(message.toast_msg or "")
        if not toast:
            return
        self._link.submit(
            LiveEvent(
                source=SOURCE_BILIBILI,
                room_id=str(self._link.room_id),
                user_id=str(message.uid or 0),
                user_name=message.username or "",
                kind=KIND_GUARD,
                text=toast,
                event_id=f"toast-{message.uid}-{message.start_time}-{message.guard_level}",
                guard_level=message.guard_level or 0,
                timestamp=message.start_time or 0,
            )
        )

    # ===== 醒目留言 =====

    def _on_super_chat(self, client: Any, message: web_models.SuperChatMessage) -> None:
        """SC 醒目留言：付费高意愿消息，不做去重，直接注入。"""

        self._link.submit(
            LiveEvent(
                source=SOURCE_BILIBILI,
                room_id=str(self._link.room_id),
                user_id=str(message.uid or 0),
                user_name=message.uname or "",
                kind=KIND_SUPER_CHAT,
                text=message.message or "",
                event_id=f"sc-{message.id}",
                avatar=message.face or "",
                guard_level=message.guard_level or 0,
                price=float(message.price or 0),
                timestamp=message.start_time or 0,
                extra={
                    "live_sc_id": str(message.id or 0),
                    "live_sc_gift_name": message.gift_name or "",
                },
            )
        )

    # 进场/关注/分享/点赞这类互动消息（INTERACT_WORD_V2、LIKE_INFO_V3_*）一律不接：
    # 它们不含可聊的内容，进聊天流只会让 Bot 挨个欢迎人、感谢点赞。

    def on_client_stopped(self, client: Any, exception: Optional[Exception]) -> None:
        """客户端停止回调：记录连接已断开，由链路侧决定是否重建。"""

        self._link.mark_stopped(exception)


class BilibiliLiveLink:
    """一个 B 站直播间的弹幕链路。"""

    def __init__(self, channel: LiveChannel, room_id: int, sessdata: str = "") -> None:
        """初始化链路。

        Args:
            channel: 统一弹幕通道，事件最终交给它。
            room_id: B 站直播间号（房间号或短号）。
            sessdata: 可选的 B 站登录 Cookie SESSDATA。
        """

        self._channel = channel
        self._room_id = int(room_id or 0)
        self._sessdata = (sessdata or "").strip()
        self._run_task: Optional[asyncio.Task[None]] = None
        self._client: Optional[BLiveClient] = None
        self._session: Optional[ClientSession] = None
        self._stopping = True
        self._last_heartbeat_at = 0.0
        # 同一连击里的礼物消息平台侧 ID 会重复，用它兜底保证入站不互相去重
        self._event_seq = 0

    @property
    def room_id(self) -> int:
        """配置里的直播间号。"""

        return self._room_id

    @property
    def active(self) -> bool:
        """链路协程是否仍在运行。"""

        return self._run_task is not None and not self._run_task.done()

    def submit(self, event: LiveEvent) -> None:
        """把事件交给统一通道。"""

        self._channel.submit(event)

    def next_event_id(self, base_id: str) -> str:
        """给平台侧可能重复的 ID 追加自增序号，避免 Host 把不同礼物判成重复消息。

        Args:
            base_id: 平台侧原始事件标识。

        Returns:
            str: 带本链路自增序号的唯一事件 ID。
        """

        self._event_seq += 1
        return f"{base_id}-{self._event_seq}"

    def mark_live(self) -> None:
        """收到心跳回包，说明弹幕连接当前可用。"""

        was_live = self._is_live()
        self._last_heartbeat_at = time.time()
        if not was_live:
            uid = int(getattr(self._client, "uid", 0) or 0)
            who = f"登录身份 uid={uid}" if uid else "匿名（观众名会打码，SESSDATA 可能已失效）"
            self._channel.update_source(SOURCE_BILIBILI, connected=True, room=str(self._room_id), error="")
            self._channel.logger.info(f"[live] B站弹幕连接就绪：房间 {self._room_id}，{who}")

    def mark_stopped(self, exception: Optional[Exception]) -> None:
        """客户端停止回调：状态回落到未连接。"""

        self._last_heartbeat_at = 0.0
        error = "" if exception is None else f"{type(exception).__name__}: {exception}"
        self._channel.update_source(SOURCE_BILIBILI, connected=False, room=str(self._room_id), error=error)

    def _is_live(self) -> bool:
        """按最近一次心跳判断链路是否真的活着。"""

        return bool(self._last_heartbeat_at) and time.time() - self._last_heartbeat_at < _LIVE_SILENCE_S

    async def start(self) -> None:
        """启动链路（后台任务，不阻塞插件加载）。"""

        if self._run_task is not None and not self._run_task.done():
            return
        if self._room_id <= 0:
            self._channel.update_source(SOURCE_BILIBILI, connected=False, error="未配置有效的房间号")
            self._channel.logger.warning("[live] 未配置有效的 B 站房间号，B 站链路未启动")
            return
        self._stopping = False
        self._run_task = asyncio.create_task(self._run_loop(), name=f"bilibili-live-{self._room_id}")
        self._channel.logger.info(f"[live] B站链路已启动：房间 {self._room_id}")

    async def stop(self) -> None:
        """停止链路并释放连接资源。"""

        self._stopping = True
        client = self._client
        if client is not None and client.is_running:
            client.stop()  # 同步方法，取消网络协程后 join() 才会返回
        task, self._run_task = self._run_task, None
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=_STOP_TIMEOUT_S)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception as exc:  # 收尾异常不该阻止卸载，但要暴露出来
                self._channel.logger.error(f"[live] B站链路收尾异常：{type(exc).__name__}: {exc}")
        await self._cleanup()

    async def _run_loop(self) -> None:
        """连接直播间并常驻；异常退出后重建客户端。"""

        while not self._stopping:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self._channel.update_source(SOURCE_BILIBILI, connected=False, error=error)
                self._channel.logger.error(f"[live] B站弹幕链路异常：{error}")
            await self._cleanup()
            if self._stopping:
                break
            await asyncio.sleep(_RECONNECT_S)

    async def _connect_once(self) -> None:
        """建立一次弹幕连接，直到客户端停止（blivedm 内部自带断线重连）。"""

        self._session = ClientSession(headers={"User-Agent": _USER_AGENT})
        if self._sessdata:
            # SESSDATA 必须放进 cookie_jar：弹幕服务器对手写 Cookie 请求头的连接会直接断开，
            # 且 blivedm 的登录身份与 buvid 初始化也只认 cookie_jar。
            cookie = SimpleCookie()
            cookie["SESSDATA"] = self._sessdata
            cookie["SESSDATA"]["domain"] = "bilibili.com"
            cookie["SESSDATA"]["path"] = "/"
            self._session.cookie_jar.update_cookies(cookie)
        client = BLiveClient(self._room_id, session=self._session)
        client.set_handler(_Handler(self))
        self._client = client
        client.start()
        await client.join()

    async def _cleanup(self) -> None:
        """释放当前连接的客户端与会话（可重复调用）。"""

        client, self._client = self._client, None
        session, self._session = self._session, None
        if client is not None:
            try:
                await client.stop_and_close()
            except Exception as exc:
                self._channel.logger.error(f"[live] 关闭B站弹幕客户端失败：{type(exc).__name__}: {exc}")
        if session is not None and not session.closed:
            await session.close()