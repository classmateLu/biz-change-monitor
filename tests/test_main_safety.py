#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主流程安全测试（GPT 整改任务 1 验收）。

覆盖：
- 默认运行（无 --enable-write）→ 钉钉 HTTP 调用次数为 0，即使配置里填了"真实"凭据
- --enable-write + 示例适配器 → 拒绝写入（exit 2），HTTP 调用次数为 0
- --enable-write + 配置缺失 → 拒绝写入（exit 2）
- decide_write_mode 四象限
- sync_state.json 原子写入

全部使用虚构凭据与 Mock，绝不连接真实钉钉。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app_main  # noqa: E402
import notifications.dingtalk as dk  # noqa: E402
from adapters.base import DataSource  # noqa: E402
from adapters.example_adapter import ExampleAdapter  # noqa: E402
from core.models import make_item  # noqa: E402

FAKE_CFG = {
    "dingtalk": {
        "app_key": "FAKE-KEY-NOT-REAL",
        "app_secret": "FAKE-SECRET-NOT-REAL",
        "operator_unionid": "FAKE-UNIONID",
        "base_node_id": "FAKE-BASE",
        "sheet_a_id": "FAKE-SHEET-A",
        "sheet_b_id": "FAKE-SHEET-B",
        "webhook": "https://oapi.dingtalk.com/robot/send?access_token=FAKE",
        "webhook_keyword": "test",
    },
    "validation": {"min_records": 10},
}


class FakeRealAdapter(DataSource):
    """模拟"真实适配器"（is_example=False），数据仍是虚构的。"""

    name = "fake_real"
    is_example = False

    def fetch(self):
        from adapters.base import CollectionResult
        items = [make_item("FAKE-0001", name="测试对象", price=100.0, count=1)]
        return CollectionResult(items=items, stores={"FAKE-0001": ["Demo Alpha 店"]},
                                total=1)


def _write_fake_config():
    cfg_path = app_main.ROOT / "config" / "config.yaml"
    existed = cfg_path.exists()
    backup = cfg_path.read_text(encoding="utf-8") if existed else None
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    import yaml
    cfg_path.write_text(yaml.safe_dump(FAKE_CFG, allow_unicode=True), encoding="utf-8")
    return cfg_path, existed, backup


def _restore_config(cfg_path, existed, backup):
    if existed:
        cfg_path.write_text(backup, encoding="utf-8")
    else:
        cfg_path.unlink(missing_ok=True)


def run_checks():
    checks = []
    original_http = dk._http_json
    cfg_path, existed, backup = _write_fake_config()

    http_calls = []

    def counting_fake(url, payload=None, token=None, timeout=60):
        http_calls.append(url)  # 任何真实 HTTP 意图都会被记录
        return {"kind": "ok", "data": {}, "http_status": 200, "detail": ""}

    try:
        # ---- decide_write_mode 四象限 ----
        mode, _ = app_main.decide_write_mode(False, ExampleAdapter(), FAKE_CFG["dingtalk"])
        checks.append((mode == "dry_run", "未启用写入 → dry_run"))
        mode, reason = app_main.decide_write_mode(True, ExampleAdapter(), FAKE_CFG["dingtalk"])
        checks.append((mode == "refuse" and "示例" in reason,
                       "--enable-write + 示例适配器 → refuse（虚构数据禁写真实表）"))
        mode, reason = app_main.decide_write_mode(True, FakeRealAdapter(), None)
        checks.append((mode == "refuse" and "配置不完整" in reason,
                       "--enable-write + 配置缺失 → refuse"))
        mode, _ = app_main.decide_write_mode(True, FakeRealAdapter(), FAKE_CFG["dingtalk"])
        checks.append((mode == "write", "--enable-write + 真实适配器 + 配置齐备 → write"))

        # ---- 默认运行：即使配置含凭据，HTTP 调用次数必须为 0 ----
        dk._http_json = counting_fake
        sys.argv = ["main.py", "--date", "2099-01-01"]
        rc = app_main.main()
        checks.append((rc == 0, f"默认运行 exit 0（实际 {rc}）"))
        checks.append((len(http_calls) == 0,
                       f"默认运行钉钉 HTTP 调用次数=0（实际 {len(http_calls)}）"))

        # ---- --enable-write + 示例适配器 → 拒绝（exit 2），HTTP 仍为 0 ----
        sys.argv = ["main.py", "--date", "2099-01-02", "--enable-write"]
        rc = app_main.main()
        checks.append((rc == 2, f"enable-write+示例适配器 exit 2（实际 {rc}）"))
        checks.append((len(http_calls) == 0,
                       f"拒绝后 HTTP 调用次数仍=0（实际 {len(http_calls)}）"))

        # ---- sync_state 原子写入：mark_synced 后文件是合法 JSON，无 .tmp 残留 ----
        app_main.mark_synced("test_label", "2099-01-01")
        st = json.loads(app_main.SYNC_MARK.read_text(encoding="utf-8"))
        checks.append((st.get("test_label") == "2099-01-01", "sync_state 标记可读"))
        checks.append((not list(app_main.SYNC_MARK.parent.glob("*.tmp")),
                       "原子写入无 .tmp 残留"))
    finally:
        dk._http_json = original_http
        _restore_config(cfg_path, existed, backup)
        app_main.SYNC_MARK.unlink(missing_ok=True)
        app_main._LOCK_FH = None  # 释放进程锁，避免影响后续用例

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


def test_main_safety():
    """pytest 入口。"""
    assert not [msg for ok, msg in run_checks() if not ok]


if __name__ == "__main__":
    sys.exit(main())
