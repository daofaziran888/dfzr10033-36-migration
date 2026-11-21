"""数据契约 V2 迁移域测试：身份解析、整组幂等、上线门禁、回滚与变更传播。"""
from __future__ import annotations

import pytest

from app.services.contract_v2.demo_data import DEMO, build_demo_store
from app.services.contract_v2.identities import (
    FieldIdentity,
    SourceRef,
    legacy_substring_match,
)
from app.services.contract_v2.migration import (
    HISTORY_TABLE,
    MANIFEST_TABLE,
    MAPPING_TABLE,
    CONTRACT_RESOURCE,
    MODEL_RESOURCE,
    ContractMigrationV2Service,
    fingerprint,
)
from app.services.contract_v2.promotion import SemanticModelV2Service


@pytest.fixture()
def ctx():
    store = build_demo_store()
    return {
        "store": store,
        "migration": ContractMigrationV2Service(store),
        "semantic": SemanticModelV2Service(store),
    }


class TestV2Identity:
    def test_v1_substring_false_positive(self, ctx):
        m2_mapping = ctx["store"].find(MODEL_RESOURCE, DEMO["model_m2"])["源表映射"]
        # V1 缺陷：短编号在长编号文本里子串命中
        assert legacy_substring_match(m2_mapping, "SRC-1") is True

    def test_exact_resolution_separates_prefix_overlap(self, ctx):
        refs = ctx["migration"].catalog().resolve_many("SRC-1, SRC-10, SRC-1-V2")
        assert [(r.source_ref.source_type, r.source_ref.source_no) for r in refs] == [
            ("mysql", "SRC-1"),
            ("postgresql", "SRC-10"),
            ("clickhouse", "SRC-1-V2"),
        ]
        assert all(not r.pending_review for r in refs)

    def test_ambiguous_and_unknown_keep_raw_input(self, ctx):
        refs = {r.raw_input: r for r in ctx["migration"].resolve_source_text("SRC-2, SRC-GHOST")}
        assert refs["SRC-2"].status == "ambiguous"
        assert len(refs["SRC-2"].candidates) == 2
        assert refs["SRC-GHOST"].status == "unknown"
        assert refs["SRC-GHOST"].source_ref is None

    def test_field_identity_roundtrip(self):
        identity = FieldIdentity("mysql", "SRC-1", "user_id")
        assert FieldIdentity.parse(identity.serialize()) == identity
        assert SourceRef.parse("v2://mysql/SRC-1").serialize() == "v2://mysql/SRC-1"
        with pytest.raises(ValueError):
            SourceRef.parse("SRC-1")


class TestContractMigration:
    def test_contract_c1_direct_conversion(self, ctx):
        result = ctx["migration"].migrate_contract(DEMO["contract_c1"])
        assert result["ok"] and result["pending_review_count"] == 0
        row = ctx["store"].find(CONTRACT_RESOURCE, DEMO["contract_c1"])
        assert row["v2_dataset_refs"] == [
            "v2://mysql/SRC-1", "v2://postgresql/SRC-10"]
        # 旧字符串列原样保留
        assert row["数据集"] == "SRC-1, SRC-10"

    def test_duplicate_submission_confirms_once(self, ctx):
        migration = ctx["migration"]
        first = migration.migrate_contract(DEMO["contract_c1"])
        second = migration.migrate_contract(DEMO["contract_c1"])
        assert not first["idempotent"] and second["idempotent"]
        assert first["batch_id"] == second["batch_id"]
        manifests = [m for m in migration.manifests()
                     if m["consumer_id"] == DEMO["contract_c1"]]
        assert len(manifests) == 1
        # 历史也只有一次 migrate
        migrate_events = [h for h in migration.history() if h["action"] == "migrate"]
        assert len(migrate_events) == 1

    def test_pending_review_not_silently_mapped(self, ctx):
        ctx["migration"].migrate_contract(DEMO["contract_c2"])
        manifest = ctx["migration"].get_manifest(CONTRACT_RESOURCE, DEMO["contract_c2"])
        assert manifest["confirmed_field_keys"] == []
        assert {p["raw_input"] for p in manifest["pending_review"]} == {"SRC-2", "SRC-GHOST"}
        assert ctx["migration"].confirmed_mappings_for(
            CONTRACT_RESOURCE, DEMO["contract_c2"]) == []

    def test_fingerprint_stable_for_same_tokens(self):
        assert fingerprint({"a": "SRC-1, SRC-10"}) == fingerprint({"a": "SRC-10,SRC-1"})
        assert fingerprint({"a": "SRC-1"}) != fingerprint({"a": "SRC-10"})


class TestModelMigration:
    def test_model_field_identities_generated(self, ctx):
        result = ctx["migration"].migrate_model(DEMO["model_m1"])
        assert result["confirmed_mappings"] == 4
        row = ctx["store"].find(MODEL_RESOURCE, DEMO["model_m1"])
        keys = {tuple(k) for k in ctx["migration"]
                .get_manifest(MODEL_RESOURCE, DEMO["model_m1"])["confirmed_field_keys"]}
        assert ("mysql", "SRC-1", "user_id") in keys
        assert ("postgresql", "SRC-10", "event_id") in keys
        assert len(row["v2_field_identities"]) == 4

    def test_shared_mapping_reuses_record_and_marks_consumers(self, ctx):
        migration = ctx["migration"]
        migration.migrate_model(DEMO["model_m1"])
        migration.migrate_model(DEMO["model_m2"])
        # M1 与 M2 没有共享字段；构造第二个消费者引用同一字段验证复用
        ctx["store"].rows(MODEL_RESOURCE).append({
            "id": 4, "模型编号": "MODEL-M4", "模型名称": "复用模型",
            "源表映射": "SRC-1", "字段别名": "user_id",
            "度量定义": "COUNT(*)", "维度定义": "user_id", "计算口径": "-",
            "版本序号": "v1.0", "status": "草稿", "pending": True, "abnormal": False})
        migration.migrate_model(4)
        record = next(r for r in migration.mappings()
                      if tuple(r["field_key"]) == ("mysql", "SRC-1", "user_id"))
        assert set(record["consumers"]) == {"semantic_model:1", "semantic_model:4"}

    def test_unknown_field_goes_pending(self, ctx):
        ctx["store"].rows(MODEL_RESOURCE).append({
            "id": 5, "模型编号": "MODEL-M5", "模型名称": "幽灵字段模型",
            "源表映射": "SRC-1", "字段别名": "not_a_real_column",
            "度量定义": "COUNT(*)", "维度定义": "x", "计算口径": "-",
            "版本序号": "v1.0", "status": "草稿", "pending": True, "abnormal": False})
        result = ctx["migration"].migrate_model(5)
        assert result["confirmed_mappings"] == 0
        assert result["pending_review"][0]["status"] == "unknown_field"


class TestPromotionGate:
    def test_old_format_model_blocked_when_dependency_converted(self, ctx):
        result = ctx["semantic"].promote_model(DEMO["model_m2"])
        assert result["ok"] is False and result["code"] == "not_migrated"
        assert "SRC-1-V2" in result["message"]

    def test_pending_review_blocks_promotion(self, ctx):
        ctx["migration"].migrate_model(3)  # M3: SRC-2 歧义 + SRC-GHOST 未知
        result = ctx["semantic"].promote_model(3)
        assert result["ok"] is False and result["code"] == "pending_review"

    def test_complete_group_promotes_and_writes_back(self, ctx):
        ctx["migration"].migrate_model(DEMO["model_m1"])
        result = ctx["semantic"].promote_model(DEMO["model_m1"])
        assert result["ok"] and result["fields_count"] == 4
        manifest = ctx["migration"].get_manifest(MODEL_RESOURCE, DEMO["model_m1"])
        assert manifest["adopted_mapping_ids"] == result["adopted_mapping_ids"]
        assert ctx["store"].find(MODEL_RESOURCE, DEMO["model_m1"])["status"] == "已投产"

    def test_every_field_must_belong_to_one_complete_group(self, ctx):
        migration = ctx["migration"]
        migration.migrate_model(DEMO["model_m1"])
        # 外部塞进映射表的幽灵字段，不属于 M1 整组、目录里也不存在
        ctx["store"].rows(MAPPING_TABLE).append({
            "id": 999, "field_identity": "v2://mysql/SRC-1#event_id",
            "field_key": ["mysql", "SRC-1", "event_id"],
            "source_type": "mysql", "source_no": "SRC-1", "field_name": "event_id",
            "source_ref": "v2://mysql/SRC-1", "status": "confirmed",
            "consumers": ["external:ghost"], "created_at": "", "updated_at": ""})
        result = ctx["semantic"].promote_model(DEMO["model_m1"])
        # M1 自身整组完整，幽灵字段因不在清单组内而不被采信，上线仍成功
        assert result["ok"]
        assert 999 not in result["adopted_mapping_ids"]

    def test_consume_requires_migrated_data(self, ctx):
        assert ctx["semantic"].consume_migrated_model(DEMO["model_m1"])["ok"] is False
        ctx["migration"].migrate_model(DEMO["model_m1"])
        consumed = ctx["semantic"].consume_migrated_model(DEMO["model_m1"])
        assert consumed["ok"] and len(consumed["resolved_fields"]) == 4


class TestRollback:
    def test_rollback_restores_rewritten_record(self, ctx):
        migration = ctx["migration"]
        migration.migrate_model(DEMO["model_m1"])
        result = migration.rollback(MODEL_RESOURCE, DEMO["model_m1"])
        assert result["ok"]
        row = ctx["store"].find(MODEL_RESOURCE, DEMO["model_m1"])
        assert "v2_field_identities" not in row
        assert row["源表映射"] == "SRC-1, SRC-10"
        manifest = migration.get_manifest(MODEL_RESOURCE, DEMO["model_m1"])
        assert manifest["status"] == "rolled_back"

    def test_rollback_removes_owned_mappings_keeps_shared(self, ctx):
        migration = ctx["migration"]
        migration.migrate_model(DEMO["model_m1"])
        migration.migrate_model(DEMO["model_m2"])
        migration.rollback(MODEL_RESOURCE, DEMO["model_m1"])
        keys = {tuple(r["field_key"]) for r in migration.mappings()}
        assert ("postgresql", "SRC-10", "event_id") not in keys
        assert ("clickhouse", "SRC-1-V2", "metric_value") in keys

    def test_rollback_idempotent_and_history_readable(self, ctx):
        migration = ctx["migration"]
        migration.migrate_model(DEMO["model_m1"])
        first = migration.rollback(MODEL_RESOURCE, DEMO["model_m1"])
        second = migration.rollback(MODEL_RESOURCE, DEMO["model_m1"])
        assert not first["idempotent"] and second["idempotent"]
        versions = migration.history_versions(MODEL_RESOURCE, DEMO["model_m1"])
        assert [v["action"] for v in versions] == ["migrate", "rollback"]
        # 回滚后历史快照仍可还原迁移前内容
        snapshots = [h for h in ctx["store"].rows(HISTORY_TABLE)
                     if h["action"] == "migrate"]
        assert snapshots[0]["before"]["源表映射"] == "SRC-1, SRC-10"

    def test_promotion_rejected_after_rollback(self, ctx):
        ctx["migration"].migrate_model(DEMO["model_m1"])
        ctx["semantic"].promote_model(DEMO["model_m1"])
        ctx["migration"].rollback(MODEL_RESOURCE, DEMO["model_m1"])
        assert ctx["semantic"].promote_model(DEMO["model_m1"])["ok"] is False


class TestChangePropagation:
    def test_change_follows_v2_mapping_only(self, ctx):
        ctx["migration"].migrate_model(DEMO["model_m1"])
        ctx["semantic"].promote_model(DEMO["model_m1"])
        result = ctx["semantic"].propagate_field_change(
            "postgresql", "SRC-10", "event_id", change="DROP")
        assert [a["model_id"] for a in result["affected_models"]] == [DEMO["model_m1"]]
        row = ctx["store"].find(MODEL_RESOURCE, DEMO["model_m1"])
        assert row["v2_pending_changes"][0]["field_identity"] == \
            "v2://postgresql/SRC-10#event_id"

    def test_change_without_mapping_rejected(self, ctx):
        result = ctx["semantic"].propagate_field_change(
            "mysql", "SRC-1", "region", change="DROP")
        assert result["ok"] is False and result["code"] == "no_mapping"

    def test_manifests_table_shape(self, ctx):
        ctx["migration"].migrate_contract(DEMO["contract_c1"])
        manifest = ctx["migration"].get_manifest(CONTRACT_RESOURCE, DEMO["contract_c1"])
        assert set(manifest) >= {
            "resource", "consumer_id", "version", "status", "input_fingerprint",
            "batch_id", "v2_source_refs", "confirmed_field_keys", "pending_review",
            "adopted_mapping_ids",
        }
        assert ctx["store"].rows(MANIFEST_TABLE)  # 表已在 store 中建立
