#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可复现演示脚本：仓库真实引擎 + 示例适配器，数据全部虚构。

运行：python run_demo.py

做什么：
    1. 用 ExampleAdapter 两个不同 seed 模拟两天采集，走真实链路：
       validate_collection → save_snapshot → load_latest_snapshot → diff_changes
    2. 用"今天"同 seed 重新实例化适配器再采一遍（数据源没变），
       验证可复现性：引擎对相同输入产出零事件，不制造假新闻。

边界声明：
    - 快照写入系统临时目录（tempfile），不触碰仓库 data/；
    - 只调用仓库真实引擎函数，未复制/未修改任何核心逻辑；
    - 固定 seed，任何人克隆仓库运行都得到与本页演示一致的输出。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from adapters.example_adapter import ExampleAdapter
from core.diff import diff_changes, summarize
from core.models import check_required_fields, index_by_id
from core.snapshot import load_latest_snapshot, save_snapshot
from core.validation import validate_collection

ITEM_COUNT = 60


def collect(seed: int):
    """一次真实采集：校验闸门与 main.py 完全一致。"""
    result = ExampleAdapter(item_count=ITEM_COUNT, seed=seed).fetch()
    items_index = index_by_id(result.items)
    structure_errors = list(check_required_fields(result.items))
    ok, reason = validate_collection(
        len(result.items), min_records=50,
        failed_pages=result.failed_pages, expected_count=result.total,
        structure_errors=structure_errors)
    assert ok, f"校验未通过（演示数据应完整）: {reason}"
    return result, items_index


def main() -> int:
    out = Path(tempfile.mkdtemp(prefix="biz-demo-"))
    day1, day2 = "2026-10-05", "2026-10-06"

    print(f"# ExampleAdapter 两个 seed 模拟两天，走完整 "
          f"save_snapshot → load_latest_snapshot → diff_changes 链路")
    r1, idx1 = collect(seed=42)
    print(f"[昨日 {day1}] 完整性校验: PASS（{len(idx1)} 个对象）")
    save_snapshot(out, day1, r1.items, stores=r1.stores)

    r2, idx2 = collect(seed=7)
    print(f"[今日 {day2}] 完整性校验: PASS（{len(idx2)} 个对象）")
    base_date, base = load_latest_snapshot(out, before_date=day2)
    old_items = index_by_id(base["items"])
    changes = diff_changes(old_items, idx2, base.get("stores"), r2.stores,
                           date=day2)
    counts = summarize(changes)
    print(f"对比 {base_date}：变化 {len(changes)} 条 {counts}")
    for c in changes[:5]:
        print(f"  [{c['type']}] {c['gid']} {c.get('name') or ''} {c['note']}")
    print(f"  ……其余 {len(changes) - 5} 条")

    print()
    print("# 可复现性实测：同 seed 重新实例化再采一天（数据源没变）")
    r3, idx3 = collect(seed=7)
    changes3 = diff_changes(idx2, idx3, r2.stores, r3.stores, date=day2)
    counts3 = summarize(changes3)
    print(f"对比 {day2}：变化 {len(changes3)} 条 {counts3}"
          f"   ← 引擎不制造假新闻")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
