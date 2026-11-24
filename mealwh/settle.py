"""结算快照：确认后只增不改，原结算依据保留供厨房和病区比较差异。

结算固化某一构建的费用资格行与锅次成本分摊行；结算后到达的转科补录、
原料退库等经新批次入仓、新构建、新结算反映，历史结算行不改动。
"""
from datetime import datetime, timezone

from .store import next_id


def settle(con, build_id, label, note=None):
    """对指定构建做一次结算确认，返回 settlement_id。"""
    exists = con.execute("SELECT 1 FROM build WHERE build_id = ?",
                         [build_id]).fetchone()
    if not exists:
        raise ValueError("构建不存在：" + str(build_id))
    settlement_id = next_id(con, "settlement", "settlement_id")
    created_at = datetime.now(timezone.utc).isoformat()
    con.execute("INSERT INTO settlement VALUES (?, ?, ?, ?, ?)",
                [settlement_id, label, build_id, created_at, note])
    con.execute(
        """
        INSERT INTO settlement_fee
        SELECT ?, slot_id, state, cancel_effective, pot_id, cutoff_at,
               eligible, fee_fen, status, reason
        FROM fact_fee WHERE build_id = ?
        """,
        [settlement_id, build_id],
    )
    # 已确认成本：按产出用途行固化分摊结果
    con.execute(
        """
        INSERT INTO settlement_cost
        SELECT ?, pot_id, product_id, meal_type, audience, portions,
               ingredient, base_g, alloc_g, alloc_fen, 'ok', NULL
        FROM fact_alloc WHERE build_id = ?
        """,
        [settlement_id, build_id],
    )
    # 未决成本：缺证据的锅次食材同样入结算，标明未决，不留白
    con.execute(
        """
        INSERT INTO settlement_cost
        SELECT ?, pot_id, '', NULL, NULL, NULL,
               ingredient, NULL, NULL, NULL, status, pending_reason
        FROM fact_pot_ingredient
        WHERE build_id = ? AND status <> 'ok'
        """,
        [settlement_id, build_id],
    )
    return settlement_id


def get_settlement(con, settlement_id):
    """结算快照全文：费用行与成本行。"""
    head = con.execute(
        "SELECT settlement_id, label, build_id, created_at, note "
        "FROM settlement WHERE settlement_id = ?",
        [settlement_id]).fetchone()
    if not head:
        raise ValueError("结算不存在：" + str(settlement_id))
    fees = con.execute(
        """
        SELECT slot_id, state, cancel_effective, pot_id, cutoff_at,
               eligible, fee_fen, status, reason
        FROM settlement_fee WHERE settlement_id = ? ORDER BY slot_id
        """,
        [settlement_id]).fetchall()
    costs = con.execute(
        """
        SELECT pot_id, product_id, meal_type, audience, portions, ingredient,
               base_g, alloc_g, alloc_fen, status, pending_reason
        FROM settlement_cost WHERE settlement_id = ?
        ORDER BY pot_id, product_id, ingredient
        """,
        [settlement_id]).fetchall()
    return {
        "settlement_id": head[0], "label": head[1], "build_id": head[2],
        "created_at": head[3], "note": head[4],
        "fees": [{"slot_id": r[0], "state": r[1], "cancel_effective": r[2],
                  "pot_id": r[3], "cutoff_at": r[4], "eligible": r[5],
                  "fee_fen": r[6], "status": r[7], "reason": r[8]}
                 for r in fees],
        "costs": [{"pot_id": r[0], "product_id": r[1], "meal_type": r[2],
                   "audience": r[3], "portions": r[4], "ingredient": r[5],
                   "base_g": r[6], "alloc_g": r[7], "alloc_fen": r[8],
                   "status": r[9], "pending_reason": r[10]}
                  for r in costs],
    }


def list_settlements(con):
    rows = con.execute(
        "SELECT settlement_id, label, build_id, created_at, note "
        "FROM settlement ORDER BY settlement_id"
    ).fetchall()
    return [{"settlement_id": r[0], "label": r[1], "build_id": r[2],
             "created_at": r[3], "note": r[4]} for r in rows]


def compare_settlements(con, first_id, second_id):
    """比较两次结算的依据差异：费用行按槽位、成本行按锅次/产出/食材对齐。"""
    first = get_settlement(con, first_id)
    second = get_settlement(con, second_id)

    def _diff(first_map, second_map):
        changes = []
        for key in sorted(set(first_map) | set(second_map)):
            before, after = first_map.get(key), second_map.get(key)
            if before is None:
                changes.append({"key": key, "change": "added", "after": after})
            elif after is None:
                changes.append({"key": key, "change": "removed", "before": before})
            elif before != after:
                changes.append({"key": key, "change": "changed",
                                "before": before, "after": after})
        return changes

    first_fees = {f["slot_id"]: {k: v for k, v in f.items() if k != "slot_id"}
                  for f in first["fees"]}
    second_fees = {f["slot_id"]: {k: v for k, v in f.items() if k != "slot_id"}
                   for f in second["fees"]}
    cost_key = lambda c: (c["pot_id"], c["product_id"], c["ingredient"])
    first_costs = {cost_key(c): {k: v for k, v in c.items()
                                 if k not in ("pot_id", "product_id", "ingredient")}
                   for c in first["costs"]}
    second_costs = {cost_key(c): {k: v for k, v in c.items()
                                  if k not in ("pot_id", "product_id", "ingredient")}
                    for c in second["costs"]}
    cost_changes = []
    for change in _diff(first_costs, second_costs):
        pot_id, product_id, ingredient = change.pop("key")
        cost_changes.append({**change, "key": {"pot_id": pot_id,
                                               "product_id": product_id,
                                               "ingredient": ingredient}})
    return {
        "first": first_id,
        "second": second_id,
        "fee_changes": _diff(first_fees, second_fees),
        "cost_changes": cost_changes,
    }
