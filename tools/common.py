"""Shared helpers for the ProcureMate tools: time, domains, secrets, polite fetching, HTML parsing.

Standard library only. Nothing here ever prints or returns a secret value in an error message.
"""
import hashlib
import json
import os
import re
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from html.parser import HTMLParser

USER_AGENT = "ProcureMateScout/0.1 (supplier research; respects robots.txt)"
ROBOTS_TOKEN = "ProcureMateScout"
SECRETS_FILE_ENV = "PROCUREMATE_SECRETS_FILE"
MAX_BYTES = 1_000_000
DEFAULT_TIMEOUT = 10

# Explicit fetch states
OK = "OK"
BLOCKED_ROBOTS = "BLOCKED_ROBOTS"
ROBOTS_UNAVAILABLE = "ROBOTS_UNAVAILABLE"
TIMEOUT = "TIMEOUT"
HTTP_ERROR = "HTTP_ERROR"
NETWORK_ERROR = "NETWORK_ERROR"
NOT_HTML = "NOT_HTML"
INVALID_URL = "INVALID_URL"
PAGE_LIMIT_REACHED = "PAGE_LIMIT_REACHED"


class SecretError(Exception):
    """Raised when a secret cannot be loaded. The message never contains the secret."""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_domain(url_or_host):
    """Lower-case host without port, trailing dot or leading 'www.'. '' if none."""
    text = (url_or_host or "").strip().lower()
    if not text:
        return ""
    if "//" not in text:
        text = "//" + text
    host = (urllib.parse.urlsplit(text).hostname or "").rstrip(".")
    return host[4:] if host.startswith("www.") else host


def site_origin(url):
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme if parts.scheme in ("http", "https") else "https"
    return f"{scheme}://{parts.hostname or ''}"


def default_secrets_path() -> Path:
    """~/.procuremate/secrets.env, or the file named by PROCUREMATE_SECRETS_FILE."""
    override = os.environ.get(SECRETS_FILE_ENV, "").strip()
    return Path(override).expanduser() if override else Path.home() / ".procuremate" / "secrets.env"


def load_secret(name: str, path: "str | os.PathLike[str] | None" = None) -> str:
    """Return a secret. Raises SecretError without echoing values.

    With no path: the environment variable NAME wins, then the default secrets file (NAME=value lines).
    With an explicit path: only that file is read.
    """
    if path is None:
        from_env = os.environ.get(name, "").strip()
        if from_env:
            return from_env
        path = default_secrets_path()
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.strip().partition("=")
                if key == name and value.strip():
                    return value.strip().strip("\"'")
    except OSError:
        raise SecretError(f"{name} not found in the environment, and the secrets file is missing or unreadable") from None
    raise SecretError(f"{name} not set in the environment or the secrets file")


RETRY_WAITS = (5, 15)  # seconds before the 1st and 2nd retry of a DNS failure or timeout (our network, not a verdict on the site)
MIN_DOMAIN_INTERVAL = 2.0  # seconds between two visits to the same site, so it does not answer 429
CACHE_HOURS = 24
DNS_CHECK_HOSTS = ("example.com", "www.wikipedia.org", "www.cloudflare.com")
TOTAL_REQUEST_SECONDS = 25  # a slow-drip server must not hold one request open indefinitely


def default_opener(url, timeout, max_bytes=MAX_BYTES):
    """Fetch url -> (status, final_url, content_type, raw_bytes, charset). May raise urllib errors.

    `timeout` limits each network wait; the whole request (headers and body) is also capped at
    TOTAL_REQUEST_SECONDS, so a server that sends a byte at a time cannot stall a run.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.5",
        },
    )
    deadline = time.monotonic() + TOTAL_REQUEST_SECONDS
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        chunks, size = [], 0
        while size <= max_bytes:
            chunk = resp.read(16384)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if time.monotonic() > deadline:
                raise TimeoutError("the request took longer than the total time allowed")
        return (
            resp.status,
            resp.geturl(),
            resp.headers.get("Content-Type", ""),
            b"".join(chunks)[:max_bytes],
            resp.headers.get_content_charset() or "utf-8",
        )


def robots_rule(robots_text: str, url: str, token: str = ROBOTS_TOKEN) -> str:
    """The Disallow rule (longest match) that applies to url for our crawler, as 'Disallow: /path'. '' if none."""
    path = urllib.parse.urlsplit(url).path or "/"
    agents: list[str] = []
    in_rules = False
    best = ""
    for raw in robots_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value.lower())
        elif field in ("disallow", "allow"):
            in_rules = True
            applies = any(a == "*" or a in token.lower() for a in agents)
            if field == "disallow" and applies and value:
                pattern = re.escape(value).replace(r"\*", ".*").replace(r"\$", "$")
                if re.match(pattern, path) and len(value) > len(best):
                    best = value
    return f"Disallow: {best}" if best else ""


def explain_failure(state, status=None, detail="", retry_after=None, rule=""):
    """A plain-words reason a page could not be checked (kept in the output next to the state)."""
    if state == BLOCKED_ROBOTS:
        return f"robots.txt asks crawlers not to fetch this page ({rule or 'a Disallow rule applies'}), so it was not fetched."
    if state == ROBOTS_UNAVAILABLE:
        return f"robots.txt could not be read ({detail}); the standard says to treat that as 'do not fetch', so the page was not fetched."
    if state == HTTP_ERROR:
        if status == 429:
            wait = f"; it asks to wait {retry_after} s" if retry_after else ""
            return f"the site is rate-limiting this address (HTTP 429 Too Many Requests{wait}). Repeated visits are the likely cause; try again later."
        if status == 403:
            return "the site refused the request (HTTP 403). This is often bot protection; the tool does not try to get around it."
        if status == 404:
            return "page not found (HTTP 404)."
        if status and status >= 500:
            return f"the site had a server error (HTTP {status})."
        return f"the site answered HTTP {status}."
    if state == TIMEOUT:
        return "the site did not answer in time."
    if state == NETWORK_ERROR:
        return f"could not reach the site ({detail}). This is a connection or DNS problem, not a block."
    if state == NOT_HTML:
        return f"the address is not a web page ({detail})."
    if state == PAGE_LIMIT_REACHED:
        return "the page limit for this site was reached."
    return detail or state


def _network_failure(err):
    """(state, detail) for an exception raised while fetching."""
    reason = getattr(err, "reason", err)
    if isinstance(err, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError)):
        return TIMEOUT, "timed out"
    if isinstance(reason, socket.gaierror):
        return NETWORK_ERROR, "DNS lookup failed"
    return NETWORK_ERROR, type(reason).__name__


def _result(state, url, **extra):
    return {"state": state, "url": url, "retrieved_at": now_iso(), **extra}


def default_cache_dir():
    """Where fetched pages are cached for a day: outside any synced folder and outside any git checkout."""
    override = os.environ.get("PROCUREMATE_CACHE_DIR")
    base = Path(override) if override else Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "procuremate-ai" / "pages"
    if not override and (any(part.lower().startswith("one" + "drive") for part in base.parts) or any((parent / ".git").exists() for parent in [base, *base.parents])):
        base = Path(tempfile.gettempdir()) / "procuremate-ai" / "pages"
    return base


class PageCache:
    """Pages fetched successfully in the last CACHE_HOURS hours, kept on disk so a rerun does not visit the site again."""

    def __init__(self, directory=None, hours=CACHE_HOURS, clock=time.time):
        self._dir = Path(directory) if directory else default_cache_dir()
        self._seconds = hours * 3600
        self._clock = clock

    def _path(self, url):
        return self._dir / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".json")

    def get(self, url):
        try:
            entry = json.loads(self._path(url).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if self._clock() - entry.get("saved_at", 0) > self._seconds:
            return None
        return entry["result"]

    def put(self, url, result):
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path(url).write_text(json.dumps({"saved_at": self._clock(), "result": result}), encoding="utf-8")
        except OSError:
            pass  # a cache that cannot be written is only a missed saving


def dns_check(hosts=DNS_CHECK_HOSTS, resolver=socket.getaddrinfo):
    """Resolve a few well-known hosts (no page is fetched). Returns {"failed": [...], "warning": str or None}."""
    failed = []
    for host in hosts:
        try:
            resolver(host, 443)
        except OSError:
            failed.append(host)
    warning = None
    if failed:
        warning = (f"DNS looks flaky on this computer ({len(failed)} of {len(hosts)} test lookups failed: {', '.join(failed)}). "
                   "Sites that cannot be reached will be retried; that is a network problem on our side, not a verdict on the sites.")
    return {"failed": failed, "checked": list(hosts), "warning": warning}


class Fetcher:
    """Polite fetcher: robots.txt respected, timeouts, a hard page budget, explicit failure states with reasons."""

    def __init__(self, opener=default_opener, timeout=DEFAULT_TIMEOUT, max_pages=5, retry_waits=(), min_domain_interval=0.0,
                 cache=None, sleep=time.sleep, clock=time.monotonic):
        self._opener = opener
        self._retry_waits = tuple(retry_waits)
        self._min_interval = min_domain_interval
        self._cache = cache
        self._sleep = sleep
        self._clock = clock
        self._last_visit = {}
        self._timeout = timeout
        self._max_pages = max_pages
        self._pages_used = 0
        self._robots = {}

    @property
    def pages_used(self):
        return self._pages_used

    def _robots_for(self, url):
        """(parser, failure_state, detail, robots_text) for the site's robots.txt, fetched once per site."""
        origin = site_origin(url)
        if origin in self._robots:
            return self._robots[origin]
        parser = urllib.robotparser.RobotFileParser()
        try:
            status, _, _, raw, charset = self._open_with_retries(origin + "/robots.txt")
            text = raw.decode(charset, "replace")
            parser.parse(text.splitlines())
            verdict = (parser, None, "", text)
        except urllib.error.HTTPError as err:
            # 4xx: no robots rules apply. 5xx: treat as fully disallowed (RFC 9309).
            verdict = (None, None, "", "") if 400 <= err.code < 500 else (None, ROBOTS_UNAVAILABLE, f"HTTP {err.code}", "")
        except (urllib.error.URLError, OSError, ValueError) as err:
            state, detail = _network_failure(err)
            verdict = (None, state, detail, "")  # the site itself is unreachable: not a robots block
        self._robots[origin] = verdict
        return verdict

    def robots_state(self, url):
        """None if allowed, else BLOCKED_ROBOTS, ROBOTS_UNAVAILABLE (server error) or NETWORK_ERROR / TIMEOUT."""
        parser, failure, _, _ = self._robots_for(url)
        if failure:
            return failure
        if parser is not None and not parser.can_fetch(ROBOTS_TOKEN, url):
            return BLOCKED_ROBOTS
        return None

    def fetch(self, url):
        """Return a dict with state, url, retrieved_at, a plain-words `why` on failure and, when OK, status/final_url/html."""
        if urllib.parse.urlsplit(url).scheme not in ("http", "https") or not normalize_domain(url):
            return _result(INVALID_URL, url, error="not an http(s) URL", why="not an http(s) address")
        if self._pages_used >= self._max_pages:
            return _result(PAGE_LIMIT_REACHED, url, error="page limit reached", why=explain_failure(PAGE_LIMIT_REACHED))
        cached = self._cache.get(url) if self._cache else None
        if cached:
            return {**cached, "from_cache": True}
        parser, failure, detail, text = self._robots_for(url)
        if failure:
            return _result(failure, url, error=detail or "robots.txt not readable", why=explain_failure(failure, detail=detail))
        if parser is not None and not parser.can_fetch(ROBOTS_TOKEN, url):
            rule = robots_rule(text, url)
            return _result(BLOCKED_ROBOTS, url, error="robots.txt does not allow fetching", robots_rule=rule, why=explain_failure(BLOCKED_ROBOTS, rule=rule))
        self._pages_used += 1
        try:
            status, final_url, ctype, raw, charset = self._open_with_retries(url)
        except urllib.error.HTTPError as err:
            retry = (getattr(err, "headers", None) or {}).get("Retry-After")
            return _result(HTTP_ERROR, url, status=err.code, error=f"HTTP {err.code}", retry_after=retry, why=explain_failure(HTTP_ERROR, err.code, retry_after=retry))
        except (socket.timeout, TimeoutError, urllib.error.URLError, OSError, ValueError) as err:
            state, detail = _network_failure(err)
            return _result(state, url, error=detail, why=explain_failure(state, detail=detail))
        if "html" not in ctype.lower() and "xml" not in ctype.lower() and "text/plain" not in ctype.lower():
            detail = f"content type {ctype or 'unknown'}"
            return _result(NOT_HTML, url, status=status, error=detail, why=explain_failure(NOT_HTML, detail=detail))
        result = _result(OK, url, status=status, final_url=final_url, html=raw.decode(charset, "replace"))
        if self._cache:
            self._cache.put(url, result)
        return result

    def _space_out(self, url):
        origin = site_origin(url)
        wait = self._last_visit.get(origin, -1e9) + self._min_interval - self._clock()
        if wait > 0:
            self._sleep(wait)
        self._last_visit[origin] = self._clock()

    def _open_with_retries(self, url):
        """Open url; a DNS failure or timeout is retried after each wait in retry_waits (it says nothing about the site)."""
        waits = list(self._retry_waits)
        while True:
            self._space_out(url)
            try:
                return self._opener(url, self._timeout)
            except urllib.error.HTTPError:
                raise
            except (socket.timeout, TimeoutError, urllib.error.URLError, OSError, ValueError) as err:
                if not waits or _network_failure(err)[0] not in (NETWORK_ERROR, TIMEOUT) or isinstance(err, ValueError):
                    raise
                self._sleep(waits.pop(0))


def polite_fetcher(max_pages, cache=True):
    """The fetcher live runs use: retries after network errors, spaced visits per site, a 24-hour page cache."""
    return Fetcher(max_pages=max_pages, retry_waits=RETRY_WAITS, min_domain_interval=MIN_DOMAIN_INTERVAL,
                   cache=PageCache() if cache else None)


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

_SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "nav", "ul", "ol", "table", "address",
}


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta = {}
        self.links = []
        self.jsonld = []
        self.address_blocks = []
        self._lines = []
        self._current = []
        self._skip = 0
        self._in_title = False
        self._ld_buffer = None
        self._href = None
        self._anchor = []
        self._in_address = 0
        self._address = []
        self.headings = []
        self._in_heading = False
        self._heading = []
        self.forms = 0
        self.scripts = 0
        self.cf_emails = []

    def _flush(self):
        line = re.sub(r"\s+", " ", "".join(self._current)).strip()
        if line:
            self._lines.append(line)
        self._current = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.scripts += 1
        elif tag == "form":
            self.forms += 1
        if attrs.get("data-cfemail"):
            decoded = decode_cf_email(attrs["data-cfemail"])
            if decoded:
                self.cf_emails.append(decoded)
        if tag == "script" and "ld+json" in (attrs.get("type") or ""):
            self._ld_buffer = []
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if tag in _BLOCK_TAGS:
            self._flush()
        else:
            self._current.append(" ")  # inline tags must not glue neighbouring text together
        if tag in ("h1", "h2", "h3", "h4"):
            self._in_heading, self._heading = True, []
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            if key and attrs.get("content"):
                self.meta[key.lower()] = attrs["content"].strip()
        elif tag == "a" and attrs.get("href"):
            self._href, self._anchor = attrs["href"].strip(), []
        elif tag == "address":
            self._in_address += 1
            self._address = []

    def handle_endtag(self, tag):
        if tag == "script" and self._ld_buffer is not None:
            try:
                self.jsonld.append(json.loads("".join(self._ld_buffer)))
            except ValueError:
                pass
            self._ld_buffer = None
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag in _BLOCK_TAGS:
            self._flush()
        else:
            self._current.append(" ")
        if tag in ("h1", "h2", "h3", "h4") and self._in_heading:
            text = re.sub(r"\s+", " ", " ".join(self._heading)).strip()
            if text:
                self.headings.append(text)
            self._in_heading = False
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", " ".join(self._anchor)).strip()
            self.links.append((self._href, text))
            self._href = None
        elif tag == "address" and self._in_address:
            self._in_address -= 1
            text = re.sub(r"\s+", " ", " ".join(self._address)).strip()
            if text:
                self.address_blocks.append(text)

    def handle_data(self, data):
        if self._ld_buffer is not None:
            self._ld_buffer.append(data)
        if self._skip:
            return
        if self._in_heading:
            self._heading.append(data)
        if self._in_title:
            self.title += data
        if self._href is not None:
            self._anchor.append(data)
        if self._in_address:
            self._address.append(data)
        self._current.append(data)


def decode_cf_email(hex_text: str) -> str:
    """Cloudflare's email protection stores an address as hex XOR-ed with its first byte; decode it."""
    try:
        data = bytes.fromhex(hex_text)
        key = data[0]
        return bytes(b ^ key for b in data[1:]).decode("utf-8")
    except (ValueError, IndexError, UnicodeDecodeError):
        return ""


def parse_html(html):
    """Return dict: title, meta, links[(href, text)], jsonld[], address_blocks[], lines[]."""
    parser = _PageParser()
    parser.feed(html or "")
    parser.close()
    parser._flush()
    return {
        "title": re.sub(r"\s+", " ", parser.title).strip(),
        "meta": parser.meta,
        "links": parser.links,
        "jsonld": parser.jsonld,
        "address_blocks": parser.address_blocks,
        "headings": parser.headings,
        "lines": parser._lines,
        "forms": parser.forms,
        "scripts": parser.scripts,
        "cf_emails": parser.cf_emails,
    }


def resolve_link(base_url, href):
    """Absolute http(s) URL without fragment, or None for mailto/tel/javascript/etc."""
    href = (href or "").strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    absolute = urllib.parse.urljoin(base_url, href)
    parts = urllib.parse.urlsplit(absolute)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return urllib.parse.urlunsplit(parts._replace(fragment=""))


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------

# Prices as written in a snippet or page line: naira (symbol, NGN, N) or dollar. Never computed or guessed.
PRICE_RE = re.compile(
    r"(?:₦|(?<![A-Za-z])NGN\s?|(?<![A-Za-z])N(?=\d)|(?<![A-Za-z])USD\s?|\$)\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?",
    re.IGNORECASE,
)
MAX_PRICES_PER_RESULT = 3


def find_prices(text: str) -> list[str]:
    """Price strings in a text (at least 4 digits for naira, 3 for dollars, to skip stray numbers)."""
    found: list[str] = []
    for match in PRICE_RE.finditer(text):
        digits = len(re.sub(r"\D", "", match.group()))
        needed = 3 if match.group().lstrip().startswith(("$", "U", "u")) else 4
        if digits >= needed and match.group().strip() not in found:
            found.append(match.group().strip())
    return found[:MAX_PRICES_PER_RESULT]


def currency_of(price_text: str) -> str:
    return "USD" if price_text.lstrip().startswith(("$", "U", "u")) else "NGN"


# ---------------------------------------------------------------------------
# Run summary (printed at the end of a tool run and stored in its JSON; not for chat)
# ---------------------------------------------------------------------------

def format_run_summary(title: str, rows: list[tuple[str, str]]) -> str:
    width = max(len(label) for label, _ in rows)
    return "\n".join([f"Run summary ({title})"] + [f"  {label.ljust(width)}  {value}" for label, value in rows])


def seconds_text(stages: dict[str, float]) -> str:
    return " | ".join(f"{name.replace('_', ' ')} {secs:.1f}" for name, secs in stages.items())
