"""配方颗粒等量换算与审核服务的单元测试（标准库 unittest）。

运行: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from products import (
    DecisionAction,
    DecoctionDose,
    GranuleConversionService,
    GranuleStandard,
    PendingReviewError,
    RecordFrozenError,
    RecordStatus,
    ReviewReason,
    StandardRegistry,
    round_to_step,
)
from products.service import LineStatus

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "granule_switch.json"
NOW = datetime(2026, 9, 17, 9, 0, 0)


def make_standard(
    standard_id: str,
    *,
    herb_code: str = "黄芪",
    manufacturer_id: str = "mfr-a",
    valid_from: date = date(2026, 1, 1),
    valid_until: date | None = None,
    ratio: str = "5.0",
    step: str = "0.5",
    max_daily: str = "15",
    group: str | None = None,
) -> GranuleStandard:
    return GranuleStandard(
        standard_id=standard_id,
        herb_code=herb_code,
        manufacturer_id=manufacturer_id,
        valid_from=valid_from,
        valid_until=valid_until,
        extract_ratio=Decimal(ratio),
        package_step_grams=Decimal(step),
        maximum_daily_grams=Decimal(max_daily),
        interchangeable_group=group,
    )


def make_service(*standards: GranuleStandard) -> GranuleConversionService:
    registry = StandardRegistry()
    for standard in standards:
        registry.register(standard)
    return GranuleConversionService(registry, clock=lambda: NOW)


def make_snapshot(
    service: GranuleConversionService,
    prescription_id: str = "rx-1",
    *,
    herb_code: str = "黄芪",
    grams: str = "30",
    portions: int = 2,
) -> str:
    snapshot = service.snapshot_prescription(
        prescription_id,
        physician_name="医师甲",
        patient_ref="患者某",
        lines=[
            DecoctionDose(
                line_id=f"{prescription_id}-1",
                herb_code=herb_code,
                source_name=herb_code,
                daily_decoction_grams=Decimal(grams),
                portions_per_day=portions,
            )
        ],
    )
    return snapshot.snapshot_id


class FixtureSwitchTest(unittest.TestCase):
    """按 fixture 复现标准切换日前后的换算。"""

    def setUp(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.fixture = fixture
        old, new = fixture["oldStandard"], fixture["newStandard"]
        self.service = make_service(
            make_standard(
                old["id"],
                manufacturer_id="mfr-old",
                valid_until=date.fromisoformat(old["validUntil"]),
                ratio=old["ratio"],
                step=old["stepGrams"],
            ),
            make_standard(
                new["id"],
                manufacturer_id="mfr-new",
                valid_from=date.fromisoformat(new["validFrom"]),
                ratio=new["ratio"],
                step=new["stepGrams"],
            ),
        )

    def convert(self, rx: dict):
        snapshot_id = make_snapshot(
            self.service, rx["id"], grams=rx["decoctionGrams"]
        )
        return self.service.create_dispensing(
            snapshot_id,
            date.fromisoformat(rx["dispensingDate"]),
            created_by="pharmacist",
        )

    def test_standard_selected_by_dispensing_date(self) -> None:
        before, after = (self.convert(rx) for rx in self.fixture["prescriptions"])
        self.assertEqual(before.lines[0].standard.standard_id, "std-a")
        self.assertEqual(after.lines[0].standard.standard_id, "std-b")

    def test_before_switch_numbers(self) -> None:
        record = self.convert(self.fixture["prescriptions"][0])
        comp = record.lines[0].computation
        self.assertEqual(comp.exact_daily_grams, Decimal("6.000000"))
        self.assertEqual(comp.dispensed_per_portion_grams, Decimal("3.0"))
        self.assertEqual(comp.dispensed_daily_grams, Decimal("6.000000"))
        self.assertEqual(comp.rounding_delta_grams, Decimal("0.000000"))

    def test_after_switch_numbers_and_visible_delta(self) -> None:
        record = self.convert(self.fixture["prescriptions"][1])
        comp = record.lines[0].computation
        # 30 ÷ 4.2 = 7.142857；每次 3.571429 按 0.4 步长入到 3.6；日量 7.2
        self.assertEqual(comp.exact_daily_grams, Decimal("7.142857"))
        self.assertEqual(comp.dispensed_per_portion_grams, Decimal("3.6"))
        self.assertEqual(comp.dispensed_daily_grams, Decimal("7.200000"))
        self.assertEqual(comp.rounding_delta_grams, Decimal("0.057143"))
        # 差额 = 调配量 − 精确值，且在公式文本中可见
        self.assertEqual(
            comp.rounding_delta_grams,
            comp.dispensed_daily_grams - comp.exact_daily_grams,
        )
        self.assertIn("+0.057143", comp.formula_text)
        self.assertIn("std-b", comp.formula_text)

    def test_comparison_report_shows_everything(self) -> None:
        records = [self.convert(rx) for rx in self.fixture["prescriptions"]]
        report = self.service.compare([r.record_id for r in records])
        text = report.render_text()
        for needle in (
            "std-a", "std-b", "当量系数", "舍入差额", "公式", "前后对照",
            "6.000000", "7.200000",
        ):
            self.assertIn(needle, text)


class RoundingTest(unittest.TestCase):
    def test_portions_affect_result(self) -> None:
        service = make_service(make_standard("std-a", ratio="4.2", step="0.4"))
        day = date(2026, 9, 16)
        snap1 = make_snapshot(service, "rx-p1", portions=1)
        snap2 = make_snapshot(service, "rx-p2", portions=2)
        r1 = service.create_dispensing(snap1, day, created_by="p")
        r2 = service.create_dispensing(snap2, day, created_by="p")
        # 每日 1 次：7.142857 → 7.2（步长 0.4，18 步）
        self.assertEqual(r1.lines[0].final.dispensed_daily_grams, Decimal("7.2"))
        # 每日 2 次：每次 3.571429 → 3.6，日量 7.2
        self.assertEqual(r2.lines[0].final.dispensed_daily_grams, Decimal("7.200000"))

    def test_round_to_step_half_up(self) -> None:
        self.assertEqual(
            round_to_step(Decimal("0.25"), Decimal("0.5")), Decimal("0.5")
        )
        self.assertEqual(
            round_to_step(Decimal("0.24"), Decimal("0.5")), Decimal("0.0")
        )
        self.assertEqual(
            round_to_step(Decimal("1.3"), Decimal("0.4")), Decimal("1.2")
        )

    def test_dispensed_amount_not_silently_reverted(self) -> None:
        # 调配量保留换算结果（7.2），差额显式记录，不回改成整数或原处方量
        service = make_service(make_standard("std-a", ratio="4.2", step="0.4"))
        snapshot_id = make_snapshot(service, "rx-x")
        record = service.create_dispensing(
            snapshot_id, date(2026, 9, 16), created_by="p"
        )
        final = record.lines[0].final
        self.assertEqual(final.dispensed_daily_grams, Decimal("7.200000"))
        self.assertNotEqual(final.rounding_delta_grams, Decimal("0"))
        contract = record.contract_records()[0]
        self.assertEqual(contract.dispensed_grams, Decimal("7.200000"))
        self.assertEqual(contract.rounding_delta_grams, Decimal("0.057143"))


class StandardCorrectionTest(unittest.TestCase):
    """标准更正不追改已审核单，只影响新单与药师重算版本。"""

    def setUp(self) -> None:
        self.service = make_service(
            make_standard(
                "std-b", valid_from=date(2026, 9, 16), ratio="4.2", step="0.4"
            )
        )
        self.snapshot_id = make_snapshot(self.service, "rx-c")
        self.day = date(2026, 9, 17)

    def correct_standard(self) -> None:
        self.service.registry.correct(
            make_standard(
                "std-b-v2", valid_from=date(2026, 9, 16), ratio="4.0", step="0.4"
            ),
            supersedes="std-b",
        )

    def test_correction_does_not_touch_approved_record(self) -> None:
        record = self.service.create_dispensing(
            self.snapshot_id, self.day, created_by="p"
        )
        approved = self.service.approve(record.record_id, pharmacist_id="ph")
        self.correct_standard()

        kept = self.service.explain(approved.record_id)
        self.assertEqual(kept.lines[0].dispensed_grams, Decimal("7.200000"))
        self.assertEqual(kept.lines[0].standard_id, "std-b")
        self.assertEqual(kept.status, RecordStatus.APPROVED)

    def test_correction_applies_to_new_dispensing(self) -> None:
        self.correct_standard()
        record = self.service.create_dispensing(
            self.snapshot_id, self.day, created_by="p"
        )
        line = record.lines[0]
        self.assertEqual(line.standard.standard_id, "std-b-v2")
        # 30 ÷ 4.0 = 7.5；每次 3.75 → 步长 0.4 → 3.6；日量 7.2
        self.assertEqual(line.computation.exact_daily_grams, Decimal("7.500000"))
        self.assertEqual(line.final.dispensed_daily_grams, Decimal("7.200000"))

    def test_pharmacist_recalc_creates_new_version(self) -> None:
        record = self.service.create_dispensing(
            self.snapshot_id, self.day, created_by="p"
        )
        self.service.approve(record.record_id, pharmacist_id="ph")
        self.correct_standard()

        recalc = self.service.recalculate(
            record.record_id,
            pharmacist_id="ph",
            reason="系数更正",
            request_key="recalc-1",
        )
        self.assertEqual(recalc.version, 2)
        self.assertEqual(recalc.supersedes_record_id, record.record_id)
        self.assertEqual(recalc.lines[0].standard.standard_id, "std-b-v2")
        # 原版本保持原样
        original = self.service.explain(record.record_id)
        self.assertEqual(original.lines[0].standard_id, "std-b")
        self.assertEqual(original.status, RecordStatus.APPROVED)

    def test_repeated_recalc_returns_same_version(self) -> None:
        record = self.service.create_dispensing(
            self.snapshot_id, self.day, created_by="p"
        )
        self.correct_standard()
        first = self.service.recalculate(
            record.record_id, pharmacist_id="ph", reason="r", request_key="k-1"
        )
        second = self.service.recalculate(
            record.record_id, pharmacist_id="ph", reason="r", request_key="k-1"
        )
        self.assertIs(first, second)
        self.assertEqual(first.version, 2)

    def test_recalc_keeps_original_dispensing_date(self) -> None:
        record = self.service.create_dispensing(
            self.snapshot_id, self.day, created_by="p"
        )
        recalc = self.service.recalculate(
            record.record_id, pharmacist_id="ph", reason="r", request_key="k-2"
        )
        self.assertEqual(recalc.dispensing_date, self.day)


class ManualReviewTest(unittest.TestCase):
    def test_missing_standard_goes_to_review_then_override(self) -> None:
        service = make_service()  # 无任何标准
        snapshot_id = make_snapshot(service, "rx-m", herb_code="党参")
        record = service.create_dispensing(
            snapshot_id, date(2026, 9, 17), created_by="p"
        )
        line = record.lines[0]
        self.assertEqual(line.status, LineStatus.PENDING_REVIEW)
        self.assertEqual(line.flags[0].reason, ReviewReason.MISSING_STANDARD)
        self.assertEqual(record.status, RecordStatus.PENDING_REVIEW)

        with self.assertRaises(PendingReviewError):
            service.approve(record.record_id, pharmacist_id="ph")

        record = service.submit_decision(
            record.record_id,
            line.line_id,
            pharmacist_id="ph",
            action=DecisionAction.OVERRIDE_AMOUNT,
            rationale="医院临时目录按 6g 调配",
            override_grams=Decimal("6"),
        )
        line = record.lines[0]
        self.assertEqual(line.status, LineStatus.RESOLVED)
        self.assertEqual(line.final.dispensed_daily_grams, Decimal("6"))
        self.assertIsNone(line.final.rounding_delta_grams)  # 无精确基准
        self.assertEqual(record.status, RecordStatus.READY)

        approved = service.approve(record.record_id, pharmacist_id="ph")
        self.assertEqual(approved.status, RecordStatus.APPROVED)
        self.assertEqual(approved.approved_at, NOW)
        contract = approved.contract_records()[0]
        self.assertEqual(contract.reviewed_at, NOW)
        self.assertEqual(contract.standard_id, "MANUAL")

    def test_exceeds_max_daily_goes_to_review(self) -> None:
        service = make_service(
            make_standard("std-a", ratio="1.0", step="0.5", max_daily="10")
        )
        snapshot_id = make_snapshot(service, "rx-e", grams="30")
        record = service.create_dispensing(
            snapshot_id, date(2026, 9, 17), created_by="p"
        )
        line = record.lines[0]
        self.assertEqual(line.status, LineStatus.PENDING_REVIEW)
        self.assertEqual(line.flags[0].reason, ReviewReason.EXCEEDS_MAX_DAILY)

        record = service.submit_decision(
            record.record_id,
            line.line_id,
            pharmacist_id="ph",
            action=DecisionAction.ACCEPT_COMPUTED,
            rationale="医师确认超量使用，监测肝肾功能",
        )
        self.assertEqual(
            record.lines[0].final.dispensed_daily_grams, Decimal("30.000000")
        )

    def test_substitute_standard_decision(self) -> None:
        service = make_service()  # 注册表为空 → 缺少对应品种
        snapshot_id = make_snapshot(service, "rx-s", herb_code="党参", grams="30")
        record = service.create_dispensing(
            snapshot_id, date(2026, 9, 17), created_by="p"
        )
        self.assertEqual(
            record.lines[0].flags[0].reason, ReviewReason.MISSING_STANDARD
        )
        # 药师指定一个已批准的替代标准
        service.registry.register(
            make_standard("std-sub", herb_code="党参", ratio="2.0", step="0.5")
        )
        record = service.submit_decision(
            record.record_id,
            "rx-s-1",
            pharmacist_id="ph",
            action=DecisionAction.SUBSTITUTE_STANDARD,
            rationale="选用已批准的党参替代标准",
            substitute_standard_id="std-sub",
        )
        line = record.lines[0]
        self.assertEqual(line.status, LineStatus.RESOLVED)
        # 30 ÷ 2.0 = 15；每次 7.5 → 步长 0.5 → 7.5；日量 15
        self.assertEqual(line.final.dispensed_daily_grams, Decimal("15.000000"))
        self.assertEqual(line.final.standard_id, "std-sub")
        self.assertIn("替代标准", line.final.formula_text)

    def test_reject_line(self) -> None:
        service = make_service()
        snapshot_id = make_snapshot(service, "rx-r", herb_code="西洋参")
        record = service.create_dispensing(
            snapshot_id, date(2026, 9, 17), created_by="p"
        )
        record = service.submit_decision(
            record.record_id,
            "rx-r-1",
            pharmacist_id="ph",
            action=DecisionAction.REJECT_LINE,
            rationale="无对应品种，退回医师改方",
        )
        self.assertEqual(record.lines[0].status, LineStatus.REJECTED)
        self.assertIsNone(record.lines[0].final)
        approved = service.approve(record.record_id, pharmacist_id="ph")
        self.assertEqual(approved.contract_records(), [])

    def test_approved_record_is_frozen(self) -> None:
        service = make_service()
        snapshot_id = make_snapshot(service, "rx-f", herb_code="党参")
        record = service.create_dispensing(
            snapshot_id, date(2026, 9, 17), created_by="p"
        )
        record = service.submit_decision(
            record.record_id,
            "rx-f-1",
            pharmacist_id="ph",
            action=DecisionAction.OVERRIDE_AMOUNT,
            rationale="核定",
            override_grams=Decimal("6"),
        )
        approved = service.approve(record.record_id, pharmacist_id="ph")
        with self.assertRaises(RecordFrozenError):
            service.submit_decision(
                approved.record_id,
                "rx-f-1",
                pharmacist_id="ph",
                action=DecisionAction.OVERRIDE_AMOUNT,
                rationale="试图追改",
                override_grams=Decimal("9"),
            )


class InterchangeabilityTest(unittest.TestCase):
    """续方/重算跨厂家且不可互换时进入人工审核。"""

    def build(self, new_group: str | None) -> GranuleConversionService:
        return make_service(
            make_standard(
                "std-old",
                manufacturer_id="mfr-old",
                valid_until=date(2026, 9, 15),
                group="grp-1",
            ),
            make_standard(
                "std-new",
                manufacturer_id="mfr-new",
                valid_from=date(2026, 9, 16),
                group=new_group,
            ),
        )

    def test_refill_across_switch_flags_review(self) -> None:
        service = self.build(new_group="grp-2")
        snapshot_id = make_snapshot(service, "rx-i")
        v1 = service.create_dispensing(
            snapshot_id, date(2026, 9, 15), created_by="p"
        )
        service.approve(v1.record_id, pharmacist_id="ph")
        # 同一处方续方，调配日跨切换日：厂家不同且互换组不同 → 人工审核
        v2 = service.create_dispensing(
            snapshot_id, date(2026, 9, 16), created_by="p"
        )
        line = v2.lines[0]
        self.assertEqual(line.status, LineStatus.PENDING_REVIEW)
        self.assertEqual(
            line.flags[0].reason, ReviewReason.NON_INTERCHANGEABLE_MANUFACTURER
        )
        self.assertEqual(line.standard.standard_id, "std-new")
        # 药师确认两厂家产品可接续使用后放行
        v2 = service.submit_decision(
            v2.record_id,
            line.line_id,
            pharmacist_id="ph",
            action=DecisionAction.ACCEPT_COMPUTED,
            rationale="已向患者说明厂家变更，继续调配",
        )
        self.assertEqual(v2.lines[0].status, LineStatus.RESOLVED)

    def test_same_interchangeable_group_passes(self) -> None:
        service = self.build(new_group="grp-1")
        snapshot_id = make_snapshot(service, "rx-g")
        service.create_dispensing(snapshot_id, date(2026, 9, 15), created_by="p")
        v2 = service.create_dispensing(
            snapshot_id, date(2026, 9, 16), created_by="p"
        )
        self.assertEqual(v2.lines[0].status, LineStatus.AUTO_CONVERTED)

    def test_first_dispensing_has_no_history_check(self) -> None:
        service = self.build(new_group=None)
        snapshot_id = make_snapshot(service, "rx-h")
        record = service.create_dispensing(
            snapshot_id, date(2026, 9, 16), created_by="p"
        )
        self.assertEqual(record.lines[0].status, LineStatus.AUTO_CONVERTED)

    def test_correction_with_new_manufacturer_flags_recalc(self) -> None:
        service = make_service(
            make_standard(
                "std-b", valid_from=date(2026, 9, 16), ratio="4.2", step="0.4"
            )
        )
        snapshot_id = make_snapshot(service, "rx-j")
        v1 = service.create_dispensing(
            snapshot_id, date(2026, 9, 17), created_by="p"
        )
        service.approve(v1.record_id, pharmacist_id="ph")
        # 更正版本换了厂家且不可互换 → 药师重算时进入人工审核
        service.registry.correct(
            make_standard(
                "std-b-v2",
                manufacturer_id="mfr-c",
                valid_from=date(2026, 9, 16),
                ratio="4.0",
                step="0.4",
            ),
            supersedes="std-b",
        )
        v2 = service.recalculate(
            v1.record_id, pharmacist_id="ph", reason="标准更正", request_key="k-j"
        )
        line = v2.lines[0]
        self.assertEqual(line.status, LineStatus.PENDING_REVIEW)
        self.assertEqual(
            line.flags[0].reason, ReviewReason.NON_INTERCHANGEABLE_MANUFACTURER
        )


class IdempotencyTest(unittest.TestCase):
    def test_duplicate_create_returns_same_record(self) -> None:
        service = make_service(make_standard("std-a"))
        snapshot_id = make_snapshot(service, "rx-d")
        day = date(2026, 9, 17)
        first = service.create_dispensing(
            snapshot_id, day, created_by="p", request_key="create-1"
        )
        second = service.create_dispensing(
            snapshot_id, day, created_by="p", request_key="create-1"
        )
        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()
