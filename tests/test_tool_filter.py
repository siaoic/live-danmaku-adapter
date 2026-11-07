# -*- coding: utf-8 -*-
"""直播流工具过滤回归：表情包工具名识别与定义名提取。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_MODULE_NAME = "live_danmaku_adapter_toolfilter_test"

spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[_MODULE_NAME] = module
spec.loader.exec_module(module)

from live_danmaku_adapter_toolfilter_test.plugin import (  # noqa: E402
    _BLOCKED_EMOJI_TOOLS,
    LiveDanmakuAdapter,
    browser_calls_in_turn,
)

_extract_name = LiveDanmakuAdapter._tool_definition_name


def test_blocked_emoji_tools_constant() -> None:
    """要隐藏的是两个表情包工具名。"""

    assert _BLOCKED_EMOJI_TOOLS == frozenset({"send_emoji", "show_emoji_list"})


def test_tool_definition_name_openai_style() -> None:
    """OpenAI function 结构取 function.name。"""

    assert _extract_name({"type": "function", "function": {"name": "send_emoji"}}) == "send_emoji"


def test_tool_definition_name_internal_style() -> None:
    """内部结构直接取 name。"""

    assert _extract_name({"name": "reply", "parameters": {}}) == "reply"
    # function 为空字典时回退到顶层 name
    assert _extract_name({"name": "show_emoji_list", "function": {}}) == "show_emoji_list"
    # 非法输入返回空串
    assert _extract_name("not-a-dict") == ""
    assert _extract_name({}) == ""


def test_filter_keeps_non_emoji_tools() -> None:
    """过滤逻辑：表情包工具被移除，其余工具原样保留。"""

    tools = [
        {"type": "function", "function": {"name": "reply"}},
        {"name": "send_emoji"},
        {"type": "function", "function": {"name": "show_emoji_list"}},
        {"name": "tool_search"},
    ]
    kept = [t for t in tools if _extract_name(t) not in _BLOCKED_EMOJI_TOOLS]
    names = {_extract_name(t) for t in kept}
    assert names == {"reply", "tool_search"}
    assert len(kept) == 2


def test_heavy_tool_patterns() -> None:
    """默认不再永久屏蔽任何工具：live_sight 陪看、browser 上网都要保留。"""

    blocked = LiveDanmakuAdapter._tool_name_blocked
    assert not blocked("live_sight", ())
    assert not blocked("browser_navigate", ())
    assert not blocked("tool_search", ())
    # 显式配置仍然能永久屏蔽
    assert blocked("browser_navigate", ("browser_",))
    print("OK 默认不永久屏蔽任何工具")


def test_browser_call_limit_per_turn() -> None:
    """浏览器按轮次计数：同一轮达到上限就摘掉，开口后清零。"""

    items = [
        {"item_type": "SystemMessageItem"},
        {"item_type": "UserMessageItem"},
        {"item_type": "FunctionCallItem", "tool_call": {"func_name": "browser_navigate"}},
        {"item_type": "FunctionCallOutputItem", "tool_name": "browser_navigate", "call_id": "a"},
        {"item_type": "FunctionCallItem", "tool_call": {"func_name": "browser_snapshot"}},
    ]
    assert browser_calls_in_turn(items) == 2
    # 已经 reply 过 = 新一轮开始，计数归零
    after_reply = items + [
        {"item_type": "FunctionCallItem", "tool_call": {"func_name": "reply"}},
        {"item_type": "UserMessageItem"},
    ]
    assert browser_calls_in_turn(after_reply) == 0
    # 新用户消息之后的历史浏览器调用不计入本轮
    with_new_turn = [
        {"item_type": "FunctionCallItem", "tool_call": {"func_name": "browser_navigate"}},
        {"item_type": "UserMessageItem"},
    ]
    assert browser_calls_in_turn(with_new_turn) == 0
    print("OK 浏览器按轮次计数")


def main() -> int:
    test_blocked_emoji_tools_constant()
    test_tool_definition_name_openai_style()
    test_tool_definition_name_internal_style()
    test_filter_keeps_non_emoji_tools()
    test_heavy_tool_patterns()
    test_browser_call_limit_per_turn()
    print("直播流工具过滤回归通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
