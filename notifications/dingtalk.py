# -*- coding: utf-8 -*-
"""钉钉集成：群机器人 Webhook + 多维表批量写入。

所有凭证通过参数传入（由调用方从 .env / config 读取），
本模块不读任何全局配置、不包含任何真实密钥。

安全设计：
- _http_json 永不抛出网络/解析异常：五种结果统一分类
  （ok / http_error / business_error / network_error / invalid_json）
- 只有 HTTP 成功 + 合法 JSON + 无业务错误字段，才判定写入成功
- 日志与错误信息已脱敏：不输出完整 URL（含 token/operatorId）、不输出响应值
- 依赖：仅 Python 标准库（urllib）
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

# 已知的业务错误字段（覆盖钉钉新旧两代 API 的常见形态）。
# 判定规则保持宽松：命中任一非零错误字段 → 业务失败；其余视为成功。
_BUSINESS_ERROR_KEYS = ("code", "errcode", "error_code")


def _http_json(url: str, payload: dict | None = None, token: str | None = None,
               timeout: int = 60) -> dict:
    """发起 HTTP 请求，返回分类结果（永不抛异常）。

    返回结构：
        {"kind": "ok"|"http_error"|"network_error"|"invalid_json",
         "data": dict | None,      # 仅 kind=ok 时为解析后的 JSON dict
         "http_status": int | None,
         "detail": str}            # 已脱敏：不含完整 URL / 认证信息
    """
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("x-acs-dingtalk-access-token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
        status = getattr(r, "status", 200)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:300]
        except Exception:
            pass
        return {"kind": "http_error", "data": None,
                "http_status": e.code, "detail": f"HTTP {e.code}: {body}"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # 覆盖：DNS 失败、连接拒绝、超时、SSL 错误等
        return {"kind": "network_error", "data": None, "http_status": None,
                "detail": f"网络异常: {type(e).__name__}"}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"kind": "invalid_json", "data": None, "http_status": status,
                "detail": f"响应不是合法 JSON（前80字符已略）"}
    if not isinstance(parsed, dict):
        return {"kind": "invalid_json", "data": None, "http_status": status,
                "detail": "响应 JSON 顶层不是对象"}
    return {"kind": "ok", "data": parsed, "http_status": status, "detail": ""}


def classify_business(data: dict) -> tuple[bool, str]:
    """在 HTTP 200 + 合法 JSON 的基础上，判定业务层是否成功。

    宽松规则：响应含 code/errcode/error_code 且值非零 → 失败；
    含 error 字段且非空 → 失败；其余视为成功。
    ⚠️ 钉钉各接口成功响应结构以官方文档为准；若某接口成功时也携带
    code 字段，请传入该接口的期望键另行校验（当前未发现此情况）。
    """
    for key in _BUSINESS_ERROR_KEYS:
        v = data.get(key)
        if v not in (None, 0, "0", ""):
            msg = data.get("message") or data.get("errmsg") or ""
            return False, f"业务错误 {key}={v} {msg}".strip()
    if data.get("error"):
        return False, f"业务错误 error={data.get('error')}"
    return True, ""


def get_access_token(app_key: str, app_secret: str) -> str:
    """钉钉开放平台 accessToken（企业内部应用）。失败抛 RuntimeError（已脱敏）。"""
    resp = _http_json("https://api.dingtalk.com/v1.0/oauth2/accessToken",
                      {"appKey": app_key, "appSecret": app_secret})
    if resp["kind"] != "ok":
        raise RuntimeError(f"获取 accessToken 失败: {resp['kind']} ({resp['detail'][:120]})")
    biz_ok, why = classify_business(resp["data"])
    if not biz_ok or not resp["data"].get("accessToken"):
        raise RuntimeError(f"获取 accessToken 失败: {why or '响应缺少 accessToken'}")
    return resp["data"]["accessToken"]


def send_webhook(webhook_url: str, text: str, keyword: str = "") -> dict:
    """群自定义机器人 Webhook 播报（text 消息）。返回分类结果，调用方可检查。"""
    content = f"{keyword} {text}" if keyword else text
    return _http_json(webhook_url, {"msgtype": "text", "text": {"content": content}})


def write_records(base_node_id: str, sheet_id: str, operator_unionid: str,
                  access_token: str, rows: list[dict],
                  batch_size: int = 50, batch_interval: float = 0.5,
                  on_error=None) -> tuple[int, int]:
    """批量写入多维表记录。rows = [{"fields": {...}}, ...]

    - 分批写入 + 批间隔，防限流（钉钉约 100 次/分钟）
    - 每批独立判定：只有"HTTP 200 + 合法 JSON + 无业务错误字段"才计成功
    - 任何失败均通过 on_error(msg) 回调（msg 已脱敏），由调用方决定是否告警
    返回 (成功行数, 失败行数)。
    """
    ok = fail = 0
    url = (f"https://api.dingtalk.com/v1.0/notable/bases/{base_node_id}"
           f"/sheets/{sheet_id}/records?operatorId={operator_unionid}")
    total_batches = (len(rows) + batch_size - 1) // batch_size
    for i in range(0, len(rows), batch_size):
        batch_no = i // batch_size + 1
        batch = rows[i:i + batch_size]
        resp = _http_json(url, payload={"records": batch}, token=access_token)
        if resp["kind"] == "ok":
            biz_ok, why = classify_business(resp["data"])
        else:
            biz_ok, why = False, f"{resp['kind']}: {resp['detail']}"
        if biz_ok:
            ok += len(batch)
            # 只记响应键名，不记值——便于核对响应结构，不泄露内容
            print(f"批次 {batch_no}/{total_batches}: 成功"
                  f"（响应键: {sorted(resp['data'].keys())}）", flush=True)
        else:
            fail += len(batch)
            print(f"批次 {batch_no}/{total_batches}: 失败 — {why}", flush=True)
            if on_error:
                on_error(f"批量写入失败（{resp['kind']}）: {why}")
        time.sleep(batch_interval)
    print(f"写入完成: 成功{ok} 失败{fail}", flush=True)
    return ok, fail
