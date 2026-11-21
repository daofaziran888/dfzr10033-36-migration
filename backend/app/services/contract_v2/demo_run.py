"""V2 契约迁移端到端演示（隔离示例数据，独立于全局 store）。

运行（在 backend/ 目录，-E 忽略外部环境变量、不加载用户级 site 包）：

    PYTHONPATH=. python3 -E -m app.services.contract_v2.demo_run

脚本顺序：
1. 用前缀重合数据复现 V1 字符串误中，并对照 V2 精确解析；
2. 迁移契约与模型（含歧义/未知保留、整组幂等确认）；
3. 模型上线门禁（未迁移冲突、待核实拦截、整组映射证明、采用映射回写）；
4. 变更传播；
5. 回滚并核对被改写记录的还原情况与完整历史版本。
"""
from __future__ import annotations

from app.services.contract_v2.demo_data import DEMO, build_demo_store
from app.services.contract_v2.identities import legacy_substring_match
from app.services.contract_v2.migration import (
    CONTRACT_RESOURCE,
    HISTORY_TABLE,
    MANIFEST_TABLE,
    MAPPING_TABLE,
    MODEL_RESOURCE,
    ContractMigrationV2Service,
)
from app.services.contract_v2.promotion import SemanticModelV2Service

SEP = "=" * 72


def _title(text: str) -> None:
    print(f"\n{SEP}\n{text}\n{SEP}")


def _kv(label: str, value: object) -> None:
    print(f"  - {label}: {value}")


def main() -> int:
    store = build_demo_store()
    migration = ContractMigrationV2Service(store)
    semantic = SemanticModelV2Service(store)

    contract_c1 = DEMO["contract_c1"]
    contract_c2 = DEMO["contract_c2"]
    model_m1 = DEMO["model_m1"]
    model_m2 = DEMO["model_m2"]
    model_m3 = DEMO["model_m3"]
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        mark = "PASS" if condition else "FAIL"
        print(f"  [{mark}] {label}" + (f" —— {detail}" if detail else ""))
        if not condition:
            failures.append(label)

    # --------------------------------------------------------------- #
    _title("0. 迁移前：V1 字符串包含匹配的误中 vs V2 精确解析")
    c1_dataset = store.find(CONTRACT_RESOURCE, contract_c1)["数据集"]
    m2_mapping = store.find(MODEL_RESOURCE, model_m2)["源表映射"]
    # V1 拿短编号在长文本里做子串匹配：含 SRC-1-V2 的文本会被误判为引用了 SRC-1
    v1_short_on_v2_text = legacy_substring_match(m2_mapping, DEMO["overlap_no_short"])
    v1_v2no_on_c1 = legacy_substring_match(c1_dataset, DEMO["v2_no"])
    print("  C1 数据集文本:", repr(c1_dataset), "| M2 源表文本:", repr(m2_mapping))
    _kv("V1: 'SRC-1' in 'SRC-1-V2' → 误判 M2 引用了 SRC-1", v1_short_on_v2_text)
    _kv("V1: 'SRC-1-V2' in C1 文本 →", v1_v2no_on_c1)
    check("V1 复现前缀误中（短编号 SRC-1 误中已转换来源 SRC-1-V2）",
          v1_short_on_v2_text is True)

    catalog = migration.catalog()
    resolved = catalog.resolve_many(c1_dataset)
    for ref in resolved:
        print("  V2 解析 C1 记号:", ref.to_dict())
    check("V2 精确解析出 2 个来源且无待核实",
          len(resolved) == 2 and all(not r.pending_review for r in resolved))
    check("V2 不会把 SRC-1-V2 当作 C1 来源",
          all((r.source_ref is None or r.source_ref.source_no != DEMO["v2_no"])
              for r in resolved))
    m2_resolved = catalog.resolve_many(m2_mapping)
    check("V2 对 M2 文本只精确命中 SRC-1-V2，不会误落到 SRC-1",
          len(m2_resolved) == 1
          and m2_resolved[0].source_ref.source_no == DEMO["v2_no"])

    # --------------------------------------------------------------- #
    _title("1. 迁移契约 C1（直接转换）与 C2（歧义/未知保留原输入）")
    r_c1 = migration.migrate_contract(contract_c1)
    r_c1_again = migration.migrate_contract(contract_c1)
    print("  C1 首次迁移:", {k: r_c1[k] for k in ("ok", "idempotent", "batch_id")})
    print("  C1 重复提交:", {k: r_c1_again[k] for k in ("ok", "idempotent", "batch_id")})
    check("C1 迁移成功且首次非幂等", r_c1["ok"] and not r_c1["idempotent"])
    check("C1 重复提交只确认一次（幂等、同批次）",
          r_c1_again["idempotent"] and r_c1_again["batch_id"] == r_c1["batch_id"])

    c1_row = store.find(CONTRACT_RESOURCE, contract_c1)
    print("  C1 被改写后的 V2 列:", c1_row.get("v2_dataset_refs"))
    print("  C1 原始字符串列保留:", repr(c1_row.get("数据集")))
    check("C1 写入结构化来源引用", c1_row["v2_dataset_refs"] == [
        "v2://mysql/SRC-1", "v2://postgresql/SRC-10"])

    r_c2 = migration.migrate_contract(contract_c2)
    print("  C2 迁移结果（含待核实）:")
    for item in r_c2["pending_review"]:
        print("    *", item)
    check("C2 两个记号全部待核实", r_c2["pending_review_count"] == 2)
    c2_manifest = migration.get_manifest(CONTRACT_RESOURCE, contract_c2)
    check("待核实项不生成 confirmed 映射",
          c2_manifest["confirmed_field_keys"] == [] and
          {p["raw_input"] for p in c2_manifest["pending_review"]} == {"SRC-2", "SRC-GHOST"})

    # --------------------------------------------------------------- #
    _title("2. 模型迁移：M1 完整字段身份，M2 部分迁移冲突场景")
    # 先证明 M2 未迁移时上线被门禁拒绝（其依赖来源已转 V2）
    pre_m2 = semantic.promote_model(model_m2)
    print("  M2 未迁移直接上线:", pre_m2["ok"], "|", pre_m2["message"])
    check("未迁移模型禁止上线（部分来源已转 V2 的冲突被拦截）", pre_m2["ok"] is False)

    r_m1 = migration.migrate_model(model_m1)
    print("  M1 迁移:", {k: r_m1[k] for k in ("ok", "confirmed_mappings",
                                              "pending_review_count", "batch_id")})
    check("M1 4 个字段全部生成 V2 来源字段映射", r_m1["confirmed_mappings"] == 4)
    check("M1 无待核实项", r_m1["pending_review_count"] == 0)

    m1_row = store.find(MODEL_RESOURCE, model_m1)
    print("  M1 V2 字段身份:", *m1_row["v2_field_identities"], sep="\n    ")
    print("  M1 V2 来源引用:", m1_row["v2_source_refs"])
    check("M1 旧字段保留、新身份列并存",
          m1_row["源表映射"] == "SRC-1, SRC-10" and len(m1_row["v2_field_identities"]) == 4)

    r_m2 = migration.migrate_model(model_m2)
    print("  M2 迁移 confirmed:", r_m2["confirmed_mappings"],
          "pending:", r_m2["pending_review_count"])
    check("M2 迁移后 3 个字段身份确认", r_m2["confirmed_mappings"] == 3)

    # 防 chimera：构造一条跨来源拼接的“幽灵字段”映射（event_id 实际属于 SRC-10）。
    # 它不进入 M1 的清单字段组，且在 SRC-1 登记目录中不存在，上线门禁不会采信。
    ghost_identity = "v2://mysql/SRC-1#event_id"  # event_id 实际属于 SRC-10
    store.rows(MAPPING_TABLE).append({
        "id": 999,
        "field_identity": ghost_identity,
        "field_key": ["mysql", "SRC-1", "event_id"],
        "source_type": "mysql", "source_no": "SRC-1", "field_name": "event_id",
        "source_ref": "v2://mysql/SRC-1", "status": "confirmed",
        "consumers": ["external:ghost"],
        "created_at": "2026-11-21", "updated_at": "2026-11-21",
    })
    # 该幽灵身份不在 SRC-1 登记目录中，且不属于 M1 这一整组映射
    m1_manifest_pre = migration.get_manifest(MODEL_RESOURCE, model_m1)
    check("幽灵字段在来源登记目录不存在（无法用不同来源拼字段）",
          catalog.has_field(
              next(e for e in catalog.entries() if e.source_no == "SRC-1").ref,
              "event_id") is False)
    check("幽灵字段不属于 M1 整组映射，不会被上线采信",
          ("mysql", "SRC-1", "event_id")
          not in {tuple(k) for k in m1_manifest_pre["confirmed_field_keys"]})

    # --------------------------------------------------------------- #
    _title("3. 模型上线门禁与映射消费")
    # C2 尚未复核；M3 引用待核实来源，迁移后尝试上线
    migration.migrate_model(model_m3)
    pre_m3 = semantic.promote_model(model_m3)
    print("  M3 带待核实项上线:", pre_m3["ok"], "|", pre_m3.get("code"))
    check("待复核来源不会被静默纳入上线", pre_m3["ok"] is False
          and pre_m3.get("code") == "pending_review")

    promote_m1 = semantic.promote_model(model_m1)
    print("  M1 上线:", promote_m1)
    check("M1 上线成功并回写采用映射",
          promote_m1["ok"] and len(promote_m1["adopted_mapping_ids"]) == 4)
    m1_manifest = migration.get_manifest(MODEL_RESOURCE, model_m1)
    check("实际采用映射已回写迁移清单",
          m1_manifest["adopted_mapping_ids"] == promote_m1["adopted_mapping_ids"])
    check("M1 状态=已投产", store.find(MODEL_RESOURCE, model_m1)["status"] == "已投产")

    consumed = semantic.consume_migrated_model(model_m1)
    print("  语义层消费 M1 字段数:", len(consumed["resolved_fields"]))
    check("语义模型只消费迁移完成的数据", consumed["ok"]
          and len(consumed["resolved_fields"]) == 4)

    promote_m2 = semantic.promote_model(model_m2)
    print("  M2 上线:", promote_m2["ok"], "|", promote_m2.get("code"))
    check("M2 迁移完整后同样可上线", promote_m2["ok"])

    # 重复上线：映射组未变，清单回写保持稳定
    promote_m1_again = semantic.promote_model(model_m1)
    check("重复上线不改变采用映射集合",
          promote_m1_again["adopted_mapping_ids"] == promote_m1["adopted_mapping_ids"])

    # --------------------------------------------------------------- #
    _title("4. 变更沿 V2 映射传播（不靠字符串猜测）")
    prop = semantic.propagate_field_change(
        "postgresql", "SRC-10", "event_id", change="TYPE_CHANGE:varchar->uuid")
    print("  传播结果:", prop)
    check("变更精确传播到 M1",
          prop["ok"] and [a["model_id"] for a in prop["affected_models"]] == [model_m1])
    m1_after = store.find(MODEL_RESOURCE, model_m1)
    check("M1 记录待处理变更事件",
          len(m1_after.get("v2_pending_changes", [])) == 1
          and m1_after["pending"] is True)
    # SRC-1 没有 region 之外... 选取一个合法来源身份但无映射记录的字段，验证不沿字符串传播
    prop_miss = semantic.propagate_field_change(
        "mysql", "SRC-1", "region", change="TYPE_CHANGE:unmapped")
    check("无 V2 映射的字段不传播（不回退字符串猜测）", prop_miss["ok"] is False
          and prop_miss.get("code") == "no_mapping")

    # --------------------------------------------------------------- #
    _title("5. 回滚：还原被改写记录，历史版本全程可读")
    versions_before = migration.history_versions(MODEL_RESOURCE, model_m1)
    print("  回滚前 M1 历史版本:", versions_before)
    rollback = migration.rollback(MODEL_RESOURCE, model_m1)
    rollback_again = migration.rollback(MODEL_RESOURCE, model_m1)
    print("  首次回滚:", {k: rollback[k] for k in ("ok", "idempotent", "batch_id",
                                                    "restored_snapshot_of")})
    print("  重复回滚:", {k: rollback_again[k] for k in ("ok", "idempotent")})
    check("回滚成功且重复回滚幂等",
          rollback["ok"] and not rollback["idempotent"] and rollback_again["idempotent"])

    m1_restored = store.find(MODEL_RESOURCE, model_m1)
    print("  回滚后 M1 键:", sorted(m1_restored.keys()))
    check("回滚删除迁移期 V2 列",
          "v2_field_identities" not in m1_restored
          and "v2_manifest_id" not in m1_restored)
    check("回滚恢复旧字符串原值",
          m1_restored["源表映射"] == "SRC-1, SRC-10"
          and m1_restored["字段别名"] == "user_id, order_amount, event_id, event_type")
    check("回滚后上线门禁重新拒绝旧格式",
          semantic.promote_model(model_m1)["ok"] is False)

    # M1 独占的映射被移除；被其他消费者共享的映射保留
    remaining_keys = {tuple(r["field_key"]) for r in migration.mappings()}
    check("M1 独占映射记录已随回滚移除",
          ("postgresql", "SRC-10", "event_type") not in remaining_keys)
    check("共享/其他消费者映射仍保留（M2 与 C 侧记录不受影响）",
          ("clickhouse", "SRC-1-V2", "metric_value") in remaining_keys)

    versions_after = migration.history_versions(MODEL_RESOURCE, model_m1)
    print("  回滚后 M1 全部历史版本:")
    for version in versions_after:
        print("    ", version)
    actions = [v["action"] for v in versions_after]
    check("迁移与回滚历史均可读且顺序完整",
          actions == ["migrate", "rollback"])
    check("历史中保留改写前快照可还原",
          any(h["resource"] == MODEL_RESOURCE and h["consumer_id"] == model_m1
              and h["action"] == "migrate"
              and h["before"]["源表映射"] == "SRC-1, SRC-10"
              for h in store.rows(HISTORY_TABLE)))

    # 清单状态终态
    print("\n  迁移清单终态:")
    for manifest in store.rows(MANIFEST_TABLE):
        print(f"    - {manifest['resource']}:{manifest['consumer_id']} "
              f"v{manifest['version']} {manifest['status']} "
              f"confirmed={len(manifest['confirmed_field_keys'])} "
              f"pending={len(manifest['pending_review'])} "
              f"adopted={manifest['adopted_mapping_ids']}")

    _title(f"演示结束：{len(failures)} 个检查失败")
    if failures:
        print("  失败项:")
        for label in failures:
            print("   *", label)
        return 1
    print("  全部检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
