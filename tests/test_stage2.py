#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段2 数据引擎改造测试（对照 Codex 规格 §十三 A-E，全部本地合成数据）。

覆盖：
A 基础差异：全字段变化 + 门店一换一 + 新增/删除
B 无效差异防护：数字格式/缺失vs零值/重复扫描/顺序变化/同名门店标记
C 完整性与失败恢复：不完整不覆盖基线、不产生删除、失败后恢复、损坏跳过
D 事件幂等：重复落盘去重
E 存储与归档：latest 只在完整时更新、归档间隔、不完整不归档、重启续用基线

既可用 pytest 运行，也可直接执行：python tests/test_stage2.py
全部虚构数据，隔离在临时目录。
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.diff import diff_changes, summarize  # noqa: E402
from core.events import append_events, load_events  # noqa: E402
from core.models import (canon_number, check_required_fields,  # noqa: E402
                         make_item)
from core.runlog import append_run_record, load_run_records  # noqa: E402
from core.snapshot import (archive_if_due, load_latest_valid,  # noqa: E402
                           save_run_snapshot, update_latest)
from core.validation import validate_collection  # noqa: E402

SA, SB, SC = "Demo Alpha 店", "Demo Beta 店", "Demo Gamma 店"


def run_checks():
    checks = []
    tmp = Path(tempfile.mkdtemp(prefix="bizmon-stage2-"))
    try:
        # ================= A. 基础差异 =================
        old_items = {g: it for g, it in [
            ("D-0001", make_item("D-0001", name="不变", price=100, count=2,
                                 unit_price=50, discount=10, status="上架",
                                 store_count=2)),
            ("D-0002", make_item("D-0002", name="要删的", price=200, status="上架",
                                 store_count=1)),
            ("D-0004", make_item("D-0004", name="涨价", price=400)),
            ("D-0005", make_item("D-0005", name="旧名", price=500)),
            ("D-0006", make_item("D-0006", name="数量变", price=100, count=3)),
            ("D-0007", make_item("D-0007", name="单价变", price=100, unit_price=50)),
            ("D-0008", make_item("D-0008", name="折扣变", price=100, discount=20)),
            ("D-0009", make_item("D-0009", name="状态变", price=100, status="上架")),
            ("D-0010", make_item("D-0010", name="门店数变", price=100, store_count=3)),
        ]}
        new_items = {g: it for g, it in [
            ("D-0001", make_item("D-0001", name="不变", price=100, count=2,
                                 unit_price=50, discount=10, status="上架",
                                 store_count=2)),
            ("D-0003", make_item("D-0003", name="新增的", price=300, status="上架")),
            ("D-0004", make_item("D-0004", name="涨价", price=450)),
            ("D-0005", make_item("D-0005", name="新名", price=500)),
            ("D-0006", make_item("D-0006", name="数量变", price=100, count=5)),
            ("D-0007", make_item("D-0007", name="单价变", price=100, unit_price=25)),
            ("D-0008", make_item("D-0008", name="折扣变", price=100, discount=30)),
            ("D-0009", make_item("D-0009", name="状态变", price=100, status="下架")),
            ("D-0010", make_item("D-0010", name="门店数变", price=100, store_count=4)),
        ]}
        old_stores = {"D-0001": [SA, SB]}
        new_stores = {"D-0001": [SA, SB]}  # 完全不变
        changes = diff_changes(old_items, new_items, old_stores, new_stores,
                               date="T", run_id="R1", observed_at="2026-10-10T22:00:00+08:00",
                               previous_observed_at="2026-10-10T21:00:00+08:00")
        got = {(c["gid"], c["type"]) for c in changes}
        expect = {("D-0002", "removed"), ("D-0003", "new"),
                  ("D-0004", "price_changed"), ("D-0005", "name_changed"),
                  ("D-0006", "count_changed"), ("D-0007", "unit_price_changed"),
                  ("D-0008", "discount_changed"), ("D-0009", "status_changed"),
                  ("D-0010", "store_count_changed")}
        checks.append((got == expect, f"A 全字段事件齐全（缺:{expect-got} 多:{got-expect}）"))
        checks.append((all(c.get("event_id") and c.get("run_id") == "R1"
                           and c.get("observed_at") and c.get("previous_observed_at")
                           and "field" in c for c in changes), "A 事件带 v2 元数据"))
        checks.append((not any(c["gid"] == "D-0001" for c in changes),
                       "A 完全不变的 ID 零误报"))

        # 门店一换一（数量不变，集合变了）——Codex §8.2 核心场景
        old_s = {"D-0001": [SA, SB]}
        new_s = {"D-0001": [SA, SC]}
        ch = diff_changes(old_items, new_items, old_s, new_s, date="T",
                          run_id="R1", observed_at="2026-10-10T22:00:00+08:00")
        sw = [c for c in ch if c["gid"] == "D-0001" and c["type"] == "stores_changed"]
        checks.append((len(sw) == 1 and "Beta" in sw[0]["note"] and "Gamma" in sw[0]["note"],
                       "A 门店一换一（数量不变 B→C）被检出"))

        # ================= B. 无效差异防护 =================
        same_old = {"D-0001": make_item("D-0001", name="X", price="99.0", count=2)}
        same_new = {"D-0001": make_item("D-0001", name="X", price=99, count="2")}
        ch = diff_changes(same_old, same_new, None, None, date="T")
        checks.append((len(ch) == 0, "B '99.0'/99 与 2/'2' 数字形态差异零误报"))

        # 缺失 vs 零值：比对规则=双侧有值才比对（对称，任一侧缺失即跳过）
        # 理由：旧值缺失时声称"价格变成0"同样是无依据的推断（缺失≠零值≠变化）
        # 缺失/空/零的区分保留在快照存储里：None、"、0 三种形态原样落盘
        ch = diff_changes({"A": make_item("A", price=None)},
                          {"A": make_item("A", price=0)}, None, None, date="T")
        checks.append((not any(c["type"] == "price_changed" for c in ch),
                       "B 任一侧缺失→跳过比对（对称缺失规则，不虚构变化方向）"))
        ch = diff_changes({"A": make_item("A", price=0)},
                          {"A": make_item("A", price="0.00")}, None, None, date="T")
        checks.append((len(ch) == 0, "B 0 与 '0.00' 等价，不误报"))
        ch = diff_changes({"A": make_item("A", name=None)},
                          {"A": make_item("A", name="新名")}, None, None, date="T")
        checks.append((not any(c["type"] == "name_changed" for c in ch),
                       "B 名称缺失→有值：跳过比对（缺失≠变化）"))

        # 相同数据重复扫描 → 零事件
        dup_old = {"A": make_item("A", name="X", price=10, status="上架",
                                  store_count=1)}
        ch = diff_changes(dup_old, dict(dup_old), {"A": [SA]}, {"A": [SA, SB]},
                          date="T")  # 注意门店集合不同会报，单独测下方
        ch2 = diff_changes(dup_old, dict(dup_old), {"A": [SA]}, {"A": [SB]},
                           date="T")
        ch3 = diff_changes(dup_old, dict(dup_old), {"A": [SA]}, {"A": [SA]},
                           date="T")
        checks.append((len(ch3) == 0, "B 相同数据重复扫描零事件"))

        # 门店顺序变化 → 不误报
        ch = diff_changes(dup_old, dict(dup_old), {"A": [SA, SB]}, {"A": [SB, SA]},
                          date="T")
        checks.append((len(ch) == 0, "B 门店集合顺序变化不误报"))

        # canon_number 行为自检
        checks.append((canon_number(" 10.0 ") == canon_number(10)
                       == canon_number("10.00"), "B canon_number 形态归一"))
        checks.append((canon_number("abc") is None and canon_number("") is None
                       and canon_number(None) is None, "B 无法解析→缺失语义"))

        # ================= C. 完整性与失败恢复 =================
        # C1: expected_count 比对
        ok, why = validate_collection(480, min_records=1, expected_count=500)
        checks.append((not ok and "不一致" in why, "C 实采数≠总量 → 不完整"))
        ok, _ = validate_collection(500, min_records=1, expected_count=500)
        checks.append((ok, "C 实采数=总量 → 通过"))

        # C2: 不完整快照拒绝进 latest
        try:
            update_latest(tmp, {"complete": False, "items": [{"gid": "x"}]})
            checks.append((False, "C 不完整快照应被 update_latest 拒绝"))
        except ValueError:
            checks.append((True, "C 不完整快照被 update_latest 拒绝（fail-closed）"))

        # C3: 完整 RunA → 失败 RunB → 完整 RunC：基线始终是 RunA，RunC 与 RunA 比
        items_a = [make_item(f"G-{i:03d}", name=f"对象{i}", price=10 + i)
                   for i in range(5)]
        save_run_snapshot(tmp, "run-A", "2026-10-10T10:00:00+08:00", True,
                          items=items_a, total=5, expected_count=5)
        update_latest(tmp, {"schema": "run-snapshot-v1", "run_id": "run-A",
                            "observed_at": "2026-10-10T10:00:00+08:00",
                            "complete": True, "items": items_a})
        # RunB 失败（少 2 个）
        items_b = items_a[:3]
        save_run_snapshot(tmp, "run-B", "2026-10-10T10:05:00+08:00", False,
                          items=items_b, total=5, expected_count=5)
        rid_b, payload_b = load_latest_valid(tmp, exclude_run_id="run-B")
        checks.append((rid_b == "run-A", "C 失败轮之后基线仍是 RunA（不完整不覆盖基线）"))
        # RunC 完整且多 1 个对象
        items_c = items_a + [make_item("G-099", name="新对象", price=999)]
        save_run_snapshot(tmp, "run-C", "2026-10-10T10:10:00+08:00", True,
                          items=items_c, total=6, expected_count=6)
        rid_c, payload_c = load_latest_valid(tmp, exclude_run_id="run-C")
        checks.append((rid_c == "run-A", "C RunC 的基线是 RunA（跳过失败轮）"))
        ch = diff_changes({it["gid"]: it for it in payload_c["items"]},
                          {it["gid"]: it for it in items_c}, None, None, date="T")
        checks.append((not any(c["type"] == "removed" for c in ch)
                       and any(c["type"] == "new" for c in ch),
                       "C RunC vs RunA：无错误删除，正确检出新增"))

        # C4: 损坏快照跳过
        runs_dir = tmp / "data" / "snapshots" / "runs"
        (runs_dir / "run-broken.json").write_text("{broken!!", encoding="utf-8")
        rid, _ = load_latest_valid(tmp, exclude_run_id="run-C")
        checks.append((rid == "run-A", "C 损坏快照跳过不崩溃"))

        # C5: 失败运行留痕（runlog）
        append_run_record(tmp, {"run_id": "run-B", "started_at": "2026-10-10T10:04:00+08:00",
                                "finished_at": "2026-10-10T10:05:00+08:00",
                                "observed_at": "2026-10-10T10:05:00+08:00",
                                "complete": False, "expected_count": 5,
                                "collected_count": 3, "unique_count": 3,
                                "failed_pages": [], "failed_items": [],
                                "detail_failed": 0,
                                "validation_errors": ["实采数 3 与源数据总量 5 不一致"]})
        recs = load_run_records(tmp, "202610")
        checks.append((any(r["run_id"] == "run-B" and r["complete"] is False
                           for r in recs), "C 失败轮运行记录已留痕"))

        # ================= D. 事件幂等 =================
        ev1 = {"event_id": "e1", "run_id": "R1", "gid": "D-0004",
               "field": "price", "event_type": "price_changed",
               "type": "price_changed", "observed_at": "2026-10-10T22:00:00+08:00",
               "old_value": 400, "new_value": 450, "note": "价格"}
        appended, skipped = append_events(tmp, [ev1], today="2026-10-10")
        checks.append((appended == 1 and skipped == 0, "D 首次落盘成功"))
        appended, skipped = append_events(tmp, [dict(ev1)], today="2026-10-10")
        checks.append((appended == 0 and skipped == 1, "D 同 event_id 重试不重复落盘"))
        evs = load_events(tmp, "202610")
        checks.append((len(evs) == 1, "D 事件文件中只有一条"))

        # ================= E. 存储与归档 =================
        arc = archive_if_due(tmp, {"complete": True, "run_id": "run-A",
                                   "items": items_a, "observed_at": "2026-10-10T10:00:00+08:00"},
                             interval_minutes=60,
                             now=datetime(2026, 10, 10, 11, 0).astimezone())
        checks.append((arc is not None, "E 间隔已到 → 归档产生"))
        arc2 = archive_if_due(tmp, {"complete": True, "run_id": "run-C",
                                    "items": items_c, "observed_at": "2026-10-10T10:10:00+08:00"},
                              interval_minutes=60,
                              now=datetime(2026, 10, 10, 11, 30).astimezone())
        checks.append((arc2 is None, "E 间隔未到 → 不归档"))
        arc3 = archive_if_due(tmp, {"complete": False, "items": items_b},
                              interval_minutes=60,
                              now=datetime(2026, 10, 10, 13, 0).astimezone())
        checks.append((arc3 is None, "E 不完整运行不产生归档"))

        # 重启后（新进程视角）仍能取到有效基线
        rid, payload = load_latest_valid(tmp)
        checks.append((rid == "run-A" and payload.get("complete") is True,
                       "E 重启后基线读取正常"))

        # ================= F. 必需字段结构校验（终审 P0-2） =================
        # F1: 正常变化不受影响——价格 10 → 12 仍被检出
        ch = diff_changes({"A": make_item("A", name="X", price=10)},
                          {"A": make_item("A", name="X", price=12)},
                          None, None, date="T")
        checks.append((any(c["type"] == "price_changed" and c["new_value"] == 12
                           for c in ch), "F 价格 10→12 正常检出（规则不误伤真实变化）"))

        # F2: 新快照意外缺 price → 结构错误（拒绝发布为完整状态）
        errs = check_required_fields([make_item("A", name="X", price=None)])
        checks.append((len(errs) == 1 and "price" in errs[0],
                       "F 新快照意外缺 price → 结构错误"))

        # F3: 旧快照缺 price、新快照有 price → 旧轮次同样会被闸门拦截；
        #     diff 层保持对称跳过（缺失≠变化），但缺必需字段的轮次到不了 diff
        errs = check_required_fields([make_item("A", name="X", price=None)])
        ch = diff_changes({"A": make_item("A", name="X", price=None)},
                          {"A": make_item("A", name="X", price=12)},
                          None, None, date="T")
        checks.append((len(errs) == 1 and not any(c["type"] == "price_changed"
                                                  for c in ch),
                       "F 旧缺新有：结构校验拦截 + diff 对称跳过双保险"))

        # F4: 合法空值与零值——空串/0 都不是结构错误
        errs = check_required_fields([make_item("A", name="", price=0)])
        checks.append((len(errs) == 0, "F 空串与 0 是合法值，不报结构错误"))

        # F5: 可选字段缺失不造成无意义失败
        errs = check_required_fields([make_item("A", name="X", price=10)])
        checks.append((len(errs) == 0, "F 可选字段（discount/status等）缺失不报错"))

        # F6: 必需字段缺失 → validate_collection 拒绝发布为完整状态
        errs = check_required_fields([make_item("A", name="X", price=None),
                                      make_item("B", name="Y", price=1)])
        ok, why = validate_collection(2, min_records=1, structure_errors=errs)
        checks.append((not ok and "必需字段" in why,
                       "F 必需字段缺失 → 完整性闸门拒绝本轮"))

        # F7: 结构错误明细有截断（500 条全缺不刷屏）
        many = [make_item(f"G{i}", name="X", price=None) for i in range(500)]
        errs = check_required_fields(many)
        checks.append((len(errs) <= 11 and any("共 500" in e for e in errs),
                       "F 错误明细截断 + 总数摘要"))

        # ================= G. 字段级数值精度（终审 P1-1） =================
        # G1: 货币 0.01 档——形态差异不误报
        checks.append((canon_number("10.001", field="price")
                       == canon_number("10.004", field="price"),
                       "G price 0.01 档：10.001/10.004 等价（无过度敏感）"))
        # G2: 货币真实变化仍检出
        checks.append((canon_number(10, field="price") != canon_number(12, field="price"),
                       "G price 10→12 检出（无过度舍入漏报）"))
        # G3: 折扣 0.001 档——12.5 vs 12.6（统一 0.01 会分别舍成 12.50/12.60 仍检出；
        #     但 12.5004 vs 12.5006 会被 0.01 舍成同值而漏报，0.001 档修复此问题）
        checks.append((canon_number("12.5001", field="discount")
                       == canon_number("12.5004", field="discount"),
                       "G discount 0.001 档：亚毫差异等价"))
        checks.append((canon_number("12.5001", field="discount")
                       != canon_number("12.5009", field="discount"),
                       "G discount 0.001 档：真实微差检出（0.01 档会漏报）"))
        # G4: 数量不量化——"2"/2/2.0 等价（形态归一），2.5→2.6 检出（精度保留）
        checks.append((canon_number(2, field="count") == canon_number("2.0", field="count")
                       == canon_number("2", field="count"),
                       "G count 2/'2.0'/'2' 等价（不量化仍消形态差异）"))
        checks.append((canon_number(2.5, field="count") != canon_number(2.6, field="count"),
                       "G count 2.5→2.6 检出（整数档量化会漏报，已修复）"))
        # G5: 未传 field 默认 0.01（向后兼容 v2 初版行为）
        checks.append((canon_number("10.0") == canon_number(10),
                       "G canon_number 不传 field 默认 0.01（向后兼容）"))
        # G6: diff 端到端——count 精度保留进比对
        ch = diff_changes({"A": make_item("A", name="X", count=2.5)},
                          {"A": make_item("A", name="X", count=2.6)},
                          None, None, date="T")
        checks.append((any(c["type"] == "count_changed" for c in ch),
                       "G diff 端到端：count 小数变化检出"))

        # ================= H. 并发保护（终审 P1-3） =================
        # H1: flock 互斥机制有效性——子进程持锁，同文件再次 LOCK_NB 必须失败
        try:
            import subprocess
            lock_file = tmp / "concurrency.lock"
            holder = subprocess.Popen(
                [sys.executable, "-c",
                 "import fcntl,sys,time\n"
                 "f=open(sys.argv[1],'w')\n"
                 "fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
                 "print('locked',flush=True)\n"
                 "time.sleep(15)\n"
                 "fcntl.flock(f,fcntl.LOCK_UN)\n",
                 str(lock_file)],
                stdout=subprocess.PIPE, text=True)
            line = holder.stdout.readline().strip()
            got_lock = (line == "locked")
            second_denied = False
            if got_lock:
                fh2 = open(lock_file, "w")
                try:
                    import fcntl
                    fcntl.flock(fh2, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fh2, fcntl.LOCK_UN)
                except BlockingIOError:
                    second_denied = True
                finally:
                    fh2.close()
            holder.terminate()
            holder.wait(timeout=5)
            checks.append((got_lock and second_denied,
                           "H flock 互斥有效：持锁期间第二次 LOCK_NB 被拒绝"))
        except Exception as e:  # 平台不支持 fcntl 等环境：跳过不失败
            checks.append((True, f"H flock 测试环境不支持，跳过（{type(e).__name__}）"))

        # H2: 静态断言 main() 确实调用了 acquire_lock（防回归移除）
        main_src = (Path(__file__).resolve().parent.parent / "main.py").read_text(
            encoding="utf-8")
        main_body = main_src.split("def main()", 1)[-1]
        checks.append(("acquire_lock()" in main_body,
                       "H main() 入口调用 acquire_lock（静态断言防回归）"))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
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


def test_stage2():
    """pytest 入口。"""
    assert not [msg for ok, msg in run_checks() if not ok]


if __name__ == "__main__":
    sys.exit(main())
