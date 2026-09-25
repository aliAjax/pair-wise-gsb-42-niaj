"""资金链路存储层：账户节点与转账边的 SQLite 持久化。

只负责建表和 SQL，不含业务规则；业务逻辑在 chain_service.py。
与主应用共用同一个 SQLite 文件，表使用 chain_ 前缀保持独立。
Store 方法都接收调用方传入的连接，便于主应用在自身事务里做冻结传导。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_no TEXT NOT NULL UNIQUE,
    holder_name TEXT NOT NULL,
    country TEXT NOT NULL,
    entity_id INTEGER REFERENCES entities(id),
    sanctioned INTEGER NOT NULL DEFAULT 0,
    high_risk INTEGER NOT NULL DEFAULT 0,
    frozen INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chain_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    biz_ref TEXT NOT NULL UNIQUE,
    from_account_id INTEGER NOT NULL REFERENCES chain_accounts(id),
    to_account_id INTEGER NOT NULL REFERENCES chain_accounts(id),
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    flag_reason TEXT NOT NULL DEFAULT '',
    case_id INTEGER REFERENCES cases(id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chain_out ON chain_transfers(from_account_id);
CREATE INDEX IF NOT EXISTS idx_chain_in ON chain_transfers(to_account_id);
CREATE INDEX IF NOT EXISTS idx_chain_case ON chain_transfers(case_id);
"""


class ChainStore:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # ---- 账户节点 ----
    def find_account(self, conn: sqlite3.Connection, account_no: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM chain_accounts WHERE account_no=?", (account_no,)).fetchone()

    def get_account(self, conn: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM chain_accounts WHERE id=?", (account_id,)).fetchone()

    def accounts_by_ids(self, conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        return conn.execute("SELECT * FROM chain_accounts WHERE id IN (%s) ORDER BY id" % marks, ids).fetchall()

    def insert_account(self, conn: sqlite3.Connection, account_no: str, holder_name: str, country: str,
                       entity_id: int | None, sanctioned: bool, high_risk: bool, now: str) -> int:
        cur = conn.execute(
            """INSERT INTO chain_accounts(account_no,holder_name,country,entity_id,sanctioned,high_risk,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (account_no, holder_name, country, entity_id, int(sanctioned), int(high_risk), now, now),
        )
        return cur.lastrowid

    def update_account(self, conn: sqlite3.Connection, account_id: int, holder_name: str, country: str,
                       entity_id: int | None, sanctioned: bool, high_risk: bool, now: str) -> None:
        conn.execute(
            """UPDATE chain_accounts SET holder_name=?,country=?,entity_id=?,sanctioned=?,high_risk=?,updated_at=?
               WHERE id=?""",
            (holder_name, country, entity_id, int(sanctioned), int(high_risk), now, account_id),
        )

    # ---- 转账边 ----
    def find_transfer_by_ref(self, conn: sqlite3.Connection, biz_ref: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM chain_transfers WHERE biz_ref=?", (biz_ref,)).fetchone()

    def get_transfer(self, conn: sqlite3.Connection, transfer_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM chain_transfers WHERE id=?", (transfer_id,)).fetchone()

    def insert_transfer(self, conn: sqlite3.Connection, biz_ref: str, from_id: int, to_id: int,
                        amount: float, currency: str, status: str, flag_reason: str,
                        case_id: int | None, actor: str, now: str) -> int:
        cur = conn.execute(
            """INSERT INTO chain_transfers(biz_ref,from_account_id,to_account_id,amount,currency,status,
               flag_reason,case_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (biz_ref, from_id, to_id, amount, currency, status, flag_reason, case_id, actor, now),
        )
        return cur.lastrowid

    def transfers_out(self, conn: sqlite3.Connection, account_id: int, limit: int = 500) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM chain_transfers WHERE from_account_id=? ORDER BY id LIMIT ?", (account_id, limit)
        ).fetchall()

    def transfers_in(self, conn: sqlite3.Connection, account_id: int, limit: int = 500) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM chain_transfers WHERE to_account_id=? ORDER BY id LIMIT ?", (account_id, limit)
        ).fetchall()

    def transfers_for_case(self, conn: sqlite3.Connection, case_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM chain_transfers WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()

    def count_transfers(self, conn: sqlite3.Connection) -> int:
        return conn.execute("SELECT COUNT(*) AS c FROM chain_transfers").fetchone()["c"]

    # ---- 冻结传导（由主应用在同一事务内调用）----
    def set_entity_accounts_frozen(self, conn: sqlite3.Connection, entity_id: int, frozen: bool, now: str) -> None:
        conn.execute(
            "UPDATE chain_accounts SET frozen=?,updated_at=? WHERE entity_id=?",
            (int(frozen), now, entity_id),
        )

    def move_review_to_frozen(self, conn: sqlite3.Connection, entity_id: int) -> None:
        conn.execute(
            """UPDATE chain_transfers SET status='frozen_review'
               WHERE status='review' AND (from_account_id IN (SELECT id FROM chain_accounts WHERE entity_id=?)
                                      OR to_account_id IN (SELECT id FROM chain_accounts WHERE entity_id=?))""",
            (entity_id, entity_id),
        )

    def restore_frozen_review(self, conn: sqlite3.Connection, entity_id: int) -> None:
        conn.execute(
            """UPDATE chain_transfers SET status='review'
               WHERE status='frozen_review' AND (from_account_id IN (SELECT id FROM chain_accounts WHERE entity_id=?)
                                             OR to_account_id IN (SELECT id FROM chain_accounts WHERE entity_id=?))""",
            (entity_id, entity_id),
        )

    def reassign_entity(self, conn: sqlite3.Connection, source_id: int, target_id: int) -> None:
        conn.execute("UPDATE chain_accounts SET entity_id=? WHERE entity_id=?", (target_id, source_id))

    # ---- 主应用表的只读查询与审计 ----
    def entity_is_frozen(self, conn: sqlite3.Connection, entity_id: int) -> bool:
        row = conn.execute("SELECT frozen FROM entities WHERE id=?", (entity_id,)).fetchone()
        return bool(row and row["frozen"])

    def active_watchlist(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM watchlist WHERE active=1").fetchall()

    def find_case(self, conn: sqlite3.Connection, case_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()

    def log_audit(self, conn: sqlite3.Connection, actor: str, action: str,
                  details: dict[str, Any], case_id: int | None, now: str) -> None:
        conn.execute(
            "INSERT INTO timeline(case_id,entity_id,actor,action,details,created_at) VALUES(?,?,?,?,?,?)",
            (case_id, None, actor, action, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )
