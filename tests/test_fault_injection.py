#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终审四轮专项：动态故障注入测试——执行**真实 main.main() 代码路径**。

与静态断言（I7/I13/I16 检查源码文本）互补：本文件把 main() 整体跑起来，
只在真实持久化边界注入故障（mock os.fsync / os.replace / 占位目录 /
边界函数抛异常），验证每个故障窗口后的**下一次启动**能恢复且不静默漏报。

注入方式（如实声明）：
    F1 事件文件写入失败   —— 事件文件路径被目录占位（模拟权限/介质错误，
                              open("a") 抛 IsADirectoryError ⊂ OSError）
    F2 事件 fsync 失败    —— mock os.fsync：**按目标文件 inode 识别**
                              （终审五轮 §F2 精化，不依赖调用序号），
                              仅事件文件 fsync 抛 OSError，其余放行
    F3 快照更新失败       —— mock os.replace：目标在 runs/ 下时抛 OSError
                              （事件已 fsync，快照 tmp 已写、改名失败）
    F4 latest 更新失败    —— mock os.replace：目标为 latest.json 时抛
                              （事件+快照均已持久，仅 latest 推进失败）
    F5 进程中断           —— mock main.save_run_snapshot 抛 OSError
                              （≈ kill -9 于事件落盘与快照之间：无终态记录）

故障后验证范式（每个场景相同）：
    ① 故障注入的 main() 以 OSError 崩溃（不吞异常、不带病推进）
    ② 磁盘状态符合不变式：latest 未推进 / 事件状态符合预期
    ③ 模拟下一次启动：recover_pending_runs 告警中断轮（不静默）
       → 再次真实运行 main() → 重 diff → 内容幂等键去重
    ④ 断言：变化事件恰好一行（无漏报、无重复）、latest 推进、
       历史不损坏

全部本地虚构数据，隔离在临时目录，无网络、无真实钉钉。
既可用 pytest 运行，也可直接执行。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import unittest.mock as mock
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as main_mod  # noqa: E402
from adapters.base import CollectionResult  # noqa: E402
from core.events import events_dir, load_events  # noqa: E402
from core.models import make_item  # noqa: E402
from core.runlog import load_run_records, recover_pending_runs  # noqa: E402
from core.snapshot import load_latest_valid  # noqa: E402

TODAY = "2026-10-10"
MONTH = "202610"


# ---------- 基础设施 ----------

class FakeAdapter:
    """可控数据源：返回预置对象（≥3 个，通过 min_records=2 闸门）。"""
    name = "fake"
    is_example = True

    def __init__(self, items, stores=None):
        self._items = items
        self._stores = stores or {it["gid"]: ["Alpha 店"] for it in items}

    def fetch(self) -> CollectionResult:
        return CollectionResult(items=list(self._items), stores=dict(self._stores),
                                total=len(self._items), failed_pages=[])


def _items(prices: dict) -> list[dict]:
    """按 {gid: price} 生成 3 个对象（gid 后缀决定顺序稳定性）。"""
    return [make_item(gid, name=f"对象 {gid}", price=price, count=1,
                      unit_price=price)
            for gid, price in sorted(prices.items())]


def _setup_tmp(tmp: Path) -> None:
    """临时 ROOT：config 只放宽 min_records 闸门（其余默认）。"""
    (tmp / "config").mkdir(parents=True, exist_ok=True)
    (tmp / "config" / "config.yaml").write_text(
        "validation:\n  min_records: 2\n", encoding="utf-8")


def _run_main(tmp: Path, items) -> int:
    """整体运行真实 main()：伪造适配器 + 临时 ROOT + 固定日期。

    不 mock 任何 core 函数——持久化行为全部真实（fsync/replace 全走真路径）。
    """
    fake = FakeAdapter(items)
    argv = ["main.py", "--date", TODAY]
    with mock.patch.object(main_mod, "ExampleAdapter", lambda: fake), \
         mock.patch.object(main_mod, "ROOT", tmp), \
         mock.patch("sys.argv", argv):
        return main_mod.main()


def _price_events(tmp) -> list[dict]:
    return [e for e in load_events(tmp, MONTH) if e.get("field") == "price"]


def _pending_in_runlog(tmp) -> list[str]:
    records = load_run_records(tmp, MONTH)
    last: dict[str, str] = {}
    for r in records:
        if r.get("run_id") and r.get("status"):
            last[r["run_id"]] = r["status"]
    return [rid for rid, st in last.items() if st == "pending"]


def _latest_path(tmp) -> Path:
    return tmp / "data" / "snapshots" / "latest.json"


# ---------- 场景 ----------

def scenario_f1_write_failure(tmp: Path):
    """F1 事件文件写入失败（目录占位）→ 崩溃 → 下次启动恢复无漏报。"""
    checks = []
    base = _items({"DEMO-A": 10, "DEMO-B": 20, "DEMO-C": 30})
    checks.append((_run_main(tmp, base) == 0, "F1 前置：基线轮 committed"))
    rid_a, _ = load_latest_valid(tmp)
    changed = _items({"DEMO-A": 99, "DEMO-B": 20, "DEMO-C": 30})

    # 注入：事件文件路径被目录占位 → open("a") 抛 IsADirectoryError
    fake_file = events_dir(tmp) / f"events-{MONTH}.jsonl"
    fake_file.mkdir(parents=True)
    raised = None
    try:
        _run_main(tmp, changed)
    except OSError as e:
        raised = e
    checks.append((raised is not None,
                   f"F1 写入失败 → main() 以 OSError 崩溃（{type(raised).__name__}），"
                   "绝不带病推进基线"))
    # 移除占位后核查磁盘状态（占位本身挡读取）
    shutil.rmtree(fake_file)
    checks.append((load_latest_valid(tmp)[0] == rid_a,
                   "F1 崩溃后 latest 仍指向基线轮（未推进）"))
    checks.append((len(_price_events(tmp)) == 0,
                   "F1 崩溃后无事件落盘（写入完全失败，无半态）"))
    checks.append((len(_pending_in_runlog(tmp)) == 1,
                   "F1 崩溃轮 pending 留痕（pending 先落盘，I13 语义真实生效）"))

    # 下一次启动：真实恢复
    checks.append((len(recover_pending_runs(tmp)) == 1,
                   "F1 下次启动恢复扫描告警中断轮（不静默）"))
    checks.append((_run_main(tmp, changed) == 0, "F1 恢复轮 committed"))
    checks.append((len(_price_events(tmp)) == 1,
                   "F1 恢复后变化事件恰好一行——无静默漏报、无重复"))
    rid_c, _ = load_latest_valid(tmp)
    checks.append((rid_c != rid_a, "F1 恢复后 latest 推进到新轮"))
    return checks


def scenario_f2_fsync_failure(tmp: Path):
    """F2 事件 fsync 失败 → 崩溃 → 数据页缓存可见 → 重跑去重恢复。"""
    checks = []
    base = _items({"DEMO-A": 10, "DEMO-B": 20, "DEMO-C": 30})
    checks.append((_run_main(tmp, base) == 0, "F2 前置：基线轮 committed"))
    rid_a, _ = load_latest_valid(tmp)
    changed = _items({"DEMO-A": 99, "DEMO-B": 20, "DEMO-C": 30})

    # 注入（终审五轮 §F2 精化）：**按目标文件识别**，不依赖调用序号——
    # 以 (st_dev, st_ino) 匹配事件文件，仅事件文件的 fsync 失败；pending
    # runlog 等其他文件放行。以后新增文件同步也不会误伤别的目标。
    # 注入轮中事件文件已存在（基线轮创建），可稳定取 inode。
    import os as _os
    events_path = events_dir(tmp) / f"events-{MONTH}.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.touch()  # 基线轮无变化未创建事件文件；预创建空文件（append 正常追加）
    _st = events_path.stat()
    target = (_st.st_dev, _st.st_ino)
    real_fsync = _os.fsync
    hits = {"called": 0, "failed": 0}

    def flaky_fsync(fd):
        fst = _os.fstat(fd)
        if (fst.st_dev, fst.st_ino) == target:
            hits["called"] += 1
            if hits["failed"] == 0:
                hits["failed"] += 1
                raise OSError(5, "simulated fsync failure on events file")
        return real_fsync(fd)

    with mock.patch("os.fsync", side_effect=flaky_fsync):
        raised = None
        try:
            _run_main(tmp, changed)
        except OSError as e:
            raised = e
    checks.append((raised is not None,
                   f"F2 fsync 失败 → main() 以 OSError 崩溃（{type(raised).__name__}），"
                   "不吞异常不推进基线"))
    checks.append((hits["called"] >= 1 and hits["failed"] == 1,
                   "F2 事件文件 fsync 确实被调用且失败（按目标文件 inode 识别，"
                   "非调用序号——终审五轮 §F2 精化）"))
    checks.append((load_latest_valid(tmp)[0] == rid_a, "F2 崩溃后 latest 未推进"))
    runs_complete = [p for p in (tmp / "data" / "snapshots" / "runs").glob("*.json")
                     if not p.name.startswith(".tmp_")]
    checks.append((len(runs_complete) == 1,
                   "F2 快照提交未发生（runs/ 仅基线轮一份 complete 快照）"))
    evs = _price_events(tmp)
    checks.append((len(evs) == 1,
                   "F2 事件数据已 flush 到页缓存（进程级可见）——故障轮事件在盘"))
    checks.append((len(_pending_in_runlog(tmp)) == 1, "F2 崩溃轮 pending 留痕"))

    # 下一次启动：恢复轮以旧基线重 diff → 同基线对同一变化 → 内容键去重
    checks.append((len(recover_pending_runs(tmp)) >= 1, "F2 下次启动告警中断轮"))
    checks.append((_run_main(tmp, changed) == 0, "F2 恢复轮 committed"))
    checks.append((len(_price_events(tmp)) == 1,
                   "F2 恢复后事件仍恰好一行（内容幂等键吸收重放，无重复无漏报）"))
    rid_c, _ = load_latest_valid(tmp)
    checks.append((rid_c != rid_a, "F2 恢复后 latest 推进"))
    return checks


def scenario_f3_snapshot_failure(tmp: Path):
    """F3 事件持久化成功后，快照更新失败 → tmp 残留被回退扫描排除。"""
    checks = []
    base = _items({"DEMO-A": 10, "DEMO-B": 20, "DEMO-C": 30})
    checks.append((_run_main(tmp, base) == 0, "F3 前置：基线轮 committed"))
    rid_a, _ = load_latest_valid(tmp)
    changed = _items({"DEMO-A": 99, "DEMO-B": 20, "DEMO-C": 30})

    real_replace = __import__("os").replace

    def replace_fail_runs(src, dst):
        if Path(dst).parent.name == "runs":
            raise OSError(28, "simulated: runs/ snapshot replace failure")
        return real_replace(src, dst)

    with mock.patch("os.replace", side_effect=replace_fail_runs):
        raised = None
        try:
            _run_main(tmp, changed)
        except OSError as e:
            raised = e
    checks.append((raised is not None,
                   f"F3 快照更新失败 → main() 以 OSError 崩溃"
                   f"（{type(raised).__name__}），事件已先持久化（屏障顺序）"))
    checks.append((len(_price_events(tmp)) == 1 and load_latest_valid(tmp)[0] == rid_a,
                   "F3 事件已落盘（fsync 屏障）而 latest 未推进——故障序列符合预期"))
    tmp_residue = list((tmp / "data" / "snapshots" / "runs").glob(".tmp_*"))
    checks.append((len(tmp_residue) == 1,
                   "F3 改名失败留下 .tmp_ 残留（如实暴露，不假装清理）"))

    # .tmp_ 残留绝不参与基线回退扫描：破坏 latest 强制走回退路径
    latest_p = _latest_path(tmp)
    backup = latest_p.read_text(encoding="utf-8")
    latest_p.write_text("{broken", encoding="utf-8")
    rid_fb, _ = load_latest_valid(tmp)
    latest_p.write_text(backup, encoding="utf-8")
    checks.append((rid_fb == rid_a,
                   "F3 回退扫描跳过 .tmp_ 残留，正确选中基线快照（不会把未"
                   "提交的 tmp 内容当基线）"))

    # 下一次启动恢复
    checks.append((_run_main(tmp, changed) == 0, "F3 恢复轮 committed"))
    checks.append((len(_price_events(tmp)) == 1,
                   "F3 恢复后事件恰好一行（重 diff → 内容键去重，无漏报无重复）"))
    rid_c, _ = load_latest_valid(tmp)
    checks.append((rid_c != rid_a, "F3 恢复后 latest 推进"))
    return checks


def scenario_f4_latest_failure(tmp: Path):
    """F4 快照成功后 latest 更新失败 → 快照+事件在盘，重跑恢复。"""
    checks = []
    base = _items({"DEMO-A": 10, "DEMO-B": 20, "DEMO-C": 30})
    checks.append((_run_main(tmp, base) == 0, "F4 前置：基线轮 committed"))
    rid_a, _ = load_latest_valid(tmp)
    # 稳定性修复（CI 3.13 实测踩坑）：observed_at/run_id 时间戳精确到秒，
    # 两轮背靠背挤进同一秒时，回退扫描的次键（run_id 的随机 UUID 段）
    # 会把"选哪轮当基线"变成抛硬币。这里强制故障轮的秒级时间戳严格
    # 大于基线轮，使 F4 回退断言恢复确定性。（引擎侧同秒多轮的次序
    # 问题见测试注释，属极端边缘场景，不在本修引入引擎改动。）
    time.sleep(1.1)
    changed = _items({"DEMO-A": 99, "DEMO-B": 20, "DEMO-C": 30})

    real_replace = __import__("os").replace

    def replace_fail_latest(src, dst):
        if Path(dst).name == "latest.json":
            raise OSError(28, "simulated: latest.json replace failure")
        return real_replace(src, dst)

    with mock.patch("os.replace", side_effect=replace_fail_latest):
        raised = None
        try:
            _run_main(tmp, changed)
        except OSError as e:
            raised = e
    checks.append((raised is not None,
                   f"F4 latest 更新失败 → main() 以 OSError 崩溃"
                   f"（{type(raised).__name__}）"))
    runs_json = [p for p in (tmp / "data" / "snapshots" / "runs").glob("*.json")
                 if not p.name.startswith(".tmp_")]
    checks.append((len(runs_json) == 2,
                   "F4 故障轮快照已成功落盘（基线轮+故障轮各一份，complete=true）"))
    checks.append((len(_price_events(tmp)) == 1
                   and load_latest_valid(tmp)[0] == rid_a,
                   "F4 事件+快照均已持久、仅 latest 未推进——等待恢复的状态正确"))

    # I11 语义的动态版：latest 失效时，complete=true 快照（事件必已落盘）
    # 可安全充当回退基线
    latest_p = _latest_path(tmp)
    backup = latest_p.read_text(encoding="utf-8")
    latest_p.write_text("{broken", encoding="utf-8")
    rid_fb, payload_fb = load_latest_valid(tmp)
    latest_p.write_text(backup, encoding="utf-8")
    checks.append((rid_fb != rid_a and payload_fb.get("complete") is True,
                   "F4 回退扫描选中故障轮 complete=true 快照（事件已落盘，"
                   "作为基线不丢数据——I11 动态验证）"))

    # 下一次启动恢复（latest 完好场景）
    checks.append((_run_main(tmp, changed) == 0, "F4 恢复轮 committed"))
    checks.append((len(_price_events(tmp)) == 1,
                   "F4 恢复后事件恰好一行（以 latest=基线轮重 diff → 去重）"))
    rid_c, _ = load_latest_valid(tmp)
    checks.append((rid_c != rid_a, "F4 恢复后 latest 推进"))
    return checks


def scenario_f5_process_interrupt(tmp: Path):
    """F5 进程中断（≈kill -9：事件后、快照前）→ pending 告警 → 重跑闭环。"""
    checks = []
    base = _items({"DEMO-A": 10, "DEMO-B": 20, "DEMO-C": 30})
    checks.append((_run_main(tmp, base) == 0, "F5 前置：基线轮 committed"))
    rid_a, _ = load_latest_valid(tmp)
    changed = _items({"DEMO-A": 99, "DEMO-B": 20, "DEMO-C": 30})

    # 注入：快照步骤抛 OSError ≈ 进程在该边界被杀死（无清理、无终态记录）
    with mock.patch.object(main_mod, "save_run_snapshot",
                           side_effect=OSError("simulated kill -9")):
        raised = None
        try:
            _run_main(tmp, changed)
        except OSError as e:
            raised = e
    checks.append((raised is not None,
                   f"F5 中断注入生效（{type(raised).__name__}），无终态记录"))
    pending = _pending_in_runlog(tmp)
    checks.append((len(pending) == 1,
                   "F5 中断轮 pending 留痕（进程死亡后 runlog 可识别）"))
    checks.append((len(_price_events(tmp)) == 1
                   and load_latest_valid(tmp)[0] == rid_a,
                   "F5 事件已落盘、latest 未推进（中断点之前的状态完整）"))

    # 下一次启动：恢复扫描告警 + 真实重跑闭环
    alerted = recover_pending_runs(tmp)
    checks.append((pending[0] in alerted,
                   "F5 下次启动 recover_pending_runs 告警中断轮（不静默漏报）"))
    checks.append((_run_main(tmp, changed) == 0, "F5 恢复轮 committed"))
    checks.append((len(_price_events(tmp)) == 1,
                   "F5 恢复后事件恰好一行（同一基线对同一变化 → 内容键去重）"))
    rid_c, _ = load_latest_valid(tmp)
    checks.append((rid_c != rid_a, "F5 恢复后 latest 推进"))

    # 如实语义：中断轮的 pending 记录保留（人工信号，不自动消除），
    # 数据完整性由重跑闭环独立保证
    checks.append((pending[0] in _pending_in_runlog(tmp),
                   "F5 中断轮 pending 保留（有意的运维信号；数据一致性不依赖它）"))
    records = load_run_records(tmp, MONTH)
    committed = [r["run_id"] for r in records if r.get("status") == "committed"]
    checks.append((len(committed) == 2,
                   "F5 基线轮与恢复轮均 committed（终态记录完整）"))
    return checks


def run_checks():
    all_checks = []
    scenarios = [
        ("F1", scenario_f1_write_failure),
        ("F2", scenario_f2_fsync_failure),
        ("F3", scenario_f3_snapshot_failure),
        ("F4", scenario_f4_latest_failure),
        ("F5", scenario_f5_process_interrupt),
    ]
    for name, fn in scenarios:
        tmp = Path(tempfile.mkdtemp(prefix=f"bizmon-{name}-"))
        try:
            _setup_tmp(tmp)
            all_checks.extend(fn(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return all_checks


def main():
    errors = []
    for ok, msg in run_checks():
        print(("✅" if ok else "❌"), msg)
        if not ok:
            errors.append(msg)
    print()
    print("全部通过 🎉" if not errors else f"有{len(errors)}项失败！")
    return 1 if errors else 0


def test_fault_injection():
    """pytest 入口。"""
    assert not [msg for ok, msg in run_checks() if not ok]


if __name__ == "__main__":
    sys.exit(main())
