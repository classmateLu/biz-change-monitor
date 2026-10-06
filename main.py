#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""示例主流程：采集 → 完整性校验 → 快照 → Diff →（可选）钉钉同步。

用法：
    python main.py                       # 默认安全模式：只采集/快照/比对，绝不写钉钉
    python main.py --date 2026-10-05     # 指定"今天"的日期标签（默认取系统日期）
    python main.py --enable-write        # 显式启用钉钉写入（需同时通过两项校验）

写入模式安全闸门（--enable-write 需同时满足，否则拒绝写入并 exit 2）：
    1. 数据源不是示例适配器（ExampleAdapter 的虚构数据禁止写入真实表）
    2. 钉钉配置完整（app_key/app_secret/base_node_id/operator_unionid/sheet_a_id/sheet_b_id）
    仅"配置文件里存在真实凭据"不会触发写入——写入永远需要显式命令行参数。

流程与退出码：
    互斥锁（防并发重复写入）
    → 采集 → validation 校验（失败 → 告警 + exit 1，不写快照不比对）
    → 原子快照 → 加载最近有效基线 → diff
    → dry-run：只打印变化 / write：同步表A/表B（幂等保护）→ exit 0
    → 写入模式被拒绝 → exit 2

幂等与恢复语义（如实声明）：
    - data/sync_state.json 记录"当天已成功写入"的表（键=表ID，值含日期+数据摘要），
      原子写入（tmp+replace）；状态文件损坏 → 显式告警并停止自动同步（fail-safe）
    - 当天重跑自动跳过已成功写入的表；强制重写删除该文件即可
    - 写入成功但标记前崩溃的极端情况仍可能产生一次重复——本系统为
      at-least-once 语义（钉钉接口不支持幂等键），不宣称"恰好一次"
    - 写入响应做严格校验：未知结构默认按失败处理（宁可拒绝重试，不静默丢数据）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
_LOCK_FH = None  # 进程内持锁句柄（同进程重复调用不自我阻塞）


def _sync_mark_path() -> Path:
    """运行时解析（而非模块级固定），便于测试隔离到临时目录。"""
    return ROOT / "data" / "sync_state.json"


# ---------- 并发保护 ----------

def acquire_lock() -> None:
    """单实例互斥锁（flock）。已有实例在跑时立即退出，避免并发重复写入。

    fcntl 仅 Unix/macOS 可用；其他平台跳过文件锁（CI 环境/Windows）。
    """
    global _LOCK_FH
    if _LOCK_FH is not None:
        return
    lock_path = ROOT / "data" / ".monitor.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        return
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        print("❌ 检测到另一个实例正在运行（data/.monitor.lock 被占用），"
              "退出以避免并发重复写入。", flush=True)
        sys.exit(1)
    _LOCK_FH = fh


# ---------- 幂等保护（原子写入 + 状态损坏 fail-safe） ----------

def _write_json_atomic(path: Path, payload) -> None:
    """先写临时文件再 os.replace，中断不会留下半文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _load_sync_state() -> tuple[dict, bool, str]:
    """读取同步状态。返回 (state, healthy, reason)。

    状态损坏/非法 → healthy=False（调用方必须停止自动写入，防重复），
    绝不静默当作空状态。
    """
    path = _sync_mark_path()
    if not path.exists():
        return {}, True, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("顶层不是 JSON 对象")
        return data, True, ""
    except Exception as e:
        return {}, False, f"同步状态文件损坏（{type(e).__name__}: {e}）"


def already_synced(sheet_id: str, today: str) -> tuple[bool, str]:
    """查询某表当天是否已成功写入。返回 (synced, note)。

    新格式：{sheet_id: {"date": ..., "digest": ...}}；
    兼容旧格式：{label: "YYYY-MM-DD"}。
    """
    st, healthy, why = _load_sync_state()
    if not healthy:
        return False, why
    entry = st.get(sheet_id)
    if isinstance(entry, dict):
        return entry.get("date") == today, ""
    if isinstance(entry, str):  # 旧格式兼容
        return entry == today, ""
    return False, ""


def mark_synced(sheet_id: str, today: str, digest: str = "") -> None:
    st, healthy, _ = _load_sync_state()
    if not healthy:
        # 状态损坏时绝不写入新标记——保持损坏现场等人工处理
        return
    st[sheet_id] = {"date": today, "digest": digest}
    _write_json_atomic(_sync_mark_path(), st)


def _rows_digest(rows: list[dict]) -> str:
    """本批数据的摘要（绑定"表+日期+内容"，换表/改数据不会误跳过同步）。"""
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


# ---------- 写入模式安全闸门 ----------

def decide_write_mode(enable_write: bool, adapter, dt_cfg: dict | None) -> tuple[str, str]:
    """决定本次运行的同步模式。返回 (mode, reason)。

    mode: "dry_run"（默认，绝不写钉钉）| "write"（显式启用且校验通过）
          | "refuse"（显式启用但校验未通过，必须拒绝）
    """
    if not enable_write:
        return "dry_run", "默认安全模式：未显式启用写入（--enable-write）"
    if getattr(adapter, "is_example", False):
        return "refuse", ("写入模式已启用，但当前数据源是示例适配器（虚构数据）——"
                          "禁止写入真实钉钉表。请接入真实适配器后重试。")
    if dt_cfg is None:
        return "refuse", ("写入模式已启用，但钉钉配置不完整"
                          "（需 app_key/app_secret/base_node_id/operator_unionid/"
                          "sheet_a_id/sheet_b_id）。")
    return "write", ""


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
                  changes: list[dict]) -> bool:
    """写表A（当日全量）+ 表B（变化），含幂等与失败告警。

    返回 True=全部写入成功；False=存在未成功项（调用方应以非零码退出，
    已成功的表受幂等保护不会被重复写入）。
    """
    from notifications import dingtalk as dk

    # 状态文件健康检查：损坏 → 停止自动同步（fail-safe，防重复写入）
    _, healthy, why = _load_sync_state()
    if not healthy:
        msg = (f"【同步已停止】{why}。"
               f"请人工检查 {_sync_mark_path()}（删除后重跑可能重复写入当日数据）。")
        print(f"❌ {msg}", flush=True)
        if dt.get("webhook"):
            dk.send_webhook(dt["webhook"], msg, dt.get("webhook_keyword", ""))
        return False

    try:
        token = dk.get_access_token(dt["app_key"], dt["app_secret"])
    except RuntimeError as e:
        print(f"❌ 获取 accessToken 失败，本次不写入（可重试）: {e}", flush=True)
        if dt.get("webhook"):
            dk.send_webhook(dt["webhook"], "【同步异常】获取 accessToken 失败，本次未写入。",
                            dt.get("webhook_keyword", ""))
        return False

    base, unionid = dt["base_node_id"], dt["operator_unionid"]

    def alert(msg: str) -> None:
        if dt.get("webhook"):
            dk.send_webhook(dt["webhook"], msg, dt.get("webhook_keyword", ""))
        print(f"[告警] {msg}", flush=True)

    all_ok = True

    # 表A：当日全量
    rows_a = [{"fields": {
        "日期": today, "ID": it["gid"], "名称": it.get("name") or "",
        "价格": it.get("price"), "份数": it.get("count"),
        "单价": it.get("unit_price")}} for it in items_index.values()]
    synced_a, _ = already_synced(dt["sheet_a_id"], today)
    if synced_a:
        print("表A今日已成功写入，跳过（幂等保护）", flush=True)
    else:
        ok, fail = dk.write_records(base, dt["sheet_a_id"], unionid, token, rows_a,
                                    on_error=alert)
        if ok and not fail:
            mark_synced(dt["sheet_a_id"], today, _rows_digest(rows_a))
        else:
            all_ok = False

    # 表B：变化（无变化写占位行，证明系统在跑）
    rows_b = [{"fields": {
        "变化日期": c["date"], "ID": c["gid"], "名称": c.get("name") or "",
        "变化类型": c["type"], "旧值": str(c.get("old_value", "")),
        "新值": str(c.get("new_value", "")), "差异说明": c["note"]}}
        for c in changes] if changes else [
        {"fields": {"变化日期": today, "ID": "—", "名称": "—",
                    "变化类型": "无变化", "旧值": "", "新值": "",
                    "差异说明": "今日无变化"}}]
    synced_b, _ = already_synced(dt["sheet_b_id"], today)
    if synced_b:
        print("表B今日已成功写入，跳过（幂等保护）", flush=True)
    else:
        ok, fail = dk.write_records(base, dt["sheet_b_id"], unionid, token, rows_b,
                                    on_error=alert)
        if ok and not fail:
            mark_synced(dt["sheet_b_id"], today, _rows_digest(rows_b))
        else:
            all_ok = False

    return all_ok


# ---------- 主流程 ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"), help="今日日期标签")
    ap.add_argument("--enable-write", dest="enable_write", action="store_true",
                    help="显式启用钉钉写入（默认绝不写入；需通过适配器与配置校验）")
    args = ap.parse_args()
    today = args.date

    # --date 严格校验：只允许合法 YYYY-MM-DD，防路径注入/意外覆盖
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", today):
        print("❌ --date 必须为 YYYY-MM-DD 格式（纯数字）。", flush=True)
        return 2
    try:
        time.strptime(today, "%Y-%m-%d")
    except ValueError:
        print(f"❌ --date 不是合法日期: {today}", flush=True)
        return 2

    acquire_lock()  # 单实例互斥：防并发重复写入

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

    # 5) 同步模式裁决（默认安全：dry-run；写入需 --enable-write + 双重校验）
    dt = load_dingtalk_config(cfg)
    mode, reason = decide_write_mode(args.enable_write, adapter, dt)
    if mode == "refuse":
        print(f"❌ {reason}", flush=True)
        return 2
    if mode == "write":
        if not dingtalk_sync(dt, today, items_index, changes):
            print("❌ 同步存在未成功项，本次以失败退出（退出码 1）。"
                  "已成功的表受幂等保护，重试不会被重复写入。", flush=True)
            return 1
    else:
        print(f"[dry-run] {reason}；"
              f"本次检测到 {len(changes)} 条变化，未写入钉钉。", flush=True)

    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
