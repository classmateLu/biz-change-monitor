#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""示例主流程：采集 → 完整性校验 → 快照 → Diff →（可选）钉钉同步。

用法：
    python main.py                 # 默认用 ExampleAdapter（虚构数据）
    python main.py --date 2026-10-05   # 指定"今天"的日期标签（默认取系统日期）

流程与退出码：
    采集 → validation 校验（失败 → 告警 + exit 1，不写快照不比对）
         → 原子快照 → 加载最近有效基线 → diff
         → 打印变化（配置了钉钉则同步表A/表B，含幂等保护）→ exit 0

幂等保护：data/sync_state.json 记录"当天已成功写入"的表；
当天重跑自动跳过写入。需要强制重写时删除该文件即可。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

from adapters.example_adapter import ExampleAdapter
from core.diff import diff_changes, summarize
from core.models import index_by_id
from core.snapshot import load_latest_snapshot, save_snapshot
from core.validation import validate_collection

ROOT = Path(__file__).resolve().parent
SYNC_MARK = ROOT / "data" / "sync_state.json"


# ---------- 幂等保护 ----------

def _load_sync_state() -> dict:
    try:
        return json.loads(SYNC_MARK.read_text(encoding="utf-8"))
    except Exception:
        return {}


def already_synced(label: str, today: str) -> bool:
    return _load_sync_state().get(label) == today


def mark_synced(label: str, today: str) -> None:
    st = _load_sync_state()
    st[label] = today
    SYNC_MARK.parent.mkdir(parents=True, exist_ok=True)
    SYNC_MARK.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


# ---------- 钉钉（可选） ----------

def load_dingtalk_config(cfg: dict) -> dict | None:
    """配置齐备才返回钉钉配置，否则返回 None（dry-run 模式）。"""
    dt = (cfg or {}).get("dingtalk") or {}
    required = ("app_key", "app_secret", "base_node_id", "operator_unionid",
                "sheet_a_id", "sheet_b_id")
    if all(dt.get(k) for k in required):
        return dt
    return None


def dingtalk_sync(dt: dict, today: str, items_index: dict,
                  changes: list[dict]) -> None:
    """写表A（当日全量）+ 表B（变化），含幂等与失败告警。"""
    from notifications import dingtalk as dk

    token = dk.get_access_token(dt["app_key"], dt["app_secret"])
    base, unionid = dt["base_node_id"], dt["operator_unionid"]

    def alert(msg: str) -> None:
        if dt.get("webhook"):
            dk.send_webhook(dt["webhook"], msg, dt.get("webhook_keyword", ""))
        print(f"[告警] {msg}", flush=True)

    # 表A：当日全量
    if already_synced("sheet_a", today):
        print("表A今日已成功写入，跳过（幂等保护）", flush=True)
    else:
        rows = [{"fields": {
            "日期": today, "ID": it["gid"], "名称": it.get("name") or "",
            "价格": it.get("price"), "份数": it.get("count"),
            "单价": it.get("unit_price")}} for it in items_index.values()]
        ok, fail = dk.write_records(base, dt["sheet_a_id"], unionid, token, rows,
                                    on_error=alert)
        if ok and not fail:
            mark_synced("sheet_a", today)

    # 表B：变化（无变化写占位行，证明系统在跑）
    if already_synced("sheet_b", today):
        print("表B今日已成功写入，跳过（幂等保护）", flush=True)
        return
    if changes:
        rows = [{"fields": {
            "变化日期": c["date"], "ID": c["gid"], "名称": c.get("name") or "",
            "变化类型": c["type"], "旧值": str(c.get("old_value", "")),
            "新值": str(c.get("new_value", "")), "差异说明": c["note"]}}
            for c in changes]
    else:
        rows = [{"fields": {"变化日期": today, "ID": "—", "名称": "—",
                            "变化类型": "无变化", "旧值": "", "新值": "",
                            "差异说明": "今日无变化"}}]
    ok, fail = dk.write_records(base, dt["sheet_b_id"], unionid, token, rows,
                                on_error=alert)
    if ok and not fail:
        mark_synced("sheet_b", today)


# ---------- 主流程 ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"), help="今日日期标签")
    args = ap.parse_args()
    today = args.date

    cfg_path = ROOT / "config" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    val_cfg = (cfg or {}).get("validation") or {}

    # 1) 采集（示例：替换成你自己的 adapter 即接入真实平台）
    adapter = ExampleAdapter()
    result = adapter.fetch()

    # 2) 完整性校验：失败 → 不写快照、不比对、不同步
    ok, reason = validate_collection(
        len(result.items),
        min_records=int(val_cfg.get("min_records", 50)),
        failed_pages=result.failed_pages,
        detail_total=result.detail_total,
        detail_failed=result.detail_failed,
        max_detail_failure_ratio=float(val_cfg.get("max_detail_failure_ratio", 0.05)))
    if not ok:
        msg = f"【采集不完整】{today} 校验未通过：{reason}。已终止（未写快照/未比对/未同步）。"
        print(f"❌ {msg}", flush=True)
        dt = load_dingtalk_config(cfg)
        if dt and dt.get("webhook"):
            from notifications import dingtalk as dk
            dk.send_webhook(dt["webhook"], msg, dt.get("webhook_keyword", ""))
        return 1

    # 3) 快照（原子写入）+ 基线
    items_index = index_by_id(result.items)
    snap = save_snapshot(ROOT / "data" / "snapshots", today,
                         result.items, result.stores, total=result.total)
    print(f"快照已保存: {snap.name}（{len(result.items)} 条）", flush=True)
    base_date, base_payload = load_latest_snapshot(ROOT / "data" / "snapshots",
                                                   before_date=today)
    if base_payload is None:
        print("无历史基线（首次运行），本次仅建快照，不产出变化事件。", flush=True)
        return 0
    old_items = index_by_id(base_payload["items"])

    # 4) Diff（纯函数）
    changes = diff_changes(old_items, items_index,
                           base_payload.get("stores"), result.stores, date=today)
    counts = summarize(changes)
    print(f"对比 {base_date}：变化 {len(changes)} 条 {counts}", flush=True)
    for c in changes[:20]:
        print(f"  [{c['type']}] {c['gid']} {c.get('name') or ''} {c['note']}", flush=True)
    if len(changes) > 20:
        print(f"  ……其余 {len(changes) - 20} 条见同步表", flush=True)

    # 5) 钉钉同步（配置齐备才执行，否则 dry-run）
    dt = load_dingtalk_config(cfg)
    if dt:
        dingtalk_sync(dt, today, items_index, changes)
    else:
        print("[dry-run] 未配置钉钉（config/config.yaml），跳过多维表同步。", flush=True)

    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
