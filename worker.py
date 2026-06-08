"""Worker entrypoint — run on each transcode node.

Usage:
    # Linux (systemd or manual):
    python worker.py --name worker-1

    # With specific encoder:
    python worker.py --name gpu-node --encoder nvenc

    # Windows (.bat worker script):
    uv run python worker.py --name gpu-node --encoder nvenc
"""

import argparse
import asyncio
import sys

from transcode_forge.config import Settings
from transcode_forge.worker.agent import WorkerAgent


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcode Forge Worker")
    parser.add_argument("--name", required=True, help="Worker name (e.g. worker-1)")
    parser.add_argument(
        "--encoder", default="auto",
        choices=["auto", "qsv", "nvenc", "cpu"],
        help="Preferred encoder (default: auto-detect)",
    )
    parser.add_argument("--max-concurrent", type=int, default=1, help="Max concurrent jobs")
    parser.add_argument("--redis-url", default=None, help="Override Redis URL")
    parser.add_argument(
        "--db-url", default=None,
        help="Database URL (postgresql://... or sqlite:///path)",
    )
    parser.add_argument("--db-path", default=None, help="(deprecated) DB path")

    args = parser.parse_args()

    # Build settings with CLI overrides
    overrides: dict = {
        "worker_name": args.name,
        "preferred_encoder": args.encoder,
        "worker_max_concurrent": args.max_concurrent,
    }
    if args.redis_url:
        overrides["redis_url"] = args.redis_url
    if args.db_url:
        overrides["db_url"] = args.db_url
    elif args.db_path:
        overrides["db_path"] = args.db_path

    settings = Settings(**overrides)
    agent = WorkerAgent(settings)

    try:
        asyncio.run(agent.start())
    except KeyboardInterrupt:
        print("\nShutdown complete.")
        sys.exit(0)


if __name__ == "__main__":
    main()
