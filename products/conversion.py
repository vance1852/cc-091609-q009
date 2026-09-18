"""饮片剂量到颗粒剂量的纯函数换算。

换算链：饮片日剂量 ÷ 当量系数 = 颗粒精确日量；
按「分次服用」均分到每次；每次按「包装步长」四舍五入；
日调配量 = 每次调配量 × 每日次数；舍入差额 = 日调配量 − 精确日量。
所有中间量都保留在公式文本中，差额显式记录，绝不回改处方原量。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .contracts import DecoctionDose, GranuleStandard

#: 精确值保留 6 位小数，保证展示出来的每个数字都能手工复算。
EXACT_QUANTUM = Decimal("0.000001")
_ONE = Decimal("1")


def _quantize(value: Decimal, quantum: Decimal = EXACT_QUANTUM) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def round_to_step(amount: Decimal, step: Decimal) -> Decimal:
    """把重量按包装步长四舍五入（half-up）到最近的步长倍数。"""
    if step <= 0:
        raise ValueError("包装步长必须为正数")
    steps = (amount / step).quantize(_ONE, rounding=ROUND_HALF_UP)
    return steps * step


@dataclass(frozen=True)
class LineComputation:
    """一味药的自动换算结果（每个数字都可溯源）。"""

    exact_daily_grams: Decimal
    exact_per_portion_grams: Decimal
    dispensed_per_portion_grams: Decimal
    dispensed_daily_grams: Decimal
    rounding_delta_grams: Decimal
    exceeds_maximum_daily: bool
    formula_text: str


def compute_line(dose: DecoctionDose, standard: GranuleStandard) -> LineComputation:
    """按标准把一味饮片剂量换算为颗粒调配量。"""
    if dose.daily_decoction_grams <= 0:
        raise ValueError(f"{dose.line_id}: 饮片日剂量必须为正数")
    if dose.portions_per_day < 1:
        raise ValueError(f"{dose.line_id}: 每日服用次数必须 ≥ 1")
    if standard.extract_ratio <= 0:
        raise ValueError(f"标准 {standard.standard_id}: 当量系数必须为正数")

    exact_daily = _quantize(dose.daily_decoction_grams / standard.extract_ratio)
    exact_per_portion = _quantize(exact_daily / dose.portions_per_day)
    dispensed_per_portion = round_to_step(
        exact_per_portion, standard.package_step_grams
    )
    dispensed_daily = _quantize(dispensed_per_portion * dose.portions_per_day)
    delta = _quantize(dispensed_daily - exact_daily)
    exceeds = dispensed_daily > standard.maximum_daily_grams

    formula = (
        f"{dose.source_name} 饮片 {dose.daily_decoction_grams}g/日 "
        f"÷ 当量系数 {standard.extract_ratio}（标准 {standard.standard_id}）"
        f" = {exact_daily}g 颗粒/日；"
        f"每日 {dose.portions_per_day} 次 → {exact_per_portion}g/次；"
        f"按包装步长 {standard.package_step_grams}g 四舍五入 "
        f"→ {dispensed_per_portion}g/次；"
        f"日调配量 = {dispensed_per_portion}g × {dose.portions_per_day} "
        f"= {dispensed_daily}g；"
        f"舍入差额 {delta:+}g"
    )
    return LineComputation(
        exact_daily_grams=exact_daily,
        exact_per_portion_grams=exact_per_portion,
        dispensed_per_portion_grams=dispensed_per_portion,
        dispensed_daily_grams=dispensed_daily,
        rounding_delta_grams=delta,
        exceeds_maximum_daily=exceeds,
        formula_text=formula,
    )
