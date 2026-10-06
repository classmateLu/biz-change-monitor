# -*- coding: utf-8 -*-
"""平台数据源抽象。

接入一个新平台 = 实现一个 DataSource 子类，把平台原始数据
转换成 core.models 的归一化结构。核心引擎不需要知道数据从哪来。

⚠️ 本仓库只带 example_adapter（虚构数据）。
平台专属适配器若涉及公司内部系统/商业平台接口，请保持在私有环境，
不要提交到公共仓库（去资产化边界）。
"""
from __future__ import annotations

from typing import Optional


class CollectionResult:
    """一次采集的结果。

    items           归一化 item 列表（见 core.models.make_item）
    stores          门店维度 {gid: [门店名, ...]}，未采集则为 None
    total           平台报告的总数（用于完整性校验；可缺省）
    failed_pages    分页失败的页码列表（非空 → 本次采集不完整）
    detail_total / detail_failed   逐对象详情查询的 总数/失败数
    """

    def __init__(self, items: list[dict], stores: Optional[dict] = None,
                 total: Optional[int] = None, failed_pages: Optional[list] = None,
                 detail_total: int = 0, detail_failed: int = 0):
        self.items = items
        self.stores = stores
        self.total = total
        self.failed_pages = failed_pages or []
        self.detail_total = detail_total
        self.detail_failed = detail_failed


class DataSource:
    """数据源接口。子类实现 fetch()。

    is_example：示例/虚构数据源必须设为 True——主流程在写入模式下
    会拒绝让示例数据进入真实钉钉表（防误写保护）。
    """

    name = "base"
    is_example = False

    def fetch(self) -> CollectionResult:
        raise NotImplementedError("adapter 必须实现 fetch()")
