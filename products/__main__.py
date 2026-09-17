"""演示：供应标准切换日前后的换算对比，以及标准更正不追改历史。

运行：python -m products
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from products.catalog import StandardCatalog
from products.contracts import GranuleStandard
from products.fixture_loader import DEFAULT_MANUFACTURER, load_granule_switch
from products.service import GranuleConversionService

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "granule_switch.json"


def main() -> None:
    fixture = load_granule_switch(FIXTURE_PATH)
    catalog = StandardCatalog()
    for standard in fixture.standards:
        catalog.register(standard)
    service = GranuleConversionService(catalog)

    print("=" * 78)
    print(f"一、{fixture.herb} 供应标准切换日前后：同一处方量的两次调配")
    print("=" * 78)
    for item in fixture.prescriptions:
        service.register_prescription(item.prescription)
        service.convert(
            item.prescription.prescription_id,
            item.dispensing_date,
            request_key=f"demo-{item.prescription.prescription_id}",
            initiated_by="系统（按调配日自动选择标准）",
        )
        print(service.explain_prescription(item.prescription.prescription_id))
        print("-" * 78)

    before = service.dispensing_sheet("rx-before").lines[0].record
    after = service.dispensing_sheet("rx-after").lines[0].record
    print(
        f"对比：饮片 30g/日 在切换日前调配 {before.dispensed_grams}g（{before.standard_id}），"
        f"切换日后调配 {after.dispensed_grams}g（{after.standard_id}）。"
        f"差异来自提取比与包装步长不同，处方本身未变。"
    )
    print()

    print("=" * 78)
    print("二、标准更正：不追改已审核调配单，只影响新单与药师主动重算")
    print("=" * 78)
    # 药师确认切换日后的调配单，使其成为"已审核"状态。
    service.confirm_version("rx-after", 1, pharmacist_id="pharmacist-01")

    # 供应商更正提取比：4.2 → 4.0，以新标准注册，旧标准保持不动。
    corrected = GranuleStandard(
        standard_id="std-b2",
        herb_code=fixture.herb,
        manufacturer_id=DEFAULT_MANUFACTURER,
        valid_from=date(2026, 9, 16),
        valid_until=None,
        extract_ratio=Decimal("4.0"),
        package_step_grams=Decimal("0.4"),
        maximum_daily_grams=Decimal("250"),
        interchangeable_group=None,
    )
    catalog.register(corrected)
    print("标准更正已注册：std-b2（提取比 4.0，2026-09-16 起），std-b 历史版本保留。")
    print()

    # 药师主动发起重算 → 新版本 v2；v1（已审核）保持 std-b 的结果。
    service.recalculate(
        "rx-after",
        request_key="recalc-rx-after-std-b2",
        initiated_by="pharmacist-01",
        reason="标准更正（std-b → std-b2），药师主动重算",
    )
    # 重复的重算请求返回同一版本，不产生 v3。
    repeated = service.recalculate(
        "rx-after",
        request_key="recalc-rx-after-std-b2",
        initiated_by="pharmacist-01",
        reason="标准更正（std-b → std-b2），药师主动重算",
    )
    print(service.explain_prescription("rx-after"))
    print("-" * 78)
    versions = service.versions("rx-after")
    print(
        f"重复重算返回同一版本：v{repeated.version_no}（共 {len(versions)} 个版本）；"
        f"v1 仍为 {versions[0].records[0].dispensed_grams}g（{versions[0].records[0].standard_id}，已审核，未被追改），"
        f"v2 为 {versions[1].records[0].dispensed_grams}g（{versions[1].records[0].standard_id}）。"
    )


if __name__ == "__main__":
    main()
