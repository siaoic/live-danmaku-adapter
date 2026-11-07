"""真实链路验证：绕开 MaiBot，直接启动两条平台链路，确认真实弹幕能被注入。

用桩 ctx 替换插件上下文，验证：
1. 网关就绪上报路径可达（桩会记录调用）；
2. B 站链路能连上并产出事件；
3. 抖音链路能连上（ttwid / roomId / 签名 / ack 全链路）并产出事件。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
MODULE_NAME = "live_danmaku_adapter_livetest"

spec = importlib.util.spec_from_file_location(
    MODULE_NAME, PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(PLUGIN_DIR)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[MODULE_NAME] = module
spec.loader.exec_module(module)

from live_danmaku_adapter_livetest.core.channel import LiveChannel
from live_danmaku_adapter_livetest.core.events import KIND_DANMAKU, SOURCE_BILIBILI, SOURCE_DOUYIN
from live_danmaku_adapter_livetest.core.mapping import ChannelSettings
from live_danmaku_adapter_livetest.platforms import BilibiliLiveLink, DouyinLiveLink


class _Logger:
    def info(self, message: object, *args: object, **kwargs: object) -> None:
        print("  LOG", message)

    def warning(self, message: object, *args: object, **kwargs: object) -> None:
        print("  WARN", message)

    def error(self, message: object, *args: object, **kwargs: object) -> None:
        print("  ERR", message)

    def debug(self, message: object, *args: object, **kwargs: object) -> None:
        pass


class _Gateway:
    def __init__(self) -> None:
        self.routed: list = []
        self.states: list = []

    async def route_message(self, gateway_name, message, *, route_metadata=None,
                            external_message_id="", dedupe_key=""):
        self.routed.append((gateway_name, message, dedupe_key))
        return True

    async def update_state(self, gateway_name, *, ready, platform="", account_id="", scope="", metadata=None):
        self.states.append((gateway_name, ready, platform, account_id, metadata))
        return True


class _Ctx:
    def __init__(self) -> None:
        self.logger = _Logger()
        self.gateway = _Gateway()


async def main() -> int:
    """跑 60 秒真实链路，统计各平台拿到的事件数。"""

    bili_room = int(sys.argv[1]) if len(sys.argv) > 1 else 21696950
    douyin_rid = sys.argv[2] if len(sys.argv) > 2 else "980375097435"

    ctx = _Ctx()
    channel = LiveChannel(ctx, "live_danmaku")
    channel.register_source(SOURCE_BILIBILI, "B站")
    channel.register_source(SOURCE_DOUYIN, "抖音")
    channel.configure_filter(300, 500, True, 100)
    await channel.start(ChannelSettings(merge_platforms=True, group_id="", group_name="直播间弹幕"),
                        {KIND_DANMAKU: True}, False)
    accepted = await channel.set_ready(True, {})
    print("网关就绪上报:", accepted)

    bili = BilibiliLiveLink(channel, bili_room, "")
    douyin = DouyinLiveLink(channel, douyin_rid, "")
    await bili.start()
    await douyin.start()

    await asyncio.sleep(60)

    stats = {}
    for _gateway_name, message, _key in ctx.gateway.routed:
        source = message["message_info"]["additional_config"]["live_source"]
        stats[source] = stats.get(source, 0) + 1

    print("--- 结果 ---")
    print("各平台注入条数:", stats)
    print("通道状态:", {k: v for k, v in channel.status().items() if k != "sources"})
    for item in channel.source_summary():
        print(" ", item)

    await bili.stop()
    await douyin.stop()
    await channel.stop()

    ok = bool(stats)
    print("链路验证:", "通过" if ok else "失败（60 秒内没有收到任何弹幕）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
