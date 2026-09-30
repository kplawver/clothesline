"""Small command-line interface for service management."""

import argparse
import json
import sqlite3
import time
from pathlib import Path

from clothesline.config import load


def main():
    parser = argparse.ArgumentParser(prog="clothesline")
    parser.add_argument("command", choices=["serve", "setup", "status", "doctor", "backup", "rebuild-index", "import-omp"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, help="Backup destination")
    parser.add_argument("--source", type=Path, help="OMP main-session JSONL file to import")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to Clothesline")
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
    elif args.command == "import-omp":
        from clothesline.omp_import import parse_session
        from clothesline.storage import Store

        if not args.source:
            parser.error("import-omp requires --source PATH")
        source = args.source.expanduser().resolve(strict=True)
        parsed = parse_session(source)
        visible = sum(bool(turn.content) for turn in parsed.turns)
        if args.dry_run:
            print(json.dumps({"source": str(source), "project": parsed.project_key,
                              "active_branch_messages": len(parsed.turns), "visible_turns": visible,
                              "reasoning_and_tool_results": "excluded", "database_changed": False}, indent=2))
        elif visible:
            store = Store(config.database)
            harness_id = store.register_harness("clothesline:omp:local-import", "Oh My Pi")
            agent_id = store.register_agent(harness_id, f"omp-session:{parsed.source_id}")
            result = store.import_omp(str(source), parsed, harness_id, agent_id)
            print(json.dumps({"source": str(source), "agent_id": agent_id, **result}, indent=2))
        else:
            print("No visible user or assistant text on this session's active branch")
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
