# -*- coding: utf-8 -*-
"""快照持久化：保存与读取历史快照（JSON）。

- save_snapshot 原子写入：先写临时文件再 os.replace，程序中断不会留下半文件
- load_latest_snapshot 容错读取：损坏文件跳过并告警，不会让整个流程崩溃
- 快照格式：snapshot_YYYY-MM-DD.json = {"date","total","items","stores"}
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def snapshot_path(directory, date: str) -> Path:
    return Path(directory) / f"snapshot_{date}.json"


def save_snapshot(directory, date: str, items: list[dict],
                  stores: dict | None = None, total: int | None = None) -> Path:
    """保存当日快照（原子写入）。items 为归一化 item 列表。"""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": date,
        "total": total if total is not None else len(items),
        "items": list(items),
        "stores": stores,
    }
    tmp = d / f".tmp_snapshot_{date}.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    target = snapshot_path(d, date)
    os.replace(tmp, target)  # 原子替换
    return target


def load_latest_snapshot(directory, before_date: str | None = None):
    """最近一份有效快照（可选：只看 before_date 之前的日期）。

    返回 (date, payload)；没有任何有效快照返回 (None, None)。
    损坏文件跳过并打印告警。
    """
    best = None
    for f in sorted(Path(directory).glob("snapshot_*.json")):
        d = f.stem.replace("snapshot_", "")
        if before_date and d >= before_date:
            continue
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️ 快照损坏已跳过: {f.name} ({type(e).__name__})", flush=True)
            continue
        if not payload.get("items"):
            continue
        if best is None or d > best[0]:
            best = (d, payload)
    return best if best else (None, None)
