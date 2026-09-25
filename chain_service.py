"""资金链路业务层：登记、前后向展开、名单标记、冻结传导、案件链路视图。

存储在 chain_store.py，页面在 static/chain.html；本层只做业务规则。
名单相似度与高风险国家集合由主应用注入，避免循环依赖。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from chain_store import ChainStore

VIEW_ROLES = {"investigator", "supervisor", "director", "auditor"}
REGISTER_ROLES = {"analyst", "investigator"}
MAX_DEPTH = 8
NODE_CAP = 200
EDGE_CAP = 500


class ChainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChainService:
    def __init__(self, db_path: str, name_similarity: Callable[[str, str], float],
                 high_risk_countries: set[str]):
        self.store = ChainStore(db_path)
        self._similarity = name_similarity
        self._high_risk = {c.upper() for c in high_risk_countries}

    # ---- 内部工具 ----
    @staticmethod
    def _require_view(actor: str, role: str) -> None:
        if not (actor or "").strip():
            raise ChainError("缺少操作人")
        if role not in VIEW_ROLES:
            raise ChainError("角色无权查看资金链路", 403)

    def _screen_holder(self, watchlist: list[sqlite3.Row], holder: str, country: str) -> tuple[bool, bool, list[str]]:
        reasons: list[str] = []
        sanctioned = False
        for entry in watchlist:
            countries = json.loads(entry["countries"])
            if countries and country not in countries:
                continue
            if self._similarity(holder, entry["name"]) >= 0.9:
                sanctioned = True
                reasons.append("sanctions_match:%s:%s" % (entry["list_name"], entry["name"]))
        high_risk = country in self._high_risk
        if high_risk:
            reasons.append("high_risk_country:" + country)
        return sanctioned, high_risk, reasons

    @staticmethod
    def _clean_account_spec(spec: Any, label: str) -> dict[str, Any]:
        if not isinstance(spec, dict):
            raise ChainError("%s账户信息必须是对象" % label)
        account_no = str(spec.get("account_no") or "").strip()
        holder_name = str(spec.get("holder_name") or "").strip()
        country = str(spec.get("country") or "").strip().upper()
        entity_id = spec.get("entity_id")
        if not account_no or not holder_name or len(country) != 2:
            raise ChainError("%s账户的账号、持有人或国家代码无效" % label)
        if entity_id is not None:
            try:
                entity_id = int(entity_id)
            except (TypeError, ValueError) as exc:
                raise ChainError("%s账户的实体编号无效" % label) from exc
        return {"account_no": account_no, "holder_name": holder_name, "country": country, "entity_id": entity_id}

    def _upsert_account(self, conn: sqlite3.Connection, watchlist: list[sqlite3.Row],
                        spec: dict[str, Any], now: str) -> sqlite3.Row:
        sanctioned, high_risk, _ = self._screen_holder(watchlist, spec["holder_name"], spec["country"])
        row = self.store.find_account(conn, spec["account_no"])
        if row:
            entity_id = spec["entity_id"] if spec["entity_id"] is not None else row["entity_id"]
            self.store.update_account(conn, row["id"], spec["holder_name"], spec["country"],
                                      entity_id, sanctioned, high_risk, now)
            return self.store.get_account(conn, row["id"])
        account_id = self.store.insert_account(conn, spec["account_no"], spec["holder_name"], spec["country"],
                                               spec["entity_id"], sanctioned, high_risk, now)
        return self.store.get_account(conn, account_id)

    def _account_blocked(self, conn: sqlite3.Connection, account: sqlite3.Row) -> bool:
        if account["frozen"]:
            return True
        if account["entity_id"] is not None and self.store.entity_is_frozen(conn, account["entity_id"]):
            return True
        return False

    def _flagged_nodes(self, conn: sqlite3.Connection, account_ids: list[int]) -> list[dict[str, Any]]:
        watchlist = self.store.active_watchlist(conn)
        nodes = []
        for row in self.store.accounts_by_ids(conn, account_ids):
            sanctioned, high_risk, reasons = self._screen_holder(watchlist, row["holder_name"], row["country"])
            frozen = bool(row["frozen"]) or (
                row["entity_id"] is not None and self.store.entity_is_frozen(conn, row["entity_id"])
            )
            if frozen:
                reasons = reasons + ["frozen_entity"]
            nodes.append({
                "id": row["id"],
                "account_no": row["account_no"],
                "holder_name": row["holder_name"],
                "country": row["country"],
                "entity_id": row["entity_id"],
                "sanctioned": sanctioned,
                "high_risk": high_risk,
                "frozen": frozen,
                "flags": reasons,
            })
        return nodes

    # ---- 登记 ----
    def register_transfer(self, actor: str, role: str, biz_ref: str, from_account: Any, to_account: Any,
                          amount: Any, currency: str, case_id: int | None = None) -> dict[str, Any]:
        actor = (actor or "").strip()
        if not actor:
            raise ChainError("缺少操作人")
        if role not in REGISTER_ROLES:
            raise ChainError("角色无权执行：登记链路转账", 403)
        biz_ref = (biz_ref or "").strip()
        try:
            amount = float(amount)
        except (TypeError, ValueError) as exc:
            raise ChainError("转账金额必须是数值") from exc
        currency = (currency or "").strip().upper()
        if not biz_ref or amount <= 0 or len(currency) != 3:
            raise ChainError("业务编号、金额或币种无效")
        source = self._clean_account_spec(from_account, "上游")
        target = self._clean_account_spec(to_account, "下游")
        if source["account_no"] == target["account_no"]:
            raise ChainError("上下游账户不能相同")
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = self.store.find_transfer_by_ref(conn, biz_ref)
            if existing:
                return {"transfer": dict(existing), "duplicate": True}
            if case_id is not None and not self.store.find_case(conn, int(case_id)):
                raise ChainError("案件不存在", 404)
            now = _utcnow()
            watchlist = self.store.active_watchlist(conn)
            from_acc = self._upsert_account(conn, watchlist, source, now)
            to_acc = self._upsert_account(conn, watchlist, target, now)
            reasons: list[str] = []
            if self._account_blocked(conn, from_acc) or self._account_blocked(conn, to_acc):
                status, reasons = "blocked", ["frozen_entity"]
            else:
                for acc in (from_acc, to_acc):
                    _, _, acc_reasons = self._screen_holder(watchlist, acc["holder_name"], acc["country"])
                    reasons.extend(acc_reasons)
                status = "review" if reasons else "recorded"
            try:
                transfer_id = self.store.insert_transfer(
                    conn, biz_ref, from_acc["id"], to_acc["id"], amount, currency,
                    status, ";".join(sorted(set(reasons))), int(case_id) if case_id is not None else None,
                    actor, now,
                )
            except sqlite3.IntegrityError:
                # 并发重复提交：同一业务编号只记一次
                return {"transfer": dict(self.store.find_transfer_by_ref(conn, biz_ref)), "duplicate": True}
            self.store.log_audit(conn, actor, "chain.transfer_registered",
                                 {"biz_ref": biz_ref, "status": status, "amount": amount, "currency": currency},
                                 int(case_id) if case_id is not None else None, now)
            return {
                "transfer": dict(self.store.get_transfer(conn, transfer_id)),
                "duplicate": False,
                "accounts": [dict(from_acc), dict(to_acc)],
            }

    # ---- 展开 ----
    def expand_transfer(self, actor: str, role: str, transfer_id: int, depth: Any = 3) -> dict[str, Any]:
        self._require_view(actor, role)
        try:
            depth = max(1, min(int(depth), MAX_DEPTH))
        except (TypeError, ValueError) as exc:
            raise ChainError("展开层数必须是数值") from exc
        with self.store.connect() as conn:
            seed = self.store.get_transfer(conn, int(transfer_id))
            if not seed:
                raise ChainError("转账不存在", 404)
            edges: dict[int, sqlite3.Row] = {seed["id"]: seed}
            visited = {seed["from_account_id"], seed["to_account_id"]}
            frontier = list(visited)
            truncated = False
            for _ in range(depth):
                nxt: list[int] = []
                for account_id in frontier:
                    rows = self.store.transfers_out(conn, account_id) + self.store.transfers_in(conn, account_id)
                    for row in rows:
                        edges.setdefault(row["id"], row)
                        for neighbor in (row["from_account_id"], row["to_account_id"]):
                            if neighbor not in visited:
                                visited.add(neighbor)
                                nxt.append(neighbor)
                        if len(visited) > NODE_CAP or len(edges) > EDGE_CAP:
                            truncated = True
                            break
                    if truncated:
                        break
                if truncated or not nxt:
                    break
                frontier = nxt
            return {
                "transfer": dict(seed),
                "nodes": self._flagged_nodes(conn, sorted(visited)),
                "edges": [dict(e) for e in sorted(edges.values(), key=lambda r: r["id"])],
                "truncated": truncated,
            }

    # ---- 案件链路 ----
    def case_chain(self, actor: str, role: str, case_id: int) -> dict[str, Any]:
        self._require_view(actor, role)
        with self.store.connect() as conn:
            case = self.store.find_case(conn, int(case_id))
            if not case:
                raise ChainError("案件不存在", 404)
            if role == "investigator" and case["assignee"] != actor:
                raise ChainError("案件仅限被指派的调查人员或授权角色访问", 403)
            edges = self.store.transfers_for_case(conn, int(case_id))
            account_ids = sorted({e["from_account_id"] for e in edges} | {e["to_account_id"] for e in edges})
            return {
                "case_id": int(case_id),
                "case_no": case["case_no"],
                "nodes": self._flagged_nodes(conn, account_ids),
                "edges": [dict(e) for e in edges],
            }

    # ---- 主应用事务内的钩子 ----
    def on_entity_frozen(self, conn: sqlite3.Connection, entity_id: int) -> None:
        self.store.set_entity_accounts_frozen(conn, entity_id, True, _utcnow())
        self.store.move_review_to_frozen(conn, entity_id)

    def on_entity_unfrozen(self, conn: sqlite3.Connection, entity_id: int) -> None:
        self.store.set_entity_accounts_frozen(conn, entity_id, False, _utcnow())
        self.store.restore_frozen_review(conn, entity_id)

    def on_entity_merged(self, conn: sqlite3.Connection, source_id: int, target_id: int) -> None:
        self.store.reassign_entity(conn, source_id, target_id)

    # ---- 演示数据 ----
    def seed_demo(self, case_id: int | None = None) -> dict[str, Any]:
        with self.store.connect() as conn:
            if self.store.count_transfers(conn):
                return {"seeded": False, "reason": "已有链路数据"}
        hops = [
            ("SEED-TR-0001",
             {"account_no": "ACCT-CLIENT-001", "holder_name": "海岳贸易有限公司", "country": "CN"},
             {"account_no": "ACCT-SHELL-01", "holder_name": "蓝桥咨询有限公司", "country": "HK"}, 88000),
            ("SEED-TR-0002",
             {"account_no": "ACCT-SHELL-01", "holder_name": "蓝桥咨询有限公司", "country": "HK"},
             {"account_no": "ACCT-SHELL-02", "holder_name": "金岸贸易公司", "country": "SG"}, 86000),
            ("SEED-TR-0003",
             {"account_no": "ACCT-SHELL-02", "holder_name": "金岸贸易公司", "country": "SG"},
             {"account_no": "ACCT-END-01", "holder_name": "沙漠之星贸易", "country": "SY"}, 83000),
        ]
        for ref, source, target, amount in hops:
            self.register_transfer("analyst-demo", "analyst", ref, source, target, amount, "USD", case_id)
        return {"seeded": True, "transfers": len(hops), "case_id": case_id}
