"""Worker module entry point — `python -m transcode_forge.worker`.

Connects to the scheduler over HTTP with a server-issued bearer token.
Requires TF_SERVER_URL and TF_WORKER_TOKEN (generate the token in
Settings → Workers). The worker holds no DB or Redis credentials.
"""

import asyncio
import logging
import os
import sys

from transcode_forge.config import get_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()

    server_url = os.environ.get("TF_SERVER_URL", "").strip()
    token = os.environ.get("TF_WORKER_TOKEN", "").strip()
    if not server_url or not token:
        print(
            "TF_SERVER_URL and TF_WORKER_TOKEN are required "
            "(generate a token in Settings → Workers).",
            file=sys.stderr,
        )
        sys.exit(2)

    from transcode_forge.worker.http_agent import HttpWorkerAgent

    agent = HttpWorkerAgent(settings, server_url=server_url, token=token)
    asyncio.run(agent.start())


if __name__ == "__main__":
    main()
