"""统一弹幕通道：收两个平台的事件 → 过滤 → 同一网关注入 MaiBot 聊天流。

B 站与抖音各有自己的连接协程，但它们只负责把平台事件翻译成 LiveEvent 并
交给本通道的 submit()。队列、过滤、限流、注入、统计全部集中在这里，
所以两个平台的弹幕走的是同一个通道、同一个聊天流。

线程/协程模型：插件跑在 Runner 子进程的 asyncio 事件循环里，各平台连接协程、
事件队列与注入协程共用同一个循环，不需要额外开线程。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, List, Optional

from .events import KIND_DANMAKU, KIND_GIFT, KIND_GUARD, KIND_SUPER_CHAT, LiveEvent, SOURCE_ASR
from .mapping import CHANNEL_ACCOUNT_ID, CHANNEL_PLATFORM, ChannelSettings, DanmakuFilter, build_message

_QUEUE_SIZE = 2000  # 事件队列上限：弹幕洪水时宁可丢弃也不让内存无界增长


class LiveChannel:
    """两个直播平台共用的一条弹幕通道。"""

    def __init__(self, ctx: Any, gateway_name: str) -> None:
        """初始化通道。

        Args:
            ctx: 插件上下文（用 ctx.gateway 注入消息、ctx.logger 记日志）。
            gateway_name: 本插件声明的消息网关组件名。
        """

        self._ctx = ctx
        self._gateway_name = gateway_name
        self._settings = ChannelSettings()
        self._filter = DanmakuFilter()
        self._enabled_kinds: Dict[str, bool] = {}
        self._queue: Optional[asyncio.Queue[LiveEvent]] = None
        self._pump_task: Optional[asyncio.Task[None]] = None
        self._stopping = True
        self._ready = False
        self._seq = 0
        self._log_events = False
        # 各平台链路自己维护连接状态，通道只汇总展示
        self._sources: Dict[str, Dict[str, Any]] = {}
        # 入站回调：插件用它"听到观众说话就做表情"，不阻塞消息注入
        self._inbound_hook: Optional[Callable[[LiveEvent, str], Any]] = None
        self._stats: Dict[str, Any] = {
            "injected": 0,
            "dropped": 0,
            "failed": 0,
            "last_error": "",
        }

    # ===== 生命周期 =====

    async def start(self, settings: ChannelSettings, enabled_kinds: Dict[str, bool], log_events: bool) -> None:
        """启动通道的注入协程（不阻塞插件加载）。

        Args:
            settings: 映射参数。
            enabled_kinds: 事件类型开关。
            log_events: 是否逐条打印已注入事件。
        """

        if self._pump_task is not None and not self._pump_task.done():
            return
        self.apply_config(settings, enabled_kinds, log_events)
        self._stopping = False
        self._queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._pump_task = asyncio.create_task(self._pump_loop(), name="live-channel-pump")

    async def stop(self) -> None:
        """停止注入协程并释放队列。"""

        self._stopping = True
        task, self._pump_task = self._pump_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._queue = None
        await self.set_ready(False, {})

    def apply_config(self, settings: ChannelSettings, enabled_kinds: Dict[str, bool], log_events: bool) -> None:
        """原地更新映射参数与事件开关，不需要重启通道。

        Args:
            settings: 映射参数。
            enabled_kinds: 事件类型开关。
            log_events: 是否逐条打印已注入事件。
        """

        self._settings = settings
        self._enabled_kinds = enabled_kinds
        self._log_events = log_events

    # ===== 网关就绪 =====

    async def set_ready(self, ready: bool, metadata: Dict[str, Any]) -> bool:
        """向 Host 上报网关就绪状态；只有 ready 之后注入才会被接受。

        Args:
            ready: 链路是否可用。
            metadata: 随状态上报的运行时元数据。

        Returns:
            bool: Host 是否接受本次状态更新。
        """

        try:
            accepted = await self._ctx.gateway.update_state(
                gateway_name=self._gateway_name,
                ready=ready,
                platform=CHANNEL_PLATFORM,
                account_id=CHANNEL_ACCOUNT_ID,
                metadata=metadata,
            )
        except Exception as exc:
            self._stats["last_error"] = f"网关状态上报失败：{type(exc).__name__}: {exc}"
            self.logger.error(f"[live] {self._stats['last_error']}")
            self._ready = False
            return False
        self._ready = bool(ready and accepted)
        if ready and not accepted:
            self._stats["last_error"] = "消息网关就绪上报被主程序拒绝"
            self._ctx.logger.error(f"[live] {self._stats['last_error']}")
        return accepted

    @property
    def logger(self) -> Any:
        """返回插件日志器，供各平台链路复用同一套日志前缀。"""

        return self._ctx.logger

    def configure_filter(
        self,
        dedup_window_s: int,
        dedup_max_entries: int,
        drop_bracket: bool,
        max_text_len: int,
    ) -> None:
        """按配置重建防刷屏过滤器。

        Args:
            dedup_window_s: 同内容去重窗口（秒）。
            dedup_max_entries: 去重记录条数上限。
            drop_bracket: 是否丢弃方括号指令弹幕。
            max_text_len: 弹幕正文最长字符数。
        """

        self._filter = DanmakuFilter(
            dedup_window_s=dedup_window_s,
            dedup_max_entries=dedup_max_entries,
            drop_bracket=drop_bracket,
            max_text_len=max_text_len,
        )

    @property
    def ready(self) -> bool:
        """网关当前是否已就绪。"""

        return self._ready

    # ===== 事件投递 =====

    def submit(self, event: LiveEvent) -> None:
        """平台连接回调的入口：过滤后入队，队列满时丢弃并计数。"""

        if not self._enabled_kinds.get(event.kind, True):
            return
        if event.kind == KIND_DANMAKU:
            filtered = self._filter.accept(event.text, time.time())
            if filtered is None:
                self._stats["dropped"] += 1
                return
            event.text = filtered
        elif event.kind in (KIND_SUPER_CHAT, KIND_GIFT, KIND_GUARD):
            # 付费事件正文不能为空，否则播放出来是一行空播报
            event.text = (event.text or "").strip()
            if not event.text and event.kind == KIND_SUPER_CHAT:
                self._stats["dropped"] += 1
                return
        queue = self._queue
        if queue is None:
            return
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            self._stats["dropped"] += 1

    def set_inbound_hook(self, hook: Optional[Callable[[LiveEvent, str], Any]]) -> None:
        """注册入站回调：每条消息进了聊天流之后回调一次（同步/协程都支持）。

        插件用它实现"听到观众说话就做表情"。回调异常只记日志，绝不影响注入。
        """

        self._inbound_hook = hook

    def _notify_inbound(self, event: LiveEvent, text: str) -> None:
        """把入站正文交给插件；返回协程就在当前事件循环里起一个任务。"""

        hook = self._inbound_hook
        if hook is None:
            return
        try:
            result = hook(event, text)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception as exc:  # noqa: BLE001 - 回调不能影响直播
            self.logger.debug(f"[live] 入站回调失败：{type(exc).__name__}: {exc}")

    def submit_voice(self, text: str, user_name: str = "主人") -> bool:
        """把一句 ASR 转写当主播发言直接入队：不过防刷屏过滤与事件开关。

        主播语音是明确的对话意图：同一句话连说两遍、带方括号都该进聊天流，
        所以绕开 DanmakuFilter 与 enabled_kinds，只留队列满时的保护。
        走同一注入队列保证与弹幕事件的先后顺序。

        Args:
            text: 转写出的正文。
            user_name: 展示昵称，可配置成本地聊天室里的同一个昵称。

        Returns:
            bool: 是否成功入队（通道未启动或队列满返回 False）。
        """

        content = str(text or "").strip()
        if not content:
            return False
        queue = self._queue
        if queue is None:
            return False
        event = LiveEvent(
            source=SOURCE_ASR,
            room_id="local",
            user_id="host",
            user_name=str(user_name or "").strip() or "主人",
            kind=KIND_DANMAKU,
            text=content,
            event_id=f"asr-{time.time_ns()}",
            # 主播当面说话必须得到回复：mention=1.0 → additional_config 数值型
            # is_mentioned，宿主强制下一轮 Planner 触发，不受回复概率配置影响
            mention=1.0,
            extra={"live_asr": "1"},
        )
        try:
            queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            return False

    async def _pump_loop(self) -> None:
        """单消费者：保持注入顺序，避免为每条弹幕开一个任务。"""

        queue = self._queue
        while True:
            assert queue is not None
            event = await queue.get()
            try:
                await self._inject(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._stats["last_error"] = f"{type(exc).__name__}: {exc}"
                self.logger.error(f"[live] 注入直播事件失败：{self._stats['last_error']}")

    async def _inject(self, event: LiveEvent) -> None:
        """把一条事件归一化后交给 Host。"""

        if not self._ready:
            # 链路刚起来但网关还没就绪时 Host 会直接拒收，这里先丢掉
            self._stats["dropped"] += 1
            return
        self._seq += 1
        message = build_message(event, self._seq, self._settings)
        accepted = await self._ctx.gateway.route_message(
            gateway_name=self._gateway_name,
            message=message,
            route_metadata={
                "live_source": event.source,
                "live_room_id": event.room_id,
                "live_event_kind": event.kind,
            },
            external_message_id=message["message_id"],
            dedupe_key=event.event_id or message["message_id"],
        )
        if accepted:
            self._stats["injected"] += 1
            text = str(message.get("processed_plain_text") or "")
            if self._log_events:
                self.logger.info(f"[live] {text}")
            elif self._stats["injected"] == 1:
                self.logger.info(f"[live] 首条直播消息已进入聊天流：{text[:60]}")
            self._notify_inbound(event, text)
            return
        self._stats["failed"] += 1
        self.logger.warning(f"[live] 消息被主程序拒收：{message['message_id']} {event.kind}")

    # ===== 状态汇总 =====

    def register_source(self, source: str, label: str) -> None:
        """登记一个平台链路，便于状态展示。

        Args:
            source: 平台标识。
            label: 展示名。
        """

        self._sources.setdefault(source, {"label": label, "connected": False, "room": "", "error": ""})

    def update_source(self, source: str, *, connected: bool, room: str = "", error: str = "") -> None:
        """更新某个平台链路的连接状态。

        Args:
            source: 平台标识。
            connected: 链路是否已连上。
            room: 当前房间号，空字符串表示未知。
            error: 最近的错误描述，空字符串表示无错误。
        """

        state = self._sources.setdefault(source, {"label": source, "connected": False, "room": "", "error": ""})
        state["connected"] = connected
        if room:
            state["room"] = room
        state["error"] = error

    def status(self) -> Dict[str, Any]:
        """返回通道整体状态，供状态命令展示。"""

        return {
            "ready": self._ready,
            "injected": self._stats["injected"],
            "dropped": self._stats["dropped"],
            "failed": self._stats["failed"],
            "queue_size": self._queue.qsize() if self._queue is not None else 0,
            "last_error": self._stats["last_error"],
            "sources": [dict(state, source=source) for source, state in self._sources.items()],
        }

    def source_summary(self) -> List[str]:
        """返回每个平台链路的单行展示文本。"""

        lines: List[str] = []
        for source, state in self._sources.items():
            room = state.get("room") or "未配置"
            connection = "已连接" if state.get("connected") else "未连接"
            line = f"{state.get('label') or source}：{connection}（房间 {room}）"
            if state.get("error"):
                line += f"，最近错误 {state['error']}"
            lines.append(line)
        return lines