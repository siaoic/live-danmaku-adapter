# -*- coding: utf-8 -*-
"""有机演出引擎管理（插件内置，移植自参考插件的 performance_mixin.py）。

引擎本体是 engine/performance_engine.py，作为独立子进程常驻：
  * 用 30/60fps 合成 Live2D 语义参数，再映射成 VTS 追踪输入注入；
  * 动作源来自 VTS 模型目录下的 motion3 曲线随机采样 + 程序化呼吸/摇摆/眨眼
    + cortico 手势叠加 + 视线跟随；
  * 引擎自带一个本地 HTTP 控制接口，插件通过它下发手势标签与视线。

之所以用子进程而不是同进程协程：引擎按固定帧率跑重计算（numpy 逐帧插值），
放进 Runner 的事件循环会把整个插件卡死。

与参考实现的差异（本插件不依赖 AstrBot）：
  * token 由插件从自己的配置里读，通过 --token 传给引擎；
  * VTS 的 host/port 由插件配置决定（本机 127.0.0.1:8001 可能被别的程序占用）；
  * 引擎日志并入插件日志流（stdout 由插件接管）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import asyncio
import logging
import os
import subprocess
import sys
import time

logger = logging.getLogger("plugin.live_stream_companion.engine")

# 引擎默认控制端口（与 VTS 的 8001 错开）
DEFAULT_ENGINE_PORT = 18766

# 引擎相对本文件的位置
# manager.py 本身就在 engine/ 目录里，脚本与它同级
_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
_ENGINE_SCRIPT = os.path.join(_ENGINE_DIR, "performance_engine.py")
_ENGINE_CLIPS = os.path.join(_ENGINE_DIR, "cortico_clips.json")


class PerformanceEngineManager:
    """把有机演出引擎作为子进程托管。"""

    def __init__(
        self,
        *,
        port: int = DEFAULT_ENGINE_PORT,
        vts_host: str = "127.0.0.1",
        vts_port: int = 8001,
        token: str = "",
        python: str = "",
        plugin_name: str = "MaiBot Live Stream Companion",
        developer: str = "MaiBot",
        startup_probe: bool = False,
        auto_props: bool = True,
    ) -> None:
        """初始化管理器（不启动进程）。

        Args:
            port: 引擎本地控制接口端口。
            vts_host: VTS WebSocket 地址。
            vts_port: VTS WebSocket 端口。
            token: VTS 认证 Token（引擎用它连 VTS）。
            python: 运行引擎的解释器；留空用当前解释器。
            plugin_name / developer: 引擎向 VTS 声明的插件身份。
            startup_probe: 启动时是否逐通道探测模型影响力（会在画面上动几秒）。
        """

        self.port = port if 1024 <= int(port) <= 65535 else DEFAULT_ENGINE_PORT
        self.vts_host = (vts_host or "").strip() or "127.0.0.1"
        self.vts_port = int(vts_port) if vts_port else 8001
        self.token = (token or "").strip()
        self.python = (python or "").strip() or sys.executable
        self.plugin_name = plugin_name
        self.developer = developer
        self.startup_probe = bool(startup_probe)
        self.auto_props = bool(auto_props)

        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._started_at = 0.0
        self._log_task: Optional[asyncio.Task[None]] = None
        self._last_error = ""
        # 「引擎问不到」之后的静默期：引擎真没起时，别让每条弹幕都去建连接
        self._probe_retry_at = 0.0

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #

    @property
    def running(self) -> bool:
        """引擎子进程是否还活着。"""

        return self._proc is not None and self._proc.poll() is None

    @property
    def endpoint(self) -> str:
        """引擎控制接口地址。"""

        return f"http://127.0.0.1:{self.port}"

    def status(self) -> Dict[str, Any]:
        """返回引擎状态快照。"""

        return {
            "running": self.running,
            "pid": getattr(self._proc, "pid", None),
            "port": self.port,
            "endpoint": self.endpoint,
            "vts_host": self.vts_host,
            "vts_port": self.vts_port,
            "uptime_seconds": round(time.time() - self._started_at, 1) if self._started_at else 0.0,
            "last_error": self._last_error,
        }

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def _port_in_use(self) -> bool:
        """检查控制端口是否已被占用（说明已经有一个引擎在跑）。"""

        import socket

        def _probe() -> bool:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.6)
                return sock.connect_ex(("127.0.0.1", self.port)) == 0

        try:
            return await asyncio.to_thread(_probe)
        except Exception:
            return False

    async def _kill_stale_listeners(self) -> None:
        """杀掉所有残留的引擎子进程（含启动失败、没监听端口的）。

        多个引擎同时往 VTS 注入参数会互相覆盖，表现就是模型抽搐；
        所以启动前必须确保全局只剩本实例一个引擎。按命令行匹配
        performance_engine，比只查端口更可靠——启动失败没抢到端口的进程
        也会继续连 VTS 注入参数，旧逻辑（只杀监听端口的）清不掉它们。
        """

        if os.name != "nt":
            return
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*performance_engine*' } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["powershell", "-NoProfile", "-NoLogo", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=10.0,
            )
        except Exception as exc:
            logger.debug(f"[演出引擎] 查询引擎进程失败：{exc}")
            return
        own_pid = os.getpid()
        for line in (result.stdout or "").splitlines():
            pid_text = line.strip()
            if not pid_text.isdigit():
                continue
            pid = int(pid_text)
            if pid == own_pid:
                continue
            logger.warning(f"[演出引擎] 发现残留引擎进程 pid={pid}，先清理")
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True, text=True, encoding="gbk", errors="ignore", timeout=8.0,
                )
            except Exception as exc:
                logger.warning(f"[演出引擎] 清理残留引擎失败 pid={pid}: {exc}")
        await asyncio.sleep(0.8)
    async def start(self) -> bool:
        """启动引擎子进程。

        Returns:
            bool: 是否已启动（已运行也算 True）。
        """

        if self.running:
            return True
        # 单实例保护：多个引擎同时注入 VTS 会互相覆盖，直接表现为模型抽搐
        await self._kill_stale_listeners()
        if not os.path.isfile(_ENGINE_SCRIPT):
            self._last_error = f"找不到引擎脚本：{_ENGINE_SCRIPT}"
            logger.warning(f"[演出引擎] {self._last_error}")
            return False
        if not self.token:
            self._last_error = "缺少 VTS 认证 Token，引擎无法连接 VTS"
            logger.warning(f"[演出引擎] {self._last_error}")
            return False

        command = [
            self.python,
            "-u",
            _ENGINE_SCRIPT,
            "--port", str(self.port),
            "--clips", _ENGINE_CLIPS,
            "--vts-host", self.vts_host,
            "--vts-port", str(self.vts_port),
            "--token", self.token,
            "--probe", "1" if self.startup_probe else "0",
            "--auto-props", "1" if self.auto_props else "0",
            "--plugin-name", self.plugin_name,
            "--developer", self.developer,
        ]
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(
                command,
                cwd=_ENGINE_DIR,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=creationflags,
            )
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"[演出引擎] 启动失败：{self._last_error}")
            return False

        self._started_at = time.time()
        self._last_error = ""
        self._log_task = asyncio.create_task(self._drain_logs(), name="perf-engine-log")
        logger.info(
            "[演出引擎] 已启动 pid=%s port=%s → VTS %s:%s",
            self._proc.pid, self.port, self.vts_host, self.vts_port,
        )
        return True

    async def _drain_logs(self) -> None:
        """把引擎子进程的输出转发到插件日志，避免管道写满阻塞引擎。"""

        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        loop = asyncio.get_running_loop()
        try:
            while True:
                line = await loop.run_in_executor(None, proc.stdout.readline)
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.info(f"[演出引擎] {text}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[演出引擎] 日志转发结束：{exc}")

    async def stop(self) -> None:
        """停止引擎子进程。"""

        task, self._log_task = self._log_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        proc, self._proc = self._proc, None
        self._started_at = 0.0
        if proc is None:
            return
        try:
            proc.terminate()
            await asyncio.to_thread(proc.wait, 5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        logger.info("[演出引擎] 已停止")

    async def restart(self) -> bool:
        """重启引擎（配置变化时用）。"""

        await self.stop()
        await asyncio.sleep(0.8)
        return await self.start()

    # ------------------------------------------------------------------ #
    # 控制接口
    # ------------------------------------------------------------------ #

    async def _request(self, path: str, timeout: float = 1.5) -> Dict[str, Any]:
        """调用引擎的本地 HTTP 控制接口。"""

        import aiohttp

        url = f"{self.endpoint}{path}"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session:
                async with session.get(url) as response:
                    data = await response.json(content_type=None)
                    return data if isinstance(data, dict) else {"ok": False}
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            return {"ok": False, "error": self._last_error}

    async def probe(self) -> bool:
        """探测引擎控制接口是否可用（只看 HTTP，不看 self.running）。"""

        data = await self._request("/status", timeout=1.2)
        return bool(data.get("ok"))

    async def _available(self) -> bool:
        """引擎此刻能不能真正干活。

        ``self.running`` 只表示「这个管理器实例自己拉起过引擎」。插件热重载后
        新实例是 False，但上一实例留下的引擎进程还好好活着——拿它当门槛，
        表情、道具、外观件就会一次性全部静默失效（日志只说「演出引擎未运行」，
        其实引擎在跑，VTS 也在线）。

        所以以 HTTP 为准；问不到时进入 3 秒静默期，避免引擎真没启动时
        每条弹幕都去建一次连接。
        """

        if self.running:
            return True
        now = time.monotonic()
        if now < self._probe_retry_at:
            return False
        if await self.probe():
            return True
        self._probe_retry_at = now + 3.0
        return False

    async def actions(self) -> Dict[str, Any]:
        """取「当前皮套能演什么」的动作清单。

        提示词与台词记号解析共用这一份：外观件（兔牙/钢盆/前倾）、已标定表情、
        成对道具、固定小动作。拿不到就返回空清单（此时不该让模型打记号）。

        Returns:
            Dict[str, Any]: {"model": 模型名, "actions": [{"key","kind","aliases"}]}
        """

        empty: Dict[str, Any] = {"actions": []}
        # 不看 self.running：管理器可能不是「本次启动引擎」的那个实例
        # （插件热重载、引擎被别的路径拉起时 running 为 False，但接口是活的）。
        # 直接问接口，失败就当没有词表——那只是不能打记号，不影响任何播出。
        data = await self._request("/actions", timeout=2.0)
        if not data.get("ok"):
            return empty
        raw = data.get("msg")
        if isinstance(raw, dict):
            return raw if isinstance(raw.get("actions"), list) else empty
        import json

        try:
            payload = json.loads(str(raw or "{}"))
        except ValueError:
            return empty
        if isinstance(payload, dict) and isinstance(payload.get("actions"), list):
            return payload
        return empty

    async def play_tag(self, tag: str, intensity: float = 1.0) -> bool:
        """让引擎做一个手势（nod / shake / tilt / wink / bounce…）。

        Args:
            tag: 手势或情绪词，见引擎里的 TAG_TO_CLIP。
            intensity: 强度倍率。

        Returns:
            bool: 引擎是否接受。
        """

        if not await self._available():
            return False
        data = await self._request(f"/tag?name={tag}&intensity={intensity}")
        return bool(data.get("ok"))

    async def set_gaze(self, x: float, y: float) -> bool:
        """设置视线方向（-1~1）。"""

        if not await self._available():
            return False
        data = await self._request(f"/gaze?x={x}&y={y}")
        return bool(data.get("ok"))

    @staticmethod
    def _expression_path(emotion: str, hold_seconds: float, text: str) -> str:
        """拼 /expression 的查询串。

        ``hold`` 只在明确指定时才带：省略就等于让引擎按台词计划时序。
        这正是想要的行为——所有情绪都挂同一个固定秒数才不自然。
        """

        from urllib.parse import quote

        path = f"/expression?name={quote(str(emotion))}"
        if hold_seconds and float(hold_seconds) > 0:
            path += f"&hold={float(hold_seconds)}"
        if text:
            path += f"&text={quote(str(text))}"
        return path

    async def set_expression(
        self, emotion: str, hold_seconds: float = 0.0, text: str = ""
    ) -> Dict[str, Any]:
        """按情绪词切换模型表情（无语→黑脸、开心→爱心眼…）。

        Args:
            emotion: 情绪词，中文（如「无语」「委屈」）；也可以是表情本名。
            hold_seconds: 保持多少秒；0 表示不指定，由引擎按台词计划时序。
            text: 触发这句表情的台词。引擎据此估强度与语速，算出
                onset/apex/offset 三段——同一个「委屈」在不同语气里长短不同。

        Returns:
            Dict[str, Any]: {"ok": bool, "name": 实际生效的表情名 或 "error": 原因}
        """

        if not await self._available():
            return {"ok": False, "error": "演出引擎未运行"}
        from urllib.parse import quote

        data = await self._request(
            self._expression_path(emotion, hold_seconds, text), timeout=3.0
        )
        if data.get("ok"):
            return {"ok": True, "name": str(data.get("msg") or "")}
        return {"ok": False, "error": str(data.get("msg") or data.get("error") or "匹配失败")}

    async def set_emotion(self, emotion: str, hold_seconds: float = 0.0, text: str = "") -> bool:
        """只走参数层演一个情绪（不切 .exp3 表情文件，脸全靠眉毛/嘴角/眼睑合成）。

        hold_seconds 传 0 表示由引擎按 text 的强度与语速计划时序。
        """

        if not await self._available():
            return False
        from urllib.parse import quote

        path = f"/emotion?name={quote(str(emotion))}"
        if hold_seconds and float(hold_seconds) > 0:
            path += f"&hold={float(hold_seconds)}"
        if text:
            path += f"&text={quote(str(text))}"
        data = await self._request(path, timeout=3.0)
        return bool(data.get("ok"))

    async def clear_expression(self) -> bool:
        """收起当前表情。"""

        if not await self._available():
            return False
        data = await self._request("/expression/clear")
        return bool(data.get("ok"))

    async def reset_stage(self) -> bool:
        """场景重置：收起表情/道具/姿势，视线回镜头（cortico 的【Reset】）。"""

        if not await self._available():
            return False
        data = await self._request("/reset")
        return bool(data.get("ok"))

    async def set_prop(self, prop: str, hold_seconds: float = 60.0) -> Dict[str, Any]:
        """打开/收起动画道具（狗尾巴草、竖手指这类开/关配对热键）。

        Args:
            prop: 道具名。
            hold_seconds: 保持秒数；<=0 表示收回。

        Returns:
            Dict[str, Any]: {"ok": bool, "name": 道具名 或 "error": 原因}
        """

        if not await self._available():
            return {"ok": False, "error": "演出引擎未运行"}
        from urllib.parse import quote

        data = await self._request(
            f"/prop?name={quote(str(prop))}&hold={float(hold_seconds)}", timeout=3.0
        )
        if data.get("ok"):
            return {"ok": True, "name": str(data.get("msg") or "")}
        return {"ok": False, "error": str(data.get("msg") or data.get("error") or "匹配失败")}

    async def set_pose(self, name: str, hold_seconds: float = 45.0) -> Dict[str, Any]:
        """戴上/收起模型自带的外观件（兔牙、钢盆、前倾…）。

        这类表情改的是外观而不是情绪，不进情绪轮换，只能由台词记号点名。
        同一个名字再说一次就收起（toggle）。

        Args:
            name: 外观件名（当前模型真实存在的）。
            hold_seconds: 保持秒数；<=0 表示一直戴着。

        Returns:
            Dict[str, Any]: {"ok": bool, "name": 结果说明 或 "error": 原因}
        """

        if not await self._available():
            return {"ok": False, "error": "演出引擎未运行"}
        from urllib.parse import quote

        data = await self._request(
            f"/pose?name={quote(str(name))}&hold={float(hold_seconds)}&toggle=1", timeout=3.0
        )
        if data.get("ok"):
            return {"ok": True, "name": str(data.get("msg") or "")}
        return {"ok": False, "error": str(data.get("msg") or data.get("error") or "没有这个外观件")}

    async def list_expressions(self) -> Dict[str, Any]:
        """列出当前模型可用的情绪表情与动画道具。"""

        if not await self._available():
            return {"ok": False, "error": "演出引擎未运行"}
        data = await self._request("/expressions")
        if not data.get("ok"):
            return {"ok": False, "error": str(data.get("error") or "读取失败")}
        import json as _json

        try:
            payload = _json.loads(str(data.get("msg") or "{}"))
        except ValueError:
            payload = {}
        return {"ok": True, **(payload if isinstance(payload, dict) else {})}

    async def engine_status(self) -> Dict[str, Any]:
        """读取引擎内部状态（当前片段、脉冲、情绪保持等）。"""

        data = await self._request("/status")
        if not data.get("ok"):
            return {"reachable": False, "error": data.get("error", "")}
        return {"reachable": True, "detail": data.get("msg", "")}
