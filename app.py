"""服务入口：解析命令行参数并启动 HTTP 服务。

分层结构：
- ``domain.py``  共享领域常量、异常与无状态规则；
- ``storage.py`` 链路存储（SQLite schema + Repository）；
- ``service.py`` 业务处理（筛查、案件、冻结、资金链路调查）；
- ``web.py``     页面/HTTP 接口；``static/index.html`` 为前端页面；
- ``app.py``     仅作启动入口，保留历史导入路径。
"""
from __future__ import annotations

import argparse
import json

from domain import DEFAULT_DB, DomainError
from service import FinancialCrimeService
from web import serve

__all__ = ["DomainError", "FinancialCrimeService", "main", "serve"]


def main() -> None:
    parser = argparse.ArgumentParser(description="金融犯罪调查与制裁筛查服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8208)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    service = FinancialCrimeService(args.db)
    if args.init:
        print(json.dumps(
            service.seed_demo() if args.seed else {"initialized": True, "db": args.db},
            ensure_ascii=False,
        ))
        return
    serve(service, args.host, args.port)


if __name__ == "__main__":
    main()
