"""配方颗粒等量换算与审核服务。

核心规则：

- 处方开具后即成为不可变快照（医师开具的饮片名与剂量原样保留），
  换算只引用快照，从不回写处方。
- 产品标准只增不改；标准更正以新标准注册，只影响新处方，
  或由药师主动发起重算生成新的版本，已审核调配单不被追改。
- 每次换算/重算生成新的 :class:`ConversionVersion`，历史版本永不修改。
- 重复的重算请求（相同 ``request_key``）返回同一版本，不产生新版本。
- 缺少对应品种、超过最大日用量安全边界、不同厂家且不可互换时，
  不自动出数，进入人工审核；药师决定（含理由）全程留痕。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Callable

from products.catalog import StandardCatalog
from products.contracts import ConversionRecord, DecoctionDose, GranuleStandard
from products.conversion import convert_line


class ServiceError(ValueError):
    """服务层通用错误。"""


class PendingReviewError(ServiceError):
    """尚有未处理的人工审核，不能确认调配单。"""


class ReviewReason(str, Enum):
    MISSING_STANDARD = "missing_standard"  # 缺少对应品种的已批准标准
    EXCEEDS_MAX_DAILY = "exceeds_max_daily"  # 超过标准最大日用量安全边界
    ROUNDED_TO_ZERO = "rounded_to_zero"  # 按包装步长舍入后为零，存在用药安全风险
    NON_INTERCHANGEABLE = "non_interchangeable"  # 不同厂家且互换组不一致，不可自动替代


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Prescription:
    """医师处方快照：饮片名与剂量一经开具不再修改。"""

    prescription_id: str
    lines: tuple[DecoctionDose, ...]
    prescribed_by: str
    prescribed_at: datetime


@dataclass(frozen=True)
class ReviewDecision:
    """药师对审核案件的决定。理由必填，决定留痕。"""

    pharmacist_id: str
    action: str  # "approve" 或 "reject"
    rationale: str
    decided_at: datetime
    standard_id: str | None = None  # 药师另行指定的产品标准
    approved_grams: Decimal | None = None  # 药师裁定的日调配量（缺省用换算结果）


@dataclass(frozen=True)
class ReviewCase:
    """一个人工审核案件；案件对象不可变，状态变化以替换方式登记。"""

    case_id: str
    prescription_id: str
    version_no: int
    line_id: str
    herb_code: str
    reason: ReviewReason
    detail: str
    candidate_standard_id: str | None
    status: ReviewStatus = ReviewStatus.PENDING
    decision: ReviewDecision | None = None


@dataclass(frozen=True)
class VersionLine:
    """版本中一味药的结果：要么自动换算出记录，要么挂起人工审核。"""

    line_id: str
    record: ConversionRecord | None
    review_case_id: str | None


@dataclass(frozen=True)
class ConversionVersion:
    """一次换算/重算的产物。版本创建后不可变；重算产生新版本。"""

    prescription_id: str
    version_no: int
    dispensing_date: date
    request_key: str
    initiated_by: str
    reason: str
    created_at: datetime
    lines: tuple[VersionLine, ...]

    @property
    def records(self) -> tuple[ConversionRecord, ...]:
        return tuple(line.record for line in self.lines if line.record is not None)

    @property
    def review_case_ids(self) -> tuple[str, ...]:
        return tuple(line.review_case_id for line in self.lines if line.review_case_id is not None)


@dataclass(frozen=True)
class DispensingLineView:
    """调配单上一味药的当前有效状态。"""

    line_id: str
    source_name: str
    decoction_grams: Decimal
    status: str  # converted / manual_approved / pending_review / rejected
    record: ConversionRecord | None
    review_case: ReviewCase | None


@dataclass(frozen=True)
class DispensingSheet:
    """处方当前有效调配单（最新版本 + 已批准的人工决定）。"""

    prescription_id: str
    version_no: int
    dispensing_date: date
    confirmed: bool
    lines: tuple[DispensingLineView, ...]


class GranuleConversionService:
    """换算与审核服务。所有状态只允许追加，不允许改写历史。"""

    def __init__(self, catalog: StandardCatalog, clock: Callable[[], datetime] | None = None) -> None:
        self._catalog = catalog
        self._clock = clock or datetime.now
        self._prescriptions: dict[str, Prescription] = {}
        self._versions: dict[str, list[ConversionVersion]] = {}
        self._by_request_key: dict[str, ConversionVersion] = {}
        self._review_cases: dict[str, ReviewCase] = {}
        self._manual_records: dict[str, ConversionRecord] = {}
        self._confirmed: set[tuple[str, int]] = set()
        self._case_seq = itertools.count(1)

    # ------------------------------------------------------------------ 处方

    def register_prescription(self, prescription: Prescription) -> Prescription:
        """登记处方快照；同一 id 重复登记同一内容时幂等返回。"""
        existing = self._prescriptions.get(prescription.prescription_id)
        if existing is not None:
            if existing != prescription:
                raise ServiceError(f"处方 {prescription.prescription_id} 已登记，处方内容不可更改")
            return existing
        if not prescription.lines:
            raise ServiceError("处方至少包含一味药")
        self._prescriptions[prescription.prescription_id] = prescription
        return prescription

    # ------------------------------------------------------------------ 换算

    def convert(
        self,
        prescription_id: str,
        dispensing_date: date,
        *,
        request_key: str,
        initiated_by: str,
        reason: str = "首次换算",
    ) -> ConversionVersion:
        """按调配日期选择已批准标准，对整张处方（合方）逐味换算。"""
        prescription = self._require_prescription(prescription_id)
        return self._new_version(
            prescription,
            dispensing_date,
            request_key=request_key,
            initiated_by=initiated_by,
            reason=reason,
        )

    def recalculate(
        self,
        prescription_id: str,
        *,
        request_key: str,
        initiated_by: str,
        reason: str,
        dispensing_date: date | None = None,
    ) -> ConversionVersion:
        """药师主动发起的重算：生成新版本，历史版本（含已审核的）不受影响。

        相同 ``request_key`` 的重复请求返回同一版本。
        """
        prescription = self._require_prescription(prescription_id)
        prior = self._versions.get(prescription_id, [])
        if not prior:
            raise ServiceError(f"处方 {prescription_id} 尚无换算版本，请使用 convert 首次换算")
        if dispensing_date is None:
            dispensing_date = prior[-1].dispensing_date
        return self._new_version(
            prescription,
            dispensing_date,
            request_key=request_key,
            initiated_by=initiated_by,
            reason=reason,
        )

    def _new_version(
        self,
        prescription: Prescription,
        dispensing_date: date,
        *,
        request_key: str,
        initiated_by: str,
        reason: str,
    ) -> ConversionVersion:
        existing = self._by_request_key.get(request_key)
        if existing is not None:
            return existing

        prior = self._versions.get(prescription.prescription_id, [])
        version_no = len(prior) + 1
        lines: list[VersionLine] = []
        for dose in prescription.lines:
            standard = self._catalog.find(dose.herb_code, dispensing_date)
            if standard is None:
                case = self._open_case(
                    prescription.prescription_id,
                    version_no,
                    dose,
                    ReviewReason.MISSING_STANDARD,
                    f"{dispensing_date} 无已批准的 {dose.herb_code}（{dose.source_name}）颗粒标准，缺少对应品种",
                    candidate_standard_id=None,
                )
                lines.append(VersionLine(dose.line_id, None, case.case_id))
                continue

            conversion = convert_line(dose, standard)
            interchange_detail = self._interchange_problem(prescription.prescription_id, dose.line_id, standard)
            if interchange_detail is not None:
                case = self._open_case(
                    prescription.prescription_id,
                    version_no,
                    dose,
                    ReviewReason.NON_INTERCHANGEABLE,
                    interchange_detail,
                    candidate_standard_id=standard.standard_id,
                )
                lines.append(VersionLine(dose.line_id, None, case.case_id))
                continue
            if conversion.exceeds_max_daily:
                case = self._open_case(
                    prescription.prescription_id,
                    version_no,
                    dose,
                    ReviewReason.EXCEEDS_MAX_DAILY,
                    f"换算 {conversion.exact_grams}g/日、步长取整后 {conversion.dispensed_grams}g/日，"
                    f"超过标准 {standard.standard_id} 最大日用量 {standard.maximum_daily_grams}g",
                    candidate_standard_id=standard.standard_id,
                )
                lines.append(VersionLine(dose.line_id, None, case.case_id))
                continue
            if conversion.rounds_to_zero:
                case = self._open_case(
                    prescription.prescription_id,
                    version_no,
                    dose,
                    ReviewReason.ROUNDED_TO_ZERO,
                    f"每次换算 {conversion.portion_exact_grams}g 按步长 {standard.package_step_grams}g "
                    f"舍入后为零，存在用药安全风险",
                    candidate_standard_id=standard.standard_id,
                )
                lines.append(VersionLine(dose.line_id, None, case.case_id))
                continue

            record = ConversionRecord(
                conversion_id=f"{prescription.prescription_id}-v{version_no}-{dose.line_id}",
                prescription_snapshot_id=prescription.prescription_id,
                dispensing_date=dispensing_date,
                standard_id=standard.standard_id,
                exact_grams=conversion.exact_grams,
                dispensed_grams=conversion.dispensed_grams,
                rounding_delta_grams=conversion.rounding_delta_grams,
                formula_text=conversion.formula_text,
                reviewed_at=None,
            )
            lines.append(VersionLine(dose.line_id, record, None))

        version = ConversionVersion(
            prescription_id=prescription.prescription_id,
            version_no=version_no,
            dispensing_date=dispensing_date,
            request_key=request_key,
            initiated_by=initiated_by,
            reason=reason,
            created_at=self._clock(),
            lines=tuple(lines),
        )
        self._versions.setdefault(prescription.prescription_id, []).append(version)
        self._by_request_key[request_key] = version
        return version

    def _interchange_problem(self, prescription_id: str, line_id: str, new_standard: GranuleStandard) -> str | None:
        """同一处方同一味药既往用其他厂家产品且互换组不一致时，返回问题说明。"""
        previous = self._latest_effective_record(prescription_id, line_id)
        if previous is None:
            return None
        previous_standard = self._catalog.get(previous.standard_id)
        if previous_standard is None or previous_standard.manufacturer_id == new_standard.manufacturer_id:
            return None
        group_old, group_new = previous_standard.interchangeable_group, new_standard.interchangeable_group
        if group_old is not None and group_old == group_new:
            return None
        return (
            f"既往使用 {previous_standard.standard_id}（厂家 {previous_standard.manufacturer_id}），"
            f"本次标准为 {new_standard.standard_id}（厂家 {new_standard.manufacturer_id}），"
            f"互换组 {group_old} → {group_new}，不同厂家不可自动互换"
        )

    def _latest_effective_record(self, prescription_id: str, line_id: str) -> ConversionRecord | None:
        for version in reversed(self._versions.get(prescription_id, [])):
            for line in version.lines:
                if line.line_id != line_id:
                    continue
                if line.review_case_id is not None:
                    manual = self._manual_records.get(line.review_case_id)
                    if manual is not None:
                        return manual
                if line.record is not None:
                    return line.record
        return None

    # ------------------------------------------------------------------ 审核

    def _open_case(
        self,
        prescription_id: str,
        version_no: int,
        dose: DecoctionDose,
        reason: ReviewReason,
        detail: str,
        candidate_standard_id: str | None,
    ) -> ReviewCase:
        case = ReviewCase(
            case_id=f"case-{next(self._case_seq)}",
            prescription_id=prescription_id,
            version_no=version_no,
            line_id=dose.line_id,
            herb_code=dose.herb_code,
            reason=reason,
            detail=detail,
            candidate_standard_id=candidate_standard_id,
        )
        self._review_cases[case.case_id] = case
        return case

    def resolve_review(self, case_id: str, decision: ReviewDecision) -> ReviewCase:
        """药师处理审核案件。批准后按决定生成换算记录并留痕。"""
        case = self._review_cases.get(case_id)
        if case is None:
            raise ServiceError(f"审核案件 {case_id} 不存在")
        if case.status is not ReviewStatus.PENDING:
            raise ServiceError(f"审核案件 {case_id} 已处理，不得重复处理")
        if decision.action not in ("approve", "reject"):
            raise ServiceError("审核决定只能是 approve 或 reject")
        if not decision.rationale.strip():
            raise ServiceError("人工决定必须填写理由")

        status = ReviewStatus.APPROVED if decision.action == "approve" else ReviewStatus.REJECTED
        updated = replace(case, status=status, decision=decision)
        self._review_cases[case_id] = updated

        if status is ReviewStatus.APPROVED:
            self._manual_records[case_id] = self._build_manual_record(updated, decision)
        return updated

    def _build_manual_record(self, case: ReviewCase, decision: ReviewDecision) -> ConversionRecord:
        prescription = self._require_prescription(case.prescription_id)
        dose = self._require_line(prescription, case.line_id)
        version = self.versions(case.prescription_id)[case.version_no - 1]

        standard_id = decision.standard_id or case.candidate_standard_id
        if standard_id is None:
            raise ServiceError("批准时必须指定采用的产品标准（standard_id）")
        standard = self._catalog.get(standard_id)
        if standard is None:
            raise ServiceError(f"标准 {standard_id} 未在目录中注册")
        if standard.herb_code != case.herb_code:
            raise ServiceError(f"标准 {standard_id} 不属于品种 {case.herb_code}")

        conversion = convert_line(dose, standard)
        dispensed = decision.approved_grams if decision.approved_grams is not None else conversion.dispensed_grams
        delta = dispensed - conversion.exact_grams
        formula = conversion.formula_text
        if decision.approved_grams is not None and decision.approved_grams != conversion.dispensed_grams:
            formula += f"；药师裁定日调配量 {dispensed}g"
        formula += f"；人工审核[{decision.pharmacist_id} {decision.decided_at:%Y-%m-%d %H:%M}]：{decision.rationale}"

        return ConversionRecord(
            conversion_id=f"{case.prescription_id}-v{case.version_no}-{case.line_id}-manual",
            prescription_snapshot_id=case.prescription_id,
            dispensing_date=version.dispensing_date,
            standard_id=standard.standard_id,
            exact_grams=conversion.exact_grams,
            dispensed_grams=dispensed,
            rounding_delta_grams=delta,
            formula_text=formula,
            reviewed_at=decision.decided_at,
        )

    def confirm_version(self, prescription_id: str, version_no: int, pharmacist_id: str) -> None:
        """药师确认某版本为已审核调配单；确认后该版本永不被修改。

        前提：该版本没有仍处于 pending 状态的审核案件。
        """
        version = self._require_version(prescription_id, version_no)
        pending = [
            self._review_cases[case_id]
            for case_id in version.review_case_ids
            if self._review_cases[case_id].status is ReviewStatus.PENDING
        ]
        if pending:
            raise PendingReviewError(
                f"版本 v{version_no} 尚有 {len(pending)} 个未处理审核案件："
                + "、".join(case.case_id for case in pending)
            )
        self._confirmed.add((prescription_id, version_no))

    # ------------------------------------------------------------------ 查询

    def versions(self, prescription_id: str) -> tuple[ConversionVersion, ...]:
        return tuple(self._versions.get(prescription_id, ()))

    def get_case(self, case_id: str) -> ReviewCase:
        case = self._review_cases.get(case_id)
        if case is None:
            raise ServiceError(f"审核案件 {case_id} 不存在")
        return case

    def pending_cases(self) -> tuple[ReviewCase, ...]:
        return tuple(case for case in self._review_cases.values() if case.status is ReviewStatus.PENDING)

    def dispensing_sheet(self, prescription_id: str) -> DispensingSheet:
        """当前有效调配单：最新版本 + 已批准的人工决定。"""
        prescription = self._require_prescription(prescription_id)
        version = self._require_latest_version(prescription_id)
        views: list[DispensingLineView] = []
        for dose in prescription.lines:
            version_line = next(line for line in version.lines if line.line_id == dose.line_id)
            if version_line.record is not None:
                views.append(
                    DispensingLineView(dose.line_id, dose.source_name, dose.daily_decoction_grams,
                                       "converted", version_line.record, None)
                )
                continue
            case = self._review_cases[version_line.review_case_id]
            if case.status is ReviewStatus.APPROVED:
                views.append(
                    DispensingLineView(dose.line_id, dose.source_name, dose.daily_decoction_grams,
                                       "manual_approved", self._manual_records[case.case_id], case)
                )
            elif case.status is ReviewStatus.REJECTED:
                views.append(
                    DispensingLineView(dose.line_id, dose.source_name, dose.daily_decoction_grams,
                                       "rejected", None, case)
                )
            else:
                views.append(
                    DispensingLineView(dose.line_id, dose.source_name, dose.daily_decoction_grams,
                                       "pending_review", None, case)
                )
        return DispensingSheet(
            prescription_id=prescription_id,
            version_no=version.version_no,
            dispensing_date=version.dispensing_date,
            confirmed=(prescription_id, version.version_no) in self._confirmed,
            lines=tuple(views),
        )

    # ------------------------------------------------------------------ 追溯

    def explain_prescription(self, prescription_id: str) -> str:
        """输出某处方全部版本的换算依据：公式、标准来源、舍入差额与人工决定。"""
        prescription = self._require_prescription(prescription_id)
        out = [
            f"处方 {prescription.prescription_id}"
            f"（医师 {prescription.prescribed_by} 开具于 {prescription.prescribed_at:%Y-%m-%d %H:%M}，"
            f"饮片名与剂量为开具快照，不再改动）"
        ]
        for version in self.versions(prescription_id):
            confirmed = "｜已审核确认" if (prescription_id, version.version_no) in self._confirmed else ""
            out.append(
                f"版本 v{version.version_no}｜调配日 {version.dispensing_date}"
                f"｜发起 {version.initiated_by}｜原因 {version.reason}｜请求键 {version.request_key}{confirmed}"
            )
            for dose in prescription.lines:
                out.append(f"  行 {dose.line_id}：{dose.source_name} {dose.daily_decoction_grams}g/日，"
                           f"分 {dose.portions_per_day} 次服用")
                version_line = next(line for line in version.lines if line.line_id == dose.line_id)
                if version_line.record is not None:
                    out.extend(self._explain_record(version_line.record, indent="    "))
                else:
                    out.extend(self._explain_case(version_line.review_case_id, indent="    "))
        return "\n".join(out)

    def _explain_record(self, record: ConversionRecord, indent: str) -> list[str]:
        standard = self._catalog.get(record.standard_id)
        if standard is not None:
            until = f"，{standard.valid_until} 止" if standard.valid_until else "，现行"
            source = (
                f"标准来源：{standard.standard_id}（厂家 {standard.manufacturer_id}，"
                f"{standard.valid_from} 起生效{until}，提取比 {standard.extract_ratio}，"
                f"包装步长 {standard.package_step_grams}g，最大日用量 {standard.maximum_daily_grams}g）"
            )
        else:
            source = f"标准来源：{record.standard_id}（目录中未找到）"
        reviewed = f"，人工复核于 {record.reviewed_at:%Y-%m-%d %H:%M}" if record.reviewed_at else ""
        return [
            indent + source,
            indent + f"公式：{record.formula_text}",
            indent + f"结果：换算值 {record.exact_grams}g → 调配 {record.dispensed_grams}g，"
                     f"舍入差额 {record.rounding_delta_grams:+}g（差额保留展示，不回改原处方量{reviewed}）",
        ]

    def _explain_case(self, case_id: str, indent: str) -> list[str]:
        case = self._review_cases[case_id]
        lines = [indent + f"人工审核[{case.reason.value}]（{case.case_id}，状态 {case.status.value}）：{case.detail}"]
        if case.decision is not None:
            decision = case.decision
            lines.append(
                indent + f"人工决定：{decision.pharmacist_id} 于 {decision.decided_at:%Y-%m-%d %H:%M} "
                f"{'批准' if case.status is ReviewStatus.APPROVED else '驳回'}：{decision.rationale}"
            )
            manual = self._manual_records.get(case_id)
            if manual is not None:
                lines.extend(self._explain_record(manual, indent=indent + "  "))
        return lines

    # ------------------------------------------------------------------ 内部

    def _require_prescription(self, prescription_id: str) -> Prescription:
        prescription = self._prescriptions.get(prescription_id)
        if prescription is None:
            raise ServiceError(f"处方 {prescription_id} 未登记")
        return prescription

    @staticmethod
    def _require_line(prescription: Prescription, line_id: str) -> DecoctionDose:
        for dose in prescription.lines:
            if dose.line_id == line_id:
                return dose
        raise ServiceError(f"处方 {prescription.prescription_id} 无行 {line_id}")

    def _require_version(self, prescription_id: str, version_no: int) -> ConversionVersion:
        for version in self._versions.get(prescription_id, []):
            if version.version_no == version_no:
                return version
        raise ServiceError(f"处方 {prescription_id} 无版本 v{version_no}")

    def _require_latest_version(self, prescription_id: str) -> ConversionVersion:
        versions = self._versions.get(prescription_id, [])
        if not versions:
            raise ServiceError(f"处方 {prescription_id} 尚无换算版本")
        return versions[-1]
