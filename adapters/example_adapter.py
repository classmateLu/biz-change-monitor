# -*- coding: utf-8 -*-
"""示例数据源：虚构数据，让任何人克隆仓库后无需任何账号即可跑通全链路。

数据完全虚构（DEMO-xxxx 对象 / Alpha~Echo 虚构门店），
可以用 modifiers 模拟一次真实运行会遇到的场景（新对象/下架/价格变化/门店变化）。
"""
from __future__ import annotations

import random

from .base import CollectionResult, DataSource
from core.models import make_item

STORE_NAMES = ["Alpha 店", "Beta 店", "Gamma 店", "Delta 店", "Echo 店"]


class ExampleAdapter(DataSource):
    """虚构数据源。每次实例化用固定种子，保证可复现。"""

    name = "example"
    is_example = True  # 示例适配器标记：写入模式下被主流程拒绝，防误写真实表

    def __init__(self, item_count: int = 60, seed: int = 42,
                 # 模拟"运行异常"的开关（默认全关，数据完整）
                 fail_pages: list | None = None, drop_ratio: float = 0.0):
        self.item_count = item_count
        self.rng = random.Random(seed)
        self.fail_pages = fail_pages or []
        self.drop_ratio = drop_ratio
        # 稳定的"昨日基线"：由同一 seed 生成，示例流程用它模拟历史
        self.base_items = self._generate()

    def _generate(self) -> list[dict]:
        items = []
        for i in range(1, self.item_count + 1):
            gid = f"DEMO-{i:04d}"
            price = round(self.rng.uniform(50, 300), 2)
            count = self.rng.choice([1, 1, 1, 2, 5])
            stores = sorted(self.rng.sample(STORE_NAMES, self.rng.randint(1, 4)))
            items.append(make_item(gid, name=f"示例项目 {i:04d}",
                                   price=price, count=count,
                                   unit_price=round(price / count, 2)))
        return items

    def fetch(self) -> CollectionResult:
        """返回"当前状态"。drop_ratio>0 时随机丢弃部分对象，模拟采集不完整。"""
        items = list(self.base_items)
        stores = {}
        for it in items:
            stores[it["gid"]] = sorted(self.rng.sample(
                STORE_NAMES, self.rng.randint(1, 4)))
        if self.drop_ratio > 0:
            keep = self.rng.sample(items, int(len(items) * (1 - self.drop_ratio)))
            items = keep
        return CollectionResult(items=items, stores=stores, total=self.item_count,
                                failed_pages=list(self.fail_pages))
