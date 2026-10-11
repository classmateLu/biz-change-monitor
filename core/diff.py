# -*- coding: utf-8 -*-
"""通用变化检测引擎（纯函数）。v2.1：字段级事件 + 内容幂等 event_id。

不依赖浏览器、网络、钉钉或任何平台 SDK —— 可独立测试、独立运行、稳定复现。

输入：两份归一化快照索引 {gid: item}（由 core.models.index_by_id 生成），
     以及可选的门店维度 {gid: [门店名, ...]}。

输出：变化事件列表。每条事件同时携带新（v2）与旧（v1 兼容）两套键：
    event_id            内容幂等键（语义见下）——同键重复落盘在存储层去重
    run_id / observed_at / previous_observed_at    时间语义（v2）
    field / event_type  字段名 + 事件类型（v2）
    type                与 event_type 同值（v1 兼容键）
    gid / name / old_value / new_value / note / date   （v1 兼容键）

event_id 语义（合并前终审 §1.3 修订：**内容键，不含 run_id**）：
    event_id = sha256(gid|field|event_type|previous_observed_at|稳定旧值|稳定新值)
    - 「同一运行重放」→ 同键 → 落盘去重（无重复行）
    - 「崩溃恢复后不同 run 对**同一基线对**的再观测」→ 同键 → 落盘去重（无重复行）
    - 「不同基线对之间再次发生的真实变化」→ previous_observed_at 不同
      → 各自成事件（绝不因去重而漏报）
    run_id 仍随事件持久化（溯源哪一轮首次发现），但不参与幂等键。

event_type 全集：
    new | removed | name_changed | price_changed | count_changed
    | unit_price_changed | discount_changed | status_changed
    | store_count_changed | stores_changed
    | {field}_missing   字段键意外消失告警（field ∈ 监控字段，语义见下）

缺失语义三分（合并前终审 §3）：
    - 新快照**键不存在**且旧快照该字段有值 → {field}_missing 结构告警
      （疑似数据源/适配器结构变化，绝不静默跳过）
    - 键存在但值为 None/空串 → 缺失值 ≠ 变化，跳过比对（既有规则不变）
    - 必需字段（gid/name/price）键缺失由 check_required_fields 闸门在更早处
      fail-closed 拒绝（main 流程中到不了 diff）
    - 旧缺键、新有值 → 字段出现，非业务变化，零事件（不虚构变化方向）

防误报设计（与需求一一对应）：
- 数值字段经 canon_number 按字段级精度规范比对——"10.0"/10/10.00 不产生无意义变更
- 门店集合比较与顺序无关；**同数量的一换一（B→C）也能检出**
- 同一对象多字段同时变化 → 多条独立事件（均带 gid+field，可追溯）
"""
from __future__ import annotations

import hashlib
import json

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


def _hash_val(v) -> str:
    """幂等键用的稳定序列化：标量 str()；集合/字典排序后 JSON（跨运行确定性）。"""
    if isinstance(v, (list, tuple, set)):
        return json.dumps(sorted(v), ensure_ascii=False, default=str)
    if isinstance(v, dict):
        return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)
    return str(v)


def _content_event_id(gid: str, field: str, event_type: str,
                      previous_observed_at, old_value, new_value) -> str:
    """内容幂等键（不含 run_id，语义见模块 docstring）。"""
    raw = "|".join((gid, field, event_type, str(previous_observed_at or ""),
                    _hash_val(old_value), _hash_val(new_value)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _set_id(ev: dict, previous_observed_at, old_hash, new_hash) -> None:
    """事件构建收尾：按内容键回填 event_id。"""
    ev["event_id"] = _content_event_id(ev["gid"], ev["field"], ev["event_type"],
                                       previous_observed_at, old_hash, new_hash)


def _base_event(gid: str, name, field: str, event_type: str,
                run_id: str, observed_at: str, previous_observed_at,
                date: str) -> dict:
    return {
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
            old_price = to_number(it.get("price"))
            ev = _base_event(gid, it.get("name"), "lifecycle", "removed",
                             run_id, observed_at, previous_observed_at, date)
            ev.update({"old_value": old_price, "new_value": "",
                       "note": "对象已不在最新采集结果中"})
            _set_id(ev, previous_observed_at, old_price, "")
            changes.append(ev)

    # ② new + 字段级比对
    for gid, it in new_items.items():
        name = it.get("name")
        if gid not in old_items:
            new_price = to_number(it.get("price"))
            ev = _base_event(gid, name, "lifecycle", "new",
                             run_id, observed_at, previous_observed_at, date)
            ev.update({"old_value": "", "new_value": new_price,
                       "note": "新出现的对象"})
            _set_id(ev, previous_observed_at, "", new_price)
            changes.append(ev)
            continue

        oit = old_items[gid]

        # 数值字段：canon 按字段级精度比对；键消失 → _missing 结构告警
        for field, event_type in _NUMERIC_FIELDS:
            old_v, new_v = oit.get(field), it.get(field)
            old_c = canon_number(old_v, field=field)
            if field not in it:
                # 新快照键消失：旧值有意义 → 告警；旧值本就缺失 → 无信息量，跳过
                if field in oit and old_c is not None:
                    ev = _base_event(gid, name, field, f"{field}_missing",
                                     run_id, observed_at, previous_observed_at, date)
                    ev.update({"old_value": old_v, "new_value": None,
                               "note": f"{field} 字段键意外消失"
                                       f"（疑似数据源/适配器结构变化）"})
                    _set_id(ev, previous_observed_at, old_c, None)
                    changes.append(ev)
                continue
            new_c = canon_number(new_v, field=field)
            if old_c is None or new_c is None:
                continue  # 任一侧值为缺失（None/空串）→ 不比对（缺失值 ≠ 变化）
            if old_c != new_c:
                ev = _base_event(gid, name, field, event_type,
                                 run_id, observed_at, previous_observed_at, date)
                ev.update({"old_value": old_v, "new_value": new_v,
                           "note": f"{field} {old_c} → {new_c}"})
                _set_id(ev, previous_observed_at, old_c, new_c)
                changes.append(ev)

        # 文本字段：键消失 → _missing 结构告警；双方均非缺失才比对
        for field, event_type in _TEXT_FIELDS:
            old_v, new_v = oit.get(field), it.get(field)
            if field not in it:
                if field in oit and not _is_missing(old_v):
                    ev = _base_event(gid, name, field, f"{field}_missing",
                                     run_id, observed_at, previous_observed_at, date)
                    ev.update({"old_value": old_v, "new_value": None,
                               "note": f"{field} 字段键意外消失"
                                       f"（疑似数据源/适配器结构变化）"})
                    _set_id(ev, previous_observed_at, old_v, None)
                    changes.append(ev)
                continue
            if _is_missing(old_v) or _is_missing(new_v):
                continue
            if old_v != new_v:
                ev = _base_event(gid, name, field, event_type,
                                 run_id, observed_at, previous_observed_at, date)
                ev.update({"old_value": old_v, "new_value": new_v,
                           "note": f"{field}变更：{old_v} → {new_v}"})
                _set_id(ev, previous_observed_at, old_v, new_v)
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
                _set_id(ev, previous_observed_at, sorted(old_s), sorted(new_s))
                changes.append(ev)

    return changes


def summarize(changes: list[dict]) -> dict:
    """按类型计数，用于播报摘要。"""
    counts: dict[str, int] = {}
    for c in changes:
        counts[c["type"]] = counts.get(c["type"], 0) + 1
    return counts
