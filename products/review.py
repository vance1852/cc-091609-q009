"""人工审核标记与药师决定。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class ReviewReason(str, Enum):
    """触发人工审核的原因。"""

    #: 调配日期当天没有该品种的已批准标准。
    MISSING_STANDARD = "MISSING_STANDARD"
    #: 换算后的日调配量超过标准规定的最大日用量。
    EXCEEDS_MAX_DAILY = "EXCEEDS_MAX_DAILY"
    #: 与上一版本所用标准厂家不同，且不在同一可互换组。
    NON_INTERCHANGEABLE_MANUFACTURER = "NON_INTERCHANGEABLE_MANUFACTURER"


class DecisionAction(str, Enum):
    """药师对审核项的处理方式。"""

    #: 接受系统自动换算结果。
    ACCEPT_COMPUTED = "ACCEPT_COMPUTED"
    #: 药师手工核定日调配量（差额仍对照精确值展示）。
    OVERRIDE_AMOUNT = "OVERRIDE_AMOUNT"
    #: 药师指定一个替代标准重新换算。
    SUBSTITUTE_STANDARD = "SUBSTITUTE_STANDARD"
    #: 该味药无法自动替代，退回医师处理。
    REJECT_LINE = "REJECT_LINE"


@dataclass(frozen=True)
class ReviewFlag:
    reason: ReviewReason
    detail: str


@dataclass(frozen=True)
class ManualDecision:
    """药师的人工决定，随调配单永久留痕。"""

    decision_id: str
    pharmacist_id: str
    decided_at: datetime
    action: DecisionAction
    rationale: str
    override_grams: Decimal | None = None
    substitute_standard_id: str | None = None
