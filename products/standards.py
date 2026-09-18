"""已批准颗粒标准的注册、按调配日期选择与更正。

注册表只收录「已批准」标准；标准更正以新版本取代旧版本，
旧版本保留可查询（供已审核调配单溯源），但不再参与新换算的选择。
"""

from __future__ import annotations

from datetime import date

from .contracts import GranuleStandard


class StandardRegistry:
    """已批准颗粒产品标准的注册表。"""

    def __init__(self) -> None:
        self._standards: dict[str, GranuleStandard] = {}
        self._superseded_by: dict[str, str] = {}

    def register(self, standard: GranuleStandard) -> None:
        """登记一个已批准标准。同 id 重复登记视为错误，应走 correct()。"""
        if standard.standard_id in self._standards:
            raise ValueError(
                f"标准 {standard.standard_id} 已存在；如需更正请使用 correct()"
            )
        if (
            standard.valid_until is not None
            and standard.valid_until < standard.valid_from
        ):
            raise ValueError(
                f"标准 {standard.standard_id} 的 valid_until 早于 valid_from"
            )
        self._standards[standard.standard_id] = standard

    def correct(self, corrected: GranuleStandard, *, supersedes: str) -> None:
        """登记更正版本。

        被取代的旧版本仍然保留（已审核调配单引用它做溯源），
        但从今往后的新换算与药师重算都会选择更正后的版本。
        """
        if supersedes not in self._standards:
            raise KeyError(f"被更正的标准 {supersedes} 不存在")
        self.register(corrected)
        self._superseded_by[supersedes] = corrected.standard_id

    def get(self, standard_id: str) -> GranuleStandard:
        """按 id 取标准（含已被取代的旧版本，用于审计溯源）。"""
        try:
            return self._standards[standard_id]
        except KeyError:
            raise KeyError(f"标准 {standard_id} 不存在") from None

    def is_superseded(self, standard_id: str) -> bool:
        return standard_id in self._superseded_by

    def superseded_by(self, standard_id: str) -> str | None:
        return self._superseded_by.get(standard_id)

    def select(self, herb_code: str, dispensing_date: date) -> GranuleStandard | None:
        """按品种与调配日期选择现行已批准标准；无对应品种时返回 None。

        已被更正取代的标准不参与选择。同一日期有多个候选时，
        取生效日最新者（更具体的标准优先），保证结果确定。
        """
        candidates = [
            s
            for s in self._standards.values()
            if s.herb_code == herb_code
            and s.standard_id not in self._superseded_by
            and s.valid_from <= dispensing_date
            and (s.valid_until is None or dispensing_date <= s.valid_until)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda s: (s.valid_from, s.standard_id))
