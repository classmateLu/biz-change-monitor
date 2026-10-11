#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终审 P0-1/P1-2 专项：崩溃恢复与一致性故障注入测试。

核心不变式（提交顺序保证）：
    diff(内存) → 事件落盘 → 快照留证 → latest 更新 → 归档
    ⇒ 任何步骤崩溃后，"latest 已更新但事件缺失"的中间态不存在；
      事件落盘后崩溃 → 重跑以旧基线重 diff，内容幂等 event_id 吸收 → 无漏报无重复。

注入点（按 GPT 要求逐步骤边界；I9-I12 为合并前终审补充）：
    I1 事件落盘后、latest 更新前崩溃 → 重跑恢复
    I2 latest 更新后崩溃 → 事件必然已在（顺序不变式）
    I3 latest.json 损坏/.schema 无效 → 回退扫描 runs/
    I4 JSONL 半行（写一半崩溃）→ 读取容错 + 追加正常
    I5 update_latest 拒绝不完整载荷 → 旧 latest 原样可用
    I6 pending/committed 状态机 → recover_pending_runs 识别
    I7 提交顺序静态断言（main.py 源码，防回归移除）
    I9 跨 run_id 再观测同一变化（同基线对）→ 内容幂等键去重，无重复行
    I10 不同基线对之间的真实变化 → 各自成事件（去重不漏报）
    I11 未提交运行：latest 有效时不越级当基线；回退时 pending-but-complete 快照安全可用
    I12 重启后重复恢复扫描幂等（结果一致、数据不变）
    I13 pending 先落盘静态断言（main.py 运行开始即写 pending，合并前终检 §2-A）
    I14 数值表示漂移（11 vs 11.0）→ 同一变化同 event_id（合并前终检 §1 复核项）
    I15 跨月崩溃恢复：上月遗留 pending 在本月首次运行可被发现（终审三轮 §4）
    I16 事件持久化屏障静态断言：append_events 返回前 fsync（终审三轮 §3）

既可用 pytest 运行，也可直接执行：python tests/test_crash_recovery.py
全部虚构数据，隔离在临时目录。
"""
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.diff import diff_changes  # noqa: E402
from core.events import _load_existing_ids, append_events, load_events  # noqa: E402
from core.events import events_dir  # noqa: E402
from core.models import index_by_id, make_item  # noqa: E402
from core.runlog import append_run_record, recover_pending_runs  # noqa: E402
from core.snapshot import (load_latest_valid, save_run_snapshot,  # noqa: E402
                           update_latest)

OBS_A = "2026-10-10T10:00:00+08:00"
OBS_B = "2026-10-10T10:01:00+08:00"


def _commit_prefix(root, run_id, observed_at, items, base_payload):
    """复刻 main.py 提交流程的**前半段**：diff → 事件落盘（latest 更新之前）。

    与 main.py 保持同一顺序——若 main.py 顺序变更而本测试未同步，
    I7 的静态断言会先报警。
    """
    changes = []
    if base_payload is not None:
        old_items = index_by_id(base_payload["items"])
        changes = diff_changes(old_items, index_by_id(items),
                               base_payload.get("stores"), None,
                               date="2026-10-10", run_id=run_id,
                               observed_at=observed_at,
                               previous_observed_at=base_payload.get("observed_at"))
    if changes:
        append_events(root, changes, today="2026-10-10")
    return changes


def _commit_suffix(root, run_id, observed_at, items, complete=True):
    """复刻 main.py 提交流程的**后半段**：快照留证 → latest 更新。"""
    save_run_snapshot(root, run_id, observed_at, complete,
                      items=items, total=len(items))
    if complete:
        update_latest(root, {
            "schema": "run-snapshot-v1", "run_id": run_id,
            "observed_at": observed_at, "complete": True,
            "items": items,
        })


def run_checks():
    checks = []
    tmp = Path(tempfile.mkdtemp(prefix="bizmon-crash-"))
    try:
        # ---------- 正常 Run A：建立基线（无变化事件） ----------
        items_a = [make_item(f"G-{i:03d}", name=f"对象{i}", price=10 + i)
                   for i in range(5)]
        _commit_prefix(tmp, "run-A", OBS_A, items_a, None)
        _commit_suffix(tmp, "run-A", OBS_A, items_a)

        # ---------- I1: 事件落盘后、latest 更新前崩溃 → 重跑恢复 ----------
        items_b = [make_item("G-000", name="对象0", price=99)] + items_a[1:]
        changes_b = _commit_prefix(tmp, "run-B", OBS_B, items_b,
                                   load_latest_valid(tmp)[1])
        checks.append((len(changes_b) == 1 and changes_b[0]["gid"] == "G-000",
                       "I1 RunB 检出价格变化（崩溃注入点前）"))
        # 【崩溃】此处中断：事件已落盘，latest 仍是 run-A
        rid_now, _ = load_latest_valid(tmp)
        checks.append((rid_now == "run-A",
                       "I1 崩溃后 latest 仍指向 run-A（未污染）"))
        evs = load_events(tmp, "202610")
        checks.append((sum(1 for e in evs if e.get("run_id") == "run-B") == 1,
                       "I1 崩溃时 run-B 事件已持久化（重放种子在盘）"))

        # 【恢复】重跑同一轮（新 run_id run-B2，同一批数据）：以旧基线（run-A）重 diff。
        # 合并前终审 §1.3 修订：event_id 为内容幂等键（不含 run_id）——同一基线对
        # 之间的同一变化，无论哪个 run 观测 → 同一 event_id → 落盘去重（无重复行）。
        items_b2 = [make_item("G-000", name="对象0", price=99)] + items_a[1:]
        rid_prev, payload_prev = load_latest_valid(tmp, exclude_run_id="run-B2")
        checks.append((rid_prev == "run-A",
                       "I1 恢复轮基线仍是 run-A（latest 未被崩溃污染）"))
        changes_b2 = _commit_prefix(tmp, "run-B2", OBS_B, items_b2, payload_prev)
        _commit_suffix(tmp, "run-B2", OBS_B, items_b2)
        evs = load_events(tmp, "202610")
        price_events = [e for e in evs if e.get("field") == "price"]
        checks.append((len(changes_b2) == 1 and len(price_events) == 1,
                       "I9 跨 run_id 再观测同一变化（同基线对）：去重后仅一行（无重复行）"))
        checks.append((changes_b2[0]["event_id"] == price_events[0]["event_id"]
                       and price_events[0].get("run_id") == "run-B",
                       "I9 内容幂等键跨 run 相同；run_id 保留首次发现者（溯源不丢）"))
        rid_final, _ = load_latest_valid(tmp)
        checks.append((rid_final == "run-B2",
                       "I1 恢复轮后 latest 推进到 run-B2"))
        ids = [e["event_id"] for e in evs]
        checks.append((len(ids) == len(set(ids)),
                       "I1 事件文件 event_id 无重复（幂等成立）"))
        # 幂等重放：完全相同的事件再次 append → 全部 skipped
        appended, skipped = append_events(tmp, changes_b2, today="2026-10-10")
        checks.append((appended == 0 and skipped == len(changes_b2),
                       "I1 同轮重试重放：全部去重跳过（无重复落盘）"))

        # ---------- I2: latest 更新后崩溃 → 事件必然已在（顺序不变式） ----------
        items_c = items_b2 + [make_item("G-099", name="新对象", price=999)]
        rid_prev, payload_prev = load_latest_valid(tmp, exclude_run_id="run-C")
        changes_c = _commit_prefix(tmp, "run-C", OBS_B, items_c, payload_prev)
        _commit_suffix(tmp, "run-C", OBS_B, items_c)
        rid_final, latest_payload = load_latest_valid(tmp)
        ev_ids_on_disk = {e["event_id"] for e in load_events(tmp, "202610")}
        c_event_ids = {e["event_id"] for e in changes_c}
        checks.append((rid_final == "run-C" and c_event_ids <= ev_ids_on_disk,
                       "I2 不变式：latest 指向 run-C 时其事件必已全部落盘"))

        # ---------- I10: 不同基线对之间的真实变化 → 各自成事件（去重不漏报） ----------
        # 崩溃形态：G-001 价格 11→12 事件已落盘，快照/latest 未写（prefix-only）
        items_c3 = items_c[:1] + [make_item("G-001", name="对象1", price=12)] + items_c[2:]
        _, payload_c3 = load_latest_valid(tmp, exclude_run_id="run-C3")
        changes_c3 = _commit_prefix(tmp, "run-C3", OBS_B, items_c3, payload_c3)
        g1_events = [e for e in load_events(tmp, "202610")
                     if e.get("gid") == "G-001" and e.get("field") == "price"]
        checks.append((len(changes_c3) == 1 and len(g1_events) == 1
                       and g1_events[0]["previous_observed_at"] == OBS_B,
                       "I10a 不同基线对的真实变化落盘为新事件（previous_observed_at=新基线）"))
        # 恢复轮（新 run_id run-C4）对同一基线对再检出同一变化 → 内容键去重，不新增行
        _, payload_c4 = load_latest_valid(tmp, exclude_run_id="run-C4")
        _commit_prefix(tmp, "run-C4", OBS_B, items_c3, payload_c4)
        g1_events = [e for e in load_events(tmp, "202610")
                     if e.get("gid") == "G-001" and e.get("field") == "price"]
        checks.append((len(g1_events) == 1,
                       "I10b 跨 run_id 再观测（同基线对）：内容幂等键去重，仍一行"))

        # ---------- I14: 数值表示漂移 → 同一变化同 event_id（终检 §1 复核项） ----------
        # 同一基线对，"10→11"（int）与"10→11.0"（float）必须同键——源数据数值
        # 表示在两次运行间漂移时，不得产生重复事件行（比对层 Decimal 按值相等
        # 已保证无误报，本项保证哈希层同样表示无关）。
        _kw = dict(date="2026-10-10", run_id="run-N", observed_at=OBS_B,
                   previous_observed_at=OBS_A)
        e_int = diff_changes(index_by_id([make_item("G-500", name="对象N", price=10)]),
                             index_by_id([make_item("G-500", name="对象N", price=11)]),
                             None, None, **_kw)
        e_flt = diff_changes(index_by_id([make_item("G-500", name="对象N", price=10)]),
                             index_by_id([make_item("G-500", name="对象N", price=11.0)]),
                             None, None, **_kw)
        checks.append((len(e_int) == 1 and len(e_flt) == 1
                       and e_int[0]["event_id"] == e_flt[0]["event_id"],
                       "I14 新值表示漂移（11 vs 11.0）：同一变化同 event_id"))
        e_old_int = diff_changes(index_by_id([make_item("G-500", name="对象N", price=10)]),
                                 index_by_id([make_item("G-500", name="对象N", price=12)]),
                                 None, None, **_kw)
        e_old_flt = diff_changes(index_by_id([make_item("G-500", name="对象N", price=10.0)]),
                                 index_by_id([make_item("G-500", name="对象N", price=12)]),
                                 None, None, **_kw)
        checks.append((e_old_int[0]["event_id"] == e_old_flt[0]["event_id"],
                       "I14 旧值表示漂移（10 vs 10.0）：同 event_id"))
        e_zero = diff_changes(index_by_id([make_item("G-500", name="对象N", price=0)]),
                              index_by_id([make_item("G-500", name="对象N", price=0.0)]),
                              None, None, **_kw)
        checks.append((len(e_zero) == 0,
                       "I14 零值表示漂移（0 vs 0.0）：无变化即零事件（比对层语义不变）"))

        # ---------- I3: latest 损坏 / schema 无效 → 回退扫描 ----------
        latest_path = tmp / "data" / "snapshots" / "latest.json"
        backup = latest_path.read_text(encoding="utf-8")
        latest_path.write_text('{"schema": "alien-v9", "complete": true}', encoding="utf-8")
        rid, payload = load_latest_valid(tmp)
        checks.append((rid == "run-C",
                       "I3 schema 不符 → 按无效快照回退扫描 runs/ 成功"))
        latest_path.write_text("{broken json!!", encoding="utf-8")
        rid, payload = load_latest_valid(tmp)
        checks.append((rid == "run-C",
                       "I3 latest 损坏 → 回退扫描成功（基线不丢）"))
        latest_path.write_text(backup, encoding="utf-8")

        # ---------- I4: JSONL 半行（写一半崩溃）→ 容错读取 + 追加正常 ----------
        ev_file = events_dir(tmp) / "events-202610.jsonl"
        with ev_file.open("a", encoding="utf-8") as fh:
            fh.write('{"event_id": "half", "run_id": "run-')  # 半行
        evs = load_events(tmp, "202610")
        checks.append((all(isinstance(e, dict) and e.get("event_id") != "half"
                           for e in evs),
                       "I4 半行被容错跳过，历史事件全部可读"))
        ok_append, _ = append_events(
            tmp, [{"event_id": "post-crash", "run_id": "run-C", "gid": "X",
                   "field": "price", "event_type": "price_changed",
                   "observed_at": OBS_B}], today="2026-10-10")
        checks.append((ok_append == 1, "I4 半行之后追加新事件正常"))
        ids2 = _load_existing_ids(ev_file)
        checks.append(("post-crash" in ids2 and "half" not in ids2,
                       "I4 幂等索引不受半行影响"))

        # ---------- I5: update_latest 拒绝不完整载荷 → 旧 latest 原样可用 ----------
        before = latest_path.read_text(encoding="utf-8")
        try:
            update_latest(tmp, {"schema": "run-snapshot-v1", "complete": False,
                                "items": []})
            checks.append((False, "I5 不完整载荷应被拒绝"))
        except ValueError:
            checks.append((before == latest_path.read_text(encoding="utf-8"),
                           "I5 拒绝后旧 latest 原样可用（未被半写污染）"))

        # ---------- I6: pending/committed 状态机 → 恢复识别 ----------
        append_run_record(tmp, {"run_id": "run-D", "status": "pending",
                                "started_at": "2026-10-10T10:02:00+08:00"})
        append_run_record(tmp, {"run_id": "run-D", "status": "committed",
                                "started_at": "2026-10-10T10:02:00+08:00",
                                "finished_at": "2026-10-10T10:03:00+08:00"})
        append_run_record(tmp, {"run_id": "run-E", "status": "pending",
                                "started_at": "2026-10-10T10:04:00+08:00"})
        append_run_record(tmp, {"run_id": "run-F", "status": "failed",
                                "started_at": "2026-10-10T10:05:00+08:00"})
        pending = recover_pending_runs(tmp, now=datetime(2026, 10, 10, 10, 6).astimezone())
        checks.append((pending == ["run-E"],
                       "I6 pending 识别正确：run-E 中断待查（D已committed/F已failed不误报）"))
        # 半行 runlog 也不影响恢复扫描
        rl = tmp / "data" / "runlog" / "runlog-202610.jsonl"
        with rl.open("a", encoding="utf-8") as fh:
            fh.write('{"run_id": "run-G", "sta')  # 半行
        pending = recover_pending_runs(tmp, now=datetime(2026, 10, 10, 10, 7).astimezone())
        checks.append((pending == ["run-E"],
                       "I6 runlog 半行容错：恢复扫描不受影响"))

        # ---------- I11: 未提交运行不被误当已提交（快照已存、latest 未更新的崩溃形态） ----------
        # run-E 状态机为 pending；其完整快照已留证（崩溃发生在 latest 更新前）
        save_run_snapshot(tmp, "run-E", "2026-10-10T10:04:30+08:00", True,
                          items=items_c, total=len(items_c))
        rid_now, _ = load_latest_valid(tmp)
        checks.append((rid_now == "run-C",
                       "I11 latest 有效时基线仍取已提交轮（pending 轮不越级充当基线）"))
        # 回退场景：latest 失效时，pending-but-complete 快照可被选为基线——
        # 安全性由提交顺序保证（事件先于快照落盘 ⇒ 该快照对应事件必已在盘），
        # 且快照数据本身已通过第一道完整性闸门（complete=true 由闸门后写入）
        latest_path2 = tmp / "data" / "snapshots" / "latest.json"
        backup2 = latest_path2.read_text(encoding="utf-8")
        latest_path2.write_text("{broken", encoding="utf-8")
        rid_fb, _ = load_latest_valid(tmp)
        latest_path2.write_text(backup2, encoding="utf-8")
        checks.append((rid_fb == "run-E",
                       "I11b 回退扫描按 (observed_at, run_id) 确定性选取（pending-but-complete 快照安全可用）"))

        # ---------- I12: 重启后重复恢复扫描幂等 ----------
        evs_before = load_events(tmp, "202610")
        p1 = recover_pending_runs(tmp, now=datetime(2026, 10, 10, 10, 8).astimezone())
        p2 = recover_pending_runs(tmp, now=datetime(2026, 10, 10, 10, 9).astimezone())
        evs_after = load_events(tmp, "202610")
        checks.append((p1 == p2 == ["run-E"] and evs_before == evs_after,
                       "I12 重复恢复扫描幂等：结果一致、事件数据不变"))

        # ---------- I7: 提交顺序静态断言（防回归移除顺序） ----------
        main_src = (Path(__file__).resolve().parent.parent / "main.py").read_text(
            encoding="utf-8")
        ev_pos = main_src.find("append_events(ROOT, changes")
        lat_pos = main_src.find("update_latest(ROOT, {")
        checks.append((0 < ev_pos < lat_pos,
                       "I7 main.py 顺序不变式：append_events 必须先于 update_latest（静态断言）"))

        # ---------- I13: pending 先落盘（合并前终检 §2 处置 A） ----------
        # main.py 必须在采集开始前就落盘 pending 记录——否则进程中途被杀时
        # runlog 无该轮痕迹，recover_pending_runs 永远扫不到真实中断轮。
        pend_pos = main_src.find('"status": "pending"')
        fetch_pos = main_src.find("result = adapter.fetch()")
        checks.append((0 < pend_pos < fetch_pos,
                       "I13 main.py 运行开始即落盘 pending（中断轮可被恢复扫描识别，静态断言）"))

        # ---------- I15: 跨月崩溃恢复（终审三轮 §4） ----------
        # 月末运行留下 pending，月初第一次运行（now=下月）必须能发现——
        # 旧实现只读当前月日志，上月遗留永远扫不到。
        prev_path = append_run_record(tmp, {
            "run_id": "run-XMONTH", "status": "pending",
            "started_at": "2026-10-31T23:58:00+08:00",
        })
        checks.append(("202610" in prev_path.name,
                       "I15 前置：10月末 pending 记录落在 10月日志文件"))
        pend_nov = recover_pending_runs(
            tmp, now=datetime(2026, 11, 1, 0, 5).astimezone())
        checks.append(("run-XMONTH" in pend_nov,
                       "I15 11月首次运行能发现 10月遗留 pending（跨月恢复）"))
        # 遗留 pending 在新月份收尾 → 不再误报（新月记录覆盖旧月）
        append_run_record(tmp, {
            "run_id": "run-XMONTH", "status": "committed",
            "started_at": "2026-10-31T23:58:00+08:00",
            "finished_at": "2026-11-01T00:04:00+08:00",
        })
        pend_nov2 = recover_pending_runs(
            tmp, now=datetime(2026, 11, 1, 0, 6).astimezone())
        checks.append(("run-XMONTH" not in pend_nov2,
                       "I15 跨月收尾后不再误报（新月覆盖旧月，末条为准）"))

        # ---------- I16: 事件持久化屏障静态断言（终审三轮 §3） ----------
        # append_events 返回前必须 fsync——「基线已推进 ⇒ 事件已在介质」
        # 的前提；缺 fsync 则断电可丢事件尾部而新基线仍在 → 结构性漏报。
        ev_src = (Path(__file__).resolve().parent.parent
                  / "core" / "events.py").read_text(encoding="utf-8")
        imp_pos = ev_src.find("import os")
        fs_pos = ev_src.find("os.fsync(fh.fileno())")
        ret_pos = ev_src.find("return appended, skipped")
        checks.append((0 < imp_pos < fs_pos < ret_pos,
                       "I16 events.py 持久化屏障：append_events 返回前 fsync（静态断言）"))

        # ---------- I8: 历史不清空——多轮 JSONL 追加 + 重启读取 ----------
        evs = load_events(tmp, "202610")
        checks.append((len(evs) >= 3,
                       "I8 全部崩溃注入后历史事件完整保留（不清空不覆盖）"))
    finally:
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


def test_crash_recovery():
    """pytest 入口。"""
    assert not [msg for ok, msg in run_checks() if not ok]


if __name__ == "__main__":
    sys.exit(main())
