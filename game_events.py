# -*- coding: utf-8 -*-
"""游戏事件：把「正在玩的游戏」变成主动对话的素材（纯事件驱动，不轮询）。

链路：
    game-hub 每推进一步  --POST-->  GameEventInbox（本插件 5687 端口）
                                         |
                                         v
                                    GameFeed.update()
                                         |
                                         v
                              主动对话拿 summary() 当解说素材

设计约束：
  * **没有定时器**：事件来了才动，没事件就安静，不做定时拉取；
  * **只读、可缺席**：game-hub 没开、推送失败，一律当作「没在玩」，
    绝不因为收不到事件就挡住主动对话；
  * **不碰弹幕**：素材只来自游戏端，主动轮仍然看不到直播间消息正文；
  * **只讲眼前**：最多带最近几步，避免把整盘棋谱塞进 intent 挤爆上下文。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

import asyncio
import time

from aiohttp import web

#: 默认端口：game-hub 往这里推事件
DEFAULT_EVENT_PORT = 5687


class GameFeed:
    """保存当前游戏事件，并整理成一句可给模型的素材。

    自身不主动拉取：由 ``GameEventInbox`` 收到推送后调用 ``update()``。
    """

    def __init__(self, *, max_recent: int = 6) -> None:
        """初始化。

        Args:
            max_recent: 素材里最多带几步最近动作。
        """

        self.max_recent = max(1, int(max_recent))
        self._payload: Optional[Dict[str, Any]] = None
        self._updated_at = 0.0
        # 每收到一次「有实质变化」的推送就 +1，供调用方判断是否有新事件
        self._version = 0
        # 「这盘已经解完了」只播报一次，之后不再重复说结束语
        self._finished_announced = False

    # ------------------------------------------------------------------ #
    # 接收
    # ------------------------------------------------------------------ #

    def update(self, payload: Dict[str, Any]) -> bool:
        """接收一次游戏事件推送。

        Args:
            payload: game-hub 推来的事件 JSON。

        Returns:
            bool: 是否算「有新动作」（版本号变了）。
        """

        if not isinstance(payload, dict) or not payload.get("ok", True):
            return False
        previous = self._payload or {}
        self._payload = payload
        self._updated_at = time.time()
        changed = (
            payload.get("step") != previous.get("step")
            or bool(payload.get("finished")) != bool(previous.get("finished"))
            or payload.get("active") != previous.get("active")
        )
        if changed:
            self._version += 1
            # 换局/重开：允许再播报一次结束语
            if not payload.get("finished"):
                self._finished_announced = False
        return changed

    def note_external_event(self) -> None:
        """记一次「别处发生了事」（日程换挡等），只推版本号，不动游戏素材。

        播完门控看的是 ``version > _consumed_game_version``。日程事件不该
        覆盖 ``_payload``（否则游戏解说素材会凭空消失），但必须让版本号前进，
        这样「换挡时正在播报、被等说完再想挡住」的那一轮，播完后还能补触发。
        """

        self._version += 1

    def mark_announced(self) -> None:
        """标记「结束语已经说过」，避免播完一段又重复一遍。"""

        data = self._payload
        if isinstance(data, dict) and data.get("finished"):
            self._finished_announced = True

    @property
    def version(self) -> int:
        """当前事件版本号（只看变化次数，不做时间判断）。"""

        return self._version

    @property
    def playing(self) -> bool:
        """现在是不是真的在玩。"""

        data = self._payload
        return bool(isinstance(data, dict) and (data.get("playing") or data.get("finished")))

    def reset(self) -> None:
        """清空（停游戏/换游戏时用）。"""

        self._payload = None
        self._updated_at = 0.0

    # ------------------------------------------------------------------ #
    # 素材
    # ------------------------------------------------------------------ #

    def summary(self) -> str:
        """整理成给主动对话 intent 的一句场景；没在玩就返回空串。

        刻意只说「在玩什么」，不报进度、不报步数、不报技巧名：
        那些数字一进上下文，模型就会照着念成数据播报，听起来像在读面板。
        """

        data = self._payload
        if not isinstance(data, dict):
            return ""
        if not data.get("playing") and not data.get("finished"):
            return ""
        if data.get("finished") and self._finished_announced:
            # 结束语已经讲过，不再重复；等下一局开始再放开
            return ""

        title = str(data.get("title") or data.get("active") or "游戏").strip()
        if data.get("finished"):
            return f"你刚把一盘「{title}」玩完了。"

        difficulty = str(data.get("difficulty_label") or data.get("difficulty") or "").strip()
        scene = f"你正在直播玩「{title}」"
        if difficulty:
            scene += f"（{difficulty}）"
        return scene + "。"


class GameEventInbox:
    """一个极小的 HTTP 服务，只负责收 game-hub 推来的游戏事件。

    为什么单独开端口而不是塞进字幕/弹幕墙：那两个服务都是可关闭的展示层，
    主动对话的素材来源不该因为关字幕就断掉。这里只听回环，外部访问不到。
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_EVENT_PORT,
        *,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        logger: Any = None,
    ) -> None:
        """初始化（不监听端口，等 start()）。

        Args:
            host: 监听地址，默认只听回环。
            port: 端口，0 表示由系统分配（单测用）。
            on_event: 收到事件后的回调（同步函数，内部只做状态更新）。
            logger: 日志器。
        """

        self.host = host
        self.port = int(port)
        self.on_event = on_event
        self._logger = logger
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._received = 0

    def _log(self, level: str, message: str) -> None:
        """按有无 logger 决定是否输出。"""

        if self._logger is None:
            return
        getattr(self._logger, level, self._logger.info)(message)

    async def _handle_event(self, request: web.Request) -> web.Response:
        """接收一条游戏事件。"""

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - 对方发了坏 JSON 不该让服务挂
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "bad payload"}, status=400)

        self._received += 1
        if self.on_event is not None:
            try:
                self.on_event(payload)
            except Exception as exc:  # noqa: BLE001 - 回调失败不影响接收
                self._log("warning", f"[游戏事件] 回调异常：{type(exc).__name__}: {exc}")
        return web.json_response({"ok": True})

    async def _handle_schedule_event(self, request: web.Request) -> web.Response:
        """接收日程换挡事件：打上来源标记后走同一条事件回调链。

        单独开一个路径，是为了让 schedule-hub 不必伪装成游戏事件，
        也方便在接入方一眼看出「这不是游戏推进」。
        """

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "bad payload"}, status=400)
        payload = dict(payload)
        payload["kind"] = "schedule"
        self._received += 1
        if self.on_event is not None:
            try:
                self.on_event(payload)
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"[事件] 日程回调异常：{type(exc).__name__}: {exc}")
        return web.json_response({"ok": True})

    async def _handle_health(self, request: web.Request) -> web.Response:
        """给 game-hub 探活用，顺便暴露收到多少条。"""

        del request
        return web.json_response({"ok": True, "received": self._received})

    def build_app(self) -> web.Application:
        """构造 aiohttp 应用。"""

        app = web.Application()
        app.router.add_post("/game-event", self._handle_event)
        app.router.add_post("/schedule-event", self._handle_schedule_event)
        app.router.add_get("/health", self._handle_health)
        return app

    async def start(self) -> int:
        """开始监听，返回实际端口（port=0 时由系统分配）。"""

        if self._runner is not None:
            return self.port
        runner = web.AppRunner(self.build_app())
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        self._runner = runner
        self._site = site
        # 端口 0 时取系统实际分配的端口
        for sock in getattr(site._server, "sockets", []) or []:  # noqa: SLF001
            if sock is not None:
                self.port = int(sock.getsockname()[1])
                break
        self._log("info", f"[游戏事件] 已监听 http://{self.host}:{self.port}/game-event")
        return self.port

    async def stop(self) -> None:
        """停止服务。"""

        runner, self._runner = self._runner, None
        self._site = None
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception as exc:  # noqa: BLE001 - 清理失败不影响卸载
                self._log("debug", f"[游戏事件] 停止异常：{type(exc).__name__}: {exc}")
