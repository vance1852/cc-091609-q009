"""把 fixtures/granule_switch.json 映射为领域对象。

脱敏样例只提供标准 id、生效日、提取比、包装步长和处方饮片量，
缺少厂家、最大日用量、分次等字段。这里显式给出占位默认值，
默认值只影响安全边界检查与追溯展示，不改变样例的换算结果。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from products.contracts import DecoctionDose, GranuleStandard
from products.service import Prescription

#: 脱敏样例未提供的字段使用的占位默认值。
DEFAULT_MANUFACTURER = "unspecified"
DEFAULT_MAX_DAILY_GRAMS = Decimal("250")
#: 旧标准未给出起始生效日，取足够早的占位日期以覆盖历史调配日。
PLACEHOLDER_VALID_FROM = date(2000, 1, 1)


@dataclass(frozen=True)
class FixturePrescription:
    prescription: Prescription
    dispensing_date: date


@dataclass(frozen=True)
class GranuleSwitchFixture:
    herb: str
    standards: tuple[GranuleStandard, ...]
    prescriptions: tuple[FixturePrescription, ...]


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def load_granule_switch(path: str | Path) -> GranuleSwitchFixture:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    herb = data["herb"]

    def build_standard(node: dict, valid_from: date) -> GranuleStandard:
        valid_until = node.get("validUntil")
        return GranuleStandard(
            standard_id=node["id"],
            herb_code=herb,
            manufacturer_id=DEFAULT_MANUFACTURER,
            valid_from=valid_from,
            valid_until=_parse_date(valid_until) if valid_until else None,
            extract_ratio=Decimal(node["ratio"]),
            package_step_grams=Decimal(node["stepGrams"]),
            maximum_daily_grams=DEFAULT_MAX_DAILY_GRAMS,
            interchangeable_group=None,
        )

    standards = (
        build_standard(data["oldStandard"], PLACEHOLDER_VALID_FROM),
        build_standard(data["newStandard"], _parse_date(data["newStandard"]["validFrom"])),
    )

    prescriptions = []
    for node in data["prescriptions"]:
        dispensing_date = _parse_date(node["dispensingDate"])
        prescription = Prescription(
            prescription_id=node["id"],
            lines=(
                DecoctionDose(
                    line_id=f"{node['id']}-L1",
                    herb_code=herb,
                    source_name=herb,
                    daily_decoction_grams=Decimal(node["decoctionGrams"]),
                    portions_per_day=1,
                ),
            ),
            prescribed_by="（脱敏样例）",
            prescribed_at=datetime.combine(dispensing_date, time.min),
        )
        prescriptions.append(FixturePrescription(prescription, dispensing_date))

    return GranuleSwitchFixture(herb=herb, standards=standards, prescriptions=tuple(prescriptions))
