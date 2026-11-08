"""VTube Studio 跨平台自动发现模块（移植自参考插件 vts_discovery.py）。

策略顺序：
1. 扫描 VTS 默认端口 8001；
2. 读取 VTS 配置文件中的端口；
3. 扫描备用端口范围；
4. 检测进程是否存在。

依赖可选 psutil，未安装时优雅降级（仅用文件/进程名简单判断）。
日志用 stdlib logging(logging.getLogger("plugin.live_stream_companion.vts"))。
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
from pathlib import Path
from typing import List, Optional, Tuple

import websockets

logger = logging.getLogger("plugin.live_stream_companion.vts")

# 常量定义
VTS_DEFAULT_PORT = 8001
VTS_SCAN_PORTS = list(range(8001, 8011))  # 8001~8010

# 各平台 VTube Studio 进程名
_VTS_PROCESS_NAMES: dict[str, List[str]] = {
    "Windows": ["VTube Studio.exe", "VTubeStudio.exe"],
    "Darwin": ["VTube Studio", "VTubeStudio"],
    "Linux": ["VTube Studio", "VTubeStudio", "vtube-studio"],
}

# 各平台 Steam 默认安装路径（含常见自定义库位置）
_STEAM_PATHS: dict[str, List[str]] = {
    "Windows": [
        r"C:\Program Files (x86)\Steam\steamapps\common\VTube Studio",
        r"C:\Program Files\Steam\steamapps\common\VTube Studio",
        r"D:\Program Files (x86)\Steam\steamapps\common\VTube Studio",
        r"D:\Program Files\Steam\steamapps\common\VTube Studio",
        r"D:\Steam\steamapps\common\VTube Studio",
        r"D:\SteamLibrary\steamapps\common\VTube Studio",
        r"E:\Steam\steamapps\common\VTube Studio",
        r"E:\SteamLibrary\steamapps\common\VTube Studio",
    ],
    "Darwin": [str(Path.home() / "Library/Application Support/Steam/steamapps/common/VTube Studio")],
    "Linux": [
        str(Path.home() / ".steam/steam/steamapps/common/VTube Studio"),
        str(Path.home() / ".local/share/Steam/steamapps/common/VTube Studio"),
    ],
}


def _get_os() -> str:
    """返回系统标识（Windows/Darwin/Linux）。"""

    system = platform.system()
    if system == "Windows":
        return "Windows"
    if system == "Darwin":
        return "Darwin"
    return "Linux"


async def _async_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """异步检查指定主机的端口是否可连接（不判断是不是 VTS）。"""

    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        del reader
        return True
    except Exception:
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def _is_vts_websocket(host: str, port: int, timeout: float = 2.0) -> bool:
    """判断该端口上跑的是不是 VTS 的 WebSocket API。

    VTS 的 WebSocket 端点接受 Upgrade 并回 101；普通 HTTP 服务（例如 MaiBot 自己的
    WebUI/文件服务）会回 200/403 等。这里直接做一次真正的 WebSocket 握手来区分，
    避免把「端口开着」误判成「VTS 在跑」。
    """

    url = f"ws://{host}:{port}"
    try:
        client = await asyncio.wait_for(websockets.connect(url), timeout=timeout)
    except Exception:
        return False
    try:
        # 握手成功后再发一条 VTS 协议请求，确认对端说的是 VTS API
        await client.send(json.dumps({
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": "vts-discovery",
            "messageType": "APIStateRequest",
            "data": {},
        }))
        raw = await asyncio.wait_for(client.recv(), timeout=timeout)
        payload = json.loads(raw)
        return payload.get("messageType") in {"APIStateResponse", "APIError"}
    except Exception:
        return False
    finally:
        try:
            await client.close()
        except Exception:
            pass


async def scan_ports(host: str = "localhost") -> Optional[int]:
    """并发扫描 VTS_SCAN_PORTS 范围内的端口，找到第一个 VTS WebSocket 就返回。"""

    async def probe(port: int) -> Optional[int]:
        if await _async_port_open(host, port, timeout=1.0):
            if await _is_vts_websocket(host, port, timeout=1.0):
                logger.info(f"[发现] VTS 命中端口：{port}")
                return port
        return None

    results = await asyncio.gather(*(probe(port) for port in VTS_SCAN_PORTS), return_exceptions=True)
    for result in results:
        if isinstance(result, int):
            return result
    return None


# VTS 把配置写在安装目录的 StreamingAssets 下（实机确认于 1.35.x）；
# 少数旧版本放在用户数据目录，两个位置都试。
_VTS_CONFIG_RELATIVE_PATHS = (
    Path("VTube Studio_Data") / "StreamingAssets" / "Config" / "vts_config.json",
    Path("VTube Studio User Data") / "config.json",
)


def read_port_from_config() -> Optional[int]:
    """从 VTS 配置文件中读取记录的 API 端口（优先于端口扫描）。"""

    for install_dir in _STEAM_PATHS.get(_get_os(), []):
        for relative in _VTS_CONFIG_RELATIVE_PATHS:
            config_path = Path(install_dir) / relative
            if not config_path.exists():
                continue
            port = _parse_vts_config_port(config_path)
            if port is not None:
                logger.debug(f"[发现] 配置文件端口：{port}")
                return port
    return None


# VTS 配置里与端口有关的键很多，只有这几个是 WebSocket API 端口；
# 其余（Config_Live2DAPI_Port、Config_Mocopi_Port、server_port…）都是别的服务，
# 误读会把插件指向一个根本不接受 WebSocket 的端口。
_VTS_API_PORT_KEYS = (
    "config_apiport",
    "apiport",
    "websocketport",
    "ws_port",
    "config_websocketport",
)


def _parse_vts_config_port(config_path: Path) -> Optional[int]:
    """从 vts_config.json 里取出 WebSocket API 端口。

    VTS 的配置是 {"StringData": [{"Key": ..., "Value": ...}], "IntData": [...]}
    这种 Unity 风格键值数组，API 端口键名是 Config_APIPort（实机确认于 1.35.x）。
    这里只认白名单里的键，避免把 Live2DAPI/Mocopi 之类端口当成 API 端口。
    """

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    candidates: list[object] = []
    for section in ("StringData", "IntData", "FloatData"):
        for item in raw.get(section) or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("Key") or "").strip().lower()
            if key in _VTS_API_PORT_KEYS:
                candidates.append(item.get("Value"))
    # 扁平结构（旧版 settings.json）也要能读
    for key in _VTS_API_PORT_KEYS:
        if key in raw:
            candidates.append(raw[key])

    for value in candidates:
        try:
            port = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if 1024 <= port <= 65535:
            return port
    return None


def is_vts_process_running() -> bool:
    """检测 VTube Studio 进程是否在运行。

    优先用 psutil；未安装时在 Windows 上退回 tasklist，其余平台返回 False。
    """

    os_name = _get_os()
    process_names = _VTS_PROCESS_NAMES.get(os_name, [])
    if not process_names:
        return False

    try:
        import psutil

        for proc in psutil.process_iter(["name"]):
            try:
                if proc.info["name"] in process_names:
                    logger.debug(f"[发现] VTube Studio 进程已启动：{proc.info['name']}")
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False
    except ImportError:
        pass

    # fallback：Windows 下用 tasklist
    if os_name == "Windows":
        try:
            import subprocess

            # 中文 Windows 的 tasklist 输出是 GBK，按默认 UTF-8 解码会直接抛 UnicodeDecodeError
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="gbk",
                errors="ignore",
                timeout=5.0,
            )
            for name in process_names:
                if name in result.stdout:
                    logger.debug(f"[发现] VTube Studio 进程已启动（tasklist）：{name}")
                    return True
        except Exception:
            pass
    return False


def find_vts_executable() -> Optional[Path]:
    """查找 VTube Studio 可执行文件的路径（从 Steam 目录）。"""

    for path_str in _STEAM_PATHS.get(_get_os(), []):
        exe_dir = Path(path_str)
        for exe_path in (
            exe_dir / "VTube Studio.exe",
            exe_dir / "VTube Studio.app/Contents/MacOS/VTube Studio",
        ):
            if exe_path.exists() and exe_path.is_file():
                logger.debug(f"[发现] VTube Studio 安装路径：{exe_path.parent}")
                return exe_path
    return None


async def _candidate_hosts(host: str) -> List[str]:
    """给出该尝试的主机列表：用户填的地址优先，然后是本机其它地址。

    Windows 上如果别的程序用更具体的绑定占了 127.0.0.1:8001（MaiBot 自己的
    HTTP 服务默认就在这个端口），VTS 监听在 0.0.0.0:8001 也连不上——loopback
    会被更具体的那条抢占。这时只有换用本机网卡 IP 才能连到 VTS。
    """

    hosts: List[str] = []

    def add(value: str) -> None:
        normalized = (value or "").strip()
        if normalized and normalized not in hosts:
            hosts.append(normalized)

    add(host)
    for loopback in ("127.0.0.1", "localhost", "::1"):
        add(loopback)
    for address in _local_ipv4_addresses():
        add(address)
    return hosts


def _local_ipv4_addresses() -> List[str]:
    """枚举本机非回环 IPv4 地址（不需要 psutil）。"""

    addresses: List[str] = []
    try:
        import socket

        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(info[4][0])
            if not address.startswith("127.") and address not in addresses:
                addresses.append(address)
    except Exception:
        pass
    if not addresses:
        # getaddrinfo 有时拿不到（多网卡/主机名未解析），用 UDP 探测出口地址兜底
        try:
            import socket

            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("8.8.8.8", 80))
                address = str(probe.getsockname()[0])
                if address and not address.startswith("127."):
                    addresses.append(address)
            finally:
                probe.close()
        except Exception:
            pass
    return addresses


async def _probe_host_port(host: str, port: int) -> bool:
    """确认某个 host:port 上确实是 VTS WebSocket API。"""

    if not await _async_port_open(host, port, timeout=1.0):
        return False
    return await _is_vts_websocket(host, port)


async def auto_discover(host: str = "localhost", timeout: float = 5.0) -> Tuple[str, int]:
    """自动发现 VTube Studio 的 host 和 port。

    策略顺序：
    1. 读取 VTS 配置文件里记录的 API 端口（最准，且能避开默认端口的占用冲突）；
    2. 试默认端口 8001；
    3. 扫描 8001~8010；
    4. 每个端口都在「用户填的地址 + 本机其它地址」上依次尝试，
       因为 loopback 端口可能被别的程序抢占（见 _candidate_hosts）；
    5. 全都连不上时回退 (host, 8001) 并打印诊断信息。

    Returns:
        Tuple[str, int]: 可用的 (host, port)。
    """

    logger.info(f"[发现] 开始自动发现 VTube Studio（系统：{_get_os()}）")
    hosts = await _candidate_hosts(host)

    # 端口候选：配置文件里的端口优先，然后是默认端口与扫描范围
    ports: List[int] = []
    config_port = read_port_from_config()
    if config_port:
        ports.append(config_port)
        logger.info(f"[发现] VTS 配置记录端口：{config_port}")
    for port in [VTS_DEFAULT_PORT, *VTS_SCAN_PORTS]:
        if port not in ports:
            ports.append(port)

    # 只保留在扫描超时范围内能试完的组合
    try:
        for port in ports:
            for candidate_host in hosts:
                if await _probe_host_port(candidate_host, port):
                    if candidate_host != host:
                        logger.warning(
                            f"[发现] 已改用 {candidate_host}:{port} 连接 VTS："
                            f"{host}:{port} 上不是 VTS（该端口可能被其它程序占用）"
                        )
                    else:
                        logger.info(f"[发现] 命中 {candidate_host}:{port}")
                    return candidate_host, port
    except asyncio.TimeoutError:
        logger.warning(f"[发现] 自动发现超时（{timeout} 秒）")

    # 记录进程状态，帮助用户判断 VTS 到底启没启动
    try:
        proc_running = is_vts_process_running()
        exe_path = find_vts_executable()

        if exe_path:
            logger.info(f"[发现] VTS 安装路径：{exe_path.parent}，但 API 端口未响应")
            logger.info("[发现] 请确认 VTube Studio 已启动，并在设置里打开「API」")
        elif not proc_running:
            logger.info("[发现] 未检测到 VTube Studio 进程，请先启动 VTube Studio")
    except Exception as exc:
        logger.debug(f"[发现] 进程检测失败：{exc}")

    logger.warning(f"[发现] 自动发现失败，回退到默认 {host}:{VTS_DEFAULT_PORT}")
    return host, VTS_DEFAULT_PORT


def get_install_info() -> dict:
    """返回当前平台的 VTS 安装信息。"""

    exe_path = find_vts_executable()
    return {
        "os": _get_os(),
        "process_running": is_vts_process_running(),
        "install_path": str(exe_path.parent) if exe_path else None,
        "exe_path": str(exe_path) if exe_path else None,
        "config_port": read_port_from_config(),
    }
