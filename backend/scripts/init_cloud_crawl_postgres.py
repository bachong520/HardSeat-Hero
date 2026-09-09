from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "cloud_crawl" / "postgres_schema.sql"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize cloud crawl PostgreSQL schema.")
    parser.add_argument(
        "--dsn",
        default=os.getenv("CLOUD_CRAWL_DATABASE_URL"),
        help="PostgreSQL DSN. Defaults to CLOUD_CRAWL_DATABASE_URL.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.dsn:
        raise SystemExit("missing --dsn or CLOUD_CRAWL_DATABASE_URL")

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with psycopg.connect(args.dsn) as conn:
        with conn.cursor() as cursor:
            cursor.execute(schema_sql)
        conn.commit()
    print(f"initialized cloud crawl PostgreSQL schema from {SCHEMA_PATH}")


if __name__ == "__main__":
    main()