#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终审二轮 §1 专项：_hash_val 哈希层单元测试（不经 diff_changes）。

背景：normalize() 应用 context precision（默认 28 位有效数字），≥29 位
输入会被舍入——两个不同数值可能得到相同表示。修复后归一化改为
"精确十进制展开 + 语法层末尾零剥离"（无算术运算，不受上下文限制）。

覆盖（按 GPT 要求逐项）：
    H1 int / float / Decimal 同值归一（11 / 11.0 / Decimal("11.00")）
    H2 0 与 -0.0 同值同键；零值与 None/字符串严格不同键
    H3 不同数值不得因归一化而意外相同
    H4 高精度 Decimal（29-31 位有效数字）：精确、无舍入碰撞
    H5 数值表示漂移：int/float/Decimal 跨表示同键（含 10**30 量级）
    H6 容器：list/tuple/set 顺序无关；dict 键序无关
    H7 嵌套数值规范：容器内 int/float/Decimal 同值同键
    H8 字符串与数值严格不同键（"11" ≠ 11）
    H9 嵌套结构歧义防护：含分隔符/引号的字符串元素不改变边界

既可用 pytest 运行，也可直接执行：python tests/test_hash_val.py
全部虚构数据，无 IO、无网络。
"""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.diff import _hash_val  # noqa: E402


def run_checks():
    checks = []

    # ---------- H1: int / float / Decimal 同值归一 ----------
    checks.append((_hash_val(11) == _hash_val(11.0) == _hash_val(Decimal("11.00")),
                   "H1 11 / 11.0 / Decimal('11.00') 同值同键"))
    checks.append((_hash_val(10) == _hash_val(10.0) == _hash_val(Decimal("1E+1")),
                   "H1 10 / 10.0 / Decimal('1E+1') 同值同键（科学计数法表示归一）"))
    checks.append((_hash_val(Decimal("0.100")) == _hash_val(0.1) == _hash_val(Decimal("0.1")),
                   "H1 0.100 / 0.1 / Decimal('0.1') 同值同键"))

    # ---------- H2: 零值归一 + 严格区分 ----------
    checks.append((_hash_val(0) == _hash_val(0.0) == _hash_val(-0.0)
                   == _hash_val(Decimal("-0.00")) == _hash_val(Decimal("0E+10")),
                   "H2 0 / 0.0 / -0.0 / Decimal('-0.00') / 0E+10 同值同键"))
    checks.append((_hash_val(0) != _hash_val(None) and _hash_val(0) != _hash_val("0"),
                   "H2 数值 0 与 None、字符串 '0' 不同键"))

    # ---------- H3: 不同数值不得意外相同 ----------
    checks.append((_hash_val(11) != _hash_val(12), "H3 11 ≠ 12"))
    checks.append((_hash_val(Decimal("0.1")) != _hash_val(Decimal("0.2")),
                   "H3 0.1 ≠ 0.2"))
    checks.append((_hash_val(10**30) != _hash_val(10**30 + 1),
                   "H3 10^30 ≠ 10^30+1（大整数相邻值不同键）"))

    # ---------- H4: 高精度 Decimal（29-31 位有效数字，normalize 会舍入的区间） ----------
    hp_a = Decimal("1.234567890123456789012345678901")   # 31 位有效数字
    hp_b = Decimal("1.234567890123456789012345678902")   # 仅末位不同
    checks.append((_hash_val(hp_a) != _hash_val(hp_b),
                   "H4 31 位有效数字、仅末位不同 → 不同键（normalize 下会同键）"))
    checks.append((_hash_val(hp_a) == _hash_val(Decimal("1.2345678901234567890123456789010")),
                   "H4 31 位数值与其补零表示同值同键"))
    hp_int_a = Decimal("123456789012345678901234567890")   # 30 位整数
    hp_int_b = Decimal("123456789012345678901234567891")
    checks.append((_hash_val(hp_int_a) != _hash_val(hp_int_b),
                   "H4 30 位整数相邻值 → 不同键"))
    checks.append((_hash_val(hp_int_a) != _hash_val(10**30 - 1),
                   "H4 30 位整数与其相邻 int 不同键"))

    # ---------- H5: 跨表示漂移（大数） ----------
    big = 10**30
    checks.append((_hash_val(big) == _hash_val(float(big)) == _hash_val(Decimal(big)),
                   "H5 10^30 的 int / float / Decimal 三表示同键"))
    checks.append((_hash_val(-big) != _hash_val(big), "H5 大数负值 ≠ 正值"))

    # ---------- H6: 容器顺序无关 ----------
    checks.append((_hash_val([1, 2, 3]) == _hash_val([3, 2, 1])
                   == _hash_val((1, 2, 3)) == _hash_val({3, 1, 2}),
                   "H6 list/tuple/set 同元素同键（顺序无关）"))
    checks.append((_hash_val({"a": 1, "b": 2}) == _hash_val({"b": 2, "a": 1}),
                   "H6 dict 键序无关"))
    checks.append((_hash_val([1, 2]) != _hash_val([2, 1, 1]),
                   "H6 元素多重性保留（[1,2] ≠ [2,1,1]）"))
    checks.append((_hash_val([]) != _hash_val([0]), "H6 空容器 ≠ 含零元素容器"))

    # ---------- H7: 嵌套数值规范 ----------
    checks.append((_hash_val([11.0, {"k": Decimal("11.00")}])
                   == _hash_val([11, {"k": 11}]),
                   "H7 嵌套 int/float/Decimal 同值同键"))
    checks.append((_hash_val([0.0]) == _hash_val([-0.0]) == _hash_val([0]),
                   "H7 嵌套零值归一（[0.0] / [-0.0] / [0] 同键）"))
    checks.append((_hash_val({"a": [1.5], "b": Decimal("2.50")})
                   == _hash_val({"b": 2.5, "a": [Decimal("1.500")]}),
                   "H7 混合嵌套（dict+list+数值表示漂移）同键"))

    # ---------- H8: 字符串与数值严格不同键 ----------
    checks.append((_hash_val("11") != _hash_val(11), "H8 '11'（字符串）≠ 11（数值）"))
    checks.append((_hash_val("") != _hash_val(None), "H8 空串 ≠ None"))
    checks.append((_hash_val("") != _hash_val([]), "H8 空串 ≠ 空列表"))

    # ---------- H9: 嵌套结构歧义防护 ----------
    # 字符串元素含分隔符/引号/括号时，不得与多元素结构混淆
    tricky = 'a","b'
    checks.append((_hash_val([tricky]) != _hash_val(["a", "b"]),
                   "H9 含分隔符的单字符串元素 ≠ 两元素列表"))
    checks.append((_hash_val([tricky]) == _hash_val([tricky]), "H9 tricky 字符串自洽"))
    checks.append((_hash_val({"{}": [1]}) == _hash_val({"{}": (1,)}),
                   "H9 键含花括号不影响键序规范"))

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


def test_hash_val():
    """pytest 入口。"""
    assert not [msg for ok, msg in run_checks() if not ok]


if __name__ == "__main__":
    sys.exit(main())
