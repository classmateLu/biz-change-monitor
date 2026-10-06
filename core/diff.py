# -*- coding: utf-8 -*-
"""通用变化检测引擎（纯函数）。

不依赖浏览器、网络、钉钉或任何平台 SDK —— 可独立测试、独立运行、稳定复现。

输入：两份归一化快照索引 {gid: item}（由 core.models.index_by_id 生成），
     以及可选的门店维度 {gid: [门店名, ...]}。

输出：变化事件列表，每条包含：
    type      new | removed | price_changed | name_changed | stores_changed
    gid       业务对象 ID
    name      当前名称
    old_value / new_value   变化前后的关键值
    note      人读的差异说明
    date      本次检测日期标签

防误报设计（与需求一一对应）：
- 价格/名称字段缺失（None）时不产生该字段的变化事件——字段缺失 ≠ 价格归零
- 价格经 to_number 容错——"99.0" 与 99 不产生无意义变更
- 门店集合比较与顺序无关
- 同一对象多字段同时变化 → 产生多条独立事件（均带 gid，可追溯）
"""
from __future__ import annotations

from .models import to_number


def diff_changes(old_items: dict, new_items: dict,
                 old_stores: dict | None = None, new_stores: dict | None = None,
                 date: str = "") -> list[dict]:
    changes: list[dict] = []

    # ① removed：旧有今无（前提：本次采集已通过完整性校验，由调用方保证）
    for gid, it in old_items.items():
        if gid not in new_items:
            changes.append({
                "type": "removed", "gid": gid, "name": it.get("name"),
                "old_value": to_number(it.get("price")), "new_value": "",
                "note": "对象已不在最新采集结果中", "date": date,
            })

    # ② new / ③ price_changed / ④ name_changed
    for gid, it in new_items.items():
        name = it.get("name")
        if gid not in old_items:
            changes.append({
                "type": "new", "gid": gid, "name": name,
                "old_value": "", "new_value": to_number(it.get("price")),
                "note": "新出现的对象", "date": date,
            })
            continue

        oit = old_items[gid]

        # 价格：双方都有有效值才比较（缺失 ≠ 变化）
        new_price, old_price = to_number(it.get("price")), to_number(oit.get("price"))
        if new_price is not None and old_price is not None and new_price != old_price:
            changes.append({
                "type": "price_changed", "gid": gid, "name": name,
                "old_value": old_price, "new_value": new_price,
                "note": f"价格 {old_price} → {new_price}", "date": date,
            })

        # 名称：双方都非空才比较（缺失 ≠ 变化）
        new_name, old_name = it.get("name"), oit.get("name")
        if new_name is not None and old_name is not None and new_name != old_name:
            changes.append({
                "type": "name_changed", "gid": gid, "name": name,
                "old_value": old_name, "new_value": new_name,
                "note": f"名称变更：{old_name} → {new_name}", "date": date,
            })

        # ⑤ stores_changed：仅当双方都提供了门店维度才比较
        if old_stores is not None and new_stores is not None:
            old_s = set(old_stores.get(gid) or [])
            new_s = set(new_stores.get(gid) or [])
            if old_s != new_s:
                added = "、".join(sorted(new_s - old_s)) or "无"
                removed = "、".join(sorted(old_s - new_s)) or "无"
                changes.append({
                    "type": "stores_changed", "gid": gid, "name": name,
                    "old_value": sorted(old_s), "new_value": sorted(new_s),
                    "note": f"新增门店：{added}；减少门店：{removed}"[:900], "date": date,
                })

    return changes


def summarize(changes: list[dict]) -> dict:
    """按类型计数，用于播报摘要。"""
    counts: dict[str, int] = {}
    for c in changes:
        counts[c["type"]] = counts.get(c["type"], 0) + 1
    return counts
