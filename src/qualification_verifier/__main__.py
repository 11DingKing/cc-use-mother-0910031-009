"""命令行入口：python -m qualification_verifier --db q.db --port 8080"""
from __future__ import annotations

import argparse

from .api import serve


def main() -> int:
    parser = argparse.ArgumentParser(description="执业资质范围核验后端服务")
    parser.add_argument("--db", default="qualification.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    serve(args.db, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
