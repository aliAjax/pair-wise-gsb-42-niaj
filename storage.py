"""存储层：SQLite schema 与 Repository，只负责持久化，不承载业务规则。"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from domain import DEFAULT_DB, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    risk_level TEXT NOT NULL DEFAULT 'low',
    frozen INTEGER NOT NULL DEFAULT 0,
    merged_into INTEGER REFERENCES entities(id),
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    customer_no TEXT NOT NULL UNIQUE,
    country TEXT NOT NULL,
    occupation TEXT NOT NULL DEFAULT '',
    allowlisted INTEGER NOT NULL DEFAULT 0,
    risk_score REAL NOT NULL DEFAULT 0,
    merged_into INTEGER REFERENCES customers(id),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_name TEXT NOT NULL,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    countries TEXT NOT NULL DEFAULT '[]',
    version INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(list_name, normalized_name)
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_ref TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    counterparty_name TEXT NOT NULL,
    counterparty_country TEXT NOT NULL,
    status TEXT NOT NULL,
    risk_score REAL NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    transaction_id INTEGER REFERENCES transactions(id),
    reason TEXT NOT NULL,
    risk_score REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    occurrences INTEGER NOT NULL DEFAULT 1,
    case_id INTEGER,
    dismissed_reason TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_no TEXT NOT NULL UNIQUE,
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    alert_id INTEGER REFERENCES alerts(id),
    status TEXT NOT NULL DEFAULT 'open',
    risk_score REAL NOT NULL,
    assignee TEXT,
    freeze_target INTEGER NOT NULL DEFAULT 0,
    report_ref TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    actor TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_transfers (
    case_id INTEGER NOT NULL REFERENCES cases(id),
    transfer_id INTEGER NOT NULL REFERENCES chain_transfers(id),
    linked_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(case_id, transfer_id)
);
CREATE TABLE IF NOT EXISTS entity_merges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    actor TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER REFERENCES cases(id),
    entity_id INTEGER REFERENCES entities(id),
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 资金链路：账户为节点（上游付款方 / 下游收款方），转账为有向边。
CREATE TABLE IF NOT EXISTS chain_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_no TEXT NOT NULL UNIQUE,
    holder_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    country TEXT NOT NULL,
    entity_id INTEGER REFERENCES entities(id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chain_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    biz_ref TEXT NOT NULL UNIQUE,
    from_account_id INTEGER NOT NULL REFERENCES chain_accounts(id),
    to_account_id INTEGER NOT NULL REFERENCES chain_accounts(id),
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'registered',
    flags TEXT NOT NULL DEFAULT '[]',
    risk_score REAL NOT NULL DEFAULT 0,
    case_id INTEGER REFERENCES cases(id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 冻结时转入的待复核转账队列：主管放行或维持阻断。
CREATE TABLE IF NOT EXISTS freeze_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id INTEGER NOT NULL UNIQUE REFERENCES chain_transfers(id),
    frozen_entity_id INTEGER NOT NULL REFERENCES entities(id),
    frozen_by TEXT NOT NULL DEFAULT '',
    case_id INTEGER REFERENCES cases(id),
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_txn_entity ON transactions(entity_id, created_at);
CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status, risk_score DESC);
CREATE INDEX IF NOT EXISTS idx_transfer_from ON chain_transfers(from_account_id);
CREATE INDEX IF NOT EXISTS idx_transfer_to ON chain_transfers(to_account_id);
CREATE INDEX IF NOT EXISTS idx_account_entity ON chain_accounts(entity_id);
CREATE INDEX IF NOT EXISTS idx_freeze_review_status ON freeze_reviews(status);
"""


class Repository:
    """SQLite 数据访问对象。所有方法使用调用方/服务层打开的连接，事务边界由业务层控制。"""

    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # ---- 通用 ----
    def audit(self, conn: sqlite3.Connection, actor: str, action: str, details: dict[str, Any],
              entity_id: int | None = None, case_id: int | None = None) -> None:
        conn.execute(
            "INSERT INTO timeline(case_id,entity_id,actor,action,details,created_at) VALUES(?,?,?,?,?,?)",
            (case_id, entity_id, actor, action, json.dumps(details, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    # ---- 实体 ----
    def find_entity(self, conn: sqlite3.Connection, entity_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()

    def active_entities(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM entities WHERE merged_into IS NULL").fetchall()

    def insert_entity(self, conn: sqlite3.Connection, entity_type: str, canonical_name: str,
                      normalized_name: str, aliases_json: str, actor: str) -> int:
        now = utcnow()
        cur = conn.execute(
            """INSERT INTO entities(entity_type,canonical_name,normalized_name,aliases,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (entity_type, canonical_name, normalized_name, aliases_json, actor, now, now),
        )
        return cur.lastrowid

    def update_entity_aliases(self, conn: sqlite3.Connection, entity_id: int, aliases_json: str,
                              expected_version: int) -> None:
        conn.execute(
            "UPDATE entities SET aliases=?,version=version+1,updated_at=? WHERE id=? AND version=?",
            (aliases_json, utcnow(), entity_id, expected_version),
        )

    def mark_entity_frozen(self, conn: sqlite3.Connection, entity_id: int) -> None:
        conn.execute(
            "UPDATE entities SET frozen=1,risk_level='high',version=version+1,updated_at=? WHERE id=?",
            (utcnow(), entity_id),
        )

    def mark_entity_unfrozen(self, conn: sqlite3.Connection, entity_id: int) -> None:
        conn.execute(
            "UPDATE entities SET frozen=0,risk_level='medium',version=version+1,updated_at=? WHERE id=?",
            (utcnow(), entity_id),
        )

    def redirect_entity(self, conn: sqlite3.Connection, source_id: int, target_id: int, now: str) -> None:
        conn.execute("UPDATE entities SET merged_into=?,version=version+1,updated_at=? WHERE id=?", (target_id, now, source_id))

    def merge_entity_into(self, conn: sqlite3.Connection, target_id: int, aliases_json: str,
                          frozen: int, risk_level: str, now: str) -> None:
        conn.execute(
            """UPDATE entities SET aliases=?,frozen=MAX(frozen,?),risk_level=?,version=version+1,updated_at=?
               WHERE id=?""",
            (aliases_json, frozen, risk_level, now, target_id),
        )

    def latest_entities(self, conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM entities ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ---- 客户 ----
    def find_customer(self, conn: sqlite3.Connection, customer_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()

    def insert_customer(self, conn: sqlite3.Connection, entity_id: int, customer_no: str,
                        country: str, occupation: str, allowlisted: int, risk_score: float) -> int:
        cur = conn.execute(
            """INSERT INTO customers(entity_id,customer_no,country,occupation,allowlisted,risk_score,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (entity_id, customer_no, country, occupation, allowlisted, risk_score, utcnow()),
        )
        return cur.lastrowid

    def reassign_customers(self, conn: sqlite3.Connection, target_id: int, source_id: int) -> None:
        conn.execute(
            "UPDATE customers SET entity_id=?,version=version+1 WHERE entity_id=? AND merged_into IS NULL",
            (target_id, source_id),
        )

    # ---- 名单 ----
    def active_watchlist(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM watchlist WHERE active=1").fetchall()

    def find_watchlist_entry(self, conn: sqlite3.Connection, list_name: str,
                             normalized_name: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM watchlist WHERE list_name=? AND normalized_name=?", (list_name, normalized_name)
        ).fetchone()

    def insert_watchlist(self, conn: sqlite3.Connection, list_name: str, name: str,
                         normalized_name: str, countries_json: str, active: int, actor: str) -> int:
        now = utcnow()
        cur = conn.execute(
            "INSERT INTO watchlist(list_name,name,normalized_name,countries,active,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)",
            (list_name, name, normalized_name, countries_json, active, actor, now),
        )
        return cur.lastrowid

    def update_watchlist(self, conn: sqlite3.Connection, entry_id: int, name: str,
                         countries_json: str, active: int, actor: str) -> None:
        conn.execute(
            "UPDATE watchlist SET name=?,countries=?,active=?,version=version+1,updated_by=?,updated_at=? WHERE id=?",
            (name, countries_json, active, actor, utcnow(), entry_id),
        )

    # ---- 交易筛查 ----
    def insert_transaction(self, conn: sqlite3.Connection, values: tuple[Any, ...]) -> int:
        cur = conn.execute(
            """INSERT INTO transactions(txn_ref,customer_id,entity_id,amount,currency,counterparty_name,
               counterparty_country,status,risk_score,reason,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        return cur.lastrowid

    def find_transaction(self, conn: sqlite3.Connection, transaction_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()

    def block_open_transactions_of_entity(self, conn: sqlite3.Connection, entity_id: int) -> None:
        conn.execute(
            "UPDATE transactions SET status='blocked' WHERE entity_id=? AND status IN ('review','allowed')",
            (entity_id,),
        )

    def latest_transactions(self, conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ---- 线索 ----
    def find_alert(self, conn: sqlite3.Connection, alert_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()

    def find_alert_by_fingerprint(self, conn: sqlite3.Connection, fingerprint: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM alerts WHERE fingerprint=?", (fingerprint,)).fetchone()

    def insert_alert(self, conn: sqlite3.Connection, fingerprint: str, entity_id: int,
                     transaction_id: int, reason: str, risk_score: float) -> int:
        now = utcnow()
        cur = conn.execute(
            """INSERT INTO alerts(fingerprint,entity_id,transaction_id,reason,risk_score,first_seen,last_seen)
               VALUES(?,?,?,?,?,?,?)""",
            (fingerprint, entity_id, transaction_id, reason, risk_score, now, now),
        )
        return cur.lastrowid

    def bump_alert(self, conn: sqlite3.Connection, alert_id: int, transaction_id: int, risk_score: float) -> None:
        conn.execute(
            "UPDATE alerts SET occurrences=occurrences+1,last_seen=?,risk_score=MAX(risk_score,?),transaction_id=? WHERE id=?",
            (utcnow(), risk_score, transaction_id, alert_id),
        )

    def attach_case_to_alert(self, conn: sqlite3.Connection, alert_id: int, case_id: int) -> None:
        conn.execute("UPDATE alerts SET status='case_created',case_id=? WHERE id=?", (case_id, alert_id))

    def dismiss_alert(self, conn: sqlite3.Connection, alert_id: int, reason: str) -> None:
        conn.execute("UPDATE alerts SET status='dismissed',dismissed_reason=? WHERE id=?", (reason, alert_id))

    def latest_alerts(self, conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def alerts_for_cases(self, conn: sqlite3.Connection, case_ids: list[int]) -> list[sqlite3.Row]:
        if not case_ids:
            return []
        marks = ",".join("?" for _ in case_ids)
        return conn.execute(
            "SELECT * FROM alerts WHERE case_id IN (%s) ORDER BY id DESC LIMIT 100" % marks, case_ids
        ).fetchall()

    # ---- 案件 ----
    def find_case(self, conn: sqlite3.Connection, case_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()

    def find_case_for_entity(self, conn: sqlite3.Connection, case_id: int, entity_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM cases WHERE id=? AND entity_id=?", (case_id, entity_id)
        ).fetchone()

    def insert_case(self, conn: sqlite3.Connection, values: tuple[Any, ...]) -> int:
        cur = conn.execute(
            """INSERT INTO cases(case_no,entity_id,alert_id,status,risk_score,assignee,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            values,
        )
        return cur.lastrowid

    def update_case(self, conn: sqlite3.Connection, case_id: int, status: str, assignee: str | None) -> None:
        conn.execute(
            "UPDATE cases SET status=?,assignee=COALESCE(?,assignee),version=version+1,updated_at=? WHERE id=?",
            (status, assignee, utcnow(), case_id),
        )

    def mark_case_frozen(self, conn: sqlite3.Connection, case_id: int) -> None:
        conn.execute(
            """UPDATE cases SET freeze_target=1,status=CASE WHEN status='investigating' THEN 'escalated' ELSE status END,
               version=version+1,updated_at=? WHERE id=?""",
            (utcnow(), case_id),
        )

    def file_case_report(self, conn: sqlite3.Connection, case_id: int, report_ref: str) -> None:
        conn.execute(
            "UPDATE cases SET status='report_filed',report_ref=?,version=version+1,updated_at=? WHERE id=?",
            (report_ref, utcnow(), case_id),
        )

    def add_case_note(self, conn: sqlite3.Connection, case_id: int, actor: str, note: str) -> None:
        conn.execute(
            "INSERT INTO case_notes(case_id,actor,note,created_at) VALUES(?,?,?,?)",
            (case_id, actor, note, utcnow()),
        )

    def case_notes(self, conn: sqlite3.Connection, case_id: int) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM case_notes WHERE case_id=? ORDER BY id", (case_id,)).fetchall()

    def all_cases(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM cases ORDER BY id DESC").fetchall()

    def cases_for_assignee(self, conn: sqlite3.Connection, assignee: str) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM cases WHERE assignee=? ORDER BY id DESC", (assignee,)).fetchall()

    # ---- 合并记录 ----
    def insert_merge(self, conn: sqlite3.Connection, source_id: int, target_id: int,
                     actor: str, details_json: str, now: str) -> None:
        conn.execute(
            "INSERT INTO entity_merges(source_id,target_id,actor,details,created_at) VALUES(?,?,?,?,?)",
            (source_id, target_id, actor, details_json, now),
        )

    def reassign_chain_accounts(self, conn: sqlite3.Connection, target_id: int, source_id: int) -> None:
        conn.execute("UPDATE chain_accounts SET entity_id=? WHERE entity_id=?", (target_id, source_id))

    def attach_unlinked_accounts(self, conn: sqlite3.Connection, entity_id: int, normalized_name: str) -> int:
        cur = conn.execute(
            "UPDATE chain_accounts SET entity_id=? WHERE entity_id IS NULL AND normalized_name=?",
            (entity_id, normalized_name),
        )
        return cur.rowcount

    # ---- 时间线 ----
    def latest_timeline(self, conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM timeline ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ---- 资金链路：账户节点 ----
    def find_account_by_no(self, conn: sqlite3.Connection, account_no: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM chain_accounts WHERE account_no=?", (account_no,)).fetchone()

    def insert_account(self, conn: sqlite3.Connection, account_no: str, holder_name: str,
                       normalized_name: str, country: str, entity_id: int | None, actor: str) -> int:
        cur = conn.execute(
            """INSERT INTO chain_accounts(account_no,holder_name,normalized_name,country,entity_id,created_by,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (account_no, holder_name, normalized_name, country, entity_id, actor, utcnow()),
        )
        return cur.lastrowid

    def account_view(self, conn: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
        return conn.execute(
            """SELECT a.*, e.canonical_name AS entity_name, e.frozen AS entity_frozen, e.risk_level AS entity_risk
               FROM chain_accounts a LEFT JOIN entities e ON e.id=a.entity_id WHERE a.id=?""",
            (account_id,),
        ).fetchone()

    def account_views(self, conn: sqlite3.Connection, account_ids: list[int]) -> dict[int, sqlite3.Row]:
        if not account_ids:
            return {}
        marks = ",".join("?" for _ in account_ids)
        rows = conn.execute(
            """SELECT a.*, e.canonical_name AS entity_name, e.frozen AS entity_frozen, e.risk_level AS entity_risk
               FROM chain_accounts a LEFT JOIN entities e ON e.id=a.entity_id
               WHERE a.id IN (%s)""" % marks,
            account_ids,
        ).fetchall()
        return {r["id"]: r for r in rows}

    # ---- 资金链路：转账边 ----
    def find_transfer_by_biz_ref(self, conn: sqlite3.Connection, biz_ref: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM chain_transfers WHERE biz_ref=?", (biz_ref,)).fetchone()

    def find_transfer(self, conn: sqlite3.Connection, transfer_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM chain_transfers WHERE id=?", (transfer_id,)).fetchone()

    def insert_transfer(self, conn: sqlite3.Connection, values: tuple[Any, ...]) -> int:
        cur = conn.execute(
            """INSERT INTO chain_transfers(biz_ref,from_account_id,to_account_id,amount,currency,occurred_at,
               status,flags,risk_score,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        return cur.lastrowid

    def set_transfer_status(self, conn: sqlite3.Connection, transfer_id: int, status: str) -> None:
        conn.execute("UPDATE chain_transfers SET status=? WHERE id=?", (status, transfer_id))

    def link_transfer_case(self, conn: sqlite3.Connection, transfer_id: int, case_id: int) -> None:
        conn.execute("UPDATE chain_transfers SET case_id=? WHERE id=?", (case_id, transfer_id))

    def outgoing(self, conn: sqlite3.Connection, account_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM chain_transfers WHERE from_account_id=? ORDER BY occurred_at,id", (account_id,)
        ).fetchall()

    def incoming(self, conn: sqlite3.Connection, account_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM chain_transfers WHERE to_account_id=? ORDER BY occurred_at,id", (account_id,)
        ).fetchall()

    def transfer_ids_for_case(self, conn: sqlite3.Connection, case_id: int) -> list[int]:
        rows = conn.execute(
            "SELECT transfer_id FROM case_transfers WHERE case_id=? UNION SELECT id FROM chain_transfers WHERE case_id=?",
            (case_id, case_id),
        ).fetchall()
        return [r["transfer_id"] for r in rows]

    def link_case_transfer(self, conn: sqlite3.Connection, case_id: int, transfer_id: int, actor: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO case_transfers(case_id,transfer_id,linked_by,created_at) VALUES(?,?,?,?)",
            (case_id, transfer_id, actor, utcnow()),
        )

    def queued_review_transfers(self, conn: sqlite3.Connection,
                                account_ids: list[int]) -> list[sqlite3.Row]:
        """冻结时：命中被冻账户、处于待复核(review)状态的转账，进冻结复核队列。"""
        if not account_ids:
            return []
        marks = ",".join("?" for _ in account_ids)
        return conn.execute(
            """SELECT * FROM chain_transfers WHERE status='review'
               AND (from_account_id IN (%s) OR to_account_id IN (%s))""" % (marks, marks),
            account_ids + account_ids,
        ).fetchall()

    def blocked_transfers_for_entity(self, conn: sqlite3.Connection, account_ids: list[int]) -> list[sqlite3.Row]:
        if not account_ids:
            return []
        marks = ",".join("?" for _ in account_ids)
        return conn.execute(
            """SELECT * FROM chain_transfers WHERE status IN ('registered','review')
               AND (from_account_id IN (%s) OR to_account_id IN (%s))""" % (marks, marks),
            account_ids + account_ids,
        ).fetchall()

    def account_ids_for_entity(self, conn: sqlite3.Connection, entity_id: int) -> list[int]:
        return [r["id"] for r in conn.execute(
            "SELECT id FROM chain_accounts WHERE entity_id=?", (entity_id,)
        ).fetchall()]

    # ---- 冻结复核队列 ----
    def insert_freeze_review(self, conn: sqlite3.Connection, transfer_id: int, entity_id: int,
                             case_id: int | None, frozen_by: str = "") -> None:
        conn.execute(
            """INSERT OR IGNORE INTO freeze_reviews(transfer_id,frozen_entity_id,frozen_by,case_id,created_at)
               VALUES(?,?,?,?,?)""",
            (transfer_id, entity_id, frozen_by, case_id, utcnow()),
        )

    def find_freeze_review(self, conn: sqlite3.Connection, review_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM freeze_reviews WHERE id=?", (review_id,)).fetchone()

    def resolve_freeze_review(self, conn: sqlite3.Connection, review_id: int, status: str,
                              reason: str, reviewer: str) -> None:
        conn.execute(
            "UPDATE freeze_reviews SET status=?,reason=?,reviewed_by=?,reviewed_at=? WHERE id=?",
            (status, reason, reviewer, utcnow(), review_id),
        )

    def list_freeze_reviews(self, conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
        sql = (
            """SELECT fr.*, t.biz_ref, t.amount, t.currency, t.status AS transfer_status,
                      t.from_account_id, t.to_account_id, t.flags,
                      fa.holder_name AS from_holder, fa.account_no AS from_account_no,
                      ta.holder_name AS to_holder, ta.account_no AS to_account_no,
                      e.canonical_name AS frozen_entity_name
               FROM freeze_reviews fr
               JOIN chain_transfers t ON t.id=fr.transfer_id
               JOIN chain_accounts fa ON fa.id=t.from_account_id
               JOIN chain_accounts ta ON ta.id=t.to_account_id
               JOIN entities e ON e.id=fr.frozen_entity_id"""
        )
        if status:
            return conn.execute(sql + " WHERE fr.status=? ORDER BY fr.id DESC", (status,)).fetchall()
        return conn.execute(sql + " ORDER BY fr.id DESC LIMIT 200").fetchall()
