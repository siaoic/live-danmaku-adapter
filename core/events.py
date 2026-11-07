"""直播事件的统一模型。

B 站与抖音的原始事件字段差异很大，接入层先各自翻译成 LiveEvent，
再由统一通道归一化后注入 MaiBot 聊天流。这样两个平台走的是同一条通道、
同一套过滤与限流，只有 source 字段区分来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

# 平台标识：只出现在事件内部与消息附加字段里，不决定聊天流归属
SOURCE_BILIBILI = "bilibili"
SOURCE_DOUYIN = "douyin"
# 本机语音（ASR）：主播按热键说的话，走同一条通道进聊天流
SOURCE_ASR = "asr"

# 事件类型：两个平台共用同一套语义。
# 只有「有内容可聊」的事件：进场、点赞、关注这类互动通知不属于事件流。
KIND_DANMAKU = "danmaku"
KIND_SUPER_CHAT = "super_chat"
KIND_GIFT = "gift"
KIND_GUARD = "guard"
KIND_LIVE_END = "live_end"

SOURCE_LABELS: Dict[str, str] = {
    SOURCE_BILIBILI: "B站",
    SOURCE_DOUYIN: "抖音",
    SOURCE_ASR: "语音",
}


@dataclass(slots=True)
class LiveEvent:
    """一条已归一化的直播事件。

    Attributes:
        source: 来源平台，bilibili 或 douyin。
        room_id: 平台侧房间号（B 站真实房号 / 抖音 roomId）。
        user_id: 发送者 ID，取不到时填 "0"。
        user_name: 发送者昵称，空字符串由映射层补成「匿名观众」。
        kind: 事件类型，取值见本模块 KIND_* 常量。
        text: 事件正文；弹幕是弹幕内容，礼物/上舰是播报文本。
        event_id: 平台侧事件唯一 ID，用于入站去重。
        avatar: 头像 URL，可为空。
        guard_level: B 站舰队等级（1 总督 / 2 提督 / 3 舰长），抖音恒为 0。
        price: 金额（元），醒目留言与礼物的付费金额。
        gift_name: 礼物名称。
        gift_count: 礼物数量。
        timestamp: 秒级时间戳；0 表示由映射层补当前时间。
        mention: 提及强度。0 表示普通弹幕；填 1.0 会让宿主强制下一轮回复
            （additional_config 写入数值型 is_mentioned，主播语音用）。
        extra: 平台专有字段，原样写入消息附加配置。
    """

    source: str
    room_id: str
    user_id: str
    user_name: str
    kind: str
    text: str = ""
    event_id: str = ""
    avatar: str = ""
    guard_level: int = 0
    price: float = 0.0
    gift_name: str = ""
    gift_count: int = 0
    timestamp: int = 0
    mention: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def source_label(self) -> str:
        """返回面向用户展示的平台名。"""

        return SOURCE_LABELS.get(self.source, self.source)