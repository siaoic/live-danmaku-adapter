# -*- coding: utf-8 -*-
"""日程功能测试（从 schedule-hub 合并进直播适配器后的行为契约）。

四条契约：
  1. 时段表计算正确：解析、当前档（跨零点取模）、下一个边界；
  2. 注入是把日程段**追加进已有的 system 消息**，不新建第二条 system
     （Qwen 模板不允许 system 出现在第 0 条之后）；
  3. 换挡直连主动对话：记 focus、排一次开口——不再走 HTTP 自环；
  4. 开关语义：trigger_on_change/trigger_on_load 关掉时只换挡不开口。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tomllib
from datetime import datetime
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR))

_MODULE_NAME = "live_danmaku_schedule_test"
_spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
plugin = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = plugin
_spec.loader.exec_module(plugin)

from schedule_model import ScheduleTable, parse_hhmm  # noqa: E402


# --------------------------------------------------------------------- #
# 时段表计算
# --------------------------------------------------------------------- #
def test_parse_and_current_period() -> None:
    assert parse_hhmm("08:00") == 480
    assert parse_hhmm("8:05") == 485
    assert parse_hhmm("25:00") is None
    assert parse_hhmm("abc") is None

    table = ScheduleTable([
        {"start": "20:12", "name": "娱乐档", "focus": "看柯南"},
        {"start": "08:00", "name": "早间档", "focus": "聊天"},
    ])
    # 配置乱序也能按时间排
    assert [p.name for p in table.periods] == ["早间档", "娱乐档"]
    # 白天在早间档，深夜在娱乐档（跨零点取模）
    assert table.current(datetime(2026, 11, 7, 10, 0)).name == "早间档"
    assert table.current(datetime(2026, 11, 7, 23, 0)).name == "娱乐档"
    assert table.current(datetime(2026, 11, 7, 3, 0)).name == "娱乐档"


def test_next_boundary_wraps_midnight() -> None:
    table = ScheduleTable([
        {"start": "08:00", "name": "早间档", "focus": "聊天"},
        {"start": "20:00", "name": "晚间档", "focus": "游戏"},
    ])
    upcoming, seconds = table.next_boundary(datetime(2026, 11, 7, 21, 0))
    assert upcoming.name == "早间档", "过了最后一档，下一个边界应是明天的首档"
    assert seconds > 10 * 3600


def test_prompt_block_is_stable() -> None:
    """系统段只放规则与时段表，不含「现在几点」——否则 prompt cache 每换档就报废。"""

    table = ScheduleTable([{"start": "08:00", "name": "早间档", "focus": "聊天"}])
    block = table.prompt_block()
    assert "日程表（必须遵守）" in block
    assert "08:00 早间档" in block
    now = datetime(2026, 11, 7, 10, 0)
    assert table.prompt_block() == table.prompt_block()


# --------------------------------------------------------------------- #
# 注入：追加进已有 system，不新建第二条
# --------------------------------------------------------------------- #
def _adapter_with_table():
    data = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    adapter = plugin.LiveDanmakuAdapter()
    adapter._plugin_config_instance = plugin.LiveDanmakuConfig.model_validate(data)
    adapter._schedule_table = adapter._build_schedule_table()
    return adapter


def _system_item(text: str = "你是主播。") -> dict:
    return {
        "item_type": "SystemMessageItem",
        "meta": {"item_id": "sys-1", "logical_turn_id": None, "timestamp": "2026-11-07T00:00:00"},
        "parts": [{"type": "text", "text": text}],
    }


def test_injection_appends_to_existing_system_item() -> None:
    adapter = _adapter_with_table()
    adapter._schedule_pending_change = "娱乐档"
    items = [_system_item("你是主播。")]
    # 缓存契约（2026-11-08 二次校准）：日程拆成两条固定 Item——
    # 恒定段（规则+时段表）进规则槽、由 LATE Hook 前置成稳定前缀；
    # 易变段（当前档/时段变更）进唯一一条极短变量尾巴、放绝对末尾。
    # 旧版把 422 字符整条放在新增历史之后，每轮新增消息都把它往后推，
    # 18/20 轮的首个失配就落在这里。
    new_items, _added = adapter._append_schedule_tail(items)

    assert new_items[0] == items[0], "system 必须逐字节不变（缓存前缀）"
    assert len(new_items) == 3, "system + 恒定规则槽 + 变量尾巴"
    by_id = {str((it.get("meta") or {}).get("item_id")): it for it in new_items[1:]}
    assert set(by_id) == {"live-prefix-cache-tail-rule", "live-prefix-cache-volatile"}

    def text_of(item: dict) -> str:
        return "\n".join(
            str(pt.get("text") or "") for pt in item.get("parts", []) if isinstance(pt, dict)
        )

    stable_text = text_of(by_id["live-prefix-cache-tail-rule"])
    volatile_text = text_of(by_id["live-prefix-cache-volatile"])
    assert "日程表（必须遵守）" in stable_text
    assert "当前时段" not in stable_text, "易变内容不能污染恒定前缀"
    assert "当前时段" in volatile_text
    assert "【时段变更】" in volatile_text, "换挡标记没进上下文"
    assert "日程表（必须遵守）" not in volatile_text, "恒定段不该重复进变量尾巴"
    system_text = text_of(new_items[0])
    assert "你是主播。" in system_text, "原有 system 内容必须原样保留"
    assert "日程表" not in system_text, "日程不能进 system（缓存前缀）"
    # 注入后换挡标记要被消费掉（不然每一轮都在喊「刚换挡」）
    assert adapter._schedule_pending_change == ""


def test_injection_is_idempotent() -> None:
    adapter = _adapter_with_table()
    adapter._schedule_pending_change = ""
    once = adapter._inject_schedule_into_items([_system_item()])
    twice = adapter._inject_schedule_into_items(once)
    assert twice == once, "重复注入会让 system 越滚越大"


def test_injection_without_system_item_prepends() -> None:
    adapter = _adapter_with_table()
    items = [{"item_type": "UserMessageItem", "meta": {"item_id": "u1", "logical_turn_id": 1, "timestamp": "t"},
              "parts": [{"type": "text", "text": "hi"}]}]
    new_items = adapter._inject_schedule_into_items(items)
    assert new_items[0]["item_type"] == "SystemMessageItem", "没有 system 时日程必须顶到最前"


# --------------------------------------------------------------------- #
# 换挡直连主动对话（不再走 HTTP 自环）
# --------------------------------------------------------------------- #
def _adapter_for_change(tmp_path: Path, *, on_change=True, on_load=True):
    adapter = _adapter_with_table()
    adapter.config.schedule.trigger_on_change = on_change
    adapter.config.schedule.trigger_on_load = on_load
    calls = []
    adapter._schedule_proactive = lambda reason: calls.append(reason)  # type: ignore[method-assign]
    return adapter, calls


def test_change_directly_schedules_proactive(tmp_path) -> None:
    """换挡直连：记 focus、排一次开口，全程不碰 HTTP。"""

    adapter, calls = _adapter_for_change(tmp_path)
    period = type("P", (), {"name": "娱乐档", "focus": "去b站看柯南"})()
    asyncio.run(adapter._schedule_apply_change(period, "换挡"))

    assert adapter._schedule_focus == "去b站看柯南", "换挡后主动轮意图没更新"
    assert calls == ["日程换挡"]


def test_change_disabled_means_no_proactive_but_mark_still_set() -> None:
    """关掉换挡开口：不排主动对话，但档位变化标记照记（日程仍在约束对话）。"""

    adapter, calls = _adapter_for_change(tmp_path=None, on_change=False)
    period = type("P", (), {"name": "娱乐档", "focus": "看柯南"})()
    asyncio.run(adapter._schedule_apply_change(period, "换挡"))
    assert calls == []
    assert adapter._schedule_focus == "看柯南"


def test_load_reason_respects_its_own_switch() -> None:
    """载入对齐与换挡是两个独立开关。"""

    adapter, calls = _adapter_for_change(tmp_path=None, on_load=False)
    period = type("P", (), {"name": "娱乐档", "focus": "看柯南"})()
    asyncio.run(adapter._schedule_apply_change(period, "载入"))
    assert calls == []

    adapter2, calls2 = _adapter_for_change(tmp_path=None, on_load=True)
    asyncio.run(adapter2._schedule_apply_change(period, "载入"))
    assert calls2 == ["日程换挡"]


def test_config_ships_periods() -> None:
    """config.toml 必须带着时段数据（合并时从 schedule-hub 搬来的）。"""

    data = tomllib.loads((_PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    periods = data["schedule"]["periods"]
    assert periods, "config.toml 里没有时段表"
    assert all({"start", "name", "focus"} <= set(p) for p in periods)


def main() -> int:
    test_parse_and_current_period()
    test_next_boundary_wraps_midnight()
    test_prompt_block_is_stable()
    test_injection_appends_to_existing_system_item()
    test_injection_is_idempotent()
    test_injection_without_system_item_prepends()
    test_change_directly_schedules_proactive(tmp_path=Path("_tmp_schedule_test").resolve())
    test_change_disabled_means_no_proactive_but_mark_still_set()
    test_load_reason_respects_its_own_switch()
    test_config_ships_periods()
    print("日程合并测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
