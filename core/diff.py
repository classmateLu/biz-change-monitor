# -*- coding: utf-8 -*-
"""通用变化检测引擎（纯函数）。v2.1：字段级事件 + 内容幂等 event_id。

不依赖浏览器、网络、钉钉或任何平台 SDK —— 可独立测试、独立运行、稳定复现。

输入：两份归一化快照索引 {gid: item}（由 core.models.index_by_id 生成），
     以及可选的门店维度 {gid: [门店名, ...]}。

输出：变化事件列表。每条事件同时携带新（v2）与旧（v1 兼容）两套键：
    event_id            内容幂等键（语义见下）——同键重复落盘在存储层去重
    run_id / observed_at / previous_observed_at    时间语义（v2）
    field / event_type  字段名 + 事件类型（v2）
    type                与 event_type 同值（v1 兼容键）
    gid / name / old_value / new_value / note / date   （v1 兼容键）

event_id 语义（合并前终审 §1.3 修订：**内容键，不含 run_id**；
合并前终检 §1 修订：哈希输入为**规范 JSON 数组**，字段边界无歧义，
值含分隔符也不会产生切分碰撞）：
    event_id = sha256(规范JSON([gid, field, event_type,
                                previous_observed_at, 稳定旧值, 稳定新值]))
    - 「同一运行重放」→ 同键 → 落盘去重（无重复行）
    - 「崩溃恢复后不同 run 对**同一基线对**的再观测」→ 同键 → 落盘去重（无重复行）
    - 「不同基线对之间再次发生的真实变化」→ previous_observed_at 不同
      → 各自成事件（绝不因去重而漏报）
    run_id 仍随事件持久化（溯源哪一轮首次发现），但不参与幂等键。

event_type 全集：
    new | removed | name_changed | price_changed | count_changed
    | unit_price_changed | discount_changed | status_changed
    | store_count_changed | stores_changed
    | {field}_missing   字段键意外消失告警（field ∈ 监控字段，语义见下）

缺失语义三分（合并前终审 §3）：
    - 新快照**键不存在**且旧快照该字段有值 → {field}_missing 结构告警
      （疑似数据源/适配器结构变化，绝不静默跳过）
    - 键存在但值为 None/空串 → 缺失值 ≠ 变化，跳过比对（既有规则不变）
    - 必需字段（gid/name/price）键缺失由 check_required_fields 闸门在更早处
      fail-closed 拒绝（main 流程中到不了 diff）
    - 旧缺键、新有值 → 字段出现，非业务变化，零事件（不虚构变化方向）

防误报设计（与需求一一对应）：
- 数值字段经 canon_number 按字段级精度规范比对——"10.0"/10/10.00 不产生无意义变更
- 门店集合比较与顺序无关；**同数量的一换一（B→C）也能检出**
- 同一对象多字段同时变化 → 多条独立事件（均带 gid+field，可追溯）
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from .models import canon_number, to_number

# 数值型字段 → 事件类型（比对统一走 canon_number 定点比对）
_NUMERIC_FIELDS = (
    ("price", "price_changed"),
    ("count", "count_changed"),
    ("unit_price", "unit_price_changed"),
    ("discount", "discount_changed"),
    ("store_count", "store_count_changed"),
)
# 文本型字段 → 事件类型（双方均非 None/空串才比对）
_TEXT_FIELDS = (
    ("name", "name_changed"),
    ("status", "status_changed"),
)


def _num_repr(d: Decimal) -> str:
    """数值的**精确**十进制表示（无上下文舍入）。

    format(d, "f") 按值精确展开（绝不应用 context precision，也不产生
    科学计数法），随后仅做**语法层面**的末尾零剥离——两个不同数值不可能
    得到同一字符串（Decimal(str(x)) 构造本身也是精确的，全程无算术运算，
    因此不受默认上下文 28 位有效数字限制）。
    """
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("", "-0"):
        s = "0"
    return s


class UnsupportedHashValueError(TypeError):
    """_hash_val 收到不支持类型时抛出（fail-closed，终审四轮 §1-4）。

    旧实现曾用 str() 兜底——任意对象静默转字符串，存在与原生类型
    碰撞的可能且掩盖数据结构异常。现在：不支持类型显式失败，
    把适配器/数据源的结构问题在最早期暴露，而不是静默生成有碰撞
    风险的键。
    """


def _hash_val(v) -> str:
    """幂等键用的稳定序列化——**支持类型规格**（终审四轮 §1 定稿）。

    【支持的类型与规范化规则】
    - None   → "null"（裸字面量；字符串 "null" → '"null"'，不同键）
    - bool   → "true"/"false"（裸字面量；先于数值判断，True ≠ 1）
    - int/float/Decimal → 精确十进制展开 + 末尾零剥离（裸数字字面量）：
        * int：任意大小，str() 往返无损，精确
        * float：str() 为最短往返表示（repr）；同值 float 必同键；
          0.0/-0.0 归 "0"；精度边界 53 位——与 int 按**值**等价
          （2**53 == float(2**53) 同键；2**53+1 与 float(2**53+1)
          不同键——后者实际等于 2**53，值确实不同，键不同是正确行为）
        * Decimal：str() 往返精确，任意精度；科学计数法与展开表示
          同键（Decimal("1E+1") ≡ Decimal("10") → "10"）
        * NaN/Infinity：归一为 "NaN"/"Infinity"（确定性成立，但语义上
          与任何数值不等价；业务字段不会产生——canon_number 对 NaN
          量化失败 → None → 走缺失语义）
        * 已知边界（如实声明）：format(d, "f") 对极端指数（|adjusted
          exponent| > ~10^5）产生线性长度字符串，消耗内存/时间——
          当前业务字段（价格/份数/折扣）不会出现，未设防御上限
    - str     → JSON 字符串字面量（带引号转义，永不与裸字面量混淆）
    - list/tuple/set/frozenset → **等价容器**（终审四轮 §1-3 决策）：同一
      编码"[" + 各元素键排序拼接 + "]"——四者同元素同键、顺序无关；
      **多重性保留**（[1,1] ≠ [1] ≠ {1} 的去重结果不同键）
    - dict    → 键按原类型递归规范后排序，值递归规范（键序无关、
      键类型敏感：{1: x} ≠ {"1": x}）；键也必须是受支持类型
    - 其他任何类型 → 抛 UnsupportedHashValueError（不用 str() 兜底）

    【无歧义论证】每个叶子要么是带引号转义的 JSON 字符串（自定界），
    要么是固定互斥字面量集 {null, true, false, 数字} 的裸 token——
    四类裸 token 两两不重叠、且都不是合法 JSON 字符串的开头，因此
    拼接/键值对结构不存在第二种解析方式。
    全部确定性：同一逻辑值跨运行、跨环境、跨表示得到同一字符串。
    """
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float, Decimal)):
        return _num_repr(Decimal(str(v)))
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple, set, frozenset)):
        return "[" + ",".join(sorted(_hash_val(e) for e in v)) + "]"
    if isinstance(v, dict):
        pairs = sorted((_hash_val(k), _hash_val(val)) for k, val in v.items())
        return "{" + ",".join(f"{k}:{h}" for k, h in pairs) + "}"
    raise UnsupportedHashValueError(
        f"_hash_val 不支持类型 {type(v).__name__!r}（fail-closed，"
        f"请先在适配器层转换为受支持类型：{v!r}）")


def _content_event_id(gid: str, field: str, event_type: str,
                      previous_observed_at, old_value, new_value) -> str:
    """内容幂等键（不含 run_id，语义见模块 docstring）。

    哈希输入为**规范 JSON 数组**（合并前终检 §1 修订）：字段边界由 JSON
    结构保证无歧义——值中含分隔符（如 "|"）也不会产生不同 (old, new)
    对被切分成同一输入的碰撞。
    """
    raw = json.dumps([gid, field, event_type, str(previous_observed_at or ""),
                      _hash_val(old_value), _hash_val(new_value)],
                     ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _set_id(ev: dict, previous_observed_at, old_hash, new_hash) -> None:
    """事件构建收尾：按内容键回填 event_id。"""
    ev["event_id"] = _content_event_id(ev["gid"], ev["field"], ev["event_type"],
                                       previous_observed_at, old_hash, new_hash)


def _base_event(gid: str, name, field: str, event_type: str,
                run_id: str, observed_at: str, previous_observed_at,
                date: str) -> dict:
    return {
        "run_id": run_id,
        "gid": gid,
        "field": field,
        "event_type": event_type,
        "type": event_type,  # v1 兼容键
        "name": name,
        "observed_at": observed_at,
        "previous_observed_at": previous_observed_at,
        "date": date,
    }


def _is_missing(value) -> bool:
    """缺失语义：None 或空串。零值不算缺失。"""
    return value is None or (isinstance(value, str) and value.strip() == "")


def diff_changes(old_items: dict, new_items: dict,
                 old_stores: dict | None = None, new_stores: dict | None = None,
                 date: str = "", run_id: str = "", observed_at: str = "",
                 previous_observed_at=None) -> list[dict]:
    """比较两份完整快照，返回变化事件列表（纯函数）。

    前提（由调用方保证）：两份快照均通过完整性校验（complete=true）。
    不完整快照不得进入本函数——删除判定依赖此前提。
    """
    changes: list[dict] = []

    # ① removed：旧有今无（前提：本轮采集 complete=true）
    for gid, it in old_items.items():
        if gid not in new_items:
            old_price = to_number(it.get("price"))
            ev = _base_event(gid, it.get("name"), "lifecycle", "removed",
                             run_id, observed_at, previous_observed_at, date)
            ev.update({"old_value": old_price, "new_value": "",
                       "note": "对象已不在最新采集结果中"})
            _set_id(ev, previous_observed_at, old_price, "")
            changes.append(ev)

    # ② new + 字段级比对
    for gid, it in new_items.items():
        name = it.get("name")
        if gid not in old_items:
            new_price = to_number(it.get("price"))
            ev = _base_event(gid, name, "lifecycle", "new",
                             run_id, observed_at, previous_observed_at, date)
            ev.update({"old_value": "", "new_value": new_price,
                       "note": "新出现的对象"})
            _set_id(ev, previous_observed_at, "", new_price)
            changes.append(ev)
            continue

        oit = old_items[gid]

        # 数值字段：canon 按字段级精度比对；键消失 → _missing 结构告警
        for field, event_type in _NUMERIC_FIELDS:
            old_v, new_v = oit.get(field), it.get(field)
            old_c = canon_number(old_v, field=field)
            if field not in it:
                # 新快照键消失：旧值有意义 → 告警；旧值本就缺失 → 无信息量，跳过
                if field in oit and old_c is not None:
                    ev = _base_event(gid, name, field, f"{field}_missing",
                                     run_id, observed_at, previous_observed_at, date)
                    ev.update({"old_value": old_v, "new_value": None,
                               "note": f"{field} 字段键意外消失"
                                       f"（疑似数据源/适配器结构变化）"})
                    _set_id(ev, previous_observed_at, old_c, None)
                    changes.append(ev)
                continue
            new_c = canon_number(new_v, field=field)
            if old_c is None or new_c is None:
                continue  # 任一侧值为缺失（None/空串）→ 不比对（缺失值 ≠ 变化）
            if old_c != new_c:
                ev = _base_event(gid, name, field, event_type,
                                 run_id, observed_at, previous_observed_at, date)
                ev.update({"old_value": old_v, "new_value": new_v,
                           "note": f"{field} {old_c} → {new_c}"})
                _set_id(ev, previous_observed_at, old_c, new_c)
                changes.append(ev)

        # 文本字段：键消失 → _missing 结构告警；双方均非缺失才比对
        for field, event_type in _TEXT_FIELDS:
            old_v, new_v = oit.get(field), it.get(field)
            if field not in it:
                if field in oit and not _is_missing(old_v):
                    ev = _base_event(gid, name, field, f"{field}_missing",
                                     run_id, observed_at, previous_observed_at, date)
                    ev.update({"old_value": old_v, "new_value": None,
                               "note": f"{field} 字段键意外消失"
                                       f"（疑似数据源/适配器结构变化）"})
                    _set_id(ev, previous_observed_at, old_v, None)
                    changes.append(ev)
                continue
            if _is_missing(old_v) or _is_missing(new_v):
                continue
            if old_v != new_v:
                ev = _base_event(gid, name, field, event_type,
                                 run_id, observed_at, previous_observed_at, date)
                ev.update({"old_value": old_v, "new_value": new_v,
                           "note": f"{field}变更：{old_v} → {new_v}"})
                _set_id(ev, previous_observed_at, old_v, new_v)
                changes.append(ev)

        # 门店集合：仅当双方都提供了门店维度才比较（None=未采集=未知，跳过）
        if old_stores is not None and new_stores is not None:
            old_s = set(old_stores.get(gid) or [])
            new_s = set(new_stores.get(gid) or [])
            if old_s != new_s:
                added = "、".join(sorted(new_s - old_s)) or "无"
                removed = "、".join(sorted(old_s - new_s)) or "无"
                ev = _base_event(gid, name, "stores", "stores_changed",
                                 run_id, observed_at, previous_observed_at, date)
                ev.update({"old_value": sorted(old_s), "new_value": sorted(new_s),
                           "note": f"新增门店：{added}；减少门店：{removed}"[:900]})
                _set_id(ev, previous_observed_at, sorted(old_s), sorted(new_s))
                changes.append(ev)

    return changes


def summarize(changes: list[dict]) -> dict:
    """按类型计数，用于播报摘要。"""
    counts: dict[str, int] = {}
    for c in changes:
        counts[c["type"]] = counts.get(c["type"], 0) + 1
    return counts
