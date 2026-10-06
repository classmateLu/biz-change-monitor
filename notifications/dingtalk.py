# -*- coding: utf-8 -*-
"""钉钉集成：群机器人 Webhook + 多维表批量写入。

所有凭证通过参数传入（由调用方从 .env / config 读取），
本模块不读任何全局配置、不包含任何真实密钥。

依赖：仅 Python 标准库（urllib）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


def _http_json(url: str, payload: dict | None = None, token: str | None = None,
               timeout: int = 60) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("x-acs-dingtalk-access-token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode()[:200]}


def get_access_token(app_key: str, app_secret: str) -> str:
    """钉钉开放平台 accessToken（企业内部应用）。"""
    r = _http_json("https://api.dingtalk.com/v1.0/oauth2/accessToken",
                   {"appKey": app_key, "appSecret": app_secret})
    if r.get("_http_error") or "accessToken" not in r:
        raise RuntimeError(f"获取 accessToken 失败: {r}")
    return r["accessToken"]


def send_webhook(webhook_url: str, text: str, keyword: str = "") -> dict:
    """群自定义机器人 Webhook 播报（text 消息）。

    keyword：与机器人安全设置里的自定义关键词保持一致（可为空）。
    """
    content = f"{keyword} {text}" if keyword else text
    return _http_json(webhook_url, {"msgtype": "text", "text": {"content": content}})


def write_records(base_node_id: str, sheet_id: str, operator_unionid: str,
                  access_token: str, rows: list[dict],
                  batch_size: int = 50, batch_interval: float = 0.5,
                  on_error=None) -> tuple[int, int]:
    """批量写入多维表记录。rows = [{"fields": {...}}, ...]

    分批写入 + 批间隔，防限流（钉钉约 100 次/分钟）。
    返回 (成功行数, 失败行数)；on_error(msg) 可选回调（用于告警）。
    """
    ok = fail = 0
    url = (f"https://api.dingtalk.com/v1.0/notable/bases/{base_node_id}"
           f"/sheets/{sheet_id}/records?operatorId={operator_unionid}")
    for i in range(0, len(rows), batch_size):
        r = _http_json(url, payload={"records": rows[i:i + batch_size]}, token=access_token)
        if r.get("_http_error"):
            fail += len(rows[i:i + batch_size])
            msg = f"批次 {i // batch_size + 1} 写入失败: {r}"
            print(msg, flush=True)
            if on_error:
                on_error(msg)
        else:
            ok += len(rows[i:i + batch_size])
        time.sleep(batch_interval)
    print(f"写入: 成功{ok} 失败{fail}", flush=True)
    return ok, fail
