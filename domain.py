"""共享领域层：常量、异常与无状态规则工具（不依赖数据库和 HTTP）。"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "financial_crime.db"

HIGH_RISK_COUNTRIES = {"KP", "IR", "SY", "CU", "RU"}
SENSITIVE_ROLES = {"investigator", "supervisor", "director", "auditor"}


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_name(value: str) -> str:
    return re.sub(r"[^0-9a-z一-鿿]+", "", (value or "").casefold())


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("角色无权执行：%s" % action, 403)


def clean_actor(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise DomainError("缺少操作人")
    return actor


def name_similarity(left: str, right: str) -> float:
    left, right = normalize_name(left), normalize_name(right)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def sanctions_reason(match: dict) -> str:
    return "sanctions_match:%s:%s" % (match["list_name"], match["name"])
