"""V2 迁移服务：把契约数据集引用与模型源表映射迁移到结构化身份。

产物与记录（均放在内存 store 的独立表中，互不污染 V1 字段）：

- ``v2_source_field_mapping`` 来源字段映射记录（模型初始化与变更消费的事实来源）
- ``v2_migration_manifest``      迁移清单（每个消费者一组，记录采用了哪些映射、待复核项）
- ``v2_migration_history``       迁移/回滚历史（保留改写前快照，支撑回滚与版本读取）

设计要点：
- 可从旧记录直接还原字段身份的走直接转换；有歧义/未知的保留原输入，状态 pending_review；
- 同一批次重复提交只确认一次整组新映射（按输入指纹幂等）；
- 迁移与回滚都把完整快照写进历史，既有版本始终可读；
- 待复核项不进入 confirmed 映射，不会被语义模型静默消费。
"""
from __future__ import annotations

import copy
import hashlib
import time
from dataclasses import dataclass
from typing import Any

from app.services.contract_v2.identities import (
    FieldIdentity,
    LegacyCatalog,
    ResolvedRef,
    canonical_field_identity,
    split_legacy_tokens,
)

MAPPING_TABLE = "v2_source_field_mapping"
MANIFEST_TABLE = "v2_migration_manifest"
HISTORY_TABLE = "v2_migration_history"

CONTRACT_RESOURCE = "data_contract"
MODEL_RESOURCE = "semantic_model"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(r.get("id", 0)) for r in rows), default=0) + 1


def fingerprint(payload: dict[str, Any]) -> str:
    """对迁移输入做规范化指纹：键排序、记号级去重，保证重复提交可识别。"""
    normalized = {
        key: sorted(split_legacy_tokens(value))
        for key, value in sorted(payload.items())
    }
    blob = repr(normalized).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


@dataclass
class FieldResolution:
    """模型字段的解析结论：映射到唯一来源、待复核，或在登记字段目录中找不到。"""

    field_name: str
    status: str  # mapped | ambiguous | unknown_field
    identity: FieldIdentity | None = None
    raw_input: str = ""
    reason: str = ""


class ContractMigrationV2Service:
    def __init__(self, store: Any) -> None:
        # 接受注入的 store，测试可用隔离实例；生产注入全局单例
        self.store = store

    # ------------------------------------------------------------------ #
    # 版本读取（迁移与回滚都能读）
    # ------------------------------------------------------------------ #

    def catalog(self) -> LegacyCatalog:
        return LegacyCatalog(self.store.rows("source_registry"))

    def mappings(self) -> list[dict[str, Any]]:
        return self.store.rows(MAPPING_TABLE)

    def manifests(self) -> list[dict[str, Any]]:
        return self.store.rows(MANIFEST_TABLE)

    def history(self) -> list[dict[str, Any]]:
        return self.store.rows(HISTORY_TABLE)

    def get_manifest(self, resource: str, consumer_id: int) -> dict[str, Any] | None:
        for row in self.manifests():
            if row["resource"] == resource and row["consumer_id"] == consumer_id:
                return row
        return None

    def active_manifest(self, resource: str, consumer_id: int) -> dict[str, Any] | None:
        row = self.get_manifest(resource, consumer_id)
        if row and row["status"] == "migrated":
            return row
        return None

    def history_versions(self, resource: str, consumer_id: int) -> list[dict[str, Any]]:
        return [
            {
                "batch_id": h["batch_id"],
                "action": h["action"],
                "at": h["at"],
                "version": h["version_after"],
                "status": h["status_after"],
            }
            for h in self.history()
            if h["resource"] == resource and h["consumer_id"] == consumer_id
        ]

    def confirmed_mappings_for(self, resource: str, consumer_id: int) -> list[dict[str, Any]]:
        manifest = self.active_manifest(resource, consumer_id)
        if not manifest:
            return []
        keys = {tuple(k) for k in manifest["confirmed_field_keys"]}
        return [m for m in self.mappings() if tuple(m["field_key"]) in keys]

    # ------------------------------------------------------------------ #
    # 解析辅助
    # ------------------------------------------------------------------ #

    def resolve_source_text(self, text: Any) -> list[ResolvedRef]:
        return self.catalog().resolve_many(text)

    def resolve_model_fields(
        self, mapping_text: Any, field_text: Any
    ) -> tuple[list[FieldResolution], list[ResolvedRef]]:
        """解析模型源表映射与字段别名。

        支持两种旧写法：
        - 源表映射是来源记号列表，字段按登记字段目录归属到唯一来源；
          多个来源都含同名字段时标记 ambiguous，保留原输入；
        - 显式 ``来源编号:字段`` 记号，直接给出字段归属。
        """
        catalog = self.catalog()
        refs = catalog.resolve_many(mapping_text)
        resolved_sources = [r.source_ref for r in refs if r.source_ref is not None]
        results: list[FieldResolution] = []

        for token in split_legacy_tokens(field_text):
            explicit = catalog.resolve_token(token) if ":" in token else None
            if explicit is not None and explicit.source_ref is not None:
                _, _, fname = token.rpartition(":")
                ident = canonical_field_identity(explicit.source_ref, fname)
                if catalog.has_field(ident.source_ref, ident.field_name):
                    results.append(FieldResolution(token, "mapped", ident, token))
                else:
                    results.append(FieldResolution(
                        fname, "unknown_field", raw_input=token,
                        reason=f"字段 {fname!r} 不在来源 {ident.source_ref.serialize()} 的登记目录中"))
                continue

            # 隐式字段：在已解析来源里按字段目录定位，要求归属唯一
            owners = [ref for ref in resolved_sources if catalog.has_field(ref, token)]
            if len(owners) == 1:
                results.append(FieldResolution(
                    token, "mapped", canonical_field_identity(owners[0], token), token))
            elif len(owners) > 1:
                results.append(FieldResolution(
                    token, "ambiguous", raw_input=token,
                    reason=f"字段 {token!r} 同时存在于 "
                           f"{', '.join(o.serialize() for o in owners)}，需人工核实"))
            else:
                results.append(FieldResolution(
                    token, "unknown_field", raw_input=token,
                    reason=f"字段 {token!r} 在任一已解析来源的登记目录中都不存在"))

        return results, refs

    # ------------------------------------------------------------------ #
    # 契约迁移
    # ------------------------------------------------------------------ #

    def migrate_contract(self, contract_id: int) -> dict[str, Any]:
        contract = self.store.find(CONTRACT_RESOURCE, contract_id)
        if contract is None:
            return {"ok": False, "message": f"契约 {contract_id} 不存在"}

        payload = {"数据集": contract.get("数据集", "")}
        return self._apply_migration(CONTRACT_RESOURCE, contract_id, payload, kind="contract")

    # ------------------------------------------------------------------ #
    # 模型迁移
    # ------------------------------------------------------------------ #

    def migrate_model(self, model_id: int) -> dict[str, Any]:
        model = self.store.find(MODEL_RESOURCE, model_id)
        if model is None:
            return {"ok": False, "message": f"语义模型 {model_id} 不存在"}

        payload = {
            "源表映射": model.get("源表映射", ""),
            "字段别名": model.get("字段别名", ""),
        }
        return self._apply_migration(MODEL_RESOURCE, model_id, payload, kind="model")

    # ------------------------------------------------------------------ #
    # 迁移主流程（整组、幂等）
    # ------------------------------------------------------------------ #

    def _apply_migration(
        self, resource: str, consumer_id: int, payload: dict[str, Any], *, kind: str
    ) -> dict[str, Any]:
        existing = self.get_manifest(resource, consumer_id)
        fp = fingerprint(payload)
        if existing and existing["status"] == "migrated" and existing["input_fingerprint"] == fp:
            return {
                "ok": True, "idempotent": True, "resource": resource,
                "consumer_id": consumer_id, "batch_id": existing["batch_id"],
                "message": "该批输入已迁移，重复提交仅确认既有整组映射，不产生新记录",
                "manifest": existing,
            }

        catalog = self.catalog()
        row = self.store.find(resource, consumer_id)
        before = copy.deepcopy(row)
        confirmed_keys: set[tuple[str, str, str]] = set()
        pending: list[dict[str, Any]] = []
        source_refs: set[str] = set()

        if kind == "contract":
            for ref in catalog.resolve_many(payload["数据集"]):
                if ref.source_ref is not None:
                    source_refs.add(ref.source_ref.serialize())
                else:
                    pending.append(ref.to_dict())
        else:
            field_resolutions, source_resolutions = self.resolve_model_fields(
                payload["源表映射"], payload["字段别名"])
            for ref in source_resolutions:
                if ref.source_ref is not None:
                    source_refs.add(ref.source_ref.serialize())
                else:
                    pending.append(ref.to_dict())
            for fr in field_resolutions:
                if fr.identity is not None:
                    confirmed_keys.add(fr.identity.key())
                else:
                    pending.append({
                        "raw_input": fr.raw_input,
                        "status": fr.status,
                        "source_ref": None,
                        "candidates": [],
                        "reason": fr.reason,
                        "pending_review": True,
                    })

        # 生成（或复用）来源字段映射记录；待复核项不生成映射
        mapping_records: list[dict[str, Any]] = []
        for key in sorted(confirmed_keys):
            mapping_records.append(self._ensure_mapping(key, resource, consumer_id))

        manifest = existing
        if manifest is None:
            manifest = {"id": _next_id(self.manifests())}
            self.manifests().append(manifest)
        batch_id = f"BATCH-{resource}:{consumer_id}:{fp[:8]}"
        new_version = (manifest.get("version", 0) or 0) + 1
        manifest.update({
            "resource": resource,
            "consumer_id": consumer_id,
            "kind": kind,
            "version": new_version,
            "status": "migrated",
            "input_fingerprint": fp,
            "batch_id": batch_id,
            "v2_source_refs": sorted(source_refs),
            "confirmed_field_keys": [list(k) for k in sorted(confirmed_keys)],
            "pending_review": pending,
            "adopted_mapping_ids": [],
            "updated_at": _now(),
        })

        # 改写消费者记录（旧字段保留原值便于审计，另写 V2 结构化列）
        self._rewrite_consumer(resource, row, manifest, payload, catalog)

        self._write_history(
            resource, consumer_id, batch_id, action="migrate",
            version_after=new_version, status_after="migrated",
            payload=payload, before=before, after=copy.deepcopy(row),
            mapping_ids=[m["id"] for m in mapping_records],
        )

        return {
            "ok": True, "idempotent": False, "resource": resource,
            "consumer_id": consumer_id, "batch_id": batch_id,
            "confirmed_mappings": len(mapping_records),
            "pending_review_count": len(pending),
            "pending_review": pending,
            "manifest": manifest,
        }

    def _ensure_mapping(
        self, field_key: tuple[str, str, str], resource: str, consumer_id: int
    ) -> dict[str, Any]:
        for record in self.mappings():
            if tuple(record["field_key"]) == field_key:
                users = record.setdefault("consumers", [])
                marker = f"{resource}:{consumer_id}"
                if marker not in users:
                    users.append(marker)
                record["updated_at"] = _now()
                return record
        source_type, source_no, field_name = field_key
        identity = FieldIdentity(source_type, source_no, field_name)
        record = {
            "id": _next_id(self.mappings()),
            "field_identity": identity.serialize(),
            "field_key": list(field_key),
            "source_type": source_type,
            "source_no": source_no,
            "field_name": field_name,
            "source_ref": identity.source_ref.serialize(),
            "status": "confirmed",
            "consumers": [f"{resource}:{consumer_id}"],
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.mappings().append(record)
        return record

    def _rewrite_consumer(
        self,
        resource: str,
        row: dict[str, Any],
        manifest: dict[str, Any],
        payload: dict[str, Any],
        catalog: LegacyCatalog,
    ) -> None:
        if resource == CONTRACT_RESOURCE:
            v2_refs: list[str] = []
            for ref in catalog.resolve_many(payload["数据集"]):
                if ref.source_ref is not None:
                    v2_refs.append(ref.source_ref.serialize())
            row["v2_dataset_refs"] = v2_refs
            row["v2_manifest_id"] = manifest["id"]
            row["v2_migration_status"] = "migrated"
        else:
            row["v2_source_refs"] = list(manifest["v2_source_refs"])
            row["v2_field_identities"] = [
                FieldIdentity(*key).serialize() for key in manifest["confirmed_field_keys"]
            ]
            row["v2_manifest_id"] = manifest["id"]
            row["v2_migration_status"] = "migrated"

    # ------------------------------------------------------------------ #
    # 回滚：按历史快照还原，历史本身追加保留
    # ------------------------------------------------------------------ #

    def rollback(self, resource: str, consumer_id: int) -> dict[str, Any]:
        manifest = self.get_manifest(resource, consumer_id)
        if manifest is None:
            return {"ok": False, "message": f"{resource}:{consumer_id} 没有迁移清单，无法回滚"}
        if manifest["status"] == "rolled_back":
            return {
                "ok": True, "idempotent": True, "resource": resource,
                "consumer_id": consumer_id,
                "message": "该清单已回滚，重复提交不重复还原",
                "manifest": manifest,
            }

        last_migrate = self._last_snapshot(resource, consumer_id, "migrate")
        if last_migrate is None:
            return {"ok": False, "message": "缺少迁移前快照，无法安全回滚"}

        row = self.store.find(resource, consumer_id)
        # 还原消费者记录到迁移前（删除迁移期写入的 V2 列，恢复旧字段原值）
        before = copy.deepcopy(last_migrate["before"])
        row.clear()
        row.update(before)

        # 本消费者独占的映射记录移除；被其他消费者共享的保留并摘掉引用
        marker = f"{resource}:{consumer_id}"
        removed_mapping_ids: list[int] = []
        for record in list(self.mappings()):
            if marker in record.get("consumers", []):
                record["consumers"] = [c for c in record["consumers"] if c != marker]
                if not record["consumers"]:
                    removed_mapping_ids.append(record["id"])
        if removed_mapping_ids:
            table = self.mappings()
            for rid in removed_mapping_ids:
                table[:] = [r for r in table if r["id"] != rid]

        new_version = manifest["version"] + 1
        rollback_batch = f"ROLLBACK-{resource}:{consumer_id}:{new_version}"
        manifest.update({
            "status": "rolled_back",
            "version": new_version,
            "adopted_mapping_ids": [],
            "updated_at": _now(),
            "batch_id": rollback_batch,
        })
        self._write_history(
            resource, consumer_id, rollback_batch, action="rollback",
            version_after=new_version, status_after="rolled_back",
            payload=last_migrate.get("payload", {}),
            before=copy.deepcopy(last_migrate["after"]),
            after=copy.deepcopy(row),
            mapping_ids=removed_mapping_ids,
        )
        return {
            "ok": True, "idempotent": False, "resource": resource,
            "consumer_id": consumer_id, "batch_id": rollback_batch,
            "removed_mapping_ids": removed_mapping_ids,
            "restored_snapshot_of": last_migrate["batch_id"],
            "manifest": manifest,
        }

    def _last_snapshot(
        self, resource: str, consumer_id: int, action: str
    ) -> dict[str, Any] | None:
        snapshots = [
            h for h in self.history()
            if h["resource"] == resource and h["consumer_id"] == consumer_id
            and h["action"] == action
        ]
        return snapshots[-1] if snapshots else None

    def _write_history(
        self,
        resource: str,
        consumer_id: int,
        batch_id: str,
        *,
        action: str,
        version_after: int,
        status_after: str,
        payload: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any],
        mapping_ids: list[int],
    ) -> None:
        self.history().append({
            "id": _next_id(self.history()),
            "batch_id": batch_id,
            "resource": resource,
            "consumer_id": consumer_id,
            "action": action,
            "version_after": version_after,
            "status_after": status_after,
            "at": _now(),
            "payload": copy.deepcopy(payload),
            "before": copy.deepcopy(before),
            "after": copy.deepcopy(after),
            "mapping_ids": list(mapping_ids),
        })
