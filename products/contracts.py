"""颗粒产品标准与剂量换算记录。"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class GranuleStandard:
    standard_id: str
    herb_code: str
    manufacturer_id: str
    valid_from: date
    valid_until: date | None
    extract_ratio: Decimal
    package_step_grams: Decimal
    maximum_daily_grams: Decimal
    interchangeable_group: str | None


@dataclass(frozen=True)
class DecoctionDose:
    line_id: str
    herb_code: str
    source_name: str
    daily_decoction_grams: Decimal
    portions_per_day: int


@dataclass(frozen=True)
class ConversionRecord:
    conversion_id: str
    prescription_snapshot_id: str
    dispensing_date: date
    standard_id: str
    exact_grams: Decimal
    dispensed_grams: Decimal
    rounding_delta_grams: Decimal
    formula_text: str
    reviewed_at: datetime | None
