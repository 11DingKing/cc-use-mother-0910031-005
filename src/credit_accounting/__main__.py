"""命令行入口：``python -m credit_accounting --db data.db --port 8080``。"""
from __future__ import annotations

import argparse

from .api import make_server
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="车型年度积分核算服务端")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="credit.db", help="SQLite 文件路径，默认 credit.db")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    store = Store(args.db)
    httpd = make_server(args.host, args.port, store, quiet=args.quiet)
    print(f"车型年度积分核算服务已启动：http://{args.host}:{args.port}  数据库={args.db}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()


if __name__ == "__main__":
    main()
