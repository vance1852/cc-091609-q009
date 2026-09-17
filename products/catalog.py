"""已批准颗粒产品标准目录。

目录只增不改：标准一经注册即不可变；标准更正以新的
:class:`~products.contracts.GranuleStandard` 注册，重叠生效时段由
"生效日最新、注册顺序最后" 的标准胜出。历史标准仍然保留，
已生成的旧调配单继续引用它，不会被追改。
"""

from __future__ import annotations

from datetime import date

from products.contracts import GranuleStandard


class DuplicateStandardError(ValueError):
    """同一 standard_id 重复注册。"""


class StandardCatalog:
    """按生效日保存已批准标准的只增目录。"""

    def __init__(self) -> None:
        self._standards: list[GranuleStandard] = []
        self._ids: set[str] = set()

    def register(self, standard: GranuleStandard) -> GranuleStandard:
        """注册一个已批准标准。标准对象不可变，注册后不得修改。"""
        if standard.standard_id in self._ids:
            raise DuplicateStandardError(f"标准 {standard.standard_id} 已注册，标准更正请使用新的 standard_id")
        if standard.valid_until is not None and standard.valid_until < standard.valid_from:
            raise ValueError(f"标准 {standard.standard_id} 的失效日早于生效日")
        self._standards.append(standard)
        self._ids.add(standard.standard_id)
        return standard

    def find(self, herb_code: str, on: date) -> GranuleStandard | None:
        """返回调配日 ``on`` 当天生效的已批准标准；无对应品种时返回 ``None``。

        若同一品种在同一日期有多个标准重叠（标准更正场景），
        取 ``valid_from`` 最新者；仍并列时取注册顺序最后者。
        """
        candidates = [
            (index, standard)
            for index, standard in enumerate(self._standards)
            if standard.herb_code == herb_code
            and standard.valid_from <= on
            and (standard.valid_until is None or on <= standard.valid_until)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda pair: (pair[1].valid_from, pair[0]))
        return candidates[-1][1]

    def get(self, standard_id: str) -> GranuleStandard | None:
        """按 id 取标准，用于追溯历史调配单的标准来源。"""
        for standard in self._standards:
            if standard.standard_id == standard_id:
                return standard
        return None

    def all(self) -> tuple[GranuleStandard, ...]:
        return tuple(self._standards)
