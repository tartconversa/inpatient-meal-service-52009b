"""可读查询：需求兑现与浪费、锅次实耗与用途成本、患者餐回溯。

三个查询分别对应膳食中心的核对视角：
- 按餐次×病区看需求兑现与浪费(制作/送出/领取/废弃分列，不只留净餐数)；
- 按锅次看实耗、与标准配方的差额及各用途(患者/职工)成本；
- 从患者餐记录回溯对应锅次与费用资格，费用资格不覆盖实际供餐。
"""
import json

from .facts import latest_build


def _fen_to_yuan(fen):
    return round(fen / 100, 2) if fen is not None else None


def _resolve_build(con, build_id):
    return build_id if build_id is not None else latest_build(con)


def fulfillment(con, build_id=None, meal_at=None, ward=None):
    """按餐次×病区：需求、送出、领取、损耗与费用资格。

    病区归属取餐次时点的订餐病区(最终服务归属)；送出未领、明确废弃
    为不同损耗原因，分别列示；制作未送出份在锅次查询中按用途列示。
    """
    build_id = _resolve_build(con, build_id)
    slots = con.execute(
        "SELECT slot_id, meal_at, ward_at_meal, state_at_meal, demanded "
        "FROM fact_slot WHERE build_id = ?", [build_id]).fetchall()
    portions = con.execute(
        "SELECT slot_id, service_ward, served, sent_not_served, discarded, "
        "ward_conflict FROM fact_portion WHERE build_id = ? AND slot_id IS NOT NULL",
        [build_id]).fetchall()
    fees = con.execute(
        "SELECT slot_id, eligible, fee_fen FROM fact_fee WHERE build_id = ?",
        [build_id]).fetchall()
    slot_meal = {r[0]: r[1] for r in slots}
    fee_by_slot = {r[0]: (r[1], r[2]) for r in fees}
    groups = {}

    def _group(meal, group_ward):
        key = (meal, group_ward)
        if key not in groups:
            groups[key] = {
                "meal_at": meal, "ward": group_ward,
                "demanded": 0, "cancelled": 0, "state_unknown": 0,
                "sent": 0, "served": 0, "sent_not_served": 0,
                "discarded": 0, "ward_conflicts": 0,
                "fee_eligible": 0, "fee_fen": 0,
            }
        return groups[key]

    for slot_id, meal, slot_ward, state, demanded in slots:
        row = _group(meal, slot_ward)
        if demanded:
            row["demanded"] += 1
        if state == "cancelled":
            row["cancelled"] += 1
        elif state == "unknown":
            row["state_unknown"] += 1
        eligible, fee_fen = fee_by_slot.get(slot_id, (None, None))
        if eligible:
            row["fee_eligible"] += 1
            row["fee_fen"] += fee_fen or 0
    for slot_id, service_ward, served, not_served, discarded, conflict in portions:
        row = _group(slot_meal.get(slot_id), service_ward)
        row["sent"] += 1
        row["served"] += 1 if served else 0
        row["sent_not_served"] += 1 if not_served else 0
        row["discarded"] += 1 if discarded else 0
        row["ward_conflicts"] += 1 if conflict else 0
    result = sorted(groups.values(),
                    key=lambda r: (r["meal_at"] or "", r["ward"] or ""))
    for row in result:
        row["fee_yuan"] = _fen_to_yuan(row["fee_fen"])
    if meal_at is not None:
        result = [r for r in result if r["meal_at"] == meal_at]
    if ward is not None:
        result = [r for r in result if r["ward"] == ward]
    return result


def pot_cost(con, build_id=None, pot_id=None):
    """按锅次：实耗、与标准配方差额、各用途成本；缺证据时成本未决。"""
    build_id = _resolve_build(con, build_id)
    params = [build_id]
    filter_sql = ""
    if pot_id is not None:
        filter_sql = " AND pot_id = ?"
        params.append(pot_id)
    pots = con.execute(
        "SELECT pot_id, pot_known, recipe_version, made_at, cutoff_at, produced, "
        "sent, served, sent_not_served, discarded, made_not_sent, cost_status, "
        "pending_reason, cost_fen, patient_fen, staff_fen "
        "FROM fact_pot WHERE build_id = ?" + filter_sql + " ORDER BY pot_id",
        params).fetchall()
    products = con.execute(
        "SELECT pot_id, product_id, meal_type, audience, produced, sent, "
        "made_not_sent FROM fact_product WHERE build_id = ?" + filter_sql +
        " ORDER BY product_id", params).fetchall()
    ingredients = con.execute(
        "SELECT pot_id, ingredient, issued_g, returned_g, net_g, cost_yuan, "
        "standard_g, variance_g, status, pending_reason "
        "FROM fact_pot_ingredient WHERE build_id = ?" + filter_sql +
        " ORDER BY ingredient", params).fetchall()
    allocs = con.execute(
        "SELECT pot_id, product_id, meal_type, audience, portions, ingredient, "
        "base_g, alloc_g, alloc_fen FROM fact_alloc WHERE build_id = ?" +
        filter_sql + " ORDER BY product_id, ingredient", params).fetchall()

    def _by_pot(rows):
        grouped = {}
        for row in rows:
            grouped.setdefault(row[0], []).append(row)
        return grouped

    products_by_pot = _by_pot(products)
    ingredients_by_pot = _by_pot(ingredients)
    allocs_by_pot = _by_pot(allocs)
    result = []
    for row in pots:
        pid = row[0]
        purposes = {}
        for alloc in allocs_by_pot.get(pid, []):
            key = (alloc[3], alloc[2])  # (用途, 餐种)
            purpose = purposes.setdefault(key, {
                "audience": alloc[3], "meal_type": alloc[2],
                "portions": alloc[4], "cost_fen": 0})
            purpose["cost_fen"] += alloc[8]
        purpose_list = sorted(purposes.values(),
                              key=lambda p: (p["audience"], p["meal_type"]))
        for purpose in purpose_list:
            purpose["cost_yuan"] = _fen_to_yuan(purpose["cost_fen"])
        result.append({
            "pot_id": pid,
            "pot_known": row[1],
            "recipe_version": row[2],
            "made_at": row[3],
            "cutoff_at": row[4],
            "production": {
                "produced": row[5], "sent": row[6], "served": row[7],
                "sent_not_served": row[8], "discarded": row[9],
                "made_not_sent": row[10],
            },
            "products": [{"product_id": p[1], "meal_type": p[2],
                          "audience": p[3], "produced": p[4], "sent": p[5],
                          "made_not_sent": p[6]}
                         for p in products_by_pot.get(pid, [])],
            "ingredients": [{"ingredient": i[1], "issued_g": i[2],
                             "returned_g": i[3], "net_g": i[4],
                             "cost_yuan": i[5], "standard_g": i[6],
                             "variance_g": i[7], "status": i[8],
                             "pending_reason": i[9]}
                            for i in ingredients_by_pot.get(pid, [])],
            "purposes": purpose_list,
            "cost_status": row[11],
            "pending_reason": row[12],
            "cost_fen": row[13],
            "cost_yuan": _fen_to_yuan(row[13]),
            "patient_fen": row[14],
            "patient_yuan": _fen_to_yuan(row[14]),
            "staff_fen": row[15],
            "staff_yuan": _fen_to_yuan(row[15]),
        })
    return result


def patient_trace(con, build_id=None, slot_id=None, portion_id=None):
    """从患者餐记录回溯：槽位状态、送出份、回执、对应锅次与费用资格。"""
    build_id = _resolve_build(con, build_id)
    if slot_id is None and portion_id is None:
        raise ValueError("需要 slot_id 或 portion_id")
    if slot_id is None:
        row = con.execute(
            "SELECT slot_id FROM fact_portion WHERE build_id = ? AND portion_id = ?",
            [build_id, portion_id]).fetchone()
        if not row:
            raise ValueError("餐份不存在：" + portion_id)
        slot_id = row[0]
    slot_rows = con.execute(
        "SELECT slot_id, person, meal_at, original_ward, meal_type_at_meal, "
        "ward_at_meal, state_at_meal, demanded, cancel_effective "
        "FROM fact_slot WHERE build_id = ? AND slot_id = ?",
        [build_id, slot_id]).fetchall()
    if not slot_rows:
        raise ValueError("槽位不存在：" + slot_id)
    fee_rows = con.execute(
        "SELECT slot_id, state, cancel_effective, pot_id, cutoff_at, eligible, "
        "fee_fen, status, reason FROM fact_fee "
        "WHERE build_id = ? AND slot_id = ?", [build_id, slot_id]).fetchall()
    portion_rows = con.execute(
        "SELECT portion_id, pot_id, meal_type, sent_at, ward_at_sent, "
        "service_ward, ward_receipts, person_receipts, discard_receipts, "
        "served, serve_basis, ward_conflict, discarded, pot_known "
        "FROM fact_portion WHERE build_id = ? AND slot_id = ? "
        "ORDER BY portion_id", [build_id, slot_id]).fetchall()
    alloc_rows = con.execute(
        "SELECT pot_id, meal_type, SUM(alloc_fen) "
        "FROM fact_alloc WHERE build_id = ? AND audience = 'patient' "
        "GROUP BY pot_id, meal_type", [build_id]).fetchall()
    line_fen = {(r[0], r[1]): r[2] for r in alloc_rows}
    product_rows = con.execute(
        "SELECT pot_id, meal_type, SUM(produced) "
        "FROM fact_product WHERE build_id = ? AND audience = 'patient' "
        "GROUP BY pot_id, meal_type", [build_id]).fetchall()
    line_portions = {(r[0], r[1]): r[2] for r in product_rows}
    pot_rows = con.execute(
        "SELECT pot_id, made_at, cutoff_at, cost_status FROM fact_pot "
        "WHERE build_id = ?", [build_id]).fetchall()
    pot_info = {r[0]: r[1:] for r in pot_rows}

    def _portion(row):
        (pid, pot_id, meal_type, sent_at, ward_at_sent, service_ward,
         ward_rcpts, person_rcpts, discards, served, basis, conflict,
         discarded, pot_known) = row
        fen = line_fen.get((pot_id, meal_type))
        line_size = line_portions.get((pot_id, meal_type))
        per_portion_fen = None
        if fen is not None and line_size:
            per_portion_fen = round(fen / line_size, 2)
        pot = pot_info.get(pot_id)
        return {
            "portion_id": pid,
            "pot_id": pot_id,
            "pot_known": pot_known,
            "pot_made_at": pot[0] if pot else None,
            "pot_cutoff_at": pot[1] if pot else None,
            "pot_cost_status": pot[2] if pot else None,
            "meal_type": meal_type,
            "sent_at": sent_at,
            "ward_at_sent": ward_at_sent,
            "service_ward": service_ward,
            "ward_receipts": json.loads(ward_rcpts),
            "person_receipts": json.loads(person_rcpts),
            "discard_receipts": json.loads(discards),
            "served": served,
            "serve_basis": basis,
            "ward_conflict": conflict,
            "discarded": discarded,
            "portion_cost_fen": per_portion_fen,
            "portion_cost_yuan": _fen_to_yuan(per_portion_fen),
        }

    s = slot_rows[0]
    fees = [{"state": f[1], "cancel_effective": f[2], "pot_id": f[3],
             "cutoff_at": f[4], "eligible": f[5], "fee_fen": f[6],
             "fee_yuan": _fen_to_yuan(f[6]), "status": f[7], "reason": f[8]}
            for f in fee_rows]
    return {
        "slot_id": s[0],
        "person": s[1],
        "meal_at": s[2],
        "original_ward": s[3],
        "meal_type_at_meal": s[4],
        "ward_at_meal": s[5],
        "state_at_meal": s[6],
        "demanded": s[7],
        "cancel_effective": s[8],
        "portions": [_portion(r) for r in portion_rows],
        "fee": fees,
        "note": "费用资格不覆盖实际供餐；病区回执不证明个人领取",
    }
