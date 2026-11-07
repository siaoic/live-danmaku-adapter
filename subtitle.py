# -*- coding: utf-8 -*-
"""字幕层：把「她正在说的这句话」推给 OBS 的浏览器源。

为什么自带一个小 HTTP 服务而不是往别人的端口发：

  * 字幕要跟着语音走（每说一句换一次），最简单的接法是 OBS 浏览器源直接
    打开一个地址，页面自己保持连接、自己淡出；
  * 不需要额外进程、不需要 OBS 插件、不需要登录，开箱即可；
  * 也留了 `POST /api/subtitle` 给外部程序手动推一句（调试或其它脚本用）。

页面刻意做成「透明背景 + 底部居中大字」，直接叠在直播画面上。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Optional, Set

from aiohttp import web

#: 默认端口：用户指定的字幕地址
DEFAULT_PORT = 5679

#: 一屏最多显示多少字，超出截断（否则长句会把画面撑爆）
DEFAULT_MAX_CHARS = 80


class SubtitleServer:
    """一个极小的 HTTP 服务：托页面 + 推当前字幕。

    状态只有「当前这一句 + 更新时间」，页面靠 SSE 拿增量，
    连不上 SSE 时退回轮询 `/api/state`（两秒一次），保证 OBS 里不会空白。
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        *,
        font_size: int = 42,
        text_color: str = "#ffffff",
        background: str = "transparent",
        hold_seconds: float = 0.0,
        max_chars: int = DEFAULT_MAX_CHARS,
        logger: Any = None,
    ) -> None:
        """初始化（不监听端口，等 start()）。

        Args:
            host: 监听地址，默认只听回环。
            port: 端口，0 表示由系统分配（单测用）。
            font_size: 字号（px），按 OBS 画布自行调。
            text_color: 文字颜色。
            background: 文字底色，透明字幕可填 ``transparent``。
            hold_seconds: 一句话说完后停留多久再淡出。
            max_chars: 单行最多显示多少字。
            logger: 插件 logger。
        """

        self.host = str(host or "127.0.0.1")
        self.port = int(port)
        self.font_size = max(12, int(font_size))
        self.text_color = str(text_color or "#ffffff")
        self.background = str(background or "transparent")
        self.hold_seconds = max(0.0, float(hold_seconds))
        self.max_chars = max(10, int(max_chars))
        self._logger = logger

        self._text = ""
        self._updated_at = 0.0
        self._seq = 0
        self._subscribers: Set[asyncio.Queue] = set()
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._bound_port = self.port

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    @property
    def url(self) -> str:
        """给 OBS 浏览器源用的地址。"""

        return f"http://{self.host}:{self._bound_port}/"

    @property
    def running(self) -> bool:
        """是否已经在监听。"""

        return self._site is not None

    def update(
        self,
        *,
        font_size: Optional[int] = None,
        text_color: Optional[str] = None,
        background: Optional[str] = None,
        hold_seconds: Optional[float] = None,
        max_chars: Optional[int] = None,
    ) -> None:
        """原地更新页面渲染参数（不动监听端口，避免热重载时的端口竞态）。"""

        if font_size is not None:
            self.font_size = max(12, int(font_size))
        if text_color is not None:
            self.text_color = str(text_color or "#ffffff")
        if background is not None:
            self.background = str(background or "transparent")
        if hold_seconds is not None:
            self.hold_seconds = max(0.0, float(hold_seconds))
        if max_chars is not None:
            self.max_chars = max(10, int(max_chars))

    async def start(self) -> bool:
        """开始监听；端口被占用等错误只记日志，不影响插件其它功能。"""

        if self._site is not None:
            return True
        app = web.Application()
        app.router.add_get("/", self._handle_page)
        app.router.add_get("/api/state", self._handle_state)
        app.router.add_get("/events", self._handle_events)
        app.router.add_post("/api/subtitle", self._handle_push)
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, self.host, self.port, reuse_address=True)
            await site.start()
        except Exception as exc:  # noqa: BLE001 - 端口被占不该拖垮插件
            try:
                await runner.cleanup()
            except Exception:
                pass
            self._log("warning", f"字幕服务启动失败（{self.host}:{self.port}）：{exc}")
            return False

        self._runner = runner
        self._site = site
        self._bound_port = self._resolve_port(site) or self.port
        self._log("info", f"字幕服务已启动：{self.url}（OBS 浏览器源填这个地址）")
        return True

    async def stop(self) -> None:
        """停止监听并断开所有 SSE 订阅者。"""

        for queue in list(self._subscribers):
            try:
                queue.put_nowait(None)
            except Exception:
                pass
        self._subscribers.clear()

        site, self._site = self._site, None
        runner, self._runner = self._runner, None
        if site is not None:
            try:
                await site.stop()
            except Exception as exc:
                self._log("debug", f"字幕服务停止异常：{exc}")
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception as exc:
                self._log("debug", f"字幕服务清理异常：{exc}")

    def _resolve_port(self, site: web.TCPSite) -> Optional[int]:
        """取实际监听端口（配置成 0 时由系统分配）。"""

        server = getattr(site, "_server", None)
        sockets = getattr(server, "sockets", None) or []
        if not sockets:
            return None
        try:
            return int(sockets[0].getsockname()[1])
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 字幕内容
    # ------------------------------------------------------------------ #

    def publish(self, text: str) -> None:
        """推一句新字幕（覆盖上一句）。"""

        body = " ".join(str(text or "").split())
        if len(body) > self.max_chars:
            body = body[: self.max_chars - 1] + "…"
        if not body:
            return
        self._text = body
        self._updated_at = time.time()
        self._seq += 1
        self._broadcast()

    def clear(self) -> None:
        """清空字幕（下一页拉取时就是空的了）。"""

        if not self._text:
            return
        self._text = ""
        self._updated_at = time.time()
        self._seq += 1
        self._broadcast()

    def state(self) -> Dict[str, Any]:
        """当前字幕状态（页面轮询与 SSE 共用）。

        额外带 ``subscribers``：当前有几个页面连着 SSE（OBS 的浏览器源算一个）。
        排查「页面不显示」时先看这个数——0 就说明 OBS 根本没连上来。
        """

        return {
            "text": self._text,
            "seq": self._seq,
            "updated_at": self._updated_at,
            "hold_seconds": self.hold_seconds,
            "subscribers": len(self._subscribers),
        }

    def _broadcast(self) -> None:
        """把最新状态丢给所有 SSE 订阅者。"""

        if not self._subscribers:
            return
        payload = json.dumps(self.state(), ensure_ascii=False)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # 订阅者太慢就直接丢这条，下次更新会覆盖
                pass

    # ------------------------------------------------------------------ #
    # HTTP 处理器
    # ------------------------------------------------------------------ #

    async def _handle_page(self, request: web.Request) -> web.Response:
        """字幕页面：透明底 + 底部居中，OBS 直接当浏览器源用。"""

        del request
        html = _PAGE_TEMPLATE.format(
            font_size=self.font_size,
            text_color=self.text_color,
            background=self.background,
            hold_ms=int(self.hold_seconds * 1000),
        )
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def _handle_state(self, request: web.Request) -> web.Response:
        """轮询接口：SSE 连不上时的兜底。"""

        del request
        return web.json_response(self.state(), dumps=lambda d: json.dumps(d, ensure_ascii=False))

    async def _handle_push(self, request: web.Request) -> web.Response:
        """外部推一句字幕：``POST /api/subtitle {"text": "..."}``。"""

        try:
            data = await request.json()
        except Exception:
            data = {}
        text = str((data or {}).get("text") or "").strip()
        if not text:
            return web.json_response({"success": False, "error": "缺少 text"}, status=400)
        self.publish(text)
        return web.json_response({"success": True, "state": self.state()})

    async def _handle_events(self, request: web.Request) -> web.StreamResponse:
        """SSE：页面保持长连接，字幕一更新就推过去。"""

        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await response.prepare(request)
        queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subscribers.add(queue)
        try:
            # 连上先补一条当前状态，避免页面一直空着等下一句
            await response.write(f"data: {json.dumps(self.state(), ensure_ascii=False)}\n\n".encode())
            while True:
                payload = await queue.get()
                if payload is None:
                    break
                await response.write(f"data: {payload}\n\n".encode())
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            self._subscribers.discard(queue)
        return response

    def _log(self, level: str, msg: str) -> None:
        """按级别写日志（没传 logger 就静默，方便单测）。"""

        if self._logger is None:
            return
        getattr(self._logger, level, self._logger.info)(msg)


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>直播字幕</title>
<style>
  html, body {{
    margin: 0; padding: 0; height: 100%; background: transparent;
    overflow: hidden;
    font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans SC", sans-serif;
  }}
  #wrap {{
    position: fixed; left: 0; right: 0; bottom: 6vh;
    display: flex; justify-content: center; padding: 0 4vw;
    pointer-events: none;
  }}
  #line {{
    max-width: 88vw;
    font-size: {font_size}px;
    line-height: 1.35;
    color: {text_color};
    background: {background};
    padding: 2px 10px;
    border-radius: 0;
    text-align: center;
    word-break: break-word;
    /* 没有气泡底时靠描边+阴影压住背景，亮画面上也能看清 */
    -webkit-text-stroke: 1px rgba(0,0,0,.55);
    text-shadow: 0 1px 2px rgba(0,0,0,.85), 0 0 6px rgba(0,0,0,.55), 0 2px 10px rgba(0,0,0,.45);
    opacity: 0;
    transform: translateY(8px);
    transition: opacity .18s ease, transform .18s ease;
  }}
  #line.show {{ opacity: 1; transform: translateY(0); }}
</style>
</head>
<body>
<div id="wrap"><div id="line"></div></div>
<script>
(function () {{
  const holdMs = {hold_ms};
  const el = document.getElementById('line');
  let hideTimer = null;
  let lastSeq = -1;

  function render(state) {{
    if (!state || typeof state.seq !== 'number') return;
    if (state.seq === lastSeq) return;
    lastSeq = state.seq;
    const text = (state.text || '').trim();
    if (!text) {{
      el.classList.remove('show');
      return;
    }}
    el.textContent = text;
    el.classList.add('show');
    if (hideTimer) clearTimeout(hideTimer);
    // holdMs = 0 时不自己计时：字幕什么时候撤由插件在语音播完那一刻决定，
    // 页面只管显示/隐藏，这样字幕和声音才是严格同步的。
    if (holdMs > 0) {{
      hideTimer = setTimeout(function () {{ el.classList.remove('show'); }}, holdMs);
    }}
  }}

  // 首选 SSE：更新即时，不用轮询
  try {{
    const es = new EventSource('/events');
    es.onmessage = function (ev) {{
      try {{ render(JSON.parse(ev.data)); }} catch (e) {{}}
    }};
    es.onerror = function () {{
      // OBS 偶尔会断流；EventSource 自己会重连，这里不用管
    }};
  }} catch (e) {{}}

  // 兜底轮询：SSE 不通时也能出字幕
  setInterval(function () {{
    fetch('/api/state', {{ cache: 'no-store' }})
      .then(function (r) {{ return r.json(); }})
      .then(render)
      .catch(function () {{}});
  }}, 2000);
}})();
</script>
</body>
</html>
"""
