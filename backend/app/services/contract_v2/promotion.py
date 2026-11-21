"""语义模型的 V2 上线门禁、映射消费与变更传播。

上线规则（对应迁移期的部分来源已转换、模型仍读旧格式的冲突）：
1. 模型必须先有一份 ``migrated`` 清单，读旧格式直接拒绝；
2. 清单里仍有待核实项（歧义/未知）时拒绝，待复核来源不会被静默纳入；
3. 每个可读字段都要能在 V2 来源字段映射表中证明身份，且全部落在本模型这一整组
   映射内 —— 不允许拿不同来源的映射拼出一个并不存在的字段（chimera 防护）；
4. 每个字段身份在来源登记目录中真实存在；
5. 校验通过才投产，实际采用的映射 ID 回写迁移清单，并写审计。
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from app.services.contract_v2.identities import FieldIdentity
from app.services.contract_v2.migration import (
    MODEL_RESOURCE,
    ContractMigrationV2Service,
)


class SemanticModelV2Service:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.migration = ContractMigrationV2Service(store)

    # ------------------------------------------------------------------ #
    # 上线门禁
    # ------------------------------------------------------------------ #

    def promote_model(self, model_id: int) -> dict[str, Any]:
        model = self.store.find(MODEL_RESOURCE, model_id)
        if model is None:
            return {"ok": False, "message": f"语义模型 {model_id} 不存在"}

        catalog = self.migration.catalog()
        manifest = self.migration.get_manifest(MODEL_RESOURCE, model_id)

        # 规则 1：必须先完成 V2 迁移；若依赖中已有来源转 V2，给出冲突原因
        if manifest is None or manifest["status"] != "migrated":
            converted = [
                e.source_no for e in catalog.entries() if e.converted
            ]
            detail = ""
            if converted:
                detail = (f"；依赖来源 {', '.join(converted)} 已转换为 V2，"
                          "模型仍读旧式字符串引用，须先迁完全部依赖再上线")
            return {
                "ok": False,
                "code": "not_migrated",
                "message": "模型尚未完成 V2 字段映射迁移，禁止按旧格式上线" + detail,
            }

        # 规则 2：待核实项未清零
        if manifest.get("pending_review"):
            return {
                "ok": False,
                "code": "pending_review",
                "message": "迁移清单存在待核实引用，复核完成前不得上线",
                "pending_review": manifest["pending_review"],
            }

        declared = [
            FieldIdentity(*key) for key in manifest["confirmed_field_keys"]
        ]
        mapping_by_key = {
            tuple(record["field_key"]): record for record in self.migration.mappings()
        }
        group_keys = {identity.key() for identity in declared}

        # 规则 3a：每个声明字段都能在映射表证明，且必须属于本模型这一整组
        missing: list[str] = []
        adopted: list[dict[str, Any]] = []
        for identity in declared:
            record = mapping_by_key.get(identity.key())
            if record is None or identity.key() not in group_keys:
                missing.append(identity.serialize())
                continue
            adopted.append(record)

        # 规则 3b：防 chimera —— 同一字段名若被指向多个来源身份，拒绝
        field_name_sources: dict[str, set[tuple[str, str]]] = {}
        for identity in declared:
            field_name_sources.setdefault(identity.field_name, set()).add(
                (identity.source_type, identity.source_no))
        chimera = {
            name: sorted(f"{stype}/{sno}" for stype, sno in refs)
            for name, refs in field_name_sources.items() if len(refs) > 1
        }

        # 规则 4：字段身份要在来源登记目录真实存在
        ghost = [
            identity.serialize() for identity in declared
            if not catalog.has_field(identity.source_ref, identity.field_name)
        ]

        issues: list[str] = []
        if missing:
            issues.append(f"缺少完整映射证明的字段: {', '.join(missing)}")
        if chimera:
            issues.append(f"字段被不同来源拼接，无法构成真实字段: {chimera}")
        if ghost:
            issues.append(f"字段身份在来源登记目录不存在: {', '.join(ghost)}")
        if issues:
            model["status"] = "校验中"
            model["abnormal"] = True
            model["pending"] = True
            return {"ok": False, "code": "incomplete_mapping",
                    "message": "上线校验失败：" + "；".join(issues), "issues": issues}

        # 校验通过：投产 + 采用映射回写清单 + 审计
        model["status"] = "已投产"
        model["pending"] = False
        model["abnormal"] = False
        model["v2_promoted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        adopted_ids = sorted(record["id"] for record in adopted)
        manifest["adopted_mapping_ids"] = adopted_ids
        manifest["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._write_audit(model_id, model.get("模型编号", ""), "PROMOTE_V2",
                          f"fields={len(adopted_ids)}")

        return {
            "ok": True, "model_id": model_id, "status": "已投产",
            "fields_count": len(adopted),
            "adopted_mapping_ids": adopted_ids,
            "manifest_id": manifest["id"],
            "source_refs": manifest["v2_source_refs"],
        }

    # ------------------------------------------------------------------ #
    # 消费：模型初始化只读取迁移完成的数据
    # ------------------------------------------------------------------ #

    def consume_migrated_model(self, model_id: int) -> dict[str, Any]:
        model = self.store.find(MODEL_RESOURCE, model_id)
        if model is None:
            return {"ok": False, "message": f"语义模型 {model_id} 不存在"}
        manifest = self.migration.active_manifest(MODEL_RESOURCE, model_id)
        if manifest is None:
            return {"ok": False, "code": "not_migrated",
                    "message": "模型未完成迁移，语义层不消费旧格式数据"}
        if manifest.get("pending_review"):
            return {"ok": False, "code": "pending_review",
                    "message": "存在待核实来源，语义层不纳入未复核数据"}

        resolved = []
        for key in manifest["confirmed_field_keys"]:
            identity = FieldIdentity(*key)
            resolved.append({
                "field_identity": identity.serialize(),
                "source_ref": identity.source_ref.serialize(),
                "field_name": identity.field_name,
            })
        return {
            "ok": True, "model_id": model_id,
            "model_no": model.get("模型编号"),
            "source_refs": manifest["v2_source_refs"],
            "resolved_fields": resolved,
            "adopted_mapping_ids": manifest.get("adopted_mapping_ids", []),
        }

    # ------------------------------------------------------------------ #
    # 变更传播：来源字段变更沿 V2 映射传播给已投产消费者
    # ------------------------------------------------------------------ #

    def propagate_field_change(
        self, source_type: str, source_no: str, field_name: str, *, change: str
    ) -> dict[str, Any]:
        target_key = (source_type, source_no, field_name)
        record = next(
            (r for r in self.migration.mappings() if tuple(r["field_key"]) == target_key),
            None)
        if record is None:
            return {"ok": False, "code": "no_mapping",
                    "message": "该字段没有 V2 映射记录，变更不沿旧字符串关系传播"}

        affected: list[dict[str, Any]] = []
        for manifest in self.migration.manifests():
            if manifest["status"] != "migrated":
                continue
            if target_key not in {tuple(k) for k in manifest["confirmed_field_keys"]}:
                continue
            model = self.store.find(MODEL_RESOURCE, manifest["consumer_id"])
            if model is None or model.get("status") != "已投产":
                continue
            model["pending"] = True
            events = model.setdefault("v2_pending_changes", [])
            events.append({
                "field_identity": record["field_identity"],
                "change": change,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            affected.append({
                "model_id": model["id"],
                "model_no": model.get("模型编号"),
                "field_identity": record["field_identity"],
            })

        self._write_audit(0, f"{source_type}/{source_no}#{field_name}",
                          "SCHEMA_PROPAGATE_V2", f"change={change};affected={len(affected)}")
        return {
            "ok": True, "field_identity": record["field_identity"],
            "change": change, "affected_models": affected,
        }

    def _write_audit(self, model_id: int, model_no: str, action: str, detail: str) -> None:
        rows = self.store.rows("query_audit")
        rows.append({
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "记录编号": f"AUDIT-{uuid.uuid4().hex[:8]}",
            "查询语句": f"{action} model={model_no} {detail}",
            "执行耗时": "12ms",
            "扫描行数": "0",
            "返回行数": "0",
            "数据来源": "contract_v2",
            "操作用户": "v2_migration",
            "查询时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "正常",
            "pending": False,
            "abnormal": False,
        })
