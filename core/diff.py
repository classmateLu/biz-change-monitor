# -*- coding: utf-8 -*-
"""通用变化检测引擎（纯函数）。v2：字段级事件 + 幂等 event_id。

不依赖浏览器、网络、钉钉或任何平台 SDK —— 可独立测试、独立运行、稳定复现。

输入：两份归一化快照索引 {gid: item}（由 core.models.index_by_id 生成），
     以及可选的门店维度 {gid: [门店名, ...]}。

输出：变化事件列表。每条事件同时携带新（v2）与旧（v1 兼容）两套键：
    event_id            幂等键：sha256(run_id|gid|field|event_type) 截断——
                        同一轮重试/重复处理生成完全相同的事件，存储层可去重
    run_id / observed_at / previous_observed_at    时间语义（v2）
    field / event_type  字段名 + 事件类型（v2）
    type                与 event_type 同值（v1 兼容键）
    gid / name / old_value / new_value / note / date   （v1 兼容键）

event_type 全集：
    new | removed | price_changed | count_changed | unit_price_changed
    | discount_changed | status_changed | store_count_changed | stores_changed
    | name_changed

防误报设计（与需求一一对应）：
- 字段缺失（None/空串）时不产生该字段的变化事件——缺失 ≠ 零值 ≠ 变化
- 数值字段经 canon_number 规范比对——"10.0"/10/10.00 不产生无意义变更
- 门店集合比较与顺序无关；**同数量的一换一（B→C）也能检出**
- 同一对象多字段同时变化 → 多条独立事件（均带 gid+field，可追溯）
"""
from __future__ import annotations

import hashlib

from .models import canon_number, to_number

# 数值型字段 → 事件类型（比对统一走 canon_number 定点比对）
_NUMERIC_FIELDS = (
    ("price", "price_changed"),
    ("count", "count_changed"),
    ("unit_price", "unit_price_changed"),
    ("discount", "discount_changed"),
    ("store_count", "store_count_changed"),
)
# 文本型字段 → 事件类型（双方均非 None/空串才比对）
_TEXT_FIELDS = (
    ("name", "name_changed"),
    ("status", "status_changed"),
)


def _event_id(run_id: str, gid: str, field: str, event_type: str) -> str:
    """稳定幂等键：同一 run 内同一对象同一字段只可能有一条事件。"""
    raw = f"{run_id}|{gid}|{field}|{event_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _base_event(gid: str, name, field: str, event_type: str,
                run_id: str, observed_at: str, previous_observed_at,
                date: str) -> dict:
    return {
        "event_id": _event_id(run_id, gid, field, event_type),
        "run_id": run_id,
        "gid": gid,
        "field": field,
        "event_type": event_type,
        "type": event_type,  # v1 兼容键
        "name": name,
        "observed_at": observed_at,
        "previous_observed_at": previous_observed_at,
        "date": date,
    }


def _is_missing(value) -> bool:
    """缺失语义：None 或空串。零值不算缺失。"""
    return value is None or (isinstance(value, str) and value.strip() == "")


def diff_changes(old_items: dict, new_items: dict,
                 old_stores: dict | None = None, new_stores: dict | None = None,
                 date: str = "", run_id: str = "", observed_at: str = "",
                 previous_observed_at=None) -> list[dict]:
    """比较两份完整快照，返回变化事件列表（纯函数）。

    前提（由调用方保证）：两份快照均通过完整性校验（complete=true）。
    不完整快照不得进入本函数——删除判定依赖此前提。
    """
    changes: list[dict] = []

    # ① removed：旧有今无（前提：本轮采集 complete=true）
    for gid, it in old_items.items():
        if gid not in new_items:
            ev = _base_event(gid, it.get("name"), "lifecycle", "removed",
                             run_id, observed_at, previous_observed_at, date)
            ev.update({"old_value": to_number(it.get("price")),
                       "new_value": "", "note": "对象已不在最新采集结果中"})
            changes.append(ev)

    # ② new + 字段级比对
    for gid, it in new_items.items():
        name = it.get("name")
        if gid not in old_items:
            ev = _base_event(gid, name, "lifecycle", "new",
                             run_id, observed_at, previous_observed_at, date)
            ev.update({"old_value": "", "new_value": to_number(it.get("price")),
                       "note": "新出现的对象"})
            changes.append(ev)
            continue

        oit = old_items[gid]

        # 数值字段：canon 按字段级精度比对（缺失/None 跳过；0 是真实值）
        for field, event_type in _NUMERIC_FIELDS:
            old_v, new_v = oit.get(field), it.get(field)
            old_c, new_c = canon_number(old_v, field=field), canon_number(new_v, field=field)
            if old_c is None or new_c is None:
                continue  # 任一侧缺失 → 不比对（缺失 ≠ 变化）
            if old_c != new_c:
                ev = _base_event(gid, name, field, event_type,
                                 run_id, observed_at, previous_observed_at, date)
                ev.update({"old_value": old_v, "new_value": new_v,
                           "note": f"{field} {old_c} → {new_c}"})
                changes.append(ev)

        # 文本字段：双方均非缺失才比对
        for field, event_type in _TEXT_FIELDS:
            old_v, new_v = oit.get(field), it.get(field)
            if _is_missing(old_v) or _is_missing(new_v):
                continue
            if old_v != new_v:
                ev = _base_event(gid, name, field, event_type,
                                 run_id, observed_at, previous_observed_at, date)
                ev.update({"old_value": old_v, "new_value": new_v,
                           "note": f"{field}变更：{old_v} → {new_v}"})
                changes.append(ev)

        # 门店集合：仅当双方都提供了门店维度才比较（None=未采集=未知，跳过）
        if old_stores is not None and new_stores is not None:
            old_s = set(old_stores.get(gid) or [])
            new_s = set(new_stores.get(gid) or [])
            if old_s != new_s:
                added = "、".join(sorted(new_s - old_s)) or "无"
                removed = "、".join(sorted(old_s - new_s)) or "无"
                ev = _base_event(gid, name, "stores", "stores_changed",
                                 run_id, observed_at, previous_observed_at, date)
                ev.update({"old_value": sorted(old_s), "new_value": sorted(new_s),
                           "note": f"新增门店：{added}；减少门店：{removed}"[:900]})
                changes.append(ev)

    return changes


def summarize(changes: list[dict]) -> dict:
    """按类型计数，用于播报摘要。"""
    counts: dict[str, int] = {}
    for c in changes:
        counts[c["type"]] = counts.get(c["type"], 0) + 1
    return counts
