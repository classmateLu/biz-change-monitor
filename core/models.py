# -*- coding: utf-8 -*-
"""归一化数据模型。

所有平台适配器（adapter）必须把平台原始数据转换为本模块定义的结构，
核心引擎（diff/snapshot/validation）只认这里的数据形态，与任何平台解耦。

Item 字段：
    gid         业务对象的稳定唯一标识（字符串）——整个系统的关联键
    name        名称（可缺省；缺失时不参与名称比对，避免误报）
    price       价格（数值或可转数值的字符串；缺失时不参与价格比对）
    count       数量/份数（可缺省）
    unit_price  单价（可缺省）
    discount    折扣/优惠（可缺省；数值或可转数值的字符串）
    status      项目状态，如 上架/下架/暂停（可缺省；缺失时不参与状态比对）
    store_count 门店数量（可缺省；与 stores 集合独立，未采集详情时也可提供）
    extra       其他平台特有字段（原样携带，核心引擎不解释）

缺失语义约定（v2 + 合并前终审 §3 细化，与需求一一对应）：
    键不存在     → 字段未提供（此前有值时 diff 触发 _missing 结构告警）
    None / 空串  → 缺失值（缺失 ≠ 零值 ≠ 变化；make_item 不写 None 键）
    0            → 真实业务值"零"，参与比对
    数值比较统一走 canon_number（Decimal 定点），避免浮点近似比较

stores 维度：
    {gid: [门店名, ...]}   比对与顺序无关；None = 未采集（未知），[] = 确认无门店
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

ITEM_FIELDS = ("gid", "name", "price", "count", "unit_price",
               "discount", "status", "store_count")

# 必需字段（P0-2，终审补充）：意外缺失 = 数据读取异常 → 结构错误 → 闸门拒绝本轮。
# 可配置覆盖（config: validation.required_fields）；默认三字段为对账核心。
# 语义边界：
#   key 不存在 / None  → 结构错误（拒绝发布为完整状态）
#   空串 ""            → 合法空值（不报错）
#   0                  → 合法业务值（不报错）
REQUIRED_ITEM_FIELDS = ("gid", "name", "price")


def make_item(gid, name=None, price=None, count=None, unit_price=None,
              discount=None, status=None, store_count=None, **extra) -> dict:
    """构造一个归一化 item。gid 强制转字符串（平台侧可能是数字）。

    键存在性语义（合并前终审 §3）：未提供（None）的**可选字段不写入键**——
    键不存在 = 适配器未提供该字段（此前有值时可在 diff 触发 _missing 结构告警，
    用于感知数据源结构变化）；空串 = 显式合法空值。两种形态刻意区分，
    绝不用 None 键值掩蔽"字段消失"信号。
    """
    it: dict[str, Any] = {"gid": str(gid)}
    for key, val in (("name", name), ("price", price), ("count", count),
                     ("unit_price", unit_price), ("discount", discount),
                     ("status", status), ("store_count", store_count)):
        if val is not None:
            it[key] = val
    if extra:
        it["extra"] = extra
    return it


def index_by_id(items: Iterable[dict]) -> dict:
    """把 item 列表转成 {gid: item} 索引。

    重复 gid → 抛 ValueError（信息含重复 gid）：重复意味着数据源异常，
    绝不静默覆盖丢失数据（fail-closed）。
    """
    index: dict = {}
    for i in items:
        gid = str(i["gid"])
        if gid in index:
            raise ValueError(
                f"采集结果存在重复 gid: {gid}（疑似数据源异常，已拒绝建立索引）")
        index[gid] = i
    return index


def to_number(value):
    """容错数值化：'99.0' 与 99.0 等价；无法转换返回 None（缺失语义）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def check_required_fields(items: Iterable, required=None,
                          max_report: int = 10) -> list[str]:
    """必需字段结构校验（P0-2）：意外缺失 → 返回结构错误列表（fail-closed）。

    规则（与 docstring 缺失语义约定对应）：
        key 不存在 / None  → 结构错误
        空串 "" / 0 / 其他 → 合法，不报错
    错误最多报 max_report 条 + 总数摘要，避免海量重复刷屏。
    返回空列表 = 全部通过。
    """
    if required is None:
        required = REQUIRED_ITEM_FIELDS
    errors: list[str] = []
    total = 0
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            errors.append(f"第{i}条不是字典结构")
            total += 1
            continue
        for f in required:
            if f not in it or it[f] is None:
                total += 1
                if len(errors) < max_report:
                    errors.append(f"第{i}条(gid={it.get('gid')})必需字段缺失: {f}")
    if total > len(errors):
        errors.append(f"……共 {total} 处必需字段缺失（仅展示前 {max_report} 条）")
    return errors


# 字段级数值精度（P1-1，终审补充）：不同字段按业务语义选量化档位，
# 避免统一 0.01 对折扣（12.5% 级精度）过度舍入、对数量（整数语义）无谓定点。
#   price/unit_price  货币 → 0.01（分）
#   discount          折扣/优惠 → 0.001（支持 12.5% 一类表示）
#   count/store_count 数量 → None = 不量化（Decimal 原值数值比较，
#                     "2"/2/2.0 自动等价，且 2.5→2.6 这类真实变化不漏报）
#   未列出的字段      默认 0.01（向后兼容 v2 初版行为）
FIELD_QUANTUM = {
    "price": "0.01",
    "unit_price": "0.01",
    "discount": "0.001",
    "count": None,
    "store_count": None,
}


def canon_number(value, field=None):
    """规范数值（v2）：转 Decimal 并按**字段级精度**定点，用于**比对**。

    - field 决定量化档位（见 FIELD_QUANTUM）；未传/未知字段默认 0.01
    - '10.0' / 10 / 10.00 → 相等，杜绝形态差异误报
    - None / 空串 / 无法解析 → None（缺失语义，调用方跳过比对）
    - 0 是真实业务值，与 None 严格区分
    - 事件里的 old_value/new_value 仍存原始值（人类可读），比对只用本函数结果
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    quantum = FIELD_QUANTUM.get(field, "0.01")
    try:
        d = Decimal(str(value))
        if quantum is not None:
            d = d.quantize(Decimal(quantum))
        return d
    except (InvalidOperation, ValueError, TypeError):
        return None
