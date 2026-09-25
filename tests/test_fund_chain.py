import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, FinancialCrimeService  # noqa: E402


class FundChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = FinancialCrimeService(Path(self.tmp.name) / "chain.db")
        # 客户实体（链头），后续两个空壳 + 受限地区收款方
        self.entity = self.svc.create_entity("analyst1", "analyst", "organization", "远海贸易", ["远海"])
        self.shell = self.svc.create_entity("sup1", "supervisor", "organization", "空壳中转C有限公司", [])
        self.customer = self.svc.create_customer("analyst1", "analyst", self.entity["id"], "C-001", "CN", risk_score=0.2)
        self.svc.add_or_update_watchlist("sup1", "supervisor", "OFAC", "空壳中转C有限公司", ["HK"])

    def tearDown(self):
        self.tmp.cleanup()

    def _register_chain(self):
        t1 = self.svc.register_transfer("analyst1", "analyst", "BIZ-1", "ACC-0", "ACC-1", 100000, "USD",
                                        "远海贸易", "空壳A公司", "CN", "HK")
        t2 = self.svc.register_transfer("analyst1", "analyst", "BIZ-2", "ACC-1", "ACC-2", 98000, "USD",
                                        "空壳A公司", "空壳中转C有限公司", "HK", "HK")
        t3 = self.svc.register_transfer("analyst1", "analyst", "BIZ-3", "ACC-2", "ACC-3", 96000, "USD",
                                        "空壳中转C有限公司", "平壤金达莱商社", "HK", "KP")
        return t1, t2, t3

    def test_register_keeps_upstream_downstream_and_idempotent(self):
        t1, _, _ = self._register_chain()
        self.assertFalse(t1["duplicate"])
        self.assertEqual("ACC-0", t1["from"]["account_no"])
        self.assertEqual("ACC-1", t1["to"]["account_no"])
        self.assertEqual(100000, t1["transfer"]["amount"])
        # 同一业务编号重复提交：只记一次
        again = self.svc.register_transfer("analyst1", "analyst", "BIZ-1", "ACC-0", "ACC-1", 999, "USD",
                                           "远海贸易", "空壳A公司", "CN", "HK")
        self.assertTrue(again["duplicate"])
        self.assertEqual(100000, again["transfer"]["amount"])
        state = self.svc.state("sup1", "supervisor")
        self.assertEqual(3, len(state["chain_transfers"]))

    def test_expand_marks_sanctions_and_high_risk_nodes(self):
        t1, t2, t3 = self._register_chain()
        # 第二笔命中制裁名单（空壳中转C），第三笔落入高风险国家 KP -> 均待复核
        self.assertEqual("registered", t1["transfer"]["status"])
        self.assertEqual("review", t2["transfer"]["status"])
        self.assertEqual("review", t3["transfer"]["status"])
        # 从第一笔向前后展开，可达全部 4 个节点、3 条边
        graph = self.svc.expand_chain("inv1", "investigator", transfer_id=t1["transfer"]["id"])
        self.assertEqual(4, len(graph["nodes"]))
        self.assertEqual(3, len(graph["edges"]))
        flagged = {n["account_no"]: {f["code"] for f in n["flags"]} for n in graph["nodes"] if n["flags"]}
        self.assertIn("sanctions", flagged["ACC-2"])
        self.assertIn("high_risk_country", flagged["ACC-3"])
        # 从链头账户节点展开时，受限地区收款方在 3 跳之外
        from_account = self.svc.expand_chain("inv1", "investigator", account_id=t1["transfer"]["from_account_id"])
        tail_from_account = next(n for n in from_account["nodes"] if n["account_no"] == "ACC-3")
        self.assertEqual(3, tail_from_account["distance"])
        up = self.svc.expand_chain("inv1", "investigator", transfer_id=t3["transfer"]["id"],
                                   direction="upstream")
        self.assertEqual(4, len(up["nodes"]))
        # 下游 1 跳：锚点两端 ACC-0/ACC-1，再加上 ACC-2，共 3 个节点
        down = self.svc.expand_chain("inv1", "investigator", transfer_id=t1["transfer"]["id"],
                                     direction="downstream", max_depth=1)
        self.assertEqual(3, len(down["nodes"]))

    def test_freeze_blocks_following_transfers_and_queues_review(self):
        t1, t2, t3 = self._register_chain()
        # 主管冻结链路上的空壳实体（ACC-2 已按名称自动挂靠到该实体）
        frozen = self.svc.freeze_entity("sup1", "supervisor", self.shell["id"], "链路命中制裁",
                                        self.shell["version"])
        self.assertEqual(1, frozen["frozen"])
        # 待复核的 BIZ-2/BIZ-3 转入冻结复核队列，状态 frozen_review
        reviews = self.svc.list_freeze_reviews("sup2", "supervisor")["reviews"]
        biz_refs = {r["biz_ref"] for r in reviews}
        self.assertEqual({"BIZ-2", "BIZ-3"}, biz_refs)
        # 后续登记、经过被冻账户的交易被阻断
        later = self.svc.register_transfer("analyst1", "analyst", "BIZ-4", "ACC-1", "ACC-2", 1000, "USD",
                                           "空壳A公司", "空壳中转C有限公司", "HK", "HK")
        self.assertEqual("blocked", later["transfer"]["status"])
        # 复核：其他主管放行 BIZ-2
        review_biz2 = next(r for r in reviews if r["biz_ref"] == "BIZ-2")
        decision = self.svc.review_freeze_transfer("sup2", "supervisor", review_biz2["id"],
                                                    "release", "补充材料证明交易合规")
        self.assertEqual("registered", decision["transfer_status"])
        # 冻结操作人不能复核自己冻结的记录
        with self.assertRaises(DomainError) as ctx:
            self.svc.review_freeze_transfer("sup1", "supervisor",
                                             next(r for r in reviews if r["biz_ref"] == "BIZ-3")["id"],
                                             "uphold", "维持阻断")
        self.assertEqual(409, ctx.exception.status)
        # 无理由不能复核
        with self.assertRaises(DomainError):
            self.svc.review_freeze_transfer("sup2", "supervisor",
                                             next(r for r in reviews if r["biz_ref"] == "BIZ-3")["id"],
                                             "uphold", "")
        upheld = self.svc.review_freeze_transfer("sup2", "supervisor",
                                                 next(r for r in reviews if r["biz_ref"] == "BIZ-3")["id"],
                                                 "uphold", "资金最终流向受限地区")
        self.assertEqual("blocked", upheld["transfer_status"])
        # 已复核记录不能重复裁决
        with self.assertRaises(DomainError):
            self.svc.review_freeze_transfer("sup2", "supervisor", review_biz2["id"], "uphold", "再次裁决")

    def test_case_page_shows_full_chain(self):
        t1, t2, t3 = self._register_chain()
        # 先由客户交易产生线索与案件，再把链路首笔转账挂到案件上
        self.svc.add_or_update_watchlist("sup1", "supervisor", "OFAC", "远海贸易", ["CN"])
        alert = self.svc.ingest_transaction("analyst1", "analyst", "T-1", self.customer["id"],
                                            120000, "USD", "远海贸易", "CN")["alert"]
        case = self.svc.triage_alert("inv1", "investigator", alert["id"], "escalate", "inv1", "CASE-CHAIN")["case"]
        self.svc.link_transfer_to_case("inv1", "investigator", case["id"], t1["transfer"]["id"], "补资金链路")
        detail = self.svc.get_case("inv1", "investigator", case["id"])
        chain = detail["chain"]
        # 案件页从关联的一笔展开，能拼出经过空壳到受限地区的全部节点
        self.assertEqual(4, len(chain["nodes"]))
        self.assertEqual(3, len(chain["edges"]))
        codes = {f["code"] for n in chain["nodes"] for f in n["flags"]}
        self.assertIn("sanctions", codes)
        self.assertIn("high_risk_country", codes)
        # 非被指派调查员看不到案件链路
        with self.assertRaises(DomainError) as ctx:
            self.svc.get_case("inv2", "investigator", case["id"])
        self.assertEqual(403, ctx.exception.status)

    def test_merge_moves_chain_accounts(self):
        self._register_chain()
        # 空壳中转C 的账户 ACC-2 已挂靠 shell 实体；合并到客户实体后，账户节点跟随迁移
        self.svc.merge_entities("sup1", "supervisor", self.shell["id"], self.entity["id"],
                                self.shell["version"], self.entity["version"])
        with self.svc.connect() as conn:
            account = self.svc.repo.find_account_by_no(conn, "ACC-2")
            self.assertEqual(self.entity["id"], account["entity_id"])
        # 账户挂靠实体变化后，新登记的同名账户解析到新实体；目标实体未冻结，制裁命中仍进复核
        later = self.svc.register_transfer("analyst1", "analyst", "BIZ-MERGED", "ACC-1", "ACC-2", 10, "USD",
                                           "空壳A公司", "空壳中转C有限公司", "HK", "HK")
        self.assertEqual("review", later["transfer"]["status"])

    def test_register_with_case_id_directly_links(self):
        t0 = self.svc.ingest_transaction("analyst1", "analyst", "T-9", self.customer["id"],
                                         1000, "USD", "普通客户", "US")
        # 低风险交易不产生线索；手工建案件走线索路径
        self.svc.add_or_update_watchlist("sup1", "supervisor", "OFAC", "远海贸易", ["CN"])
        alert = self.svc.ingest_transaction("analyst1", "analyst", "T-10", self.customer["id"],
                                            120000, "USD", "远海贸易", "CN")["alert"]
        case = self.svc.triage_alert("inv1", "investigator", alert["id"], "escalate", "inv1", "CASE-2")["case"]
        linked = self.svc.register_transfer("inv1", "investigator", "BIZ-9", "ACC-0", "ACC-9", 500, "USD",
                                            "远海贸易", "某公司", "CN", "SG", case_id=case["id"])
        self.assertFalse(linked["duplicate"])
        self.assertEqual(case["id"], linked["transfer"]["case_id"])
        chain = self.svc.case_chain("inv1", "investigator", case["id"])
        self.assertEqual(2, len(chain["nodes"]))


if __name__ == "__main__":
    unittest.main()
