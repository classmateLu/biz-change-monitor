# -*- coding: utf-8 -*-
"""变化事件存储（v2）：JSONL 追加 + event_id 幂等去重。

目录规范：data/events/events-YYYYMM.jsonl（按月分区，避免单文件无限膨胀）

设计要点：
- 追加式：历史变化默认不自动删除（需求 2.3）
- 幂等：event_id 已存在的行跳过——同一轮重试/重复处理不会重复落盘
  （存储层自身防重，不依赖通知层）
- 崩溃安全：每行独立 JSON + 换行；进程中断最坏留下半行，
  读取时跳过损坏行并告警，不影响其余数据
"""
from __future__ import annotations

import json
from pathlib import Path


def events_dir(root) -> Path:
    return Path(root) / "data" / "events"


def _month_of(observed_at: str, fallback: str) -> str:
    """从 ISO 观测时间取 YYYYMM；解析失败退回 fallback（today 的 YYYYMM）。"""
    return (observed_at or "")[:7].replace("-", "") or fallback


def _month_file(root, month: str) -> Path:
    return events_dir(root) / f"events-{month}.jsonl"


def _load_existing_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    if not path.exists():
        return ids
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("event_id"):
                    ids.add(obj["event_id"])
            except ValueError:
                # 半行/损坏行：跳过并告警（不中断读取，不影响其余数据）
                print(f"⚠️ 事件文件存在损坏行已跳过: {path.name}", flush=True)
    return ids


def _ensure_trailing_newline(path: Path) -> None:
    """崩溃残留半行隔离（I4 实证缺陷修复）：文件不以换行结尾时先补一个换行。

    否则后续追加的 JSON 会拼接在半行上 → 整行解析失败 → 新事件永久丢失。
    补换行后，半行成为独立损坏行（读取时跳过并告警），新事件完整独立。
    """
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("rb") as fh:
        fh.seek(-1, 2)
        if fh.read(1) != b"\n":
            with path.open("a", encoding="utf-8") as fh2:
                fh2.write("\n")
            print(f"⚠️ 检测到未写完的半行（疑似崩溃残留），已补换行隔离: {path.name}",
                  flush=True)


def append_events(root, events: list[dict], today: str = "") -> tuple[int, int]:
    """把变化事件追加落盘（按月分区 + event_id 去重）。

    返回 (新增条数, 去重跳过条数)。
    """
    appended = skipped = 0
    fallback = (today or "")[:7].replace("-", "")
    by_month: dict[str, list[dict]] = {}
    for ev in events:
        month = _month_of(ev.get("observed_at", ""), fallback or "197001")
        by_month.setdefault(month, []).append(ev)

    for month, evs in by_month.items():
        path = _month_file(root, month)
        _ensure_trailing_newline(path)
        existing = _load_existing_ids(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for ev in evs:
                eid = ev.get("event_id")
                if eid and eid in existing:
                    skipped += 1
                    continue
                fh.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
                if eid:
                    existing.add(eid)
                appended += 1
    return appended, skipped


def load_events(root, month: str) -> list[dict]:
    """读取某月（YYYYMM）全部有效事件（损坏行跳过）。"""
    path = _month_file(root, month)
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
