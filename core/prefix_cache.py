# -*- coding: utf-8 -*-
"""DeepSeek 前缀缓存稳定层（只动插件，不改宿主）。

参考两个上下文工程项目的做法：

* `dsh-context`：逐请求观测上下文组成与缓存命中率；冷缓存按**持久化服务的
  稳定身份**做 key，而不是把每轮重新绑定的包装对象当 key（后者每次读都
  refold，命中率必然归零）。这里对应 :class:`MetaIdentityRegistry` —— 宿主
  每轮会重建大量 Context Item 并刷新 ``meta.item_id`` / ``timestamp``，而
  宿主 ``build_prompt`` 会把整个 Item 的 JSON（含 meta）逐字拼进 prompt，
  于是第 0 条就失配，后面整段缓存作废。插件在请求前把这些 Item 钉回
  「首次出现时的身份」，跨轮逐字节一致。
* `dsh-ops`：发布自己的 scoped 工具集与可选路由引导，**不改写共享注册表**；
  工具集固定，约束靠尾部引导而不是每轮增删工具。这里对应
  :class:`ToolTableFreezer` —— 工具表是 prompt 的第一个 token 块，集合或
  字节一变，后面整段缓存作废。

另外提供 :class:`PrefixCacheMonitor`：按 64-token 缓存块估算相邻请求的最长
公共前缀，给出预测命中率与首个失配归因（对应 dsh-context 的 Context Stats /
逐请求趋势），让「99%」是可测量、可回归的，而不是一句口号。

设计约束：
    - 不修改任何业务字段（parts / tool_call / call_id / logical_turn_id）；
    - 首轮只**学习**身份、不改写任何 Item：宿主按 item_id 复用 provider
      replay fragment 的机制不受影响，也让既有「普通轮返回 None」的契约成立；
    - 纯标准库，插件内可离线测试。
"""

from __future__ import annotations

import bisect
import copy
import hashlib
import json
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: DeepSeek 上下文硬盘缓存以 64 token 为最小命中块
CACHE_BLOCK_TOKENS = 64
#: 中英混排的粗估密度（字符/token）；只用于离线预测，不参与任何业务逻辑
CHARS_PER_TOKEN = 1.8
_BLOCK_CHARS = int(CACHE_BLOCK_TOKENS * CHARS_PER_TOKEN)

#: 尾部规则槽（多个 Hook 共享同一条 Item，避免条数与顺序抖动）
TAIL_RULE_MARKER = "【直播轮次规则·稳定槽】"
TAIL_RULE_ITEM_ID = "live-prefix-cache-tail-rule"
TAIL_RULE_TIMESTAMP = "2020-01-01T00:00:00"
#: 拼接顺序固定，与 Hook 执行先后无关
TAIL_SLOT_ORDER: Tuple[str, ...] = ("round", "schedule")

#: 变量尾巴：所有**本轮会变**的内容（当前时段 / 时间 / 动态规则）只占这一条
#: 极短 Item，放在 prompt 绝对末尾。恒定规则槽则进前缀。
#: 这样相邻请求的公共前缀 = 上一轮除最后一条之外的全部内容。
TAIL_VOLATILE_ITEM_ID = "live-prefix-cache-volatile"
TAIL_VOLATILE_MARKER = "【直播变量尾·稳定槽】"
TAIL_VOLATILE_SLOTS: Tuple[str, ...] = ("round", "schedule", "time")
_VOLATILE_SLOT_RE = re.compile(r"\n@@V:(round|schedule|time)@@\n")


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def canonical_json(value: Any) -> str:
    """稳定的 JSON 表示（key 排序、不转义非 ASCII）。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def item_text(item: Any) -> str:
    """取一条 Context Item 的文本内容（纯函数）。"""

    if not isinstance(item, dict):
        return ""
    parts = item.get("parts")
    if not isinstance(parts, list):
        return ""
    return " ".join(
        str(part.get("text") or "")
        for part in parts
        # 宿主不同版本的快照里 text part 可能省略 type 字段，缺省按 text 处理
        if isinstance(part, dict) and "text" in part and str(part.get("type") or "text") == "text"
    )


def content_identity(item: Any) -> str:
    """Item 的「内容身份」：除 meta 外全部字段的规范化哈希。

    ``meta`` 里的 item_id / timestamp 是宿主每轮重建时刷新的易变字段，不能
    参与身份计算；``logical_turn_id`` 是关系字段，保留原样不参与改写。
    """

    if not isinstance(item, dict):
        return ""
    payload = {key: value for key, value in item.items() if key != "meta"}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def is_time_item(item: Any) -> bool:
    text = item_text(item).lstrip()
    return text.startswith("时间：") or text.startswith("当前时间：")


def is_summary_item(item: Any) -> bool:
    return "【上下文状态摘要" in item_text(item)


# --------------------------------------------------------------------------- #
# 稳定身份：把每轮被重建的 Item 钉回首次身份
# --------------------------------------------------------------------------- #
class MetaIdentityRegistry:
    """跨请求维护 Item 的「稳定身份」。

    宿主有些路径会复用 ContextItem 对象（item_id 稳定），有些路径会从持久化
    历史整体重建（item_id 与 timestamp 全部刷新）。两种路径交替时，相邻两次
    请求的最长公共前缀只有 0.2%~35%。

    本类按**内容身份**登记第一次见到的 ``(item_id, timestamp)``；之后只要
    内容相同，就把被重建的 Item 钉回首次身份。于是：

    * 首次出现：零改写 —— provider replay 复用链完好；
    * 之后每轮：相同内容 -> 相同 meta -> 相同 prompt 字节；
    * 内容真的变了（新弹幕、新摘要、时间行）：登记新身份，属于必变区，
      由 :func:`upsert_tail_rule` 统一压到上下文尾部。
    """

    def __init__(self, max_entries: int = 4096) -> None:
        self._identities: Dict[str, Tuple[str, str]] = {}
        self._max_entries = max(256, int(max_entries))
        self.learned = 0
        self.restored = 0
        self.evictions = 0

    def stabilize(self, items: Any) -> Tuple[List[Any], int]:
        """返回 (规范化后的 items, 改写条数)。输入非 list 时原样返回。"""

        if not isinstance(items, list) or not items:
            return items if isinstance(items, list) else [], 0

        seen: Dict[str, int] = {}
        result: List[Any] = []
        used_ids: set = set()
        changed = 0
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("meta"), dict):
                result.append(item)
                continue
            key = content_identity(item)
            if not key:
                result.append(item)
                continue
            # 同一份 items 里出现多条完全相同的 Item：用序号区分，避免
            # validate_context_items 的 duplicate_item_ids 校验失败。
            occurrence = seen.get(key, 0)
            seen[key] = occurrence + 1
            scoped_key = f"{key}#{occurrence}" if occurrence else key
            meta = item["meta"]
            current_id = str(meta.get("item_id") or "")
            current_ts = str(meta.get("timestamp") or "")
            identity = self._identities.get(scoped_key)
            out_item = item
            if identity is None:
                self._learn(scoped_key, current_id, current_ts)
            else:
                stable_id, stable_ts = identity
                if not (current_id == stable_id and current_ts == stable_ts):
                    new_meta = dict(meta)
                    if stable_id:
                        new_meta["item_id"] = stable_id
                    if stable_ts:
                        new_meta["timestamp"] = stable_ts
                    out_item = {**item, "meta": new_meta}
                    changed += 1
                    self.restored += 1
            # 防御：钉回的身份可能恰好是本次请求里另一条 Item 的 id（比如宿主
            # 部分复用对象、部分重建）。宿主 validate_context_items 会因
            # duplicate_item_ids 直接拒绝整份 items，宁可放弃这次钉回。
            out_id = str((out_item.get("meta") or {}).get("item_id") or "")
            if out_id and out_id in used_ids and out_item is not item:
                out_item = item
                out_id = current_id
                changed -= 1
                self.restored -= 1
            if out_id:
                used_ids.add(out_id)
            result.append(out_item)
        return result, changed

    def _learn(self, key: str, item_id: str, timestamp: str) -> None:
        if len(self._identities) >= self._max_entries:
            # 简单 FIFO 淘汰，避免长直播无限增长；淘汰后下一轮重新学习一次
            oldest = next(iter(self._identities))
            self._identities.pop(oldest, None)
            self.evictions += 1
        self._identities[key] = (item_id, timestamp)
        self.learned += 1

    def reset(self) -> None:
        """热重载/会话切换时清空（下一轮重新学习一次，只有一轮代价）。"""

        self._identities.clear()
        self.learned = 0
        self.restored = 0
        self.evictions = 0

    def stats(self) -> Dict[str, Any]:
        return {
            "identities": len(self._identities),
            "learned": self.learned,
            "restored": self.restored,
            "evictions": self.evictions,
        }


# --------------------------------------------------------------------------- #
# 固定工具表
# --------------------------------------------------------------------------- #
def tool_name(definition: Any) -> str:
    """读取工具定义名称，兼容内部定义与 OpenAI function 结构。"""

    if not isinstance(definition, dict):
        return ""
    inner = definition.get("function")
    if isinstance(inner, dict) and str(inner.get("name") or "").strip():
        return str(inner["name"]).strip()
    return str(definition.get("name") or "").strip()


class ToolTableFreezer:
    """会话级冻结工具表：并集 + 稳定排序 + 首次定义内容。

    宿主会把 deferred 池里「已被 tool_search 发现」的工具临时塞进
    ``tool_definitions``，实测出现 4 种集合在轮换（10~22 个工具来回跳）。
    工具表是 prompt 的第一个 token 块，它一变，后面整段缓存作废。

    这里不删任何工具（删工具会改变模型行为，A/B 实测工具轮数从 15~20
    涨到 38~48），而是把「见过即纳入」的并集固定下来，之后每轮输出同一份
    逐字节一致的表。本轮该不该用某个工具，改由尾部规则表达 —— 与 dsh-ops
    「固定 scoped 工具集 + 可选路由引导」一致。
    """

    def __init__(self, blocked: Iterable[str] = (), blocked_patterns: Iterable[str] = ()) -> None:
        self._blocked = frozenset(str(name) for name in blocked if name)
        self._blocked_patterns = tuple(str(p) for p in blocked_patterns if p)
        self._snapshot: Dict[str, Dict[str, Any]] = {}
        self.added_total = 0
        self.freeze_calls = 0
        #: 工具表发生扩展的次数。每次扩展都会让 prompt 的块边界整体错位，
        #: 是一次必然的冷缓存；插件的目标是把这个数字压到最低。
        self.expansions = 0

    def set_block_patterns(self, patterns: Iterable[str]) -> None:
        """更新用户配置的屏蔽前缀（配置来自宿主，插件初始化时还拿不到）。"""

        self._blocked_patterns = tuple(str(p).strip() for p in patterns if str(p).strip())

    def _is_blocked(self, name: str) -> bool:
        if name in self._blocked:
            return True
        return any(name == pattern or name.startswith(pattern) for pattern in self._blocked_patterns)

    def freeze(self, tools: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """返回 (冻结后的工具表, 本轮变更信息)。"""

        self.freeze_calls += 1
        if not isinstance(tools, list) or not tools:
            return [], {"added": [], "size": 0, "stable": True}
        added: List[str] = []
        for definition in tools:
            if not isinstance(definition, dict):
                continue
            name = tool_name(definition)
            if not name or self._is_blocked(name) or name in self._snapshot:
                continue
            self._snapshot[name] = copy.deepcopy(definition)
            added.append(name)
        self.added_total += len(added)
        if added:
            self.expansions += 1
        # 按「首次纳入顺序」输出，而不是按名字排序：排序会把新发现的工具插到
        # 列表中间，让工具表 JSON 从插入点起整段变化；追加在末尾则前缀字节
        # 保持原样，只有工具表尾块失配。顺序本身由本类固定，宿主怎么抖都不变。
        frozen = [
            copy.deepcopy(definition)
            for name, definition in self._snapshot.items()
            if not self._is_blocked(name)
        ]
        return frozen, {"added": added, "size": len(frozen), "stable": not added}

    def snapshot(self) -> List[Dict[str, Any]]:
        """返回当前冻结表（不依赖本轮传入的 tools，便于诊断与测试）。"""

        return [
            copy.deepcopy(definition)
            for name, definition in self._snapshot.items()
            if not self._is_blocked(name)
        ]

    def reset(self) -> None:
        self._snapshot.clear()
        self.added_total = 0
        self.freeze_calls = 0
        self.expansions = 0

    def stats(self) -> Dict[str, Any]:
        return {
            "tools": len(self._snapshot),
            "names": sorted(self._snapshot),
            "added_total": self.added_total,
            "expansions": self.expansions,
            "freeze_calls": self.freeze_calls,
            "blocked": sorted(self._blocked),
        }


# --------------------------------------------------------------------------- #
# 尾部规则槽：所有易变内容只占最后一条 Item
# --------------------------------------------------------------------------- #
#: schedule 槽的显式标签。必须带名字：只有 schedule 槽时，若靠「位置」
#: 去猜，它会被误当成 round 槽，下一次 upsert(round) 就把日程内容覆盖掉。
_SCHEDULE_SEPARATOR = "\n@@SLOT:schedule@@\n"
#: 旧版无标签编码的兼容分隔符
_SLOT_SEPARATOR = "\n@@SLOT@@\n"
#: stabilize_layout 把量化后的时间行合并进规则槽时用的标记；解析时剔除，
#: 防止宿主复用上一轮 items 时把时间文本当成规则内容逐轮累积。
_VOLATILE_TIME_TAG = "[pc-time]"
_VOLATILE_TIME_RE = re.compile(r"\[pc-time\][^\n]*")


def parse_tail_slots(text: str) -> Dict[str, str]:
    """解析规则 Item 里各 slot 的文本（按名字还原，兼容旧格式）。"""

    slots: Dict[str, str] = {}
    body = str(text or "").replace(TAIL_RULE_MARKER, "")
    body = _VOLATILE_TIME_RE.sub("", body)
    if not body.strip():
        return slots
    if _SCHEDULE_SEPARATOR in body:
        before, after = body.split(_SCHEDULE_SEPARATOR, 1)
        if before.strip("\n"):
            slots["round"] = before.strip("\n")
        if after.strip("\n"):
            slots["schedule"] = after.strip("\n")
        return slots
    if _SLOT_SEPARATOR in body:  # 兼容重构前写入的唯一一份旧快照
        chunks = body.split(_SLOT_SEPARATOR)
        for slot, chunk in zip(TAIL_SLOT_ORDER, chunks, strict=False):
            value = chunk.strip("\n")
            if value:
                slots[slot] = value
        return slots
    slots["round"] = body.strip("\n")
    return slots


def is_tail_rule_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    meta = item.get("meta")
    return (
        isinstance(meta, dict)
        and str(meta.get("item_id") or "") == TAIL_RULE_ITEM_ID
    )


def upsert_tail_rule(items: Any, slot: str, text: str) -> Tuple[List[Any], bool]:
    """在上下文尾部维护**唯一一条**规则 Item，多个 slot 按固定顺序合并。

    为什么必须唯一：Planner 上同时挂了「工具过滤」与「日程注入」两个
    before_request Hook，执行先后不保证。各自 ``append`` 一条带随机 uuid 的
    Item 会让 items 的条数、顺序与字节每轮抖动；而 prompt cache 是前缀命中，
    尾部块一抖，后面的时间/摘要全部重新 prefill。

    幂等：同一 slot 内容未变时返回 ``(原列表, False)``。

    返回顺序：历史 + 静态块 + 规则 + 摘要 + 时间。每轮真正变化的只剩
    摘要与最后一行时间，命中率因此逼近 100%。
    """

    if not isinstance(items, list):
        return [], False
    if slot not in TAIL_SLOT_ORDER:
        raise ValueError(f"未知的尾部规则槽: {slot}")
    text = str(text or "").strip()
    if not text:
        return items, False

    slots: Dict[str, str] = {}
    time_items: List[Any] = []
    summary_items: List[Any] = []
    rest: List[Any] = []
    for item in items:
        if is_tail_rule_item(item):
            slots.update(parse_tail_slots(item_text(item)))
            continue
        if is_time_item(item):
            time_items.append(item)
            continue
        if is_summary_item(item):
            summary_items.append(item)
            continue
        rest.append(item)

    if slots.get(slot) == text and any(is_tail_rule_item(it) for it in items):
        return items, False

    slots[slot] = text
    # round 槽排在最前且不带标签：既有契约要求「尾部最后一条以本轮规则开头」；
    # schedule 槽带显式名字，保证只有 schedule 时也能被正确还原。
    # marker 放末尾只用于 stable_block_rank 归类，不参与语义。
    body = slots.get("round") or ""
    if slots.get("schedule"):
        body += _SCHEDULE_SEPARATOR + slots["schedule"]
    body += "\n" + TAIL_RULE_MARKER
    rule_item = {
        "item_type": "UserMessageItem",
        "meta": {
            "item_id": TAIL_RULE_ITEM_ID,
            "logical_turn_id": None,
            "timestamp": TAIL_RULE_TIMESTAMP,
        },
        "parts": [{"type": "text", "text": body}],
    }
    return [*rest, rule_item, *summary_items, *time_items], True


def is_volatile_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    meta = item.get("meta")
    return isinstance(meta, dict) and str(meta.get("item_id") or "") == TAIL_VOLATILE_ITEM_ID


def parse_volatile_slots(text: str) -> Dict[str, str]:
    """按名字还原变量尾巴的各个 slot（带标签，缺哪个槽都不影响其它槽）。"""

    body = str(text or "").replace(TAIL_VOLATILE_MARKER, "")
    slots: Dict[str, str] = {}
    matches = list(_VOLATILE_SLOT_RE.finditer(body))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        value = body[match.end() : end].strip("\n")
        if value:
            slots[match.group(1)] = value
    return slots


def upsert_volatile_tail(items: Any, slot: str, text: str) -> Tuple[List[Any], bool]:
    """维护**唯一一条**变量尾巴 Item（绝对末尾，固定 meta，按 slot 合并）。

    与 :func:`upsert_tail_rule` 的分工：

    * 规则槽：跨轮恒定的基线规则 + 日程稳定段 -> 进前缀；
    * 变量尾巴：本轮动态规则 / 当前时段 / 量化时间 -> 绝对末尾。

    真机诊断：规则槽（422 字符）留在新增内容之后时，每轮新增的历史消息
    会把它往后推，从插入点起所有缓存块错位，稳态 ``waste`` 约 6%。
    拆成「恒定长槽前置 + 极短尾巴」后，前缀完全不变，变化只影响最后一块。

    ``text`` 传空字符串表示清除该 slot；多个 Hook 各写一个 slot，顺序由
    ``TAIL_VOLATILE_SLOTS`` 固定，与执行先后无关。
    """

    if not isinstance(items, list):
        return [], False
    if slot not in TAIL_VOLATILE_SLOTS:
        raise ValueError(f"未知的变量尾巴槽: {slot}")
    text = str(text or "").strip()
    slots: Dict[str, str] = {}
    rest: List[Any] = []
    had_volatile = False
    for item in items:
        if is_volatile_item(item):
            had_volatile = True
            slots.update(parse_volatile_slots(item_text(item)))
        else:
            rest.append(item)
    if not text:
        if slot not in slots:
            return items, False
        slots.pop(slot, None)
    else:
        if slots.get(slot) == text and had_volatile:
            return items, False
        slots[slot] = text
    if not slots:
        return rest, had_volatile
    body = "".join(
        f"\n@@V:{name}@@\n{slots[name]}" for name in TAIL_VOLATILE_SLOTS if slots.get(name)
    ) + "\n" + TAIL_VOLATILE_MARKER
    volatile_item = {
        "item_type": "UserMessageItem",
        "meta": {
            "item_id": TAIL_VOLATILE_ITEM_ID,
            "logical_turn_id": None,
            "timestamp": TAIL_RULE_TIMESTAMP,
        },
        "parts": [{"type": "text", "text": body}],
    }
    return [*rest, volatile_item], True


# --------------------------------------------------------------------------- #
# 前缀存档：宿主压缩后仍保持「严格前缀 + 只追加」
# --------------------------------------------------------------------------- #
#: 存档上限。超过就接受一次必然的冷启动并重建，避免上下文无限增长。
PREFIX_ARCHIVE_MAX_ITEMS = 110


def merge_with_prefix_archive(
    items: Any,
    archive: Optional[List[Any]],
    *,
    max_items: int = PREFIX_ARCHIVE_MAX_ITEMS,
) -> Tuple[List[Any], List[Any], bool]:
    """把本轮 items 合并进上一轮的 prompt 前缀存档，只追加、不删除。

    设计来源：``dsh-context`` 的「已移除消息存档」（能重建压缩前的上下文）
    与 ``dsh-ops`` 的「状态变更在 owning agent 的下一轮 provider request
    生效，不在本轮插入」。

    宿主上下文压缩会把 49 条历史裁成 35 条、59 条裁成 16 条，历史开头整批
    替换，前缀在第 3 条就断（真机 42~57%）。DeepSeek 缓存是全局前缀，
    「删除中间内容」必然让后面所有块错位，插件无法恢复被丢弃的历史——
    但可以在请求前维护自己的存档：本轮 prompt = 上一轮 prompt（除最后一条
    变量尾巴）+ 本轮新增 Item + 变量尾巴，于是压缩轮仍是上一轮的超集。

    Args:
        items: 已经过 layout / 身份钉回的**最终** items。
        archive: 上一轮最终 items 中除变量尾巴之外的部分；``None`` 表示首轮。
        max_items: 存档上限，超过则本轮放弃合并并重置（返回 ``reset=True``）。

    Returns:
        ``(合并后的 items, 新 archive, 是否触发上限重置)``

    注意：按 ``meta.item_id`` 去重，宿主压缩后仍保留的消息不会重复出现；
    变量尾巴始终重建并放在绝对末尾，不进存档。
    """

    if not isinstance(items, list) or not items:
        return (items if isinstance(items, list) else []), [], False
    body = [it for it in items if not is_volatile_item(it)]
    volatile = [it for it in items if is_volatile_item(it)]
    if archive is None:
        return items, body, False

    def identity(item: Any) -> str:
        if not isinstance(item, dict):
            return canonical_json(item)
        meta = item.get("meta")
        item_id = str((meta or {}).get("item_id") or "") if isinstance(meta, dict) else ""
        return item_id or canonical_json(item)

    known = {identity(it) for it in archive}
    fresh: List[Any] = []
    for item in body:
        key = identity(item)
        if key in known:
            continue
        known.add(key)
        fresh.append(item)
    merged = [*archive, *fresh, *volatile]
    if len(merged) > max_items:
        # 上限保护：接受一次冷启动，从本轮宿主给的短上下文重新建存档。
        return items, body, True
    return merged, [it for it in merged if not is_volatile_item(it)], False


# --------------------------------------------------------------------------- #
# 布局稳定：稳定块前置，变量后置
# --------------------------------------------------------------------------- #
#: 宿主/插件每轮注入的「文本内容恒定」块，按固定序号排在历史之前。
#: 只要文本命中任一特征就归入稳定区，序号决定跨轮一致的顺序。
#: 注意：TAIL_RULE_MARKER **不能**进稳定区。规则槽是「本轮」规则，主动轮、
#: 工具轮上限、换挡都会改它的内容。真实快照 A/B：规则恒定时放前缀只赚
#: +0.9%，但只要 3 轮规则变化，放前缀就把平均命中率从 73.3% 打到 62.0%
#: —— 变化被放大到整段前缀。它由 stabilize_layout 显式归入变量尾巴。
_STABLE_BLOCK_RULES: Tuple[Tuple[str, int], ...] = (
    ("当前聊天额外注意事项", 0),
    ("【弹幕回复规则】", 0),
    ("输出格式建议", 0),
    ("请你进行分析", 1),
    ("## 日程表（必须遵守）", 2),
    ("当前时段：", 3),
    ("【自己发言识别规则", 4),
    ("弹幕防伪规则", 4),
    ("【本轮强制覆盖规则", 4),
    ("【本轮工具已用完】", 4),
)


def stable_block_rank(text: str) -> Optional[int]:
    """返回稳定块的固定序号；不属于稳定区时返回 None。"""

    for marker, rank in _STABLE_BLOCK_RULES:
        if marker in text:
            return rank
    return None


#: 「时间：2026-11-08 14:05:23」->「时间：2026-11-08 14:05」，秒数对日程/
#: 对话判断没有意义，却让同一分钟内的每一轮都产生一次必然失配。
_TIME_SECONDS_RE = re.compile(r"(时间[:：]\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}):\d{2}")


def quantize_time_item(item: Any) -> Any:
    """把时间行精确到分钟，并给它一套由**量化后文本**派生的稳定 meta。

    只改正文不够：宿主每轮重建时间行时连 ``meta.item_id`` / ``timestamp``
    一起刷新，正文相同而 meta 不同照样失配。这里把两者都钉到「分钟级身份」，
    同一分钟内的连续轮次整条 Item 逐字节相同。
    """

    if not isinstance(item, dict) or not is_time_item(item):
        return item
    parts = item.get("parts")
    if not isinstance(parts, list):
        return item
    new_parts: List[Any] = []
    changed = False
    minute_text = ""
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            text = _TIME_SECONDS_RE.sub(r"\1", part["text"])
            if text != part["text"]:
                new_parts.append({**part, "text": text})
                minute_text = minute_text or text
                changed = True
                continue
        new_parts.append(part)
    if not changed:
        return item
    meta = dict(item.get("meta") or {})
    meta["item_id"] = "pc-time-" + hashlib.sha256(minute_text.encode("utf-8")).hexdigest()[:24]
    match = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", minute_text)
    if match:
        meta["timestamp"] = f"{match.group(1)}T{match.group(2)}:00"
    return {**item, "parts": new_parts, "meta": meta}


def is_deferred_reminder(item: Any) -> bool:
    """宿主那条「可用 tool_search 发现未暴露工具」的提示。

    工具表冻结后所有工具都常驻可见，这条提示永远过时；它时有时无会让
    items 的条数与字节每轮抖动，必须无条件移除。
    """

    text = item_text(item)
    return "<system-reminder>" in text and "tool_search" in text


def stabilize_layout(items: Any) -> Tuple[List[Any], Dict[str, int]]:
    """把请求重排成「恒定前缀 + 历史 + 极短变量尾巴」。

    为什么要重排：宿主把静态提示块、时间行放在**历史之后**，插件又把日程段
    与本轮规则追加在它们中间。每轮新产生的弹幕 / 工具调用 / 工具回执会插在
    历史末尾，也就是这些稳定块**之前**。于是相邻两轮的最长公共前缀永远停在
    倒数第 5 条，后面 5~8% 的 token 每轮重新 prefill。

    重命名后的顺序：

    ``SystemMessageItem -> 静态块 + 恒定规则槽 + 压缩摘要 -> 历史 -> 变量尾巴``
    压缩摘要归稳定区：真机 21 轮里它只变 2 次（每次上下文压缩），其余轮
    逐字节相同；放尾部会被每轮新增消息推着错位，放前缀只有压缩轮受损。

    2026-11-08 真机校准：21 轮真机快照里有 18 轮的首个失配落在「上一轮
    规则槽的位置」—— 每轮新增内容插在规则槽**之前**，把这条 422 字符的
    Item 往后推，从插入点起所有缓存块错位，稳态 ``waste`` 约 6%。
    现在把规则槽里恒定的部分（基线规则 + 日程稳定段）放进前缀，把所有
    本轮才变的内容（动态规则 / 当前时段 / 量化时间）收进唯一一条极短的
    变量尾巴并放绝对末尾。于是：

    * 前缀跨轮逐字节相同，一次动态规则变化不再毁掉整段历史；
    * 新增内容之后只剩一条短 Item，损失接近理论下界。

    注意：只重排、不删业务 Item（deferred 提示除外），不改任何 Item 的文本
    与 meta；SystemMessageItem 仍留在最前面，兼容 Qwen 模板与既有契约。
    """

    if not isinstance(items, list) or not items:
        return (items if isinstance(items, list) else []), {"moved": 0, "removed": 0}

    systems: List[Any] = []
    stable: List[Tuple[int, int, Any]] = []
    history: List[Any] = []
    times: List[Any] = []
    volatile: List[Any] = []
    removed = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            history.append(item)
            continue
        if is_deferred_reminder(item):
            removed += 1
            continue
        if is_tail_rule_item(item):
            # 恒定规则槽进前缀；动态内容已由 Hook 拆到变量尾巴
            stable.append((2, index, item))
            continue
        if is_volatile_item(item):
            volatile.append(item)
            continue
        if is_summary_item(item):
            # 真机 21 轮里压缩摘要只有 2 个版本：压缩那一刻变一次，之后
            # 连续 6~8 轮逐字节相同。它放尾部时每轮新增消息都把它往后推，
            # 造成 5~9% waste；放前缀则只有压缩轮会毁一次（那一轮历史被
            # 裁本来就必冷），其余轮整段历史前缀完整命中。
            stable.append((3, index, item))
            continue
        if is_time_item(item):
            times.append(item)
            continue
        if str(item.get("item_type") or "") == "SystemMessageItem":
            systems.append(item)
            continue
        rank = stable_block_rank(item_text(item))
        if rank is not None:
            stable.append((rank, index, item))
            continue
        history.append(item)
    stable.sort(key=lambda entry: (entry[0], entry[1]))
    stable_items = [entry[2] for entry in stable]
    quantized = [quantize_time_item(item) for item in times]
    time_text = "\n".join(item_text(item) for item in quantized)
    # 摘要已归入稳定区（rank 3）。量化后的时间行合并进变量尾巴的 time 槽；
    # 没有时间行时清掉旧槽。upsert_volatile_tail 把唯一一条变量 Item 放到
    # 绝对末尾，于是公共前缀 = 上一轮除最后这条短尾巴之外的全部内容。
    base = [*systems, *stable_items, *history, *volatile]
    result, _ = upsert_volatile_tail(base, "time", time_text)
    # 长度会变（摘要/时间被合并进变量尾巴），zip 截断会漏算新增/删除的条数。
    shared = min(len(items), len(result))
    moved = sum(1 for index in range(shared) if items[index] is not result[index])
    moved += abs(len(items) - len(result))
    return result, {"moved": moved, "removed": removed, "time_quantized": len(times)}


# --------------------------------------------------------------------------- #
# 逐请求命中率观测（预测值）
# --------------------------------------------------------------------------- #
def request_segments(
    tool_definitions: Any,
    items: Any,
    extra_prompt: str = "",
) -> List[Tuple[str, str]]:
    """把一次请求拆成「可归因的连续文本段」，顺序与真实 prompt 一致。"""

    segments: List[Tuple[str, str]] = [
        ("tool_definitions", canonical_json(tool_definitions or [])),
    ]
    for index, item in enumerate(items if isinstance(items, list) else []):
        segments.append((f"item#{index}:{str((item or {}).get('item_type') or '')}", canonical_json(item)))
    if extra_prompt:
        segments.append(("extra_prompt", str(extra_prompt)))
    return segments


def build_cache_blocks(segments: List[Tuple[str, str]]) -> Tuple[List[str], List[str]]:
    """按 64-token 块切分整段 prompt，返回 (块哈希, 块所属标签)。

    DeepSeek 的缓存是**全局前缀**按块对齐，不是按 Item 对齐，所以这里先把
    所有段拼成连续字符串再切块，避免高估命中率。
    """

    text = "".join(segment for _label, segment in segments)
    if not text:
        return [], []
    # 只记录每个段的**起始偏移**（O(段数)）。旧实现给每个字符复制一份标签
    # （labels.extend([label] * len(segment))），30k 字符的 prompt 每轮都要
    # 构造 3 万个字符串引用，而真正需要的只有每个块起点对应的那一个标签。
    starts: List[int] = []
    owners: List[str] = []
    cursor = 0
    for label, segment in segments:
        if not segment:
            continue
        starts.append(cursor)
        owners.append(label)
        cursor += len(segment)
    blocks: List[str] = []
    block_labels: List[str] = []
    for start in range(0, len(text), _BLOCK_CHARS):
        chunk = text[start : start + _BLOCK_CHARS]
        blocks.append(hashlib.sha256(chunk.encode("utf-8")).hexdigest())
        owner = bisect.bisect_right(starts, start) - 1
        block_labels.append(owners[owner] if owner >= 0 else "unknown")
    return blocks, block_labels


def common_prefix_length(left: List[str], right: List[str]) -> int:
    count = 0
    # 两侧长度不同是常态（本轮 prompt 通常更长），按较短一方比较即可
    for a, b in zip(left, right, strict=False):
        if a != b:
            break
        count += 1
    return count


class PrefixCacheMonitor:
    """相邻请求的预测前缀命中率、趋势与失配归因。

    指标定义：``命中率 = 相邻两请求的最长公共缓存块数 / 本请求总块数``，
    对齐 DeepSeek 返回的 ``prompt_cache_hit_tokens / prompt_tokens``。
    这是离线预测值；真机以服务商 usage 为准，但两者的失配归因一致。
    """

    def __init__(self, *, target: float = 0.99, history: int = 60, dedupe_ms: float = 500.0) -> None:
        self.target = float(target)
        self._history = max(1, int(history))
        self._dedupe_seconds = float(dedupe_ms) / 1000.0
        self._last: Dict[str, Dict[str, Any]] = {}
        self._records: List[Dict[str, Any]] = []
        self._sum_hit = 0.0
        self._sum_total = 0.0
        self._sum_waste = 0.0
        self._low = 0
        self._observations = 0

    def observe(
        self,
        kind: str,
        *,
        tool_definitions: Any,
        items: Any,
        extra_prompt: str = "",
        prompt_tokens: int = 0,
        note: str = "",
    ) -> Dict[str, Any]:
        """记录一次请求并返回本轮命中率报告。"""

        segments = request_segments(tool_definitions, items, extra_prompt)
        blocks, labels = build_cache_blocks(segments)
        now = time.monotonic()
        previous = self._last.get(kind)
        # 同一请求被多个 Hook 重复观测且指纹完全一致时只记一次。
        if previous and previous["blocks"] == blocks and now - previous["ts"] < self._dedupe_seconds:
            return {key: value for key, value in previous["record"].items() if key != "blocks"}
        common = common_prefix_length(previous["blocks"], blocks) if previous else 0
        total = len(blocks) or 1
        hit_rate = min(1.0, common / total)
        # 理论上限：本轮新增的 token 本来就必须 prefill，不属于插件能消除的
        # 损失。ceiling = 上一轮全部命中时的命中率；waste = 本可命中却被打碎
        # 的比例，是真正用来验收稳定层的指标。
        ceiling = min(1.0, len(previous["blocks"]) / total) if previous else 0.0
        waste = max(0.0, ceiling - hit_rate)
        first_diff = ""
        if previous and common < total and common < len(previous["blocks"]):
            first_diff = labels[common] if common < len(labels) else "unknown"
        record: Dict[str, Any] = {
            "kind": kind,
            "ts": now,
            "hit_rate": round(hit_rate, 6),
            "ceiling": round(ceiling, 6),
            "waste": round(waste, 6),
            "hit_tokens_est": int(common * CACHE_BLOCK_TOKENS),
            "total_tokens_est": int(total * CACHE_BLOCK_TOKENS),
            "prompt_tokens": int(prompt_tokens or 0),
            "blocks": blocks,
            "first_diff": first_diff,
            "note": note,
            # 宿主上下文压缩会让本轮比上一轮短（ceiling≈1）且历史开头被整批
            # 替换，命中率必然塌一次（真机 42~57%）。这是宿主行为，不是插件
            # 能消除的损失，单独标注，避免运维把冷启动误判成稳定层的 bug。
            "cold_reason": (
                "host_context_compaction"
                if previous and ceiling >= 0.999 and hit_rate < 0.7
                else ""
            ),
        }
        self._sum_waste += waste
        self._observations += 1
        # 历史记录不需要块哈希：trend()/stats() 只看指标字段。旧实现把整份
        # blocks（数百个 64 字符哈希）复制进 60 条历史，纯内存浪费；相邻比较
        # 所需的上一轮 blocks 由 self._last 单独持有。
        lean = {key: value for key, value in record.items() if key != "blocks"}
        self._records.append(lean)
        self._records = self._records[-self._history :]
        self._last[kind] = {"blocks": blocks, "ts": now, "record": lean}
        self._sum_hit += common
        self._sum_total += total
        if hit_rate < self.target:
            self._low += 1
        return dict(lean)

    def stats(self) -> Dict[str, Any]:
        overall = self._sum_hit / self._sum_total if self._sum_total else 0.0
        recent = [r["hit_rate"] for r in self._records[-20:]]
        return {
            "target": self.target,
            "observations": self._observations,
            "overall_hit_rate": round(overall, 6),
            "recent_mean_hit_rate": round(sum(recent) / len(recent), 6) if recent else 0.0,
            "mean_waste": round(self._sum_waste / self._observations, 6) if self._observations else 0.0,
            "low_samples": self._low,
            "last": {
                key: self._records[-1][key]
                for key in ("kind", "hit_rate", "first_diff", "note", "prompt_tokens")
                if self._records and key in self._records[-1]
            } if self._records else {},
        }

    def trend(self, limit: int = 20) -> List[Dict[str, Any]]:
        return [
            {key: record[key] for key in ("kind", "hit_rate", "first_diff", "note") if key in record}
            for record in self._records[-max(1, int(limit)) :]
        ]

    def reset(self) -> None:
        self._last.clear()
        self._records.clear()
        self._sum_hit = 0.0
        self._sum_total = 0.0
        self._sum_waste = 0.0
        self._low = 0
        self._observations = 0


# --------------------------------------------------------------------------- #
# 离线回放：用宿主保存的 prompt 快照评估稳定策略
# --------------------------------------------------------------------------- #
def replay_snapshots(
    snapshots: List[Dict[str, Any]],
    *,
    stabilize_meta: bool = True,
    freeze_tools: bool = True,
    tail_variable: bool = True,
    stabilize_layout: bool = False,
    simulate_refactor: bool = False,
) -> Dict[str, Any]:
    """对一组按时间排序的 prompt 快照回放稳定策略，返回命中率汇总。

    ``snapshots`` 每项形如宿主 ``logs/maisaka_prompt/planner/*.json``：
    ``{"tool_definitions": [...], "request_items": [...], "metadata": {...}}``。

    ``simulate_refactor=True`` 会把旧插件每轮新建的日程/规则 Item 折叠成
    重构后的**唯一稳定槽**，用于模拟新版本插件的输出；这是模拟值，报告里
    必须标注，不能冒充真机实测。纯函数，不依赖插件实例。
    """

    registry = MetaIdentityRegistry() if stabilize_meta else None
    freezer = ToolTableFreezer() if freeze_tools else None
    prepared: List[Dict[str, Any]] = []
    for snapshot in snapshots:
        tools = snapshot.get("tool_definitions") or []
        if freezer is not None:
            tools, _ = freezer.freeze(tools)
        items: List[Any] = list(snapshot.get("request_items") or [])
        if simulate_refactor:
            kept: List[Any] = []
            for item in items:
                meta = item.get("meta") if isinstance(item, dict) else None
                item_id = str((meta or {}).get("item_id") or "")
                text = item_text(item)
                if item_id.startswith("round-rule-") or "## 日程表（必须遵守）" in text:
                    continue
                if "【自己发言识别规则" in text or "弹幕防伪规则" in text:
                    continue
                kept.append(item)
            items, _ = upsert_tail_rule(kept, "round", "【稳定轮次规则】按直播日程自然回应。")
        if registry is not None:
            items, _ = registry.stabilize(items)
        if stabilize_layout:
            items, _ = stabilize_layout_fn(items)
        if tail_variable and not stabilize_layout:
            # 把每轮重建的「时间/摘要」压到尾部，模拟 upsert_tail_rule 的效果
            variable: List[Any] = []
            rest: List[Any] = []
            for item in items:
                (variable if (is_time_item(item) or is_summary_item(item)) else rest).append(item)
            items = [*rest, *variable]
        prepared.append({"tool_definitions": tools, "request_items": items})
    monitor = PrefixCacheMonitor(dedupe_ms=0.0)
    rates: List[float] = []
    wastes: List[float] = []
    ceilings: List[float] = []
    for index, request in enumerate(prepared):
        report = monitor.observe(
            "planner",
            tool_definitions=request["tool_definitions"],
            items=request["request_items"],
            prompt_tokens=int((snapshots[index].get("metadata") or {}).get("prompt_tokens") or 0),
        )
        if index:  # 第一次没有可比对象
            rates.append(report["hit_rate"])
            wastes.append(report["waste"])
            ceilings.append(report["ceiling"])
    if not rates:
        return {"samples": 0, "mean": 0.0, "min": 0.0, "p50": 0.0,
                "ge_target": 0, "overall": 0.0, "mean_waste": 0.0, "rates": []}
    ordered = sorted(rates)
    return {
        "samples": len(rates),
        "mean": round(sum(rates) / len(rates), 6),
        "min": round(ordered[0], 6),
        "p50": round(ordered[len(ordered) // 2], 6),
        "ge_target": sum(1 for rate in rates if rate >= monitor.target),
        "overall": round(monitor.stats()["overall_hit_rate"], 6),
        "mean_waste": round(sum(wastes) / len(wastes), 6),
        "mean_ceiling": round(sum(ceilings) / len(ceilings), 6),
        "rates": [round(rate, 6) for rate in rates],
    }


# 避免参数名与函数名冲突
stabilize_layout_fn = stabilize_layout
