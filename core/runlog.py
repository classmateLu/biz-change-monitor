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
"""
from __future__ import annotations

import json
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
    # 崩溃残留半行隔离（同 core/events.py 的 I4 修复）：防新记录拼接到半行上
    if path.exists() and path.stat().st_size > 0:
        with path.open("rb") as fh:
            fh.seek(-1, 2)
            if fh.read(1) != b"\n":
                with path.open("a", encoding="utf-8") as fh2:
                    fh2.write("\n")
                print(f"⚠️ 检测到未写完的半行（疑似崩溃残留），已补换行隔离: {path.name}",
                      flush=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


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

    提交流程保证「事件先于 latest 落盘」+ event_id 幂等，因此 pending 运行
    无需数据修复：下一轮以旧基线重新比对，同样变化再次发现时被幂等吸收——
    无漏报、无重复。本函数只负责**识别与告警**，供人工知晓中断区间。
    返回 pending 的 run_id 列表。
    """
    if now is None:
        from datetime import datetime
        now = datetime.now().astimezone()
    month = now.strftime("%Y%m")
    records = load_run_records(root, month)
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
