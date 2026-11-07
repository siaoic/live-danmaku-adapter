"""直播适配器（MaiBot）：弹幕接入 + TTS 播报 + VTS 演出引擎 + 字幕/弹幕墙。

本插件由两个原插件合并而来，一个目录承载「输入 + 输出」全链路：

**输入（原 live-danmaku-adapter）**
把 B 站与抖音直播间的事件（弹幕/礼物/醒目留言/上舰）归一化后经同一条
MessageGateway 注入 MaiBot 聊天流，供 LLM 识别来源并回复。

**输出（原 live-stream-companion）**
1. **VTS 控制**：有机演出引擎（engine/performance_engine.py）以子进程常驻，
   按帧合成 Live2D 语义参数并注入 VTS 追踪输入，让模型一直在动；
2. **TTS**：调用 Breeze TTS 2 API 合成语音，按配置的播放设备播出；
3. **口型**：把 TTS 音频直接播到 VTS 监听的那块声卡，由 VTS 内置 ULipSync 驱动嘴型；
4. **主动对话**：任何时间都可主动开口自言自语，本轮上下文会隐藏直播间消息，
   不接弹幕、不回应观众；
5. **字幕层**（http://127.0.0.1:5679/）与 **弹幕墙**（http://127.0.0.1:5686/），
   给 OBS 浏览器源用。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import asyncio
import json
import logging
import re
import time
import uuid
from collections import deque
from datetime import datetime

from maibot_sdk import API, Command, Field, HookHandler, MaiBotPlugin, MessageGateway, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .core.channel import LiveChannel
from .core.events import KIND_DANMAKU, KIND_GIFT, KIND_GUARD, KIND_SUPER_CHAT
from .core.mapping import CHANNEL_ACCOUNT_ID, CHANNEL_PLATFORM, ChannelSettings
from .platforms import BilibiliLiveLink, DouyinLiveLink

from .asr import AsrHotkey, MicRecorder, SiliconFlowASR, pcm_to_wav
from .engine.manager import DEFAULT_ENGINE_PORT, PerformanceEngineManager
from .game_events import GameEventInbox, GameFeed
from .proactive import ProactiveDirector
from .subtitle import SubtitleServer
from .danmaku import DanmakuServer, parse_inbound
from .tts.devices import find_virtual_cable_output
from .tts.breeze import BreezeClient, BreezeError, BreezeParams
from .tts.playback import AudioPlayer
from .schedule_model import ScheduleTable, format_hhmm
from .vts.client import VTSClient, VTSClientError, VTSConnectionError, VTSResponseError
from .vts.discovery import auto_discover
from .vts.expression import detect_emotion, detect_prop, detect_reaction, parse_emotion_map
from .vts.markers import (
    DEFAULT_PROMPT_TEMPLATE,
    DIRECTION_ACTIONS,
    GAZE_TARGETS,
    MAX_ACTIONS_PER_REPLY,
    RESET_ACTIONS,
    action_kind_of,
    build_action_prompt,
    canonical_action_of,
    direction_instruction_of,
    gaze_vector_of,
    split_actions,
    strip_unknown_bracket_tags,
    strip_vocal_events,
)

logger = logging.getLogger("plugin.live_danmaku_adapter")

PLUGIN_ID = "local.live-danmaku-adapter"
GATEWAY_NAME = "live_danmaku"

# 兼容旧配置用的默认值；主动对话已改为纯事件驱动，不再有巡检定时器
PROACTIVE_TICK_SECONDS = 3.0
# 向宿主查询直播聊天流的超时（秒）：查不到就安静降级，绝不卡住事件回调链
STREAM_LOOKUP_TIMEOUT = 3.0
# 一条主动任务在 Maisaka 里跑完（规划 + 说话）通常要 6~10 秒，忙窗口默认 9 秒
PROACTIVE_INFLIGHT_SECONDS = 9.0
# 主动轮上下文隔离的兜底超时（秒）；实际值由 [proactive].isolation_seconds 覆盖
PROACTIVE_ISOLATION_SECONDS = 60.0
# 一次主动任务最多允许跑几步浏览器工具链。够「打开 -> 读快照 -> 再点进一个视频」，
# 超过就把 browser_* 工具从本轮工具表拿掉，逼模型用 reply 讲它刚看到的东西。
# 实测限流时每步要 10~20s，阈值过大会让收口拖到两分钟后，观众早就等走了。
MAX_PROACTIVE_BROWSER_CALLS = 4
# 「回完话接着自己找话题」的默认额度：一整段回到观众之后，允许再自说几轮。
# 只给回应别人的段发额度，自语段不再续额度，所以链长有上限，不会退化成
# 「音频多长、话就多密」的自循环节拍器；0 = 回完就安静（旧行为）。
PROACTIVE_FOLLOWUP_TURNS = 1

# AI 在台词里点过表情/道具之后，多久内不再让关键词层重新猜。
# 一条回复会被拆成多条消息依次出站，只有第一句带记号；这整段都该是
# AI 选的那张脸，不能播到第二句又被「哈哈」判成开心换掉。
AI_ACTION_GRACE = 10.0

# 语音快速通道的默认写作要求。
# Replyer 在宿主里的默认风格是「长回复」，直播里那样会一口气说一大段；
# 这里必须明确压短，否则绕开 Planner 反而更啰嗦。
VOICE_REPLY_HINT = (
    "这是直播间的实时语音对话：像顺口接话一样回答，一到两句，口语，"
    "别用书面语，不要分段、不要列点、不要复述对方的问题。"
)

# 弹幕回复节流的频率倍率边界：0 会让宿主进入「静默消费」连主动轮次都不规划，
# 所以下限留 0.05；1.0 等于不节流。
REPLY_FREQUENCY_MIN = 0.05
REPLY_FREQUENCY_MAX = 1.0
# 巡检循环唤醒间隔（秒）；真正的校验由配置里的 recheck_seconds 节流
REPLY_ADJUST_TICK_SECONDS = 1.0

# TTS 输出的采样率（breeze-tts-2 输出 24k 单声道 int16 裸 PCM）
STREAM_SAMPLE_RATE = 24000
# 流式播放的位宽（int16 → 2 字节/采样），用来把 PCM 字节数换算成真实音频时长
STREAM_BYTES_PER_SAMPLE = 2

# 通道归属写死（不再是配置项）：B 站与抖音合并进同一个聊天流。
# 分房模式（每平台一个流）需要连动门控/节流/表情包屏蔽一起重新验证，
# 不是改一个布尔值的事；要改直接改这里。
CHANNEL_MERGE_PLATFORMS = True
CHANNEL_GROUP_ID = ""              # 空 = 使用默认 live-all
CHANNEL_GROUP_NAME = "直播间弹幕"

# 直播流里隐藏的表情包工具（send_emoji=旧逻辑独立工具；show_emoji_list=新逻辑选图）
_BLOCKED_EMOJI_TOOLS = frozenset({"send_emoji", "show_emoji_list"})

# 直播间消息在 Planner/Replyer 上下文里的前缀，来自 core/mapping.py::format_event_text
# 例：[B站弹幕] / [抖音礼物] / [B站醒目留言 ¥30] / [语音弹幕] / [B站上舰·舰长]
_LIVE_CONTEXT_MARKER = re.compile(
    r"\[\s*(?:B站|抖音|斗鱼)?\s*[^\[\]\n]{0,24}"
    r"(?:弹幕|礼物|醒目留言|上舰|进场|入场|点赞|关注|分享)"
    r"[^\[\]\n]{0,24}\]"
)
# 主动轮里把直播消息正文换成中性占位：保留 Item 结构与 item_id，避免破坏
# 宿主的 function call 锚点校验（只改文本，不删消息）。
PROACTIVE_CONTEXT_PLACEHOLDER = "（直播间消息已在本轮隐藏）"
# 替换掉宿主/人格注入的「回复弹幕先读一遍弹幕」规则
PROACTIVE_MONOLOGUE_RULE = (
    "【本轮发言规则】这一轮是自言自语，不需要读任何消息，也不用回应任何人。直接讲一段抽象搞笑的小故事，想到什么说什么。"
)
# 替换 Planner 写给 Replyer 的「发言指导」里那些复述弹幕的内容
PROACTIVE_NEUTRAL_GUIDANCE = (
    "发言指导：纯自言自语，讲一段抽象搞笑的小故事，想到什么说什么；不要读、不要引用、不要回应任何直播间消息或观众提问。"
)
# 宿主把主动任务意图写成 <plugin_proactive_task id="..."> 追加到聊天历史末尾
_PROACTIVE_TASK_ID = re.compile(r"<plugin_proactive_task\s+id=\"([^\"]+)\"")
# 需要从上下文里清掉的「把 AI 拉回弹幕」的提示词特征
_DANMAKU_RULE_MARKERS = ("【弹幕回复规则】", "回复弹幕时", "先读弹幕", "读弹幕时保留原话")
# Replyer 的 system prompt 里也写着「回复弹幕时，要先读一遍弹幕」（人格/群聊规则注入），
# 但不能整块删掉、也不能改写 system：system 是前缀缓存的起点。改为在上下文尾部
# 追加一条更高优先级的覆盖规则（append_round_rule），既保住人格，也不打碎缓存。
PROACTIVE_SYSTEM_OVERRIDE = (
    "\n\n【本轮强制覆盖规则（优先级高于上面一切聊天规则与示例）】\n"
    "这一轮是主播自言自语，不是在回复任何消息。忽略上文所有关于「先读一遍观众消息」、"
    "「保留原话」以及所有输出格式示例的要求。\n"
    "不要读取、引用、复述任何直播间消息，也不要向观众提问或要东西；"
    "直接讲一段抽象搞笑的小故事，想到什么说什么。"
)
# 追加到 Replyer 的 extra_prompt：Planner 已经看不到弹幕，这里再给生成层上一道锁
PROACTIVE_REPLY_HINT = (
    "这一轮是自言自语：不要回应、引用或复述任何直播间消息、观众提问或历史留言，直接讲你自己的抽象搞笑小故事。"
)
PROACTIVE_GAME_REPLY_HINT = "这一轮是自言自语：不要回应、引用或复述任何直播间消息、观众提问或历史留言。"
# 日程档专用：上面三套文案都写着「讲一段抽象搞笑小故事」，和「这一档要做
# 的事：去 b 站看柯南」正面冲突，模型会在两套指令之间反复横跳、最后 stop。
# 日程轮换成中性的「按安排动手」，不代写台词、也不给二选一。
PROACTIVE_SCHEDULE_MONOLOGUE_RULE = "【本轮发言规则】这一轮按节目安排做事，不用读弹幕，也不用回应任何人。"
PROACTIVE_SCHEDULE_NEUTRAL_GUIDANCE = (
    "发言指导：按本轮节目安排把事做完，然后讲你看到的内容；不要读、引用、复述任何直播间消息，也不要向观众提问或要东西。"
)
PROACTIVE_SCHEDULE_SYSTEM_OVERRIDE = (
    "\n\n【本轮强制覆盖规则（优先级高于上面一切聊天规则与示例）】\n"
    "这一轮是主播按节目安排自言自语，不是在回复任何消息。忽略上文所有关于「先读一遍观众消息」、"
    "「保留原话」以及所有输出格式示例的要求。\n"
    "不要读取、引用、复述任何直播间消息，也不要向观众提问或要东西；"
    "按本轮节目安排动手，做完直接讲看到的东西。"
)
# 主动轮只清洗上下文最后这么多条 Item：更早的历史保持原样，prompt 前缀稳定，
# DeepSeek 的上下文缓存才能持续命中；新弹幕集中在尾部，清尾部足够隔离本轮输入。
PROACTIVE_TAIL_ITEMS = 8

# 「回完弹幕接着聊」专用：上一套文案写着「讲一段新的抽象搞笑小故事」，和
# 「顺着刚才那句话往下说」正面冲突，模型会在两套指令之间横跳、最后 stop。
# 续聊轮统一改成「接着刚才那句说」，不代写台词、也不给二选一。
PROACTIVE_FOLLOWUP_SYSTEM_OVERRIDE = (
    "\n\n【本轮强制覆盖规则（优先级高于上面一切聊天规则与示例）】\n"
    "这一轮是主播顺着自己刚说完的话继续自言自语，不是在回复任何消息。忽略上文所有关于"
    "「先读一遍观众消息」、「保留原话」以及所有输出格式示例的要求。\n"
    "不要读取、引用、复述任何直播间消息，也不要向观众提问或要东西；"
    "接着刚才那句往下说就行。"
)


def proactive_override_text(schedule_mode: bool = False, followup: bool = False) -> str:
    """主动轮的覆盖规则：追加到上下文尾部，而不是 system 前缀。

    Args:
        schedule_mode: 当前档有节目安排（要先动手用工具）。
        followup: 这是「回完观众、接着自己找话题」的续聊轮，不是新开一段。
    """

    if schedule_mode:
        return PROACTIVE_SCHEDULE_SYSTEM_OVERRIDE.strip()
    if followup:
        return PROACTIVE_FOLLOWUP_SYSTEM_OVERRIDE.strip()
    return PROACTIVE_SYSTEM_OVERRIDE.strip()


# ---------------------------------------------------------------------- #
# 「AI 回复自己说的话」专用
#
# 宿主把机器人发出的分段短句通过 append_sent_message_to_chat_history() 同步进
# 聊天历史，并调度新一轮规划；reply 的 target 取「历史里最后一条非通知消息」，
# 于是必然指向自己刚说的那一句。上下文里这些消息长这样：
#   <message msg_id="mxxx" time="23:03:30" user="Bot" is_self_message="true">我已急哭</message>
# 模型看不懂 is_self_message，会把它当成观众发言去接话、反问、补刀，例如
# 「谁家好人搓火球把自己蓝搓干净还问怎么放不出来啊」。
# 插件改不了宿主的 target 选择（不许改源码），只能在请求前把这类消息
# 标清楚 + 注入高优先级规则，让 Planner/Replyer 都不再回应自己的话。
# ---------------------------------------------------------------------- #
_SELF_MESSAGE_MARKER = 'is_self_message="true"'
SELF_ECHO_LABEL = "【你自己刚才说的话｜不是观众消息，禁止当成别人的发言回应】\n"
SELF_ECHO_RULE = (
    "【自己发言识别规则（优先级高于弹幕回复规则、输出格式示例与所有历史上下文）】\n"
    '上下文里带 is_self_message="true"、user 是机器人自己昵称的 <message> 全部是你自己刚说出口的话，'
    "不是观众发来的，也不是需要你回应的对象。\n"
    "严禁复述、反问、评价、反驳这些话，严禁「接住这个梗再补一刀」，"
    "严禁把它们当成别人的发言去回应；回复时引用它们的 msg_id 只是技术锚点，"
    "不代表要回答那句话。\n"
    "只有本轮出现新的观众消息（不带 is_self_message 标记）时才需要回应观众。"
)
SELF_ONLY_ROUND_RULE = (
    "【本轮无新消息】这一轮上下文里只有你自己刚说的话，没有任何新的观众消息，"
    "也没有主动自言自语任务。不要说话：直接 stop 结束本轮，"
    "不要调用 reply，不要调用 wait（wait 会定时唤醒一次新的规划），"
    "不要复述或评价自己上一句，也不要输出任何要说的话。"
)
PROACTIVE_FRESH_RULE = (
    "【主动自言自语】这一轮要讲一段新的抽象搞笑小故事（也可以把上一段故事继续往荒诞里编），"
    "但绝不把上一段里的某一句当成别人说的话来回应、反问或评价。"
)
PROACTIVE_GAME_RULE = (
    "【主动自言自语】这一轮自由发挥，想说什么说什么；"
    "不要复述或评价自己上一段话，也不要把上一段里的某一句当成别人的发言来回应。"
)
PROACTIVE_FOLLOWUP_RULE = (
    "【主动自言自语·接着聊】这一轮顺着你自己刚说完的话往下讲：补充细节、抖个包袱、"
    "或者自然换个相关话题都行；不要复读上一段，也不要把上一段里的某一句当成别人的发言来回应。"
)


def proactive_schedule_rule(focus: str) -> str:
    """当前节目安排：主动轮尾部规则，要求先用工具把事真的做了。

    日程插件已经把「当前时段」写进 system，但那只是约束文字；直播流里
    Planner 默认只看到 12 个常驻工具，浏览器／看视频工具要么 deferred
    要么需要 tool_search，模型不会自己去翻。这里把「动手」写成必须项。
    """

    return (
        f"【按节目安排动手】这一档要做的事：{focus.strip()}。\n"
        "本轮先用 browser_navigate 打开网页（清单里没有就 tool_search 搜「打开网页」「读取页面」），"
        "再用 browser_snapshot（带 depth=4）读一遍页面内容，然后立刻开口讲看到的东西；"
        "打开一个就够，别反复导航；别只开网页不说话，也别跳过工具直接 stop，别凭空编。"
    )


# 宿主 reasoning_engine.run_loop 在工具执行完后会 continue 到下一轮内部思考
# （MAX_INTERNAL_ROUNDS=16，日志里的「回合 3/16、4/16」），哪怕这一轮没有任何
# 新消息。这正是「整段 TTS 还没播完，Planner 又起来了」的源头，也是上下文被
# 工具调用/回执堆到 16k token 的原因。stop 工具的返回值带
# metadata["pause_execution"]=True，宿主会 break 出内部循环进入等待新消息状态，
# 于是下一轮 Planner 压根不会被触发（不是拦截、不是挂起请求）。
STOP_TOOL_NAME = "stop"


def _item_text(item: Any) -> str:
    """取一个 Context Item 的文本内容（纯函数）。"""

    if not isinstance(item, dict):
        return ""
    parts = item.get("parts")
    if not isinstance(parts, list):
        return ""
    return " ".join(
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict) and str(part.get("type") or "") == "text"
    )


def annotate_self_messages(items: Any) -> tuple[list, int]:
    """给「自己发出的历史消息」加上明确标签（纯函数，只改文本 part）。

    不删 Item、不动 item_id/meta，宿主的 Context Item 协议与 function call
    锚点校验都能通过；保留原文是为了让 AI 还记得自己讲过什么，避免重复。
    """

    if not isinstance(items, list):
        return [], 0
    result: list = []
    annotated = 0
    for item in items:
        if not isinstance(item, dict):
            result.append(item)
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            result.append(item)
            continue
        new_parts: list = []
        changed = False
        for part in parts:
            if (
                isinstance(part, dict)
                and str(part.get("type") or "") == "text"
                and isinstance(part.get("text"), str)
                and _SELF_MESSAGE_MARKER in part["text"]
                and SELF_ECHO_LABEL not in part["text"]
            ):
                new_parts.append({**part, "text": SELF_ECHO_LABEL + part["text"]})
                changed = True
                continue
            new_parts.append(part)
        if changed:
            result.append({**item, "parts": new_parts})
            annotated += 1
        else:
            result.append(item)
    return result, annotated


def append_round_rule(items: Any, rule: str) -> tuple[list, bool]:
    """把本轮规则放到最后一条文本 Item 的开头（纯函数，只改文本 part）。

    放在尾部而不是 system：system 是前缀缓存的起点，每轮改动会让整段
    prompt 缓存失效；尾部规则是常量，连续轮次之间前缀保持稳定。
    """

    if not isinstance(items, list):
        return [], False
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if not isinstance(item, dict) or not isinstance(item.get("parts"), list):
            continue
        parts = item["parts"]
        for part_index, part in enumerate(parts):
            if not (
                isinstance(part, dict) and str(part.get("type") or "") == "text" and isinstance(part.get("text"), str)
            ):
                continue
            if rule in part["text"]:
                return items, False
            new_parts = list(parts)
            new_parts[part_index] = {**part, "text": f"{rule}\n\n{part['text']}"}
            return (
                items[:index] + [{**item, "parts": new_parts}] + items[index + 1 :],
                True,
            )
    return items, False


def append_stop_tool_call(items: Any) -> tuple[list, bool]:
    """Planner 输出里出现 reply 时，在末尾补一个合法的 stop 工具调用（纯函数）。

    只改输出 Items，不动宿主的源码与内部循环。补的 Item 必须能被
    `deserialize_prompt_items(..., mode=MODEL_OUTPUT)` 还原并通过协议校验：
    - item_id / call_id 全局唯一；
    - logical_turn_id 取本轮已有 FunctionCallItem 的（不能为空）；
    - timestamp 是合法 ISO 字符串；
    - tool_call.args 是字典。
    """

    if not isinstance(items, list):
        return [], False
    names: list[str] = []
    turn_id = ""
    for item in items:
        if not isinstance(item, dict) or item.get("item_type") != "FunctionCallItem":
            continue
        call = item.get("tool_call")
        if isinstance(call, dict):
            names.append(str(call.get("func_name") or ""))
        meta = item.get("meta")
        if isinstance(meta, dict) and isinstance(meta.get("logical_turn_id"), str):
            turn_id = meta["logical_turn_id"] or turn_id
    # 没有 reply（本来就要 stop/wait 或无工具输出）就不需要收口；
    # 模型已经自己调了 stop/wait 也别重复追加。
    if "reply" not in names or "stop" in names or "wait" in names:
        return items, False

    stop_item = {
        "item_type": "FunctionCallItem",
        "meta": {
            "item_id": uuid.uuid4().hex,
            "logical_turn_id": turn_id or uuid.uuid4().hex,
            "timestamp": datetime.now().isoformat(),
        },
        "tool_call": {
            "call_id": f"call_live_stop_{uuid.uuid4().hex[:24]}",
            "func_name": STOP_TOOL_NAME,
            "args": {},
            "extra_content": {"tool_call_source": "response"},
        },
    }
    return [*items, stop_item], True


def retarget_proactive_reply(items: Any, msg_id: str) -> tuple[list, int]:
    """主动轮：把 reply 的目标改成这一轮的主动任务本身（纯函数）。

    宿主 reply 工具要求 ``msg_id`` 能在会话里找到。主动轮没有「要回复的观众
    消息」，模型就会抓一个过期的旧 id（真机实测 ``maxeeau``）或干脆填 ``none``，
    结果宿主返回「未找到要回复的目标消息」→ 不发送 → 不播报，日志看起来像
    「回复工具未生成可见消息」，其实话是生成过的。

    ``<plugin_proactive_task id="...">`` 就是本轮写进历史的那条内部消息，
    拿它当锚点，reply 才能真的发出去（也仍然不指向任何观众消息）。
    """

    if not isinstance(items, list):
        return [], 0
    if not msg_id:
        return items, 0
    fixed = 0
    out: list = []
    for item in items:
        if not isinstance(item, dict) or item.get("item_type") != "FunctionCallItem":
            out.append(item)
            continue
        call = item.get("tool_call")
        if not isinstance(call, dict) or str(call.get("func_name") or "") != "reply":
            out.append(item)
            continue
        args = call.get("args")
        if not isinstance(args, dict):
            out.append(item)
            continue
        current = str(args.get("msg_id") or "").strip()
        if current == msg_id:
            out.append(item)
            continue
        out.append({**item, "tool_call": {**call, "args": {**args, "msg_id": msg_id}}})
        fixed += 1
    # 没有需要改的就原样返回，避免无谓的列表复制
    if not fixed:
        return items, 0
    return out, fixed


#: 字幕断句用的句末/停顿标点
_SUBTITLE_SENTENCE_END = "。！？!?；;…\n"
_SUBTITLE_CLAUSE_END = "，,、：:"


def split_subtitle_sentences(text: str, *, max_chars: int = 24) -> list[str]:
    """把一段话切成适合逐句上字幕的短句（纯函数）。

    整段一次性推给字幕页面就是「一下全显示」。按句末标点切开、过长的再按
    逗号拆一档，播放推进到哪句就只显示哪句。切分不丢字：各片段用 ``strip``
    去掉首尾空白，但句子内容原样保留。
    """

    raw = str(text or "").strip()
    if not raw:
        return []
    limit = max(6, int(max_chars or 24))

    chunks: list[str] = []
    buf = ""
    for ch in raw:
        buf += ch
        if ch in _SUBTITLE_SENTENCE_END:
            if buf.strip():
                chunks.append(buf.strip())
            buf = ""
    if buf.strip():
        chunks.append(buf.strip())

    sentences: list[str] = []
    for chunk in chunks:
        if len(chunk) <= limit:
            sentences.append(chunk)
            continue
        # 太长再按停顿拆，避免一句字幕铺满整个画面
        piece = ""
        for ch in chunk:
            piece += ch
            if ch in _SUBTITLE_CLAUSE_END and len(piece) >= max(6, limit // 2):
                sentences.append(piece.strip())
                piece = ""
        if piece.strip():
            sentences.append(piece.strip())
    return sentences or [raw]


def last_context_message_id(items: Any) -> str:
    """取上下文里最后一条聊天消息的 msg_id（纯函数）。

    主动轮 reply 需要一条宿主查得到的「真实消息」当锚点：主动任务的
    ``<plugin_proactive_task>`` 属于内部消息，``find_source_message_by_id``
    查不到；而上下文里这些 ``<message msg_id="...">`` 是真实会话消息，
    拿它当锚点既能发出声音，又不指向任何要回应的对象。
    """

    if not isinstance(items, list):
        return ""
    anchor = ""
    for item in items:
        for match in re.findall(r'<message\b[^>]*\bmsg_id="([^"]+)"', _item_text(item)):
            if match.strip():
                anchor = match.strip()
    return anchor


def self_only_round(items: Any) -> bool:
    """本轮上下文里的聊天消息是否**全是**机器人自己刚说的（没有任何观众消息）。

    宿主把发出的分段短句同步进历史后会立刻调度一轮规划，这一轮里往往只有
    自己的 3~6 条短句（实测快照 23:03:33），模型便开始反问、反驳自己。
    必须用**清洗前**的原始 items 判定：主动轮清洗后弹幕会变成占位符，
    再看就会把所有轮都误判成「没有新消息」。聊天历史不保证严格按时间排序，
    所以这里统计「有没有非自身消息」，而不是看最后一条。
    """

    if not isinstance(items, list):
        return False
    tags = [tag for item in items for tag in re.findall(r"<message\b[^>]*>", _item_text(item))]
    return bool(tags) and all(_SELF_MESSAGE_MARKER in tag for tag in tags)


def _proactive_text_replacement(text: str, item_type: str, schedule_mode: bool = False) -> Optional[str]:
    """主动轮里这段上下文文本应替换成什么；返回 None 表示原样保留。

    ``schedule_mode`` 为真时换成日程档文案，避免「讲抽象搞笑小故事」把
    「按节目安排打开网页」顶掉。默认 False 保持原有行为与测试兼容。
    """

    guidance = PROACTIVE_SCHEDULE_NEUTRAL_GUIDANCE if schedule_mode else PROACTIVE_NEUTRAL_GUIDANCE
    monologue = PROACTIVE_SCHEDULE_MONOLOGUE_RULE if schedule_mode else PROACTIVE_MONOLOGUE_RULE

    # 自己说的话即便正文里提到「弹幕」，也绝不能被当成直播弹幕清洗掉：
    # 它已经由 annotate_self_messages() 标注，保留下来才不会让 AI 忘了
    # 自己刚讲过什么，同时又不会把它当成观众发言去回应。
    if _SELF_MESSAGE_MARKER in text or SELF_ECHO_LABEL in text:
        return None
    # system 是 prompt 前缀缓存的第一段：主动轮往 system 里追加覆盖规则，
    # 会让之后所有轮次的缓存前缀整体失效（实测命中率从 ~90% 掉到 <10%）。
    # 覆盖规则改由调用方追加到上下文尾部（append_round_rule），这里不动 system。
    if item_type == "SystemMessageItem":
        return None
    if _LIVE_CONTEXT_MARKER.search(text):
        return PROACTIVE_CONTEXT_PLACEHOLDER
    if any(marker in text for marker in _DANMAKU_RULE_MARKERS):
        return guidance if "发言指导" in text else monologue
    if "【上下文状态摘要" in text and ("弹幕" in text or "观众" in text):
        return PROACTIVE_CONTEXT_PLACEHOLDER
    # Planner 历史里「弹幕读到 xxx」的思考，会把之后的轮次又带回弹幕
    if item_type == "AssistantMessageItem" and "弹幕" in text:
        return PROACTIVE_CONTEXT_PLACEHOLDER
    if item_type == "UserMessageItem" and "弹幕" in text:
        # 「发言指导」「弹幕：xxx」这类块里往往还夹着自言自语的具体点子，
        # 按行删掉提到弹幕/直播消息的句子，其余保留，再补一条中性要求。
        if text.lstrip().startswith("弹幕："):
            return guidance
        if "发言指导" in text:
            kept = [line for line in text.splitlines() if "弹幕" not in line and not _LIVE_CONTEXT_MARKER.search(line)]
            body = "\n".join(line for line in kept if line.strip())
            return f"{body}\n{guidance}" if body else guidance
        # 其余含弹幕的提示块（句式建议、注意事项等）整段隐藏
        return PROACTIVE_CONTEXT_PLACEHOLDER
    return None


def strip_live_context_items(items: Any, schedule_mode: bool = False, tail_items: int = 0) -> tuple[list, int]:
    """主动轮清洗请求上下文（纯函数，便于离线测试）。

    清掉四类会把 AI 拉回弹幕的内容：直播消息正文、宿主/人格注入的
    「回复弹幕先读弹幕」规则、含弹幕的上下文状态摘要、Planner 历史里
    复述弹幕的发言指导。只改文本 part，不删 Item、不改 item_id/meta，
    因此宿主的 Context Item 协议校验与 function call 锚点校验都能通过。

    ``tail_items`` > 0 时只清洗最后这么多条 Item：更早的历史保持原样，
    这样 prompt 的前缀不会在每次主动轮被改写，DeepSeek 的上下文缓存才能
    持续命中；新弹幕集中在尾部，只清尾部就足以隔离本轮输入。
    """

    if not isinstance(items, list):
        return [], 0
    result: list = []
    hidden = 0
    start = max(0, len(items) - tail_items) if tail_items > 0 else 0
    for index, item in enumerate(items):
        if index < start or not isinstance(item, dict):
            result.append(item)
            continue
        item_type = str(item.get("item_type") or "")
        if item_type not in ("SystemMessageItem", "UserMessageItem", "AssistantMessageItem"):
            result.append(item)
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            result.append(item)
            continue
        new_parts: list = []
        changed = False
        for part in parts:
            if isinstance(part, dict) and str(part.get("type") or "") == "text" and isinstance(part.get("text"), str):
                replacement = _proactive_text_replacement(part["text"], item_type, schedule_mode=schedule_mode)
                if replacement is not None:
                    new_parts.append({**part, "text": replacement})
                    changed = True
                    continue
            new_parts.append(part)
        if changed:
            result.append({**item, "parts": new_parts})
            hidden += 1
        else:
            result.append(item)
    return result, hidden


def items_have_active_proactive_task(items: Any, active_task_ids: Any) -> bool:
    """判断这次请求是不是「本插件刚发起的主动任务」那一轮。

    宿主把 ``<plugin_proactive_task id="...">`` 写进上下文。弹幕回复轮里只有
    旧的 task_id，主动轮里才有刚登记的 id，因此可以精确区分，比按消息先后
    顺序可靠（直播弹幕随时会插在主动任务后面）。

    * 没拿到 task_id（能力返回缺字段）→ 退回时间窗判据，返回 True；
    * 上下文里完全没有任务标记（被裁剪）→ 同样退回时间窗，返回 True；
    * 有标记但没有一个属于当前活跃任务 → 这是后续轮次，返回 False。
    """

    active = {str(tid) for tid in (active_task_ids or ()) if str(tid).strip()}
    if not active or not isinstance(items, list):
        return True
    found: list[str] = []
    for item in items:
        found.extend(_PROACTIVE_TASK_ID.findall(_item_text(item)))
    if not found:
        return True
    return any(task_id in active for task_id in found)


def items_have_proactive_tool_chain(items: Any) -> bool:
    """上下文尾部还挂着主动任务的工具链，说明同一个 turn 没跑完。

    宿主在每次工具执行后都会重新调度 Planner，并把 ``<plugin_proactive_task>``
    标记从新上下文里换掉/裁掉。只靠 task_id 判断会把「工具链进行中」误判成
    「上一轮主动任务已结束的弹幕回复轮」，于是隔离被提前解除、弹幕规则回流、
    接着注入「直接 stop」—— AI 网页都打开了却一句话不说。

    只看尾部，避免历史里很早以前的浏览器工具把普通弹幕轮误判成主动轮。
    """

    if not isinstance(items, list):
        return False
    last_tool = ""
    for item in items[-24:]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("item_type") or "")
        if kind == "FunctionCallItem":
            call = item.get("tool_call")
            if isinstance(call, dict):
                last_tool = str(call.get("func_name") or "") or last_tool
        elif kind == "FunctionCallOutputItem":
            name = str(item.get("tool_name") or "")
            if name:
                last_tool = name
    return last_tool.startswith(("browser_", "tool_search", "game_"))


def browser_calls_in_turn(items: Any) -> int:
    """本轮（最后一次开口/最新用户消息之后）已经调过几次浏览器工具。

    用来做"每轮最多 N 次"的限制：新弹幕进来是新一轮，计数归零；
    同一轮里打开网页、读完快照后达到上限，就把 browser_* 拿掉，逼她开口。
    """

    if not isinstance(items, list):
        return 0
    count = 0
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("item_type") or "")
        if kind == "UserMessageItem":
            break
        if kind != "FunctionCallItem":
            continue
        call = item.get("tool_call")
        name = str(call.get("func_name") or "") if isinstance(call, dict) else ""
        if name in ("reply", "say", "live_sight"):
            # 上一轮已经开口，或陪看插件已经说过话 -> 这里是新一轮
            if name in ("reply", "say"):
                break
            continue
        if name.startswith("browser_"):
            count += 1
    return count


def resolve_reply_frequency(enabled: bool, value: float) -> float:
    """把 [reply] 配置换算成宿主可接受的回复频率倍率（纯函数，便于离线测试）。

    0 会让宿主进入「静默消费」（连主动轮次都不规划），所以下限锁 0.05；
    enabled=false 时返回 1.0，把频率控制权交还宿主全局 talk_value。
    """

    if not enabled:
        return REPLY_FREQUENCY_MAX
    try:
        target = float(value)
    except (TypeError, ValueError):
        target = 0.3
    return min(REPLY_FREQUENCY_MAX, max(REPLY_FREQUENCY_MIN, target))


# ==================================================================== #
# 配置类
# ==================================================================== #


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    # 这个键由宿主读写（WebUI 插件列表的启停按钮、is_plugin_disabled）
    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={"label": "启用插件", "hint": "填好 TTS 地址与参考音频、并让 VTS 授权后再打开"},
    )
    config_version: str = Field(
        default="1.2.0",
        description="配置版本，由程序维护，请勿手动修改",
        json_schema_extra={"label": "配置版本", "hint": "由程序自动维护"},
    )


class BilibiliConfig(PluginConfigBase):
    """B 站直播间配置。"""

    __ui_label__ = "B站"
    __ui_icon__ = "tv"
    __ui_order__ = 1

    enabled: bool = Field(
        default=True,
        description="是否接入 B 站直播间",
        json_schema_extra={"label": "接入B站"},
    )
    room_id: int = Field(
        default=0,
        description="B 站直播间号，0 表示不启动",
        json_schema_extra={"label": "直播间号"},
    )
    sessdata: str = Field(
        default="",
        description="可选：B 站 Cookie SESSDATA",
        json_schema_extra={"label": "登录凭证SESSDATA", "hint": "留空也能收弹幕，只是观众昵称会被打码"},
    )


class DouyinConfig(PluginConfigBase):
    """抖音直播间配置。"""

    __ui_label__ = "抖音"
    __ui_icon__ = "music-2"
    __ui_order__ = 2

    enabled: bool = Field(
        default=False,
        description="是否接入抖音直播间",
        json_schema_extra={"label": "接入抖音"},
    )
    web_rid: str = Field(
        default="",
        description="抖音直播间号（live.douyin.com/ 后的数字）",
        json_schema_extra={"label": "直播间号", "hint": "打开抖音直播间，地址栏 live.douyin.com/ 后面那串数字"},
    )
    extra_cookie: str = Field(
        default="",
        description="可选：额外的抖音 Cookie",
        json_schema_extra={"label": "额外Cookie", "hint": "一般留空即可"},
    )


class EventsConfig(PluginConfigBase):
    """直播事件开关。"""

    __ui_label__ = "直播事件"
    __ui_icon__ = "radio"
    __ui_order__ = 5

    danmaku: bool = Field(default=True, description="普通弹幕", json_schema_extra={"label": "弹幕"})
    super_chat: bool = Field(default=True, description="醒目留言（SC）", json_schema_extra={"label": "醒目留言"})
    gift: bool = Field(default=True, description="礼物播报", json_schema_extra={"label": "礼物"})
    guard: bool = Field(default=True, description="上舰播报（B 站总督/提督/舰长）", json_schema_extra={"label": "上舰"})


class FilterConfig(PluginConfigBase):
    """弹幕防刷屏配置。"""

    __ui_label__ = "防刷屏"
    __ui_icon__ = "filter"
    __ui_order__ = 6

    dedup_window_s: int = Field(
        default=300, description="同内容弹幕去重窗口（秒），0 表示不去重", json_schema_extra={"label": "去重窗口（秒）"}
    )
    dedup_max_entries: int = Field(
        default=500, description="去重记录条数上限", json_schema_extra={"label": "去重记录上限"}
    )
    drop_bracket: bool = Field(
        default=True, description="丢弃正文含方括号的指令/红包弹幕", json_schema_extra={"label": "过滤方括号指令弹幕"}
    )
    max_text_len: int = Field(
        default=100, description="弹幕正文最长字符数，0 表示不截断", json_schema_extra={"label": "正文最长字符数"}
    )


class DebugConfig(PluginConfigBase):
    """调试输出配置。"""

    __ui_label__ = "调试输出"
    __ui_icon__ = "scroll-text"
    __ui_order__ = 99

    log_events: bool = Field(
        default=False,
        description="日志逐条打印直播事件",
        json_schema_extra={"label": "逐条打印事件日志"},
    )


class TtsConfig(PluginConfigBase):
    """Breeze TTS 2 配置。"""

    __ui_label__ = "TTS（Breeze TTS 2）"
    __ui_icon__ = "audio-lines"
    __ui_order__ = 10

    enabled: bool = Field(default=True, description="启用 TTS 语音播报", json_schema_extra={"label": "启用语音播报"})
    auto_speak_reply: bool = Field(
        default=True,
        description="自动把机器人的文字回复合成成语音播出来",
        json_schema_extra={
            "label": "自动播报机器人回复",
            "hint": "开启后每条机器人回复都会合成语音并播到虚拟声卡；关闭则完全不播报",
        },
    )
    auto_speak_platforms: str = Field(
        default="live",
        description="自动播报的平台，逗号分隔",
        json_schema_extra={
            "label": "自动播报的平台",
            "hint": "默认 live（直播弹幕适配器）；留空=所有平台；多个用英文逗号分隔",
        },
    )
    auto_speak_max_length: int = Field(
        default=200,
        description="超过这个字数的回复不自动播报",
        json_schema_extra={"label": "自动播报最大字数", "hint": "太长会合成很久，0 表示不限制"},
    )
    server: str = Field(
        default="http://127.0.0.1:9880",
        description="Breeze server 地址",
        json_schema_extra={"label": "服务地址", "hint": "例如 http://127.0.0.1:9880；对应 start-api.bat 里 --port 的那个端口"},
    )
    timeout: float = Field(
        default=180.0,
        description="单次合成请求超时（秒）",
        json_schema_extra={"label": "请求超时（秒）", "hint": "首次合成要加载模型，可适当调大"},
    )
    voice_id: str = Field(
        default="engu",
        description="Breeze 音色名（voices/ 里的 .breeze 文件）",
        json_schema_extra={
            "label": "音色",
            "hint": "填 voices/ 里的文件名（如 engu）；浏览器打开 http://127.0.0.1:9880 的 WebUI 或访问 GET /v1/voices 可查全部音色",
        },
    )
    ref_audio_path: str = Field(
        default="",
        description="参考音频 WAV 路径（填了自动注册成音色）",
        json_schema_extra={"label": "参考音频路径"},
    )
    ref_text: str = Field(
        default="",
        description="参考音频的逐字转写",
        json_schema_extra={
            "label": "参考音频文本",
            "hint": "要和参考音频里说的话一字不差；转写会存进 .breeze，之后不用再传",
            "x-widget": "textarea",
            "rows": 3,
        },
    )
    # 自动注册音色不再是配置项：填了参考音频就注册（见 _ensure_breeze_voice）。
    voice_name: str = Field(
        default="live_voice",
        description="自动注册的音色名",
        json_schema_extra={
            "label": "注册音色名",
            "hint": "注册成功后会写到 voices/<这里>.breeze；"
                    "注册成功后建议把上面的「音色」也改成这个名字（插件会自动同步）",
        },
    )
    cfg_scale: float = Field(
        default=2.5,
        ge=0.0,
        le=4.0,
        description="贴合表演指令的程度；1.0 关闭",
        json_schema_extra={"label": "cfg_scale", "hint": "1.0=关闭引导；想更贴合指令可提到 1.5~2.0"},
    )
    temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
        description="采样温度；0 = 用模型默认",
        json_schema_extra={"label": "温度", "hint": "0=模型默认；越高语气越有变化但越不稳"},
    )
    split_chars: int = Field(
        default=0,
        ge=0,
        le=2000,
        description="长文本切分粒度；0=服务端默认",
        json_schema_extra={"label": "切分粒度", "hint": "0=服务端默认；长句会按句子边界切分后逐段生成"},
    )
    seed: int = Field(
        default=42,
        description="随机种子；直播里固定值可保证音色稳定",
        json_schema_extra={"label": "随机种子", "hint": "固定值让同一句每次音色一致；-1=每次随机"},
    )
    output_device: str = Field(
        default="",
        description="TTS 播放设备；留空自动选虚拟声卡",
        json_schema_extra={
            "label": "TTS 播放设备",
            "hint": "留空=自动：优先虚拟声卡（这样 VTS 才听得到），否则系统默认",
        },
    )
    virtual_cable_device: str = Field(
        default="CABLE Input",
        description="喂给 VTS 口型同步的虚拟声卡关键字",
        json_schema_extra={"label": "虚拟声卡关键字", "hint": "VTS 从 CABLE Output 录音，所以这里填 CABLE Input"},
    )
    # 取消分段打字等待不再是配置项：直播流必须保持 true（见 typing_delay_hook）。
    # 宿主对 typing=True 的分段按 0.3 秒/字 sleep，一句 20 字短句要等 6 秒，
    # 比它的音频还长——第一句播完时第二句还没发出来，门控会短暂打开，
    # Planner 插进来堆对话。这个行为写死，不给关的机会。

    speech_gate_hold_seconds: float = Field(
        default=2.0,
        ge=0.0,
        le=15.0,
        description="整段回复的门控兜底窗口（秒）",
        json_schema_extra={
            "label": "整段门控兜底窗口（秒）",
            "hint": "覆盖宿主分批发送的间隔；只在整段播报期间生效，不会在正常播完后额外拖延",
        },
    )
    buffer_seconds: float = Field(
        default=0.0,
        ge=0.0,
        le=3.0,
        description="开播前预缓冲的秒数",
        json_schema_extra={
            "label": "预缓冲（秒）",
            "hint": "0 = 收到第一块音频就开播（首字最快）；声音断续时加 0.3~0.5 秒换平滑",
        },
    )


class PerformanceConfig(PluginConfigBase):
    """演出引擎：VTube Studio 连接、有机演出与表情道具。"""

    __ui_label__ = "演出引擎"
    __ui_icon__ = "activity"
    __ui_order__ = 11

    # ---- 有机演出引擎 ----
    enabled: bool = Field(
        default=True,
        description="启用有机演出引擎（让模型一直在动）",
        json_schema_extra={"label": "启用演出引擎", "hint": "纯参数驱动：呼吸/眨眼/姿态漂移/手势/视线，不播放待机动画"},
    )
    engine_port: int = Field(
        default=DEFAULT_ENGINE_PORT,
        description="引擎本地控制接口端口",
        json_schema_extra={"label": "引擎控制端口", "hint": "与 VTS 的 8001 错开，默认 18766"},
    )
    python: str = Field(
        default="",
        description="运行引擎的解释器路径，留空用当前解释器",
        json_schema_extra={"label": "引擎解释器路径", "hint": "留空即可"},
    )
    # 启动校准探测不再是配置项：写死关闭。
    # 开启会逐个通道推到极限实测，画面有 6~7 秒不受控的校准动作，期间控制
    # 接口也不响应；默认幅度已够准，没有理由让直播画面抽 7 秒。

    auto_props: bool = Field(
        default=False,
        description="时不时自动掏出模型的道具",
        json_schema_extra={"label": "自动使用道具"},
    )

    # ---- VTube Studio 连接 ----
    auto_discover: bool = Field(
        default=True,
        description="自动发现 VTS 的 host/port",
        json_schema_extra={"label": "自动发现地址", "hint": "端口被占用时插件会自动换用本机网卡 IP"},
    )
    vts_host: str = Field(default="127.0.0.1", description="VTS WebSocket 地址", json_schema_extra={"label": "VTS 地址"})
    vts_port: int = Field(
        default=8001,
        description="VTS WebSocket API 端口",
        json_schema_extra={"label": "VTS 端口", "hint": "VTS 设置里的 API 端口，默认 8001"},
    )
    plugin_name: str = Field(
        default="MaiBot Live Stream Companion",
        description="向 VTS 申请的插件名称",
        json_schema_extra={"label": "插件名称", "hint": "显示在 VTS 授权弹窗里，换名字会要求重新授权"},
    )
    plugin_developer: str = Field(
        default="MaiBot", description="插件开发者名称", json_schema_extra={"label": "开发者名称"}
    )
    auth_token: str = Field(
        default="",
        description="认证 Token，首次授权后自动保存复用",
        json_schema_extra={
            "label": "认证 Token",
            "hint": "留空即可：插件会自动申请并保存到插件数据目录；填了则以这里为准",
        },
    )

    # ---- 表情与道具 ----
    # 自动表情保持时长不再是配置项：永远由引擎按台词计划三段时序（0 的行为）。
    # 手填一个数会让所有表情都挂同一秒数——那正是「一律挂 3 秒」的来源。

    auto_expression: bool = Field(
        default=True,
        description="让 AI 按回复内容自动触发模型表情",
        json_schema_extra={
            "label": "启用自动表情",
            "hint": "只使用当前模型真实存在的表情文件；换模型后自动重新匹配，没有的表情会被跳过",
        },
    )
    emotion_map: str = Field(
        default="",
        description="自定义情绪映射：情绪词=表情名，逗号分隔",
        json_schema_extra={
            "label": "自定义情绪映射",
            "hint": "例如：无语=黑脸,开心=爱心眼；留空则用内置的通用关键字匹配",
        },
    )
    allow_props: bool = Field(
        default=True,
        description="允许 AI 使用道具热键",
        json_schema_extra={"label": "允许使用道具动作", "hint": "这些是 VTS 里的动画热键，模型没有就自动跳过"},
    )
    action_prompt: str = Field(
        default=DEFAULT_PROMPT_TEMPLATE,
        description="注入给模型的动作记号提示词；清空恢复内置默认",
        json_schema_extra={
            "label": "动作记号提示词",
            "hint": "下面是当前正在用的全文，直接改字句就行。四个占位符 "
                    "{small_actions} / {poses} / {expressions} / {props} "
                    "会自动填成当前皮套真有的动作，所以换模型后你的修改依然有效；"
                    "某类动作为空时那一行会被整行删掉。清空 = 恢复内置默认",
            # 插件配置的控件类型来自 x-widget，行数来自 rows（不是宿主配置的
            # x-textarea-rows）。不给这两项会渲染成单行输入框，几百字的提示词
            # 根本没法看。
            "x-widget": "textarea",
            "rows": 14,
        },
    )
    # 对观众的话做反应不再是配置项：写死启用。
    # 「观众说讨厌她→委屈、听人吐槽→讪笑」是直播间的基本表演能力，
    # 走纯参数通道且有节流，没有理由关。

    react_min_interval: float = Field(
        default=2.0, ge=0.0, le=30.0,
        description="两次反应之间至少间隔多少秒",
        json_schema_extra={"label": "反应最小间隔（秒）", "hint": "0=不限；弹幕多时建议 1.5~3 秒"},
    )
    react_hold_seconds: float = Field(
        default=0.0, ge=0.0, le=30.0,
        description="对观众反应的表情保持秒数",
        json_schema_extra={"label": "反应保持时长（秒）", "hint": "0=按情绪（惊讶 1.2s、委屈 3.5s）；短一点更像「听懂了就变脸」"},
    )


class LiveToolsConfig(PluginConfigBase):
    """直播回复轮的工具屏蔽：浏览器/看视频这类重工具会把 Planner 拖到几十秒。"""

    __ui_label__ = "直播工具屏蔽"
    __ui_icon__ = "wrench"
    __ui_order__ = 18

    enabled: bool = Field(
        default=True,
        description="按轮次限制浏览器工具",
        json_schema_extra={
            "label": "限制浏览器工具",
            "hint": "一轮里已经调过浏览器就把 browser_* 拿掉，逼她看完开口；"
                    "新的一轮（下一条弹幕）又能重新上网，不会无限逛",
        },
    )
    max_browser_calls: int = Field(
        default=1, ge=0, le=10,
        description="一轮最多几次浏览器调用；0=不限",
        json_schema_extra={"label": "每轮浏览器调用上限", "hint": "1 = 打开+读一遍就够；0 = 允许一直逛（延迟会飙到几十秒）"},
    )
    blocked: str = Field(
        default="",
        description="额外屏蔽的工具，逗号分隔",
        json_schema_extra={
            "label": "额外屏蔽名单",
            "hint": "默认空：live_sight 陪看、tool_search、game_*、记忆查询全部可用；"
                    "只有你确认某个工具特别慢才填进来（如 browser_ 表示永久不给）",
        },
    )


class ProactiveConfig(PluginConfigBase):
    """主动对话配置。"""

    __ui_label__ = "主动对话"
    __ui_icon__ = "message-circle"
    __ui_order__ = 14

    enabled: bool = Field(
        default=True,
        description="整段语音播完后主动接着自言自语",
        json_schema_extra={"label": "启用主动对话"},
    )
    min_interval_seconds: int = Field(
        default=0,
        ge=0,
        le=120,
        description="播完后停几秒再自言自语",
        json_schema_extra={
            "label": "说完后停顿（秒）",
            "hint": "0 = 说完立刻接下一句；想留白填 2~4",
        },
    )
    followup_turns: int = Field(
        default=PROACTIVE_FOLLOWUP_TURNS,
        ge=0,
        le=10,
        description="回完话后自己再说几轮",
        json_schema_extra={
            "label": "回完话后自说几轮",
            "hint": "想让她一直说下去就调大；0 = 回完就安静",
        },
    )
    inflight_seconds: int = Field(
        default=8,
        ge=2,
        le=120,
        description="忙窗口兜底（秒）",
        json_schema_extra={
            "label": "忙窗口（秒）",
            "hint": "防任务卡死占位的兜底；别设太小",
        },
    )
    isolation_seconds: int = Field(
        default=20,
        ge=10,
        le=300,
        description="主动开口后隐藏直播消息正文的窗口（秒）",
        json_schema_extra={
            "label": "上下文隔离窗口（秒）",
            "hint": "开口后隐藏弹幕/礼物正文，出站即解除；此为卡住时的兜底超时",
        },
    )
    game_events: bool = Field(
        default=True,
        description="把正在玩的游戏当主动对话素材",
        json_schema_extra={"label": "游戏解说素材"},
    )
    game_hub_url: str = Field(
        default="http://127.0.0.1:8787",
        description="game-hub 服务地址",
        json_schema_extra={"label": "游戏服务地址", "hint": "游戏插件默认监听 8787；改过端口就填新的"},
    )
    event_port: int = Field(
        default=5687,
        ge=1024,
        le=65535,
        description="游戏事件接收端口",
        json_schema_extra={"label": "事件接收端口"},
    )


class ReplyConfig(PluginConfigBase):
    """回复弹幕。"""

    __ui_label__ = "回复弹幕"
    __ui_icon__ = "gauge"
    __ui_order__ = 4

    enabled: bool = Field(
        default=True,
        description="压低弹幕触发的回复频率",
        json_schema_extra={"label": "启用弹幕回复节流"},
    )
    danmaku_frequency: float = Field(
        default=0.08,
        ge=0.05,
        le=1.0,
        description="放行弹幕回复的比例",
        json_schema_extra={
            "label": "弹幕回复比例",
            "hint": "越小越少回弹幕、越多自言自语；建议 0.2~0.4。这个值与 WebUI 聊天流里的频率滑块是同一个倍率，插件会接管直播流的它",
        },
    )
    recheck_seconds: int = Field(
        default=10,
        ge=3,
        le=120,
        description="频率倍率校验间隔（秒）",
        json_schema_extra={"label": "倍率校验间隔（秒）"},
    )
    end_turn_after_reply: bool = Field(
        default=True,
        description="说完一轮就结束本轮思考，不续跑 Planner",
        json_schema_extra={"label": "说完即收口（不续跑 Planner）"},
    )


class SubtitleConfig(PluginConfigBase):
    """字幕层配置：给 OBS 浏览器源用的那个小页面。"""

    __ui_label__ = "字幕"
    __ui_icon__ = "type"
    __ui_order__ = 15

    enabled: bool = Field(
        default=True,
        description="本地字幕页，OBS 浏览器源",
        json_schema_extra={
            "label": "启用字幕",
            "hint": "页面地址 http://127.0.0.1:5679/ ，透明背景+底部居中，跟着语音一句一换",
        },
    )
    host: str = Field(
        default="127.0.0.1",
        description="监听地址，默认只给本机",
        json_schema_extra={"label": "监听地址", "hint": "OBS 在另一台机器上时改成 0.0.0.0，再用本机 IP 打开"},
    )
    port: int = Field(
        default=5679,
        ge=1,
        le=65535,
        description="字幕页面端口",
        json_schema_extra={"label": "端口", "hint": "默认 5679；被占用时换一个，OBS 地址跟着改"},
    )
    font_size: int = Field(
        default=42,
        ge=12,
        le=120,
        description="字号（px）",
        json_schema_extra={"label": "字号（px）", "hint": "按 OBS 画布大小调，1080p 下 40~56 比较合适"},
    )
    text_color: str = Field(
        default="#ffffff",
        description="文字颜色",
        json_schema_extra={"label": "文字颜色", "hint": "CSS 颜色值，例如 #ffffff 或 #ffe9a8"},
    )
    background: str = Field(
        default="transparent",
        description="文字底色；默认透明（只留文字+描边阴影）",
        json_schema_extra={
            "label": "文字底色",
            "hint": "默认 transparent = 纯字幕，没有气泡底；想要半透明黑底就填 rgba(0, 0, 0, 0.45)",
        },
    )
    hold_seconds: int = Field(
        default=0,
        ge=0,
        le=60,
        description="字幕播完后停留的秒数",
        json_schema_extra={"label": "播完后停留（秒）"},
    )
    max_chars: int = Field(
        default=80,
        ge=10,
        le=300,
        description="单条字幕最多多少字，超出截断",
        json_schema_extra={"label": "单条字数上限", "hint": "太长会把画面撑爆，超出的用省略号截断"},
    )


class DanmakuConfig(PluginConfigBase):
    """弹幕墙配置：OBS 浏览器源打开的第二个本地页面。"""

    __ui_label__ = "弹幕墙"
    __ui_icon__ = "message-square"
    __ui_order__ = 16

    enabled: bool = Field(
        default=True,
        description="把弹幕推到本地网页给 OBS",
        json_schema_extra={
            "label": "启用弹幕墙",
            "hint": "页面地址 http://127.0.0.1:5686/ ，右侧竖排、每条停留几秒后淡出",
        },
    )
    host: str = Field(
        default="127.0.0.1",
        description="监听地址，默认只给本机（OBS 同机即可）",
        json_schema_extra={"label": "监听地址"},
    )
    port: int = Field(default=5686, ge=1, le=65535, description="弹幕墙端口", json_schema_extra={"label": "端口"})
    max_lines: int = Field(
        default=12,
        ge=3,
        le=40,
        description="同时显示几条弹幕",
        json_schema_extra={"label": "同屏条数", "hint": "超出的从最旧的挤掉"},
    )
    hold_seconds: int = Field(
        default=12, ge=2, le=120, description="每条弹幕停留多少秒后淡出", json_schema_extra={"label": "停留（秒）"}
    )
    font_size: int = Field(
        default=22, ge=12, le=64, description="字号（px）", json_schema_extra={"label": "字号（px）"}
    )
    text_color: str = Field(default="#ffffff", description="正文颜色", json_schema_extra={"label": "正文颜色"})
    name_color: str = Field(default="#8fd3ff", description="昵称颜色", json_schema_extra={"label": "昵称颜色"})
    background: str = Field(
        default="transparent",
        description="面板底色，默认透明",
        json_schema_extra={"label": "面板底色", "hint": "transparent = 纯文字；想要深色底填 rgba(0, 0, 0, 0.35)"},
    )


class PeriodConfig(PluginConfigBase):
    """单个时段。"""

    start: str = Field(
        default="08:00",
        description="开始时刻 HH:MM",
        json_schema_extra={"label": "开始时刻", "hint": "24 小时制，如 08:00"},
    )
    name: str = Field(
        default="",
        description="档位名",
        json_schema_extra={"label": "档位名", "hint": "如「晚间游戏档」"},
    )
    focus: str = Field(
        default="",
        description="这一档该干什么",
        json_schema_extra={"label": "这一档做什么", "hint": "原样进入上下文，写具体一点"},
    )


class ScheduleConfig(PluginConfigBase):
    """按时段规定她该干什么，挂钟到点自动换挡。

    时段表是硬性安排（整段注入 Planner 与 Replyer）；换挡到点直接排一轮主动开口。
    """

    __ui_label__ = "日程表"
    __ui_icon__ = "calendar"
    # 紧跟「抖音」：日程属于"直播间安排"，和平台链路放在一起；后面的
    # 直播事件/防刷屏顺延为 4/5，调试输出(6)及之后不动。
    __ui_order__ = 3

    enabled: bool = Field(
        default=True,
        description="启用日程表",
        json_schema_extra={"label": "启用日程表", "hint": "关掉后不注入时段表，也不换挡"},
    )
    periods: List[PeriodConfig] = Field(
        default_factory=list,
        description="时段表",
        json_schema_extra={"label": "时段表", "hint": "按开始时刻升序；写清每档她该干什么"},
    )
    fallback: str = Field(
        default="",
        description="没有任何时段命中时的兜底提示",
        json_schema_extra={"label": "兜底提示", "hint": "可留空"},
    )
    inject_planner: bool = Field(
        default=True,
        description="往 Planner 请求里注入时段表",
        json_schema_extra={"label": "注入 Planner", "hint": "规划阶段也能看到安排"},
    )
    inject_replyer: bool = Field(
        default=True,
        description="往 Replyer 请求里注入时段表",
        json_schema_extra={"label": "注入 Replyer", "hint": "说话阶段也受日程约束"},
    )
    trigger_on_change: bool = Field(
        default=True,
        description="换挡时立刻主动开口",
        json_schema_extra={
            "label": "换挡主动开口",
            "hint": "到点直接排一轮主动对话；关掉则日程只在已有对话里生效",
        },
    )
    trigger_on_load: bool = Field(
        default=True,
        description="插件载入时按当前档位开口一次",
        json_schema_extra={
            "label": "启动即按当前档",
            "hint": "重启后不用等到下一个边界，直接按现在这一档行动",
        },
    )


class AsrConfig(PluginConfigBase):
    """ASR 语音对话配置：按住热键说话，松开转写发给 AI。"""

    __ui_label__ = "语音对话（ASR）"
    __ui_icon__ = "mic"
    __ui_order__ = 17

    enabled: bool = Field(
        default=True,
        description="按住热键说话，AI 回复并语音播报",
        json_schema_extra={"label": "启用语音对话", "hint": "AI 说话时按下热键会先打断她的语音再录音"},
    )
    api_key: str = Field(
        default="",
        description="SiliconFlow API Key",
        json_schema_extra={"label": "SiliconFlow API Key"},
    )
    base_url: str = Field(
        default="https://api.siliconflow.cn/v1",
        description="转写接口根地址",
        json_schema_extra={"label": "接口地址"},
    )
    model: str = Field(
        default="Qwen/Qwen3-ASR-1.7B",
        description="ASR 转写模型",
        json_schema_extra={
            "label": "转写模型",
            "hint": "可选：SenseVoiceSmall / XingChenASR-V3.2 等，Qwen3-ASR-1.7B 综合最快",
        },
    )
    hotkey: str = Field(
        default="f9",
        description="全局热键键名：按住它说话，松开发送",
        json_schema_extra={
            "label": "录音热键",
            "hint": "keyboard 库的键名，如 f9 / f2 / ctrl+shift+s 用 press 模式不适用，建议单键",
        },
    )
    user_name: str = Field(
        default="",
        description="你的话在聊天流里的显示昵称",
        json_schema_extra={
            "label": "昵称",
            "hint": "可填你在本地聊天室用的同一个昵称，配合人格与长期记忆，AI 会把你当同一个人；注意直播流与本地聊天室的短期上下文是分开的",
        },
    )
    device: str = Field(
        default="",
        description="录音麦克风：设备名或索引；留空用系统默认",
        json_schema_extra={"label": "录音设备", "hint": "留空=系统默认麦克风"},
    )
    min_duration: float = Field(
        default=0.3,
        ge=0.0,
        le=10.0,
        description="最短录音秒数（误触保护）",
        json_schema_extra={"label": "最短录音（秒）"},
    )
    max_duration: int = Field(
        default=30,
        ge=1,
        le=300,
        description="单次录音最长秒数，超出的部分截断",
        json_schema_extra={"label": "最长录音（秒）"},
    )
    bypass_planner: bool = Field(
        default=True,
        description="语音由 Replyer 直答，不经过 Planner",
        json_schema_extra={
            "label": "语音绕过 Planner（快速应答）",
            "hint": "省掉 Planner 的决策与工具调用",
        },
    )
    voice_reply_hint: str = Field(
        default="",
        description="快速通道回复要求；留空用内置",
        json_schema_extra={
            "label": "快速通道回复要求",
            "hint": "留空=「像在直播间顺口接话，一到两句，口语，不要长篇」。"
                    "想让她更简短或更有情绪时在这里改",
            "x-widget": "textarea",
            "rows": 4,
        },
    )


class LiveDanmakuConfig(PluginConfigBase):
    """直播适配器总配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig, json_schema_extra={"label": "插件"})
    bilibili: BilibiliConfig = Field(default_factory=BilibiliConfig, json_schema_extra={"label": "B站"})
    douyin: DouyinConfig = Field(default_factory=DouyinConfig, json_schema_extra={"label": "抖音"})
    events: EventsConfig = Field(default_factory=EventsConfig, json_schema_extra={"label": "直播事件"})
    filter: FilterConfig = Field(default_factory=FilterConfig, json_schema_extra={"label": "防刷屏"})
    # 「通道」不再是配置节：B 站与抖音合并进 live-all 已写死（见
    # CHANNEL_MERGE_PLATFORMS / CHANNEL_GROUP_ID / CHANNEL_GROUP_NAME）。

    debug: DebugConfig = Field(default_factory=DebugConfig, json_schema_extra={"label": "调试输出"})
    tts: TtsConfig = Field(default_factory=TtsConfig, json_schema_extra={"label": "TTS（Breeze TTS 2）"})
    performance: PerformanceConfig = Field(
        default_factory=PerformanceConfig, json_schema_extra={"label": "有机演出引擎"}
    )
    live_tools: LiveToolsConfig = Field(default_factory=LiveToolsConfig, json_schema_extra={"label": "直播工具屏蔽"})
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig, json_schema_extra={"label": "主动对话"})
    reply: ReplyConfig = Field(default_factory=ReplyConfig, json_schema_extra={"label": "弹幕回复节流"})
    subtitle: SubtitleConfig = Field(default_factory=SubtitleConfig, json_schema_extra={"label": "字幕"})
    danmaku: DanmakuConfig = Field(default_factory=DanmakuConfig, json_schema_extra={"label": "弹幕墙"})
    asr: AsrConfig = Field(default_factory=AsrConfig, json_schema_extra={"label": "语音对话（ASR）"})
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig, json_schema_extra={"label": "日程表"})


# ==================================================================== #
# 主插件
# ==================================================================== #


class LiveDanmakuAdapter(MaiBotPlugin):
    """直播适配器：B 站/抖音弹幕接入 + TTS 播报 + VTS 演出引擎 + 字幕/弹幕墙。"""

    config_model = LiveDanmakuConfig

    def __init__(self) -> None:
        """初始化插件状态。"""

        super().__init__()
        # --- 弹幕接入（输入） ---
        self._channel: Optional[LiveChannel] = None
        self._bilibili: Optional[BilibiliLiveLink] = None
        self._douyin: Optional[DouyinLiveLink] = None
        # 出站方向「不回发弹幕」的说明只打一次，别每条回复都刷
        self._outbound_notice_logged = False

        # --- 表现层（输出） ---
        self._tts_client: Optional[BreezeClient] = None
        self._player: Optional[AudioPlayer] = None
        self._vts_client: Optional[VTSClient] = None
        self._engine: Optional[PerformanceEngineManager] = None
        # VTS 后台重连：先开 MaiBot 后开 VTS 也能自动连上，断线也能自动恢复
        self._vts_reconnect_task: Optional[asyncio.Task[None]] = None
        self._speak_lock = asyncio.Lock()
        # 断句后的多条短句按到达顺序排队播报，避免并发抢字幕
        self._reply_queue_lock = asyncio.Lock()
        # 语音播放期间挡住新的入站消息，避免她在说话时又被触发规划
        self._speaking = asyncio.Event()
        # 宿主开启回复断句后，一条回复会拆成多条消息分别送到 auto_speak_hook。
        # 这个计数表示「还有多少条短句没播完」：整段播完前都算她在说话，
        # 句间不留空隙给 Planner 插队，主动对话也只在整段结束后触发一次。
        self._speech_pending = 0
        # 代次：主播按热键打断时 +1，排队中的旧句子直接作废，不再念出来
        self._speech_generation = 0
        # 整段回复门控的兜底窗口（monotonic 时间戳）：每条短句入队时续期，
        # 最后一句播完立即清零。覆盖宿主分批发送短句的间隔，保证从第一条
        # 开口到最后一条播完，中途没有任何一帧让 Planner 插进来。
        self._speech_gate_until = 0.0
        # 「本轮只有自己刚说的话」的空转轮：即使模型没听话、仍吐出一句
        # 接自己话的文本，也不播出、不上字幕。由 Planner 请求前 Hook 续期，
        # 下一轮有观众消息或主动任务时自动清零。
        self._self_echo_suppress_until = 0.0
        # 播报任务引用：热重载/卸载时取消，别让旧实例继续念旧台词
        self._reply_tasks: set[asyncio.Task[None]] = set()
        # 本次运行内有效且在用的 VTS Token（避免重复申请导致旧 Token 被吊销）
        self._active_token = ""
        # 主动对话
        self._proactive: Optional[ProactiveDirector] = None
        # 事件驱动的单次开口任务：同一时刻最多一个，避免事件堆成队列
        self._proactive_kick_task: Optional[asyncio.Task[None]] = None
        # 游戏解说素材（由 game-hub 推送更新；关闭时为 None）
        self._game_feed: Optional[GameFeed] = None
        # 日程推来的「当前档该做的事」：换挡/载入事件都会更新它。
        # 它是主动轮意图的素材，不进弹幕上下文、也不改写 system 人格。
        self._schedule_focus = ""
        # 日程（从 schedule-hub 合并）：时段表、挂钟定时器与换挡标记。
        # 合并前换挡靠 HTTP 自环（POST /schedule-event），现在直连内部方法。
        self._schedule_table: Optional[ScheduleTable] = None
        self._schedule_task: Optional[asyncio.Task[None]] = None
        self._schedule_last_period = ""
        self._schedule_pending_change = ""
        # 视线超时任务（cortico 式 gaze 状态机）：新视线记号会取消重挂
        self._gaze_reset_task: Optional[asyncio.Task[None]] = None
        # 语气记号（voice direction）：记号钩子每条消息先清空、摘到才设置，
        # speak() 取走后生效于那一句的合成（breeze 的 instruction 覆盖）。
        self._pending_direction: Optional[str] = None
        # 接收游戏/日程事件的本地 HTTP 服务（只听回环）
        self._event_inbox: Optional[GameEventInbox] = None
        # 上一条主动任务什么时候算「忙完」：期间不再叠加新任务
        self._proactive_inflight_until = 0.0
        # 节奏参数：默认值来自模块常量，_start_proactive 时会用配置覆盖
        self._proactive_tick_seconds = PROACTIVE_TICK_SECONDS
        self._proactive_inflight_seconds = PROACTIVE_INFLIGHT_SECONDS
        self._last_stream_id = ""
        self._proactive_said = 0
        # 主动轮上下文隔离：session_id -> 到期时间。触发主动任务时置位，
        # 机器人一发出消息就解除；这里的时间只是防卡死的兜底超时。
        self._proactive_isolation: Dict[str, float] = {}
        # 本次主动任务里已经跑了多少步浏览器工具链：到上限就收口，
        # 否则模型会在「搜 -> 点视频 -> 再搜」里无限逛，一直不开口。
        self._proactive_browser_calls = 0
        # 本插件刚登记的主动任务 id：用来把「主动轮」和后续弹幕回复轮精确区分开
        self._proactive_task_ids: set[str] = set()
        # 最近一次主动任务的 id：主动轮 reply 找不到目标消息时用它当锚点
        self._proactive_last_task_id = ""
        # 本轮上下文里最后一条真实消息的 msg_id（规划请求前记录）
        self._live_anchor_msg_id = ""
        # 已经讲过的游戏事件版本号：只有更大的版本才是「新事件」，避免播完空转
        self._consumed_game_version = 0
        # 「回完话接着自己找话题」的剩余额度：整段回复（回应观众）播完后才发，
        # 自语段不再续额度，链长有上限；0 表示播完就安静。
        self._followup_turns_left = 0
        # 当前这次主动轮是不是「续聊」轮：只影响尾部规则文案，出站/解除隔离即清
        self._proactive_followup = False
        # 动作词表快照（演出引擎按当前皮套写好的 vts/actions.json）：(mtime, payload)
        self._actions_cache: tuple[float, Dict[str, Any]] | None = None
        # 直播流 session_id 集合：从自己注入的消息里学到的（插入序淘汰，防无界增长）
        self._live_stream_ids: Dict[str, None] = {}
        # 弹幕回复节流：把直播聊天流的频率倍率压低，后台巡检保证宿主重建后能自愈
        self._reply_adjust_task: Optional[asyncio.Task[None]] = None
        self._reply_adjust_checked_at = 0.0
        self._reply_adjust_dirty = False
        self._reply_adjust_warned = False
        # 字幕层：本地 HTTP 页面，OBS 用浏览器源打开
        self._subtitle: Optional[SubtitleServer] = None
        # 每个字大约占多少秒音频：用来把「已播到第几秒」换算成「说到第几个字」。
        # 每次播完用真实时长校准一次，避免语速变化后字幕越跑越偏。
        self._subtitle_seconds_per_char = 0.22
        # 弹幕墙：另一个本地 HTTP 页面，显示观众弹幕
        self._danmaku: Optional[DanmakuServer] = None
        # ASR 语音对话：热键录音 → 转写 → 聊天流
        self._asr: Optional[SiliconFlowASR] = None
        self._asr_hotkey: Optional[AsrHotkey] = None
        self._mic: Optional[MicRecorder] = None
        self._asr_loop: Optional[asyncio.AbstractEventLoop] = None
        # 语音快速通道的滚动上下文：Replyer 那条链路拿到的是空 chat_history，
        # 不自己拼最近几轮，她每句话都会像失忆重来。
        self._voice_history: "deque[Tuple[str, str]]" = deque(maxlen=6)
        # 快速通道的串行锁：上一次还没答完又按下热键时，新的一句排队而不是打架
        self._voice_lock = asyncio.Lock()
        # 按热键打断当前 TTS 播报的事件：speak() 在合成流与收尾等待里检查
        self._speech_interrupt = asyncio.Event()
        # 字幕序号：用来判断「延迟清除」时这句是不是已经被下一句替换了
        self._subtitle_token = 0
        self._stats: Dict[str, Any] = {
            "spoken": 0,
            "tts_errors": 0,
            "last_spoken": "",
            # 上一句音频的真实时长（秒）；字幕持续时长与它严格相等
            "last_audio_seconds": 0.0,
            "vts_host": "",
            "vts_port": 0,
        }

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def on_load(self) -> None:
        """启动通道、链路，并准备 TTS/VTS/引擎/字幕/弹幕墙。"""

        # 热重载复用同一个实例时，别把上一轮的排队计数与门控带进来
        self._speech_pending = 0
        self._speech_generation += 1
        self._speech_gate_until = 0.0
        self._speaking.clear()
        # 上一轮留下的续聊额度属于旧实例的行为，不带到重载后的新一轮
        self._followup_turns_left = 0
        self._proactive_followup = False
        # 观众反应表情的节流状态
        self._last_reaction_at = 0.0
        self._last_reaction_emotion = ""
        # 延迟埋点：最近一条观众消息进聊天流的时刻
        self._last_inbound_at = 0.0

        # 弹幕接入
        channel = LiveChannel(self.ctx, GATEWAY_NAME)
        channel.register_source("bilibili", "B站")
        channel.register_source("douyin", "抖音")
        # 听到观众说话就做反应表情（不是等自己回复时才演）
        channel.set_inbound_hook(self._react_to_chat)
        self._channel = channel
        await self._start_links()

        # 表现层
        config = self.config
        self._tts_client = BreezeClient(self._breeze_params())
        # 参考音频 → 保存音色：后台注册，不阻塞插件加载（服务可能还没起）
        asyncio.create_task(self._ensure_breeze_voice(), name="live-breeze-voice")
        self._player = AudioPlayer(self._effective_device(), logger=logger)
        # VTS 连接与演出引擎都交给后台重连任务：VTS 授权等待最长 90 秒，
        # 若同步 await 在 on_load 里，插件初始化期间所有 hook 都会报
        # E_PLUGIN_NOT_FOUND（插件已注册但未就绪）。后台 loop 首次立即尝试连接，
        # 连上后由它补启动演出引擎。
        self._start_vts_reconnect()
        # 演出引擎不再在这里启动：交给后台 loop 在连上 VTS 后统一启动。
        # 否则 on_load 与后台 loop 会并发各启一个引擎，多引擎同时注入 VTS
        # 互相覆盖，表现就是模型抽搐。
        if config.proactive.enabled:
            self._start_proactive()
        # 弹幕回复节流：独立巡检，不依赖主动对话是否开启
        self._start_reply_adjust()
        await self._start_subtitle()
        await self._start_danmaku()
        # ASR 语音对话：需要事件循环引用（热键回调从 keyboard 线程投递回来）
        self._asr_loop = asyncio.get_running_loop()
        if config.asr.enabled:
            self._start_asr()
        # 日程：建时段表、挂边界、按当前档对齐（从 schedule-hub 合并）
        await self._start_schedule()

        logger.info(
            "[直播] 插件已加载：弹幕=%s，TTS=%s，演出引擎=%s，VTS=%s，主动对话=%s，字幕=%s，弹幕墙=%s，语音对话=%s，日程=%s",
            bool(self._channel),
            config.tts.enabled,
            config.performance.enabled,
            bool(self._vts_client),
            config.proactive.enabled,
            config.subtitle.enabled,
            config.danmaku.enabled,
            bool(self._asr_hotkey),
            self._schedule_table is not None,
        )

    async def on_unload(self) -> None:
        """停止全部链路与表现层，释放连接。"""

        # 弹幕接入
        await self._stop_links()
        channel, self._channel = self._channel, None
        if channel is not None:
            await channel.stop()

        # 表现层
        await self._stop_proactive()
        await self._stop_schedule()
        await self._stop_reply_adjust()
        await self._stop_vts_reconnect()
        await self._stop_subtitle()
        await self._stop_danmaku()
        await self._stop_asr()

        # 视线超时任务（有新视线记号就会重挂，卸载时收掉）
        gaze_task, self._gaze_reset_task = self._gaze_reset_task, None
        if gaze_task is not None and not gaze_task.done():
            gaze_task.cancel()

        # 取消还在排队/播放的旧播报：热重载复用实例时，旧任务的 finally
        # 会把 _speaking 清掉，必须在 on_load 之前全部收干净。
        self._speech_generation += 1
        tasks = list(self._reply_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reply_tasks.clear()
        self._speech_pending = 0
        self._speech_gate_until = 0.0
        self._speaking.clear()
        self._followup_turns_left = 0
        self._proactive_followup = False

        engine, self._engine = self._engine, None
        if engine is not None:
            await engine.stop()

        vts, self._vts_client = self._vts_client, None
        if vts is not None:
            try:
                await vts.disconnect()
            except Exception as exc:
                logger.debug(f"[VTS] 断开连接失败：{exc}")

        tts, self._tts_client = self._tts_client, None
        if tts is not None:
            await tts.close()

        player, self._player = self._player, None
        if player is not None:
            await player.close()

        logger.info("[直播] 插件已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新：链路重连 + TTS 参数原地更新 + VTS/引擎按需重建。

        Args:
            scope: 配置变更范围。
            config_data: 最新配置数据。
            version: 配置版本号。
        """

        del config_data
        del version
        if scope != "self":
            return

        config = self.config

        # 弹幕接入：房间号/Cookie 变化必须重连，事件开关与过滤参数可以原地生效
        channel = self._channel
        if channel is not None:
            channel.apply_config(*self._channel_settings())
            await self._stop_links()
            await self._start_links()

        # 表现层：TTS 参数原地更新，VTS 与引擎按需重建
        if self._tts_client is not None:
            self._tts_client.update(self._tts_params())
        if self._player is not None:
            self._player.set_device(self._effective_device())

        await self._connect_vts()
        self._start_vts_reconnect()

        engine, self._engine = self._engine, None
        if engine is not None:
            await engine.stop()
        if config.performance.enabled:
            await self._start_engine()

        await self._stop_proactive()
        if config.proactive.enabled:
            self._start_proactive()
        # 频率倍率可能变了（开关/比例），下一轮巡检立刻按新值重算
        self._reply_adjust_dirty = True

        # 字幕/弹幕墙：已运行就原地更新渲染参数，不重启。
        # 热重载时 on_config_update 与 on_load 会先后触发，若这里 stop+start，
        # 旧 aiohttp 服务可能没释放端口、新服务 bind 失败，导致 self._danmaku 变 None、
        # 弹幕永远推不进去（之前「抖音弹幕不显示」的根因）。原地更新即可避开这个竞态。
        await self._refresh_subtitle()
        await self._refresh_danmaku()

        # ASR：热键/模型/密钥变化直接重建（录音与热键本身没有需要原地更新的状态）
        await self._stop_asr()
        if config.asr.enabled:
            self._start_asr()

        # 日程：时段表变化重建（挂钟定时器重挂）
        await self._reload_schedule()

        logger.info("[直播] 配置已热更新")

    # ------------------------------------------------------------------ #
    # 弹幕接入（输入）：消息网关
    # ------------------------------------------------------------------ #

    @MessageGateway(
        "duplex",
        name=GATEWAY_NAME,
        description="B站/抖音直播弹幕对称网关：入站注入弹幕，出站接住 AI 回复",
        platform=CHANNEL_PLATFORM,
        protocol="live_danmaku",
    )
    async def live_gateway(
        self,
        message: Dict[str, Any] | None = None,
        route: Dict[str, Any] | None = None,
        metadata: Dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """出站方向：AI 回复落到 live 平台时由这里接住。

        直播弹幕的发送受平台风控与频率限制（B 站还要登录态），所以这条网关
        **按设计不回发弹幕**：回复只写入聊天流，语音由本插件的 TTS 播出去、
        画面由字幕层读，跟聊天流里的文字是同一份内容。
        这里只把文本记到 debug，避免把「正常行为」写成看起来像失败的日志。
        """

        del route
        del metadata
        del kwargs

        if message is None:
            return {"success": False, "error": "缺少 message 参数"}
        text = _extract_plain_text(message)
        if not self._outbound_notice_logged:
            self._outbound_notice_logged = True
            self.ctx.logger.info(
                "[live] 出站方向：AI 回复不回发弹幕（平台风控/频率限制），只写入聊天流供 TTS 与字幕层使用"
            )
        self.ctx.logger.debug(f"[live] AI 回复已写入聊天流：{text[:80]}")
        return {"success": True, "external_message_id": str(message.get("message_id") or "")}

    # ------------------------------------------------------------------ #
    # 弹幕接入（输入）：链路管理
    # ------------------------------------------------------------------ #

    def _channel_settings(self) -> tuple[ChannelSettings, Dict[str, bool], bool]:
        """把插件配置翻译成通道参数。"""

        config = self.config
        settings = ChannelSettings(
            merge_platforms=CHANNEL_MERGE_PLATFORMS,
            group_id=CHANNEL_GROUP_ID,
            group_name=CHANNEL_GROUP_NAME,
        )
        enabled_kinds = {
            KIND_DANMAKU: config.events.danmaku,
            KIND_SUPER_CHAT: config.events.super_chat,
            KIND_GIFT: config.events.gift,
            KIND_GUARD: config.events.guard,
        }
        return settings, enabled_kinds, config.debug.log_events

    async def _start_links(self) -> None:
        """按配置启动 B 站与抖音链路。"""

        channel = self._channel
        if channel is None:
            return
        config = self.config
        settings, enabled_kinds, log_events = self._channel_settings()
        channel.configure_filter(
            config.filter.dedup_window_s,
            config.filter.dedup_max_entries,
            config.filter.drop_bracket,
            config.filter.max_text_len,
        )
        await channel.start(settings, enabled_kinds, log_events)

        # 先上报就绪：Host 只接受来自已就绪网关的入站消息
        await channel.set_ready(
            True,
            {
                "bilibili_room": str(config.bilibili.room_id),
                "douyin_room": config.douyin.web_rid.strip(),
            },
        )

        if config.bilibili.enabled and config.bilibili.room_id > 0:
            self._bilibili = BilibiliLiveLink(channel, config.bilibili.room_id, config.bilibili.sessdata)
            await self._bilibili.start()
        elif config.bilibili.enabled:
            self.ctx.logger.warning("[live] B站已启用但未填写有效房间号，B站链路未启动")

        if config.douyin.enabled and config.douyin.web_rid.strip():
            self._douyin = DouyinLiveLink(channel, config.douyin.web_rid, config.douyin.extra_cookie)
            await self._douyin.start()
        elif config.douyin.enabled:
            self.ctx.logger.warning("[live] 抖音已启用但未填写直播间号，抖音链路未启动")

    async def _stop_links(self) -> None:
        """停止 B 站与抖音链路。"""

        bilibili, self._bilibili = self._bilibili, None
        douyin, self._douyin = self._douyin, None
        if bilibili is not None:
            await bilibili.stop()
        if douyin is not None:
            await douyin.stop()

    # ------------------------------------------------------------------ #
    # 播放设备
    # ------------------------------------------------------------------ #

    def _effective_device(self) -> str:
        """实际播放设备：配置 > VTS 监听的虚拟声卡 > 系统默认。"""

        configured = self.config.tts.output_device.strip()
        if configured:
            return configured
        cable = find_virtual_cable_output(self.config.tts.virtual_cable_device)
        return cable or ""

    # ------------------------------------------------------------------ #
    # VTS Token 持久化
    # ------------------------------------------------------------------ #

    def _token_store_path(self) -> Any:
        """VTS Token 的保存位置（插件数据目录下的 vts_token.json）。"""

        return self.ctx.paths.data_dir / "vts_token.json"

    def _load_saved_token(self) -> str:
        """读取自动保存的 VTS Token；读不到返回空串。"""

        try:
            path = self._token_store_path()
        except (AttributeError, RuntimeError):
            return ""
        if not path.exists():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"[VTS] 读取已保存的 Token 失败：{exc}")
            return ""
        return str(data.get("token", "") if isinstance(data, dict) else "").strip()

    def _save_token(self, token: str) -> None:
        """把 Token 存到插件数据目录。"""

        path = self._token_store_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"token": token}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(f"[VTS] Token 已保存到 {path}，下次加载自动复用")
        except Exception as exc:
            logger.warning(f"[VTS] 保存 Token 失败：{exc}")

    def _effective_token(self) -> str:
        """实际使用的 Token：内存（刚授权拿到的） > 配置 > 插件数据目录保存的。"""

        active = getattr(self, "_active_token", "").strip()
        if active:
            return active
        configured = self.config.performance.auth_token.strip()
        return configured or self._load_saved_token()

    # ------------------------------------------------------------------ #
    # 主动对话
    # ------------------------------------------------------------------ #

    def _start_proactive(self) -> None:
        """创建主动对话导演；不启动任何定时器，改为等事件。"""

        config = self.config.proactive
        self._proactive_inflight_seconds = float(max(2, int(config.inflight_seconds)))
        self._proactive = ProactiveDirector(
            min_interval_seconds=config.min_interval_seconds,
        )
        # 事件端口同时收「游戏推进」和「日程换挡」，所以即使关掉游戏素材也
        # 要监听：否则日程事件没有入口，AI 永远不知道当前档该做什么。
        self._game_feed = GameFeed() if config.game_events else None
        if self._event_inbox is None:
            self._event_inbox = GameEventInbox(
                port=config.event_port,
                on_event=self._on_game_event,
                logger=logger,
            )
            asyncio.create_task(self._start_event_inbox(), name="live-game-inbox")
        logger.info(
            "[主动对话] 已启用（纯事件驱动，无定时器）：说完后停顿 %s，忙窗口 %ss（仅异常兜底），事件端口 :%s，游戏素材 %s",
            f"{config.min_interval_seconds} 秒" if config.min_interval_seconds > 0 else "0 秒（说完就接）",
            self._proactive_inflight_seconds,
            config.event_port,
            "开" if config.game_events else "关",
        )

    async def _start_event_inbox(self) -> None:
        """启动游戏事件接收服务（失败只记日志，不影响主动对话）。"""

        inbox = self._event_inbox
        if inbox is None:
            return
        try:
            await inbox.start()
        except Exception as exc:  # noqa: BLE001 - 端口占用不该拖垮插件
            logger.warning(f"[游戏事件] 监听失败（游戏素材将不可用）：{type(exc).__name__}: {exc}")

    def _on_game_event(self, payload: Dict[str, Any]) -> None:
        """收到一条事件：日程换挡记住 focus，游戏推进更新素材，都排一次开口。"""

        if str(payload.get("kind") or "").strip() == "schedule":
            # 关键：把「这一档该做什么」留下来当主动轮的意图。丢掉它的话，
            # 日程只剩 system 里一句约束文字，模型会继续讲抽象小故事、从不
            # 去调用浏览器/看视频工具 —— 这就是「不会自己打开网页看动漫」。
            focus = str(payload.get("focus") or "").strip()
            if focus:
                self._schedule_focus = focus
            feed = self._game_feed
            if feed is not None:
                # 日程不覆盖游戏素材（否则正在解说的游戏会凭空消失）；
                # 只把「有新事」记进版本，播完被挡住时仍能补触发。
                feed.note_external_event()
            logger.info(
                "[主动对话] 日程事件：%s（%s）",
                str(payload.get("name") or "").strip() or "当前档",
                focus or "无 focus",
            )
            self._schedule_proactive("日程换挡")
            return
        feed = self._game_feed
        if feed is None:
            return
        if not feed.update(payload):
            return
        # 只做「排一次」，不在这里等回复：事件可能比播报快得多
        self._schedule_proactive("游戏推进")

    async def _stop_proactive(self) -> None:
        """停掉主动对话相关资源。"""

        self._proactive = None
        self._proactive_isolation.clear()
        self._proactive_task_ids.clear()
        self._game_feed = None

    # ------------------------------------------------------------------ #
    # 日程（从 schedule-hub 合并）：挂钟到点换挡，直接排主动对话
    # ------------------------------------------------------------------ #
    def _build_schedule_table(self) -> Optional[ScheduleTable]:
        """从配置构建时段表；空表返回 None（日程空转）。"""

        config = self.config.schedule
        raw = [
            {"start": period.start, "name": period.name, "focus": period.focus}
            for period in (config.periods or [])
        ]
        table = ScheduleTable(raw, config.fallback)
        if table.empty:
            logger.info("[日程] 没有配置时段表，日程空转")
            return None
        current = table.current()
        logger.info(
            "[日程] 时段表已加载：%d 档，当前「%s」",
            len(table.periods), current.name if current else "?",
        )
        return table

    async def _schedule_apply_change(self, period: Any, reason: str) -> None:
        """把「进入某一档」直接接进主动对话链路。

        合并前这一步是 HTTP 自环（schedule-hub POST /schedule-event →
        本插件的事件端口再回调 ``_on_game_event``）；合并后少绕一跳。
        档位变化（focus 记录）无条件生效——日程约束对话这件事不依赖开关；
        开关只控制「要不要为这次换挡排一轮主动开口」。
        """

        config = self.config.schedule
        if not config.enabled:
            return
        focus = str(period.focus or "").strip()
        if focus:
            self._schedule_focus = focus
        if reason == "换挡" and not config.trigger_on_change:
            return
        if reason == "载入" and not config.trigger_on_load:
            return
        feed = self._game_feed
        if feed is not None:
            # 日程不覆盖游戏素材（否则正在解说的游戏会凭空消失）；
            # 只把「有新事」记进版本，播完被挡住时仍能补触发。
            feed.note_external_event()
        logger.info(
            "[日程] 已%s：%s（%s）",
            "对齐当前档" if reason == "载入" else "换挡",
            str(period.name or "") or "当前档",
            focus or "无 focus",
        )
        self._schedule_proactive("日程换挡")

    async def _schedule_emit_on_load(self, period: Any) -> None:
        """启动后一次性对齐当前档位。"""

        try:
            await asyncio.sleep(2.0)
            await self._schedule_apply_change(period, "载入")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 对齐失败不影响日程本身
            logger.debug("[日程] 启动事件异常：%s: %s", type(exc).__name__, exc)

    def _schedule_arm_next(self) -> None:
        """只挂「下一个边界」的单次定时器（纯事件驱动，不做轮询）。"""

        task, self._schedule_task = self._schedule_task, None
        if task is not None and not task.done():
            task.cancel()
        self._schedule_task = asyncio.create_task(
            self._schedule_wait_boundary(), name="live-schedule-boundary"
        )

    async def _schedule_wait_boundary(self) -> None:
        """等下一个时段边界，到点换挡，再挂下一个。"""

        table = self._schedule_table
        if table is None:
            return
        upcoming, seconds = table.next_boundary()
        if upcoming is None:
            return
        logger.info(
            "[日程] 下一个边界 %s（%s），%.0f 分钟后",
            format_hhmm(upcoming.start), upcoming.name, seconds / 60,
        )
        try:
            await asyncio.sleep(seconds)
            current = table.current()
            if current is not None:
                logger.info("[日程] 进入时段：%s（%s）", current.name, current.focus)
                self._schedule_last_period = current.name
                self._schedule_pending_change = current.name
            # 先把下一档挂上：排主动对话不该把换挡链路拖断
            self._schedule_arm_next()
            if current is not None:
                await self._schedule_apply_change(current, "换挡")
        except asyncio.CancelledError:
            raise

    async def _start_schedule(self) -> None:
        """建时段表、挂边界、按当前档对齐一次。"""

        config = self.config.schedule
        if not config.enabled:
            logger.info("[日程] 已停用")
            return
        self._schedule_table = self._build_schedule_table()
        if self._schedule_table is None:
            return
        current = self._schedule_table.current()
        self._schedule_last_period = current.name if current else ""
        self._schedule_arm_next()
        if current is not None:
            asyncio.create_task(
                self._schedule_emit_on_load(current), name="live-schedule-load"
            )

    async def _stop_schedule(self) -> None:
        """取消换挡定时器。"""

        task, self._schedule_task = self._schedule_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._schedule_table = None
        logger.info("[日程] 已停止")

    async def _reload_schedule(self) -> None:
        """配置热更新时重建时段表。"""

        await self._stop_schedule()
        if not self.config.schedule.enabled:
            return
        await self._start_schedule()
        logger.info("[日程] 配置已热更新")
        inbox, self._event_inbox = self._event_inbox, None
        if inbox is not None:
            await inbox.stop()

    # ------------------------------------------------------------------ #
    # 弹幕回复节流（只压低「弹幕触发的回复」）
    # ------------------------------------------------------------------ #

    def _reply_config(self) -> ReplyConfig:
        """读 [reply] 配置；配置尚未注入或来自旧版 schema 时回退默认值。"""

        try:
            config = getattr(self.config, "reply", None)
        except RuntimeError:
            config = None
        return config if isinstance(config, ReplyConfig) else ReplyConfig()

    def _start_reply_adjust(self) -> None:
        """启动频率倍率巡检：独立于主动对话，关掉主动对话时也要生效。"""

        if self._reply_adjust_task is None or self._reply_adjust_task.done():
            self._reply_adjust_task = asyncio.create_task(self._reply_adjust_loop(), name="live-reply-adjust")
        config = self._reply_config()
        target = resolve_reply_frequency(config.enabled, config.danmaku_frequency)
        logger.info(
            "[弹幕节流] 已启用：直播流回复频率倍率 %.2f（弹幕约 %.0f%% 轮次进 Planner），主动对话与主播语音不受影响",
            target,
            target * 100,
        )

    async def _stop_reply_adjust(self) -> None:
        """停掉巡检，并把直播流的频率倍率还给宿主默认值。"""

        task, self._reply_adjust_task = self._reply_adjust_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._restore_reply_frequency()

    async def _reply_adjust_loop(self) -> None:
        """低频巡检：新学到直播流或宿主重建聊天流后，把倍率补回去。"""

        while True:
            try:
                await asyncio.sleep(REPLY_ADJUST_TICK_SECONDS)
                dirty = self._reply_adjust_dirty
                recheck = max(3, int(self._reply_config().recheck_seconds))
                if not dirty and time.time() - self._reply_adjust_checked_at < recheck:
                    continue
                await self._apply_reply_frequency(force=bool(dirty))
                self._reply_adjust_dirty = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 巡检不能因为一次异常就退出
                logger.debug("[弹幕节流] 巡检异常：%s: %s", type(exc).__name__, exc)

    async def _apply_reply_frequency(self, *, force: bool = False) -> None:
        """把每个已知直播流的回复频率倍率同步到配置值。

        Args:
            force: 忽略 recheck 节流立刻执行（新流出现、配置热更新时用）。
        """

        config = self._reply_config()
        if not force and time.time() - self._reply_adjust_checked_at < max(3, int(config.recheck_seconds)):
            return
        self._reply_adjust_checked_at = time.time()

        target = resolve_reply_frequency(config.enabled, config.danmaku_frequency)
        for stream_id in list(self._live_stream_ids):
            await self._sync_one_stream_frequency(stream_id, target)

    async def _sync_one_stream_frequency(self, stream_id: str, target: float) -> bool:
        """读一次、必要时设一次、再读一次确认，返回这个流是否已经生效。"""

        try:
            current = await self.ctx.call_capability("frequency.get_adjust", chat_id=stream_id, timeout_ms=1500)
        except Exception as exc:  # noqa: BLE001 - 能力不可用时只降级，不影响直播
            self._warn_reply_adjust(f"读取直播流频率失败：{type(exc).__name__}: {exc}")
            return False
        if isinstance(current, dict) and not current.get("success", True):
            self._warn_reply_adjust(f"读取直播流频率被宿主拒绝：{current}")
            return False
        current_value = self._capability_float(current)
        if current_value is not None and abs(current_value - target) < 0.001:
            return True

        try:
            result = await self.ctx.call_capability(
                "frequency.set_adjust", chat_id=stream_id, value=target, timeout_ms=1500
            )
        except Exception as exc:  # noqa: BLE001
            self._warn_reply_adjust(f"设置直播流频率失败：{type(exc).__name__}: {exc}")
            return False
        if result is False or (isinstance(result, dict) and not result.get("success", True)):
            self._warn_reply_adjust(f"宿主拒绝设置直播流频率：{result}")
            return False

        # 聊天流还没建好时宿主会返回 success 但实际没写进去；二次读取确认，
        # 不确认就当没成功，下一轮巡检继续重试（不缓存假成功）。
        try:
            applied = self._capability_float(
                await self.ctx.call_capability("frequency.get_adjust", chat_id=stream_id, timeout_ms=1500)
            )
        except Exception:  # noqa: BLE001
            applied = None
        if applied is None or abs(applied - target) >= 0.001:
            logger.debug("[弹幕节流] 直播流 %s 尚未就绪，下轮重试", stream_id)
            return False

        self._reply_adjust_warned = False
        logger.info("[弹幕节流] 直播流 %s 回复频率倍率已设为 %.2f", stream_id, target)
        return True

    async def _restore_reply_frequency(self) -> None:
        """卸载/关闭节流时把倍率还原成 1.0，别把宿主状态留成低频率。"""

        for stream_id in list(self._live_stream_ids):
            try:
                current = self._capability_float(
                    await self.ctx.call_capability("frequency.get_adjust", chat_id=stream_id, timeout_ms=1000)
                )
                if current is None or current >= REPLY_FREQUENCY_MAX - 0.001:
                    continue
                await self.ctx.call_capability(
                    "frequency.set_adjust",
                    chat_id=stream_id,
                    value=REPLY_FREQUENCY_MAX,
                    timeout_ms=1000,
                )
            except Exception as exc:  # noqa: BLE001 - 卸载阶段 ctx 可能已经失效
                logger.debug("[弹幕节流] 还原频率失败：%s", exc)

    @staticmethod
    def _capability_float(value: Any) -> Optional[float]:
        """把能力返回值（float 或 {success, value}）安全转成 float。"""

        if isinstance(value, dict):
            value = value.get("value", value.get("success"))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    def _warn_reply_adjust(self, detail: str) -> None:
        """能力异常只警告一次，别每轮巡检刷一条。"""

        if self._reply_adjust_warned:
            logger.debug("[弹幕节流] %s", detail)
            return
        self._reply_adjust_warned = True
        logger.warning(
            "[弹幕节流] %s（若提示未获授权 frequency.*，请完全重启 MaiBot 让 manifest 的权限生效）",
            detail,
        )

    def _speech_active(self) -> bool:
        """严格口径：当前是否真的有音频在播或还有短句没开口。

        主动对话与 ASR 打断用这个：整段最后一句播完就应为 False，不额外拖延。
        """

        return self._speaking.is_set() or self._speech_pending > 0

    def _planner_blocked(self, now: Optional[float] = None) -> bool:
        """整段 LLM 回复播完前，是否要挡住新入站消息（即不给 Planner 空隙）。

        宿主会把一条回复拆成多条短句分批发出，短句之间可能隔几秒。仅靠
        ``_speech_active()`` 会在最后一句播完、下一句还没到达时短暂放行。
        ``_speech_gate_until`` 由每条短句入队时续期，最后一句播完时清零，
        因此只在「整段还没结束」期间兜底，不会在正常播完后多挡一秒。
        """

        if self._speech_active():
            return True
        stamp = time.monotonic() if now is None else now
        return stamp < self._speech_gate_until

    @HookHandler(
        "chat.receive.before_process",
        name="live_stream_speaking_gate",
        description="整段 LLM 回复（含所有断句短句）播完前挡住新入站消息，不给 Planner 空隙",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def speaking_gate_hook(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """整段播报期间中止这条入站消息，等她说完再放新的进来。"""

        try:
            if not self._planner_blocked():
                return None
            message = kwargs.get("message")
            if not isinstance(message, dict):
                return None
            # 只拦直播弹幕；别的平台不该被这层节流影响
            if not self._platform_allowed(message):
                return None
            # 命令（/xxx）不拦，否则播放期间连指令都发不出去
            if bool(message.get("is_command")):
                return None
            logger.debug("[直播] 播报中，暂缓这条入站消息")
            return {"action": "abort"}
        except Exception as exc:  # noqa: BLE001 - 门禁出问题也不能卡住消息链
            logger.debug(f"[直播] 播报门禁钩子异常：{exc}")
            return None

    @HookHandler(
        "chat.receive.after_process",
        name="live_stream_activity_watch",
        description="记住最近的直播聊天流 ID 并把弹幕同步到弹幕墙；主动对话不读取任何直播间消息",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def activity_hook(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """记住直播聊天流 ID、同步弹幕墙；刻意不记录/不传递给主动对话。"""

        try:
            message = kwargs.get("message")
            if isinstance(message, dict):
                is_live = str(message.get("platform") or "").strip().lower() == CHANNEL_PLATFORM
                stream_id = str(message.get("session_id") or "").strip()
                if is_live and stream_id:
                    # 新学到一条直播流：下一轮巡检立刻给它下发回复频率倍率
                    if stream_id not in self._live_stream_ids:
                        self._reply_adjust_dirty = True
                    self._last_stream_id = stream_id
                    # 直播流集合：主动对话只在直播流开口，工具过滤与表情拦截也靠它识别
                    self._remember_live_stream(stream_id)
                # 主动对话不消费任何入站消息：这里只留「流 ID 学习」，
                # 触发条件、意图与上下文都与弹幕无关。
                # 直播平台的弹幕/礼物同步到弹幕墙（OBS 第二个浏览器源）；
                # 主播自己的语音消息不是弹幕，不上墙。
                if self._platform_allowed(message) and not self._is_asr_message(message):
                    self._push_danmaku(message)
        except Exception as exc:  # noqa: BLE001 - 观测钩子绝不能影响消息链
            logger.debug(f"[直播] 活动观测钩子异常：{exc}")
        return None

    @staticmethod
    def _is_asr_message(message: Dict[str, Any]) -> bool:
        """判断这条入站消息是不是主播的 ASR 语音（submit_voice 打了 live_asr 标记）。"""

        info = message.get("message_info")
        extra = info.get("additional_config") if isinstance(info, dict) else None
        return bool(isinstance(extra, dict) and str(extra.get("live_asr") or "").strip())

    # ------------------------------------------------------------------ #
    # 主动轮上下文隔离
    # ------------------------------------------------------------------ #

    def _proactive_isolation_ttl(self) -> int:
        """隔离窗口长度。浏览器工具链动辄十几秒，过短的兜底值会在半路饿死。"""

        try:
            configured = int(self.config.proactive.isolation_seconds)
        except Exception:  # noqa: BLE001 - 配置不可用时用默认值，绝不影响主动说话
            configured = int(PROACTIVE_ISOLATION_SECONDS)
        # 下限 30s：一次「导航 -> 读快照 -> 再点进视频」就可能超过 10s，
        # 且 WebUI 保存配置时可能把旧值写回；工具链本身另有滑动续期兜底。
        return max(30, configured)

    def _arm_proactive_isolation(self, stream_id: str, now: Optional[float] = None) -> None:
        """主动任务被受理后打开隔离：本轮 Planner/Replyer 都看不到直播消息。"""

        sid = str(stream_id or "").strip()
        if not sid:
            return
        self._proactive_isolation[sid] = (
            now if now is not None else time.time()
        ) + self._proactive_isolation_ttl()
        self._proactive_browser_calls = 0

    def _renew_proactive_isolation(self, session_id: str, now: Optional[float] = None) -> None:
        """工具链还在跑就把隔离窗口往后推，别让固定 TTL 在半路把主动轮饿死。

        宿主每执行完一个工具都会重排 Planner。开网页 -> 读快照 -> 再点进视频
        经常要十几秒，固定窗口一到期，这一轮会突然从「主动轮」掉回普通弹幕
        回复轮：强制覆盖规则被弹幕规则取代，AI 网页都读完了却直接 stop。
        只在工具链活跃时续期；工具一停就正常倒计时，出站时仍会立即解除。
        """

        sid = str(session_id or "").strip()
        if sid not in self._proactive_isolation:
            return
        self._proactive_isolation[sid] = (
            now if now is not None else time.time()
        ) + self._proactive_isolation_ttl()

    def _proactive_turn_active(self, session_id: str, now: Optional[float] = None) -> bool:
        """当前这个规划/生成请求是不是属于一次主动开口（带超时自清理）。"""

        sid = str(session_id or "").strip()
        if not sid:
            return False
        stamp = now if now is not None else time.time()
        for key, deadline in list(self._proactive_isolation.items()):
            if stamp >= deadline:
                self._proactive_isolation.pop(key, None)
        if not self._proactive_isolation:
            self._proactive_task_ids.clear()
            self._proactive_last_task_id = ""
        deadline = self._proactive_isolation.get(sid)
        if deadline is None and len(self._proactive_isolation) == 1:
            # 宿主在 planner 与 replyer 里可能用不同形式的会话 ID；单直播流时
            # 视作同一次主动轮，并记住这个别名供后续内部思考轮精确命中。
            deadline = next(iter(self._proactive_isolation.values()))
            self._proactive_isolation[sid] = deadline
        return deadline is not None and stamp < deadline

    def _disarm_proactive_isolation(self, session_id: str = "") -> None:
        """机器人已把话说出去，隔离结束；传空则清掉全部（兜底）。"""

        sid = str(session_id or "").strip()
        if sid:
            self._proactive_isolation.pop(sid, None)
        else:
            self._proactive_isolation.clear()
        self._proactive_task_ids.clear()
        self._proactive_browser_calls = 0
        # 续聊标记只对「正在飞的那一轮」有效，出站即失效
        self._proactive_followup = False

    def _schedule_proactive(self, reason: str, *, followup: bool = False) -> None:
        """事件驱动入口：有事发生就排一次开口判断，不排队、不叠加。

        一个事件只排一次；如果上一轮判断还没跑完，直接丢掉这次（下一个事件
        会再来）。这样游戏每 2 秒推一步也不会在队列里堆出一串请求。

        Args:
            followup: 这次是「回完观众、接着自己找话题」，不是新开一段。
        """

        if self._proactive is None:
            return
        task = self._proactive_kick_task
        if task is not None and not task.done():
            return
        try:
            self._proactive_kick_task = asyncio.create_task(
                self._kick_proactive(reason, followup), name="live-proactive-kick"
            )
        except RuntimeError:
            # 没有运行中的事件循环（卸载过程/单测）：这个事件已经没有意义
            self._proactive_kick_task = None

    async def _kick_proactive(self, reason: str, followup: bool = False) -> None:
        """按事件触发一次开口判断（异常只记日志，不影响事件循环）。"""

        try:
            await self._maybe_speak_proactively(reason, followup=followup)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 一次事件失败不能拖垮插件
            logger.warning(f"[主动对话] 事件触发失败（{reason}）：{type(exc).__name__}: {exc}")

    def _game_summary(self) -> str:
        """当前游戏素材；没在玩或没启用则返回空串。"""

        feed = self._game_feed
        if feed is None:
            return ""
        try:
            return feed.summary()
        except Exception:  # noqa: BLE001 - 素材整理失败就当作没在玩
            return ""

    def _followup_budget(self, config: Any = None) -> int:
        """「回完观众接着自己找话题」的额度，读 ``[proactive].followup_turns``。

        配置未注入或来自旧版 schema 时回退默认值：这是个纯锦上添花的节拍参数，
        读不到就按默认继续，绝不能因为配置问题把主动对话整条链路掐掉。
        """

        try:
            source = config if config is not None else self.config
            value = int(getattr(source.proactive, "followup_turns", PROACTIVE_FOLLOWUP_TURNS))
        except Exception:  # noqa: BLE001 - 配置不可用时用默认值
            value = PROACTIVE_FOLLOWUP_TURNS
        return max(0, min(10, value))

    def _has_pending_activity(self) -> bool:
        """有没有「还没讲过」的新事件。

        事件流里，播完只是「麦克风空了」，本身不该触发开口；真正决定要不要
        再说一段的是这期间有没有新东西（游戏推进了、弹幕来了）。没有就安静，
        这样才不会变成「音频多长、话就多密」的自循环节拍器。
        """

        feed = self._game_feed
        if feed is not None and feed.version > self._consumed_game_version:
            return True
        return False

    def _mark_speech_finished(self) -> None:
        """整段（含所有断句短句）播完：从这里开始算「说完后停顿」。

        抽成方法便于单测；正常路径由 ``_perform_reply`` 的 finally 在
        ``_speech_pending`` 归零时调用，不依赖是否真的播出了声音。
        """

        if self._proactive is not None:
            self._proactive.mark_spoken()
        # 播完不是「开口的理由」，只是「现在可以开口」；有新事件才继续说。
        if self._has_pending_activity():
            self._schedule_proactive("播完且有待讲的新事件")
            return
        # 刚回完观众的话：这一整段说的是「别人的事」，播完必须把 Planner 再叫起来
        # 让她自己接着找话题。额度只在「回应观众」的段发放（见 auto_speak_hook），
        # 自语段不续额度，所以链长有上限，不会退化成「音频多长、话就多密」。
        left = int(getattr(self, "_followup_turns_left", 0) or 0)
        if left > 0:
            self._followup_turns_left = left - 1
            logger.debug(f"[主动对话] 播完，接着自己找话题（续聊额度剩 {left - 1} 轮）")
            self._schedule_proactive("回复完接着聊", followup=True)
            return
        logger.debug("[主动对话] 播完，没有新事件，保持安静")

    async def _resolve_live_stream_id(self) -> str:
        """确定主动对话要朝哪个聊天流开口（只读，不产生任何消息）。

        旧实现只从入站弹幕里「学」流 ID。改成纯事件驱动后没有巡检定时器，
        插件刚重启、直播间又还没来弹幕时集合是空的，而第一个触发点很可能
        就是游戏事件 —— 于是永远卡在「还没有已知的直播聊天流」。这里按
        「已学到 -> 配置固定 ID -> 宿主会话表」三级兜底，保证开局即可开口。
        """

        learned = self._latest_live_stream_id()
        if learned:
            return learned
        # 配置未注入时 `_remember_live_stream` 也不会有数据；这里安静降级，
        # 保证任何初始化阶段都不会把事件回调打崩。
        configured = CHANNEL_GROUP_ID
        if configured:
            self._remember_live_stream(configured)
            return configured

        chat = getattr(getattr(self, "_ctx", None), "chat", None)
        if chat is None:
            return ""
        try:
            # 必须限时：这次查询是在事件回调链上，IPC 一旦挂住会让
            # _schedule_proactive 认为「上一轮还没跑完」，后面所有事件都被静默丢掉。
            streams = await asyncio.wait_for(
                chat.get_all_streams(platform=CHANNEL_PLATFORM), timeout=STREAM_LOOKUP_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001 - 查不到/超时都安静降级
            logger.debug(f"[主动对话] 查询直播聊天流失败：{type(exc).__name__}: {exc}")
            return ""
        if not isinstance(streams, list):
            return ""

        group_name = CHANNEL_GROUP_NAME
        fallback = ""
        for stream in reversed(streams):
            if not isinstance(stream, dict):
                continue
            sid = str(stream.get("stream_id") or stream.get("session_id") or "").strip()
            if not sid:
                continue
            # 平台已经限定为 live，最后一个流一般就是直播间；有名字时优先精确匹配
            if not fallback:
                fallback = sid
            if group_name and group_name not in str(stream.get("group_name") or ""):
                continue
            chosen = sid
            self._remember_live_stream(chosen)
            self._last_stream_id = chosen
            self._reply_adjust_dirty = True
            logger.info(f"[主动对话] 从宿主会话表找到直播聊天流：{chosen}")
            return chosen
        if fallback:
            self._remember_live_stream(fallback)
            self._last_stream_id = fallback
            self._reply_adjust_dirty = True
            logger.info(f"[主动对话] 按平台取到直播聊天流：{fallback}")
        return fallback

    async def _maybe_speak_proactively(self, trigger: str = "", *, followup: bool = False) -> None:
        """判断并执行一次主动开口，交给 Maisaka 自己处理。

        Args:
            trigger: 触发来源（播完 / 游戏推进），只用于日志定位。
            followup: 这次是「刚回完观众、接着自己找话题」，意图与尾部规则都要
                按「顺着刚才那句往下说」来写，不能再说「讲一段新的小故事」。
        """

        director = self._proactive
        if director is None:
            return
        # 整段话（含排队中的断句短句、以及分批空窗的兜底窗口）没说完前
        # 不发起新一轮：主动开口本身也是一次 Planner 轮次，插在两句中间
        # 就会把对话堆起来、显得特别话痨。
        if self._planner_blocked():
            logger.debug(f"[主动对话] 上一整段还没播完，等说完再想（事件：{trigger or '未知'}）")
            return
        now = time.time()
        if now < self._proactive_inflight_until:
            logger.debug(f"[主动对话] 上一轮还在生成，先不叠加新任务（事件：{trigger or '未知'}）")
            return
        ok, reason = director.should_speak()
        if not ok:
            logger.debug(f"[主动对话] 暂不开口：{reason}")
            return

        # 主动对话只在直播聊天流生效：用最近活跃的直播流，绝不发到本地聊天室。
        # 纯事件驱动版本没有巡检定时器，不能再靠「等一条弹幕」把流 ID 学到手；
        # 开局第一件事就是游戏事件时，这里要自己向宿主查一次会话表。
        stream_id = await self._resolve_live_stream_id()
        if not stream_id:
            logger.debug("[主动对话] 找不到直播聊天流，先不开口")
            return

        game_summary = self._game_summary()
        schedule_focus = self._schedule_focus.strip()
        intent = director.build_intent(game_summary, schedule_focus=schedule_focus, followup=followup)
        # 冷却不在这里起算：从「发起任务」算会和整段播报门控重叠，实际永远
        # 不生效（一轮 TTS 通常远大于这个数）。改由 _mark_speech_finished()
        # 在最后一句播完时打点，说完后停顿才是真正在两句话之间生效的间隔。
        # 先隔离再投递：宿主一旦唤醒 Planner，请求前 Hook 就能识别这是主动轮。
        self._arm_proactive_isolation(stream_id, now)
        # 续聊标记只对「正在飞的这一轮」有效：Planner/Replyer 的尾部规则据此换成
        # 「接着刚才那句往下说」，出站（_disarm_proactive_isolation）即失效。
        self._proactive_followup = bool(followup)
        try:
            result = await self.ctx.maisaka.trigger_proactive(
                stream_id,
                intent,
                reason="主动开口，自言自语",
                priority="normal",
                metadata={
                    "source": "live-danmaku-adapter",
                    "context": "isolated",
                    "game": "on" if game_summary else "off",
                    "schedule": "on" if schedule_focus else "off",
                },
            )
        except Exception as exc:  # noqa: BLE001 - 主动说话失败不能影响直播
            logger.warning(f"[主动对话] 触发失败：{type(exc).__name__}: {exc}")
            self._disarm_proactive_isolation(stream_id)
            self._proactive_inflight_until = 0.0
            return

        if isinstance(result, dict) and not result.get("success", True):
            logger.warning(f"[主动对话] 未被受理：{result.get('error') or result}")
            self._disarm_proactive_isolation(stream_id)
            self._proactive_inflight_until = 0.0
            return
        # 登记本次主动任务 id：Hook 靠它把这一轮和之后只带旧 task_id 的
        # 弹幕回复轮区分开，直播间弹幕插在任务后面也不会误判。
        task_id = str(result.get("task_id") or "").strip() if isinstance(result, dict) else ""
        if task_id:
            self._proactive_task_ids.add(task_id)
            self._proactive_last_task_id = task_id
        self._proactive_inflight_until = now + self._proactive_inflight_seconds
        self._proactive_said += 1
        # 这一版事件已经讲过了：下次播完不会再拿同一批进展重复开口
        if self._game_feed is not None:
            self._consumed_game_version = self._game_feed.version
            # 「这盘已经解完」只播一次：之后不再重复说结束语
            self._game_feed.mark_announced()
        logger.info(
            "[主动对话] 已请求 Maisaka 自言自语（本轮上下文已隔离直播间消息，游戏素材 %s，日程安排 %s，task=%s）",
            "有" if game_summary else "无",
            schedule_focus or "无",
            task_id or "无",
        )

    # ------------------------------------------------------------------ #
    # ASR 语音对话
    # ------------------------------------------------------------------ #

    def _start_asr(self) -> None:
        """启动 ASR：转写客户端 + 麦克风 + 全局热键；缺配置只降级不报错。"""

        config = self.config.asr
        key = config.api_key.strip()
        if not key:
            logger.warning("[ASR] 未配置 SiliconFlow api_key，语音对话不可用")
            return
        self._asr = SiliconFlowASR(
            api_key=key,
            base_url=config.base_url.strip() or "https://api.siliconflow.cn/v1",
            model=config.model.strip() or "Qwen/Qwen3-ASR-1.7B",
            timeout=30.0,
            logger=logger,
        )
        self._mic = MicRecorder(device=config.device.strip(), logger=logger)
        hotkey = AsrHotkey(
            hotkey=config.hotkey.strip() or "f9",
            loop=self._asr_loop or asyncio.get_running_loop(),
            on_press=self._asr_press,
            on_release=self._asr_release,
            logger=logger,
        )
        if not hotkey.start():
            logger.warning("[ASR] 热键监听启动失败，语音对话不可用")
            return
        self._asr_hotkey = hotkey
        logger.info(f"[ASR] 语音对话已启用：按住 {hotkey.hotkey} 说话，松开发送（模型 {self._asr.model}）")

    async def _stop_asr(self) -> None:
        """停掉热键监听并释放麦克风。"""

        hotkey, self._asr_hotkey = self._asr_hotkey, None
        if hotkey is not None:
            hotkey.stop()
        mic, self._mic = self._mic, None
        if mic is not None and mic.recording:
            mic.stop()
        self._asr = None

    async def _asr_press(self) -> None:
        """按下热键：她在说话就先打断，闭嘴后开始录音。"""

        mic = self._mic
        if mic is None or mic.recording:
            return
        if self._speech_active():
            # 主播开口：当前播报和排队中的断句短句一起作废，别再念旧台词
            self._speech_generation += 1
            # 门控一并解除：主播的语音必须能立刻进 Planner，不能被旧台词挡住
            self._speech_gate_until = 0.0
            self._speech_pending = 0
            # 打断当前播报：合成流提前收尾、收尾等待立即退出
            self._speech_interrupt.set()
            deadline = time.monotonic() + 2.0
            while self._speaking.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            # 留一点残响缓冲，别把她的尾音录进识别
            await asyncio.sleep(0.25)
        if not mic.start():
            logger.warning("[ASR] 麦克风打开失败，这句话没录上")

    async def _asr_release(self) -> None:
        """松开热键：停止录音 → 转写 → 送进聊天流。"""

        mic = self._mic
        if mic is None or not mic.recording:
            return
        config = self.config.asr
        pcm = mic.stop()
        rate = mic.sample_rate
        # 超长截断：按字节（int16 单声道 = 每秒 2*rate 字节）
        max_bytes = int(float(config.max_duration) * rate * 2)
        if len(pcm) > max_bytes:
            pcm = pcm[:max_bytes]
        duration = len(pcm) / float(rate * 2)
        if duration < float(config.min_duration):
            logger.debug(f"[ASR] 录音太短（{duration:.2f}s），丢弃")
            return
        client = self._asr
        if client is None:
            return
        try:
            text = await client.transcribe(pcm_to_wav(pcm, rate))
        except Exception as exc:  # noqa: BLE001 - 转写失败不能影响直播
            logger.warning(f"[ASR] 转写失败：{type(exc).__name__}: {exc}")
            return
        text = text.strip()
        if not text:
            logger.info("[ASR] 没听清（空转写），丢弃")
            return
        user_name = config.user_name.strip() or "主人"
        if bool(getattr(config, "bypass_planner", False)):
            # 快速通道：不等 Planner 决策，直接让 Replyer 答。
            # 用 task 是为了让热键回调立刻返回，不把按键线程卡在一次 LLM 调用上。
            asyncio.create_task(
                self._voice_reply_fast(text, user_name), name="live-voice-fast")
            return
        channel = self._channel
        if channel is None or not channel.submit_voice(text, user_name):
            logger.warning(f"[ASR] 未能送进聊天流：{text[:40]}")
            return
        logger.info(f"[ASR] 已发送（{user_name}）：{text[:60]}")

    # ------------------------------------------------------------------ #
    # 语音快速通道：绕过 Planner，Replyer 直接答
    # ------------------------------------------------------------------ #
    def _voice_reference(self) -> str:
        """把最近几轮拼成 Replyer 的写作材料。

        ``maisaka.reply.generate`` 内部传的是空 chat_history，看不到聊天流里
        已经发生过的对话。不自己带上最近几轮，她每句话都会像失忆重来。
        """

        if not self._voice_history:
            return ""
        lines = [f"{who}：{line}" for who, line in self._voice_history]
        return "刚才的直播对话（最后一句是现在要回答的）：\n" + "\n".join(lines)

    async def _voice_append_context(self, stream_id: str, speaker: str, text: str) -> None:
        """把一句语音对话写回 Maisaka 上下文，但不触发回复。

        ``replyable=False`` 是关键：写进去让后续 Planner 回合看得见，
        又不会反过来排一轮规划，否则就绕不开了。
        """

        line = f"{speaker}：{text}"
        try:
            await self.ctx.maisaka.append_context(
                stream_id,
                [{"type": "text", "data": line}],
                visible_text=line,
                source_kind="live_voice",
                replyable=False,
            )
        except Exception as exc:  # noqa: BLE001 - 写不进去只影响记忆，不影响这一句
            logger.debug(f"[ASR] 写回上下文失败：{type(exc).__name__}: {exc}")

    async def _voice_reply_fast(self, text: str, user_name: str = "主人") -> None:
        """语音快速通道：Replyer 直答 → 摘记号 → 表情 → 播报 → 写回上下文。

        绕开 Planner 换来的是快：没有规划轮、没有工具调用，一次 Replyer
        调用就能开口。代价同样明确——她不会真的动手做事（开网页、玩游戏、
        查日程），那些能力只有 Planner 路径给得了。

        上下文仍然一致：两边都写回 Maisaka（replyable=False），所以下一轮
        Planner 回合知道这段说过什么，不会变成两套记忆。
        """

        content = str(text or "").strip()
        if not content:
            return
        stream_id = self._latest_live_stream_id()
        if not stream_id:
            # 还没学到直播流就先按老路走，别把这句话丢了
            channel = self._channel
            if channel is not None:
                channel.submit_voice(content, user_name)
            logger.info(f"[ASR] 还没有直播流，按原路送进聊天流：{content[:40]}")
            return

        hint = str(getattr(self.config.asr, "voice_reply_hint", "") or "").strip() or VOICE_REPLY_HINT
        async with self._voice_lock:
            started = time.monotonic()
            self._voice_history.append((user_name, content))
            await self._voice_append_context(stream_id, user_name, content)
            try:
                result = await self.ctx.maisaka.reply.generate(
                    stream_id,
                    self._voice_reference(),
                    format_instruction=hint,
                    timeout_ms=30000,
                )
            except Exception as exc:  # noqa: BLE001 - 生成失败不能拖垮直播
                logger.warning(f"[ASR] 快速通道生成失败：{type(exc).__name__}: {exc}")
                return
            if not isinstance(result, dict) or not result.get("success"):
                error = result.get("error") if isinstance(result, dict) else ""
                logger.warning(f"[ASR] 快速通道没拿到文本：{error or '未知原因'}")
                return
            reply = str(result.get("text") or "").strip()
            if not reply:
                return
            # 这条链路不经过 send 钩子，记号要自己摘，否则【流泪】会被念出来
            names = self._action_names()
            if names:
                clean, markers = split_actions(reply, names)
                if markers:
                    await self._run_actions(markers)
                    logger.info("[ASR] 快速通道摘到 %d 个动作记号", len(markers))
                    reply = clean
            self._voice_history.append(("我", reply))
            await self._voice_append_context(stream_id, "我", reply)
            logger.info(
                "[ASR] 快速通道 %.2fs：「%s」→「%s」",
                time.monotonic() - started, content[:24], reply[:40],
            )
            await self.react_expression(reply)
            await self.speak(reply)

    # ------------------------------------------------------------------ #
    # 表情包拦截
    # ------------------------------------------------------------------ #

    def _remember_live_stream(self, stream_id: str) -> None:
        """记住一个直播流 session_id（超过上限淘汰最旧的，防无界增长）。"""

        sid = str(stream_id or "").strip()
        if not sid:
            return
        self._live_stream_ids.pop(sid, None)
        self._live_stream_ids[sid] = None
        while len(self._live_stream_ids) > 20:
            oldest = next(iter(self._live_stream_ids))
            self._live_stream_ids.pop(oldest, None)

    def _latest_live_stream_id(self) -> str:
        """取最近活跃的直播流 session_id（主动对话只朝直播开口）。

        _remember_live_stream 的插入顺序即最近活跃顺序，取最后一个即可。
        """

        for sid in reversed(self._live_stream_ids):
            if sid:
                return sid
        return ""

    def _is_live_stream_id(self, stream_id: str) -> bool:
        """判断会话 ID 是不是直播聊天流（表情包只在直播流里禁用）。

        优先用从直播消息里学到的 session_id 集合；再按配置与命名规则兜底。
        """

        sid = str(stream_id or "").strip()
        if not sid:
            return False
        if sid in self._live_stream_ids:
            return True
        if sid == "live-all":
            return True
        gid = CHANNEL_GROUP_ID
        if gid and sid == gid:
            return True
        # 分房模式：每个平台房间各自一个流，形如 bilibili:<room> / douyin:<room>
        return sid.startswith(("bilibili:", "douyin:"))

    @staticmethod
    def _tool_definition_name(definition: Any) -> str:
        """读取工具定义名称，兼容内部定义与 OpenAI function 结构。"""

        if not isinstance(definition, dict):
            return ""
        inner = definition.get("function")
        if isinstance(inner, dict) and str(inner.get("name") or "").strip():
            return str(inner["name"]).strip()
        return str(definition.get("name") or "").strip()

    def _live_heavy_tool_patterns(self) -> tuple:
        """用户额外屏蔽的工具（默认空；浏览器按"每轮次数"单独限制）。"""

        try:
            cfg = getattr(self.config, "live_tools", None)
        except Exception:  # noqa: BLE001 - 配置尚未注入（启动早期/单测）时不做额外屏蔽
            cfg = None
        if cfg is None:
            return ()
        if not bool(getattr(cfg, "enabled", True)):
            return ()
        raw = str(getattr(cfg, "blocked", "") or "").replace("，", ",")
        return tuple(part.strip() for part in raw.split(",") if part.strip())

    def _browser_call_limit(self) -> int:
        """同一轮允许的浏览器调用次数；0=不限。"""

        try:
            return int(getattr(self.config.live_tools, "max_browser_calls", 1))
        except Exception:  # noqa: BLE001 - 配置未注入时按 1 次
            return 1

    @staticmethod
    def _tool_name_blocked(name: str, patterns: tuple) -> bool:
        """工具名是否命中屏蔽名单（支持 browser_ 这种前缀写法）。"""

        if not name or not patterns:
            return False
        return any(name == pattern or name.startswith(pattern) for pattern in patterns)

    @HookHandler(
        "maisaka.planner.before_request",
        name="live_stream_tool_filter",
        description="直播流隐藏表情包工具；标注自己说过的话防自问自答；主动轮隐藏弹幕、空转轮要求 wait",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=5000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def planner_tool_filter_hook(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """规划请求前：过滤表情工具 + 主动轮上下文隔离。

        非直播流原样放行；没有变化时返回 None 不改写任何参数。
        """

        try:
            session_id = str(kwargs.get("session_id") or "")
            if not self._is_live_stream_id(session_id):
                return None
            modified: Dict[str, Any] = {}
            proactive = self._proactive_turn_active(session_id)
            request_items = kwargs.get("items")
            # 浏览器不是完全不给，而是"每轮最多 N 次"：
            # 一次 browser_* 调用就把 Planner 拖到 8~200 秒，逛起来没完；
            # 达到上限就摘掉 browser_*，逼她看完开口。新弹幕进来计数归零，又能上网。
            heavy = list(self._live_heavy_tool_patterns())
            limit = self._browser_call_limit()
            if not proactive and limit > 0 and browser_calls_in_turn(request_items) >= limit:
                heavy.append("browser_")
            heavy = tuple(heavy)

            # 1) 表情包工具与重工具：直播流里模型根本看不到
            tools = kwargs.get("tool_definitions")
            if isinstance(tools, list) and tools:
                filtered = [
                    definition
                    for definition in tools
                    if self._tool_definition_name(definition) not in _BLOCKED_EMOJI_TOOLS
                    and not self._tool_name_blocked(self._tool_definition_name(definition), heavy)
                ]
                if len(filtered) != len(tools):
                    removed = [self._tool_definition_name(d) for d in tools if d not in filtered]
                    kept = [self._tool_definition_name(d) for d in filtered if self._tool_definition_name(d)]
                    modified["tool_definitions"] = filtered
                    logger.info(
                        "[直播] 工具表 %d -> %d：屏蔽 %s；保留 %s",
                        len(tools), len(filtered),
                        "、".join(name for name in removed[:4] if name) or "表情包工具",
                        "、".join(kept[:10]),
                    )

            # 2) 上下文：主动轮清洗弹幕 + 所有直播轮标注「自己说的话」
            # 记下本轮上下文里最后一条真实消息：主动轮 reply 找不到目标时用它当锚点
            anchor = last_context_message_id(request_items)
            if anchor:
                self._live_anchor_msg_id = anchor
            new_items: list = request_items if isinstance(request_items, list) else []
            items_changed = False
            has_active_task = items_have_active_proactive_task(request_items, self._proactive_task_ids)
            tool_chain_active = items_have_proactive_tool_chain(request_items)
            browser_exhausted = False
            if proactive and tool_chain_active:
                self._proactive_browser_calls += 1
                browser_exhausted = (
                    self._proactive_browser_calls > MAX_PROACTIVE_BROWSER_CALLS
                )
                # 工具链每跑一步就续期：实测「导航 -> 快照 -> 查找 -> 再导航」
                # 要 12.7s，固定 10s 窗口会在读完页面、正要开口那一轮到期，
                # 强制覆盖规则被弹幕规则取代，AI 就只剩一个 stop。
                # 但只能续到上限，否则模型会在「搜 -> 点视频 -> 再搜」里无限逛。
                if not browser_exhausted:
                    self._renew_proactive_isolation(session_id)
            if proactive and not has_active_task:
                # 只带旧 task_id 的通常是后续弹幕回复轮（上一轮主动任务被
                # stop，隔离残留到现在）：解除隔离让弹幕回复照常看到弹幕。
                # 但工具链还在跑时不能解：宿主每执行完一个工具就重排 Planner，
                # 那几轮的 task 标记经常已经换掉，提前解除会让「直接 stop」
                # 规则回流，把刚读完网页、正要开口的这一轮按死。
                if not tool_chain_active:
                    self._disarm_proactive_isolation()
                    proactive = False
            if proactive:
                schedule_mode = bool(self._schedule_focus.strip())
                new_items, hidden = strip_live_context_items(
                    new_items, schedule_mode=schedule_mode, tail_items=PROACTIVE_TAIL_ITEMS
                )
                if hidden:
                    items_changed = True
                    logger.info(f"[主动对话] Planner 上下文已清洗 {hidden} 条（弹幕/规则/摘要/发言指导）")

            # 自己发出的历史消息带 is_self_message="true"，模型会当成观众发言
            # 去反问/补刀。原文保留（避免重复讲同一个段子），但加显式标签。
            new_items, annotated = annotate_self_messages(new_items)
            if annotated:
                items_changed = True
                logger.info(f"[直播] 已标注 {annotated} 条自己说过的话，禁止当成观众发言回应")

            # 只有「非主动轮 + 上下文里全是自己刚说的话」才要求 wait。
            # 不能用 has_active_task 判空：活跃任务表为空时它会保守返回 True，
            # 那样空转轮就永远拿不到「不要说话」的兜底规则。
            self_only = (not proactive) and self_only_round(request_items)
            # 出站兜底：空转轮里即使模型仍吐了字，也不让它出声/上字幕。
            # 其它轮（有弹幕、主动任务、主播语音）一律清零，绝不误伤。
            self._self_echo_suppress_until = time.monotonic() + 30.0 if self_only else 0.0
            rule = SELF_ECHO_RULE
            if proactive:
                # 日程优先：当前档有事要做时，先要求动手用工具，别又去讲小故事；
                # 没有日程才按「有没有在玩游戏 / 是不是回完话续聊」决定文案。
                schedule_focus = self._schedule_focus.strip()
                followup = bool(getattr(self, "_proactive_followup", False))
                if schedule_focus:
                    fresh = proactive_schedule_rule(schedule_focus)
                elif followup:
                    fresh = PROACTIVE_FOLLOWUP_RULE
                elif self._game_summary():
                    fresh = PROACTIVE_GAME_RULE
                else:
                    fresh = PROACTIVE_FRESH_RULE
                # 覆盖规则放尾部：system 前缀保持稳定，避免每轮主动对话把缓存打碎
                rule = f"{proactive_override_text(schedule_mode, followup)}\n{rule}\n{fresh}"
            elif self_only:
                rule = f"{rule}\n{SELF_ONLY_ROUND_RULE}"
            new_items, added = append_round_rule(new_items, rule)
            if added:
                items_changed = True
                if self_only:
                    logger.info("[直播] 本轮只有自己刚说的话、没有新观众消息：要求模型直接 stop")
            if items_changed:
                modified["items"] = new_items

            # 主动轮已经看过页面了：把浏览器工具从本轮工具表拿掉，逼它开口讲。
            # 不给工具而不是事后拦截，模型自然只能 reply；这是长工具链的收口点。
            if proactive and browser_exhausted:
                base_tools = modified.get("tool_definitions", tools)
                if isinstance(base_tools, list) and base_tools:
                    trimmed = [
                        definition
                        for definition in base_tools
                        if not self._tool_definition_name(definition).startswith("browser_")
                    ]
                    if len(trimmed) != len(base_tools):
                        modified["tool_definitions"] = trimmed
                        logger.info(
                            "[主动对话] 页面已读过，收口浏览器工具：%d -> %d（第 %d 步）",
                            len(base_tools), len(trimmed), self._proactive_browser_calls,
                        )

            # 宿主的 modified_kwargs 是「整体替换」而非合并：必须回传完整
            # kwargs，只覆盖要改的字段，否则 items / session_id 等会丢失。
            return {"modified_kwargs": {**kwargs, **modified}} if modified else None
        except Exception as exc:  # noqa: BLE001 - 过滤失败不能影响规划链路
            logger.debug(f"[直播] 规划请求钩子异常：{exc}")
        return None

    @HookHandler(
        "maisaka.planner.after_response",
        name="live_stream_end_turn_after_reply",
        description="直播流里 reply 之后补一个 stop 工具调用：从源头结束本轮，宿主不再自动续跑下一轮 Planner",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def end_turn_after_reply_hook(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """说完就收口，让下一轮 Planner 根本不被触发（不是拦截/挂起）。

        宿主 ``run_loop`` 在工具执行完会无条件 ``continue`` 到下一轮内部思考，
        实测同一 turn 能连跑 3/16、4/16 轮，而此时上一整段 TTS 还在播。
        这是 TTS 期间又起 Planner 的唯一源头；入站门控只管新消息，管不到它。
        ``stop`` 的返回值带 ``pause_execution=True``，宿主会 break 出循环并进入
        「等待新消息」状态，之后只有新弹幕 / 主播语音 / 主动任务才会开新 turn。
        """

        try:
            session_id = str(kwargs.get("session_id") or "")
            if not self._is_live_stream_id(session_id):
                return None
            if not bool(getattr(self.config.reply, "end_turn_after_reply", True)):
                return None

            raw_items = kwargs.get("output_items")
            new_items, retargeted = raw_items, 0
            # 主动轮没有「要回复的观众消息」，模型会抓过期 id 或填 none，
            # 宿主直接判「未找到要回复的目标消息」→ 不发送、不播报。
            # 用本轮上下文里最后一条真实消息当锚点（task_id 属于内部消息，
            # 宿主 find_source_message_by_id 查不到），既能出声又不指向任何人。
            if self._proactive_turn_active(session_id):
                anchor = self._live_anchor_msg_id or self._proactive_last_task_id
                if anchor:
                    new_items, retargeted = retarget_proactive_reply(raw_items, anchor)
                    if retargeted:
                        logger.info(
                            "[主动对话] reply 目标已改成本轮上下文锚点（%s 个，锚点=%s）",
                            retargeted,
                            anchor,
                        )

            new_items, added = append_stop_tool_call(new_items)
            if not added and not retargeted:
                return None
            if added:
                logger.info("[直播] 本轮已生成可见回复，收口本轮思考：不自动续跑下一轮 Planner")
            # 宿主的 modified_kwargs 是整体替换，必须把原始 kwargs 全带回去。
            return {"modified_kwargs": {**kwargs, "output_items": new_items}}
        except Exception as exc:  # noqa: BLE001 - 收口失败不能影响回复链路
            logger.debug(f"[直播] 收口钩子异常：{exc}")
        return None

    @HookHandler(
        "maisaka.replyer.before_request",
        name="live_stream_proactive_reply_hint",
        description="给回复生成追加「不接自己说过的话」规则；主动轮再加自言自语要求",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def proactive_reply_hint_hook(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Replyer 提示词加锁：所有直播轮都不接自己的话，主动轮还不接弹幕。"""

        try:
            session_id = str(kwargs.get("session_id") or "")
            if not self._is_live_stream_id(session_id):
                return None
            current = str(kwargs.get("extra_prompt") or "")
            additions = [rule for rule in (SELF_ECHO_RULE,) if rule not in current]
            # 动作记号词表：只列当前皮套真有的外观/表情/道具/小动作，
            # 模型照着这份清单打记号，出站时由 action_marker_hook 摘掉并派发。
            await self._warm_actions()
            action_prompt = self._action_prompt()
            if action_prompt and action_prompt not in current:
                additions.append(action_prompt)
            if self._proactive_turn_active(session_id):
                if self._game_summary() or getattr(self, "_proactive_followup", False):
                    hint = PROACTIVE_GAME_REPLY_HINT
                else:
                    hint = PROACTIVE_REPLY_HINT
                if hint not in current:
                    additions.append(hint)
            if not additions:
                return None
            merged = "\n".join(part for part in (current.strip(), *additions) if part)
            return {"modified_kwargs": {**kwargs, "extra_prompt": merged}}
        except Exception as exc:  # noqa: BLE001 - 提示词失败不能挡住回复生成
            logger.debug(f"[主动对话] 回复提示词钩子异常：{exc}")
        return None

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="live_stream_proactive_reply_context",
        description="所有直播轮标注自己说过的话；主动开口轮再隐藏历史直播消息",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def proactive_reply_context_hook(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Replyer 也会读完整聊天历史：标注自己说过的话，主动轮再隐藏弹幕。"""

        try:
            session_id = str(kwargs.get("session_id") or "")
            if not self._is_live_stream_id(session_id):
                return None
            original = kwargs.get("items")
            if not isinstance(original, list):
                return None
            new_items, annotated = annotate_self_messages(original)
            hidden = 0
            added = False
            if self._proactive_turn_active(session_id):
                schedule_mode = bool(self._schedule_focus.strip())
                new_items, hidden = strip_live_context_items(
                    new_items, schedule_mode=schedule_mode, tail_items=PROACTIVE_TAIL_ITEMS
                )
                # 覆盖规则放尾部；system 保持原样，缓存前缀才不会被主动轮打碎
                new_items, added = append_round_rule(
                    new_items,
                    proactive_override_text(schedule_mode, bool(getattr(self, "_proactive_followup", False))),
                )
                if hidden:
                    logger.info(f"[主动对话] Replyer 上下文已清洗 {hidden} 条（弹幕/规则/摘要/发言指导）")
            if annotated:
                logger.info(f"[直播] Replyer 已标注 {annotated} 条自己说过的话")
            if not annotated and not hidden and not added:
                return None
            return {"modified_kwargs": {**kwargs, "items": new_items}}
        except Exception as exc:  # noqa: BLE001 - 上下文改写失败就原样发送
            logger.debug(f"[主动对话] 回复上下文钩子异常：{exc}")
        return None

    @HookHandler(
        "emoji.maisaka.before_select",
        name="live_stream_emoji_block",
        description="直播聊天流禁用表情包：命中直播流时中止表情选择（工具列表过滤的兜底）",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def emoji_block_hook(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """表情选择前兜底拦截：万一表情工具仍被调用（如子代理路径），在此中止。"""

        try:
            if self._is_live_stream_id(str(kwargs.get("stream_id") or "")):
                logger.debug("[直播] 已拦截直播流的表情包发送")
                return {
                    "action": "abort",
                    # modified_kwargs 是整体替换，带上原始 kwargs 避免丢参数
                    "modified_kwargs": {
                        **kwargs,
                        "abort_message": "直播画面不支持表情包，请直接用文字表达情绪。",
                    },
                }
        except Exception as exc:  # noqa: BLE001 - 拦截失败不能影响表情系统
            logger.debug(f"[直播] 表情包拦截钩子异常：{exc}")
        return None

    # ------------------------------------------------------------------ #
    # 字幕
    # ------------------------------------------------------------------ #

    async def _refresh_subtitle(self) -> None:
        """热更新字幕：已运行就原地更新渲染参数，未运行才启动，禁用才停。"""

        config = self.config.subtitle
        if not config.enabled:
            await self._stop_subtitle()
            return
        if self._subtitle is not None:
            self._subtitle.update(
                font_size=config.font_size,
                text_color=config.text_color,
                background=config.background,
                hold_seconds=config.hold_seconds,
                max_chars=config.max_chars,
            )
        else:
            await self._start_subtitle()

    async def _start_subtitle(self) -> None:
        """按配置起字幕页面（端口被占只告警，不影响其它功能）。"""

        config = self.config.subtitle
        if not config.enabled or self._subtitle is not None:
            return
        server = SubtitleServer(
            host=config.host,
            port=config.port,
            font_size=config.font_size,
            text_color=config.text_color,
            background=config.background,
            hold_seconds=config.hold_seconds,
            max_chars=config.max_chars,
            logger=logger,
        )
        if await server.start():
            self._subtitle = server

    async def _stop_subtitle(self) -> None:
        """停掉字幕页面。"""

        server, self._subtitle = self._subtitle, None
        if server is not None:
            await server.stop()

    def _publish_subtitle(self, text: str, auto_clear_after: Optional[float] = None) -> None:
        """把当前这句推给字幕页面；没开字幕时什么都不做。"""

        server = self._subtitle
        if server is None:
            return
        self._subtitle_token += 1
        token = self._subtitle_token
        try:
            server.publish(text)
        except Exception as exc:  # noqa: BLE001 - 字幕失败不能影响播报
            logger.debug(f"[字幕] 推送失败：{exc}")
            return
        if auto_clear_after is not None:
            self._schedule_subtitle_clear(token, delay=auto_clear_after)

    async def _drive_subtitle(
        self,
        text: str,
        progress: Dict[str, float],
        finished: asyncio.Event,
    ) -> None:
        """按真实播放进度逐句推进字幕：说哪句显示哪句。

        以「已写入声卡的秒数 ÷ 每字秒数」估算说到第几个字，再落到具体句子。
        每字秒数由每次播完后实测校准，所以不会越跑越偏。
        """

        sentences = split_subtitle_sentences(text)
        if len(sentences) <= 1:
            self._publish_subtitle(text)
            return

        rate = max(0.05, float(self._subtitle_seconds_per_char))
        shown = 0
        self._publish_subtitle(sentences[0])
        while not finished.is_set():
            spoken_chars = float(progress.get("seconds") or 0.0) / rate
            acc = 0
            index = 0
            for i, sentence in enumerate(sentences):
                acc += len(sentence)
                index = i
                if spoken_chars < acc:
                    break
            if index > shown:
                shown = index
                self._publish_subtitle(sentences[shown])
            try:
                await asyncio.wait_for(finished.wait(), timeout=0.08)
            except asyncio.TimeoutError:
                continue
        # 播完时确保最后一句确实露出过（语速比估算快时可能还没轮到）
        if shown != len(sentences) - 1:
            self._publish_subtitle(sentences[-1])

    def _schedule_subtitle_clear(self, token: int, delay: Optional[float] = None) -> None:
        """一句播完就撤字幕；``hold_seconds`` > 0 时多停留一会儿。"""

        server = self._subtitle
        if server is None:
            return
        hold = float(self.config.subtitle.hold_seconds if delay is None else delay)
        if hold <= 0:
            if token == self._subtitle_token:
                try:
                    server.clear()
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[字幕] 清除失败：{exc}")
            return

        async def _later() -> None:
            await asyncio.sleep(hold)
            if token == self._subtitle_token and self._subtitle is not None:
                self._subtitle.clear()

        try:
            asyncio.create_task(_later(), name="live-subtitle-clear")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[字幕] 延迟清除失败：{exc}")

    # ------------------------------------------------------------------ #
    # 弹幕墙
    # ------------------------------------------------------------------ #

    async def _refresh_danmaku(self) -> None:
        """热更新弹幕墙：已运行就原地更新渲染参数，未运行才启动，禁用才停。"""

        config = self.config.danmaku
        if not config.enabled:
            await self._stop_danmaku()
            return
        if self._danmaku is not None:
            self._danmaku.update(
                max_lines=config.max_lines,
                hold_seconds=config.hold_seconds,
                font_size=config.font_size,
                text_color=config.text_color,
                name_color=config.name_color,
                background=config.background,
            )
        else:
            await self._start_danmaku()

    async def _start_danmaku(self) -> None:
        """按配置起弹幕页面（端口被占只告警）。"""

        config = self.config.danmaku
        if not config.enabled or self._danmaku is not None:
            return
        server = DanmakuServer(
            host=config.host,
            port=config.port,
            max_lines=config.max_lines,
            hold_seconds=config.hold_seconds,
            font_size=config.font_size,
            text_color=config.text_color,
            name_color=config.name_color,
            background=config.background,
            logger=logger,
        )
        if await server.start():
            self._danmaku = server

    async def _stop_danmaku(self) -> None:
        """停掉弹幕页面。"""

        server, self._danmaku = self._danmaku, None
        if server is not None:
            await server.stop()

    def _push_danmaku(self, message: Dict[str, Any]) -> None:
        """把一条入站直播消息推到弹幕墙；解析不出内容就跳过。"""

        server = self._danmaku
        if server is None:
            return
        try:
            item = parse_inbound(message)
            if item:
                server.push(item)
        except Exception as exc:  # noqa: BLE001 - 弹幕墙失败不能影响消息链
            logger.debug(f"[弹幕墙] 推送失败：{exc}")

    # ------------------------------------------------------------------ #
    # TTS 自动播报
    # ------------------------------------------------------------------ #

    def _learn_live_stream_from_outbound(self, message: Any, stream_id: Any) -> None:
        """从出站轮次里确认直播流 ID。

        入站的 ``activity_hook`` 只有在真的有弹幕时才能学到 session_id；
        弹幕源关掉、或插件刚重启还没来弹幕时集合是空的，主动对话就找不到
        开口目标。出站消息同样带 ``stream_id`` 且能确认平台，AI 每说一次话
        就能补一次，不需要额外宿主能力，也不需要重启授权。
        """

        sid = str(stream_id or "").strip()
        if not sid or not isinstance(message, dict) or not self._platform_allowed(message):
            return
        if sid not in self._live_stream_ids:
            self._reply_adjust_dirty = True
        self._last_stream_id = sid
        self._remember_live_stream(sid)

    @HookHandler(
        "send_service.after_build_message",
        name="live_stream_no_typing_delay",
        description="直播流取消断句分段的模拟打字等待，让一条 LLM 回复的所有短句连续排队、播完前门控不中断",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def typing_delay_hook(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """把直播流分段消息的 ``typing`` 改成 False。

        宿主对 ``typing=True`` 的分段会按 0.3 秒/字 ``await asyncio.sleep``
        （``send_service._prepare_message_for_platform_io``）。一条 20 字短句
        就是 6 秒多，比这句音频还长：第一句播完时第二句还没发出来，
        ``_speech_pending`` 归零，入站门控短暂打开，Planner 就插进来堆对话。
        这个钩子跑在 sleep 之前且允许改写 kwargs，关掉等待后所有短句会在
        毫秒内连续入队，整段期间门控一直接地。

        宿主的 ``modified_kwargs`` 是整体替换而非合并，所以必须回传完整
        kwargs，只覆盖 ``typing``，否则 message / set_reply 等参数会丢失。
        """

        try:
            self._learn_live_stream_from_outbound(kwargs.get("message"), kwargs.get("stream_id"))
            if not kwargs.get("typing"):
                return None
            message = kwargs.get("message")
            stream_id = str(kwargs.get("stream_id") or "")
            if not self._is_live_stream_id(stream_id):
                if not isinstance(message, dict) or not self._platform_allowed(message):
                    return None
            logger.debug("[直播] 取消分段模拟打字等待，保持整段回复的门控连续")
            return {"modified_kwargs": {**kwargs, "typing": False}}
        except Exception as exc:  # noqa: BLE001 - 改写失败只是恢复原等待，不能影响发送
            logger.debug(f"[直播] 取消打字等待钩子异常：{exc}")
            return None

    # ------------------------------------------------------------------ #
    # 台词动作记号：AI 在回复里写【兔牙】/<点头>，这里摘掉并派给演出引擎
    # ------------------------------------------------------------------ #
    def _actions_snapshot(self) -> Dict[str, Any]:
        """当前缓存的「这个皮套能演什么」词表（由 :meth:`_warm_actions` 刷新）。

        走引擎的 HTTP 接口而不是落盘文件：插件目录被宿主的 file_watcher 盯着，
        往里写文件会触发整插件热重载（引擎刚起来就会被重启）。
        """

        cached = self._actions_cache
        return cached[0] if cached else {"actions": []}

    async def _warm_actions(self, ttl: float = 60.0) -> None:
        """按 TTL 向演出引擎要一次动作词表（引擎没起来就留空）。"""

        now = time.monotonic()
        cached = self._actions_cache
        if cached is not None and now - cached[1] < ttl:
            return
        payload: Dict[str, Any] = {"actions": []}
        engine = self._engine
        # 不判 engine.running：插件热重载后管理器实例可能是新的，而引擎还在跑。
        # 拿不到就当没有词表（只是不能打记号），2 秒超时 + 60 秒缓存，成本可忽略。
        if engine is not None:
            try:
                data = await engine.actions()
                if isinstance(data, dict) and isinstance(data.get("actions"), list):
                    payload = data
            except Exception as exc:  # noqa: BLE001 - 词表拿不到只是不能打记号
                logger.debug(f"[直播] 拉取动作词表失败：{type(exc).__name__}: {exc}")
        self._actions_cache = (payload, now)

    def _action_names(self) -> set:
        """当前可用的动作名与别名（记号合法性判定用）。

        视线、重置与语气记号是内置的（不依赖模型资产），永远合法。
        """

        names = set()
        for key, aliases, _gx, _gy in GAZE_TARGETS:
            names.add(key)
            for alias in aliases:
                names.add(alias)
        names.update(RESET_ACTIONS)
        for key, aliases, _ins in DIRECTION_ACTIONS:
            names.add(key)
            for alias in aliases:
                names.add(alias)
        for item in self._actions_snapshot().get("actions") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if key:
                names.add(key)
            for alias in item.get("aliases") or []:
                alias = str(alias or "").strip()
                if alias:
                    names.add(alias)
        return names

    def _action_prompt(self) -> str:
        """给模型的「你可以打哪些记号」提示词；没有可用动作时返回空串。"""

        try:
            template = str(getattr(self.config.performance, "action_prompt", "") or "")
            return build_action_prompt(
                self._actions_snapshot().get("actions") or [], template=template)
        except Exception as exc:  # noqa: BLE001 - 提示词失败不能挡住回复
            logger.debug(f"[直播] 动作提示词生成失败：{exc}")
            return ""

    def _remember_ai_actions(self, markers: list) -> None:
        """记下这一轮 AI 用记号点过的表情/道具。

        记号是模型自己下的指令，优先级高于任何关键词推断——它说了要哭，
        就不能因为后半句有个「哈哈」又被判成开心。带时间戳是因为一条回复
        会拆成多条消息依次出站：只有第一句带记号，整段却都该是这张脸。
        """

        actions = self._actions_snapshot().get("actions") or []
        now = time.monotonic()
        for marker in markers:
            kind = action_kind_of(str(marker.name), actions)
            key = canonical_action_of(str(marker.name), actions) or str(marker.name)
            if kind == "expression":
                self._ai_expression = (now, key)
            elif kind == "prop":
                self._ai_prop = (now, key)

    def _ai_action_recent(self, attr: str) -> str:
        """AI 刚用记号点过的动作（超过 :data:`AI_ACTION_GRACE` 返回空串）。"""

        picked = getattr(self, attr, None)
        if not picked:
            return ""
        try:
            stamp, name = picked
            if time.monotonic() - float(stamp) > AI_ACTION_GRACE:
                return ""
        except (TypeError, ValueError):
            return ""
        return str(name or "")

    def _arm_gaze_reset(self) -> None:
        """挂（或重挂）视线超时：4~6 秒随机档后滑回默认注视「看向镜头」。

        参考 cortico-world-vtuber 的 gaze 状态机：超时档位取随机值而不是
        固定秒数——固定值会让"瞟一眼弹幕 → 回镜头"显得像上了发条。
        新视线指令会取消旧的超时任务，以最后一条记号为准。
        """

        import random

        task = getattr(self, "_gaze_reset_task", None)
        if task is not None and not task.done():
            task.cancel()
        delay = random.uniform(4.0, 6.0)
        self._gaze_reset_task = asyncio.create_task(
            self._gaze_reset_later(delay), name="live-gaze-reset"
        )

    async def _gaze_reset_later(self, delay: float) -> None:
        """超时滑回默认注视。"""

        try:
            await asyncio.sleep(delay)
            engine = self._engine
            if engine is not None and engine.running:
                await engine.set_gaze(0.0, 0.0)
                logger.debug("[直播] 视线超时，滑回看向镜头")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 回正失败不影响其它功能
            logger.debug(f"[直播] 视线回正失败：{type(exc).__name__}: {exc}")

    async def _run_actions(self, markers: list) -> None:
        """把摘出来的记号派给演出引擎（异常只记日志，绝不影响打字幕）。"""

        engine = self._engine
        if engine is None:
            return
        actions = self._actions_snapshot().get("actions") or []
        for marker in markers[:MAX_ACTIONS_PER_REPLY]:
            kind = action_kind_of(marker.name, actions)
            key = canonical_action_of(marker.name, actions) or marker.name
            try:
                if kind == "pose":
                    result = await engine.set_pose(key)
                elif kind == "expression":
                    result = await engine.set_expression(key)
                elif kind == "prop":
                    result = await engine.set_prop(key, 45.0)
                elif kind == "tag":
                    ok = await engine.play_tag(key)
                    result = {"ok": ok, "name": key}
                elif kind == "gaze":
                    vec = gaze_vector_of(key)
                    if vec is None:
                        logger.debug(f"[直播] 视线目标「{key}」没有向量定义，跳过")
                        continue
                    result = await engine.set_gaze(vec[0], vec[1])
                    # cortico 的 gaze 状态机：4~6 秒（随机档）没有新指令就滑回
                    # 默认注视「看向镜头」——不然她会一直歪着头盯住一个方向。
                    self._arm_gaze_reset()
                elif kind == "reset":
                    # cortico 台本的【Reset】：换话题时一键收起表情/道具/姿势
                    result = {"ok": await engine.reset_stage(), "name": key}
                elif kind == "direction":
                    # breeze 的 voice direction：记下英文指令，speak() 合成
                    # 这一条短句时覆盖配置里的表演指令（只在那一生效）。
                    instruction = direction_instruction_of(key)
                    if instruction is None:
                        continue
                    self._pending_direction = instruction
                    result = {"ok": True, "name": key}
                else:
                    logger.debug(f"[直播] 记号「{marker.name}」不在当前模型的动作表里，跳过")
                    continue
            except Exception as exc:  # noqa: BLE001 - 一个动作失败不影响别的
                logger.warning(f"[直播] 动作「{marker}」执行失败：{type(exc).__name__}: {exc}")
                continue
            ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
            logger.info(
                "[直播] 动作记号 %s -> %s %s（%s）",
                marker, kind, key, "成功" if ok else (result.get("error") if isinstance(result, dict) else "失败"),
            )

    def _message_without_markers(self, message: Dict[str, Any], names: set) -> Dict[str, Any]:
        """把出站消息里的记号摘掉，观众看到的只有台词。"""

        cleaned = dict(message)
        segments = message.get("raw_message")
        if isinstance(segments, list):
            new_segments = []
            for item in segments:
                if isinstance(item, dict) and str(item.get("type") or "").lower() == "text":
                    data = item.get("data")
                    if isinstance(data, str):
                        stripped, _ = split_actions(data, names)
                        if stripped != data:
                            item = {**item, "data": stripped}
                new_segments.append(item)
            cleaned["raw_message"] = new_segments
        plain = message.get("processed_plain_text")
        if isinstance(plain, str):
            stripped, _ = split_actions(plain, names)
            if stripped != plain:
                cleaned["processed_plain_text"] = stripped
        return cleaned

    @HookHandler(
        "send_service.before_send",
        name="live_stream_action_markers",
        description="把台词里的动作记号（【兔牙】/<点头>）摘出来交给演出引擎，再让干净台词去播报",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def action_marker_hook(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """出站前处理动作记号。

        顺序很关键：本钩子是 ``NORMAL``，比 ``auto_speak``（``LATE``）先跑，
        所以 TTS/字幕拿到的是已经摘干净的台词；派发用 ``create_task``，
        不让引擎的 HTTP 调用拖慢发送。
        """

        try:
            message = kwargs.get("message")
            if not isinstance(message, dict):
                return None
            stream_id = str(kwargs.get("stream_id") or "")
            if not self._is_live_stream_id(stream_id) and not self._platform_allowed(message):
                return None
            # 语气指令是"每条短句独立"的：这条消息没写语气记号就用默认。
            # （action_marker 是 NORMAL、auto_speak 是 LATE，同一消息内先清后设
            # 再由 speak 取走，不会串到下一条。）
            self._pending_direction = None
            await self._warm_actions()
            names = self._action_names()
            if not names:
                return None
            text = self._spoken_text(message)
            if not text:
                return None
            clean, markers = split_actions(text, names)
            if not markers:
                return None
            # 语气记号必须同步处理：auto_speak（LATE）在下一个钩子里就会合成，
            # 而 create_task(_run_actions) 的执行顺序不保证在它之前。
            # direction_instruction_of 只查内置表，不需要模型动作表。
            for marker in markers:
                instruction = direction_instruction_of(marker.name)
                if instruction is not None:
                    self._pending_direction = instruction
            # 记号是 AI 的显式决定：记下它点过的表情/道具，稍后关键词那层
            # 不许再猜一遍把它盖掉（auto_speak 是 LATE，比本钩子后跑）。
            self._remember_ai_actions(markers)
            asyncio.create_task(self._run_actions(markers), name="live-actions")
            logger.info(
                "[直播] 摘到 %d 个动作记号：%s（台词 %d → %d 字）",
                len(markers), "、".join(str(m) for m in markers), len(text), len(clean),
            )
            return {"modified_kwargs": {**kwargs, "message": self._message_without_markers(message, names)}}
        except Exception as exc:  # noqa: BLE001 - 记号处理失败不能挡住发送
            logger.debug(f"[直播] 动作记号钩子异常：{exc}")
            return None

    @HookHandler(
        "send_service.before_send",
        name="live_stream_auto_speak",
        description="把机器人的文字回复合成为语音，播到 VTS 监听的声卡上",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=120000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def auto_speak_hook(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """出站拦截：把机器人回复合成成语音播出来。

        这个钩子只播报、不改写消息：返回 None 让原文字消息照常发出，
        语音只是额外播到虚拟声卡（VTS 靠它驱动嘴型）。
        任何一步出问题都放行文字，绝不吞掉回复。
        """

        try:
            message = kwargs.get("message")
            if not isinstance(message, dict):
                return None
            self._learn_live_stream_from_outbound(message, kwargs.get("stream_id"))
            # 这一轮是不是主动自言自语：必须在解除隔离之前判断，解除后现场就没了。
            # 只有「回应观众」的段才发续聊额度，自语段不发，链长才有上限。
            proactive_turn = self._proactive_turn_active(str(kwargs.get("stream_id") or "").strip())
            # 出站就说明这一轮已经说完了（不管 TTS 开没开）：解除忙窗口，
            # 同时结束主动轮的上下文隔离，别让下一轮正常的弹幕回复看不到弹幕。
            self._proactive_inflight_until = 0.0
            self._disarm_proactive_isolation()
            config = self.config
            text = self._spoken_text(message)
            # 兜底：这一轮的上下文里只有自己刚说的话、没有任何观众消息。
            # 规则已要求模型 wait，万一它仍吐出一句「接自己话」的文本，
            # 文字照常发出（不 abort，避免诱发 reply 发送失败重试），
            # 但不合成语音、也不上字幕 —— 观众根本听不到这句自问自答。
            if self._platform_allowed(message) and time.monotonic() < self._self_echo_suppress_until:
                logger.info("[直播] 空转轮（只有自己刚说的话）：跳过播报与字幕，不接自己的话")
                return None
            # 宿主的「错别字纠正」会另发一条只有一两个字的消息，还引用自己
            # 上一条（例：正文只有「叫」）。直播里单独念一个字、字幕闪一帧
            # 很出戏，这里直接跳过播报；文字消息本身照常发出，不干预宿主。
            if self._is_correction_fragment(message, text):
                logger.debug(f"[直播] 跳过错别字纠正碎片不播报：{text!r}")
                return None
            is_error = bool(text) and self._is_llm_error(text)
            limit = int(config.tts.auto_speak_max_length)
            too_long = bool(text) and limit > 0 and len(text) > limit
            tts_capable = (
                config.tts.enabled
                and config.tts.auto_speak_reply
                and self._tts_client is not None
                and self._player is not None
                and self._platform_allowed(message)
            )
            will_speak = bool(text) and not is_error and not too_long and tts_capable
            # 字幕必须和语音同步：会出声的句子由 speak() 在音频首包到达那一刻推，
            # 这里只补「不会出声」的情况（TTS 关掉 / 过长被跳过），免得字幕空着。
            if self._subtitle is not None and text and not is_error and not will_speak:
                if self._platform_allowed(message):
                    self._publish_subtitle(
                        strip_vocal_events(text), auto_clear_after=max(3.0, len(text) / 6.0)
                    )
            if not tts_capable:
                return None
            if not text:
                return None
            if is_error:
                logger.info(f"[直播] 跳过报错文本不播报：{text[:50]}")
                return None
            if too_long:
                logger.info(f"[直播] 回复过长（{len(text)} 字），跳过自动播报")
                return None

            # 宿主开启回复断句后，一条回复会被拆成多条消息依次送到这里。
            # 先登记排队再交给串行队列：字幕按句子顺序切换，且从第一条开口
            # 到最后一条播完，_speech_pending 全程 > 0，门控不给 Planner 空隙。
            # gate_until 是分批发送的兜底窗口：每条短句入队都续期，最后一句
            # 播完由 _perform_reply 清零。
            if self._speech_pending == 0:
                # 新的一段话开头：只有「回应观众」的段才发续聊额度，
                # 主动自语段不续，否则又变成「音频多长、话就多密」。
                if not proactive_turn:
                    self._followup_turns_left = max(
                        self._followup_turns_left, self._followup_budget(config)
                    )
            self._speech_pending += 1
            self._speech_gate_until = time.monotonic() + max(
                0.0, float(getattr(config.tts, "speech_gate_hold_seconds", 2.0))
            )
            generation = self._speech_generation
            task = asyncio.create_task(self._perform_reply(text, generation), name="live-reply-perform")
            self._reply_tasks.add(task)
            task.add_done_callback(self._reply_tasks.discard)
        except Exception as exc:  # noqa: BLE001 - 钩子绝不能让回复发不出去
            logger.warning(f"[直播] 自动播报钩子异常：{type(exc).__name__}: {exc}")
        return None

    # ------------------------------------------------------------------ #
    # 日程注入（从 schedule-hub 合并）：Planner/Replyer 发请求前插时段表
    # ------------------------------------------------------------------ #
    def _build_schedule_injection(self, now: Optional[datetime] = None) -> str:
        """system 里的日程段：**只含稳定内容**（规则 + 时段表）。

        缓存关键：这段文本追加在 system（prompt 第 0 条）末尾，它一变，
        后面全部对话历史的缓存就报废。「现在在哪一档」「时段变更」都是
        易变内容，由 :meth:`_append_schedule_tail` 放到对话尾部，
        system 在整个会话期间逐字节不变。
        """

        table = self._schedule_table
        if table is None or table.empty:
            return ""
        return table.prompt_block()

    def _schedule_tail_text(self, now: Optional[datetime] = None) -> str:
        """易变段：当前档 + 一次性时段变更提示（放对话尾部，不碰 system）。"""

        table = self._schedule_table
        if table is None or table.empty:
            return ""
        parts = []
        current_line = table.current_line(now)
        if current_line:
            parts.append(current_line)
        if self._schedule_pending_change:
            parts.append(
                "【时段变更】 已进入「" + self._schedule_pending_change + "」档，新的安排立即生效。"
            )
            self._schedule_pending_change = ""
        return "\n".join(parts)

    def _append_schedule_tail(self, items: Any, now: Optional[datetime] = None) -> tuple[Any, bool]:
        """把易变的日程尾段追加到最后一条文本 part 前（复用轮次规则的尾部机制）。

        同档内尾段内容不变：宿主若每轮给原始历史，每轮都在尾部追加同一行，
        前缀缓存到尾部为止全部命中；换档只 miss 尾部一小段。
        """

        text = self._schedule_tail_text(now)
        if not text:
            return items, False
        return append_round_rule(items, text)

    def _inject_schedule_into_items(self, items: Any) -> Any:
        """把日程段并进 items 里那条已有的 system 消息。

        **不能新建第二条 SystemMessageItem 插到最前面**：Qwen 系列（llama.cpp 内置
        模板）只允许 system 出现在第 0 条，模板里写死了
        `if message.role == "system" and not loop.first → raise_exception`，
        新插一条会把原来的 system prompt 挤到第 1 位，llama-server 直接回 500
        「System message must be at the beginning.」。
        所以这里把日程文本追加到第一条 system 消息的 parts 末尾，
        既保持「system 在开头且只有一条」，又能让日程生效。

        Item 必须完全符合 Context Item 快照协议（meta 里的 item_id / logical_turn_id /
        timestamp 一个都不能少），否则宿主 deserialize_prompt_items 会抛异常，
        整份 items 被丢弃 —— 表现就是「钩子跑了但提示词里什么都没有」。
        """

        text = self._build_schedule_injection()
        if not text or not isinstance(items, list):
            return items

        for index, item in enumerate(items):
            if not isinstance(item, dict) or item.get("item_type") != "SystemMessageItem":
                continue
            parts = item.get("parts")
            if not isinstance(parts, list):
                continue
            # 已经有就别重复插
            if any(
                isinstance(part, dict) and "日程表（必须遵守）" in str(part.get("text") or "")
                for part in parts
            ):
                return items
            # 浅拷贝一条新的 system item，避免把历史里那份 dict 改脏
            merged = dict(item)
            merged["parts"] = list(parts) + [{"type": "text", "text": text}]
            new_items = list(items)
            new_items[index] = merged
            return new_items

        # 整份 items 里没有 system（罕见）：此时插到最前面才是合法的
        injection = {
            "item_type": "SystemMessageItem",
            "meta": {
                "item_id": "schedule-inject-" + uuid.uuid4().hex,
                "logical_turn_id": None,
                "timestamp": datetime.now().isoformat(),
            },
            "parts": [{"type": "text", "text": text}],
        }
        return [injection] + list(items)

    @HookHandler(
        "maisaka.planner.before_request",
        name="live_stream_schedule_planner_inject",
        description="规划前把日程表与时序注入上下文",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def schedule_planner_inject_hook(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """把日程段插进 Planner 的 Context Items。"""

        try:
            config = self.config.schedule
            if not config.enabled or not config.inject_planner:
                return None
            items = kwargs.get("items")
            new_items = self._inject_schedule_into_items(items)
            # 易变的当前档/时段变更放对话尾部：system 前缀全程稳定（缓存友好）
            new_items, _tail_added = self._append_schedule_tail(new_items)
            if new_items is items:
                return None
            # 钩子契约：改写后的参数必须包在 modified_kwargs 里，且必须把原始
            # kwargs 原样带回只覆盖 items——宿主的 modified_kwargs 是整体替换
            # 而非按字段合并，只回 items 会把 session_id 等一并抹掉。
            return {"modified_kwargs": {**kwargs, "items": new_items}}
        except Exception as exc:  # noqa: BLE001 - 注入失败不影响本轮规划
            logger.warning("[日程] Planner 注入失败：%s: %s", type(exc).__name__, exc)
            return None

    @HookHandler(
        "maisaka.replyer.before_request",
        name="live_stream_schedule_replyer_inject",
        description="说话前把日程表与时序注入上下文",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def schedule_replyer_inject_hook(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """把日程段插进 Replyer 的 Context Items。"""

        try:
            config = self.config.schedule
            if not config.enabled or not config.inject_replyer:
                return None
            items = kwargs.get("items")
            new_items = self._inject_schedule_into_items(items)
            # 同 Planner：易变段走尾部，system 前缀稳定
            new_items, _tail_added = self._append_schedule_tail(new_items)
            if new_items is items:
                return None
            return {"modified_kwargs": {**kwargs, "items": new_items}}
        except Exception as exc:  # noqa: BLE001 - 注入失败不影响本轮回复
            logger.warning("[日程] Replyer 注入失败：%s: %s", type(exc).__name__, exc)
            return None

    async def _perform_reply(self, text: str, generation: int = 0) -> None:
        """串行队列里的一条播报：表情 + 合成播放。

        宿主开启回复断句后，同一轮回复会拆成多条消息几乎同时到达。这里用
        ``_reply_queue_lock`` 把它们排成一队，字幕才能按句子顺序一条条换，
        而不是几条并发互相覆盖。``generation`` 用于主播热键打断时让排队中的
        旧句子直接作废。
        """

        spoke = False
        try:
            async with self._reply_queue_lock:
                # 已被打断/热重载：这一批剩下的句子不再念
                if generation != self._speech_generation:
                    logger.debug(f"[直播] 播报已被打断，跳过旧句子：{text[:20]}")
                    return
                try:
                    await self.react_expression(text)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(f"[直播] 自动表情失败：{type(exc).__name__}: {exc}")
                if generation != self._speech_generation:
                    return
                try:
                    spoke = await self.speak(text)
                    logger.info(f"[直播] 自动播报{'成功' if spoke else '未播放'}：{text[:40]}")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(f"[直播] 自动播报失败：{type(exc).__name__}: {exc}")
        except asyncio.CancelledError:
            raise
        finally:
            self._speech_pending = max(0, self._speech_pending - 1)
            if self._speech_pending == 0:
                # 整段最后一句播完：门控立即解除，不额外拖延一秒。
                self._speech_gate_until = 0.0
                # 「说完后停顿」从这一刻起算，而不是从发起任务时起算。
                # 下一段要不要开口，由 _mark_speech_finished 里的「有没有新事件」决定，
                # 不在这里无条件续跑——那会退回「音频多长、话就多密」的自循环。
                self._mark_speech_finished()
                if generation != self._speech_generation:
                    self._schedule_subtitle_clear(self._subtitle_token)

    async def react_expression(self, text: str) -> Optional[str]:
        """按文本内容给模型一个表情（可选附带道具）。"""

        # 延迟埋点：最近一条观众消息进聊天流 → 这轮回复出站（含去抖与 LLM 生成）
        inbound_at = getattr(self, "_last_inbound_at", 0.0)
        if inbound_at:
            logger.info("[直播] 延迟：入站→出站 %.2fs", time.monotonic() - inbound_at)
            self._last_inbound_at = 0.0
        config = self.config
        if not config.performance.auto_expression:
            return None
        engine = self._engine
        if engine is None or not engine.running:
            return None

        # 自动表情保持时长写死 0：由引擎按台词计划三段时序（见 vts/timing.py）
        hold = 0
        extra = parse_emotion_map(config.performance.emotion_map)
        applied: Optional[str] = None
        # AI 已经在台词里用【流泪】这类记号指定过表情：那张脸就是它要的，
        # 这里再按关键词猜一遍只会在后半句把它换掉（写了哭却被「哈哈」判成开心）。
        # 记号缺失时（模型忘了写、或这条不是她的台词）才退回关键词推断。
        chosen = self._ai_action_recent("_ai_expression")
        if chosen:
            applied = chosen
            logger.debug(f"[直播] 本轮表情由台词记号决定（{chosen}），跳过关键词推断")
        else:
            emotion = detect_emotion(text, extra)
            if emotion:
                # 把台词一起带上：引擎按它的强度（感叹号/叠字）和语速计划
                # onset/apex/offset，而不是所有委屈都挂同一个秒数。
                result = await engine.set_expression(emotion, float(hold), text=text)
                if result.get("ok"):
                    applied = str(result.get("name") or "")
                    logger.info(f"[直播] 表情「{emotion}」→ {applied}")
                else:
                    logger.debug(f"[直播] 表情「{emotion}」未匹配：{result.get('error')}")

        if config.performance.allow_props:
            # 道具同理：AI 点了【狗尾巴草】，就不用再按"挠/草"这种字面去猜
            if not self._ai_action_recent("_ai_prop"):
                prop = detect_prop(text)
                if prop:
                    got = await engine.set_prop(prop, float(hold) * 3)
                    if got.get("ok"):
                        logger.info(f"[直播] 道具 → {got.get('name')}")
                    else:
                        logger.debug(f"[直播] 道具「{prop}」未匹配：{got.get('error')}")
        return applied

    async def _react_to_chat(self, event: Any, text: str) -> None:
        """听到直播间正文就给一个反应表情（观众弹幕、主播语音都算）。

        和"回复文本触发表情"互补：不用等她开口，听到那句「我讨厌她」
        脸就先有反应了。判断逻辑在 vts/expression.py 的 detect_reaction()，
        做的是"人的反应"（被凶会委屈、听人吐槽别人会讪笑），而不是照抄观众情绪。
        走纯参数通道（set_emotion），不切 .exp3 表情文件，弹幕刷屏也不会闪。
        """

        # 延迟埋点：记下最近一条观众消息进入聊天流的时刻
        self._last_inbound_at = time.monotonic()
        config = self.config
        if not config.performance.auto_expression:
            return
        engine = self._engine
        if engine is None or not engine.running:
            return
        emotion, reason = detect_reaction(text)
        if not emotion:
            return
        now = time.time()
        gap = max(0.0, float(getattr(config.performance, "react_min_interval", 2.0) or 0.0))
        last_at = getattr(self, "_last_reaction_at", 0.0)
        # 弹幕刷屏时别每条都演：间隔内直接跳过；同一情绪连续出现也压一压
        if gap > 0 and now - last_at < gap:
            return
        if emotion == getattr(self, "_last_reaction_emotion", "") and now - last_at < max(gap, 1.0) * 2:
            return
        self._last_reaction_at = now
        self._last_reaction_emotion = emotion
        hold = max(0.0, float(getattr(config.performance, "react_hold_seconds", 0.0) or 0.0))
        if await engine.set_emotion(emotion, hold, text=text):
            logger.debug(f"[直播] 听到「{text[:24]}」→ 反应表情「{emotion}」（{reason}）")

    def _platform_allowed(self, message: Dict[str, Any]) -> bool:
        """判断这条消息的平台是否在自动播报范围内。"""

        raw = (self.config.tts.auto_speak_platforms or "").strip()
        if not raw:
            return True
        allowed = {item.strip().lower() for item in raw.split(",") if item.strip()}
        platform = str(message.get("platform") or "").strip().lower()
        return platform in allowed

    @classmethod
    def _spoken_text(cls, message: Dict[str, Any]) -> str:
        """从出站消息里取出真正要朗读的正文。"""

        segments = message.get("raw_message")
        if not isinstance(segments, list):
            plain = message.get("processed_plain_text")
            return str(plain or "").strip()

        parts: list[str] = []
        for item in segments:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").strip().lower() != "text":
                continue
            value = item.get("data")
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())

        text = " ".join(parts).strip()
        if not text:
            return ""

        for name in cls._collect_at_names(message):
            mention = f"@{name}"
            if mention in text:
                text = text.replace(mention, " ").strip()
        return text

    @staticmethod
    def _is_correction_fragment(message: Dict[str, Any], text: str) -> bool:
        """识别宿主「错别字纠正」单独发出的碎片消息。

        特征：正文只有 1~2 个字，且 ``reply_to`` 指向一条机器人自己发出的
        消息（出站消息 ID 以 ``send_api_`` 开头）。正常引用弹幕的短回复
        指向 ``Live-*`` / ``live-douyin-*``，不会被误伤。

        根治手段是关掉 ``[chinese_typo]``；这里只是防止配置被改回后
        这类碎片又被 TTS 念出来、写进字幕。
        """

        punctuation = "，。！？、,.!?~…：:；;\"'“”‘’()（）[]【】<>《》-—_"
        cleaned = "".join(ch for ch in str(text or "") if not ch.isspace() and ch not in punctuation)
        if not cleaned or len(cleaned) > 2:
            return False
        reply_to = str(message.get("reply_to") or "").strip()
        return reply_to.startswith("send_api_")

    @staticmethod
    def _is_llm_error(text: str) -> bool:
        """判断这段文本是不是模型/系统的报错，而不是她真的说了话。"""

        content = str(text or "").strip()
        if not content:
            return False
        lowered = content.lower()

        strong = (
            "traceback (most recent call last)",
            "error code:",
            "rate limit",
            "ratelimiterror",
            "insufficient balance",
            "unauthorized",
            "internal server error",
            "hard timeout",
        )
        if any(token in lowered for token in strong):
            return True

        weak = (
            "硬超时",
            "触发超时",
            "调用失败",
            "模型调用出错",
            "请求超时",
            "网络异常",
            "连接失败",
            "余额不足",
            "请求过于频繁",
            "出错了",
        )
        return any(token in content for token in weak)

    @staticmethod
    def _collect_at_names(message: Dict[str, Any]) -> list[str]:
        """取出消息里所有 at 段的目标昵称。"""

        segments = message.get("raw_message")
        if not isinstance(segments, list):
            return []
        names: list[str] = []
        for item in segments:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").strip().lower() != "at":
                continue
            data = item.get("data")
            if isinstance(data, dict):
                value = str(data.get("nickname") or data.get("name") or "").strip()
            else:
                value = str(data or "").strip()
            if value:
                names.append(value)
        return names

    def _breeze_params(self) -> BreezeParams:
        """把插件配置翻译成 Breeze TTS 参数。"""

        config = self.config.tts
        return BreezeParams(
            server=(config.server.strip() or "http://127.0.0.1:9880"),
            timeout=float(config.timeout),
            voice_id=(getattr(config, "voice_id", "") or "engu"),
            cfg_scale=float(getattr(config, "cfg_scale", 1.0) or 1.0),
            temperature=float(getattr(config, "temperature", 0.0) or 0.0),
            split_chars=int(getattr(config, "split_chars", 0) or 0),
            seed=int(getattr(config, "seed", 42)),
        )

    async def _ensure_breeze_voice(self) -> None:
        """启动时把配置里的参考音频注册成 Breeze 保存音色。

        为什么要注册：Breeze 的首字延迟里最大的一块是参考音频编码（约 548ms，
        RTX 3060 实测），编一次的结果可以存成 ``.breeze`` 文件反复用——
        注册后合成走缓存路径，首字延迟从约 900ms 降到约 280ms。

        策略（都以"不阻塞、不折腾"为先）：
        1. 没填参考音频 / 关了自动注册 / 服务没起：直接返回，合成仍用 ``voice_id``；
        2. 先查服务端音色列表：目标名字已存在就跳过（.breeze 文件在重启后仍在）；
        3. 不存在才用参考音频注册，成功后把配置里的 ``voice_id`` 同步成这个名字，
           下一次合成就走缓存路径。

        注册只在启动时做一次；服务端重启也不需要重做（保存音色会随 voices/
        目录自动加载）。
        """

        # 鸭子类型而不是 isinstance：这里只关心「客户端能不能注册音色」。
        # 用 isinstance 会在测试环境里被模块身份绊倒（测试以无包上下文加载
        # plugin.py，from .tts.breeze 和 from tts.breeze 会变成两个模块对象）。
        client = self._tts_client
        register_voice = getattr(client, "register_voice", None)
        list_voices = getattr(client, "list_voices", None)
        health = getattr(client, "health", None)
        if not (register_voice and list_voices and health):
            return
        config = self.config.tts
        # 自动注册音色写死启用：填了参考音频就注册，没有理由关
        # （服务端按音色内容哈希缓存，重复注册不花额外时间）。
        voice_name = str(getattr(config, "voice_name", "") or "").strip()
        if not voice_name:
            return

        # 等服务就绪：Breeze 启动要加载模型 + warmup（约 3~8 秒），前几次探测会失败
        ok, detail = False, ""
        for _ in range(10):
            try:
                ok, detail = await health()
            except Exception:  # noqa: BLE001 - 探测失败就是没就绪
                ok = False
            if ok:
                break
            await asyncio.sleep(3.0)
        if not ok:
            logger.warning(f"[直播] Breeze 未就绪，跳过音色注册：{detail}")
            return

        # 音色已存在（voices/ 重启自动加载）：不用注册，把配置的 voice_id 对齐它即可。
        # 这一步放在"检查参考音频"之前——已存在的音色不需要参考音频也能直接用。
        try:
            voices = await list_voices()
        except Exception as exc:  # noqa: BLE001 - 注册是锦上添花，失败就跳过
            logger.warning(f"[直播] 列音色失败，跳过注册：{exc}")
            return
        existing = {str(item.get("id") or "") for item in voices}
        if voice_name in existing:
            logger.info(f"[直播] Breeze 音色「{voice_name}」已存在，跳过注册")
            self._sync_voice_id(voice_name)
            return

        # 需要真正注册：参考音频和转写一个都不能少
        ref_path = str(getattr(config, "ref_audio_path", "") or "").strip()
        ref_text = str(getattr(config, "ref_text", "") or "").strip()
        if not ref_path:
            logger.info("[直播] 未填参考音频，跳过音色注册（合成继续用 voice_id）")
            return
        if not ref_text:
            logger.warning("[直播] 填了参考音频但没填转写文本，无法注册音色")
            return

        try:
            result = await register_voice(
                ref_path, ref_text, voice_name,
            )
        except Exception as exc:  # noqa: BLE001 - 注册失败不阻塞启动，合成继续用 voice_id
            logger.warning(f"[直播] 注册 Breeze 音色失败（合成会继续用 voice_id）：{exc}")
            return
        seconds = result.get("seconds")
        logger.info(
            "[直播] Breeze 音色「%s」注册成功（%.2fs 参考），后续合成走缓存路径",
            voice_name, float(seconds) if isinstance(seconds, (int, float)) else 0.0,
        )
        self._sync_voice_id(voice_name)

    def _sync_voice_id(self, voice_name: str) -> None:
        """把注册成功的音色名同步进配置的 voice_id（只改内存实例）。"""

        if not voice_name:
            return
        try:
            if self.config.tts.voice_id.strip() != voice_name:
                self.config.tts.voice_id = voice_name
                logger.info(f"[直播] 已切换合成音色为「{voice_name}」")
            client = self._tts_client
            if client is not None and getattr(client, "params", None) is not None:
                if client.params.voice_id != voice_name:
                    client.update({"voice_id": voice_name})
        except Exception as exc:  # noqa: BLE001 - 配置只读时跳过，不影响运行
            logger.debug(f"[直播] 同步音色名失败：{type(exc).__name__}: {exc}")

    def _tts_sample_rate(self) -> int:
        """当前 TTS 输出的采样率（播放器按它初始化）。

        breeze-tts-2 输出 24kHz 裸 PCM；客户端每次响应头里也会更新实际值，
        这里优先读它，拿不到就用已知默认。
        """

        if isinstance(self._tts_client, BreezeClient):
            rate = self._tts_client.sample_rate
            if rate and rate > 0:
                return int(rate)
        return STREAM_SAMPLE_RATE

    def _take_pending_direction(self) -> Optional[str]:
        """取走本句的语气覆盖（voice direction），每句只生效一次。

        Returns:
            英文/自然语言 instruction；None = 没有语气记号，走 breeze 自然朗读。
        """

        instruction, self._pending_direction = self._pending_direction, None
        return instruction

    async def speak(self, text: str) -> bool:
        """合成并播放一句话（内部串行）。"""

        content = (text or "").strip()
        if not content:
            return False
        if not self.config.tts.enabled or self._tts_client is None or self._player is None:
            logger.debug("[直播] TTS 未启用，跳过播报")
            return False
        # 音效标签（[叹气]/[laugh]…）breeze 会演成真实声音，但不该上字幕；
        # AI 编造的词表外标签（[吸鼻子]）breeze 不认识会念出来，TTS 文本里要摘。
        tts_text = strip_unknown_bracket_tags(content)
        subtitle_text = strip_vocal_events(content)

        async with self._speak_lock:
            self._speech_interrupt.clear()
            self._speaking.set()
            # 字幕随真实播放进度逐句推进；progress 由播放器每写一块 PCM 更新
            progress: Dict[str, float] = {"seconds": 0.0}
            subtitle_done = asyncio.Event()
            subtitle_task: Optional[asyncio.Task[None]] = None
            try:
                started = False
                started_at = 0.0
                total_bytes = 0
                spoke = False

                async def _stream():
                    nonlocal started, started_at, total_bytes, subtitle_task
                    request_at = time.monotonic()
                    source = self._tts_client.synthesize_stream(
                        tts_text,
                        instruction_override=self._take_pending_direction(),
                    )
                    try:
                        async for piece in source:
                            # 热键打断：合成流提前收尾，播放随迭代器结束而停下
                            if self._speech_interrupt.is_set():
                                return
                            if not started:
                                started = True
                                started_at = time.monotonic()
                                # 延迟埋点：合成请求 → 第一个 PCM 块（真正能开口的时刻）
                                logger.info("[直播] 延迟：TTS 首包 %.2fs（%d 字）",
                                            started_at - request_at, len(content))
                                subtitle_task = asyncio.create_task(
                                    self._drive_subtitle(subtitle_text, progress, subtitle_done),
                                    name="live-subtitle",
                                )
                            total_bytes += len(piece)
                            yield piece
                    finally:
                        # 流式下提前 return 必须显式关闭，否则 chunked 响应和
                        # 合成锁会一直挂着，下一次播报被卡住。
                        await source.aclose()

                async def _prebuffered(source: Any, seconds: float, rate: int):
                    """攒够 N 秒音频再开播，之后到货即送。

                    0 秒时纯透传（首字最快）；不为 0 时用攒下的余量吸收
                    合成/播放链路的抖动，代价是开播推迟同样的秒数。
                    注意打断检查在 source（_stream）内部，这里无需重复。
                    """

                    if seconds <= 0:
                        async for piece in source:
                            yield piece
                        return
                    need = int(seconds * rate * STREAM_BYTES_PER_SAMPLE)
                    buffer = bytearray()
                    async for piece in source:
                        buffer.extend(piece)
                        if len(buffer) >= need:
                            yield bytes(buffer)
                            buffer.clear()
                            break
                    else:
                        # 整段音频都没攒够预缓冲就结束了：全部一次给出
                        if buffer:
                            yield bytes(buffer)
                        return
                    async for piece in source:
                        yield piece

                try:
                    # breeze 输出 24kHz 裸 PCM；播放器必须按它初始化。
                    sample_rate = self._tts_sample_rate()
                    prebuffer = max(
                        0.0, float(getattr(self.config.tts, "buffer_seconds", 0.0) or 0.0)
                    )
                    played = await self._player.play_stream(
                        _prebuffered(_stream(), prebuffer, sample_rate),
                        sample_rate=sample_rate,
                        on_progress=lambda seconds: progress.__setitem__("seconds", seconds),
                    )
                except BreezeError as exc:
                    self._stats["tts_errors"] += 1
                    logger.warning(f"[直播] TTS 合成失败：{exc}")
                    return False
                except Exception as exc:  # noqa: BLE001 - 播报失败不能影响其它功能
                    self._stats["tts_errors"] += 1
                    logger.error(f"[直播] TTS 合成异常：{type(exc).__name__}: {exc}")
                    return False

                if played and started and total_bytes > 0:
                    audio_seconds = total_bytes / float(sample_rate * STREAM_BYTES_PER_SAMPLE)
                    remaining = audio_seconds - (time.monotonic() - started_at)
                    # 等待真实音频播完（设备写完 ≠ 播完）；被热键打断时立刻让出
                    deadline = time.monotonic() + max(0.0, remaining)
                    while True:
                        left = deadline - time.monotonic()
                        if left <= 0 or self._speech_interrupt.is_set():
                            break
                        await asyncio.sleep(min(0.1, left))
                    self._stats["last_audio_seconds"] = round(audio_seconds, 3)
                    # 用真实时长校准「每字多少秒」，下一段字幕就不会越跑越偏
                    chars = max(1, len(content))
                    observed = audio_seconds / chars
                    if 0.08 <= observed <= 0.6:
                        self._subtitle_seconds_per_char = 0.6 * self._subtitle_seconds_per_char + 0.4 * observed

                if started:
                    self._stats["last_spoken"] = content
                if played:
                    self._stats["spoken"] += 1
                return played
            finally:
                # 让字幕任务按最后一句收尾（它会把最后一句补出来），再清状态
                subtitle_done.set()
                if subtitle_task is not None and not subtitle_task.done():
                    try:
                        await asyncio.wait_for(subtitle_task, timeout=0.4)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        subtitle_task.cancel()
                self._speaking.clear()
                # 字幕严格跟着这一句音频。队列里还有后续短句时先别撤，
                # 让下一句的首包直接覆盖，避免句间字幕闪一下空白；
                # 最后一句播完（或整段被打断）才真正清除。
                if self._speech_pending <= 1:
                    self._schedule_subtitle_clear(self._subtitle_token)

    # ------------------------------------------------------------------ #
    # VTS 连接
    # ------------------------------------------------------------------ #

    async def _connect_vts(self) -> bool:
        """连接 VTube Studio（必要时弹窗授权），成功后保留客户端供查询/热键用。"""

        config = self.config.performance
        host = config.vts_host.strip() or "127.0.0.1"
        port = int(config.vts_port) or 8001
        if config.auto_discover:
            host, port = await auto_discover(host)

        client = VTSClient(
            host=host,
            port=port,
            plugin_name=config.plugin_name.strip() or "MaiBot Live Stream Companion",
            plugin_developer=config.plugin_developer.strip() or "MaiBot",
        )
        try:
            token = self._effective_token()
            need_save = False
            if token and await client.authenticate(token):
                logger.info("[VTS] 使用已保存的 Token 认证成功")
            else:
                if token:
                    logger.warning("[VTS] 已保存的 Token 失效（可能在 VTS 里被撤销），重新申请授权")
                token = await self._request_token_with_wait(client)
                need_save = True
                if not await client.authenticate(token):
                    raise VTSConnectionError("VTS 授权后认证仍未通过")
            if need_save:
                self._save_token(token)
                self._active_token = token
        except VTSClientError as exc:
            logger.warning(f"[VTS] 初始化失败：{exc}")
            await client.disconnect()
            self._stats["vts_host"] = host
            self._stats["vts_port"] = port
            old = self._vts_client
            self._vts_client = None
            if old is not None:
                await old.disconnect()
            return False
        except asyncio.CancelledError:
            # 热重载/卸载时后台连接任务被取消：必须断开当前客户端，
            # 否则 VTS 里会残留「插件连接」，累积成多个连接、互相打架（抽搐）。
            try:
                await client.disconnect()
            except Exception:
                pass
            raise

        old = self._vts_client
        self._vts_client = client
        if old is not None and old is not client:
            try:
                await old.disconnect()
            except Exception:
                pass
        self._stats["vts_host"] = host
        self._stats["vts_port"] = port
        logger.info(f"[VTS] 已连接 {host}:{port}")
        return True

    # ------------------------------------------------------------------ #
    # VTS 后台自动重连
    # ------------------------------------------------------------------ #

    def _start_vts_reconnect(self) -> None:
        """启动 VTS 后台重连：先开 MaiBot、后开 VTS 也能自动连上，断线也能恢复。"""

        if self._vts_reconnect_task is None or self._vts_reconnect_task.done():
            self._vts_reconnect_task = asyncio.create_task(self._vts_reconnect_loop(), name="live-vts-reconnect")

    async def _stop_vts_reconnect(self) -> None:
        """停掉 VTS 后台重连任务。"""

        task, self._vts_reconnect_task = self._vts_reconnect_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _vts_reconnect_loop(self) -> None:
        """每 10 秒检查一次 VTS：没连上就重试，连上后补启动演出引擎。

        覆盖两个场景：
        1. 用户先开 MaiBot、后开 VTS——on_load 时连不上，等 VTS 起来后这里补连；
        2. VTS 中途退出/网络抖动断线——自动重连，不用手动重载插件。
        连上后引擎若因当时没 Token 没起来，也在这里一并补启动。
        """

        first = True
        while True:
            if first:
                first = False  # 首次立即尝试连接，不等 10 秒
            else:
                try:
                    await asyncio.sleep(10.0)
                except asyncio.CancelledError:
                    raise

            client = self._vts_client
            if client is not None and client.is_connected:
                continue  # 正常连着，什么都不做

            # 已断线：清掉旧客户端，重新走连接流程
            if client is not None:
                self._vts_client = None
                try:
                    await client.disconnect()
                except Exception:
                    pass

            try:
                ok = await self._connect_vts()
                if ok:
                    logger.info("[VTS] 后台已自动连接")
                    if self.config.performance.enabled and (self._engine is None or not self._engine.running):
                        await self._start_engine()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 重连失败等下一轮再试
                logger.debug(f"[VTS] 后台重连尝试失败：{type(exc).__name__}: {exc}")

    async def _request_token_with_wait(self, client: VTSClient, timeout: float = 90.0) -> str:
        """申请 VTS 授权 Token；弹窗未点时会等待用户操作。"""

        import asyncio
        import time as _time

        deadline = _time.time() + timeout
        logged = False
        while True:
            try:
                return await client.request_auth_token()
            except VTSResponseError as exc:
                message = str(exc)
                if "APIError 51" not in message:
                    raise
                if not logged:
                    logger.warning(
                        "[VTS] VTube Studio 里有一个授权窗口正开着：请切到 VTS 点「允许 / Allow」，插件会自动继续"
                    )
                    logged = True
                if _time.time() >= deadline:
                    raise VTSConnectionError(
                        "等待 VTS 授权超时：请切换到 VTube Studio 窗口点击「允许 / Allow」后重载插件"
                    ) from exc
                await asyncio.sleep(2.0)

    # ------------------------------------------------------------------ #
    # 演出引擎
    # ------------------------------------------------------------------ #

    async def _start_engine(self) -> bool:
        """按配置启动有机演出引擎。"""

        config = self.config
        token = self._effective_token()
        if not token:
            logger.warning(
                "[演出引擎] 还没有 VTS Token：插件加载时会自动向 VTS 申请授权，请先确认 VTS 已启动并允许授权"
            )
            return False

        engine = PerformanceEngineManager(
            port=int(config.performance.engine_port),
            vts_host=self._stats["vts_host"] or config.performance.vts_host,
            vts_port=self._stats["vts_port"] or config.performance.vts_port,
            token=token,
            python=config.performance.python,
            plugin_name=config.performance.plugin_name,
            developer=config.performance.plugin_developer,
            auto_props=bool(getattr(config.performance, "auto_props", True)),
        )
        started = await engine.start()
        self._engine = engine
        if started:
            await asyncio.sleep(1.5)
            if not await engine.probe():
                logger.warning("[演出引擎] 进程已启动但控制接口无响应，请查看日志")
                return started
            detail = await engine.engine_status()
            raw = detail.get("detail") or "{}"
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except ValueError:
                parsed = {}
            if parsed and not parsed.get("vts_connected", True):
                logger.warning(
                    "[演出引擎] 已启动，但引擎连不上 VTS：%s。通常在 VTS 里重新授权一次即可",
                    parsed.get("vts_error") or "原因未知",
                )
            elif parsed:
                logger.info(
                    "[演出引擎] 已在驱动 VTS（纯参数模式）：抖动自检 %s 帧 / 超限 %s 次",
                    parsed.get("jitter_frames", 0),
                    parsed.get("jitter_over_limit", 0),
                )
        return started

    # ------------------------------------------------------------------ #
    # 命令与 API
    # ------------------------------------------------------------------ #

    @Command("live_status", description="查看直播适配器状态", pattern=r"^/live_status$")
    async def handle_status(self, stream_id: str = "", **kwargs: Any):
        """回复插件运行状态（弹幕链路 + 表现层）。"""

        del kwargs
        config = self.config
        lines = ["直播适配器状态"]

        # 弹幕链路
        channel = self._channel
        if channel is not None:
            status = channel.status()
            lines.append(f"弹幕网关：{'就绪' if status['ready'] else '未就绪'}")
            lines.extend(channel.source_summary())
            lines.append(
                f"已注入 {status['injected']} 条，防刷屏丢弃 {status['dropped']} 条，"
                f"拒收 {status['failed']} 条，队列积压 {status['queue_size']} 条"
            )
            if status["last_error"]:
                lines.append(f"最近错误：{status['last_error']}")
        else:
            lines.append("弹幕链路：未启动")

        # 表现层
        engine_status = self._engine.status() if self._engine is not None else {"running": False}
        lines.append(f"Breeze TTS：{config.tts.server}（{'启用' if config.tts.enabled else '关闭'}）")
        lines.append(f"播放设备：{self._effective_device() or '系统默认'}")
        lines.append(
            f"VTS：{self._stats['vts_host']}:{self._stats['vts_port']}（{'已连接' if self._vts_client else '未连接'}）"
        )
        lines.append(f"演出引擎：{'运行中' if engine_status.get('running') else '未运行'}")
        lines.append(f"语音播报 {self._stats['spoken']} 次，合成失败 {self._stats['tts_errors']} 次")
        if config.subtitle.enabled:
            subtitle = self._subtitle
            lines.append(
                f"字幕：{subtitle.url if subtitle is not None else '未启动'}"
                f"（上句音频 {self._stats.get('last_audio_seconds', 0)} 秒，字幕等长）"
            )
        if config.danmaku.enabled:
            danmaku = self._danmaku
            lines.append(f"弹幕墙：{danmaku.url if danmaku is not None else '未启动'}")
        if self._stats["last_spoken"]:
            lines.append(f"最近播报：{self._stats['last_spoken'][:60]}")

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "已回复直播适配器状态", True

    @Command("schedule", description="查看当前时段和完整日程表", pattern=r"^/(schedule|日程)(\s|$)")
    async def handle_schedule(self, stream_id: str = "", **kwargs: Any):
        """回复当前时段。"""

        del kwargs
        table = self._schedule_table
        if table is None or table.empty:
            await self.ctx.send.text("还没有配置日程表。", stream_id)
            return True, "未配置", True

        current = table.current()
        lines = [table.current_line(), "", "完整时段表："]
        for period in table.periods:
            mark = "▶" if current and period.name == current.name else " "
            lines.append(
                mark + " " + format_hhmm(period.start) + " " + period.name + "：" + period.focus
            )
        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "已回复日程", True

    @API("schedule_table", description="读取时段表与当前档位", version="1")
    async def api_schedule_table(self) -> Dict[str, Any]:
        """给页面/其它插件查日程。"""

        table = self._schedule_table
        if table is None or table.empty:
            return {"ok": True, "empty": True, "periods": [], "current": None}
        current = table.current()
        upcoming, seconds = table.next_boundary()
        return {
            "ok": True,
            "empty": False,
            "periods": [
                {"start": format_hhmm(period.start), "name": period.name, "focus": period.focus}
                for period in table.periods
            ],
            "current": {"name": current.name, "focus": current.focus} if current else None,
            "current_line": table.current_line(),
            "next": {
                "name": upcoming.name,
                "start": format_hhmm(upcoming.start),
                "in_seconds": round(seconds, 1),
            } if upcoming else None,
        }

    @API("engine_control", description="启停或重启有机演出引擎", version="1")
    async def api_engine_control(self, action: str = "status") -> Dict[str, Any]:
        """演出引擎控制：status / start / stop / restart。"""

        name = str(action or "status").strip().lower()
        if name == "start":
            if self._engine is not None and self._engine.running:
                return {"ok": True, "already": True, "status": self._engine.status()}
            started = await self._start_engine()
            return {"ok": started, "status": self._engine.status() if self._engine else {}}
        if name == "stop":
            engine, self._engine = self._engine, None
            if engine is not None:
                await engine.stop()
            return {"ok": True}
        if name == "restart":
            started = await self._start_engine()
            return {"ok": started, "status": self._engine.status() if self._engine else {}}
        if self._engine is None:
            return {"ok": True, "status": {"running": False}, "engine": {"reachable": False}}
        return {
            "ok": True,
            "status": self._engine.status(),
            "engine": await self._engine.engine_status(),
        }

    @API("list_expressions", description="列出当前模型可用的表情与道具", version="1")
    async def api_list_expressions(self) -> Dict[str, Any]:
        """读取当前 VTS 模型真实可用的情绪表情与动画道具。"""

        engine = self._engine
        if engine is None or not engine.running:
            return {"ok": False, "error": "演出引擎未运行", "emotions": [], "props": []}
        data = await engine.list_expressions()
        return data

    @API("set_expression", description="让模型切换到指定表情", version="1")
    async def api_set_expression(self, emotion: str = "", hold: float = 12.0) -> Dict[str, Any]:
        """按情绪词切换表情。"""

        engine = self._engine
        if engine is None or not engine.running:
            return {"ok": False, "error": "演出引擎未运行"}
        return await engine.set_expression(str(emotion or ""), float(hold))

    @API("clear_expression", description="收起当前表情", version="1")
    async def api_clear_expression(self) -> Dict[str, Any]:
        """收起所有表情。"""

        engine = self._engine
        if engine is None or not engine.running:
            return {"ok": False, "error": "演出引擎未运行"}
        ok = await engine.clear_expression()
        return {"ok": ok}

    @API("set_prop", description="打开或收起模型道具（狗尾巴草等）", version="1")
    async def api_set_prop(self, prop: str = "", hold: float = 60.0) -> Dict[str, Any]:
        """打开或收起动画道具。"""

        engine = self._engine
        if engine is None or not engine.running:
            return {"ok": False, "error": "演出引擎未运行"}
        return await engine.set_prop(str(prop or ""), float(hold))

    @API("engine_tag", description="让演出引擎做一个手势或情绪", version="1")
    async def api_engine_tag(self, tag: str = "nod", intensity: float = 1.0) -> Dict[str, Any]:
        """触发引擎手势（nod / shake / tilt / wink / bounce / wide / pout / smug…）。"""

        engine = self._engine
        if engine is None or not engine.running:
            return {"ok": False, "error": "演出引擎未运行"}
        name = str(tag or "nod").strip() or "nod"
        ok = await engine.play_tag(name, float(intensity))
        return {"ok": ok, "tag": name}

    # ------------------------------------------------------------------ #
    # AI 工具：让 AI 在不说话时也能做表情/动作（Planner 自主调用）
    # ------------------------------------------------------------------ #
    def _live_tool_engine(self):
        """演出控制工具共用的引擎检查；不可用时返回 None（工具返回错误说明）。"""

        engine = self._engine
        if engine is None or not getattr(engine, "running", False):
            return None
        return engine

    @Tool(
        "live_gaze",
        description="让皮套看向一个地方（会自动保持几秒后转回镜头）。"
                    "想表达「去看弹幕」「盯着她」「环顾四周」时用，不用等说话。",
        parameters={"target": {"type": "string", "description":
                    "看向镜头/弹幕/屏幕/上面/下面/左边/右边"}},
        visibility="visible",
    )
    async def tool_live_gaze(self, target: str = "", **kwargs: Any) -> Dict[str, Any]:
        """视线控制（复用台词记号的向量表与超时滑回）。"""

        engine = self._live_tool_engine()
        if engine is None:
            return {"ok": False, "error": "演出引擎未运行"}
        vec = gaze_vector_of(str(target or "").strip())
        if vec is None:
            return {"ok": False, "error": f"未知视线目标「{target}」",
                    "targets": [k for k, _a, _x, _y in GAZE_TARGETS]}
        ok = await engine.set_gaze(vec[0], vec[1])
        self._arm_gaze_reset()
        return {"ok": ok, "target": str(target)}

    @Tool(
        "live_gesture",
        description="让皮套做一个手势/小动作：点头、摇头、歪头、眨眼、翻白眼、"
                    "星星眼、鼓嘴、得意、大笑、惊讶、打哈欠等。"
                    "适合表达反应但不想插话的时候。",
        parameters={"name": {"type": "string", "description":
                    "手势名（中文即可，如 翻白眼/点头/星星眼）"}},
        visibility="visible",
    )
    async def tool_live_gesture(self, name: str = "nod", **kwargs: Any) -> Dict[str, Any]:
        """手势/小动作（引擎按中文别名归一）。"""

        engine = self._live_tool_engine()
        if engine is None:
            return {"ok": False, "error": "演出引擎未运行"}
        ok = await engine.play_tag(str(name or "nod").strip() or "nod")
        return {"ok": ok, "gesture": name}

    @Tool(
        "live_expression",
        description="让皮套切换到某个情绪表情（用当前模型真实存在的表情名，"
                    "如 开心/无语/流泪/生气；不确定可先不填情感只做手势）。",
        parameters={"emotion": {"type": "string", "description": "表情/情绪名"}},
        visibility="visible",
    )
    async def tool_live_expression(self, emotion: str = "", **kwargs: Any) -> Dict[str, Any]:
        """表情切换（走引擎的模型标定）。"""

        engine = self._live_tool_engine()
        if engine is None:
            return {"ok": False, "error": "演出引擎未运行"}
        result = await engine.set_expression(str(emotion or "").strip())
        ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
        return {"ok": ok, "emotion": emotion, "detail": result}

    @Tool(
        "live_reset_stage",
        description="把皮套恢复到中性：收起所有表情/道具/姿势，视线转回镜头。"
                    "换话题、或者觉得她身上挂着的表情/道具已经不合适时用。",
        visibility="visible",
    )
    async def tool_live_reset_stage(self, **kwargs: Any) -> Dict[str, Any]:
        """场景重置。"""

        engine = self._live_tool_engine()
        if engine is None:
            return {"ok": False, "error": "演出引擎未运行"}
        return {"ok": await engine.reset_stage()}


def _extract_plain_text(message: Dict[str, Any]) -> str:
    """从标准消息字典里取出纯文本，用于日志展示。"""

    segments = message.get("raw_message")
    if not isinstance(segments, list):
        return ""
    return "".join(
        str(item.get("data") or "") for item in segments if isinstance(item, dict) and item.get("type") == "text"
    )


def create_plugin() -> LiveDanmakuAdapter:
    """创建插件实例（Runner 入口）。"""

    return LiveDanmakuAdapter()
