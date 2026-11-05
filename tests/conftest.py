import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeNet:
    """Offline stand-in for common.default_opener: maps URL -> fixture or exception."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float, max_bytes: int = 0):
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None:
            import urllib.error

            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if isinstance(route, BaseException):
            raise route
        body, ctype = route if isinstance(route, tuple) else (route, "text/html; charset=utf-8")
        return 200, url, ctype, body.encode("utf-8"), "utf-8"


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tests never see a developer's real key, secrets file, demo switch or agent folders."""
    for name in ("BRAVE_API_KEY", "DEMO_MODE", "PROCUREMATE_SECRETS_FILE", "PROCUREMATE_AGENT_DIRS", "PROCUREMATE_DEMO_DIR"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PROCUREMATE_CACHE_DIR", str(tmp_path / "pages-cache"))  # tests never touch the real page or RDAP cache
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


@pytest.fixture
def fixtures() -> Any:
    return fixture_text
