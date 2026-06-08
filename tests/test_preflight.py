"""Tests for startup preflight checks."""

from transcode_forge.config import Settings
from transcode_forge.preflight import run_preflight


def test_missing_library_path_is_critical(tmp_path):
    settings = Settings(library_movies=str(tmp_path / "does-not-exist"))
    issues = run_preflight(settings)
    codes = {i["code"] for i in issues}
    assert "library_movies_missing" in codes
    assert any(i["level"] == "critical" for i in issues)


def test_valid_library_path_has_no_library_issue(tmp_path):
    settings = Settings(library_movies=str(tmp_path))
    issues = run_preflight(settings)
    assert not any(i["code"].startswith("library_") for i in issues)


def test_no_libraries_warns():
    settings = Settings(library_movies="", library_tv="", library_anime="")
    issues = run_preflight(settings)
    assert any(i["code"] == "no_libraries" for i in issues)
