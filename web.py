"""页面/HTTP 层：仅做请求解析、鉴权头透传和 JSON 响应，业务规则全部在 service 层。"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from domain import DEFAULT_DB, DomainError
from service import FinancialCrimeService

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"


class ApiHandler(BaseHTTPRequestHandler):
    service: FinancialCrimeService

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, rel: str, content_type: str) -> None:
        body = (STATIC_DIR / rel).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _headers(self) -> tuple[str, str]:
        return self.headers.get("X-User", ""), self.headers.get("X-Role", "viewer")

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise DomainError("请求体过大", 413)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise DomainError("JSON 请求体必须是对象")
        return value

    @staticmethod
    def _query(path: str) -> dict[str, Any]:
        parsed = urlparse(path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if "transfer_id" in query:
            query["transfer_id"] = int(query["transfer_id"])
        if "account_id" in query:
            query["account_id"] = int(query["account_id"])
        if "case_id" in query:
            query["case_id"] = int(query["case_id"])
        if "max_depth" in query:
            query["max_depth"] = int(query["max_depth"])
        if "review_id" in query:
            query["review_id"] = int(query["review_id"])
        query["_path"] = parsed.path
        return query

    def do_GET(self) -> None:
        try:
            query = self._query(self.path)
            path = query.pop("_path")
            if path in {"/", "/index.html"}:
                self._serve_static("index.html", "text/html; charset=utf-8")
                return
            actor, role = self._headers()
            if path == "/health":
                self._send(200, {"status": "ok", "service": "financial-crime"})
            elif path == "/api/state":
                self._send(200, self.service.state(actor, role))
            elif path == "/api/chain/expand":
                self._send(200, self.service.expand_chain(actor, role, **query))
            elif path == "/api/freeze-reviews":
                self._send(200, self.service.list_freeze_reviews(
                    actor, role, query.get("status", "pending")))
            elif path.startswith("/api/cases/"):
                # /api/cases/{id} 与 /api/cases/{id}/chain
                parts = [p for p in path.split("/") if p]
                case_id = int(parts[2])
                if len(parts) == 4 and parts[3] == "chain":
                    self._send(200, self.service.case_chain(
                        actor, role, case_id, query.get("max_depth")))
                else:
                    self._send(200, self.service.get_case(
                        actor, role, case_id, include_chain=True))
            else:
                self._send(404, {"error": "接口不存在"})
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (ValueError, IndexError) as exc:
            self._send(400, {"error": str(exc)})

    # (接口, 是否允许 200 幂等返回)
    POST_ROUTES = {
        "/api/entities": ("create_entity", False),
        "/api/entities/alias": ("add_alias", False),
        "/api/entities/merge": ("merge_entities", False),
        "/api/entities/freeze": ("freeze_entity", False),
        "/api/entities/unfreeze": ("unfreeze_entity", False),
        "/api/customers": ("create_customer", False),
        "/api/watchlist": ("add_or_update_watchlist", False),
        "/api/transactions": ("ingest_transaction", False),
        "/api/alerts/triage": ("triage_alert", False),
        "/api/cases/update": ("update_case", False),
        "/api/cases/report": ("file_report", False),
        "/api/chain/transfers": ("register_transfer", True),
        "/api/chain/link-case": ("link_transfer_to_case", False),
        "/api/freeze-reviews/review": ("review_freeze_transfer", False),
    }

    def do_POST(self) -> None:
        try:
            path, data, (actor, role) = urlparse(self.path).path, self._json(), self._headers()
            route = self.POST_ROUTES.get(path)
            if not route:
                raise DomainError("接口不存在", 404)
            method_name, idempotent = route
            result = getattr(self.service, method_name)(actor, role, **data)
            status = 200 if idempotent and result.get("duplicate") else 201
            self._send(status, result)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": "请求参数错误: %s" % exc})
        except Exception as exc:  # pragma: no cover - 兜底
            self._send(500, {"error": "服务器内部错误", "detail": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def create_server(service: FinancialCrimeService, host: str, port: int) -> ThreadingHTTPServer:
    ApiHandler.service = service
    return ThreadingHTTPServer((host, port), ApiHandler)


def serve(service: FinancialCrimeService, host: str, port: int) -> None:
    server = create_server(service, host, port)
    print("Financial crime service listening on http://%s:%s" % (host, port))
    server.serve_forever()
