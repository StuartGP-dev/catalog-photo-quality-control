from __future__ import annotations

import argparse
import sys

from .catalog_config import load_settings
from .catalog_db import init_db_from_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create/upgrade the shared catalog DB schema.")
    parser.add_argument("--db-dsn", default=None, help="Override CATALOG_DB_DSN for this run.")
    parser.add_argument(
        "--require-postgres",
        action="store_true",
        help="Fail if the configured DB is not PostgreSQL.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(db_dsn=args.db_dsn)
    if args.require_postgres and not settings.is_postgres:
        raise SystemExit(
            "Refusing to initialize a non-PostgreSQL DB. Set CATALOG_DB_DSN=postgresql://..."
        )
    init_db_from_settings(settings)
    print("CATALOG DB READY")
    print(f"backend: {settings.db_backend}")
    print(f"db: {settings.db_dsn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
