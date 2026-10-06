#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""钉钉同步安全测试（全 Mock，绝不连接真实钉钉服务）。

覆盖 GPT 整改任务 2 的验收标准：
- HTTP 200 但业务失败 → 不得报告同步成功
- 网络异常/超时 → 不得误判为成功
- 非法 JSON → 不得误判为成功，且不得崩溃
- 只有确认业务成功才计入成功数
- 失败时 on_error 告警回调被触发

既可 pytest 运行，也可直接执行本文件。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notifications.dingtalk as dk  # noqa: E402


def run_checks():
    checks = []
    rows = [{"fields": {"ID": f"DEMO-{i:04d}"}} for i in range(3)]

    # ---- classify_business 直接判定 ----
    checks.append((dk.classify_business({"records": [1]}) == (True, ""),
                   "正常响应（无错误字段）→ 业务成功"))
    checks.append((dk.classify_business({"code": 0, "records": []})[0] is True,
                   "code=0 → 业务成功"))
    checks.append((dk.classify_business({"code": "dingtalk.x.y", "message": "forbidden"})[0] is False,
                   "code=非零字符串 → 业务失败"))
    checks.append((dk.classify_business({"errcode": 310000, "errmsg": "bad request"})[0] is False,
                   "errcode=310000 → 业务失败"))
    checks.append((dk.classify_business({"error": "something"})[0] is False,
                   "error 字段非空 → 业务失败"))

    # ---- write_records：HTTP 200 + 业务失败 ≠ 成功 ----
    calls = []
    original = dk._http_json

    def fake_ok(url, payload=None, token=None, timeout=60):
        calls.append(url)
        return {"kind": "ok", "data": {"records": [{"id": "x"}]}, "http_status": 200, "detail": ""}

    dk._http_json = fake_ok
    ok, fail = dk.write_records("BASE", "SHEET", "UID", "TOKEN", rows, on_error=lambda m: None)
    checks.append(((ok, fail) == (3, 0), f"业务成功 → 3行全部成功（实际 ok={ok}, fail={fail}）"))

    dk._http_json = lambda *a, **k: {"kind": "ok", "data": {"code": "dingtalk.forbidden",
                                                            "message": "no permission"},
                                     "http_status": 200, "detail": ""}
    alerts = []
    ok, fail = dk.write_records("BASE", "SHEET", "UID", "TOKEN", rows, on_error=alerts.append)
    checks.append(((ok, fail) == (0, 3), f"HTTP 200 + 业务失败 → 0成功3失败（实际 ok={ok}, fail={fail}）"))
    checks.append((len(alerts) >= 1, "业务失败时 on_error 告警回调被触发"))

    # ---- 网络异常 / HTTP 错误 / 非法 JSON → 全部计为失败，不崩溃 ----
    for kind, fake in [
        ("network_error", lambda *a, **k: {"kind": "network_error", "data": None,
                                           "http_status": None, "detail": "网络异常: TimeoutError"}),
        ("http_error", lambda *a, **k: {"kind": "http_error", "data": None,
                                        "http_status": 403, "detail": "HTTP 403"}),
        ("invalid_json", lambda *a, **k: {"kind": "invalid_json", "data": None,
                                          "http_status": 200, "detail": "响应不是合法 JSON"}),
    ]:
        dk._http_json = fake
        alerts = []
        ok, fail = dk.write_records("BASE", "SHEET", "UID", "TOKEN", rows, on_error=alerts.append)
        checks.append(((ok, fail) == (0, 3), f"{kind} → 0成功3失败，不崩溃（实际 ok={ok}, fail={fail}）"))
        checks.append((len(alerts) >= 1, f"{kind} → 告警回调触发"))

    # ---- 部分批次失败：只有成功批次计入成功 ----
    responses = iter([
        {"kind": "ok", "data": {"records": [{"id": "x"}]}, "http_status": 200, "detail": ""},
        {"kind": "network_error", "data": None, "http_status": None, "detail": "网络异常"},
    ])

    def fake_partial(url, payload=None, token=None, timeout=60):
        return next(responses)

    dk._http_json = fake_partial
    six_rows = [{"fields": {"ID": str(i)}} for i in range(6)]  # batch_size=3 → 2 批
    ok, fail = dk.write_records("BASE", "SHEET", "UID", "TOKEN", six_rows,
                                batch_size=3, on_error=lambda m: None)
    checks.append(((ok, fail) == (3, 3), f"部分批次失败 → 成功3失败3（实际 ok={ok}, fail={fail}）"))

    # ---- get_access_token：成功 / 失败 ----
    dk._http_json = lambda *a, **k: {"kind": "ok", "data": {"accessToken": "T"}, "http_status": 200, "detail": ""}
    checks.append((dk.get_access_token("K", "S") == "T", "get_access_token 成功路径"))
    dk._http_json = lambda *a, **k: {"kind": "network_error", "data": None,
                                     "http_status": None, "detail": "网络异常"}
    try:
        dk.get_access_token("K", "S")
        checks.append((False, "get_access_token 网络失败应抛 RuntimeError"))
    except RuntimeError:
        checks.append((True, "get_access_token 网络失败 → RuntimeError（不误判成功）"))

    # ---- URL 脱敏：detail 不含完整 URL ----
    dk._http_json = lambda *a, **k: {"kind": "http_error", "data": None, "http_status": 500,
                                     "detail": "HTTP 500: server error"}
    alerts = []
    dk.write_records("BASE", "SHEET", "UID", "TOKEN", rows, on_error=alerts.append)
    leaked = any("access_token=" in m or "operatorId=" in m for m in alerts)
    checks.append((not leaked, "告警信息不含 URL 凭证片段（access_token=/operatorId=）"))

    dk._http_json = original  # 还原
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


def test_dingtalk_sync():
    """pytest 入口。"""
    assert not [msg for ok, msg in run_checks() if not ok]


if __name__ == "__main__":
    sys.exit(main())
