import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import HIGH_RISK_COUNTRIES, FinancialCrimeService, name_similarity  # noqa: E402
from chain_service import ChainError, ChainService  # noqa: E402


def account(no, holder, country, entity_id=None):
    spec = {"account_no": no, "holder_name": holder, "country": country}
    if entity_id is not None:
        spec["entity_id"] = entity_id
    return spec


class FundChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = Path(self.tmp.name) / "test.db"
        self.service = FinancialCrimeService(db)
        self.chain = ChainService(db, name_similarity, HIGH_RISK_COUNTRIES)
        self.service.chain_freeze_hook = self.chain.on_entity_frozen
        self.service.chain_unfreeze_hook = self.chain.on_entity_unfrozen
        self.service.chain_merge_hook = self.chain.on_entity_merged

    def tearDown(self):
        self.tmp.cleanup()

    def _register(self, ref, frm, to, amount=1000, case_id=None):
        return self.chain.register_transfer("analyst1", "analyst", ref, frm, to, amount, "USD", case_id)

    def test_register_keeps_counterparties_and_is_idempotent(self):
        first = self._register("BIZ-1", account("A-1", "客户甲", "CN"), account("A-2", "壳公司乙", "HK"), 5000)
        self.assertFalse(first["duplicate"])
        self.assertEqual("recorded", first["transfer"]["status"])
        again = self._register("BIZ-1", account("A-1", "客户甲", "CN"), account("A-2", "壳公司乙", "HK"), 9999)
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["transfer"]["id"], again["transfer"]["id"])
        self.assertEqual(5000, again["transfer"]["amount"])
        with self.chain.store.connect() as conn:
            self.assertEqual(1, self.chain.store.count_transfers(conn))
        with self.assertRaises(ChainError) as ctx:
            self.chain.register_transfer("viewer1", "viewer", "BIZ-2", account("A-1", "客户甲", "CN"), account("A-3", "丙", "US"), 10, "USD")
        self.assertEqual(403, ctx.exception.status)

    def test_expand_flags_sanctions_and_high_risk_nodes(self):
        self.service.add_or_update_watchlist("sup1", "supervisor", "OFAC", "壳公司乙", [])
        hops = [
            ("BIZ-1", account("A-1", "客户甲", "CN"), account("A-2", "壳公司乙", "HK")),
            ("BIZ-2", account("A-2", "壳公司乙", "HK"), account("A-3", "中转丙", "SG")),
            ("BIZ-3", account("A-3", "中转丙", "SG"), account("A-4", "终端丁", "IR")),
            ("BIZ-4", account("A-4", "终端丁", "IR"), account("A-5", "末端戊", "AE")),
            ("BIZ-5", account("A-5", "末端戊", "AE"), account("A-6", "干净己", "US")),
        ]
        ids = [self._register(ref, frm, to)["transfer"]["id"] for ref, frm, to in hops]
        # 待复核：命中名单或高风险国家的转账
        with self.chain.store.connect() as conn:
            statuses = {r["biz_ref"]: r["status"] for r in conn.execute("SELECT biz_ref,status FROM chain_transfers")}
        self.assertEqual("review", statuses["BIZ-1"])
        self.assertEqual("review", statuses["BIZ-2"])
        self.assertEqual("review", statuses["BIZ-3"])
        self.assertEqual("recorded", statuses["BIZ-5"])
        # 从中间一笔展开一层：覆盖前后各一跳，不含末端戊
        view = self.chain.expand_transfer("inv1", "investigator", ids[1], depth=1)
        names = {n["holder_name"]: n for n in view["nodes"]}
        self.assertEqual({"客户甲", "壳公司乙", "中转丙", "终端丁"}, set(names))
        self.assertTrue(names["壳公司乙"]["sanctioned"])
        self.assertTrue(names["终端丁"]["high_risk"])
        self.assertFalse(names["客户甲"]["sanctioned"] or names["客户甲"]["high_risk"])
        self.assertEqual(3, len(view["edges"]))
        # 加深一层后覆盖全链路
        deep = self.chain.expand_transfer("sup1", "supervisor", ids[1], depth=2)
        self.assertEqual(5, len(deep["nodes"]))
        self.assertEqual(4, len(deep["edges"]))
        with self.assertRaises(ChainError) as ctx:
            self.chain.expand_transfer("clerk", "analyst", ids[1])
        self.assertEqual(403, ctx.exception.status)

    def test_freeze_blocks_followup_and_moves_review_to_frozen_review(self):
        shell = self.service.create_entity("sup1", "supervisor", "organization", "壳公司乙", [])
        self._register("BIZ-1", account("A-1", "客户甲", "CN"), account("A-2", "壳公司乙", "HK", shell["id"]))
        self._register("BIZ-2", account("A-2", "壳公司乙", "HK", shell["id"]), account("A-4", "终端丁", "IR"))
        self.service.freeze_entity("sup1", "supervisor", shell["id"], "链路冻结", shell["version"])
        with self.chain.store.connect() as conn:
            rows = {r["biz_ref"]: r["status"] for r in conn.execute("SELECT biz_ref,status FROM chain_transfers")}
        self.assertEqual("recorded", rows["BIZ-1"])
        self.assertEqual("frozen_review", rows["BIZ-2"])
        # 后续交易被阻断
        blocked = self._register("BIZ-3", account("A-2", "壳公司乙", "HK", shell["id"]), account("A-6", "新对手", "GB"))
        self.assertEqual("blocked", blocked["transfer"]["status"])
        inbound = self._register("BIZ-4", account("A-7", "另一对手", "US"), account("A-2", "壳公司乙", "HK", shell["id"]))
        self.assertEqual("blocked", inbound["transfer"]["status"])
        # 解冻后冻结复核回到待复核
        current = self.service.state("sup1", "supervisor")
        version = [e for e in current["entities"] if e["id"] == shell["id"]][0]["version"]
        self.service.unfreeze_entity("dir1", "director", shell["id"], "复核排除", version)
        with self.chain.store.connect() as conn:
            restored = conn.execute("SELECT status FROM chain_transfers WHERE biz_ref='BIZ-2'").fetchone()
        self.assertEqual("review", restored["status"])

    def test_case_chain_view_and_access_control(self):
        entity = self.service.create_entity("analyst1", "analyst", "organization", "远海贸易", [])
        customer = self.service.create_customer("analyst1", "analyst", entity["id"], "C-100", "CN", risk_score=0.2)
        self.service.add_or_update_watchlist("sup1", "supervisor", "OFAC", "远海贸易", ["CN"])
        alert = self.service.ingest_transaction("analyst1", "analyst", "T-100", customer["id"], 150000, "USD", "远海贸易", "CN")["alert"]
        case = self.service.triage_alert("sup1", "supervisor", alert["id"], "escalate", "inv1", "CASE-100")["case"]
        self._register("BIZ-1", account("A-1", "客户甲", "CN"), account("A-2", "壳公司乙", "HK"), case_id=case["id"])
        self._register("BIZ-2", account("A-2", "壳公司乙", "HK"), account("A-4", "终端丁", "IR"), case_id=case["id"])
        view = self.chain.case_chain("inv1", "investigator", case["id"])
        self.assertEqual(2, len(view["edges"]))
        self.assertEqual(3, len(view["nodes"]))
        with self.assertRaises(ChainError) as ctx:
            self.chain.case_chain("inv2", "investigator", case["id"])
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(ChainError) as ctx2:
            self.chain.case_chain("analyst1", "analyst", case["id"])
        self.assertEqual(403, ctx2.exception.status)
        with self.assertRaises(ChainError) as ctx3:
            self._register("BIZ-9", account("A-1", "客户甲", "CN"), account("A-9", "无关方", "US"), case_id=9999)
        self.assertEqual(404, ctx3.exception.status)

    def test_entity_merge_reassigns_chain_accounts(self):
        source = self.service.create_entity("sup1", "supervisor", "organization", "壳公司乙", [])
        target = self.service.create_entity("sup1", "supervisor", "organization", "乙集团", [])
        self._register("BIZ-1", account("A-1", "客户甲", "CN"), account("A-2", "壳公司乙", "HK", source["id"]))
        self.service.merge_entities("sup1", "supervisor", source["id"], target["id"], source["version"], target["version"])
        with self.chain.store.connect() as conn:
            row = conn.execute("SELECT entity_id FROM chain_accounts WHERE account_no='A-2'").fetchone()
        self.assertEqual(target["id"], row["entity_id"])


if __name__ == "__main__":
    unittest.main()
