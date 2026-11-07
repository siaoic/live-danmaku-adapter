"""弹幕墙 parse_inbound 回归：四类直播事件都要干净解析成一行。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_MODULE_NAME = "live_danmaku_adapter_danmaku_test"

spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(_PLUGIN_DIR)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[_MODULE_NAME] = module
spec.loader.exec_module(module)

from live_danmaku_adapter_danmaku_test.danmaku import (  # noqa: E402
    DanmakuServer,
    parse_inbound,
)


def _msg(text: str, kind: str, nick: str = "", price: str = ""):
    extra = {"live_event_kind": kind}
    if price:
        extra["live_price"] = price
    return {
        "message_info": {"user_info": {"user_nickname": nick}, "additional_config": extra},
        "processed_plain_text": text,
        "raw_message": [{"type": "text", "data": text}],
    }


def test_danmaku_split_user_content() -> None:
    """弹幕：昵称与内容分列。"""

    r = parse_inbound(_msg("[B站弹幕] 观众甲: 你好呀", "danmaku", "观众甲"))
    assert r["user"] == "观众甲"
    assert r["text"] == "你好呀"
    assert r["badge"] == ""


def test_gift_whole_line_no_dup_user_no_price() -> None:
    """礼物：整句显示，昵称不单列，末尾金额不显示。"""

    r = parse_inbound(_msg("[B站礼物] 观众甲 送出了 粉丝团灯牌x1，¥0.1", "gift", "观众甲", "0.1"))
    assert r["user"] == ""
    assert r["text"] == "观众甲 送出了 粉丝团灯牌x1"
    assert r["badge"] == "礼物"


def test_super_chat_no_price_in_badge() -> None:
    """醒目留言：前缀与 badge 里的金额都不显示，昵称与内容分列。"""

    r = parse_inbound(_msg("[B站醒目留言 ¥30] 观众甲: 主播加油", "super_chat", "观众甲", "30"))
    assert r["user"] == "观众甲"
    assert r["text"] == "主播加油"
    assert r["badge"] == "SC"


def test_all_priced_events_hide_amount() -> None:
    """礼物/上舰/SC：正文与小标签里都不该出现 ¥ / ￥ 金额。"""

    cases = [
        ("gift", "[抖音礼物] 路人 送出了 小心心x5，￥12.5"),
        ("gift", "[B站礼物] 路人 送出了 摩天大楼x1 ，¥100"),
        ("gift", "[B站礼物] 路人 送出了 辣条x1"),
        ("super_chat", "[B站醒目留言 ¥30] 路人: 主播加油"),
        ("super_chat", "[B站醒目留言 ¥30] 路人: 加油，¥30"),
        ("guard", "[B站上舰·舰长] 路人 开通了舰长"),
    ]
    for kind, text in cases:
        r = parse_inbound(_msg(text, kind, "路人", "30"))
        assert "¥" not in r["text"] and "￥" not in r["text"], (kind, text, r)
        assert "¥" not in r["badge"] and "￥" not in r["badge"], (kind, text, r)


def test_viewer_price_text_is_kept() -> None:
    """普通弹幕里观众自己打的价格是弹幕内容，不该被当成礼物金额删掉。"""

    r = parse_inbound(_msg("[B站弹幕] 观众甲: 这个灯牌 ¥30 值吗", "danmaku", "观众甲"))
    assert r["text"] == "这个灯牌 ¥30 值吗"
    assert r["badge"] == ""


def test_server_push_strips_price_from_legacy_item() -> None:
    """展示层兜底：直接 POST / 旧状态塞进来的带价条目也要洗掉金额。"""

    server = DanmakuServer(port=0)
    server.push({"user": "", "text": "观众甲 送出了 灯牌x1，¥0.1", "badge": "礼物", "kind": "gift"})
    server.push({"user": "观众乙", "text": "主播加油", "badge": "SC ¥30", "kind": "super_chat"})
    server.push({"user": "观众丙", "text": "灯牌 ¥30 好贵", "badge": "", "kind": "danmaku"})
    rows = server.state()["items"]
    assert rows[0]["text"] == "观众甲 送出了 灯牌x1"
    assert rows[0]["badge"] == "礼物"
    assert rows[1]["badge"] == "SC"
    assert rows[2]["text"] == "灯牌 ¥30 好贵"


def test_page_renders_with_price_strippers() -> None:
    """页面模板能正常 format（新增 JS 兜底花括号都没漏转义）。"""

    import asyncio

    server = DanmakuServer(port=0)
    html = asyncio.run(server._handle_page(None)).text
    assert "stripBadgePrice" in html
    assert "stripPrice" in html
    # 模板不是 raw string，JS 正则里的 \s 必须写成 \\s，页面拿到的才是单反斜杠
    assert r"\s*[,，]" in html
    assert r"\\s" not in html


def test_guard_whole_line_no_dup_user() -> None:
    """上舰：整句显示。"""

    r = parse_inbound(_msg("[B站上舰·舰长] 观众甲 开通了舰长", "guard", "观众甲"))
    assert r["user"] == ""
    assert r["text"] == "观众甲 开通了舰长"
    assert r["badge"] == "上舰"


def test_douyin_danmaku() -> None:
    """抖音弹幕同样解析。"""

    r = parse_inbound(_msg("[抖音弹幕] 路人: 哈哈哈", "danmaku", "路人"))
    assert r["user"] == "路人"
    assert r["text"] == "哈哈哈"


def test_empty_text_returns_none() -> None:
    """解析不出内容返回 None。"""

    assert parse_inbound(_msg("", "danmaku")) is None


def main() -> int:
    test_danmaku_split_user_content()
    test_gift_whole_line_no_dup_user_no_price()
    test_super_chat_no_price_in_badge()
    test_all_priced_events_hide_amount()
    test_viewer_price_text_is_kept()
    test_server_push_strips_price_from_legacy_item()
    test_page_renders_with_price_strippers()
    test_guard_whole_line_no_dup_user()
    test_douyin_danmaku()
    test_empty_text_returns_none()
    print("弹幕解析回归通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
