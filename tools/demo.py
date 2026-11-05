"""Demo mode: run the whole flow from saved fixtures in the repo's demo/ folder. No API key, no network.

Switched on by DEMO_MODE=1, or automatically when no Brave key is found (see each tool's --help).
The fixtures describe fictional businesses on reserved .example domains, so nothing here is real data.
Every tool says so in its output ("mode": "demo", "data_source", and a note on stderr).

Override the fixture folder with PROCUREMATE_DEMO_DIR.
"""
import json
import os
import urllib.error
from pathlib import Path
from typing import Any, Callable

from common import normalize_domain

DEMO_ENV = "DEMO_MODE"
DEMO_DIR_ENV = "PROCUREMATE_DEMO_DIR"
DEMO_NOTICE = (
    "DEMO MODE: results come from saved fixtures in the repo's demo/ folder "
    "(fictional businesses, no network, not live data)."
)
DEMO_SUMMARY_PREFIX = "DEMO (saved fixtures, not live results): "
_TRUTHY = {"1", "true", "yes", "on"}

SearchFn = Callable[[str], list[dict[str, str]]]


def demo_requested() -> bool:
    return os.environ.get(DEMO_ENV, "").strip().lower() in _TRUTHY


def demo_dir() -> Path:
    override = os.environ.get(DEMO_DIR_ENV, "").strip()
    return Path(override).expanduser() if override else Path(__file__).resolve().parent.parent / "demo"


def demo_available() -> bool:
    return (demo_dir() / "index.json").is_file()


def _load(name: str) -> Any:
    return json.loads((demo_dir() / name).read_text(encoding="utf-8"))


def make_opener() -> Callable[..., tuple[int, str, str, bytes, str]]:
    """A page opener with the same shape as common.default_opener, reading demo/ instead of the network."""
    base = demo_dir()
    pages: dict[str, str] = _load("index.json")["pages"]

    def opener(url: str, timeout: float = 10, max_bytes: int = 0) -> tuple[int, str, str, bytes, str]:
        relative = pages.get(url) or pages.get(url.rstrip("/") + "/")
        if relative is None:  # includes robots.txt: nothing disallowed in the demo
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        return 200, url, "text/html; charset=utf-8", (base / relative).read_bytes(), "utf-8"

    return opener


def make_search() -> SearchFn:
    """A search function that answers from demo/brave_results.json. Quoted queries are the independent-listing checks."""
    data = _load("brave_results.json")

    def search(query: str) -> list[dict[str, str]]:
        lowered = query.lower()
        if query.startswith('"'):
            return next((e["results"] for e in data["independent"] if e["match"].lower() in lowered), [])
        return next((e["results"] for e in data["queries"] if e["match"].lower() in lowered), data["default"])

    return search


def make_rdap() -> Callable[[str], Any]:
    """A get_json stand-in for the RDAP lookup: IANA bootstrap plus per-domain registration dates."""
    data = _load("rdap.json")

    def get_json(url: str) -> Any:
        if url.endswith("dns.json"):
            return data["bootstrap"]
        domain = url.rsplit("/domain/", 1)[-1]
        date = data["domains"].get(domain)
        return {"events": [{"eventAction": "registration", "eventDate": date}] if date else []}

    return get_json


def demo_domains() -> set[str]:
    """Supplier domains that have fixture pages (used to decide whether a domain can run in demo mode)."""
    if not demo_available():
        return set()
    return {normalize_domain(url) for url in _load("index.json")["pages"]}
