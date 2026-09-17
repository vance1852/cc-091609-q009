"""配方颗粒等量换算与审核服务的测试。"""

from __future__ import annotations

import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from products.catalog import DuplicateStandardError, StandardCatalog
from products.contracts import DecoctionDose, GranuleStandard
from products.conversion import convert_line
from products.fixture_loader import load_granule_switch
from products.service import (
    GranuleConversionService,
    PendingReviewError,
    Prescription,
    ReviewDecision,
    ReviewReason,
    ReviewStatus,
    ServiceError,
)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "granule_switch.json"
NOW = datetime(2026, 9, 17, 9, 30, 0)


def make_standard(
    standard_id: str,
    *,
    herb: str = "HQ",
    ratio: str = "5.0",
    step: str = "0.5",
    max_daily: str = "50",
    manufacturer: str = "mfr-a",
    group: str | None = None,
    valid_from: date = date(2026, 1, 1),
    valid_until: date | None = None,
) -> GranuleStandard:
    return GranuleStandard(
        standard_id=standard_id,
        herb_code=herb,
        manufacturer_id=manufacturer,
        valid_from=valid_from,
        valid_until=valid_until,
        extract_ratio=Decimal(ratio),
        package_step_grams=Decimal(step),
        maximum_daily_grams=Decimal(max_daily),
        interchangeable_group=group,
    )


def make_dose(
    line_id: str = "L1",
    *,
    herb: str = "HQ",
    name: str = "黄芪",
    grams: str = "30",
    portions: int = 1,
) -> DecoctionDose:
    return DecoctionDose(
        line_id=line_id,
        herb_code=herb,
        source_name=name,
        daily_decoction_grams=Decimal(grams),
        portions_per_day=portions,
    )


def make_prescription(prescription_id: str, *lines: DecoctionDose) -> Prescription:
    return Prescription(
        prescription_id=prescription_id,
        lines=tuple(lines),
        prescribed_by="dr-wang",
        prescribed_at=datetime(2026, 9, 10, 8, 0, 0),
    )


def make_catalog_service(*standards: GranuleStandard) -> tuple[StandardCatalog, GranuleConversionService]:
    catalog = StandardCatalog()
    for standard in standards:
        catalog.register(standard)
    return catalog, GranuleConversionService(catalog, clock=lambda: NOW)


def make_service(*standards: GranuleStandard) -> GranuleConversionService:
    return make_catalog_service(*standards)[1]


def decide(action: str = "approve", rationale: str = "理由", **kwargs) -> ReviewDecision:
    return ReviewDecision(
        pharmacist_id=kwargs.pop("pharmacist_id", "ph-01"),
        action=action,
        rationale=rationale,
        decided_at=kwargs.pop("decided_at", NOW),
        **kwargs,
    )


class ConversionMathTests(unittest.TestCase):
    """换算链路：饮片量 → 换算值 → 分次 → 步长 → 日调配量 → 差额。"""

    def test_exact_multiple_of_step_has_zero_delta(self):
        conv = convert_line(make_dose(grams="30"), make_standard("s1", ratio="5.0", step="0.5"))
        self.assertEqual(conv.exact_grams, Decimal("6.0000"))
        self.assertEqual(conv.dispensed_grams, Decimal("6.0000"))
        self.assertEqual(conv.rounding_delta_grams, Decimal("0.0000"))
        self.assertFalse(conv.exceeds_max_daily)
        self.assertFalse(conv.rounds_to_zero)

    def test_rounding_delta_is_visible_not_silently_reverted(self):
        # 30 ÷ 4.2 = 7.1429，按 0.4 步长 → 7.2，差额 +0.0571 必须保留展示。
        conv = convert_line(make_dose(grams="30"), make_standard("s1", ratio="4.2", step="0.4"))
        self.assertEqual(conv.exact_grams, Decimal("7.1429"))
        self.assertEqual(conv.dispensed_grams, Decimal("7.2000"))
        self.assertEqual(conv.rounding_delta_grams, Decimal("0.0571"))
        self.assertNotEqual(conv.dispensed_grams, conv.exact_grams)
        self.assertIn("舍入差额 +0.0571g", conv.formula_text)

    def test_portions_round_per_portion(self):
        # 10 ÷ 4 = 2.5/日，分 2 次 → 每次 1.25，按 0.5 步长 → 1.5，日调配 3.0。
        conv = convert_line(
            make_dose(grams="10", portions=2),
            make_standard("s1", ratio="4.0", step="0.5"),
        )
        self.assertEqual(conv.portion_exact_grams, Decimal("1.2500"))
        self.assertEqual(conv.portion_dispensed_grams, Decimal("1.5000"))
        self.assertEqual(conv.dispensed_grams, Decimal("3.0000"))
        self.assertEqual(conv.rounding_delta_grams, Decimal("0.5000"))
        self.assertIn("每日 2 次", conv.formula_text)

    def test_rounds_to_zero_is_flagged(self):
        conv = convert_line(make_dose(grams="1"), make_standard("s1", ratio="10", step="0.5"))
        self.assertTrue(conv.rounds_to_zero)
        self.assertEqual(conv.dispensed_grams, Decimal("0.0000"))

    def test_exceeds_max_daily_is_flagged(self):
        conv = convert_line(make_dose(grams="30"), make_standard("s1", ratio="5.0", max_daily="5"))
        self.assertTrue(conv.exceeds_max_daily)

    def test_formula_text_contains_every_number(self):
        conv = convert_line(make_dose(grams="30", portions=2), make_standard("std-x", ratio="4.2", step="0.4"))
        for token in ("30", "4.2", "std-x", "0.4", "舍入差额"):
            self.assertIn(token, conv.formula_text)


class CatalogTests(unittest.TestCase):
    def test_selects_standard_by_dispensing_date(self):
        catalog = StandardCatalog()
        catalog.register(make_standard("old", valid_from=date(2026, 1, 1), valid_until=date(2026, 9, 15)))
        catalog.register(make_standard("new", valid_from=date(2026, 9, 16)))
        self.assertEqual(catalog.find("HQ", date(2026, 9, 15)).standard_id, "old")
        self.assertEqual(catalog.find("HQ", date(2026, 9, 16)).standard_id, "new")

    def test_correction_wins_overlapping_period_by_registration_order(self):
        catalog = StandardCatalog()
        catalog.register(make_standard("v1", valid_from=date(2026, 9, 1)))
        catalog.register(make_standard("v2", valid_from=date(2026, 9, 1)))
        self.assertEqual(catalog.find("HQ", date(2026, 9, 10)).standard_id, "v2")

    def test_missing_herb_returns_none(self):
        catalog = StandardCatalog()
        self.assertIsNone(catalog.find("UNKNOWN", date(2026, 9, 10)))

    def test_duplicate_standard_id_rejected(self):
        catalog = StandardCatalog()
        catalog.register(make_standard("s1"))
        with self.assertRaises(DuplicateStandardError):
            catalog.register(make_standard("s1"))


class FixtureSwitchTests(unittest.TestCase):
    """样例：同一处方量跨标准生效日的两次调配。"""

    def setUp(self):
        fixture = load_granule_switch(FIXTURE_PATH)
        self.fixture = fixture
        catalog = StandardCatalog()
        for standard in fixture.standards:
            catalog.register(standard)
        self.service = GranuleConversionService(catalog, clock=lambda: NOW)
        for item in fixture.prescriptions:
            self.service.register_prescription(item.prescription)
            self.service.convert(
                item.prescription.prescription_id,
                item.dispensing_date,
                request_key=f"t-{item.prescription.prescription_id}",
                initiated_by="test",
            )

    def test_before_effective_date_uses_old_standard(self):
        sheet = self.service.dispensing_sheet("rx-before")
        record = sheet.lines[0].record
        self.assertEqual(record.standard_id, "std-a")
        self.assertEqual(record.exact_grams, Decimal("6.0000"))
        self.assertEqual(record.dispensed_grams, Decimal("6.0000"))
        self.assertEqual(record.rounding_delta_grams, Decimal("0.0000"))

    def test_after_effective_date_uses_new_standard(self):
        sheet = self.service.dispensing_sheet("rx-after")
        record = sheet.lines[0].record
        self.assertEqual(record.standard_id, "std-b")
        self.assertEqual(record.exact_grams, Decimal("7.1429"))
        self.assertEqual(record.dispensed_grams, Decimal("7.2000"))
        self.assertEqual(record.rounding_delta_grams, Decimal("0.0571"))

    def test_explain_shows_formula_source_and_delta_for_both(self):
        for prescription_id, standard_id in (("rx-before", "std-a"), ("rx-after", "std-b")):
            text = self.service.explain_prescription(prescription_id)
            self.assertIn("标准来源", text)
            self.assertIn(standard_id, text)
            self.assertIn("公式", text)
            self.assertIn("舍入差额", text)
            self.assertIn("30", text)  # 医师开具的饮片量保留在快照中


class VersioningTests(unittest.TestCase):
    """标准更正不追改历史；重算生成新版本；重复请求幂等。"""

    def setUp(self):
        self.catalog, self.service = make_catalog_service(make_standard("std-v1", ratio="5.0", step="0.5"))
        self.service.register_prescription(make_prescription("P1", make_dose(grams="30")))
        self.service.convert("P1", date(2026, 9, 10), request_key="k1", initiated_by="system")
        self.service.confirm_version("P1", 1, pharmacist_id="ph-01")

    def test_correction_does_not_rewrite_reviewed_version(self):
        self.catalog.register(make_standard("std-v2", ratio="4.0", step="0.5", valid_from=date(2026, 9, 1)))
        self.service.recalculate("P1", request_key="k2", initiated_by="ph-01", reason="标准更正重算")

        v1, v2 = self.service.versions("P1")
        # v1 已审核，仍引用旧标准，数值不变。
        self.assertEqual(v1.records[0].standard_id, "std-v1")
        self.assertEqual(v1.records[0].dispensed_grams, Decimal("6.0000"))
        # v2 采用更正后的标准：30 ÷ 4.0 = 7.5。
        self.assertEqual(v2.records[0].standard_id, "std-v2")
        self.assertEqual(v2.records[0].dispensed_grams, Decimal("7.5000"))

    def test_correction_applies_to_new_prescriptions(self):
        self.catalog.register(make_standard("std-v2", ratio="4.0", valid_from=date(2026, 9, 1)))
        self.service.register_prescription(make_prescription("P2", make_dose(grams="30")))
        version = self.service.convert("P2", date(2026, 9, 10), request_key="k-p2", initiated_by="system")
        self.assertEqual(version.records[0].standard_id, "std-v2")

    def test_repeated_recalculate_returns_same_version(self):
        self.catalog.register(make_standard("std-v2", ratio="4.0", valid_from=date(2026, 9, 1)))
        first = self.service.recalculate("P1", request_key="k2", initiated_by="ph-01", reason="标准更正重算")
        second = self.service.recalculate("P1", request_key="k2", initiated_by="ph-01", reason="标准更正重算")
        self.assertIs(first, second)
        self.assertEqual(len(self.service.versions("P1")), 2)

    def test_repeated_convert_returns_same_version(self):
        again = self.service.convert("P1", date(2026, 9, 10), request_key="k1", initiated_by="system")
        self.assertEqual(again.version_no, 1)
        self.assertEqual(len(self.service.versions("P1")), 1)

    def test_recalculate_requires_existing_version(self):
        self.service.register_prescription(make_prescription("P9", make_dose()))
        with self.assertRaises(ServiceError):
            self.service.recalculate("P9", request_key="k9", initiated_by="ph-01", reason="无首版")


class CombinedPrescriptionTests(unittest.TestCase):
    """合方：一张处方多味药，可换算的出记录，有问题的进审核。"""

    def test_multi_herb_prescription(self):
        service = make_service(
            make_standard("s-hq", herb="HQ", ratio="5.0"),
            make_standard("s-gc", herb="GC", ratio="3.0", step="0.5"),
        )
        service.register_prescription(
            make_prescription(
                "P1",
                make_dose("L1", herb="HQ", name="黄芪", grams="30"),
                make_dose("L2", herb="GC", name="甘草", grams="6"),
                make_dose("L3", herb="UNKNOWN", name="未知药", grams="10"),
            )
        )
        version = service.convert("P1", date(2026, 9, 10), request_key="k1", initiated_by="system")
        self.assertEqual(len(version.records), 2)
        self.assertEqual(len(version.review_case_ids), 1)
        case = service.get_case(version.review_case_ids[0])
        self.assertEqual(case.reason, ReviewReason.MISSING_STANDARD)
        self.assertEqual(case.line_id, "L3")


class ReviewFlowTests(unittest.TestCase):
    def test_missing_standard_goes_to_review_then_approved(self):
        catalog, service = make_catalog_service()
        service.register_prescription(make_prescription("P1", make_dose(grams="30")))
        version = service.convert("P1", date(2026, 9, 10), request_key="k1", initiated_by="system")
        self.assertEqual(version.records, ())
        case = service.get_case(version.review_case_ids[0])
        self.assertEqual(case.reason, ReviewReason.MISSING_STANDARD)

        # 药师补录标准后批准。
        catalog.register(make_standard("std-new", ratio="5.0"))
        resolved = service.resolve_review(case.case_id, decide(standard_id="std-new", rationale="已补录品种标准"))
        self.assertEqual(resolved.status, ReviewStatus.APPROVED)
        sheet = service.dispensing_sheet("P1")
        self.assertEqual(sheet.lines[0].status, "manual_approved")
        record = sheet.lines[0].record
        self.assertEqual(record.standard_id, "std-new")
        self.assertEqual(record.reviewed_at, NOW)
        self.assertIn("已补录品种标准", record.formula_text)

    def test_exceeds_max_daily_goes_to_review_with_pharmacist_cap(self):
        service = make_service(make_standard("s1", ratio="5.0", max_daily="5"))
        service.register_prescription(make_prescription("P1", make_dose(grams="30")))
        version = service.convert("P1", date(2026, 9, 10), request_key="k1", initiated_by="system")
        case = service.get_case(version.review_case_ids[0])
        self.assertEqual(case.reason, ReviewReason.EXCEEDS_MAX_DAILY)
        self.assertIn("最大日用量", case.detail)

        # 药师裁定按 5.0g 调配，差额 −1.0g 同样保留展示。
        service.resolve_review(case.case_id, decide(approved_grams=Decimal("5.0"), rationale="超上限，裁定减量"))
        record = service.dispensing_sheet("P1").lines[0].record
        self.assertEqual(record.dispensed_grams, Decimal("5.0"))
        self.assertEqual(record.rounding_delta_grams, Decimal("-1.0000"))
        self.assertIn("药师裁定日调配量 5.0g", record.formula_text)

    def test_rounded_to_zero_goes_to_review(self):
        service = make_service(make_standard("s1", ratio="10", step="0.5"))
        service.register_prescription(make_prescription("P1", make_dose(grams="1")))
        version = service.convert("P1", date(2026, 9, 10), request_key="k1", initiated_by="system")
        case = service.get_case(version.review_case_ids[0])
        self.assertEqual(case.reason, ReviewReason.ROUNDED_TO_ZERO)

    def test_non_interchangeable_manufacturer_goes_to_review(self):
        catalog, service = make_catalog_service(make_standard("std-a", manufacturer="mfr-a", group=None))
        service.register_prescription(make_prescription("P1", make_dose(grams="30")))
        service.convert("P1", date(2026, 9, 10), request_key="k1", initiated_by="system")
        service.confirm_version("P1", 1, pharmacist_id="ph-01")

        # 供应商切换为另一厂家、互换组不一致 → 重算进入人工审核。
        catalog.register(
            make_standard("std-b", manufacturer="mfr-b", group=None, valid_from=date(2026, 9, 1))
        )
        version = service.recalculate("P1", request_key="k2", initiated_by="ph-01", reason="供应商切换")
        self.assertEqual(version.records, ())
        case = service.get_case(version.review_case_ids[0])
        self.assertEqual(case.reason, ReviewReason.NON_INTERCHANGEABLE)
        self.assertIn("mfr-a", case.detail)
        self.assertIn("mfr-b", case.detail)

        service.resolve_review(case.case_id, decide(rationale="医师同意换厂家"))
        sheet = service.dispensing_sheet("P1")
        self.assertEqual(sheet.lines[0].status, "manual_approved")
        self.assertEqual(sheet.lines[0].record.standard_id, "std-b")

    def test_same_interchangeable_group_allows_auto_switch(self):
        catalog, service = make_catalog_service(make_standard("std-a", manufacturer="mfr-a", group="G1"))
        service.register_prescription(make_prescription("P1", make_dose(grams="30")))
        service.convert("P1", date(2026, 9, 10), request_key="k1", initiated_by="system")
        service._catalog.register(
            make_standard("std-b", manufacturer="mfr-b", group="G1", valid_from=date(2026, 9, 1))
        )
        version = service.recalculate("P1", request_key="k2", initiated_by="ph-01", reason="同组互换")
        self.assertEqual(len(version.records), 1)
        self.assertEqual(version.records[0].standard_id, "std-b")

    def test_rejected_case_leaves_line_undispensed(self):
        service = make_service()
        service.register_prescription(make_prescription("P1", make_dose()))
        version = service.convert("P1", date(2026, 9, 10), request_key="k1", initiated_by="system")
        case_id = version.review_case_ids[0]
        service.resolve_review(case_id, decide(action="reject", rationale="暂停调配，待医师确认"))
        sheet = service.dispensing_sheet("P1")
        self.assertEqual(sheet.lines[0].status, "rejected")
        self.assertIsNone(sheet.lines[0].record)

    def test_case_cannot_be_resolved_twice(self):
        service = make_service()
        service.register_prescription(make_prescription("P1", make_dose()))
        version = service.convert("P1", date(2026, 9, 10), request_key="k1", initiated_by="system")
        case_id = version.review_case_ids[0]
        service.resolve_review(case_id, decide(action="reject", rationale="驳回"))
        with self.assertRaises(ServiceError):
            service.resolve_review(case_id, decide(action="reject", rationale="重复处理"))

    def test_decision_requires_rationale(self):
        service = make_service()
        service.register_prescription(make_prescription("P1", make_dose()))
        version = service.convert("P1", date(2026, 9, 10), request_key="k1", initiated_by="system")
        with self.assertRaises(ServiceError):
            service.resolve_review(version.review_case_ids[0], decide(action="reject", rationale="  "))

    def test_confirm_blocked_while_review_pending(self):
        service = make_service()
        service.register_prescription(make_prescription("P1", make_dose()))
        service.convert("P1", date(2026, 9, 10), request_key="k1", initiated_by="system")
        with self.assertRaises(PendingReviewError):
            service.confirm_version("P1", 1, pharmacist_id="ph-01")

    def test_explain_shows_manual_decision(self):
        service = make_service(make_standard("s1", ratio="5.0", max_daily="5"))
        service.register_prescription(make_prescription("P1", make_dose(grams="30")))
        version = service.convert("P1", date(2026, 9, 10), request_key="k1", initiated_by="system")
        service.resolve_review(
            version.review_case_ids[0],
            decide(approved_grams=Decimal("5.0"), rationale="超上限，裁定减量"),
        )
        text = service.explain_prescription("P1")
        self.assertIn("人工审核[exceeds_max_daily]", text)
        self.assertIn("人工决定：ph-01", text)
        self.assertIn("超上限，裁定减量", text)
        self.assertIn("-1.0000", text)


class SnapshotTests(unittest.TestCase):
    def test_prescription_snapshot_is_immutable_and_kept(self):
        service = make_service(make_standard("s1"))
        prescription = make_prescription("P1", make_dose(grams="30"))
        service.register_prescription(prescription)
        with self.assertRaises(ServiceError):
            service.register_prescription(make_prescription("P1", make_dose(grams="15")))
        # 同一内容重复登记幂等。
        self.assertIs(service.register_prescription(prescription), prescription)
        # 换算记录回指处方快照，医师开具量原样保留。
        service.convert("P1", date(2026, 9, 10), request_key="k1", initiated_by="system")
        record = service.dispensing_sheet("P1").lines[0].record
        self.assertEqual(record.prescription_snapshot_id, "P1")
        self.assertEqual(service._prescriptions["P1"].lines[0].daily_decoction_grams, Decimal("30"))


if __name__ == "__main__":
    unittest.main()
