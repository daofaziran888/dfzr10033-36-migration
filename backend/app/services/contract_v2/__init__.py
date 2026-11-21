"""数据契约 V2 迁移域：结构化来源身份、来源字段映射、迁移清单与回滚。

子模块：
- identities：V2 来源身份/字段身份、旧格式记号解析（精确匹配，杜绝子串误中）
- migration：契约与模型字段引用迁移、整组幂等、清单与历史、回滚
- promotion：语义模型 V2 上线门禁、映射消费与变更传播
- demo_data：前缀重合场景的示例数据集构造
"""
from __future__ import annotations

from app.services.contract_v2.identities import (
    FieldIdentity,
    LegacyCatalog,
    ResolvedRef,
    SourceRef,
    canonical_field_identity,
    split_legacy_tokens,
)
from app.services.contract_v2.migration import ContractMigrationV2Service
from app.services.contract_v2.promotion import SemanticModelV2Service

__all__ = [
    "FieldIdentity",
    "LegacyCatalog",
    "ResolvedRef",
    "SourceRef",
    "canonical_field_identity",
    "split_legacy_tokens",
    "ContractMigrationV2Service",
    "SemanticModelV2Service",
]
