# -*- coding: utf-8 -*-
"""运行记录（v2）：每轮采集一条，JSONL 按月追加，失败轮也留痕。

目录规范：data/runlog/runlog-YYYYMM.jsonl

设计要点：
- 一轮扫描即使失败（校验未通过/异常退出前），也应尽可能留下运行记录
- 运行记录与快照分离：不会被最新快照覆盖
- 记录字段语义（字段名可调，语义必须保留）：
    run_id            本轮唯一标识
    started_at        开始时间（ISO 含时区）
    finished_at       结束时间
    complete          本轮是否完整（只有校验通过才是 true，绝不看 HTTP 状态码）
    expected_count    源数据可信总量（不可得为 None，不虚构）
    collected_count   实际采集对象数
    unique_count      去重后对象数
    failed_pages      失败页列表
    failed_items      失败对象列表
    detail_failed     详情查询失败数
    validation_errors 完整性校验失败原因
- 运行记录不自动删除（保留期限待业务定，不擅自清理）

持久性保证的如实区分（终审二轮 §2-5，终审三轮 §3 修订）：
- **进程异常退出**（SIGKILL/异常，OS 存活）：write + close 后数据已在
  内核页缓存，进程死亡不影响——后续读取可见。
- **操作系统崩溃/断电**：append_run_record 与 append_events 均在函数
  返回前 fsync（新建文件额外 fsync 目录条目）——两份日志在返回时已
  落介质，断电不丢。**持久化顺序**：事件文件 fsync 完成后才可能执行
  快照/latest 更新，故 GPT 指出的「事件尾部丢失但新基线已持久化 →
  下一轮以新基线比对 → 漏报」故障序列结构上不可能发生。
- latest/快照为 tmp+os.replace 原子写（进程级）：断电取旧值或新值、
  绝无半态；任一结果均安全（用旧基线重 diff，内容幂等键吸收），
  不依赖介质级持久。
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def runlog_dir(root) -> Path:
    return Path(root) / "data" / "runlog"


def append_run_record(root, record: dict) -> Path:
    """追加一条运行记录。月份取 started_at（缺省退到 finished_at/now）。"""
    month = ""
    for key in ("started_at", "finished_at"):
        val = record.get(key) or ""
        if val[:7]:
            month = val[:7].replace("-", "")
            break
    if not month:
        from datetime import datetime
        month = datetime.now().astimezone().strftime("%Y%m")

    path = runlog_dir(root) / f"runlog-{month}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    # 崩溃残留半行隔离（同 core/events.py 的 I4 修复）：防新记录拼接到半行上
    if not created and path.stat().st_size > 0:
        with path.open("rb") as fh:
            fh.seek(-1, 2)
            if fh.read(1) != b"\n":
                with path.open("a", encoding="utf-8") as fh2:
                    fh2.write("\n")
                print(f"⚠️ 检测到未写完的半行（疑似崩溃残留），已补换行隔离: {path.name}",
                      flush=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())  # 断电/内核崩溃下也持久（进程崩溃本就由 close 保证）
    if created:
        _fsync_dir(path.parent)  # 新建文件：目录条目也要持久（断电下文件名不丢）
    return path


def _fsync_dir(path: Path) -> None:
    """目录 fsync（同 core/events.py）：新建文件后同步目录条目。"""
    try:
        dfd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def load_run_records(root, month: str) -> list[dict]:
    """读取某月（YYYYMM）全部运行记录（损坏行跳过）。"""
    path = runlog_dir(root) / f"runlog-{month}.jsonl"
    out: list[dict] = []
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def recover_pending_runs(root, now=None) -> list[str]:
    """识别未正常收尾的运行（P0-1 终审补充：崩溃恢复）。

    状态机语义（每 run_id 以 JSONL 中最后一条记录的 status 为准）：
        pending    提交流程开始时写入
        committed  提交流程全部完成（事件落盘+latest 更新+归档）后写入
        failed     校验失败/同步拒绝/同步失败等受控退出

    跨月恢复（终审三轮 §4）：同时扫描**上一个月**与当前月的日志——
    月末留下的 pending 在月初第一次运行时也能被发现（旧月在前、
    当前月覆盖，与 JSONL 末条为准语义一致）。

    提交流程保证「事件先于 latest 落盘」+ 内容幂等 event_id（不含 run_id：
    同一基线对之间的同一变化，无论哪个 run 观测都去重为同一事件），因此
    pending 运行无需数据修复：下一轮以旧基线重新比对，同样变化再次发现时
    被内容键吸收——无漏报、无重复行。本函数只负责**识别与告警**，
    供人工知晓中断区间。返回 pending 的 run_id 列表。
    """
    if now is None:
        from datetime import datetime
        now = datetime.now().astimezone()
    month = now.strftime("%Y%m")
    prev_year, prev_m = ((now.year - 1, 12) if now.month == 1
                         else (now.year, now.month - 1))
    prev_month = f"{prev_year}{prev_m:02d}"
    # 旧月在前、当前月在后：同 run_id 跨月出现时以较新记录为准
    records = (load_run_records(root, prev_month)
               + load_run_records(root, month))
    last_status: dict[str, str] = {}
    for r in records:
        rid, st = r.get("run_id"), r.get("status")
        if rid and st:
            last_status[rid] = st
    pending = [rid for rid, st in last_status.items() if st == "pending"]
    if pending:
        shown = ", ".join(pending[:5]) + ("…" if len(pending) > 5 else "")
        print(f"⚠️ 检测到 {len(pending)} 轮未正常收尾的运行: {shown}", flush=True)
        print("   提交流程保证事件先于 latest 落盘（event_id 幂等吸收重放），"
              "本轮将以旧基线重新比对，无漏报/重复风险。", flush=True)
    return pending
