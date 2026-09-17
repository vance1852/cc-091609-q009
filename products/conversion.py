"""单味饮片 → 配方颗粒的等量换算（纯函数，全程 Decimal）。

换算链路与公式文本使用的数字完全一致，保证记录可复算：

1. 医师开具的饮片日剂量 ÷ 提取比 = 颗粒日换算值（量化到 0.1mg）；
2. 按每日分次均分，得到每次换算值（同样量化）；
3. 每次量按包装步长四舍五入到步长整数倍；
4. 日调配量 = 每次调配量 × 每日次数；
5. 舍入差额 = 日调配量 − 日换算值，差额保留展示，绝不回改为原处方量。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from products.contracts import DecoctionDose, GranuleStandard

#: 换算值记录精度：0.1mg。记录、公式与差额统一使用该精度，保证可复算。
EXACT_QUANTUM = Decimal("0.0001")


def _quantize(value: Decimal, quantum: Decimal = EXACT_QUANTUM) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class LineConversion:
    """一味饮片在某产品标准下的换算结果。"""

    dose: DecoctionDose
    standard: GranuleStandard
    exact_grams: Decimal
    portion_exact_grams: Decimal
    portion_dispensed_grams: Decimal
    dispensed_grams: Decimal
    rounding_delta_grams: Decimal
    exceeds_max_daily: bool
    rounds_to_zero: bool
    formula_text: str


def convert_line(dose: DecoctionDose, standard: GranuleStandard) -> LineConversion:
    """按产品标准换算一味饮片，返回全部中间量与公式文本。"""
    if dose.daily_decoction_grams <= 0:
        raise ValueError(f"{dose.line_id}：饮片日剂量必须为正数")
    if dose.portions_per_day < 1:
        raise ValueError(f"{dose.line_id}：每日分次必须 ≥ 1")
    if standard.extract_ratio <= 0:
        raise ValueError(f"标准 {standard.standard_id}：提取比必须为正数")
    if standard.package_step_grams <= 0:
        raise ValueError(f"标准 {standard.standard_id}：包装步长必须为正数")

    exact = _quantize(dose.daily_decoction_grams / standard.extract_ratio)
    portion_exact = _quantize(exact / dose.portions_per_day)
    steps = int((portion_exact / standard.package_step_grams).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    portion_dispensed = _quantize(standard.package_step_grams * steps)
    dispensed = _quantize(portion_dispensed * dose.portions_per_day)
    delta = _quantize(dispensed - exact)

    exceeds_max_daily = exact > standard.maximum_daily_grams or dispensed > standard.maximum_daily_grams
    rounds_to_zero = portion_dispensed == 0 and exact > 0

    formula_text = (
        f"{dose.source_name} {dose.daily_decoction_grams}g/日 ÷ {standard.extract_ratio}"
        f"（{standard.standard_id} 提取比）= {exact}g/日；"
        f"每日 {dose.portions_per_day} 次，每次 {portion_exact}g；"
        f"按包装步长 {standard.package_step_grams}g 四舍五入 → 每次 {portion_dispensed}g；"
        f"日调配量 {dispensed}g；舍入差额 {delta:+}g"
    )

    return LineConversion(
        dose=dose,
        standard=standard,
        exact_grams=exact,
        portion_exact_grams=portion_exact,
        portion_dispensed_grams=portion_dispensed,
        dispensed_grams=dispensed,
        rounding_delta_grams=delta,
        exceeds_max_daily=exceeds_max_daily,
        rounds_to_zero=rounds_to_zero,
        formula_text=formula_text,
    )
