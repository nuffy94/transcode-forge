"""QA sweep harness — boots the real app in demo-static mode (seeded,
deterministic, no Redis/ffmpeg) through the one boot substrate
(qa/instance.py) and completes first-run setup, so the deterministic sweep
and the AI exploratory sweep share one consistent, populated target with no
live box required.

`launch_qa_app` is the reusable launcher: test_setup_flow.py uses it with
`create_admin=False` on its own port to exercise the real first-run /setup.

Run with:  uv run pytest tests/qa/        (excluded from the default suite)
"""

from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from qa.instance import launch

QA_PORT = 18799
BASE_URL = f"http://127.0.0.1:{QA_PORT}"
ADMIN_PW = "qa-sweep-password-123"


def launch_qa_app(qa_dir: Path, port: int, *, create_admin: bool) -> AbstractContextManager[str]:
    """Boot a demo-static app instance on `port`; yields its base URL.

    create_admin=True completes first-run setup with ADMIN_PW (the normal
    sweep target). create_admin=False leaves the instance fresh so /setup
    itself can be exercised.
    """
    return launch(qa_dir, port, admin_password=ADMIN_PW if create_admin else None)


@pytest.fixture(scope="session")
def qa_base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    qa_dir = tmp_path_factory.mktemp("qa")
    with launch_qa_app(qa_dir, QA_PORT, create_admin=True) as base_url:
        yield base_url


@pytest.fixture(scope="session")
def admin_pw() -> str:
    return ADMIN_PW


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    return {**browser_context_args, "viewport": {"width": 1440, "height": 900}}
