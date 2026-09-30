"""Small command-line interface for service management."""

import argparse
import json
import sqlite3
import time
from pathlib import Path

from clothesline.config import load


def main():
    parser = argparse.ArgumentParser(prog="clothesline")
    parser.add_argument("command", choices=["serve", "setup", "status", "doctor", "backup", "rebuild-index"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, help="Backup destination")
    args = parser.parse_args()
    config = load(args.config)
    if args.command == "serve":
        import uvicorn

        from clothesline.app import create_app

        uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1)
    elif args.command == "setup":
        from clothesline.models import LocalModels

        LocalModels(config.model_dir).install()
        if config.database.exists():
            from clothesline.storage import Store

            with Store(config.database).connect() as db:
                db.execute("UPDATE jobs SET next_at=?,error=NULL WHERE kind IN ('embed','archive')", (time.time(),))
        print("Local models installed; pending jobs are ready to retry")
    elif args.command == "rebuild-index":
        from clothesline.storage import Store

        if not config.database.exists():
            parser.error("No database exists to reindex")
        count = Store(config.database).rebuild_indexes()
        print(f"Rebuilt keyword search for {count} chunks; queued embedding refresh")
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
        from clothesline.models import LocalModels

        print(json.dumps({"database": str(config.database), "database_exists": config.database.exists(),
                          "embedding_cache_exists": (LocalModels(config.model_dir).embedding_path / "model_optimized.onnx").exists(),
                          "text_model_exists": LocalModels(config.model_dir).text_path.exists(),
                          "address": f"{config.host}:{config.port}"}, indent=2))
    elif args.command == "doctor":
        from clothesline.storage import Store

        store = Store(config.database)
        with store.connect() as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            jobs = [dict(row) for row in db.execute("SELECT kind,source_id,attempts,error FROM jobs WHERE error IS NOT NULL")]
        print(json.dumps({"integrity": integrity, "failed_jobs": jobs}, indent=2))


if __name__ == "__main__":
    main()
