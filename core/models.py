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
    extra       其他平台特有字段（原样携带，核心引擎不解释）

stores 维度：
    {gid: [门店名, ...]}   比对与顺序无关
"""
from __future__ import annotations

from typing import Any, Iterable

ITEM_FIELDS = ("gid", "name", "price", "count", "unit_price")


def make_item(gid, name=None, price=None, count=None, unit_price=None, **extra) -> dict:
    """构造一个归一化 item。gid 强制转字符串（平台侧可能是数字）。"""
    it: dict[str, Any] = {
        "gid": str(gid),
        "name": name,
        "price": price,
        "count": count,
        "unit_price": unit_price,
    }
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
