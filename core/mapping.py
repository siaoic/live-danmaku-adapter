"""直播事件 → MaiBot 标准消息字典的映射，以及通用防刷屏过滤。

纯函数 + 纯状态，不依赖任何平台客户端，方便离线验证。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from .events import (
    KIND_DANMAKU,
    KIND_GIFT,
    KIND_GUARD,
    KIND_SUPER_CHAT,
    LiveEvent,
)

# 通道对外声明的平台名；B 站与抖音都归一到这里，保证只占一个 platform 身份
CHANNEL_PLATFORM = "live"

# 网关声明的机器人账号；入站消息必须使用同一个值，否则 Host 会判为归属不一致
CHANNEL_ACCOUNT_ID = "live"

_GUARD_NAMES = {1: "总督", 2: "提督", 3: "舰长"}

# 事件类型 → 文本前缀；合并通道里 LLM 靠它分辨消息来自哪个平台
_KIND_PREFIXES = {
    KIND_DANMAKU: "弹幕",
    KIND_SUPER_CHAT: "醒目留言",
    KIND_GIFT: "礼物",
    KIND_GUARD: "上舰",
}

# 不可信文本（弹幕正文/观众昵称）里的结构符号 → 无害近似字符。
# 半角方括号是真实平台播报的格式（`[B站礼物] …送出了…`），全角【】是
# AI 自己的动作记号语法——这两样绝不能让观众用打字复现出来。
_UNTRUSTED_TRANSLATE = str.maketrans({
    "[": "（",
    "]": "）",
    "【": "〖",
    "】": "〗",
    "<": "〈",
    ">": "〉",
    "\r": " ",
    "\n": " ",
})


def sanitize_untrusted(text: str) -> str:
    """中和不可信文本里的结构符号（防注入/防伪造事件，见上方表注释）。

    内容本身保留（聊天价值不丢），但：
    1. 永远拼不出与真实平台播报一致的 `[…]` 前缀——假礼物/假上舰/假主播
       语音在格式上就不可能成立；
    2. 永远打不出 AI 的动作记号语法 `【…】`，观众无法诱导 AI 模仿记号。
    """

    return str(text or "").translate(_UNTRUSTED_TRANSLATE)


def format_event_text(event: LiveEvent) -> str:
    """把事件渲染成单行文本，前缀标明平台与事件类型。

    观众可控的字段（昵称、弹幕正文、SC 正文、上舰提示里的人名）一律经过
    :func:`sanitize_untrusted`——真实事件的播报格式只有插件自己能拼出来。
    """

    user_name = sanitize_untrusted(event.user_name) or "匿名观众"
    label = event.source_label

    if event.kind == KIND_SUPER_CHAT:
        return f"[{label}醒目留言 ¥{event.price:g}] {user_name}: {sanitize_untrusted(event.text)}"
    if event.kind == KIND_GIFT:
        count = event.gift_count or 1
        name = event.gift_name or "礼物"
        amount = f"，¥{event.price:g}" if event.price > 0 else ""
        return f"[{label}礼物] {user_name} 送出了 {name}x{count}{amount}"
    if event.kind == KIND_GUARD:
        guard = _GUARD_NAMES.get(event.guard_level, "舰长")
        detail = sanitize_untrusted(event.text) or f"{user_name} 开通了{guard}"
        return f"[{label}上舰·{guard}] {detail}"
    prefix = _KIND_PREFIXES.get(event.kind, event.kind)
    return f"[{label}{prefix}] {user_name}: {sanitize_untrusted(event.text)}"


@dataclass(slots=True)
class ChannelSettings:
    """通道侧的映射参数，由插件配置转换而来。"""

    merge_platforms: bool = True
    group_id: str = ""
    group_name: str = "直播间弹幕"


def build_message(event: LiveEvent, seq: int, settings: ChannelSettings) -> Dict[str, Any]:
    """构造 Host 构建消息所需的字典。

    字段约束（Host 侧会校验）：message_id 与 platform 必须是非空字符串，
    user_info.user_id 与 user_info.user_nickname 必须非空，群消息还要求
    group_info.group_id 非空，timestamp 是秒级 epoch 的字符串。
    """

    timestamp = event.timestamp or int(datetime.now().timestamp())
    text = format_event_text(event)

    if settings.merge_platforms:
        # 合并通道：两个平台写进同一个聊天流，来源只体现在文本前缀与附加字段
        group_id = settings.group_id or "live-all"
        group_name = settings.group_name or "直播弹幕"
    else:
        # 分房通道：每个平台房间各自一个聊天流
        group_id = f"{event.source}:{event.room_id}"
        group_name = f"{event.source_label}直播间{event.room_id}"

    additional_config: Dict[str, Any] = {
        "live_source": event.source,
        "live_platform_label": event.source_label,
        "live_room_id": event.room_id,
        "live_event_kind": event.kind,
        "live_user_id": event.user_id,
        "live_guard_level": str(event.guard_level or 0),
        "live_avatar": event.avatar,
    }
    if event.gift_name:
        additional_config["live_gift_name"] = event.gift_name
        additional_config["live_gift_count"] = str(event.gift_count or 0)
    if event.price > 0:
        additional_config["live_price"] = f"{event.price:g}"
    if event.mention > 0:
        # 数值型提及标记：宿主 is_mentioned_bot_in_message 把 >=1.0 视为
        # 「强制下一轮 Planner 回复」，不依赖 reply_timing 配置开关。
        additional_config["is_mentioned"] = float(event.mention)
    additional_config.update(event.extra)

    return {
        "message_id": f"live-{event.source}-{timestamp}-{seq}",
        "timestamp": str(timestamp),
        "platform": CHANNEL_PLATFORM,
        # 账号与平台必须与网关就绪声明一致，Host 会逐字段校验归属
        "account_id": CHANNEL_ACCOUNT_ID,
        "message_info": {
            "user_info": {
                "user_id": str(event.user_id or "0"),
                "user_nickname": event.user_name or "匿名观众",
            },
            "group_info": {
                "group_id": group_id,
                "group_name": group_name,
            },
            "additional_config": additional_config,
        },
        "raw_message": [{"type": "text", "data": text}],
        "processed_plain_text": text,
        "is_mentioned": event.mention > 0,
        "is_at": False,
        "is_notify": False,
        "is_emoji": False,
        "is_picture": False,
        "is_command": False,
    }


class DanmakuFilter:
    """弹幕防刷屏：空文本、方括号指令弹幕、同内容窗口去重、超长截断。

    脏话与敏感词不在这里处理——Host 入站链路已有过滤词与正则黑名单，
    插件重复实现只会互相打架。
    """

    def __init__(
        self,
        dedup_window_s: int = 300,
        dedup_max_entries: int = 500,
        drop_bracket: bool = True,
        max_text_len: int = 100,
    ) -> None:
        """初始化过滤器。

        Args:
            dedup_window_s: 同内容去重窗口（秒），0 表示不去重。
            dedup_max_entries: 去重记录条数上限。
            drop_bracket: 是否丢弃正文含方括号的指令/红包弹幕。
            max_text_len: 弹幕正文最长字符数，0 表示不截断。
        """

        self.dedup_window_s = dedup_window_s
        self.dedup_max_entries = dedup_max_entries
        self.drop_bracket = drop_bracket
        self.max_text_len = max_text_len
        # 文本 → 最近一次放行时间戳；只按插入顺序淘汰最旧的，防止无界增长
        self._recent: OrderedDict[str, float] = OrderedDict()

    def reset(self) -> None:
        """清空去重记录。"""

        self._recent.clear()

    def accept(self, text: str, now: float) -> Optional[str]:
        """返回过滤后的文本；None 表示这条不该进入聊天流。"""

        stripped = (text or "").strip()
        if not stripped:
            return None
        # 直播间的红包、投票、指令弹幕正文多为 [xxx] 占位，没有可聊的内容
        if self.drop_bracket and ("[" in stripped or "]" in stripped):
            return None
        # 防注入兜底：drop_bracket 关掉时半角 [] 也要中和，全角【】（AI 的
        # 动作记号语法）与换行（伪装多条消息）无论开关都中和。
        stripped = sanitize_untrusted(stripped)
        if self.dedup_window_s > 0:
            last_seen = self._recent.get(stripped)
            if last_seen is not None and now - last_seen < self.dedup_window_s:
                return None
            self._recent[stripped] = now
            self._recent.move_to_end(stripped)
            while len(self._recent) > self.dedup_max_entries:
                self._recent.popitem(last=False)
        if self.max_text_len > 0 and len(stripped) > self.max_text_len:
            return stripped[: self.max_text_len]
        return stripped