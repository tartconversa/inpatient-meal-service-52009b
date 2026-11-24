"""业务事实仓测试：供餐、损耗、费用资格与共锅成本口径。"""
from pathlib import Path
import tempfile
import unittest

from mealwh import facts, ingest, queries, settle, store

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CONTRACT = ingest.load_contract(DATA / "catalog.json")

T1 = "2026-10-16T21:00:00+08:00"  # 基础资料接收
T2 = "2026-10-17T09:00:00+08:00"  # 后续补录接收


def write_csv(folder, name, text):
    path = Path(folder) / name
    path.write_text(text, encoding="utf-8")
    return path


class WarehouseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "wh.duckdb"
        self.con = store.connect(self.db)
        self.addCleanup(self.con.close)

    def receive_base(self, batch="BASE", at=T1):
        files = [(name, DATA / f"{name}.csv") for name in CONTRACT]
        return ingest.receive(self.con, CONTRACT, files, batch, received_at=at)

    def receive_rows(self, batch, at, table, header, rows):
        path = write_csv(self.tmp.name, f"{batch}_{table}.csv",
                         "\n".join([header] + rows) + "\n")
        return ingest.receive(self.con, CONTRACT, [(table, path)], batch,
                              received_at=at)


class BaseBuildTests(WarehouseCase):
    """样本包口径：S1 转区后取消、X1/X2 冲突、P1 废弃、staff 入分母。"""

    def setUp(self):
        super().setUp()
        self.receive_base()
        self.build = facts.build_facts(self.con)

    def test_slot_states_at_meal(self):
        rows = self.con.execute(
            "SELECT slot_id, state_at_meal, ward_at_meal, demanded "
            "FROM fact_slot WHERE build_id = ? ORDER BY slot_id",
            [self.build]).fetchall()
        self.assertEqual(rows, [
            ("S1", "cancelled", "W2", False),   # 转区 W2 后取消,不增餐
            ("S2", "active", "W1", True),
            ("S3", "active", "W2", True),
        ])

    def test_portion_service_and_conflict(self):
        rows = {r[0]: r for r in self.con.execute(
            "SELECT portion_id, served, ward_conflict, discarded, sent_not_served "
            "FROM fact_portion WHERE build_id = ?", [self.build]).fetchall()}
        # P1：病区回执不证明个人领取；X1/X2 两病区回执冲突待核实；最终废弃
        self.assertEqual(rows["P1"][1:], (False, True, True, True))
        # P2：病区回执+本人回执只算一次服务
        self.assertEqual(rows["P2"][1:], (True, False, False, False))
        self.assertEqual(rows["P3"][1:], (True, False, False, False))

    def test_fee_eligibility_not_service(self):
        row = self.con.execute(
            "SELECT eligible, fee_fen, status FROM fact_fee "
            "WHERE build_id = ? AND slot_id = 'S1'", [self.build]).fetchone()
        self.assertEqual(row, (True, 2000, "ok"))  # 取消 17:00 晚于截止 14:00
        # 费用资格不等于供餐：S1 有费用资格但 served 仍为 False
        served = self.con.execute(
            "SELECT served FROM fact_portion WHERE build_id = ? AND portion_id = 'P1'",
            [self.build]).fetchone()[0]
        self.assertFalse(served)

    def test_pot_consumption_and_shared_denominator(self):
        rows = {r[0]: r for r in self.con.execute(
            "SELECT ingredient, issued_g, returned_g, net_g, standard_g, variance_g "
            "FROM fact_pot_ingredient WHERE build_id = ? AND pot_id = 'K1'",
            [self.build]).fetchall()}
        # 实耗=领-退；标准分母含未送出 staff 两份(4×100+1×150=550)
        self.assertEqual(rows["rice"][1:], (600.0, 50.0, 550.0, 550.0, 0.0))
        self.assertEqual(rows["vegetable"][1:], (600.0, 80.0, 520.0, 520.0, 0.0))

    def test_alloc_by_purpose(self):
        rows = self.con.execute(
            "SELECT audience, meal_type, SUM(alloc_fen) FROM fact_alloc "
            "WHERE build_id = ? GROUP BY audience, meal_type ORDER BY 1, 2",
            [self.build]).fetchall()
        self.assertEqual(rows, [
            ("patient", "M1", 600), ("patient", "M2", 390), ("staff", "M1", 600),
        ])
        total = self.con.execute(
            "SELECT cost_fen, patient_fen, staff_fen FROM fact_pot "
            "WHERE build_id = ? AND pot_id = 'K1'", [self.build]).fetchone()
        self.assertEqual(total, (1590, 990, 600))

    def test_discard_is_not_ingredient_return(self):
        # X6 成品废弃不恢复原料库存：rice 退回仍只有 I2 的 50 克
        returned = self.con.execute(
            "SELECT returned_g FROM fact_pot_ingredient "
            "WHERE build_id = ? AND pot_id = 'K1' AND ingredient = 'rice'",
            [self.build]).fetchone()[0]
        self.assertEqual(returned, 50.0)

    def test_loss_categories_separate(self):
        row = self.con.execute(
            "SELECT produced, sent, served, sent_not_served, discarded, "
            "made_not_sent FROM fact_pot WHERE build_id = ? AND pot_id = 'K1'",
            [self.build]).fetchone()
        # 制作5/送出3/领取2/送出未领1/废弃1/制作未送2,不从制作量里净扣
        self.assertEqual(row, (5, 3, 2, 1, 1, 2))

    def test_fulfillment_by_ward(self):
        rows = {r["ward"]: r for r in queries.fulfillment(self.con, self.build)}
        self.assertEqual((rows["W1"]["demanded"], rows["W1"]["sent"],
                          rows["W1"]["served"]), (1, 1, 1))
        w2 = rows["W2"]
        self.assertEqual((w2["demanded"], w2["cancelled"], w2["sent"],
                          w2["served"], w2["sent_not_served"], w2["discarded"],
                          w2["ward_conflicts"], w2["fee_fen"]),
                         (1, 1, 2, 1, 1, 1, 1, 2000))

    def test_trace_back_to_pot_and_fee(self):
        trace = queries.patient_trace(self.con, self.build, slot_id="S1")
        portion = trace["portions"][0]
        self.assertEqual(portion["pot_id"], "K1")
        self.assertEqual(portion["pot_cutoff_at"], "2026-10-16T14:00:00+08:00")
        self.assertEqual(portion["ward_at_sent"], "W1")    # 送出时仍属 W1
        self.assertEqual(portion["service_ward"], "W2")    # 最终归属 W2
        self.assertFalse(portion["served"])
        self.assertEqual(trace["fee"][0]["fee_fen"], 2000)
        self.assertEqual(portion["portion_cost_fen"], 300.0)  # M1 每份 3 元


class IngestionTests(WarehouseCase):
    """持续入仓：摘要去重、整行修订、可知截点。"""

    def test_same_digest_adds_no_facts(self):
        first = self.receive_base()
        second = self.receive_base(batch="BASE2", at=T2)
        self.assertEqual(first["rows_added"], 29)
        self.assertEqual(second["rows_added"], 0)
        self.assertTrue(all("skipped" in f for f in second["files"]))
        count = self.con.execute("SELECT COUNT(*) FROM raw_row").fetchone()[0]
        self.assertEqual(count, 29)

    def test_revision_keeps_history_and_asof(self):
        self.receive_base()
        self.receive_rows("FIX", T2, "meal_slots",
                          "slot_id,person,meal_at,original_ward",
                          ["S1,P1,2026-10-16T18:00:00+08:00,W9"])
        early = facts.build_facts(self.con, as_of=T1)
        late = facts.build_facts(self.con)
        ward = lambda b: self.con.execute(
            "SELECT original_ward FROM fact_slot WHERE build_id = ? AND slot_id = 'S1'",
            [b]).fetchone()[0]
        self.assertEqual(ward(early), "W1")  # 历史截点不用晚知值
        self.assertEqual(ward(late), "W9")   # 整行修订生效
        versions = self.con.execute(
            "SELECT COUNT(*) FROM raw_row WHERE table_name = 'meal_slots' "
            "AND record_key LIKE '%S1%'").fetchone()[0]
        self.assertEqual(versions, 2)        # 两个版本都保留

    def test_batch_order_is_explicit(self):
        self.receive_base()
        with self.assertRaisesRegex(ValueError, "批次已存在"):
            self.receive_base()


class SettlementTests(WarehouseCase):
    """结算确认后补录到达：原依据保留，差异可比较。"""

    def test_supplement_after_settlement(self):
        self.receive_base()
        build1 = facts.build_facts(self.con)
        first = settle.settle(self.con, build1, "晚餐结算")
        # 结算后到达：原料退库补录 + 转科补录
        self.receive_rows("SUP1", T2, "ingredient_moves",
                          "move_id,pot_id,ingredient,kind,grams,yuan_per_g,at",
                          ["I5,K1,rice,return,20,0.01,2026-10-16T20:30:00+08:00"])
        self.receive_rows("SUP2", T2, "order_changes",
                          "change_id,slot_id,effective,meal_type,ward,state",
                          ["C6,S2,2026-10-16T16:30:00+08:00,M1,W2,active"])
        build2 = facts.build_facts(self.con)
        second = settle.settle(self.con, build2, "晚餐结算-补录后")

        # 原结算依据不变
        kept = self.con.execute(
            "SELECT alloc_fen FROM settlement_cost WHERE settlement_id = ? "
            "AND pot_id = 'K1' AND product_id = 'PP1' AND ingredient = 'rice'",
            [first]).fetchone()[0]
        self.assertEqual(kept, 200)
        # 新结算反映退库：rice 实耗 530,PP1 分摊 530×200/530=200 分中按比例
        rice = self.con.execute(
            "SELECT net_g FROM fact_pot_ingredient WHERE build_id = ? "
            "AND pot_id = 'K1' AND ingredient = 'rice'", [build2]).fetchone()[0]
        self.assertEqual(rice, 530.0)
        # 转科补录改变服务归属
        ward = self.con.execute(
            "SELECT ward_at_meal FROM fact_slot WHERE build_id = ? AND slot_id = 'S2'",
            [build2]).fetchone()[0]
        self.assertEqual(ward, "W2")
        # 两次结算可比较,且第一次的费用行未消失
        diff = settle.compare_settlements(self.con, first, second)
        self.assertTrue(diff["cost_changes"])
        fees1 = settle.get_settlement(self.con, first)["fees"]
        self.assertEqual(len(fees1), 3)
        # 原构建事实仍可查
        old_ward = self.con.execute(
            "SELECT ward_at_meal FROM fact_slot WHERE build_id = ? AND slot_id = 'S2'",
            [build1]).fetchone()[0]
        self.assertEqual(old_ward, "W1")


class EvidenceGapTests(WarehouseCase):
    """缺锅次或缺领退证据：供餐可确认，成本未决。"""

    def test_missing_pot_record(self):
        self.receive_rows("B1", T1, "meal_slots",
                          "slot_id,person,meal_at,original_ward",
                          ["S9,P9,2026-10-16T18:00:00+08:00,W1"])
        self.receive_rows("B2", T1, "order_changes",
                          "change_id,slot_id,effective,meal_type,ward,state",
                          ["C9,S9,2026-10-16T08:00:00+08:00,M1,W1,active"])
        self.receive_rows("B3", T1, "portions",
                          "portion_id,pot_id,slot_id,meal_type,sent_at",
                          ["P9,K9,S9,M1,2026-10-16T15:00:00+08:00"])
        self.receive_rows("B4", T1, "receipts",
                          "receipt_id,portion_id,kind,party,at",
                          ["X9,P9,person_receipt,P9,2026-10-16T18:00:00+08:00"])
        build = facts.build_facts(self.con)
        served, known = self.con.execute(
            "SELECT served, pot_known FROM fact_portion "
            "WHERE build_id = ? AND portion_id = 'P9'", [build]).fetchone()
        self.assertTrue(served)        # 供餐事实仍可确认
        self.assertFalse(known)        # 锅次证据缺失
        pot = queries.pot_cost(self.con, build, pot_id="K9")[0]
        self.assertEqual(pot["cost_status"], "pending")
        self.assertEqual(pot["pending_reason"], "无领退与配方证据")

    def test_pot_without_moves_is_pending(self):
        self.receive_rows("B1", T1, "pots",
                          "pot_id,recipe_version,made_at,cutoff_at",
                          ["K8,V1,2026-10-16T15:00:00+08:00,2026-10-16T14:00:00+08:00"])
        self.receive_rows("B2", T1, "pot_product",
                          "product_id,pot_id,meal_type,audience,portions",
                          ["PP8,K8,M1,patient,1"])
        self.receive_rows("B3", T1, "recipes",
                          "recipe_id,version,meal_type,ingredient,grams",
                          ["R8,V1,M1,rice,100"])
        build = facts.build_facts(self.con)
        row = self.con.execute(
            "SELECT status, pending_reason, net_g FROM fact_pot_ingredient "
            "WHERE build_id = ? AND pot_id = 'K8'", [build]).fetchone()
        self.assertEqual(row, ("pending", "缺领退证据", None))

    def test_cancelled_slot_without_portion_fee_pending(self):
        self.receive_rows("B1", T1, "meal_slots",
                          "slot_id,person,meal_at,original_ward",
                          ["S7,P7,2026-10-16T18:00:00+08:00,W1"])
        self.receive_rows("B2", T1, "order_changes",
                          "change_id,slot_id,effective,meal_type,ward,state",
                          ["C7,S7,2026-10-16T17:00:00+08:00,M1,W1,cancelled"])
        build = facts.build_facts(self.con)
        row = self.con.execute(
            "SELECT eligible, fee_fen, status FROM fact_fee "
            "WHERE build_id = ? AND slot_id = 'S7'", [build]).fetchone()
        self.assertEqual(row, (None, None, "pending"))  # 无锅次证据,截止未决


class RoundingTests(WarehouseCase):
    """结算舍入到分：尾差归分摊基数最大项，并列按业务键升序。"""

    def test_residual_to_largest_base_then_key_order(self):
        self.receive_rows("B1", T1, "pots",
                          "pot_id,recipe_version,made_at,cutoff_at",
                          ["K2,V2,2026-10-16T15:00:00+08:00,2026-10-16T14:00:00+08:00"])
        self.receive_rows("B2", T1, "recipes",
                          "recipe_id,version,meal_type,ingredient,grams",
                          ["R9,V2,M9,rice,100"])
        self.receive_rows("B3", T1, "pot_product",
                          "product_id,pot_id,meal_type,audience,portions",
                          ["PP9,K2,M9,patient,1", "PP10,K2,M9,staff,1"])
        self.receive_rows("B4", T1, "ingredient_moves",
                          "move_id,pot_id,ingredient,kind,grams,yuan_per_g,at",
                          ["I9,K2,rice,issue,1,0.01,2026-10-16T13:00:00+08:00"])
        build = facts.build_facts(self.con)
        rows = dict(self.con.execute(
            "SELECT product_id, alloc_fen FROM fact_alloc "
            "WHERE build_id = ? AND pot_id = 'K2'", [build]).fetchall())
        # 总额 1 分,两项各 0.5 分进位为 1,尾差 -1 归并列中业务键升序首项 PP10
        self.assertEqual(rows, {"PP9": 1, "PP10": 0})
        self.assertEqual(sum(rows.values()), 1)

    def test_allocate_fen_directly(self):
        fen = facts.allocate_fen(100, {"a": __import__("decimal").Decimal(1),
                                       "b": __import__("decimal").Decimal(3)})
        self.assertEqual(fen, {"a": 25, "b": 75})
        self.assertEqual(sum(fen.values()), 100)


class CliTests(unittest.TestCase):
    """产品入口：命令行走通 入仓→构建→查询→结算→比较。"""

    def test_cli_flow(self):
        import json
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "wh.duckdb")

            def run(*args):
                out = subprocess.run(
                    [sys.executable, "-m", "mealwh", *args, "--db", db],
                    capture_output=True, text=True, cwd=ROOT)
                self.assertEqual(out.returncode, 0, out.stderr)
                return json.loads(out.stdout)

            run("ingest", "--batch", "BASE", "--received-at", T1,
                "--data-dir", str(DATA))
            build = run("build")
            self.assertEqual(build["build_id"], 1)
            rows = run("fulfillment", "--ward", "W2")
            self.assertEqual(rows[0]["sent_not_served"], 1)
            pots = run("pot-cost", "--pot", "K1")
            self.assertEqual(pots[0]["cost_fen"], 1590)
            trace = run("trace", "--slot", "S1")
            self.assertTrue(trace["fee"][0]["eligible"])
            first = run("settle", "--label", "晚餐结算")
            self.assertEqual(first["settlement_id"], 1)
            detail = run("settlement", "--id", "1")
            self.assertEqual(len(detail["fees"]), 3)
            self.assertEqual(len(detail["costs"]), 6)


if __name__ == "__main__":
    unittest.main()
