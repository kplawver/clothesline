"""Small command-line interface for service management."""

import argparse
import json
import sqlite3
from pathlib import Path

from clothesline.config import load


def main():
    parser = argparse.ArgumentParser(prog="clothesline")
    parser.add_argument("command", choices=["serve", "status", "doctor", "backup", "rebuild-index"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, help="Backup destination")
    args = parser.parse_args()
    config = load(args.config)
    if args.command == "serve":
        import uvicorn

        from clothesline.app import create_app

        uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1)
    elif args.command == "rebuild-index":
        from clothesline.storage import Store

        if not config.database.exists():
            parser.error("No database exists to reindex")
        count = Store(config.database).rebuild_indexes()
        print(f"Rebuilt keyword search for {count} messages")
    elif args.command == "backup":
        if not args.output or not config.database.exists():
            parser.error("--output and an existing database are required")
        target = sqlite3.connect(args.output)
        args.output.chmod(0o600)
        try:
            with sqlite3.connect(config.database) as source:
                source.backup(target)
        finally:
            target.close()
        print(f"Backup written to {args.output}")
    elif args.command == "status":
        print(json.dumps({"database": str(config.database), "database_exists": config.database.exists(),
                          "address": f"{config.host}:{config.port}"}, indent=2))
    elif args.command == "doctor":
        from clothesline.storage import Store

        store = Store(config.database)
        with store.connect() as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        print(json.dumps({"integrity": integrity}, indent=2))


if __name__ == "__main__":
    main()