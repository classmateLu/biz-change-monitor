#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主流程安全测试（GPT 两轮整改验收，全部在临时目录隔离运行）。

覆盖：
- decide_write_mode 四象限
- 默认运行（config 含凭据）→ 钉钉 HTTP 调用次数 = 0
- --enable-write + 示例适配器 → 拒绝（exit 2），HTTP 调用 = 0
- --date 非法格式/路径注入 → 拒绝（exit 2）
- 状态文件损坏 → 停止自动同步（exit 1），HTTP 调用 = 0
- 同步失败 → 主流程 exit 1；同步成功 → exit 0 且标记含数据摘要
- 同日重跑幂等：已写入的表不再发起 HTTP 调用
- sync_state 新旧格式兼容

全部使用虚构凭据与 Mock，绝不连接真实钉钉；测试数据隔离在临时目录。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app_main  # noqa: E402
import notifications.dingtalk as dk  # noqa: E402
from adapters.base import CollectionResult, DataSource  # noqa: E402
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
    "validation": {"min_records": 1},
}


class FakeRealAdapter(DataSource):
    """模拟"真实适配器"（is_example=False），数据仍是虚构的。"""

    name = "fake_real"
    is_example = False

    def fetch(self):
        items = [make_item("FAKE-0001", name="测试对象", price=100.0, count=1)]
        return CollectionResult(items=items, stores={"FAKE-0001": ["Demo Alpha 店"]},
                                total=1)


class _Ctx:
    """每个用例套件的隔离上下文：临时 ROOT + HTTP 调用计数。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bizmon-test-"))
        self.original_root = app_main.ROOT
        app_main.ROOT = self.tmp  # main.py 全部路径运行时解析 → 整体切到临时目录
        self.http_calls: list[str] = []
        self.webhook_alerts: list[str] = []
        self.original_http = dk._http_json
        self.original_webhook = dk.send_webhook
        self.original_adapter = app_main.ExampleAdapter

        def counting_http(url, payload=None, token=None, timeout=60):
            self.http_calls.append(url)
            return {"kind": "ok", "data": {}, "http_status": 200, "detail": ""}

        def noop_webhook(url, text, keyword=""):
            self.webhook_alerts.append(text)
            return {"kind": "ok", "data": {}, "http_status": 200, "detail": ""}

        dk._http_json = counting_http
        dk.send_webhook = noop_webhook

    def write_fake_config(self):
        import yaml
        cfg_path = self.tmp / "config" / "config.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(yaml.safe_dump(FAKE_CFG, allow_unicode=True), encoding="utf-8")

    def run_main(self, date: str = "2099-01-01", *extra_args) -> int:
        sys.argv = ["main.py", "--date", date, *extra_args]
        return app_main.main()

    def cleanup(self):
        dk._http_json = self.original_http
        dk.send_webhook = self.original_webhook
        app_main.ExampleAdapter = self.original_adapter
        app_main.ROOT = self.original_root
        app_main._LOCK_FH = None
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


def run_checks():
    checks = []
    ctx = _Ctx()
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
        ctx.write_fake_config()
        rc = ctx.run_main("2099-01-01")  # 建基线
        checks.append((rc == 0, f"默认运行 exit 0（实际 {rc}）"))
        checks.append((len(ctx.http_calls) == 0,
                       f"默认运行钉钉 HTTP 调用次数=0（实际 {len(ctx.http_calls)}）"))

        # ---- --enable-write + 示例适配器 → 拒绝（exit 2），HTTP 仍为 0 ----
        rc = ctx.run_main("2099-01-02", "--enable-write")
        checks.append((rc == 2, f"enable-write+示例适配器 exit 2（实际 {rc}）"))
        checks.append((len(ctx.http_calls) == 0, "拒绝后 HTTP 调用次数仍=0"))

        # ---- --date 非法格式 / 路径注入 → exit 2，不产生任何文件 ----
        rc = ctx.run_main("2099-01-02", "--date", "../../etc/passwd")
        checks.append((rc == 2, f"--date 路径注入 → exit 2（实际 {rc}）"))
        rc = ctx.run_main("2099-01-02", "--date", "2099-13-40")
        checks.append((rc == 2, f"--date 非法日期(13月40日) → exit 2（实际 {rc}）"))
        bad_files = list((ctx.tmp / "data" / "snapshots").glob("*passwd*")) \
            if (ctx.tmp / "data" / "snapshots").exists() else []
        checks.append((not bad_files, "路径注入未产生任何文件"))

        # ---- 状态文件损坏 → 停止自动同步（fail-safe），HTTP 调用 = 0 ----
        app_main.ExampleAdapter = FakeRealAdapter  # 注入"真实适配器"以通过写入闸门
        mark_path = app_main._sync_mark_path()
        mark_path.parent.mkdir(parents=True, exist_ok=True)
        mark_path.write_text("{broken json!!", encoding="utf-8")
        rc = ctx.run_main("2099-01-03", "--enable-write")
        checks.append((rc == 1, f"状态损坏 → exit 1（实际 {rc}）"))
        checks.append((len(ctx.http_calls) == 0,
                       f"状态损坏时 HTTP 调用次数=0（实际 {len(ctx.http_calls)}）"))
        checks.append((any("同步已停止" in a for a in ctx.webhook_alerts),
                       "状态损坏触发显式告警（含'同步已停止'）"))

        # ---- 同步失败 → exit 1；同步成功 → exit 0 且标记含摘要 ----
        mark_path.unlink(missing_ok=True)  # 恢复健康状态
        dk.get_access_token = lambda k, s: "FAKE-TOKEN"
        dk.write_records = lambda *a, **k: (0, 1)  # 模拟写入失败
        rc = ctx.run_main("2099-01-04", "--enable-write")
        checks.append((rc == 1, f"同步失败 → 主流程 exit 1（实际 {rc}）"))

        dk.write_records = lambda *a, **k: (1, 0)  # 模拟写入成功（两张表各 1 行）
        rc = ctx.run_main("2099-01-05", "--enable-write")
        checks.append((rc == 0, f"同步成功 → exit 0（实际 {rc}）"))
        st = json.loads(app_main._sync_mark_path().read_text(encoding="utf-8"))
        entry = st.get("FAKE-SHEET-A", {})
        checks.append((entry.get("date") == "2099-01-05" and entry.get("digest"),
                       "同步标记绑定 表ID+日期+数据摘要（新格式）"))

        # ---- 幂等：同一天重跑成功写入的表 → 跳过（HTTP 0 次） ----
        before = len(ctx.http_calls)
        rc = ctx.run_main("2099-01-05", "--enable-write")
        checks.append((rc == 0, f"同日重跑 exit 0（实际 {rc}）"))
        checks.append((len(ctx.http_calls) == before,
                       "同日重跑：已写入的表不再发起 HTTP 调用"))

        # ---- 旧格式状态文件兼容 ----
        legacy = {"FAKE-SHEET-A": "2099-01-05"}
        app_main._write_json_atomic(app_main._sync_mark_path(), legacy)
        synced, note = app_main.already_synced("FAKE-SHEET-A", "2099-01-05")
        checks.append((synced and note == "", "旧格式状态文件兼容读取"))
    finally:
        ctx.cleanup()

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
