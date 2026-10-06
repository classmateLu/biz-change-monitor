# -*- coding: utf-8 -*-
"""采集完整性校验。

设计原则：只有当本次采集被判定为"完整且有效"时，才允许进入 Diff 与同步。
校验失败 → 阻止错误变化进入历史（宁可停摆报警，不可写入错误数据）。

阈值全部可通过 config 调整，不硬编码在业务流程里。
"""
from __future__ import annotations


def validate_collection(item_count: int,
                        min_records: int = 50,
                        failed_pages: list | None = None,
                        detail_total: int = 0,
                        detail_failed: int = 0,
                        max_detail_failure_ratio: float = 0.05) -> tuple[bool, str]:
    """校验一次采集的完整性。

    参数：
        item_count                 本次采集的对象总数
        min_records                最小记录数阈值（低于即视为不完整）
        failed_pages               分页失败的页码列表（非空即不完整）
        detail_total/detail_failed 逐对象详情查询的 总数/失败数
        max_detail_failure_ratio   详情失败比例上限（超过即不完整）

    返回 (ok, reason)；reason 为空串表示通过。
    """
    reasons: list[str] = []
    if failed_pages:
        reasons.append(f"分页失败页: {','.join(map(str, failed_pages))}")
    if item_count < min_records:
        reasons.append(f"记录数 {item_count} 低于阈值 {min_records}")
    if detail_total:
        ratio = detail_failed / detail_total
        if ratio > max_detail_failure_ratio:
            reasons.append(f"详情失败比例 {ratio:.0%} 超过上限 {max_detail_failure_ratio:.0%}"
                           f"（{detail_failed}/{detail_total}）")
    return (not reasons, "; ".join(reasons))
