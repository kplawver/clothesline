"""Small command-line interface for service management."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from clothesline.config import load
from clothesline.hooks import HARNESSES


def main():
    parser = argparse.ArgumentParser(prog="clothesline")
    parser.add_argument("command", choices=["serve", "status", "doctor", "backup", "rebuild-index", "hook"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, help="Backup destination")
    parser.add_argument("--harness", choices=sorted(HARNESSES), default="claude", help="Harness calling `hook`")
    parser.add_argument("--event", help="Hook event, for harnesses whose payload does not name it")
    args = parser.parse_args()
    config = load(args.config)
    if args.command == "serve":
        import uvicorn

        from clothesline.app import create_app

        uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1)
    elif args.command == "hook":
        from clothesline.hooks import handle

        try:
            output = handle(config, json.load(sys.stdin), args.harness, args.event)
            if output:
                print(json.dumps(output))
        except Exception as error:  # noqa: BLE001 - A presence hook must never interrupt an agent's turn.
            print(f"Clothesline hook skipped ({type(error).__name__})", file=sys.stderr)
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