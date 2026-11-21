"""V2 迁移示例数据集：刻意制造多标识格式的前缀重合与歧义。

隔离构造（不复用全局 store、不加载 V1 种子），供演示脚本与测试使用：

- ``SRC-1``        旧式 mysql 来源
- ``SRC-10``       旧式 postgresql 来源，编号与 ``SRC-1`` 前缀重合
- ``SRC-1-V2``     已转换的 V2 来源，编号同时以 ``SRC-1`` 为前缀
- 两个 ``SRC-2``   同编号不同来源类型（clickhouse / kafka），裸记号无法唯一确定
- ``SRC-GHOST``    仅在文本里出现、未登记的来源编号

V1 的 ``编号 in 文本`` 在这组数据上必然互相误中；V2 精确解析则逐条可判定。
"""
from __future__ import annotations

from typing import Any

from app.store import Store


def _source(
    id_: int, no: str, stype: str, fields: list[str], *, converted: bool = False
) -> dict[str, Any]:
    return {
        "id": id_,
        "源编号": no,
        "源类型": stype,
        "连接地址": f"db://demo/{stype}/{id_}",
        "驱动配置": f"{stype}_driver",
        "数据库名": "demo_db",
        "认证方式": "token",
        "接入状态": "已注册",
        "最近同步": "2026-11-21",
        "v2_converted": converted,
        "v2_fields": fields,
        "status": "草稿",
        "pending": True,
        "abnormal": False,
    }


def build_demo_store() -> Store:
    """返回只含 V2 演示数据的隔离 store。"""
    store = Store.__new__(Store)
    # 绕过 __init__ 中的 V1 种子装载，给一个空表集合
    store._tables = {}  # type: ignore[attr-defined]

    store.rows("source_registry").extend([
        _source(1, "SRC-1", "mysql",
                ["id", "user_id", "order_amount", "order_time", "region"]),
        _source(2, "SRC-10", "postgresql",
                ["id", "event_id", "event_type", "event_time"]),
        _source(3, "SRC-1-V2", "clickhouse",
                ["date", "metric_value", "dimension_key"], converted=True),
        _source(4, "SRC-2", "clickhouse",
                ["date", "metric_value", "partition_id"]),
        _source(5, "SRC-2", "kafka",
                ["topic", "partition", "offset", "key", "value"]),
    ])

    # 契约 C1：两个前缀重合的旧式来源
    store.rows("data_contract").append({
        "id": 1,
        "契约编号": "CONTRACT-C1",
        "数据集": "SRC-1, SRC-10",
        "Schema约束": "id,user_id,event_id",
        "SLA定义": "每小时更新",
        "更新频率": "3600",
        "负责人": "demo",
        "消费者": "bi_dashboard",
        "生效时间": "2026-11-21",
        "status": "草稿",
        "pending": True,
        "abnormal": False,
    })

    # 契约 C2：裸 SRC-2 有歧义（两种类型），SRC-GHOST 未登记 —— 均保留原输入待核实
    store.rows("data_contract").append({
        "id": 2,
        "契约编号": "CONTRACT-C2",
        "数据集": "SRC-2, SRC-GHOST",
        "Schema约束": "topic,offset",
        "SLA定义": "每日更新",
        "更新频率": "86400",
        "负责人": "demo",
        "消费者": "risk_pipeline",
        "生效时间": "2026-11-21",
        "status": "草稿",
        "pending": True,
        "abnormal": False,
    })

    # 模型 M1：完整可读，字段在来源目录中归属唯一
    store.rows("semantic_model").append({
        "id": 1,
        "模型编号": "MODEL-M1",
        "模型名称": "订单事件模型",
        "源表映射": "SRC-1, SRC-10",
        "字段别名": "user_id, order_amount, event_id, event_type",
        "度量定义": "SUM(order_amount)",
        "维度定义": "region,event_type",
        "计算口径": "标准口径",
        "版本序号": "v1.0",
        "status": "草稿",
        "pending": True,
        "abnormal": False,
    })

    # 模型 M2：依赖已转 V2 的 SRC-1-V2，但仍以旧式字符串声明 —— 部分迁移冲突
    store.rows("semantic_model").append({
        "id": 2,
        "模型编号": "MODEL-M2",
        "模型名称": "指标宽表模型",
        "源表映射": "SRC-1-V2",
        "字段别名": "date, metric_value, dimension_key",
        "度量定义": "SUM(metric_value)",
        "维度定义": "dimension_key,date",
        "计算口径": "标准口径",
        "版本序号": "v1.0",
        "status": "草稿",
        "pending": True,
        "abnormal": False,
    })

    # 模型 M3：引用裸 SRC-2（两种类型歧义）与未登记的 SRC-GHOST，迁移后必然有待核实项
    store.rows("semantic_model").append({
        "id": 3,
        "模型编号": "MODEL-M3",
        "模型名称": "待核实模型",
        "源表映射": "SRC-2, SRC-GHOST",
        "字段别名": "topic",
        "度量定义": "COUNT(*)",
        "维度定义": "topic",
        "计算口径": "-",
        "版本序号": "v1.0",
        "status": "草稿",
        "pending": True,
        "abnormal": False,
    })

    return store


# 演示过程中关心的关键编号，集中暴露给脚本与测试
DEMO = {
    "contract_c1": 1,
    "contract_c2": 2,
    "model_m1": 1,
    "model_m2": 2,
    "model_m3": 3,
    "overlap_no_short": "SRC-1",
    "overlap_no_long": "SRC-10",
    "v2_no": "SRC-1-V2",
    "ambiguous_no": "SRC-2",
    "unknown_no": "SRC-GHOST",
}
