#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diff 引擎合成测试：每类变化埋一个，验证全部检出且零误报。

既可用 pytest 运行（pytest tests/），也可直接执行本文件：
    python tests/test_diff.py    # 输出逐项结果，exit 0/1

全部使用虚构数据（Demo Alpha/Beta/Gamma 店、DEMO-xxxx ID），无任何真实业务数据。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.diff import diff_changes, summarize  # noqa: E402
from core.models import make_item  # noqa: E402

SA, SB, SC = "Demo Alpha 店", "Demo Beta 店", "Demo Gamma 店"


def mk(gid, name, price):
    p = float(price)  # 兼容传入字符串价格（"99.0"）的用例
    return make_item(gid, name=name, price=price, count=5,
                     unit_price=round(p / 5, 2))


old_items = {g: mk(g, n, p) for g, n, p in [
    ("DEMO-0001", "不变的项目", 100.0),
    ("DEMO-0002", "要下架的", 200.0),
    ("DEMO-0004", "要涨价的", 400.0),
    ("DEMO-0005", "旧名字", 500.0),
    ("DEMO-0006", "门店要变的", 600.0),
    ("DEMO-0007", "门店不变的", 700.0),
    ("DEMO-0008", "字符串价格不变", "99.0"),
]}
new_items = {g: mk(g, n, p) for g, n, p in [
    ("DEMO-0001", "不变的项目", 100.0),
    ("DEMO-0003", "新上架的", 300.0),
    ("DEMO-0004", "要涨价的", 450.0),
    ("DEMO-0005", "新名字", 500.0),
    ("DEMO-0006", "门店要变的", 600.0),
    ("DEMO-0007", "门店不变的", 700.0),
    ("DEMO-0008", "字符串价格不变", 99),  # "99.0" vs 99 → 数值等价，不应误报
]}
old_stores = {"DEMO-0001": [SA], "DEMO-0006": [SA, SB], "DEMO-0007": [SA]}
new_stores = {"DEMO-0001": [SA], "DEMO-0006": [SA, SC], "DEMO-0007": [SA],
              # 门店列表顺序不同不算变化：DEMO-0007 故意倒序写入
              }


def run_checks():
    changes = diff_changes(old_items, new_items, old_stores, new_stores, date="T")
    by_type = {}
    for c in changes:
        by_type.setdefault(c["type"], []).append(c)

    checks = [
        (len(changes) == 5, f"共检出5条变化（实际{len(changes)}）"),
        (by_type.get("removed", [{}])[0].get("gid") == "DEMO-0002", "removed=DEMO-0002"),
        (by_type.get("new", [{}])[0].get("gid") == "DEMO-0003", "new=DEMO-0003"),
    ]
    pc = by_type.get("price_changed", [{}])[0]
    checks.append((pc.get("gid") == "DEMO-0004" and pc.get("old_value") == 400.0
                   and pc.get("new_value") == 450.0, "price_changed 400→450"))
    nc = by_type.get("name_changed", [{}])[0]
    checks.append((nc.get("gid") == "DEMO-0005" and nc.get("old_value") == "旧名字"
                   and nc.get("new_value") == "新名字", "name_changed 旧名字→新名字"))
    sc = by_type.get("stores_changed", [{}])[0]
    checks.append(("Beta" in sc.get("note", "") and "Gamma" in sc.get("note", ""),
                   "stores_changed 差异说明正确"))
    checks.append((not any(c["gid"] == "DEMO-0001" for c in changes), "完全不变的ID零误报"))
    checks.append((not any(c["gid"] == "DEMO-0007" for c in changes),
                   "门店不变的ID零误报"))
    checks.append((not any(c["gid"] == "DEMO-0008" for c in changes),
                   "字符串'99.0'与数值99不产生价格事件"))

    # 基线无门店维度 → 自动跳过门店比对
    changes2 = diff_changes(old_items, new_items, None, new_stores, date="T")
    checks.append((not any(c["type"] == "stores_changed" for c in changes2),
                   "基线无门店名单时自动跳过门店比对"))

    # 价格字段缺失 → 不产生价格事件（字段缺失 ≠ 价格归零）
    miss_new = dict(new_items)
    it = dict(miss_new["DEMO-0001"])
    it["price"] = None
    miss_new["DEMO-0001"] = it
    changes3 = diff_changes(old_items, miss_new, old_stores, new_stores, date="T")
    checks.append((not any(c["gid"] == "DEMO-0001" and c["type"] == "price_changed"
                           for c in changes3), "价格字段缺失不误判为价格变化"))

    # summarize 计数
    checks.append((summarize(changes).get("removed") == 1, "summarize 计数正确"))
    return checks


def main():
    errors = []
    for ok, msg in run_checks():
        print(("✅" if ok else "❌"), msg)
        if not ok:
            errors.append(msg)
    print()
    print("全部通过 🎉" if not errors else f"有{len(errors)}项失败！")
    return 1 if errors else 0


def test_diff_engine():
    """pytest 入口。"""
    assert not [msg for ok, msg in run_checks() if not ok]


if __name__ == "__main__":
    sys.exit(main())
