# -*- coding: utf-8 -*-
"""主动对话：纯事件触发 + 一句「自言自语」意图。

设计约束（按实际链路）：
  * **与弹幕完全解耦**：本模块不收集、不保存、不读取、不引用任何直播间
    消息（弹幕 / 礼物 / 醒目留言 / 上舰 / 进场 / 主播语音）；
  * 开口时机只看事件（游戏推进、日程换挡、播完且有新事件），没有任何轮询
    定时器；只有一个可选的「说完后停顿」防止刷屏；
  * 说话内容由 AI 自己决定；没有安排时只给「这一轮必须开口」的触发机制，
    不代写台词、不提供话题素材；
  * 有日程安排时只交代「这一档该做什么」，并要求先用浏览器工具把事做了再
    开口；不写脚本、不给二选一，也不依赖任何特定网站插件；
  * 主动轮仍会经过 Maisaka 的 Planner 与 Replyer，而它们的上下文里天然带着
    历史弹幕，真正的隔离由插件的三个请求前 Hook 完成（见 plugin.py 的
    ``strip_live_context_items`` 与 ``_proactive_isolation``）。
"""

from __future__ import annotations

from typing import Optional

import time


class ProactiveDirector:
    """决定什么时候主动开口、给 planner 什么意图。

    只负责计时与意图文案，真正的触发由插件调用
    ``ctx.maisaka.trigger_proactive`` 完成，这样单测不需要机器人运行时。
    """

    def __init__(self, *, min_interval_seconds: int = 0) -> None:
        """初始化。

        Args:
            min_interval_seconds: 「上一整段播完之后」再等多少秒才允许下一轮
                开口；0 表示说完就接。计时起点由插件在播完时调用
                ``mark_spoken()`` 打入，**不是**发起任务的时刻。
        """

        self.min_interval_seconds = max(0, int(min_interval_seconds))
        self._last_speak_at = 0.0

    # ------------------------------------------------------------------ #
    # 开口时机
    # ------------------------------------------------------------------ #

    def should_speak(self, now: Optional[float] = None) -> tuple[bool, str]:
        """判断现在该不该主动开口。

        没有任何时间窗与次数上限，也不看直播间是否有新消息。
        唯一可能拦住它的是「播完后的冷却」（配置为 0 时连这个也没有）。

        Returns:
            tuple[bool, str]: (是否该说, 不说的原因)
        """

        stamp = now if now is not None else time.time()
        if self.min_interval_seconds <= 0:
            return True, ""
        if self._last_speak_at:
            elapsed = stamp - self._last_speak_at
            if elapsed < self.min_interval_seconds:
                return False, f"刚说完，停顿中（还差 {int(self.min_interval_seconds - elapsed)} 秒）"
        return True, ""

    def mark_spoken(self, now: Optional[float] = None) -> None:
        """记一次「整段说完」：冷却从这里开始计时。"""

        self._last_speak_at = now if now is not None else time.time()

    # ------------------------------------------------------------------ #
    # 意图
    # ------------------------------------------------------------------ #

    def build_intent(
        self, game_summary: str = "", schedule_focus: str = "", followup: bool = False
    ) -> str:
        """构造交给 Maisaka 的意图说明。

        刻意不提「弹幕」二字：一旦写了「不用管弹幕」，小模型反而会去找弹幕
        接话。只保留「必须说话」这一句触发机制（开放式指令下它十次有六次会
        直接 stop 结束本轮）。

        Args:
            game_summary: 正在玩的游戏的客观事实（来自 game-hub）。为空表示
                现在没在玩游戏，回到「讲抽象小故事」的自由发挥。
            schedule_focus: 日程插件推来的「当前档该做的事」。非空时优先按
                安排行动，并且要求真的调用工具去做，而不是只嘴上说说。
            followup: 这是「刚回完观众、接着自己找话题」的续聊轮：没有别的
                素材时要顺着上一句往下说，而不是另起一段新故事。

        Returns:
            str: 给 Planner 的意图文本。
        """

        focus = schedule_focus.strip()
        if focus:
            # 日程是用户自己写进配置的硬安排，不是话术提示：只交代「该做什么」
            # 和「先动手」，不代写台词、不给出戏的二选一。
            return (
                "现在轮到你按节目安排做事，别等别人来问，也别去回应任何人。\n"
                "\n"
                f"这一档要做的事：{focus}\n"
                "\n"
                "先动手把这件事真的做了：用浏览器工具打开对应网页；"
                "工具清单里没有，就先调用 tool_search 搜「打开网页」「读取页面」的工具。"
                "打开之后必须再调用 browser_snapshot（带 depth=4）读一遍页面，"
                "看清标题和内容再开口讲，别拿别的画面凑，也别凭空编。\n"
                "打开一个网页就够了，别反复导航；读完立刻开口，这一轮必须说出一句话。\n"
                "\n"
                "这一轮不要直接 stop 或 wait，也不要一件事都没做就结束。"
            )

        if followup:
            # 刚回完观众：接着刚才那句往下说，别另起一段，也别提「观众/弹幕」
            # 这些被隔离掉的词，否则模型会反过来找弹幕接话。
            if game_summary.strip():
                return (
                    "现在轮到你接着说，别等别人来问，也别去回应任何人。\n"
                    "\n"
                    f"{game_summary.strip()}\n"
                    "\n"
                    "刚才那段已经说完了，顺着它往下讲：补充细节、抖个包袱、"
                    "或者自然换个话题都行，别复读刚才说过的原句。\n"
                    "\n"
                    "这一轮必须说话：直接把你要说的话写出来，"
                    "不要用 stop 结束本轮，也不要用 wait 等下一轮。"
                )
            return (
                "现在轮到你接着说，别等别人来问，也别去回应任何人。\n"
                "\n"
                "刚才那段已经说完了，顺着它往下讲：补充细节、抖个包袱、"
                "编个更离谱的后续，或者自然换个相关话题都行，别复读刚才说过的原句。\n"
                "\n"
                "这一轮必须说话：直接把你要说的话写出来，"
                "不要用 stop 结束本轮，也不要用 wait 等下一轮。"
            )

        if game_summary.strip():
            # 只交代「你在玩什么」，不给解说角度、不给二选一。
            # 提示越具体，模型越容易照着念，听起来像在演提词器。
            return (
                "现在轮到你开口，别等别人来问，也别去回应任何人。\n"
                "\n"
                f"{game_summary.strip()}\n"
                "\n"
                "这一轮必须说话：直接把你要说的话写出来，"
                "不要用 stop 结束本轮，也不要用 wait 等下一轮。"
            )

        return (
            "现在轮到你自言自语，别等别人来问，也别去回应任何人。\n"
            "\n"
            "讲一段抽象搞笑、抽象的小故事，或者吹牛逼，想到什么说什么。\n"
            "\n"
            "这一轮必须说话：直接把你要说的话写出来，"
            "不要用 stop 结束本轮，也不要用 wait 等下一轮。"
        )
