# -*- coding: utf-8 -*-
"""快照持久化：保存与读取历史快照（JSON）。

- save_snapshot 原子写入：先写临时文件再 os.replace，程序中断不会留下半文件
- load_latest_snapshot 容错读取：损坏文件跳过并告警，不会让整个流程崩溃
- 快照格式：snapshot_YYYY-MM-DD.json = {"date","total","items","stores"}
"""
from __future__ import annotations

import json
import os
from datetime import datetime
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


# ============================================================
# v2：run_id 粒度快照（阶段2 数据引擎改造，R1/R3 修复）
#
# 四类数据（目录规范，均在 data/ 下）：
#   snapshots/runs/{run_id}.json   每轮运行快照（含 complete 标记，留证）
#   snapshots/latest.json          最新有效完整快照（只有 complete=true 才更新）
#   snapshots/archive/arc-*.json   归档快照（默认每小时一份，可配置）
#   运行记录与变化事件见 core/runlog.py / core/events.py
#
# 防误报语义：
#   - complete=false 的快照绝不进入 latest.json，也绝不作为 diff 基线
#   - 快照含 run_id / observed_at（ISO 含时区）——回答"上次确认时间"
#   - 原子写入（tmp+replace），中断不留半文件；损坏文件跳过并告警
# ============================================================

_RUN_SNAPSHOT_SCHEMA = "run-snapshot-v1"


def runs_dir(root) -> Path:
    return Path(root) / "data" / "snapshots" / "runs"


def _fsync_dir(path: Path) -> None:
    """目录 fsync：改名/新建文件后同步目录条目（终审四轮 §顺序）。

    os.replace 的改名操作也依赖目录条目持久化——仅 fsync 文件不保证
    断电后改名生效。个别文件系统（网络挂载/FAT 等）不支持目录 fsync
    时静默降级（进程级保证仍在，见模块末尾持久性声明）。
    """
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


def _atomic_write_json(path: Path, payload) -> None:
    """原子写 JSON（终审四轮 §顺序定稿，四步顺序固定）：

        ① 写临时文件 → ② fsync 临时文件（内容先落介质）
        → ③ os.replace 原子改名 → ④ fsync 目录（改名条目持久）

    断电语义：任一步之前断电 → 旧文件原样在（tmp 残留无害，回退扫描
    已排除 .tmp_ 前缀）；③④ 之间断电 → 改名可能未持久 → 仍取旧值，
    安全（用旧基线重 diff，内容幂等键吸收）。绝无半写状态。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp_{path.name}")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str))
        fh.flush()
        os.fsync(fh.fileno())  # ② 内容先落介质
    os.replace(tmp, path)      # ③ 原子改名
    _fsync_dir(path.parent)    # ④ 改名条目持久


def save_run_snapshot(root, run_id: str, observed_at: str, complete: bool,
                      items: list[dict], stores: dict | None = None,
                      total: int | None = None, expected_count: int | None = None,
                      collected_count: int | None = None,
                      failed_pages: list | None = None,
                      failed_items: list | None = None,
                      validation_errors: list | None = None) -> Path:
    """保存本轮运行快照到 snapshots/runs/{run_id}.json（complete 标记如实写入）。"""
    payload = {
        "schema": _RUN_SNAPSHOT_SCHEMA,
        "run_id": run_id,
        "observed_at": observed_at,
        "complete": bool(complete),
        "total": total,
        "expected_count": expected_count,
        "collected_count": collected_count if collected_count is not None else len(items),
        "unique_count": len(items),
        "failed_pages": list(failed_pages or []),
        "failed_items": list(failed_items or []),
        "validation_errors": list(validation_errors or []),
        "items": list(items),
        "stores": stores,
    }
    _atomic_write_json(runs_dir(root) / f"{run_id}.json", payload)
    return runs_dir(root) / f"{run_id}.json"


def update_latest(root, run_payload: dict) -> Path:
    """用**完整**快照更新 latest.json。complete=false 时拒绝（fail-closed）。"""
    if not run_payload.get("complete"):
        raise ValueError("拒绝将不完整快照写入 latest.json（完整性保护）")
    return _atomic_write_json(Path(root) / "data" / "snapshots" / "latest.json",
                              run_payload)


def load_latest_valid(root, exclude_run_id: str | None = None):
    """最近一份有效完整基线。返回 (run_id, payload)；没有返回 (None, None)。

    优先读 latest.json；损坏/不完整时回退扫描 runs/*.json（complete=true），
    按 (observed_at, run_id) 确定性排序取最大者（同秒多轮取 run_id 更大者）。
    损坏文件跳过并告警。
    exclude_run_id：排除当前运行自身（防止本轮快照充当自己的基线）。
    """
    latest_path = Path(root) / "data" / "snapshots" / "latest.json"
    if latest_path.exists():
        try:
            payload = json.loads(latest_path.read_text(encoding="utf-8"))
            if (isinstance(payload, dict)
                    and payload.get("schema") == _RUN_SNAPSHOT_SCHEMA  # P1-2 契约校验
                    and payload.get("complete")
                    and payload.get("items")
                    and payload.get("run_id") != exclude_run_id):
                return payload.get("run_id"), payload
            if isinstance(payload, dict) and payload.get("schema") != _RUN_SNAPSHOT_SCHEMA:
                print(f"⚠️ latest.json schema 不符合 {_RUN_SNAPSHOT_SCHEMA}，"
                      "按无效快照处理，回退扫描运行快照", flush=True)
        except Exception as e:
            print(f"⚠️ latest.json 损坏已忽略，回退扫描运行快照: {e}", flush=True)

    candidates: list[tuple[str, str, dict]] = []  # (observed_at, run_id, payload)
    for f in runs_dir(root).glob("*.json"):
        if f.name.startswith(".tmp_"):
            continue  # 原子写失败残留的临时文件（终审四轮 §F3）：不参与基线扫描
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️ 运行快照损坏已跳过: {f.name} ({type(e).__name__})", flush=True)
            continue
        if not isinstance(payload, dict) or not payload.get("complete"):
            continue
        if not payload.get("items"):
            continue
        if exclude_run_id and payload.get("run_id") == exclude_run_id:
            continue
        candidates.append((str(payload.get("observed_at") or ""),
                           str(payload.get("run_id") or f.stem), payload))
    if not candidates:
        return None, None
    # 确定性排序（合并前终审 §2）：主键 observed_at、次键 run_id（字典序），
    # 不依赖文件系统遍历顺序——同观测时刻多轮取 run_id 更大者，结果可复现
    candidates.sort(key=lambda c: (c[0], c[1]))
    obs, rid, payload = candidates[-1]
    ties = sorted(c[1] for c in candidates if c[0] == obs and c[1] != rid)
    if ties:
        print(f"⚠️ 同一观测时刻存在 {len(ties) + 1} 轮完整快照"
              f"（{rid} 与 {', '.join(ties)}），已按确定性规则取 {rid}", flush=True)
    return rid, payload


def archive_if_due(root, run_payload: dict, interval_minutes: int = 60,
                   now=None):
    """按配置间隔归档一份**完整**快照。不完整运行不得产生归档。

    归档文件名含时间戳（arc-YYYYMMDD-HHMMSS.json）；间隔未到则跳过。
    返回归档文件路径，未归档返回 None。
    """
    if not run_payload.get("complete"):
        return None  # 不完整扫描不能伪装成完整归档
    arc_dir = Path(root) / "data" / "snapshots" / "archive"
    arc_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(arc_dir.glob("arc-*.json"))
    if now is None:
        now = datetime.now().astimezone()
    if existing:
        try:
            last_name = existing[-1].stem  # arc-YYYYMMDD-HHMMSS
            last_dt = datetime.strptime(last_name[len("arc-"):], "%Y%m%d-%H%M%S")
            last_dt = last_dt.replace(tzinfo=now.tzinfo)  # 归档名即本地时间
            elapsed_min = (now - last_dt).total_seconds() / 60
            if elapsed_min < interval_minutes:
                return None
        except ValueError:
            pass  # 文件名异常 → 保守起见继续归档
    stamp = now.strftime("%Y%m%d-%H%M%S")
    payload = dict(run_payload)
    payload["archived_at"] = now.isoformat(timespec="seconds")
    target = arc_dir / f"arc-{stamp}.json"
    _atomic_write_json(target, payload)
    return target
