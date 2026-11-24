"""多粒度事实构建。

由入仓原文按"可知截点"(as_of)推导并固化：

- 槽位事实：订餐变更时间线在餐次时点的状态，修订沿原槽位延续、不增餐；
- 餐份事实：送出、病区签收、本人领取、废弃分开记录；同一份餐凭 portion_id
  只算一次服务；病区回执不证明个人吃到；多病区回执记冲突待核实；
- 锅次事实：实际产出(含未送出份与职工份)、实耗=领-退、与标准配方的差额；
- 分摊事实：实耗成本按该锅实际产出的各餐种标准原料权重分担，分母包括
  已制作未送出份与职工份；金额舍入到分，尾差归分摊基数最大项，并列按业务键升序；
- 费用事实：取消时点与厨房收费截止比较得出费用资格，费用资格不覆盖实际供餐。

缺锅次或缺领退证据时，供餐事实仍可确认，食材成本标记未决(pending)。
成品退餐按合成卫生口径只记废弃，不恢复原料库存。
"""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json

from .store import next_id

FEE_PER_CANCEL_FEN = 2000  # 内部服务费：收费截止后取消每份 20 元

TABLES = ["meal_slots", "order_changes", "pots", "pot_product",
          "recipes", "ingredient_moves", "portions", "receipts"]

_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


def _dt(value):
    return datetime.fromisoformat(value) if value else None


def _dec(value):
    return Decimal(value) if value not in (None, "") else None


def _iso(moment):
    return moment.isoformat() if moment else None


def asof_rows(con, table, as_of):
    """取截点可知的最新行版本：同记录键按接收次序取新，晚知值不进历史截点。"""
    if as_of is None:
        sql = """
            SELECT payload FROM (
                SELECT payload,
                       ROW_NUMBER() OVER (PARTITION BY record_key ORDER BY seq DESC) AS rn
                FROM raw_row WHERE table_name = ?
            ) WHERE rn = 1
        """
        params = [table]
    else:
        sql = """
            SELECT payload FROM (
                SELECT payload,
                       ROW_NUMBER() OVER (PARTITION BY record_key ORDER BY seq DESC) AS rn
                FROM raw_row
                WHERE table_name = ?
                  AND seq IN (SELECT seq FROM receive_batch WHERE received_at <= ?)
            ) WHERE rn = 1
        """
        params = [table, as_of]
    return [json.loads(row[0]) for row in con.execute(sql, params).fetchall()]


def allocate_fen(total_fen, bases):
    """把账单总额(分)按分摊基数比例分到各项。

    各项舍入到分，尾差归分摊基数最大项，并列按业务键升序。
    """
    keys = sorted(bases)
    result = {key: 0 for key in keys}
    if total_fen == 0:
        return result
    total_base = sum(bases.values())
    if total_base <= 0:
        raise ValueError("分摊基数为零，无法分摊")
    for key in keys:
        share = Decimal(total_fen) * bases[key] / total_base
        result[key] = int(share.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    residual = total_fen - sum(result.values())
    if residual:
        target = sorted(keys, key=lambda k: (-bases[k], k))[0]
        result[target] += residual
    return result


class _Model:
    """一个截点下的来源行集合与派生事实。"""

    def __init__(self, raw):
        self.slots = {}
        for row in raw["meal_slots"]:
            self.slots[row["slot_id"]] = {
                "slot_id": row["slot_id"],
                "person": row["person"],
                "meal_at": _dt(row["meal_at"]),
                "original_ward": row["original_ward"],
            }
        self.changes = defaultdict(list)
        for row in raw["order_changes"]:
            self.changes[row["slot_id"]].append({
                "change_id": row["change_id"],
                "effective": _dt(row["effective"]),
                "meal_type": row["meal_type"],
                "ward": row["ward"],
                "state": row["state"],
            })
        for timeline in self.changes.values():
            timeline.sort(key=lambda c: (c["effective"] is None,
                                         c["effective"] or _MIN_DT,
                                         c["change_id"]))
        self.pots = {}
        for row in raw["pots"]:
            self.pots[row["pot_id"]] = {
                "pot_id": row["pot_id"],
                "recipe_version": row["recipe_version"],
                "made_at": _dt(row["made_at"]),
                "cutoff_at": _dt(row["cutoff_at"]),
            }
        self.products = [{
            "product_id": row["product_id"],
            "pot_id": row["pot_id"],
            "meal_type": row["meal_type"],
            "audience": row["audience"],
            "portions": int(row["portions"]),
        } for row in raw["pot_product"]]
        self.recipe_g = {}
        for row in raw["recipes"]:
            key = (row["version"], row["meal_type"], row["ingredient"])
            self.recipe_g[key] = _dec(row["grams"])
        self.moves = defaultdict(list)
        for row in raw["ingredient_moves"]:
            self.moves[row["pot_id"]].append({
                "move_id": row["move_id"],
                "ingredient": row["ingredient"],
                "kind": row["kind"],
                "grams": _dec(row["grams"]),
                "price": _dec(row["yuan_per_g"]),
                "at": _dt(row["at"]),
            })
        self.portions = [{
            "portion_id": row["portion_id"],
            "pot_id": row["pot_id"],
            "slot_id": row["slot_id"] or None,
            "meal_type": row["meal_type"],
            "sent_at": _dt(row["sent_at"]),
        } for row in raw["portions"]]
        self.receipts = defaultdict(list)
        for row in raw["receipts"]:
            self.receipts[row["portion_id"]].append({
                "receipt_id": row["receipt_id"],
                "kind": row["kind"],
                "party": row["party"],
                "at": _dt(row["at"]),
            })
        for items in self.receipts.values():
            items.sort(key=lambda r: (r["at"] is None, r["at"] or _MIN_DT,
                                      r["receipt_id"]))

    @staticmethod
    def _state_at(timeline, moment):
        """时间线上某业务时点生效的变更；无则 None。"""
        if moment is None:
            return None
        current = None
        for change in timeline:
            if change["effective"] is not None and change["effective"] <= moment:
                current = change
            else:
                break
        return current

    def slot_facts(self):
        """槽位在餐次时点的需求状态；修订沿原槽位延续，不增餐。"""
        facts = []
        for slot_id in sorted(self.slots):
            slot = self.slots[slot_id]
            at_meal = self._state_at(self.changes.get(slot_id, []), slot["meal_at"])
            if at_meal is None:
                state, meal_type, ward = "unknown", None, None
                demanded, cancel_effective = None, None
            else:
                state = at_meal["state"]
                meal_type = at_meal["meal_type"]
                ward = at_meal["ward"]
                demanded = state == "active"
                cancel_effective = at_meal["effective"] if state == "cancelled" else None
            facts.append({
                "slot_id": slot_id,
                "person": slot["person"],
                "meal_at": slot["meal_at"],
                "original_ward": slot["original_ward"],
                "meal_type_at_meal": meal_type,
                "ward_at_meal": ward,
                "state_at_meal": state,
                "demanded": demanded,
                "cancel_effective": cancel_effective,
            })
        return facts

    def portion_facts(self):
        """餐份事实：送出、签收、领取、废弃分层记录，同份只算一次服务。"""
        facts = []
        for portion in sorted(self.portions, key=lambda p: p["portion_id"]):
            slot = self.slots.get(portion["slot_id"]) if portion["slot_id"] else None
            timeline = self.changes.get(portion["slot_id"], []) if portion["slot_id"] else []
            at_sent = self._state_at(timeline, portion["sent_at"])
            at_meal = self._state_at(timeline, slot["meal_at"]) if slot else None
            receipts = self.receipts.get(portion["portion_id"], [])
            ward_rcpts = [r for r in receipts if r["kind"] == "ward_receipt"]
            person_rcpts = [r for r in receipts if r["kind"] == "person_receipt"]
            discards = [r for r in receipts if r["kind"] == "discard"]
            person = slot["person"] if slot else None
            # 当前资料无代理关系表：仅本人回执证明患者服务；
            # 代领取须有明确代理关系，病区收餐不证明个人吃到。
            served = bool(person) and any(r["party"] == person for r in person_rcpts)
            signed_wards = {r["party"] for r in ward_rcpts if r["party"]}
            facts.append({
                "portion_id": portion["portion_id"],
                "pot_id": portion["pot_id"],
                "slot_id": portion["slot_id"],
                "meal_type": portion["meal_type"],
                "sent_at": portion["sent_at"],
                "person": person,
                "ward_at_sent": at_sent["ward"] if at_sent else None,
                "service_ward": at_meal["ward"] if at_meal else None,
                "ward_receipts": ward_rcpts,
                "person_receipts": person_rcpts,
                "discard_receipts": discards,
                "served": served,
                "serve_basis": "本人回执" if served else "无本人或代理回执",
                "ward_conflict": len(signed_wards) > 1,
                "discarded": bool(discards),
                "sent_not_served": not served,
                "pot_known": portion["pot_id"] in self.pots,
            })
        return facts

    def product_facts(self):
        """锅次产出核对：制作、送出分开，未送出份保留为独立损耗原因。"""
        sent_count = defaultdict(int)
        for portion in self.portions:
            audience = "patient" if portion["slot_id"] else "staff"
            sent_count[(portion["pot_id"], portion["meal_type"], audience)] += 1
        facts = []
        for line in sorted(self.products, key=lambda l: l["product_id"]):
            key = (line["pot_id"], line["meal_type"], line["audience"])
            sent = sent_count.get(key, 0)
            facts.append({**line, "produced": line["portions"], "sent": sent,
                          "made_not_sent": line["portions"] - sent})
        return facts

    def ingredient_facts(self, products):
        """锅次食材事实：实耗=领-退，保留与标准配方的差额，缺证据记未决。"""
        lines_by_pot = defaultdict(list)
        for line in products:
            lines_by_pot[line["pot_id"]].append(line)
        pot_ids = set(self.pots) | set(lines_by_pot) | set(self.moves)
        pot_ids |= {p["pot_id"] for p in self.portions}
        facts = []
        for pot_id in sorted(pot_ids):
            pot = self.pots.get(pot_id)
            lines = lines_by_pot.get(pot_id, [])
            moves = self.moves.get(pot_id, [])
            version = pot["recipe_version"] if pot else None
            made_types = {line["meal_type"] for line in lines}
            moved_ings = {m["ingredient"] for m in moves}
            recipe_ings = {ing for (v, mt, ing) in self.recipe_g
                           if v == version and mt in made_types} if version else set()
            for ingredient in sorted(moved_ings | recipe_ings):
                own = [m for m in moves if m["ingredient"] == ingredient]
                if moves:
                    issued = sum((m["grams"] for m in own if m["kind"] == "issue"),
                                 Decimal(0))
                    returned = sum((m["grams"] for m in own if m["kind"] == "return"),
                                   Decimal(0))
                    net = issued - returned
                    cost = sum((m["grams"] * m["price"] for m in own
                                if m["kind"] == "issue"), Decimal(0))
                    cost -= sum((m["grams"] * m["price"] for m in own
                                 if m["kind"] == "return"), Decimal(0))
                else:
                    issued = returned = net = cost = None
                standard = None
                if pot is not None and version and lines:
                    standard = sum(
                        (Decimal(line["portions"])
                         * self.recipe_g.get((version, line["meal_type"], ingredient),
                                             Decimal(0))
                         for line in lines), Decimal(0))
                if pot is None:
                    status, reason = "pending", "缺锅次记录"
                elif not moves:
                    status, reason = "pending", "缺领退证据"
                elif not lines:
                    status, reason = "pending", "缺锅次产出记录"
                elif standard is None or (ingredient in moved_ings and standard == 0):
                    status, reason = "pending", "缺配方或配方权重为零"
                else:
                    status, reason = "ok", None
                variance = None
                if net is not None and standard is not None:
                    variance = net - standard
                facts.append({
                    "pot_id": pot_id,
                    "ingredient": ingredient,
                    "issued_g": issued,
                    "returned_g": returned,
                    "net_g": net,
                    "cost_yuan": cost,
                    "standard_g": standard,
                    "variance_g": variance,
                    "status": status,
                    "pending_reason": reason,
                })
        return facts

    def alloc_facts(self, products, ingredients):
        """共锅实耗按各餐种标准原料权重分担；分母含未送出份与职工份。"""
        lines_by_pot = defaultdict(list)
        for line in products:
            lines_by_pot[line["pot_id"]].append(line)
        allocs = []
        for fact in ingredients:
            if fact["status"] != "ok":
                continue
            pot_id = fact["pot_id"]
            ingredient = fact["ingredient"]
            version = self.pots[pot_id]["recipe_version"]
            lines = lines_by_pot[pot_id]
            bases = {}
            for line in lines:
                grams = self.recipe_g.get((version, line["meal_type"], ingredient),
                                          Decimal(0))
                bases[line["product_id"]] = Decimal(line["portions"]) * grams
            total_base = sum(bases.values())
            total_fen = int((fact["cost_yuan"] * 100).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP))
            fen_by_line = allocate_fen(total_fen, bases)
            for line in lines:
                base = bases[line["product_id"]]
                alloc_g = fact["net_g"] * base / total_base if total_base else Decimal(0)
                allocs.append({
                    "pot_id": pot_id,
                    "product_id": line["product_id"],
                    "meal_type": line["meal_type"],
                    "audience": line["audience"],
                    "portions": line["portions"],
                    "ingredient": ingredient,
                    "base_g": base,
                    "alloc_g": alloc_g,
                    "alloc_fen": fen_by_line[line["product_id"]],
                })
        return allocs

    def fee_facts(self, slot_facts):
        """费用资格：取消晚于厨房收费截止仍收内部服务费；与供餐事实分离。"""
        portions_by_slot = defaultdict(list)
        for portion in self.portions:
            if portion["slot_id"]:
                portions_by_slot[portion["slot_id"]].append(portion)
        facts = []
        for slot in slot_facts:
            slot_id = slot["slot_id"]
            base = {"slot_id": slot_id, "state": slot["state_at_meal"],
                    "cancel_effective": slot["cancel_effective"],
                    "pot_id": None, "cutoff_at": None}
            if slot["state_at_meal"] == "unknown":
                facts.append({**base, "eligible": None, "fee_fen": None,
                              "status": "pending", "reason": "订餐状态未知"})
                continue
            if slot["state_at_meal"] != "cancelled":
                facts.append({**base, "eligible": False, "fee_fen": 0,
                              "status": "ok", "reason": "未取消"})
                continue
            own = sorted(portions_by_slot.get(slot_id, []),
                         key=lambda p: (p["sent_at"] is None, p["sent_at"] or _MIN_DT,
                                        p["portion_id"]))
            if not own:
                facts.append({**base, "eligible": None, "fee_fen": None,
                              "status": "pending",
                              "reason": "无送出份，缺锅次收费截止证据"})
                continue
            pot_id = own[-1]["pot_id"]
            pot = self.pots.get(pot_id)
            base["pot_id"] = pot_id
            if pot is None or pot["cutoff_at"] is None:
                facts.append({**base, "eligible": None, "fee_fen": None,
                              "status": "pending", "reason": "缺锅次记录，收费截止未决"})
                continue
            base["cutoff_at"] = pot["cutoff_at"]
            eligible = slot["cancel_effective"] > pot["cutoff_at"]
            facts.append({**base, "eligible": eligible,
                          "fee_fen": FEE_PER_CANCEL_FEN if eligible else 0,
                          "status": "ok",
                          "reason": "取消晚于厨房收费截止，仍收内部服务费"
                          if eligible else "取消不晚于收费截止"})
        return facts


def _receipts_json(receipts):
    return json.dumps([{**r, "at": _iso(r["at"])} for r in receipts],
                      ensure_ascii=False)



def _insert_many(con, sql, rows):
    if rows:
        con.executemany(sql, rows)


def build_facts(con, as_of=None):
    """按可知截点构建一版事实并固化，返回 build_id。历史构建不改动。"""
    if as_of is not None:
        as_of = datetime.fromisoformat(as_of)
        if as_of.tzinfo is None:
            raise ValueError("as_of 必须带时区")
        as_of = as_of.astimezone(timezone.utc).isoformat()
        row = con.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM receive_batch WHERE received_at <= ?",
            [as_of]).fetchone()
        watermark = int(row[0])
    else:
        watermark = int(con.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM receive_batch").fetchone()[0])
    raw = {name: asof_rows(con, name, as_of) for name in TABLES}
    model = _Model(raw)
    slots = model.slot_facts()
    portions = model.portion_facts()
    products = model.product_facts()
    ingredients = model.ingredient_facts(products)
    allocs = model.alloc_facts(products, ingredients)
    fees = model.fee_facts(slots)

    # 锅次汇总：制作/送出/领取/损耗分列，成本按用途汇总，未决不并入确定数。
    pots_summary = {}
    pot_ids = set()
    for line in products:
        pot_ids.add(line["pot_id"])
    for fact in ingredients:
        pot_ids.add(fact["pot_id"])
    for portion in portions:
        pot_ids.add(portion["pot_id"])
    for pot_id in pot_ids:
        pot = model.pots.get(pot_id)
        own_products = [p for p in products if p["pot_id"] == pot_id]
        own_portions = [p for p in portions if p["pot_id"] == pot_id]
        own_ings = [i for i in ingredients if i["pot_id"] == pot_id]
        own_allocs = [a for a in allocs if a["pot_id"] == pot_id]
        pending = [i for i in own_ings if i["status"] != "ok"]
        if pending:
            cost_status = "pending"
            pending_reason = pending[0]["pending_reason"]
        elif not own_ings:
            cost_status = "pending"
            pending_reason = "无领退与配方证据"
        else:
            cost_status = "ok"
            pending_reason = None
        pots_summary[pot_id] = {
            "pot_id": pot_id,
            "pot_known": pot is not None,
            "recipe_version": pot["recipe_version"] if pot else None,
            "made_at": pot["made_at"] if pot else None,
            "cutoff_at": pot["cutoff_at"] if pot else None,
            "produced": sum(p["produced"] for p in own_products),
            "sent": len(own_portions),
            "served": sum(1 for p in own_portions if p["served"]),
            "sent_not_served": sum(1 for p in own_portions if p["sent_not_served"]),
            "discarded": sum(1 for p in own_portions if p["discarded"]),
            "made_not_sent": sum(p["made_not_sent"] for p in own_products),
            "cost_status": cost_status,
            "pending_reason": pending_reason,
            "cost_fen": sum(a["alloc_fen"] for a in own_allocs),
            "patient_fen": sum(a["alloc_fen"] for a in own_allocs
                               if a["audience"] == "patient"),
            "staff_fen": sum(a["alloc_fen"] for a in own_allocs
                             if a["audience"] == "staff"),
        }

    build_id = next_id(con, "build", "build_id")
    created_at = datetime.now(timezone.utc).isoformat()
    con.execute("INSERT INTO build VALUES (?, ?, ?, ?)",
                [build_id, as_of, created_at, watermark])
    _insert_many(con, 
        "INSERT INTO fact_slot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [[build_id, s["slot_id"], s["person"], _iso(s["meal_at"]),
          s["original_ward"], s["meal_type_at_meal"], s["ward_at_meal"],
          s["state_at_meal"], s["demanded"], _iso(s["cancel_effective"])]
         for s in slots])
    _insert_many(con, 
        "INSERT INTO fact_portion VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [[build_id, p["portion_id"], p["pot_id"], p["slot_id"], p["meal_type"],
          _iso(p["sent_at"]), p["person"], p["ward_at_sent"], p["service_ward"],
          _receipts_json(p["ward_receipts"]), _receipts_json(p["person_receipts"]),
          _receipts_json(p["discard_receipts"]), p["served"], p["serve_basis"],
          p["ward_conflict"], p["discarded"], p["sent_not_served"], p["pot_known"]]
         for p in portions])
    _insert_many(con, 
        "INSERT INTO fact_product VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [[build_id, p["product_id"], p["pot_id"], p["meal_type"], p["audience"],
          p["produced"], p["sent"], p["made_not_sent"]] for p in products])
    _insert_many(con, 
        "INSERT INTO fact_pot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [[build_id, s["pot_id"], s["pot_known"], s["recipe_version"],
          _iso(s["made_at"]), _iso(s["cutoff_at"]), s["produced"], s["sent"],
          s["served"], s["sent_not_served"], s["discarded"], s["made_not_sent"],
          s["cost_status"], s["pending_reason"], s["cost_fen"], s["patient_fen"],
          s["staff_fen"]] for s in
         [pots_summary[k] for k in sorted(pots_summary)]])
    _insert_many(con, 
        "INSERT INTO fact_pot_ingredient VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [[build_id, i["pot_id"], i["ingredient"],
          float(i["issued_g"]) if i["issued_g"] is not None else None,
          float(i["returned_g"]) if i["returned_g"] is not None else None,
          float(i["net_g"]) if i["net_g"] is not None else None,
          float(i["cost_yuan"]) if i["cost_yuan"] is not None else None,
          float(i["standard_g"]) if i["standard_g"] is not None else None,
          float(i["variance_g"]) if i["variance_g"] is not None else None,
          i["status"], i["pending_reason"]] for i in ingredients])
    _insert_many(con, 
        "INSERT INTO fact_alloc VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [[build_id, a["pot_id"], a["product_id"], a["meal_type"], a["audience"],
          a["portions"], a["ingredient"], float(a["base_g"]), float(a["alloc_g"]),
          a["alloc_fen"]] for a in allocs])
    _insert_many(con, 
        "INSERT INTO fact_fee VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [[build_id, f["slot_id"], f["state"], _iso(f["cancel_effective"]),
          f["pot_id"], _iso(f["cutoff_at"]), f["eligible"], f["fee_fen"],
          f["status"], f["reason"]] for f in fees])
    return build_id


def latest_build(con):
    """最新构建编号；无构建时报错提示先构建。"""
    row = con.execute("SELECT MAX(build_id) FROM build").fetchone()
    if row is None or row[0] is None:
        raise ValueError("尚无事实构建，请先执行 build")
    return int(row[0])


def list_builds(con):
    rows = con.execute(
        "SELECT build_id, as_of, created_at, batch_seq FROM build ORDER BY build_id"
    ).fetchall()
    return [{"build_id": r[0], "as_of": r[1], "created_at": r[2],
             "batch_seq": r[3]} for r in rows]
