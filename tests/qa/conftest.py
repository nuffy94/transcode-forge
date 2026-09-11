"""QA sweep harness — boots the real app in demo-static mode (seeded,
deterministic, no Redis/ffmpeg) through the one boot substrate
(qa/instance.py), so the deterministic sweep and the AI exploratory sweep
share one consistent, populated target with no live box required.

Instances mint their own admin at startup (R-040), so there is no bootstrap
step here; `launch_qa_app` is the reusable launcher for a second instance on
its own port.

Run with:  uv run pytest tests/qa/        (excluded from the default suite)
"""

from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from qa.instance import QA_ADMIN_PASSWORD, launch

QA_PORT = 18799
BASE_URL = f"http://127.0.0.1:{QA_PORT}"
ADMIN_PW = QA_ADMIN_PASSWORD


def launch_qa_app(qa_dir: Path, port: int) -> AbstractContextManager[str]:
    """Boot a demo-static app instance on `port`; yields its base URL."""
    return launch(qa_dir, port)


@pytest.fixture(scope="session")
def qa_base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    qa_dir = tmp_path_factory.mktemp("qa")
    with launch_qa_app(qa_dir, QA_PORT) as base_url:
        yield base_url


@pytest.fixture(scope="session")
def admin_pw() -> str:
    return ADMIN_PW


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    return {**browser_context_args, "viewport": {"width": 1440, "height": 900}}
