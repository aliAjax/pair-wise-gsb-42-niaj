"""业务处理层：筛查、案件、冻结与资金链路调查的领域规则。

链路领域模型：
- ``chain_accounts`` 为资金节点（上下游账户），可挂靠到 ``entities`` 以复用冻结机制；
- ``chain_transfers`` 为有向转账边，``biz_ref``（同一业务编号）唯一，重复提交只记一次；
- 从任意一笔/任意账户做前后向 BFS 展开，节点按制裁名单命中、高风险国家、冻结实时标记；
- 主管冻结链路上的实体后，后续登记的相关转账被阻断，待复核转账转入冻结复核队列。
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import deque
from typing import Any

from domain import (
    DEFAULT_DB,
    HIGH_RISK_COUNTRIES,
    SENSITIVE_ROLES,
    DomainError,
    clean_actor,
    name_similarity,
    normalize_name,
    require_role,
    sanctions_reason,
    utcnow,
)
from storage import Repository


class FinancialCrimeService:
    MAX_CHAIN_DEPTH = 6

    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self.repo = Repository(self.db_path)
        self.repo.init_schema()

    def connect(self) -> sqlite3.Connection:
        return self.repo.connect()

    # ---- 内部辅助 ----
    def _resolve_entity(self, conn: sqlite3.Connection, entity_id: int) -> sqlite3.Row:
        seen = set()
        current = entity_id
        while True:
            if current in seen:
                raise DomainError("实体合并关系存在循环", 409)
            seen.add(current)
            row = self.repo.find_entity(conn, current)
            if not row:
                raise DomainError("实体不存在", 404)
            if row["merged_into"] is None:
                return row
            current = row["merged_into"]

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, details: dict[str, Any],
               entity_id: int | None = None, case_id: int | None = None) -> None:
        self.repo.audit(conn, actor, action, details, entity_id, case_id)

    def _screen_rows(self, conn: sqlite3.Connection, name: str, country: str):
        best_score, best = 0.0, None
        for row in self.repo.active_watchlist(conn):
            countries = json.loads(row["countries"])
            if countries and country.upper() not in countries:
                continue
            score = name_similarity(name, row["name"])
            if score > best_score:
                best_score, best = score, row
        return best_score, dict(best) if best else None

    # ---- 实体 ----
    def create_entity(self, actor: str, role: str, entity_type: str, canonical_name: str,
                      aliases: list[str] | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"analyst", "investigator", "supervisor"}, "创建实体")
        entity_type = entity_type.strip().lower()
        canonical_name = canonical_name.strip()
        if entity_type not in {"person", "organization"} or not canonical_name:
            raise DomainError("实体类型或名称无效")
        normalized = normalize_name(canonical_name)
        clean_aliases = sorted({a.strip() for a in (aliases or []) if a and a.strip()}, key=str.casefold)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in self.repo.active_entities(conn):
                known_names = [row["canonical_name"]] + json.loads(row["aliases"])
                if any(normalize_name(item) == normalized for item in known_names):
                    raise DomainError("实体名称或别名已存在，应使用已有实体或执行合并", 409)
            try:
                entity_id = self.repo.insert_entity(
                    conn, entity_type, canonical_name, normalized,
                    json.dumps(clean_aliases, ensure_ascii=False), actor,
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("实体已存在", 409) from exc
            # 先登记的同名链路账户自动挂靠，保证冻结能覆盖链路节点
            linked_accounts = self.repo.attach_unlinked_accounts(conn, entity_id, normalized)
            self._audit(conn, actor, "entity.created",
                        {"name": canonical_name, "linked_accounts": linked_accounts}, entity_id)
            return dict(self.repo.find_entity(conn, entity_id))

    def add_alias(self, actor: str, role: str, entity_id: int, alias: str,
                  expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"analyst", "investigator", "supervisor"}, "维护实体别名")
        alias = alias.strip()
        if not alias:
            raise DomainError("别名不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            entity = self._resolve_entity(conn, entity_id)
            if entity["version"] != int(expected_version):
                raise DomainError("实体已变化，请刷新后重试", 409)
            aliases = json.loads(entity["aliases"])
            normalized = normalize_name(alias)
            for row in self.repo.active_entities(conn):
                if row["id"] == entity["id"]:
                    continue
                names = [row["canonical_name"]] + json.loads(row["aliases"])
                if any(normalize_name(name) == normalized for name in names):
                    raise DomainError("别名已属于其他实体，应先执行实体合并", 409)
            if alias not in aliases:
                aliases.append(alias)
                aliases.sort(key=str.casefold)
            self.repo.update_entity_aliases(
                conn, entity["id"], json.dumps(aliases, ensure_ascii=False), expected_version
            )
            self._audit(conn, actor, "entity.alias_added", {"alias": alias}, entity["id"])
            return dict(self.repo.find_entity(conn, entity["id"]))

    def create_customer(self, actor: str, role: str, entity_id: int, customer_no: str,
                        country: str, occupation: str = "", allowlisted: bool = False,
                        risk_score: float = 0.0) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"analyst", "supervisor"}, "建立客户档案")
        try:
            risk_score = float(risk_score)
        except (TypeError, ValueError) as exc:
            raise DomainError("风险评分必须是数值") from exc
        if not customer_no.strip() or len(country.strip()) != 2 or not 0 <= risk_score <= 1:
            raise DomainError("客户编号、国家代码或风险评分无效")
        with self.connect() as conn:
            entity = self._resolve_entity(conn, entity_id)
            try:
                customer_id = self.repo.insert_customer(
                    conn, entity["id"], customer_no.strip(), country.strip().upper(),
                    occupation.strip(), int(bool(allowlisted)), risk_score,
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("客户编号已存在", 409) from exc
            self._audit(conn, actor, "customer.created", {"customer_no": customer_no.strip()}, entity["id"])
            row = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
            return dict(row)

    def add_or_update_watchlist(self, actor: str, role: str, list_name: str, name: str,
                                countries: list[str] | None = None,
                                active: bool = True, expected_version: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "维护制裁名单")
        list_name, name = list_name.strip(), name.strip()
        normalized = normalize_name(name)
        countries_json = json.dumps(
            sorted({c.strip().upper() for c in (countries or []) if c.strip()}), ensure_ascii=False
        )
        if not list_name or not normalized:
            raise DomainError("名单名称和实体名称不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self.repo.find_watchlist_entry(conn, list_name, normalized)
            if row:
                if expected_version is not None and row["version"] != int(expected_version):
                    raise DomainError("名单条目已变化，请刷新后重试", 409)
                self.repo.update_watchlist(conn, row["id"], name, countries_json, int(bool(active)), actor)
                self._audit(conn, actor, "watchlist.updated",
                            {"list_name": list_name, "name": name, "active": active})
                return dict(self.repo.find_watchlist_entry(conn, list_name, normalized))
            entry_id = self.repo.insert_watchlist(
                conn, list_name, name, normalized, countries_json, int(bool(active)), actor
            )
            self._audit(conn, actor, "watchlist.added", {"list_name": list_name, "name": name})
            return dict(conn.execute("SELECT * FROM watchlist WHERE id=?", (entry_id,)).fetchone())

    # ---- 交易筛查（既有客户交易） ----
    def ingest_transaction(self, actor: str, role: str, txn_ref: str, customer_id: int,
                           amount: float, currency: str, counterparty_name: str,
                           counterparty_country: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"analyst", "investigator"}, "录入交易")
        try:
            amount = float(amount)
        except (TypeError, ValueError) as exc:
            raise DomainError("交易金额必须是数值") from exc
        if not txn_ref.strip() or amount <= 0 or len(currency.strip()) != 3 or len(counterparty_country.strip()) != 2:
            raise DomainError("交易编号、金额、币种或国家代码无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            customer = self.repo.find_customer(conn, customer_id)
            if not customer:
                raise DomainError("客户不存在", 404)
            entity = self._resolve_entity(conn, customer["entity_id"])
            country = counterparty_country.strip().upper()
            match_score, match = self._screen_rows(conn, counterparty_name.strip(), country)
            match_reason = sanctions_reason(match) if match and match_score >= 0.9 else None
            base_risk = customer["risk_score"]
            reason = "normal"
            if match_reason:
                risk, reason = max(match_score, base_risk), match_reason
            elif country in HIGH_RISK_COUNTRIES:
                risk, reason = max(0.75, base_risk), "high_risk_country:" + country
            elif amount >= 100000:
                risk, reason = max(0.6, base_risk), "large_value_transaction"
            elif amount >= 50000:
                risk, reason = max(0.35, base_risk), "enhanced_review"
            else:
                risk = base_risk
            if entity["frozen"]:
                status = "blocked"
                reason = "frozen_entity"
                risk = 1.0
            elif match_reason:
                status = "escalated"
            elif risk >= 0.5:
                status = "review"
            else:
                status = "allowed"
            if customer["allowlisted"] and risk < 0.8 and amount <= 200000:
                status, risk, reason = "allowed", min(risk, 0.1), "allowlist_false_positive_reduction"
            try:
                txn_id = self.repo.insert_transaction(
                    conn,
                    (txn_ref.strip(), customer["id"], entity["id"], amount, currency.strip().upper(),
                     counterparty_name.strip(), country, status, risk, reason, actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("交易编号已存在", 409) from exc
            alert = None
            if status in {"review", "escalated", "blocked"}:
                fingerprint = "%s|%s|%s" % (entity["id"], reason.split(":")[0], normalize_name(counterparty_name))
                existing = self.repo.find_alert_by_fingerprint(conn, fingerprint)
                if existing:
                    self.repo.bump_alert(conn, existing["id"], txn_id, risk)
                    alert_id = existing["id"]
                else:
                    alert_id = self.repo.insert_alert(conn, fingerprint, entity["id"], txn_id, reason, risk)
                alert = dict(conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone())
            if entity["frozen"]:
                conn.execute(
                    "UPDATE transactions SET status='blocked' WHERE entity_id=? AND status='review'",
                    (entity["id"],),
                )
            self._audit(conn, actor, "transaction.ingested",
                        {"txn_ref": txn_ref, "status": status, "reason": reason}, entity["id"])
            return {
                "transaction": dict(self.repo.find_transaction(conn, txn_id)),
                "alert": alert,
                "resolved_entity_id": entity["id"],
            }

    # ---- 线索处置与案件 ----
    def triage_alert(self, actor: str, role: str, alert_id: int, decision: str,
                    assignee: str | None = None, case_no: str | None = None,
                    reason: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"investigator", "supervisor"}, "处置可疑线索")
        if decision not in {"dismiss", "escalate"}:
            raise DomainError("线索处置决定无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            alert = self.repo.find_alert(conn, alert_id)
            if not alert:
                raise DomainError("线索不存在", 404)
            if alert["status"] not in {"new", "triaged"}:
                raise DomainError("线索已处置", 409)
            if decision == "dismiss":
                if not reason.strip():
                    raise DomainError("误报关闭必须填写理由", 409)
                if alert["risk_score"] >= 0.9:
                    raise DomainError("制裁命中线索不能直接关闭", 409)
                self.repo.dismiss_alert(conn, alert_id, reason.strip())
                self._audit(conn, actor, "alert.dismissed",
                            {"alert_id": alert_id, "reason": reason.strip()}, alert["entity_id"])
                return {"alert": dict(self.repo.find_alert(conn, alert_id)), "case": None}
            number = (case_no or "CASE-%06d" % alert_id).strip()
            now = utcnow()
            try:
                case_id = self.repo.insert_case(
                    conn,
                    (number, alert["entity_id"], alert_id, "investigating", alert["risk_score"],
                     (assignee or actor).strip(), actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("案件编号已存在", 409) from exc
            self.repo.attach_case_to_alert(conn, alert_id, case_id)
            self._audit(conn, actor, "case.created",
                        {"case_no": number, "alert_id": alert_id}, alert["entity_id"], case_id)
            return {
                "alert": dict(self.repo.find_alert(conn, alert_id)),
                "case": dict(self.repo.find_case(conn, case_id)),
            }

    def _case_access(self, actor: str, role: str, case: sqlite3.Row) -> None:
        if role in {"supervisor", "director", "auditor"}:
            return
        if role == "investigator" and case["assignee"] == actor:
            return
        raise DomainError("案件仅限被指派的调查人员或授权角色访问", 403)

    def _load_case_checked(self, conn: sqlite3.Connection, actor: str, role: str, case_id: int) -> sqlite3.Row:
        case = self.repo.find_case(conn, case_id)
        if not case:
            raise DomainError("案件不存在", 404)
        self._case_access(actor, role, case)
        return case

    def update_case(self, actor: str, role: str, case_id: int, note: str,
                    expected_version: int, status: str | None = None,
                    assignee: str | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"investigator", "supervisor"}, "更新案件")
        if not note.strip():
            raise DomainError("调查记录不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            case = self.repo.find_case(conn, case_id)
            if not case:
                raise DomainError("案件不存在", 404)
            self._case_access(actor, role, case)
            if case["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if case["status"] in {"closed", "report_filed"} and role != "supervisor":
                raise DomainError("已结束案件不能再更新", 409)
            new_status = status or case["status"]
            if new_status not in {"open", "investigating", "escalated", "closed", "report_filed"}:
                raise DomainError("案件状态无效")
            if role == "investigator" and new_status in {"closed", "report_filed"}:
                raise DomainError("调查员不能自行结束案件", 403)
            self.repo.add_case_note(conn, case_id, actor, note.strip())
            self.repo.update_case(conn, case_id, new_status, assignee.strip() if assignee else None)
            self._audit(conn, actor, "case.updated", {"status": new_status, "note": note.strip()},
                        case["entity_id"], case_id)
            return dict(self.repo.find_case(conn, case_id))

    # ---- 冻结 / 解冻 ----
    def freeze_entity(self, actor: str, role: str, entity_id: int, reason: str,
                      expected_version: int, case_id: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor", "director"}, "冻结实体")
        if not reason.strip():
            raise DomainError("冻结原因不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            entity = self._resolve_entity(conn, entity_id)
            if entity["version"] != int(expected_version):
                raise DomainError("实体已变化，请刷新后重试", 409)
            self.repo.mark_entity_frozen(conn, entity["id"])
            # 既有客户交易即时阻断
            self.repo.block_open_transactions_of_entity(conn, entity["id"])
            # 链路：阻断命中实体账户的后续/在册转账，待复核转账转入冻结复核队列
            blocked_transfer_ids, review_ids = self._freeze_chain_for_entity(conn, entity["id"], case_id, actor)
            if case_id is not None:
                case = self.repo.find_case_for_entity(conn, case_id, entity["id"])
                if not case:
                    raise DomainError("案件与实体不匹配", 409)
                self.repo.mark_case_frozen(conn, case_id)
            self._audit(conn, actor, "entity.frozen", {
                "reason": reason.strip(),
                "blocked_transfers": blocked_transfer_ids,
                "freeze_reviews": review_ids,
            }, entity["id"], case_id)
            return dict(self.repo.find_entity(conn, entity["id"]))

    def _freeze_chain_for_entity(self, conn: sqlite3.Connection, entity_id: int,
                                 case_id: int | None, actor: str) -> tuple[list[int], list[int]]:
        account_ids = self.repo.account_ids_for_entity(conn, entity_id)
        review_ids: list[int] = []
        for transfer in self.repo.queued_review_transfers(conn, account_ids):
            self.repo.set_transfer_status(conn, transfer["id"], "frozen_review")
            self.repo.insert_freeze_review(conn, transfer["id"], entity_id, case_id, actor)
            review_ids.append(transfer["id"])
        blocked_ids: list[int] = []
        for transfer in self.repo.blocked_transfers_for_entity(conn, account_ids):
            # queued_review 已置为 frozen_review，这里只会剩 registered
            self.repo.set_transfer_status(conn, transfer["id"], "blocked")
            blocked_ids.append(transfer["id"])
        return blocked_ids, review_ids

    def unfreeze_entity(self, actor: str, role: str, entity_id: int, reason: str,
                        expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"director"}, "解除冻结")
        if not reason.strip():
            raise DomainError("解冻原因不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            entity = self._resolve_entity(conn, entity_id)
            if entity["version"] != int(expected_version):
                raise DomainError("实体已变化，请刷新后重试", 409)
            self.repo.mark_entity_unfrozen(conn, entity["id"])
            self._audit(conn, actor, "entity.unfrozen", {"reason": reason.strip()}, entity["id"])
            return dict(self.repo.find_entity(conn, entity["id"]))

    def merge_entities(self, actor: str, role: str, source_id: int, target_id: int,
                       source_version: int, target_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "合并实体")
        if source_id == target_id:
            raise DomainError("不能合并同一实体")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._resolve_entity(conn, source_id)
            target = self._resolve_entity(conn, target_id)
            if source["version"] != int(source_version) or target["version"] != int(target_version):
                raise DomainError("实体已变化，请刷新后重试", 409)
            if source["id"] == target["id"]:
                raise DomainError("实体已经合并到目标", 409)
            source_aliases = json.loads(source["aliases"])
            target_aliases = json.loads(target["aliases"])
            aliases = sorted(set(target_aliases + source_aliases + [source["canonical_name"]]), key=str.casefold)
            now = utcnow()
            risk_rank = {"low": 0, "medium": 1, "high": 2}
            risk_level = "high" if source["frozen"] or target["frozen"] else max(
                source["risk_level"], target["risk_level"], key=lambda x: risk_rank.get(x, 0)
            )
            self.repo.merge_entity_into(
                conn, target["id"], json.dumps(aliases, ensure_ascii=False), source["frozen"], risk_level, now
            )
            self.repo.redirect_entity(conn, source["id"], target["id"], now)
            self.repo.reassign_customers(conn, target["id"], source["id"])
            conn.execute("UPDATE transactions SET entity_id=? WHERE entity_id=?", (target["id"], source["id"]))
            conn.execute("UPDATE alerts SET entity_id=? WHERE entity_id=?", (target["id"], source["id"]))
            conn.execute(
                "UPDATE cases SET entity_id=?,version=version+1,updated_at=? WHERE entity_id=?",
                (target["id"], now, source["id"]),
            )
            # 链路账户节点随实体合并迁移
            self.repo.reassign_chain_accounts(conn, target["id"], source["id"])
            details = {"source": source["canonical_name"], "target": target["canonical_name"], "aliases": aliases}
            self.repo.insert_merge(
                conn, source["id"], target["id"], actor,
                json.dumps(details, ensure_ascii=False), now,
            )
            self._audit(conn, actor, "entity.merged", details, target["id"])
            return {"source": dict(self.repo.find_entity(conn, source["id"])),
                    "target": dict(self.repo.find_entity(conn, target["id"]))}

    def file_report(self, actor: str, role: str, case_id: int, report_ref: str,
                    expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "提交监管报告")
        if not report_ref.strip():
            raise DomainError("监管报告编号不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            case = self.repo.find_case(conn, case_id)
            if not case:
                raise DomainError("案件不存在", 404)
            if case["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if case["status"] not in {"investigating", "escalated"}:
                raise DomainError("当前案件状态不能提交监管报告", 409)
            if case["risk_score"] < 0.5:
                raise DomainError("低风险案件不需要监管报告", 409)
            self.repo.file_case_report(conn, case_id, report_ref.strip())
            self._audit(conn, actor, "case.report_filed",
                        {"report_ref": report_ref.strip()}, case["entity_id"], case_id)
            return dict(self.repo.find_case(conn, case_id))

    # ==================================================================
    # 资金链路调查
    # ==================================================================
    def _node_flags(self, conn: sqlite3.Connection, account_row: sqlite3.Row) -> list[dict[str, str]]:
        """节点风险标记：制裁名单命中、高风险国家、已冻结实体（全部实时计算）。"""
        flags: list[dict[str, str]] = []
        score, match = self._screen_rows(conn, account_row["holder_name"], account_row["country"])
        if match and score >= 0.9:
            flags.append({"code": "sanctions", "label": "制裁名单命中",
                          "detail": "%s：%s（相似度 %.2f）" % (match["list_name"], match["name"], score)})
        if account_row["country"] in HIGH_RISK_COUNTRIES:
            flags.append({"code": "high_risk_country", "label": "高风险国家",
                          "detail": "账户所在国家/地区 %s" % account_row["country"]})
        if account_row["entity_id"] is not None and account_row["entity_frozen"]:
            flags.append({"code": "frozen", "label": "实体已冻结",
                          "detail": account_row["entity_name"] or ("实体#%s" % account_row["entity_id"])})
        return flags

    def _node_payload(self, conn: sqlite3.Connection, account_row: sqlite3.Row, distance: int) -> dict[str, Any]:
        return {
            "id": account_row["id"],
            "account_no": account_row["account_no"],
            "holder_name": account_row["holder_name"],
            "country": account_row["country"],
            "entity_id": account_row["entity_id"],
            "entity_name": account_row["entity_name"],
            "entity_frozen": bool(account_row["entity_frozen"]),
            "distance": distance,
            "flags": self._node_flags(conn, account_row),
        }

    def _get_or_create_account(self, conn: sqlite3.Connection, account_no: str, holder_name: str,
                               country: str, actor: str) -> sqlite3.Row:
        account_no = account_no.strip()
        holder_name = holder_name.strip()
        country = country.strip().upper()
        if not account_no:
            raise DomainError("账户编号不能为空")
        if not holder_name or len(country) != 2:
            raise DomainError("账户持有人名称或国家代码无效")
        existing = self.repo.find_account_by_no(conn, account_no)
        if existing:
            return self.repo.account_view(conn, existing["id"])
        entity_id = self._find_matching_entity_id(conn, holder_name)
        account_id = self.repo.insert_account(
            conn, account_no, holder_name, normalize_name(holder_name), country, entity_id, actor
        )
        self._audit(conn, actor, "chain.account_registered",
                    {"account_no": account_no, "holder_name": holder_name, "country": country,
                     "entity_id": entity_id}, entity_id)
        return self.repo.account_view(conn, account_id)

    def _find_matching_entity_id(self, conn: sqlite3.Connection, holder_name: str) -> int | None:
        normalized = normalize_name(holder_name)
        for row in self.repo.active_entities(conn):
            names = [row["canonical_name"]] + json.loads(row["aliases"])
            if any(normalize_name(name) == normalized for name in names):
                return row["id"]
        return None

    def register_transfer(self, actor: str, role: str, biz_ref: str, from_account_no: str,
                          to_account_no: str, amount: float, currency: str,
                          from_holder: str = "", to_holder: str = "",
                          from_country: str = "", to_country: str = "",
                          occurred_at: str | None = None, case_id: int | None = None) -> dict[str, Any]:
        """登记一笔链路转账：保留上下游账户与金额；同一业务编号重复提交只记一次。"""
        actor = clean_actor(actor)
        require_role(role, {"analyst", "investigator", "supervisor"}, "登记链路转账")
        biz_ref = biz_ref.strip()
        if not biz_ref:
            raise DomainError("业务编号不能为空")
        try:
            amount = float(amount)
        except (TypeError, ValueError) as exc:
            raise DomainError("转账金额必须是数值") from exc
        if amount <= 0 or len(currency.strip()) != 3:
            raise DomainError("金额或币种无效")
        if from_account_no.strip() == to_account_no.strip():
            raise DomainError("上下游账户不能相同")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = self.repo.find_transfer_by_biz_ref(conn, biz_ref)
            if existing:
                # 幂等：同一业务编号重复提交只记一次，返回首记内容并标记 duplicate
                return {"duplicate": True,
                        "transfer": dict(existing),
                        "from": dict(self.repo.account_view(conn, existing["from_account_id"])),
                        "to": dict(self.repo.account_view(conn, existing["to_account_id"]))}
            linked_case = None
            if case_id is not None:
                linked_case = self.repo.find_case(conn, int(case_id))
                if not linked_case:
                    raise DomainError("关联案件不存在", 404)
            src = self._get_or_create_account(conn, from_account_no, from_holder or from_account_no,
                                              from_country or "XX", actor)
            dst = self._get_or_create_account(conn, to_account_no, to_holder or to_account_no,
                                              to_country or "XX", actor)
            # 重新取视图，确保实体冻结状态是本事务内的最新值（如刚刚发生实体合并）
            src, dst = self.repo.account_view(conn, src["id"]), self.repo.account_view(conn, dst["id"])
            flags: list[str] = []
            risk = 0.0
            for side, account in (("from", src), ("to", dst)):
                score, match = self._screen_rows(conn, account["holder_name"], account["country"])
                if match and score >= 0.9:
                    flags.append("%s:%s" % (side, sanctions_reason(match)))
                    risk = max(risk, 0.95)
                if account["country"] in HIGH_RISK_COUNTRIES:
                    flags.append("%s:high_risk_country:%s" % (side, account["country"]))
                    risk = max(risk, 0.75)
            frozen = bool(src["entity_frozen"] or dst["entity_frozen"])
            if frozen:
                status, risk = "blocked", 1.0
                flags.append("frozen_entity")
            elif any(f.startswith(("from:sanctions", "to:sanctions")) for f in flags):
                status = "review"
            elif any("high_risk_country" in f for f in flags):
                status = "review"
            else:
                status = "registered"
            transfer_id = self.repo.insert_transfer(
                conn,
                (biz_ref, src["id"], dst["id"], amount, currency.strip().upper(),
                 (occurred_at or utcnow()).strip(), status,
                 json.dumps(flags, ensure_ascii=False), risk, actor, utcnow()),
            )
            if linked_case is not None:
                self.repo.link_transfer_case(conn, transfer_id, linked_case["id"])
                self.repo.link_case_transfer(conn, linked_case["id"], transfer_id, actor)
            transfer = dict(self.repo.find_transfer(conn, transfer_id))
            self._audit(conn, actor, "chain.transfer_registered", {
                "biz_ref": biz_ref, "from": src["account_no"], "to": dst["account_no"],
                "amount": amount, "currency": currency.strip().upper(), "status": status,
                "flags": flags, "case_id": linked_case["id"] if linked_case else None,
            }, linked_case["entity_id"] if linked_case else None, linked_case["id"] if linked_case else None)
            return {"duplicate": False, "transfer": transfer,
                    "from": dict(src), "to": dict(dst),
                    "node_flags": {"from": self._node_flags(conn, src), "to": self._node_flags(conn, dst)}}

    def expand_chain(self, actor: str, role: str, transfer_id: int | None = None,
                     account_id: int | None = None, direction: str = "both",
                     max_depth: int | None = None) -> dict[str, Any]:
        """从任意一笔转账或任意账户出发，向前后节点做 BFS 展开并标记风险节点。"""
        actor = clean_actor(actor)
        if role not in SENSITIVE_ROLES:
            raise DomainError("角色无权查看资金链路", 403)
        if direction not in {"upstream", "downstream", "both"}:
            raise DomainError("展开方向无效")
        depth = self.MAX_CHAIN_DEPTH if max_depth is None else min(int(max_depth), self.MAX_CHAIN_DEPTH)
        if depth < 1:
            raise DomainError("展开层数无效")
        with self.connect() as conn:
            roots: set[int] = set()
            root_transfer_id = None
            if transfer_id is not None:
                transfer = self.repo.find_transfer(conn, int(transfer_id))
                if not transfer:
                    raise DomainError("转账不存在", 404)
                root_transfer_id = transfer["id"]
                roots.update({transfer["from_account_id"], transfer["to_account_id"]})
            elif account_id is not None:
                if not self.repo.account_view(conn, int(account_id)):
                    raise DomainError("账户不存在", 404)
                roots.add(int(account_id))
            else:
                raise DomainError("必须指定转账编号或账户编号")
            return self._traverse(conn, roots, depth, direction, root_transfer_id)

    def _traverse(self, conn: sqlite3.Connection, roots: set[int], depth: int,
                  direction: str, root_transfer_id: int | None) -> dict[str, Any]:
        nodes: dict[int, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        seen_edges: set[tuple[int, int, int]] = set()
        distances = {account_id: 0 for account_id in roots}
        queue = deque((account_id, 0) for account_id in roots)
        while queue:
            account_id, level = queue.popleft()
            if account_id not in nodes:
                account_row = self.repo.account_view(conn, account_id)
                nodes[account_id] = self._node_payload(conn, account_row, distances[account_id])
            if level >= depth:
                continue
            transfers: list[sqlite3.Row] = []
            if direction in {"upstream", "both"}:
                transfers.extend(self.repo.incoming(conn, account_id))
            if direction in {"downstream", "both"}:
                transfers.extend(self.repo.outgoing(conn, account_id))
            for transfer in transfers:
                edge_key = (transfer["from_account_id"], transfer["to_account_id"], transfer["id"])
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edges.append({
                        "id": transfer["id"], "biz_ref": transfer["biz_ref"],
                        "source": transfer["from_account_id"], "target": transfer["to_account_id"],
                        "amount": transfer["amount"], "currency": transfer["currency"],
                        "occurred_at": transfer["occurred_at"], "status": transfer["status"],
                        "flags": json.loads(transfer["flags"]), "risk_score": transfer["risk_score"],
                        "case_id": transfer["case_id"],
                    })
                neighbor = (transfer["to_account_id"] if transfer["from_account_id"] == account_id
                            else transfer["from_account_id"])
                if neighbor not in distances:
                    distances[neighbor] = level + 1
                    queue.append((neighbor, level + 1))
        hit_nodes = [n for n in nodes.values() if n["flags"]]
        return {
            "root_transfer_id": root_transfer_id,
            "nodes": sorted(nodes.values(), key=lambda n: (n["distance"], n["id"])),
            "edges": edges,
            "hit_count": len(hit_nodes),
            "blocked_edge_count": sum(1 for e in edges if e["status"] in {"blocked", "frozen_review"}),
            "max_depth_reached": max(distances.values()) if distances else 0,
        }

    def link_transfer_to_case(self, actor: str, role: str, case_id: int, transfer_id: int,
                              note: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"investigator", "supervisor"}, "关联链路转账到案件")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            case = self._load_case_checked(conn, actor, role, int(case_id))
            transfer = self.repo.find_transfer(conn, int(transfer_id))
            if not transfer:
                raise DomainError("转账不存在", 404)
            self.repo.link_transfer_case(conn, transfer["id"], case["id"])
            self.repo.link_case_transfer(conn, case["id"], transfer["id"], actor)
            if note.strip():
                self.repo.add_case_note(conn, case["id"], actor, note.strip())
            self._audit(conn, actor, "chain.linked_to_case",
                        {"transfer_id": transfer["id"], "biz_ref": transfer["biz_ref"]},
                        case["entity_id"], case["id"])
            return {"case_id": case["id"], "transfer_id": transfer["id"], "linked": True}

    def case_chain(self, actor: str, role: str, case_id: int,
                   max_depth: int | None = None) -> dict[str, Any]:
        """案件页资金链路：以案件关联转账为种子展开，拼出多层去向。"""
        actor = clean_actor(actor)
        with self.connect() as conn:
            case = self._load_case_checked(conn, actor, role, int(case_id))
            transfer_ids = self.repo.transfer_ids_for_case(conn, case["id"])
            if not transfer_ids:
                return {"case_id": case["id"], "root_transfer_ids": [], "nodes": [], "edges": [],
                        "hit_count": 0, "blocked_edge_count": 0, "max_depth_reached": 0}
            roots: set[int] = set()
            for transfer_id in transfer_ids:
                transfer = self.repo.find_transfer(conn, transfer_id)
                if transfer:
                    roots.update({transfer["from_account_id"], transfer["to_account_id"]})
            graph = self._traverse(
                conn, roots, self.MAX_CHAIN_DEPTH if max_depth is None else min(int(max_depth), self.MAX_CHAIN_DEPTH),
                "both", None,
            )
            graph["case_id"] = case["id"]
            graph["root_transfer_ids"] = transfer_ids
            return graph

    def get_case(self, actor: str, role: str, case_id: int,
                 include_chain: bool = True) -> dict[str, Any]:
        actor = clean_actor(actor)
        if role not in SENSITIVE_ROLES:
            raise DomainError("角色无权查看案件", 403)
        with self.connect() as conn:
            case = self._load_case_checked(conn, actor, role, int(case_id))
            notes = [dict(r) for r in self.repo.case_notes(conn, case["id"])]
            result: dict[str, Any] = {"case": dict(case), "notes": notes}
            if include_chain:
                result["chain"] = self.case_chain(actor, role, case["id"])
            return result

    # ---- 冻结复核队列 ----
    def list_freeze_reviews(self, actor: str, role: str, status: str = "pending") -> dict[str, Any]:
        actor = clean_actor(actor)
        if role not in SENSITIVE_ROLES:
            raise DomainError("角色无权查看冻结复核", 403)
        with self.connect() as conn:
            rows = self.repo.list_freeze_reviews(conn, None if status == "all" else status)
            return {"reviews": [dict(r) for r in rows]}

    def review_freeze_transfer(self, actor: str, role: str, review_id: int, decision: str,
                               reason: str = "") -> dict[str, Any]:
        """主管对冻结时转入的待复核转账做裁决：release 放行，uphold 维持阻断。"""
        actor = clean_actor(actor)
        require_role(role, {"supervisor", "director"}, "冻结复核裁决")
        if decision not in {"release", "uphold"}:
            raise DomainError("复核决定无效")
        if not reason.strip():
            raise DomainError("复核必须填写理由", 409)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            review = self.repo.find_freeze_review(conn, int(review_id))
            if not review:
                raise DomainError("复核记录不存在", 404)
            if review["status"] != "pending":
                raise DomainError("该转账已完成复核", 409)
            if review["frozen_by"] and review["frozen_by"] == actor:
                raise DomainError("冻结操作人与复核人不能为同一人，请交由其他主管复核", 409)
            new_status = "released" if decision == "release" else "upheld"
            transfer_status = "registered" if decision == "release" else "blocked"
            self.repo.resolve_freeze_review(conn, review["id"], new_status, reason.strip(), actor)
            self.repo.set_transfer_status(conn, review["transfer_id"], transfer_status)
            self._audit(conn, actor, "chain.freeze_reviewed", {
                "review_id": review["id"], "transfer_id": review["transfer_id"],
                "decision": decision, "reason": reason.strip(),
            }, review["frozen_entity_id"], review["case_id"])
            return {"review": dict(self.repo.find_freeze_review(conn, review["id"])),
                    "transfer_status": transfer_status}

    # ---- 全局视图 ----
    def state(self, actor: str = "", role: str = "viewer") -> dict[str, Any]:
        if role not in SENSITIVE_ROLES:
            return {"entities": [], "transactions": [], "alerts": [], "cases": [], "timeline": [],
                    "chain_accounts": [], "chain_transfers": [], "freeze_reviews": [], "access_limited": True}
        with self.connect() as conn:
            if role == "investigator":
                cases = [dict(r) for r in self.repo.cases_for_assignee(conn, actor)]
            else:
                cases = [dict(r) for r in self.repo.all_cases(conn)]
            entities = [dict(r) for r in self.repo.latest_entities(conn)]
            transactions = [dict(r) for r in self.repo.latest_transactions(conn)]
            case_ids = [c["id"] for c in cases]
            if role == "investigator":
                alerts = [dict(r) for r in self.repo.alerts_for_cases(conn, case_ids)]
            else:
                alerts = [dict(r) for r in self.repo.latest_alerts(conn)]
            timeline = [dict(r) for r in self.repo.latest_timeline(conn)]
            chain_accounts = [dict(r) for r in conn.execute(
                "SELECT * FROM chain_accounts ORDER BY id DESC LIMIT 100"
            ).fetchall()]
            chain_transfers = [dict(r) for r in conn.execute(
                "SELECT * FROM chain_transfers ORDER BY id DESC LIMIT 100"
            ).fetchall()]
            freeze_reviews = [dict(r) for r in self.repo.list_freeze_reviews(conn, "pending")]
        return {
            "entities": entities, "transactions": transactions, "alerts": alerts,
            "cases": cases, "timeline": timeline,
            "chain_accounts": chain_accounts, "chain_transfers": chain_transfers,
            "freeze_reviews": freeze_reviews, "access_limited": False,
        }

    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM entities").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        entity = self.create_entity("analyst-demo", "analyst", "organization", "海岳贸易有限公司", ["海岳贸易"])
        customer = self.create_customer("analyst-demo", "analyst", entity["id"], "CUST-0001", "CN", "贸易", False, 0.2)
        self.add_or_update_watchlist("sup-demo", "supervisor", "OFAC", "海岳贸易有限公司", ["CN"])
        result = self.ingest_transaction("analyst-demo", "analyst", "TXN-DEMO-0001", customer["id"], 150000, "USD", "海岳贸易有限公司", "CN")
        # 资金链路：客户 -> 多层空壳 -> 受限地区（KP），制裁名单命中空壳层之一
        shell = self.create_entity("sup-demo", "supervisor", "organization", "空壳中转C有限公司", [])
        self.add_or_update_watchlist("sup-demo", "supervisor", "OFAC", "空壳中转C有限公司", ["HK"])
        t1 = self.register_transfer("analyst-demo", "analyst", "BIZ-DEMO-0001",
                                    "ACC-CUST-1", "ACC-SHELL-A", 150000, "USD",
                                    "海岳贸易有限公司", "空壳A公司", "CN", "HK")
        t2 = self.register_transfer("analyst-demo", "analyst", "BIZ-DEMO-0002",
                                    "ACC-SHELL-A", "ACC-SHELL-B", 148000, "USD",
                                    "空壳A公司", "空壳中转C有限公司", "HK", "HK")
        t3 = self.register_transfer("analyst-demo", "analyst", "BIZ-DEMO-0003",
                                    "ACC-SHELL-B", "ACC-DPRK-1", 146000, "USD",
                                    "空壳中转C有限公司", "平壤金达莱商社", "HK", "KP")
        return {
            "seeded": True, "entity_id": entity["id"], "customer_id": customer["id"],
            "alert_id": result["alert"]["id"] if result["alert"] else None,
            "chain_transfer_ids": [t1["transfer"]["id"], t2["transfer"]["id"], t3["transfer"]["id"]],
        }
