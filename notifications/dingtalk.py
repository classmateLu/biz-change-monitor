# -*- coding: utf-8 -*-
"""钉钉集成：群机器人 Webhook + 多维表批量写入。

所有凭证通过参数传入（由调用方从 .env / config 读取），
本模块不读任何全局配置、不包含任何真实密钥。

安全设计：
- URL 白名单：API 仅允许 api.dingtalk.com，Webhook 仅允许 oapi.dingtalk.com，
  且必须 HTTPS——拒绝本地/内网/任意第三方目标
- _http_json 永不抛出网络/解析异常，六类结果统一分类：
  ok / policy_error / http_error / business_error / network_error / invalid_json
- 日志脱敏：错误信息只含类别 + HTTP 状态码，不读取也不记录响应正文、
  不输出完整 URL（token/operatorId 片段）；成功日志只记响应键名
- 写入成功判定严格化（fail-safe）：HTTP 200 + 合法 JSON + 无错误字段
  + 响应含已知数据键之一；未知结构默认按失败处理（宁可拒绝重试，不静默丢数据）
- 依赖：仅 Python 标准库（urllib）
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

_API_HOSTS = {"api.dingtalk.com"}
_WEBHOOK_HOSTS = {"oapi.dingtalk.com"}
# 已知的业务错误字段（覆盖钉钉新旧两代 API 的常见形态）
_BUSINESS_ERROR_KEYS = ("code", "errcode", "error_code")
# 写入接口成功响应的候选数据键。
# ⚠️ 首次真实写入若提示"未知响应结构"，请按官方文档/实际响应更新此元组。
_WRITE_SUCCESS_KEYS = ("records", "id", "ids", "success")


def _url_allowed(url: str, allowed_hosts: set[str]) -> tuple[bool, str]:
    """目标地址白名单校验：仅 HTTPS + 主机在白名单内。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False, "URL 无法解析"
    if parts.scheme != "https":
        return False, f"仅允许 HTTPS（实际 scheme: {parts.scheme or '无'}）"
    host = (parts.hostname or "").lower()
    if host not in allowed_hosts:
        return False, f"目标主机不在白名单内（仅允许 {sorted(allowed_hosts)}）"
    return True, ""


def _http_json(url: str, payload: dict | None = None, token: str | None = None,
               timeout: int = 60) -> dict:
    """发起 HTTP 请求，返回分类结果（永不抛异常）。

    返回结构：
        {"kind": "ok"|"policy_error"|"http_error"|"network_error"|"invalid_json",
         "data": dict | None,      # 仅 kind=ok 时为解析后的 JSON dict
         "http_status": int | None,
         "detail": str}            # 已脱敏：不含 URL、不含响应正文
    """
    allowed, why = _url_allowed(url, _API_HOSTS | _WEBHOOK_HOSTS)
    if not allowed:
        return {"kind": "policy_error", "data": None, "http_status": None,
                "detail": why}
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
        # 不读取/记录错误响应正文——正文可能含敏感信息；仅保留状态码
        return {"kind": "http_error", "data": None, "http_status": e.code,
                "detail": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # 覆盖：DNS 失败、连接拒绝、超时、SSL 错误等
        return {"kind": "network_error", "data": None, "http_status": None,
                "detail": f"网络异常: {type(e).__name__}"}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"kind": "invalid_json", "data": None, "http_status": status,
                "detail": "响应不是合法 JSON"}
    if not isinstance(parsed, dict):
        return {"kind": "invalid_json", "data": None, "http_status": status,
                "detail": "响应 JSON 顶层不是对象"}
    return {"kind": "ok", "data": parsed, "http_status": status, "detail": ""}


def classify_business(data: dict) -> tuple[bool, str]:
    """业务层错误字段检查（HTTP 200 + 合法 JSON 的前提下调用）。

    宽松规则：响应含 code/errcode/error_code 且值非零 → 失败；
    含 error 字段且非空 → 失败；其余视为成功。
    message/errmsg 截断至 100 字符，防止把潜在敏感内容整段带进日志。
    """
    for key in _BUSINESS_ERROR_KEYS:
        v = data.get(key)
        if v not in (None, 0, "0", ""):
            msg = (data.get("message") or data.get("errmsg") or "")[:100]
            return False, f"业务错误 {key}={v} {msg}".strip()
    if data.get("error"):
        return False, f"业务错误 error={str(data.get('error'))[:100]}"
    return True, ""


def validate_write_response(data: dict) -> tuple[bool, str]:
    """写入接口的严格成功判定（fail-safe）。

    规则：无业务错误字段 且 响应非空 且 含已知数据键之一，且**值有效**：
        success       → 必须为真值（False/0/"0"/"false"/空 均为失败）
        records/ids   → 必须为非空列表
        id            → 必须非空
    未知响应结构 → 默认失败，并打印响应键名供人工核对（不打印值）。
    """
    biz_ok, why = classify_business(data)
    if not biz_ok:
        return False, why
    if not data:
        return False, "空响应，无法确认写入结果"
    if "success" in data:
        s = data["success"]
        if s is False or s in (0, "0", "", None) or (
                isinstance(s, str) and s.strip().lower() in ("false", "0", "no")):
            return False, f"success 字段指示失败: {s!r}"
        return True, ""
    for key in ("records", "ids"):
        v = data.get(key)
        if v is not None:
            if not v:
                return False, f"响应 {key} 为空，无法确认写入结果"
            return True, ""
    if "id" in data:
        v = data["id"]
        if v in (None, "", []):
            return False, "响应 id 为空，无法确认写入结果"
        return True, ""
    return False, (f"未知响应结构（键: {sorted(data.keys())}），按失败处理——"
                   f"请人工核对官方文档后更新 _WRITE_SUCCESS_KEYS")


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
    """群自定义机器人 Webhook 播报（text 消息）。

    目标仅允许 https://oapi.dingtalk.com/...，其他地址一律拒绝（防 SSRF/误配）。
    返回分类结果，调用方可检查。
    """
    allowed, why = _url_allowed(webhook_url, _WEBHOOK_HOSTS)
    if not allowed:
        return {"kind": "policy_error", "data": None, "http_status": None,
                "detail": f"Webhook 地址被拒绝: {why}"}
    content = f"{keyword} {text}" if keyword else text
    return _http_json(webhook_url, {"msgtype": "text", "text": {"content": content}})


def write_records(base_node_id: str, sheet_id: str, operator_unionid: str,
                  access_token: str, rows: list[dict],
                  batch_size: int = 50, batch_interval: float = 0.5,
                  on_error=None) -> tuple[int, int]:
    """批量写入多维表记录。rows = [{"fields": {...}}, ...]

    - 分批写入 + 批间隔，防限流（钉钉约 100 次/分钟）
    - 每批独立做严格成功判定（validate_write_response），未知结构按失败处理
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
            biz_ok, why = validate_write_response(resp["data"])
        else:
            biz_ok, why = False, f"{resp['kind']}: {resp['detail']}"
        if biz_ok:
            ok += len(batch)
            print(f"批次 {batch_no}/{total_batches}: 成功"
                  f"（响应键: {sorted(resp['data'].keys())}）", flush=True)
        else:
            fail += len(batch)
            print(f"批次 {batch_no}/{total_batches}: 失败 — {why}", flush=True)
            if on_error:
                on_error(f"批量写入失败: {why}")
        time.sleep(batch_interval)
    print(f"写入完成: 成功{ok} 失败{fail}", flush=True)
    return ok, fail
