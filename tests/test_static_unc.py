r"""The public static mount never hands a UNC path to the filesystem.

Starlette below 1.1.0 joined a URL-encoded ``\\host\share`` request onto
the static directory and resolved it with ``os.path.realpath`` before the
containment check, which on Windows opens an SMB connection to ``host`` and
offers NTLM credentials (GHSA-wqp7-x3pw-xc5r). The mount is public (no
session), so the check belongs here, on the app, not on the library version.

Linux ``realpath`` treats the same bytes as an ordinary file name, so the
test is meaningful only on Windows; it is skipped elsewhere rather than
asserting something the platform cannot violate.
"""

from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="UNC resolution is a Windows path")

UNC_TARGET = r"\\fake-review.invalid\share\missing.txt"
ENCODED = "%5C%5Cfake-review.invalid%5Cshare%5Cmissing.txt"


async def test_unc_request_never_reaches_realpath(unauthed_client: AsyncClient) -> None:
    seen: list[str] = []
    real = os.path.realpath

    def spy(path: Any, *args: Any, **kwargs: Any) -> Any:
        text = os.fspath(path)
        seen.append(text)
        if text.startswith("\\\\"):
            # Never resolve a UNC path for real: that is the network call
            # this test exists to forbid. Answer with a local path on the
            # repo's drive so the containment check runs and refuses it.
            return os.path.join(real(os.getcwd()), "unc-blocked")
        return real(path, *args, **kwargs)

    with patch("os.path.realpath", side_effect=spy):
        resp = await unauthed_client.get(f"/static/{ENCODED}")

    assert resp.status_code == 404
    unc = [p for p in seen if p.startswith("\\\\") or UNC_TARGET in p]
    assert unc == [], f"UNC path reached the filesystem resolver: {unc}"
