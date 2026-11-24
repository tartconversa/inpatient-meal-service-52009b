"""仓库存储结构：入仓层、事实层与结算层。

时刻一律存 ISO-8601 文本：接收时刻规范为 UTC 以便按词典序比较，
业务时刻保留来源原有的 +08:00 偏移，不在仓内换算。
"""
from pathlib import Path

import duckdb

STATEMENTS = [
    # ---- 入仓层 ----
    """
    CREATE TABLE IF NOT EXISTS receive_batch (
        batch_id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL,
        received_at TEXT NOT NULL,
        note TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_file (
        file_id INTEGER PRIMARY KEY,
        batch_id TEXT NOT NULL,
        table_name TEXT NOT NULL,
        source_label TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        row_count INTEGER NOT NULL,
        content TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_row (
        table_name TEXT NOT NULL,
        record_key TEXT NOT NULL,
        row_hash TEXT NOT NULL,
        payload TEXT NOT NULL,
        batch_id TEXT NOT NULL,
        seq INTEGER NOT NULL
    )
    """,
    # ---- 事实层(按 build 固化) ----
    """
    CREATE TABLE IF NOT EXISTS build (
        build_id INTEGER PRIMARY KEY,
        as_of TEXT,
        created_at TEXT NOT NULL,
        batch_seq INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_slot (
        build_id INTEGER NOT NULL,
        slot_id TEXT NOT NULL,
        person TEXT,
        meal_at TEXT,
        original_ward TEXT,
        meal_type_at_meal TEXT,
        ward_at_meal TEXT,
        state_at_meal TEXT,
        demanded BOOLEAN,
        cancel_effective TEXT,
        PRIMARY KEY (build_id, slot_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_portion (
        build_id INTEGER NOT NULL,
        portion_id TEXT NOT NULL,
        pot_id TEXT,
        slot_id TEXT,
        meal_type TEXT,
        sent_at TEXT,
        person TEXT,
        ward_at_sent TEXT,
        service_ward TEXT,
        ward_receipts TEXT,
        person_receipts TEXT,
        discard_receipts TEXT,
        served BOOLEAN,
        serve_basis TEXT,
        ward_conflict BOOLEAN,
        discarded BOOLEAN,
        sent_not_served BOOLEAN,
        pot_known BOOLEAN,
        PRIMARY KEY (build_id, portion_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_product (
        build_id INTEGER NOT NULL,
        product_id TEXT NOT NULL,
        pot_id TEXT NOT NULL,
        meal_type TEXT,
        audience TEXT,
        produced INTEGER,
        sent INTEGER,
        made_not_sent INTEGER,
        PRIMARY KEY (build_id, product_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_pot (
        build_id INTEGER NOT NULL,
        pot_id TEXT NOT NULL,
        pot_known BOOLEAN,
        recipe_version TEXT,
        made_at TEXT,
        cutoff_at TEXT,
        produced INTEGER,
        sent INTEGER,
        served INTEGER,
        sent_not_served INTEGER,
        discarded INTEGER,
        made_not_sent INTEGER,
        cost_status TEXT,
        pending_reason TEXT,
        cost_fen INTEGER,
        patient_fen INTEGER,
        staff_fen INTEGER,
        PRIMARY KEY (build_id, pot_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_pot_ingredient (
        build_id INTEGER NOT NULL,
        pot_id TEXT NOT NULL,
        ingredient TEXT NOT NULL,
        issued_g DOUBLE,
        returned_g DOUBLE,
        net_g DOUBLE,
        cost_yuan DOUBLE,
        standard_g DOUBLE,
        variance_g DOUBLE,
        status TEXT,
        pending_reason TEXT,
        PRIMARY KEY (build_id, pot_id, ingredient)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_alloc (
        build_id INTEGER NOT NULL,
        pot_id TEXT NOT NULL,
        product_id TEXT NOT NULL,
        meal_type TEXT,
        audience TEXT,
        portions INTEGER,
        ingredient TEXT NOT NULL,
        base_g DOUBLE,
        alloc_g DOUBLE,
        alloc_fen INTEGER,
        PRIMARY KEY (build_id, pot_id, product_id, ingredient)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_fee (
        build_id INTEGER NOT NULL,
        slot_id TEXT NOT NULL,
        state TEXT,
        cancel_effective TEXT,
        pot_id TEXT,
        cutoff_at TEXT,
        eligible BOOLEAN,
        fee_fen INTEGER,
        status TEXT,
        reason TEXT,
        PRIMARY KEY (build_id, slot_id)
    )
    """,
    # ---- 结算层(只增不改) ----
    """
    CREATE TABLE IF NOT EXISTS settlement (
        settlement_id INTEGER PRIMARY KEY,
        label TEXT NOT NULL,
        build_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        note TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settlement_fee (
        settlement_id INTEGER NOT NULL,
        slot_id TEXT NOT NULL,
        state TEXT,
        cancel_effective TEXT,
        pot_id TEXT,
        cutoff_at TEXT,
        eligible BOOLEAN,
        fee_fen INTEGER,
        status TEXT,
        reason TEXT,
        PRIMARY KEY (settlement_id, slot_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settlement_cost (
        settlement_id INTEGER NOT NULL,
        pot_id TEXT NOT NULL,
        product_id TEXT NOT NULL,
        meal_type TEXT,
        audience TEXT,
        portions INTEGER,
        ingredient TEXT NOT NULL,
        base_g DOUBLE,
        alloc_g DOUBLE,
        alloc_fen INTEGER,
        status TEXT,
        pending_reason TEXT,
        PRIMARY KEY (settlement_id, pot_id, product_id, ingredient)
    )
    """,
]


def connect(path):
    """打开(必要时创建)仓库并确保表结构存在。"""
    con = duckdb.connect(str(Path(path)))
    for statement in STATEMENTS:
        con.execute(statement)
    return con


def next_id(con, table, column):
    """单调编号：不重用、不回填。"""
    row = con.execute(f"SELECT COALESCE(MAX({column}), 0) + 1 FROM {table}").fetchone()
    return int(row[0])
