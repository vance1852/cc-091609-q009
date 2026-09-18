#!/usr/bin/env python3
"""读取 fixtures/granule_switch.json，演示供应标准切换日前后的换算对照。

用法: python scripts/compare_fixture.py

fixture 未携带的字段采用演示默认值（每日 2 次、最大日量 15g、
厂家按标准 id 派生），实际部署时应来自产品主数据。
"""

from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from products import (  # noqa: E402
    DecoctionDose,
    GranuleConversionService,
    GranuleStandard,
    StandardRegistry,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "granule_switch.json"

#: 演示默认值（fixture 未提供）
PORTIONS_PER_DAY = 2
MAX_DAILY_GRAMS = Decimal("15")
OLD_VALID_FROM = date(2026, 1, 1)


def build_service(fixture: dict) -> GranuleConversionService:
    registry = StandardRegistry()
    herb_code = fixture["herb"]
    old = fixture["oldStandard"]
    new = fixture["newStandard"]
    registry.register(
        GranuleStandard(
            standard_id=old["id"],
            herb_code=herb_code,
            manufacturer_id=f"mfr-{old['id']}",
            valid_from=OLD_VALID_FROM,
            valid_until=date.fromisoformat(old["validUntil"]),
            extract_ratio=Decimal(old["ratio"]),
            package_step_grams=Decimal(old["stepGrams"]),
            maximum_daily_grams=MAX_DAILY_GRAMS,
            interchangeable_group=None,
        )
    )
    registry.register(
        GranuleStandard(
            standard_id=new["id"],
            herb_code=herb_code,
            manufacturer_id=f"mfr-{new['id']}",
            valid_from=date.fromisoformat(new["validFrom"]),
            valid_until=None,
            extract_ratio=Decimal(new["ratio"]),
            package_step_grams=Decimal(new["stepGrams"]),
            maximum_daily_grams=MAX_DAILY_GRAMS,
            interchangeable_group=None,
        )
    )
    return GranuleConversionService(registry)


def main() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    service = build_service(fixture)
    herb = fixture["herb"]

    record_ids = []
    for rx in fixture["prescriptions"]:
        snapshot = service.snapshot_prescription(
            rx["id"],
            physician_name="（脱敏）医师",
            patient_ref="（脱敏）患者",
            lines=[
                DecoctionDose(
                    line_id=f"{rx['id']}-line-1",
                    herb_code=herb,
                    source_name=herb,
                    daily_decoction_grams=Decimal(rx["decoctionGrams"]),
                    portions_per_day=PORTIONS_PER_DAY,
                )
            ],
        )
        record = service.create_dispensing(
            snapshot.snapshot_id,
            date.fromisoformat(rx["dispensingDate"]),
            created_by="pharmacist-demo",
        )
        record = service.approve(record.record_id, pharmacist_id="pharmacist-demo")
        record_ids.append(record.record_id)

    print("=" * 72)
    print(f"供应标准切换前后对照：{herb}（每日 {PORTIONS_PER_DAY} 次，"
          f"最大日量 {MAX_DAILY_GRAMS}g 为演示默认值）")
    print("=" * 72)
    print(service.compare(record_ids).render_text())

    # 标准更正：不追改已审核单，只影响药师主动发起的重算版本。
    print()
    print("=" * 72)
    print("标准更正演示：std-b 系数更正为 4.0（std-b-v2 取代 std-b）")
    print("=" * 72)
    registry = service.registry
    corrected = GranuleStandard(
        standard_id="std-b-v2",
        herb_code=herb,
        manufacturer_id="mfr-std-b",
        valid_from=date(2026, 9, 16),
        valid_until=None,
        extract_ratio=Decimal("4.0"),
        package_step_grams=Decimal("0.4"),
        maximum_daily_grams=MAX_DAILY_GRAMS,
        interchangeable_group=None,
    )
    registry.correct(corrected, supersedes="std-b")

    before = service.explain(record_ids[1])
    print(f"已审核单 {before.record_id} 仍为: "
          f"{before.lines[0].dispensed_grams}g/日（标准 {before.lines[0].standard_id}）"
          "——更正不追改")

    recalc = service.recalculate(
        record_ids[1],
        pharmacist_id="pharmacist-demo",
        reason="标准 std-b 系数更正，药师发起重算",
        request_key="recalc-rx-after-001",
    )
    again = service.recalculate(
        record_ids[1],
        pharmacist_id="pharmacist-demo",
        reason="标准 std-b 系数更正，药师发起重算",
        request_key="recalc-rx-after-001",
    )
    print(f"重复重算请求返回同一版本: {again.record_id == recalc.record_id}")
    print()
    print(service.compare_snapshot(recalc.snapshot.snapshot_id).render_text())


if __name__ == "__main__":
    main()
