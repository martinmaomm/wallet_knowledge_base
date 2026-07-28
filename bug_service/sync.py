from __future__ import annotations

import argparse
from pathlib import Path

from .db import BugRepository
from .zentao import ZenTaoClient, ZenTaoConfig


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "bugs.sqlite3"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synchronize all bugs of one ZenTao product to SQLite.")
    parser.add_argument("--env-file", required=True, help="Existing .env containing ZENTAO_* credentials")
    parser.add_argument("--database", default=str(DEFAULT_DB_PATH), help="SQLite output path")
    parser.add_argument("--product-id", type=int, default=9, help="ZenTao product ID")
    parser.add_argument("--product-name", default="内部钱包", help="Fallback product name")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = ZenTaoConfig.from_env_file(
        args.env_file,
        product_id=args.product_id,
        product_name=args.product_name,
    )
    client = ZenTaoClient(config)
    client.login()
    records = client.fetch_records()
    records.sort(key=lambda item: item.bug_id)

    repository = BugRepository(args.database)
    repository.initialize()
    count = repository.replace_all(records)
    print(f"database={Path(args.database).resolve()}")
    print(f"product_id={config.product_id}")
    print(f"bugs={count}")


if __name__ == "__main__":
    main()

