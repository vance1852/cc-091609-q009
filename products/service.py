"""配方颗粒等量换算与审核服务。

核心不变式：
- 处方快照保留医师开具的饮片名与剂量，换算过程永不回改；
- 每次调配按「调配日期」选择当时已批准的产品标准，结果连同公式、
  标准来源、舍入差额一起固化在调配单版本里；
- 标准更正不追改任何已存在的调配单（尤其已审核单），只影响新单
  或药师主动发起的重算版本；重算以 request_key 幂等，重复请求返回同一版本；
- 缺少品种标准、超过最大日用量、厂家不可互换时进入人工审核，
  药师的每个决定都留痕。
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from .contracts import ConversionRecord, DecoctionDose, GranuleStandard
from .conversion import LineComputation, compute_line
from .review import DecisionAction, ManualDecision, ReviewFlag, ReviewReason
from .standards import StandardRegistry


class RecordFrozenError(RuntimeError):
    """已审核调配单不可再更改。"""


class PendingReviewError(RuntimeError):
    """存在未处理的人工审核项。"""


@dataclass(frozen=True)
class PrescriptionSnapshot:
    """医师开具的处方快照：饮片名与剂量保持原样，是换算的唯一输入。"""

    snapshot_id: str
    prescription_id: str
    physician_name: str
    patient_ref: str
    created_at: datetime
    lines: tuple[DecoctionDose, ...]


class LineStatus(str, Enum):
    AUTO_CONVERTED = "AUTO_CONVERTED"  # 自动换算通过
    PENDING_REVIEW = "PENDING_REVIEW"  # 待人工审核
    RESOLVED = "RESOLVED"  # 药师已处理
    REJECTED = "REJECTED"  # 药师退回（不调配）


class RecordStatus(str, Enum):
    PENDING_REVIEW = "PENDING_REVIEW"  # 存在待审核行
    READY = "READY"  # 全部行已有结果，待药师审核签发
    APPROVED = "APPROVED"  # 已审核（自此不可变）


@dataclass(frozen=True)
class FinalAmount:
    """一行最终的生效结果（自动换算或药师决定之后）。"""

    dispensed_daily_grams: Decimal
    #: 相对精确日量的舍入差额；缺少标准而无精确基准时为 None。
    rounding_delta_grams: Decimal | None
    formula_text: str
    standard_id: str | None


@dataclass(frozen=True)
class DispensingLine:
    """调配单中的一味药。"""

    line_id: str
    dose: DecoctionDose
    status: LineStatus
    #: 系统自动选择的标准快照（标准来源）；缺品种时为 None。
    standard: GranuleStandard | None
    #: 系统自动换算的原始尝试（含完整公式），即使进入人工审核也保留。
    computation: LineComputation | None
    flags: tuple[ReviewFlag, ...]
    decision: ManualDecision | None
    #: 生效结果；待审核或被退回时为 None。
    final: FinalAmount | None


@dataclass(frozen=True)
class DispensingRecord:
    """一张调配单版本。创建后内容不可变；审核只追加签发信息。"""

    record_id: str
    snapshot: PrescriptionSnapshot
    dispensing_date: date
    version: int
    supersedes_record_id: str | None
    recalc_reason: str | None
    status: RecordStatus
    lines: tuple[DispensingLine, ...]
    created_at: datetime
    created_by: str
    approved_at: datetime | None = None
    approved_by: str | None = None

    def contract_records(self) -> list[ConversionRecord]:
        """导出契约层换算记录（已审核单带 reviewed_at）。"""
        records: list[ConversionRecord] = []
        for line in self.lines:
            if line.final is None:
                continue
            records.append(
                ConversionRecord(
                    conversion_id=f"{self.record_id}:{line.line_id}",
                    prescription_snapshot_id=self.snapshot.snapshot_id,
                    dispensing_date=self.dispensing_date,
                    standard_id=line.final.standard_id or "MANUAL",
                    exact_grams=(
                        line.computation.exact_daily_grams
                        if line.computation is not None
                        else line.final.dispensed_daily_grams
                    ),
                    dispensed_grams=line.final.dispensed_daily_grams,
                    rounding_delta_grams=(
                        line.final.rounding_delta_grams
                        if line.final.rounding_delta_grams is not None
                        else Decimal("0")
                    ),
                    formula_text=line.final.formula_text,
                    reviewed_at=self.approved_at,
                )
            )
        return records


# ---------------------------------------------------------------------------
# 对照报告


@dataclass(frozen=True)
class LineExplanation:
    line_id: str
    source_name: str
    decoction_grams: Decimal
    portions_per_day: int
    standard_id: str | None
    manufacturer_id: str | None
    extract_ratio: Decimal | None
    package_step_grams: Decimal | None
    maximum_daily_grams: Decimal | None
    valid_from: date | None
    valid_until: date | None
    exact_grams: Decimal | None
    dispensed_grams: Decimal | None
    rounding_delta_grams: Decimal | None
    formula_text: str | None
    status: LineStatus
    flags: tuple[ReviewFlag, ...]
    decision: ManualDecision | None


@dataclass(frozen=True)
class RecordExplanation:
    record_id: str
    prescription_id: str
    snapshot_id: str
    dispensing_date: date
    version: int
    status: RecordStatus
    supersedes_record_id: str | None
    recalc_reason: str | None
    approved_by: str | None
    lines: tuple[LineExplanation, ...]


@dataclass(frozen=True)
class ComparisonReport:
    """跨版本/跨处方的对照视图：每个数字的公式、标准来源、差额与人工决定。"""

    records: tuple[RecordExplanation, ...]

    def render_text(self) -> str:
        chunks = [self._render_record(rec) for rec in self.records]
        body = "\n\n".join(chunks)
        diff = self._render_diff()
        return f"{body}\n\n{diff}" if diff else body

    @staticmethod
    def _render_record(rec: RecordExplanation) -> str:
        header = (
            f"调配单 {rec.record_id} · 处方 {rec.prescription_id} · "
            f"调配日 {rec.dispensing_date} · 版本 v{rec.version} · "
            f"状态 {rec.status.value}"
        )
        if rec.recalc_reason:
            header += f"\n  重算原因: {rec.recalc_reason}（取代 {rec.supersedes_record_id}）"
        if rec.approved_by:
            header += f"\n  审核: {rec.approved_by}"
        lines = [header]
        for line in rec.lines:
            lines.append(ComparisonReport._render_line(line))
        return "\n".join(lines)

    @staticmethod
    def _render_line(line: LineExplanation) -> str:
        out = [
            f"  {line.source_name}（饮片 {line.decoction_grams}g/日，"
            f"每日 {line.portions_per_day} 次）— {line.status.value}"
        ]
        if line.standard_id is not None:
            until = line.valid_until if line.valid_until else "现行有效"
            out.append(
                f"    标准来源: {line.standard_id} ｜ 厂家 {line.manufacturer_id} ｜ "
                f"当量系数 {line.extract_ratio} ｜ 步长 {line.package_step_grams}g ｜ "
                f"最大日量 {line.maximum_daily_grams}g ｜ "
                f"有效期 {line.valid_from} ~ {until}"
            )
        else:
            out.append("    标准来源: 无（缺少对应品种的已批准标准）")
        if line.formula_text:
            out.append(f"    公式: {line.formula_text}")
        if line.dispensed_grams is not None:
            delta = (
                f"{line.rounding_delta_grams:+}g"
                if line.rounding_delta_grams is not None
                else "无精确基准"
            )
            exact = (
                f"{line.exact_grams}g" if line.exact_grams is not None else "未知"
            )
            out.append(
                f"    结果: 日调配 {line.dispensed_grams}g"
                f"（精确值 {exact}，舍入差额 {delta}）"
            )
        for flag in line.flags:
            out.append(f"    审核标记: {flag.reason.value} — {flag.detail}")
        if line.decision is not None:
            d = line.decision
            out.append(
                f"    人工决定: {d.action.value} ｜ 药师 {d.pharmacist_id} ｜ "
                f"{d.decided_at:%Y-%m-%d %H:%M} ｜ 理由: {d.rationale}"
            )
        return "\n".join(out)

    def _render_diff(self) -> str | None:
        """两张单对照时，按饮片名列出日调配量差异，方便向患者解释。"""
        if len(self.records) != 2:
            return None
        first, second = self.records
        rows = []
        for line_a in first.lines:
            line_b = next(
                (l for l in second.lines if l.source_name == line_a.source_name),
                None,
            )
            if line_b is None:
                continue
            if (
                line_a.dispensed_grams is None
                or line_b.dispensed_grams is None
                or line_a.dispensed_grams == line_b.dispensed_grams
            ):
                continue
            rows.append(
                f"  {line_a.source_name}: {line_a.dispensed_grams}g/日"
                f"（{line_a.standard_id}）→ {line_b.dispensed_grams}g/日"
                f"（{line_b.standard_id}），"
                f"差异 {line_b.dispensed_grams - line_a.dispensed_grams:+}g，"
                f"源于标准切换（系数 {line_a.extract_ratio} → "
                f"{line_b.extract_ratio}，步长 {line_a.package_step_grams}g → "
                f"{line_b.package_step_grams}g）"
            )
        if not rows:
            return None
        return "前后对照:\n" + "\n".join(rows)


# ---------------------------------------------------------------------------
# 服务


class GranuleConversionService:
    """配方颗粒等量换算与审核服务。"""

    def __init__(
        self,
        registry: StandardRegistry,
        *,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._registry = registry
        self._clock = clock
        self._snapshots: dict[str, PrescriptionSnapshot] = {}
        self._records: dict[str, DispensingRecord] = {}
        self._create_index: dict[str, str] = {}
        self._recalc_index: dict[str, str] = {}
        self._seq = itertools.count(1)

    @property
    def registry(self) -> StandardRegistry:
        """标准注册表（更正标准用；服务内换算结果不受后续更正影响）。"""
        return self._registry

    # -- 处方快照 -----------------------------------------------------------

    def snapshot_prescription(
        self,
        prescription_id: str,
        *,
        physician_name: str,
        patient_ref: str,
        lines: list[DecoctionDose],
        snapshot_id: str | None = None,
    ) -> PrescriptionSnapshot:
        """留存医师开具的饮片名与剂量，作为一切换算的固定输入。"""
        if not lines:
            raise ValueError("处方至少包含一味药")
        snapshot = PrescriptionSnapshot(
            snapshot_id=snapshot_id or f"snap-{next(self._seq):04d}",
            prescription_id=prescription_id,
            physician_name=physician_name,
            patient_ref=patient_ref,
            created_at=self._clock(),
            lines=tuple(lines),
        )
        self._snapshots[snapshot.snapshot_id] = snapshot
        return snapshot

    # -- 开单 ---------------------------------------------------------------

    def create_dispensing(
        self,
        snapshot_id: str,
        dispensing_date: date,
        *,
        created_by: str,
        request_key: str | None = None,
    ) -> DispensingRecord:
        """按调配日期选择已批准标准，生成 v1 调配单。

        request_key 相同的重复请求返回同一张单（幂等）。
        """
        if request_key is not None and request_key in self._create_index:
            return self._records[self._create_index[request_key]]
        snapshot = self._snapshots[snapshot_id]
        previous = self._latest_record(snapshot.snapshot_id)
        previous_by_line = (
            {line.line_id: line for line in previous.lines}
            if previous is not None
            else {}
        )
        lines = tuple(
            self._convert_line(
                dose,
                dispensing_date,
                previous_line=previous_by_line.get(dose.line_id),
            )
            for dose in snapshot.lines
        )
        record = self._new_record(
            snapshot=snapshot,
            dispensing_date=dispensing_date,
            version=1,
            supersedes_record_id=None,
            recalc_reason=None,
            lines=lines,
            created_by=created_by,
        )
        if request_key is not None:
            self._create_index[request_key] = record.record_id
        return record

    # -- 重算 ---------------------------------------------------------------

    def recalculate(
        self,
        record_id: str,
        *,
        pharmacist_id: str,
        reason: str,
        request_key: str,
    ) -> DispensingRecord:
        """药师主动发起的重算：同一处方快照、同一调配日期，用当前标准重算。

        - 原版本（无论是否已审核）保持原样，新版本取代它成为最新版；
        - 与上一版本厂家不同且不可互换的品种会进入人工审核；
        - request_key 相同的重复重算请求返回同一版本（幂等）。
        """
        if request_key in self._recalc_index:
            return self._records[self._recalc_index[request_key]]
        base = self._records[record_id]
        previous_by_line = {line.line_id: line for line in base.lines}
        lines = tuple(
            self._convert_line(
                dose,
                base.dispensing_date,
                previous_line=previous_by_line.get(dose.line_id),
            )
            for dose in base.snapshot.lines
        )
        version = 1 + max(
            r.version
            for r in self._records.values()
            if r.snapshot.snapshot_id == base.snapshot.snapshot_id
        )
        record = self._new_record(
            snapshot=base.snapshot,
            dispensing_date=base.dispensing_date,
            version=version,
            supersedes_record_id=base.record_id,
            recalc_reason=reason,
            lines=lines,
            created_by=pharmacist_id,
        )
        self._recalc_index[request_key] = record.record_id
        return record

    # -- 人工审核 -----------------------------------------------------------

    def submit_decision(
        self,
        record_id: str,
        line_id: str,
        *,
        pharmacist_id: str,
        action: DecisionAction,
        rationale: str,
        override_grams: Decimal | None = None,
        substitute_standard_id: str | None = None,
    ) -> DispensingRecord:
        """药师处理一个待审核行；决定随单留痕。"""
        record = self._mutable_record(record_id)
        index = self._find_pending_line(record, line_id)
        line = record.lines[index]
        decision = ManualDecision(
            decision_id=f"dec-{next(self._seq):04d}",
            pharmacist_id=pharmacist_id,
            decided_at=self._clock(),
            action=action,
            rationale=rationale,
            override_grams=override_grams,
            substitute_standard_id=substitute_standard_id,
        )
        new_line = self._apply_decision(line, decision)
        return self._replace_line(record, index, new_line)

    def approve(
        self,
        record_id: str,
        *,
        pharmacist_id: str,
    ) -> DispensingRecord:
        """审核签发调配单；签发后该版本不可再变。重复签发返回原单。"""
        record = self._records[record_id]
        if record.status == RecordStatus.APPROVED:
            return record
        pending = [l.line_id for l in record.lines if l.status == LineStatus.PENDING_REVIEW]
        if pending:
            raise PendingReviewError(f"存在未处理审核项: {pending}")
        approved = DispensingRecord(
            record_id=record.record_id,
            snapshot=record.snapshot,
            dispensing_date=record.dispensing_date,
            version=record.version,
            supersedes_record_id=record.supersedes_record_id,
            recalc_reason=record.recalc_reason,
            status=RecordStatus.APPROVED,
            lines=record.lines,
            created_at=record.created_at,
            created_by=record.created_by,
            approved_at=self._clock(),
            approved_by=pharmacist_id,
        )
        self._records[record_id] = approved
        return approved

    # -- 对照视图 -----------------------------------------------------------

    def explain(self, record_id: str) -> RecordExplanation:
        return self._explain_record(self._records[record_id])

    def compare(self, record_ids: list[str]) -> ComparisonReport:
        """对照任意几张调配单（如生效日前后、重算前后）。"""
        return ComparisonReport(
            records=tuple(self._explain_record(self._records[r]) for r in record_ids)
        )

    def compare_snapshot(self, snapshot_id: str) -> ComparisonReport:
        """对照同一处方快照的全部版本（按版本号排序）。"""
        records = sorted(
            (r for r in self._records.values() if r.snapshot.snapshot_id == snapshot_id),
            key=lambda r: r.version,
        )
        if not records:
            raise KeyError(f"快照 {snapshot_id} 没有调配单")
        return ComparisonReport(
            records=tuple(self._explain_record(r) for r in records)
        )

    # -- 内部 ----------------------------------------------------------------

    def _latest_record(self, snapshot_id: str) -> DispensingRecord | None:
        """同一处方快照的最新调配单（续方时作为厂家互换比较的基准）。"""
        records = [
            r for r in self._records.values()
            if r.snapshot.snapshot_id == snapshot_id
        ]
        return max(records, key=lambda r: r.version, default=None)

    def _new_record(
        self,
        *,
        snapshot: PrescriptionSnapshot,
        dispensing_date: date,
        version: int,
        supersedes_record_id: str | None,
        recalc_reason: str | None,
        lines: tuple[DispensingLine, ...],
        created_by: str,
    ) -> DispensingRecord:
        status = (
            RecordStatus.PENDING_REVIEW
            if any(l.status == LineStatus.PENDING_REVIEW for l in lines)
            else RecordStatus.READY
        )
        record = DispensingRecord(
            record_id=f"rec-{next(self._seq):04d}",
            snapshot=snapshot,
            dispensing_date=dispensing_date,
            version=version,
            supersedes_record_id=supersedes_record_id,
            recalc_reason=recalc_reason,
            status=status,
            lines=lines,
            created_at=self._clock(),
            created_by=created_by,
        )
        self._records[record.record_id] = record
        return record

    def _convert_line(
        self,
        dose: DecoctionDose,
        dispensing_date: date,
        previous_line: DispensingLine | None,
    ) -> DispensingLine:
        standard = self._registry.select(dose.herb_code, dispensing_date)
        if standard is None:
            flag = ReviewFlag(
                ReviewReason.MISSING_STANDARD,
                f"调配日 {dispensing_date} 无品种 {dose.herb_code}"
                f"（{dose.source_name}）的已批准标准",
            )
            return DispensingLine(
                line_id=dose.line_id,
                dose=dose,
                status=LineStatus.PENDING_REVIEW,
                standard=None,
                computation=None,
                flags=(flag,),
                decision=None,
                final=None,
            )

        computation = compute_line(dose, standard)
        flags: list[ReviewFlag] = []
        if computation.exceeds_maximum_daily:
            flags.append(
                ReviewFlag(
                    ReviewReason.EXCEEDS_MAX_DAILY,
                    f"日调配量 {computation.dispensed_daily_grams}g 超过标准 "
                    f"{standard.standard_id} 最大日用量 "
                    f"{standard.maximum_daily_grams}g",
                )
            )
        interchange_flag = self._check_interchangeable(standard, previous_line)
        if interchange_flag is not None:
            flags.append(interchange_flag)

        if flags:
            return DispensingLine(
                line_id=dose.line_id,
                dose=dose,
                status=LineStatus.PENDING_REVIEW,
                standard=standard,
                computation=computation,
                flags=tuple(flags),
                decision=None,
                final=None,
            )
        return DispensingLine(
            line_id=dose.line_id,
            dose=dose,
            status=LineStatus.AUTO_CONVERTED,
            standard=standard,
            computation=computation,
            flags=(),
            decision=None,
            final=FinalAmount(
                dispensed_daily_grams=computation.dispensed_daily_grams,
                rounding_delta_grams=computation.rounding_delta_grams,
                formula_text=computation.formula_text,
                standard_id=standard.standard_id,
            ),
        )

    @staticmethod
    def _check_interchangeable(
        standard: GranuleStandard,
        previous_line: DispensingLine | None,
    ) -> ReviewFlag | None:
        """重算/续方时厂家变更且不在同一可互换组 → 人工审核。"""
        if previous_line is None or previous_line.standard is None:
            return None
        previous = previous_line.standard
        if previous.manufacturer_id == standard.manufacturer_id:
            return None
        same_group = (
            previous.interchangeable_group is not None
            and previous.interchangeable_group == standard.interchangeable_group
        )
        if same_group:
            return None
        return ReviewFlag(
            ReviewReason.NON_INTERCHANGEABLE_MANUFACTURER,
            f"厂家 {previous.manufacturer_id}（标准 {previous.standard_id}）与 "
            f"{standard.manufacturer_id}（标准 {standard.standard_id}）"
            f"不在同一可互换组，需药师确认",
        )

    def _mutable_record(self, record_id: str) -> DispensingRecord:
        record = self._records[record_id]
        if record.status == RecordStatus.APPROVED:
            raise RecordFrozenError(f"调配单 {record_id} 已审核，不可再更改")
        return record

    @staticmethod
    def _find_pending_line(record: DispensingRecord, line_id: str) -> int:
        for index, line in enumerate(record.lines):
            if line.line_id == line_id:
                if line.status != LineStatus.PENDING_REVIEW:
                    raise ValueError(f"行 {line_id} 不在待审核状态")
                return index
        raise KeyError(f"调配单 {record.record_id} 无行 {line_id}")

    def _apply_decision(
        self, line: DispensingLine, decision: ManualDecision
    ) -> DispensingLine:
        if decision.action == DecisionAction.ACCEPT_COMPUTED:
            if line.computation is None or line.standard is None:
                raise ValueError("无自动换算结果可接受，请核定剂量或指定替代标准")
            final = FinalAmount(
                dispensed_daily_grams=line.computation.dispensed_daily_grams,
                rounding_delta_grams=line.computation.rounding_delta_grams,
                formula_text=line.computation.formula_text,
                standard_id=line.standard.standard_id,
            )
            return self._resolved(line, decision, final)

        if decision.action == DecisionAction.OVERRIDE_AMOUNT:
            if decision.override_grams is None or decision.override_grams <= 0:
                raise ValueError("手工核定剂量必须为正数")
            grams = decision.override_grams
            delta = (
                grams - line.computation.exact_daily_grams
                if line.computation is not None
                else None
            )
            base = f"{line.computation.formula_text}；" if line.computation else ""
            final = FinalAmount(
                dispensed_daily_grams=grams,
                rounding_delta_grams=delta,
                formula_text=(
                    f"{base}药师 {decision.pharmacist_id} 手工核定日调配量 "
                    f"{grams}g（{decision.rationale}）"
                ),
                standard_id=(
                    line.standard.standard_id if line.standard is not None else None
                ),
            )
            return self._resolved(line, decision, final)

        if decision.action == DecisionAction.SUBSTITUTE_STANDARD:
            if decision.substitute_standard_id is None:
                raise ValueError("缺少替代标准 id")
            substitute = self._registry.get(decision.substitute_standard_id)
            recomputed = compute_line(line.dose, substitute)
            origin = (
                line.standard.standard_id if line.standard is not None else "无标准"
            )
            warning = (
                "；注意：替代标准下仍超过最大日用量"
                if recomputed.exceeds_maximum_daily
                else ""
            )
            final = FinalAmount(
                dispensed_daily_grams=recomputed.dispensed_daily_grams,
                rounding_delta_grams=recomputed.rounding_delta_grams,
                formula_text=(
                    f"{recomputed.formula_text}；药师 {decision.pharmacist_id} "
                    f"指定替代标准（原 {origin} → {substitute.standard_id}，"
                    f"{decision.rationale}）{warning}"
                ),
                standard_id=substitute.standard_id,
            )
            return self._resolved(line, decision, final)

        if decision.action == DecisionAction.REJECT_LINE:
            return DispensingLine(
                line_id=line.line_id,
                dose=line.dose,
                status=LineStatus.REJECTED,
                standard=line.standard,
                computation=line.computation,
                flags=line.flags,
                decision=decision,
                final=None,
            )
        raise ValueError(f"未知决定类型 {decision.action}")

    @staticmethod
    def _resolved(
        line: DispensingLine, decision: ManualDecision, final: FinalAmount
    ) -> DispensingLine:
        return DispensingLine(
            line_id=line.line_id,
            dose=line.dose,
            status=LineStatus.RESOLVED,
            standard=line.standard,
            computation=line.computation,
            flags=line.flags,
            decision=decision,
            final=final,
        )

    def _replace_line(
        self, record: DispensingRecord, index: int, new_line: DispensingLine
    ) -> DispensingRecord:
        lines = list(record.lines)
        lines[index] = new_line
        status = (
            RecordStatus.PENDING_REVIEW
            if any(l.status == LineStatus.PENDING_REVIEW for l in lines)
            else RecordStatus.READY
        )
        updated = DispensingRecord(
            record_id=record.record_id,
            snapshot=record.snapshot,
            dispensing_date=record.dispensing_date,
            version=record.version,
            supersedes_record_id=record.supersedes_record_id,
            recalc_reason=record.recalc_reason,
            status=status,
            lines=tuple(lines),
            created_at=record.created_at,
            created_by=record.created_by,
            approved_at=record.approved_at,
            approved_by=record.approved_by,
        )
        self._records[record.record_id] = updated
        return updated

    @staticmethod
    def _explain_record(record: DispensingRecord) -> RecordExplanation:
        lines = []
        for line in record.lines:
            standard = line.standard
            final = line.final
            computation = line.computation
            lines.append(
                LineExplanation(
                    line_id=line.line_id,
                    source_name=line.dose.source_name,
                    decoction_grams=line.dose.daily_decoction_grams,
                    portions_per_day=line.dose.portions_per_day,
                    standard_id=(
                        final.standard_id
                        if final is not None and final.standard_id is not None
                        else (standard.standard_id if standard else None)
                    ),
                    manufacturer_id=(
                        standard.manufacturer_id if standard else None
                    ),
                    extract_ratio=standard.extract_ratio if standard else None,
                    package_step_grams=(
                        standard.package_step_grams if standard else None
                    ),
                    maximum_daily_grams=(
                        standard.maximum_daily_grams if standard else None
                    ),
                    valid_from=standard.valid_from if standard else None,
                    valid_until=standard.valid_until if standard else None,
                    exact_grams=(
                        computation.exact_daily_grams if computation else None
                    ),
                    dispensed_grams=(
                        final.dispensed_daily_grams if final is not None else None
                    ),
                    rounding_delta_grams=(
                        final.rounding_delta_grams if final is not None else None
                    ),
                    formula_text=(
                        final.formula_text
                        if final is not None
                        else (computation.formula_text if computation else None)
                    ),
                    status=line.status,
                    flags=line.flags,
                    decision=line.decision,
                )
            )
        return RecordExplanation(
            record_id=record.record_id,
            prescription_id=record.snapshot.prescription_id,
            snapshot_id=record.snapshot.snapshot_id,
            dispensing_date=record.dispensing_date,
            version=record.version,
            status=record.status,
            supersedes_record_id=record.supersedes_record_id,
            recalc_reason=record.recalc_reason,
            approved_by=record.approved_by,
            lines=tuple(lines),
        )
