"""V2 结构化身份与旧格式记号解析。

V1 用「字符串包含」建立契约数据集引用与模型源表映射，``"SRC-1" in text`` 会同时
命中 ``SRC-1`` 与 ``SRC-10``。V2 把身份拆成三元组：

- 来源身份 :class:`SourceRef` = (来源类型, 来源编号)，序列化为 ``v2://<类型>/<编号>``
- 字段身份 :class:`FieldIdentity` = (来源类型, 来源编号, 字段名)，
  序列化为 ``v2://<类型>/<编号>#<字段>``

解析旧记号时只做**精确等值**匹配，任何无法唯一确定的记号保持原始输入并标记
``ambiguous`` / ``unknown``，等待人工核实，绝不靠前缀猜测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

V2_SCHEME = "v2://"
# 记号分隔：逗号（含中文逗号）、分号、空白
_TOKEN_SPLIT_RE = re.compile(r"[,，;；\s]+")
_V2_RE = re.compile(r"^v2://(?P<stype>[^/#]+)/(?P<sno>[^#]+?)(?:#(?P<fname>.+))?$")


@dataclass(frozen=True)
class SourceRef:
    """V2 来源身份：来源类型 + 来源编号，编号本身不再单独承担身份。"""

    source_type: str
    source_no: str

    def serialize(self) -> str:
        return f"{V2_SCHEME}{self.source_type}/{self.source_no}"

    @classmethod
    def parse(cls, text: str) -> "SourceRef":
        m = _V2_RE.match(text.strip())
        if not m:
            raise ValueError(f"不是合法的 V2 来源引用: {text!r}")
        return cls(source_type=m.group("stype"), source_no=m.group("sno"))


@dataclass(frozen=True)
class FieldIdentity:
    """V2 字段身份：来源身份 + 字段名，全局唯一、可直接还原。"""

    source_type: str
    source_no: str
    field_name: str

    @property
    def source_ref(self) -> SourceRef:
        return SourceRef(self.source_type, self.source_no)

    def key(self) -> tuple[str, str, str]:
        return self.source_type, self.source_no, self.field_name

    def serialize(self) -> str:
        return f"{self.source_ref.serialize()}#{self.field_name}"

    @classmethod
    def parse(cls, text: str) -> "FieldIdentity":
        m = _V2_RE.match(text.strip())
        if not m or not m.group("fname"):
            raise ValueError(f"不是合法的 V2 字段身份: {text!r}")
        return cls(m.group("stype"), m.group("sno"), m.group("fname"))


def canonical_field_identity(ref: SourceRef, field_name: str) -> FieldIdentity:
    return FieldIdentity(ref.source_type, ref.source_no, field_name.strip())


def split_legacy_tokens(text: Any) -> list[str]:
    """把旧格式自由文本切成记号，空记号丢弃；记号整体保持原样不做猜测。"""
    if text is None:
        return []
    return [tok for tok in _TOKEN_SPLIT_RE.split(str(text)) if tok.strip()]


@dataclass
class ResolvedRef:
    """单个记号的解析结论。

    - resolved：唯一确定，``source_ref`` 可用
    - ambiguous：同一编号对应多个来源类型，保留 ``raw_input`` 待核实
    - unknown：编号在来源登记表中不存在，保留 ``raw_input`` 待核实
    """

    raw_input: str
    status: str
    source_ref: SourceRef | None = None
    candidates: list[SourceRef] = field(default_factory=list)
    reason: str = ""

    @property
    def pending_review(self) -> bool:
        return self.status != "resolved"

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_input": self.raw_input,
            "status": self.status,
            "source_ref": self.source_ref.serialize() if self.source_ref else None,
            "candidates": [c.serialize() for c in self.candidates],
            "reason": self.reason,
            "pending_review": self.pending_review,
        }


@dataclass(frozen=True)
class CatalogEntry:
    source_id: int
    source_type: str
    source_no: str
    converted: bool
    fields: tuple[str, ...]

    @property
    def ref(self) -> SourceRef:
        return SourceRef(self.source_type, self.source_no)


class LegacyCatalog:
    """来源登记表的只读视图，按「类型 + 编号」精确索引，不提供任何前缀查询。"""

    def __init__(self, sources: list[dict[str, Any]]):
        self._by_no: dict[str, list[CatalogEntry]] = {}
        self._by_ref: dict[SourceRef, CatalogEntry] = {}
        for row in sources:
            entry = CatalogEntry(
                source_id=int(row["id"]),
                source_type=str(row.get("源类型", "")).strip(),
                source_no=str(row.get("源编号", "")).strip(),
                converted=bool(row.get("v2_converted", False)),
                fields=tuple(str(f).strip() for f in row.get("v2_fields", []) if str(f).strip()),
            )
            if not entry.source_no:
                continue
            self._by_no.setdefault(entry.source_no, []).append(entry)
            self._by_ref[entry.ref] = entry

    # -- 查询（全部精确匹配） --

    def get(self, ref: SourceRef) -> CatalogEntry | None:
        return self._by_ref.get(ref)

    def entries(self) -> list[CatalogEntry]:
        return list(self._by_ref.values())

    def has_field(self, ref: SourceRef, field_name: str) -> bool:
        entry = self.get(ref)
        return entry is not None and field_name in entry.fields

    def resolve_token(self, raw: str) -> ResolvedRef:
        """解析单个旧记号为 V2 来源身份；歧义与未知一律保留原输入。"""
        token = raw.strip()
        if not token:
            return ResolvedRef(raw, "unknown", reason="空记号")

        # 已是 V2 引用：解析后必须在登记表中精确存在
        if token.startswith(V2_SCHEME):
            try:
                ref = SourceRef.parse(token)
            except ValueError as exc:
                return ResolvedRef(raw, "unknown", reason=str(exc))
            if self.get(ref) is None:
                return ResolvedRef(raw, "unknown", reason="V2 来源在登记表中不存在")
            return ResolvedRef(raw, "resolved", source_ref=ref)

        # 旧格式「来源编号:字段」：冒号左侧必须是完整编号
        if ":" in token:
            left, _, _field = token.rpartition(":")
            return self._resolve_by_no(raw, left)

        return self._resolve_by_no(raw, token)

    def _resolve_by_no(self, raw: str, source_no: str) -> ResolvedRef:
        matches = self._by_no.get(source_no, [])
        if not matches:
            return ResolvedRef(raw, "unknown", reason=f"来源编号 {source_no!r} 无精确登记记录")
        if len(matches) > 1:
            candidates = [e.ref for e in matches]
            return ResolvedRef(
                raw, "ambiguous", candidates=candidates,
                reason=f"编号 {source_no!r} 同时登记了 {len(matches)} 种来源类型，需人工指定",
            )
        return ResolvedRef(raw, "resolved", source_ref=matches[0].ref)

    def resolve_many(self, text: Any) -> list[ResolvedRef]:
        return [self.resolve_token(tok) for tok in split_legacy_tokens(text)]


def legacy_substring_match(haystack: Any, source_no: str) -> bool:
    """复现 V1 的缺陷匹配（``编号 in 文本``），仅用于演示新旧行为对照。"""
    return source_no in str(haystack or "")
