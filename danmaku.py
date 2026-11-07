# -*- coding: utf-8 -*-
"""弹幕层：把直播间收到的弹幕推到一个本地网页，给 OBS 当浏览器源用。

和字幕层（`subtitle.py`）是同一套路子，只是内容方向相反：
字幕是「她说的话」，弹幕是「观众说的话」。
页面做成右侧竖排、新消息落在底部、每条停留几秒后淡出，不抢画面。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any, Dict, List, Optional, Set

from aiohttp import web

#: 默认端口：弹幕墙地址
DEFAULT_PORT = 5686

#: 适配器给入站文本加的标签，如 "[B站弹幕] " / "[抖音礼物] " / "[B站上舰·舰长] "
#: SC 带价格 "[B站醒目留言 ¥30] "，所以醒目留言后面要允许价格段。
_PREFIX = re.compile(
    r"^\s*\[\s*(?:B站|抖音|语音)?\s*(?:弹幕|醒目留言(?:\s*[¥￥]\s*[\d.]+)?|礼物|上舰(?:·[^\]]*)?)\s*\]\s*"
)
#: 事件描述末尾挂的金额，如「粉丝团灯牌x1，¥0.1」；弹幕墙不展示价格。
#: 注意：聊天流（发给 LLM 的文本）里的价格要保留，AI 靠它判断礼物轻重，
#: 所以只在这层展示解析时剥掉，不动 core/mapping.py 的 format_event_text。
_PRICE_TAIL = re.compile(r"\s*[,，]?\s*[¥￥]\s*[\d,]+(?:\.\d+)?\s*$")
#: badge 里可能残留的金额，如 "SC ¥30"（旧状态/旧服务实例兜底）
_BADGE_PRICE = re.compile(r"\s*[¥￥]\s*[\d,]+(?:\.\d+)?")
#: 事件类型 → 页面上的小标签
_KIND_LABELS = {
    "danmaku": "",
    "gift": "礼物",
    "super_chat": "SC",
    "guard": "上舰",
}
#: 这些事件才可能带金额，普通弹幕里观众自己打的「¥30」要原样保留
_PRICED_KINDS = frozenset({"gift", "guard", "super_chat"})


def _strip_price(text: str) -> str:
    """剥掉事件描述末尾的金额，弹幕墙不显示礼物/SC 价格。"""

    return _PRICE_TAIL.sub("", str(text or "")).rstrip()


def _strip_badge_price(badge: str) -> str:
    """剥掉小标签里的金额，如 "SC ¥30" → "SC"。"""

    return _BADGE_PRICE.sub("", str(badge or "")).strip()


def _split_user_content(body: str, nickname: str) -> tuple[str, str]:
    """把「昵称: 内容」拆成 (昵称, 内容)；没有冒号时返回 (nickname, body)。"""

    if ": " in body:
        head, _, tail = body.partition(": ")
        if 0 < len(head) <= 24 and not head.startswith("http"):
            if not nickname:
                nickname = head.strip()
            body = tail.strip()
    return nickname, body


def parse_inbound(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一条入站消息整理成页面要的一行；解析不出内容时返回 None。

    四类事件各有一套「前缀 + 结构」，按 kind 分别处理：

    - 弹幕：`[B站弹幕] 昵称: 内容` → 昵称 + 内容分列；
    - 醒目留言：`[B站醒目留言 ¥30] 昵称: 内容` → 昵称 + 内容，价格不显示；
    - 礼物：`[B站礼物] 昵称 送出了 礼物x1，¥0.1` → 去掉价格，整句直接显示；
    - 上舰：`[B站上舰·舰长] 昵称 开通了舰长` → 同上，整句直接显示。

    金额只给 LLM 看，弹幕墙一律不展示（含 badge 里的 SC 金额）。
    """

    if not isinstance(message, dict):
        return None

    info = message.get("message_info")
    nickname = ""
    kind = ""
    if isinstance(info, dict):
        user = info.get("user_info")
        if isinstance(user, dict):
            nickname = str(user.get("user_nickname") or "").strip()
        extra = info.get("additional_config")
        if isinstance(extra, dict):
            kind = str(extra.get("live_event_kind") or "").strip()

    text = str(message.get("processed_plain_text") or "").strip()
    if not text:
        segments = message.get("raw_message")
        if isinstance(segments, list):
            parts = [
                str(item.get("data") or "").strip()
                for item in segments
                if isinstance(item, dict) and str(item.get("type") or "").lower() == "text"
            ]
            text = " ".join(part for part in parts if part).strip()
    if not text:
        return None

    body = _PREFIX.sub("", text).strip()
    # 礼物/上舰/SC 的描述里可能挂着金额，弹幕墙不展示，只留事件本身。
    # 普通弹幕不动：观众自己打的价格是弹幕内容，不是礼物价格。
    if kind in _PRICED_KINDS:
        body = _strip_price(body)
    if not body:
        return None

    # 礼物与上舰：文本本身就是「昵称 送出了 / 开通了」的完整动作，整句显示即可，
    # 不再额外加「昵称：」前缀（否则会重复成「观众甲：观众甲 送出了…」）。
    if kind in ("gift", "guard"):
        return {
            "user": "",
            "text": body,
            "badge": _KIND_LABELS.get(kind, ""),
            "kind": kind,
            "at": time.time(),
        }

    nickname, body = _split_user_content(body, nickname)
    if not body:
        return None

    return {
        "user": nickname or "匿名观众",
        "text": body,
        "badge": _KIND_LABELS.get(kind, ""),
        "kind": kind or "danmaku",
        "at": time.time(),
    }


class DanmakuServer:
    """极小的 HTTP 服务：托弹幕页面 + 推送新弹幕（SSE）。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        *,
        max_lines: int = 12,
        hold_seconds: float = 12.0,
        font_size: int = 22,
        text_color: str = "#ffffff",
        name_color: str = "#8fd3ff",
        background: str = "transparent",
        logger: Any = None,
    ) -> None:
        """初始化（不监听端口，等 start()）。

        Args:
            host: 监听地址，默认只听回环。
            port: 端口，0 表示由系统分配（单测用）。
            max_lines: 页面最多同时显示几条。
            hold_seconds: 每条弹幕停留多久后淡出。
            font_size: 字号（px）。
            text_color: 正文颜色。
            name_color: 昵称颜色。
            background: 整块面板底色，默认透明。
            logger: 插件 logger。
        """

        self.host = str(host or "127.0.0.1")
        self.port = int(port)
        self.max_lines = max(3, int(max_lines))
        self.hold_seconds = max(2.0, float(hold_seconds))
        self.font_size = max(12, int(font_size))
        self.text_color = str(text_color or "#ffffff")
        self.name_color = str(name_color or "#8fd3ff")
        self.background = str(background or "transparent")
        self._logger = logger

        self._items: List[Dict[str, Any]] = []
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
        max_lines: Optional[int] = None,
        hold_seconds: Optional[float] = None,
        font_size: Optional[int] = None,
        text_color: Optional[str] = None,
        name_color: Optional[str] = None,
        background: Optional[str] = None,
    ) -> None:
        """原地更新页面渲染参数（不动监听端口，避免热重载时的端口竞态）。"""

        if max_lines is not None:
            self.max_lines = max(3, int(max_lines))
        if hold_seconds is not None:
            self.hold_seconds = max(2.0, float(hold_seconds))
        if font_size is not None:
            self.font_size = max(12, int(font_size))
        if text_color is not None:
            self.text_color = str(text_color or "#ffffff")
        if name_color is not None:
            self.name_color = str(name_color or "#8fd3ff")
        if background is not None:
            self.background = str(background or "transparent")

    async def start(self) -> bool:
        """开始监听；端口被占只记日志，不影响插件其它功能。"""

        if self._site is not None:
            return True
        app = web.Application()
        app.router.add_get("/", self._handle_page)
        app.router.add_get("/api/state", self._handle_state)
        app.router.add_get("/events", self._handle_events)
        app.router.add_post("/api/danmaku", self._handle_push)
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
            self._log("warning", f"弹幕墙启动失败（{self.host}:{self.port}）：{exc}")
            return False

        self._runner = runner
        self._site = site
        self._bound_port = self._resolve_port(site) or self.port
        self._log("info", f"弹幕墙已启动：{self.url}（OBS 浏览器源填这个地址）")
        return True

    async def stop(self) -> None:
        """停止监听并断开所有订阅者。"""

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
                self._log("debug", f"弹幕墙停止异常：{exc}")
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception as exc:
                self._log("debug", f"弹幕墙清理异常：{exc}")

    def _resolve_port(self, site: web.TCPSite) -> Optional[int]:
        """取实际监听端口（配置成 0 时由系统分配）。"""

        sockets = getattr(getattr(site, "_server", None), "sockets", None) or []
        if not sockets:
            return None
        try:
            return int(sockets[0].getsockname()[1])
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 弹幕内容
    # ------------------------------------------------------------------ #

    def push(self, item: Dict[str, Any]) -> None:
        """追加一条弹幕并广播；超出上限的从最旧的丢。"""

        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            return
        row = dict(item)
        # 展示层最后一道兜底：不管条目从哪来（钩子解析 / POST / 旧状态），
        # 礼物类事件的金额与 badge 金额都不上墙。
        if str(row.get("kind") or "").strip() in _PRICED_KINDS:
            row["text"] = _strip_price(str(row.get("text") or ""))
        row["badge"] = _strip_badge_price(str(row.get("badge") or ""))
        if not str(row.get("text") or "").strip():
            return
        self._seq += 1
        row["seq"] = self._seq
        self._items.append(row)
        if len(self._items) > self.max_lines:
            self._items = self._items[-self.max_lines :]
        self._broadcast(row)

    def push_text(self, text: str, user: str = "", badge: str = "") -> None:
        """便捷入口：直接塞一条纯文本。"""

        self.push({"user": user or "匿名观众", "text": str(text or "").strip(), "badge": badge, "kind": "danmaku"})

    def state(self) -> Dict[str, Any]:
        """当前面板状态（页面首次连接与轮询用）。"""

        return {
            "items": list(self._items),
            "seq": self._seq,
            "hold_seconds": self.hold_seconds,
            "max_lines": self.max_lines,
            "subscribers": len(self._subscribers),
            # 页面靠它判断自己的模板是不是旧的：改样式后 OBS 不用手动刷新
            "version": _template_version(),
        }

    def _broadcast(self, row: Dict[str, Any]) -> None:
        """把新弹幕推给所有订阅者。"""

        if not self._subscribers:
            return
        payload = json.dumps(row, ensure_ascii=False)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------ #
    # HTTP 处理器
    # ------------------------------------------------------------------ #

    async def _handle_page(self, request: web.Request) -> web.Response:
        """弹幕页面：右侧竖排，新消息贴底。"""

        del request
        html = _PAGE_TEMPLATE.format(
            font_size=self.font_size,
            text_color=self.text_color,
            name_color=self.name_color,
            background=self.background,
            hold_ms=int(self.hold_seconds * 1000),
            max_lines=self.max_lines,
            version=_template_version(),
        )
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def _handle_state(self, request: web.Request) -> web.Response:
        """轮询接口：SSE 连不上时的兜底。"""

        del request
        return web.json_response(self.state(), dumps=lambda d: json.dumps(d, ensure_ascii=False))

    async def _handle_push(self, request: web.Request) -> web.Response:
        """外部推一条弹幕：``POST /api/danmaku {"user": "...", "text": "..."}``。"""

        try:
            data = await request.json()
        except Exception:
            data = {}
        text = str((data or {}).get("text") or "").strip()
        if not text:
            return web.json_response({"success": False, "error": "缺少 text"}, status=400)
        self.push(
            {
                "user": str((data or {}).get("user") or "匿名观众"),
                "text": text,
                "badge": str((data or {}).get("badge") or ""),
                "kind": "danmaku",
                "at": time.time(),
            }
        )
        return web.json_response({"success": True})

    async def _handle_events(self, request: web.Request) -> web.StreamResponse:
        """SSE：页面保持长连接，来一条推一条。"""

        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await response.prepare(request)
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._subscribers.add(queue)
        try:
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
<title>直播弹幕</title>
<style>
  html, body {{
    margin: 0; padding: 0; height: 100%; background: transparent; overflow: hidden;
    font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans SC", sans-serif;
  }}
  #panel {{
    position: fixed; left: 2vw; bottom: 2vh; width: 34vw;
    display: flex; flex-direction: column; justify-content: flex-end; gap: 6px;
    padding: 10px 12px; border-radius: 10px; background: {background};
    pointer-events: none;
  }}
  .row {{
    display: flex; flex-wrap: wrap; gap: 2px 8px; align-items: baseline;
    font-size: {font_size}px; line-height: 1.4;
    color: {text_color};
    text-shadow: 0 1px 2px rgba(0,0,0,.9), 0 0 6px rgba(0,0,0,.6);
    opacity: 0; transform: translateX(12px);
    transition: opacity .16s ease, transform .16s ease;
    word-break: break-word;
  }}
  .row.show {{ opacity: 1; transform: translateX(0); }}
  .row.old {{ opacity: .55; }}
  .name {{ color: {name_color}; flex: none; }}
  .badge {{
    flex: none; font-size: .72em; padding: 0 5px; border-radius: 6px;
    background: rgba(255,255,255,.16);
  }}
  .text {{ flex: 1 1 auto; }}
  /* 长弹幕（>12 字）：正文整段落到名字下方，由脚本每 12 字插一个硬换行，
     所以这里不能给正文加 max-width —— 否则 flex 会把它压回名字那一行 */
  .row.stack .text {{ flex: 1 1 100%; white-space: pre-line; }}
  /* 礼物/上舰没有昵称，只有「礼物」小标签：让标签和正文第一行同排，
     别单独占一行空出半行高度 */
  .row.stack.nohdr .text {{ flex: 1 1 auto; }}
</style>
</head>
<body>
<div id="panel"></div>
<script>
(function () {{
  const holdMs = {hold_ms};
  const maxLines = {max_lines};
  // 模板指纹：服务端一改样式，页面轮询/推送拿到的 version 就会变，自动重载
  const pageVersion = "{version}";
  const DOM_MAX = 100;  // DOM 里最多保留的行数（超出的旧行早已滚出视野，删除不影响观感）
  const MAX_LINE_CHARS = 12;  // 正文一行最多 12 个字，超过就在第 12 字处硬换行
  const panel = document.getElementById('panel');
  const rows = [];
  let lastSeq = -1;

  // 用户名配色：按名字哈希取色，同一个观众始终同色，不同观众颜色错开。
  const userPalette = [
    '#8fd3ff', '#ffb3c1', '#ffd08a', '#a8e6a3', '#d7b3ff',
    '#7ae0d6', '#ffa07a', '#c0e3ff', '#f7c8e0', '#b8f0a0',
    '#9ad0ff', '#ffd9a0', '#c9b8ff', '#8ff0b8', '#ffb08a', '#a0d8ef',
  ];
  function hashStr(s) {{
    let h = 0;
    for (let i = 0; i < s.length; i++) {{
      h = (h * 31 + s.charCodeAt(i)) >>> 0;
    }}
    return h;
  }}
  function userColor(name) {{
    return userPalette[hashStr(name) % userPalette.length];
  }}

  // 价格不上墙：即便页面连到旧服务实例、或 state 里残留历史条目，也兜底剥掉。
  function stripPrice(s) {{
    return String(s == null ? '' : s)
      .replace(/\\s*[,，]?\\s*[¥￥]\\s*[\\d,]+(?:\\.\\d+)?\\s*$/, '')
      .trim();
  }}
  function stripBadgePrice(s) {{
    return String(s == null ? '' : s).replace(/\\s*[¥￥]\\s*[\\d,]+(?:\\.\\d+)?/g, '').trim();
  }}

  function addRow(item, animate) {{
    const row = document.createElement('div');
    row.className = 'row' + (animate ? '' : ' show');
    if (!item.user) row.classList.add('nohdr');
    if (item.user) {{
      const name = document.createElement('span');
      name.className = 'name';
      name.textContent = item.user + '：';
      name.style.color = userColor(item.user);
      row.appendChild(name);
    }}
    const badgeText = stripBadgePrice(item.badge);
    if (badgeText) {{
      const badge = document.createElement('span');
      badge.className = 'badge';
      badge.textContent = badgeText;
      row.appendChild(badge);
    }}
    const rawText = String(item.text || '');
    const isPricedEvent = item.kind === 'gift' || item.kind === 'guard'
      || item.kind === 'super_chat' || /送出了|开通了/.test(rawText);
    const displayText = isPricedEvent ? stripPrice(rawText) : rawText;
    // 超过 12 个字的正文：每 12 字插一个硬换行，并把整段挪到名字下方。
    // 硬换行不依赖面板宽度，OBS 用 1920×1080 还是小窗口都按 12 字断行。
    const chars = Array.from(displayText);
    const tooLong = chars.length > MAX_LINE_CHARS;
    const text = document.createElement('span');
    text.className = 'text';
    if (tooLong) {{
      const lines = [];
      for (let i = 0; i < chars.length; i += MAX_LINE_CHARS) {{
        lines.push(chars.slice(i, i + MAX_LINE_CHARS).join(''));
      }}
      text.textContent = lines.join('\\n');
    }} else {{
      text.textContent = displayText;
    }}
    row.appendChild(text);
    panel.appendChild(row);
    // 12 字以内还要看会不会被长名字挤到折行：折了也整段挪到名字下方。
    const linePx = parseFloat(getComputedStyle(row).lineHeight) || 30;
    const squeezed = text.getBoundingClientRect().height > linePx * 1.6;
    if (tooLong || squeezed) {{
      row.classList.add('stack');
    }}
    if (animate) requestAnimationFrame(function () {{ row.classList.add('show'); }});

    rows.push({{ el: row }});
    // 弹幕只往上推、不消失：只限制 DOM 里保留的行数防止无限堆积，
    // 被删的都是早已被 overflow:hidden 推出面板顶部的旧行，观众看不到消失。
    while (rows.length > DOM_MAX) {{
      const old = rows.shift();
      old.el.remove();
    }}
  }}

  function render(state) {{
    if (!state) return;
    if (state.version && state.version !== pageVersion) {{
      if (!window.__dmReloading) {{ window.__dmReloading = true; location.reload(); }}
      return;
    }}
    if (Array.isArray(state.items)) {{
      if (state.seq === lastSeq) return;
      lastSeq = state.seq;
      panel.innerHTML = '';
      rows.length = 0;
      state.items.forEach(function (it) {{ addRow(it, false); }});
    }}
  }}

  try {{
    const es = new EventSource('/events');
    es.onmessage = function (ev) {{
      try {{
        const data = JSON.parse(ev.data);
        if (Array.isArray(data.items)) {{ render(data); return; }}
        if (typeof data.seq === 'number') {{
          if (data.seq === lastSeq) return;
          lastSeq = data.seq;
        }}
        addRow(data, true);
      }} catch (e) {{}}
    }};
  }} catch (e) {{}}

  // 兜底轮询
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


def _template_version() -> str:
    """页面模板指纹：模板一改，已打开的页面轮询到新值就自己重载。"""

    return hashlib.md5(_PAGE_TEMPLATE.encode("utf-8")).hexdigest()[:8]
