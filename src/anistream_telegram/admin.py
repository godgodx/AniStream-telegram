from __future__ import annotations

import argparse
import asyncio

from anistream_telegram.config import Config
from anistream_telegram.database import Database


async def run(command: str, user_id: int | None) -> int:
    config = Config.from_env(require_secrets=False)
    database = Database(config.database_url)
    try:
        await database.initialize()
        if command == "allow" and user_id is not None:
            await database.set_allowed(user_id, True)
            print(f"Allowed Telegram user {user_id}")
        elif command == "deny" and user_id is not None:
            await database.set_allowed(user_id, False)
            print(f"Denied Telegram user {user_id}")
        elif command == "list":
            for value in await database.list_allowed():
                print(value)
        elif command == "init":
            print("Database initialized")
        else:
            return 2
        return 0
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage AniStream Telegram access")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("allow", "deny"):
        child = subparsers.add_parser(name)
        child.add_argument("user_id", type=int)
    subparsers.add_parser("list")
    subparsers.add_parser("init")
    args = parser.parse_args()
    if args.command in {"allow", "deny"} and args.user_id <= 0:
        parser.error("user_id must be positive")
    raise SystemExit(asyncio.run(run(args.command, getattr(args, "user_id", None))))


if __name__ == "__main__":
    main()
