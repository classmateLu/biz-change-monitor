#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终审 P0-1/P1-2 专项：崩溃恢复与一致性故障注入测试。

核心不变式（提交顺序保证）：
    diff(内存) → 事件落盘 → 快照留证 → latest 更新 → 归档
    ⇒ 任何步骤崩溃后，"latest 已更新但事件缺失"的中间态不存在；
      事件落盘后崩溃 → 重跑以旧基线重 diff，event_id 幂等吸收 → 无漏报无重复。

注入点（按 GPT 要求逐步骤边界）：
    I1 事件落盘后、latest 更新前崩溃 → 重跑恢复
    I2 latest 更新后崩溃 → 事件必然已在（顺序不变式）
    I3 latest.json 损坏/.schema 无效 → 回退扫描 runs/
    I4 JSONL 半行（写一半崩溃）→ 读取容错 + 追加正常
    I5 update_latest 拒绝不完整载荷 → 旧 latest 原样可用
    I6 pending/committed 状态机 → recover_pending_runs 识别
    I7 提交顺序静态断言（main.py 源码，防回归移除）

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

        # 【恢复】重跑同一轮（新 run_id，同一批数据）：以旧基线重 diff，
        # 同样变化再次发现 → event_id 不同（run_id 参与哈希）但旧事件仍保留；
        # 关键断言：无任何事件丢失，latest 最终推进到新轮
        items_b2 = [make_item("G-000", name="对象0", price=99)] + items_a[1:]
        rid_prev, payload_prev = load_latest_valid(tmp, exclude_run_id="run-B2")
        changes_b2 = _commit_prefix(tmp, "run-B2", OBS_B, items_b2, payload_prev)
        _commit_suffix(tmp, "run-B2", OBS_B, items_b2)
        evs = load_events(tmp, "202610")
        price_events = [e for e in evs if e.get("field") == "price"]
        checks.append((len(changes_b2) == 1 and len(price_events) == 2,
                       "I1 恢复轮重新检出同一变化（旧事件不丢，不静默合并）"))
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

        # ---------- I7: 提交顺序静态断言（防回归移除顺序） ----------
        main_src = (Path(__file__).resolve().parent.parent / "main.py").read_text(
            encoding="utf-8")
        ev_pos = main_src.find("append_events(ROOT, changes")
        lat_pos = main_src.find("update_latest(ROOT, {")
        checks.append((0 < ev_pos < lat_pos,
                       "I7 main.py 顺序不变式：append_events 必须先于 update_latest（静态断言）"))

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
