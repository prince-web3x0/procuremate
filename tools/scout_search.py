"""Scout search: Brave queries (generic + local) -> classified, ranked, deduplicated own-site suppliers as JSON.

Usage:
    python scout_search.py --product "office chairs" --location Abuja [--target 12] [--out out/search.json]

The Brave key comes from the BRAVE_API_KEY environment variable, or from ~/.procuremate/secrets.env
(override the file with PROCUREMATE_SECRETS_FILE). It is never printed.
Demo mode (DEMO_MODE=1, or automatic when no key is found) runs the same code on saved fixtures in demo/:
no key, no network, and the output says so.
At most MAX_TOTAL_QUERIES (12) Brave queries per task, paced, one retry on HTTP 429; the count used is
printed at the end of every run. Marketplace prices already visible in result snippets are kept apart as
price_benchmarks (never supplier prices); marketplace pages are not fetched.
Every source record has: value, source_url, retrieved_at, query, rank, snippet.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from delivery import delivery_label
from budget import Budget, QueryCache, guarded, print_line as print_budget
from progress import ProgressFile, progress_path
from replay import load_bundle, notice, replay_path, replay_search, replay_speed
from relevance import is_known_overseas, search_text_is_nigerian
from regions import CITY_PROFILES, location_terms, profile_for as _profile, regions_in  # noqa: F401
from site_check import product_terms, search_fit_evidence
from demo import (
    DEMO_NOTICE,
    DEMO_SUMMARY_PREFIX,
    demo_available,
    demo_requested,
    make_opener,
    make_search,
)
from common import (
    dns_check,
    polite_fetcher,
    OK,
    Fetcher,
    SecretError,
    find_prices,
    format_run_summary,
    load_secret,
    normalize_domain,
    now_iso,
    parse_html,
    resolve_link,
    seconds_text,
    site_origin,
)

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_TOTAL_QUERIES = 12
RESOLUTION_RESERVE = 3  # of the total, kept for "<business> <location>" lookups
MIN_QUERY_INTERVAL = 1.1  # seconds between Brave calls (or between pairs of calls)
PARALLEL_WORKERS = 2  # queries run together only when the plan's own per-second limit is at least this
MAX_NEARBY_QUERIES = 3
RETRY_WAIT = 2.0
MAX_RETRY_WAIT = 5.0
DEFAULT_TARGET = 12
LOCAL_TARGET = 4
DEFAULT_PAGE_FETCHES = 6
MAX_CANDIDATES_PER_PAGE = 25
MAX_NAMED_PER_PAGE = 15
SOCIAL_ONLY_STATUS = "social-only lead - cannot be checked automatically"
LISTING_STATUS = "directory, listing or article page - a lead, not a supplier"
PRICE_BENCHMARK_LABEL = "listed price (marketplace, not a supplier quote)"

# Marketplaces plus B2B portals / trade platforms: leads, never a supplier's own site.
MARKETPLACE_DOMAINS = {
    "jumia.com.ng", "jiji.ng", "konga.com", "olx.com.ng", "amazon.com", "alibaba.com",
    "aliexpress.com", "ebay.com", "temu.com", "etsy.com", "made-in-china.com", "tradeindia.com",
    "exporthub.com", "archiproducts.com", "globalsources.com", "ec21.com", "indiamart.com",
    "tradekey.com", "dhgate.com", "europages.com", "kompass.com", "thomasnet.com",
    "demo-marketplace.example",  # used only by the demo fixtures
}
DIRECTORY_DOMAINS = {
    "nigeriayellowpages.com", "yellowpages.ng", "businesslist.com.ng", "finelib.com",
    "vconnect.com", "ngex.com", "cylex.com.ng", "infoisinfo.ng", "ong.ng", "companylist.org", "businesslist.ng",
}
# People and contact databases, property or real-estate sites, businesses for sale: never suppliers.
CONTACT_DB_DOMAINS = {"rocketreach.co", "zoominfo.com", "signalhire.com", "contactout.com", "apollo.io", "lusha.com", "hunter.io", "clearbit.com"}
PROPERTY_DOMAINS = {"naijahouses.com", "propertypro.ng", "nigeriapropertycentre.com", "privateproperty.ng"}
BUSINESS_FOR_SALE_DOMAINS = {"bizbuysell.com", "businessesforsale.com", "flippa.com", "bizquest.com"}
# Social platforms: classified but never fetched or resolved.
SOCIAL_DOMAINS = {
    "facebook.com", "instagram.com", "x.com", "twitter.com", "linkedin.com", "tiktok.com",
    "youtube.com", "pinterest.com",
    "demo-social.example",  # used only by the demo fixtures
}
# Never treated as a supplier's own website when resolving candidates.
NON_SUPPLIER_DOMAINS = {
    "wikipedia.org", "reddit.com", "quora.com", "google.com", "bing.com", "brave.com",
    "medium.com", "wordpress.com", "blogspot.com", "apple.com", "play.google.com",
    "mypricespot.com.ng",  # price-comparison and "top suppliers" articles
}
ARTICLE_HINT = re.compile(
    r"\b(best|top[ -]?\d+|guide|how to|reviews?|vs|list of|blog|news)\b", re.IGNORECASE
)
DIRECTORY_HINT = re.compile(r"directory|yellow-?pages|listings|business-?list", re.IGNORECASE)
# Listing pages are recognised from their title/snippet too, not only from known domains.
DIRECTORY_TEXT = re.compile(r"\bdirectory\b|\bbusiness listings?\b|\bcompany listings?\b|\blistings of\b|\byellow ?pages\b", re.IGNORECASE)
LISTICLE = re.compile(
    r"\b(?:top|best)\s+(?:[\w'&]+\s+){0,6}?(?:suppliers|companies|stores|shops|dealers|manufacturers|vendors|brands|sellers|businesses)\b"
    r"|\b\d+\s+(?:best|top)\b|\b(?:top|best)\s+\d+\b|\blist of\b|\bfind (?:the )?(?:best|top)\b",
    re.IGNORECASE,
)
CONTACT_DB_TEXT = re.compile(r"\bemail finder\b|\bcontact database\b|\bpeople search\b|\bfind (?:anyone'?s? )?(?:email|phone)\b|\bphone numbers? (?:and|&) emails?\b|\blead (?:database|intelligence)\b", re.IGNORECASE)
PROPERTY_TEXT = re.compile(r"\breal estate\b|\bproperties\b|\bproperty (?:for|listings?)\b|\bhouses? for (?:sale|rent)\b|\bapartments? for\b|\bto let\b|\bestate agents?\b", re.IGNORECASE)
BUSINESS_FOR_SALE_TEXT = re.compile(r"\bbusiness(?:es)? for sale\b|\bbuy (?:an? )?(?:existing )?business\b|\bsell your business\b", re.IGNORECASE)
COMPANY_LIST_TEXT = re.compile(r"\bcompany lists?\b|\blist of companies\b|\bcompanies list\b", re.IGNORECASE)
LISTING_CLASSES = ("directory", "article", "contact_database", "property_listing", "business_for_sale")
COMPANY_SUFFIX = re.compile(r"\b(?:Ltd|Limited|Enterprises?|Ventures)\b")
PHONE_IN_RESULT = re.compile(r"(?:\+?234|0)[\s-]?[789][01]\d[\s-]?\d{3}[\s-]?\d{4}")
MANY_BUSINESSES = 3  # this many company names or phone numbers in one result means a list of businesses
GENERIC_ANCHOR = re.compile(
    r"^(website|visit( website| site)?|click here|read more|more|www\..*|https?://.*|home|link)$",
    re.IGNORECASE,
)
FILE_SUFFIX = re.compile(r"\.(pdf|jpe?g|png|gif|webp|zip|docx?|xlsx?)$", re.IGNORECASE)
FURNITURE_WORDS = re.compile(r"chair|desk|table|furniture|cabinet|sofa|shelf|shelving", re.IGNORECASE)

DELIVERY_CONTEXT = r"(?:deliver\w*|distribut\w*|ship\w*|nationwide|available in|across)"

# Directory/article names that are not businesses (headings like "Conclusion", "How to choose").
NAME_REJECT = re.compile(
    r"^(how|what|why|where|when|which|best|top|conclusion|introduction|faq|frequently|read more|contact|about|"
    r"home|menu|search|related|share|comments?|summary|tips|buying guide|things to|final)\b",
    re.IGNORECASE,
)
PROFILE_HREF = re.compile(r"/(profile|company|companies|business|listing|listings|biz|vendor|supplier|store|seller|shop)s?/", re.IGNORECASE)
GENERIC_NAME_WORDS = {
    "limited", "ltd", "nigeria", "nig", "enterprises", "enterprise", "furniture", "office", "chairs",
    "chair", "company", "and", "the", "plaza", "store", "shop", "global", "services", "ventures", "abuja",
}

SearchFn = Callable[[str], list[dict[str, str]]]


class SearchError(Exception):
    """A Brave call failed. The message never contains the API key."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _matches(domain: str, known: set[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in known)


def is_third_party(domain: str) -> bool:
    return any(
        _matches(domain, group)
        for group in (MARKETPLACE_DOMAINS, DIRECTORY_DOMAINS, SOCIAL_DOMAINS, NON_SUPPLIER_DOMAINS,
                      CONTACT_DB_DOMAINS, PROPERTY_DOMAINS, BUSINESS_FOR_SALE_DOMAINS)
    )


def _lists_many_businesses(text: str) -> bool:
    return max(len(COMPANY_SUFFIX.findall(text)), len(PHONE_IN_RESULT.findall(text))) >= MANY_BUSINESSES


def classify(url: str, title: str = "", snippet: str = "") -> str:
    """supplier_site | directory | marketplace | article | social (social is never fetched).

    Only supplier_site counts as an own-site supplier. Directories, listing pages, "top 10" articles,
    marketplaces and social pages are leads, whether recognised by domain, URL, title or snippet.
    """
    domain = normalize_domain(url)
    parts = urllib.parse.urlsplit(url)
    text = f"{title} {re.sub(r'<[^>]+>', '', snippet)}"
    path_words = re.sub(r"[-_/+.]+", " ", parts.path)
    if _matches(domain, SOCIAL_DOMAINS):
        return "social"
    if _matches(domain, MARKETPLACE_DOMAINS):
        return "marketplace"
    if _matches(domain, CONTACT_DB_DOMAINS) or CONTACT_DB_TEXT.search(text):
        return "contact_database"
    if _matches(domain, PROPERTY_DOMAINS) or PROPERTY_TEXT.search(text):
        return "property_listing"
    if _matches(domain, BUSINESS_FOR_SALE_DOMAINS) or BUSINESS_FOR_SALE_TEXT.search(text):
        return "business_for_sale"
    if (_matches(domain, DIRECTORY_DOMAINS) or DIRECTORY_HINT.search(domain + parts.path) or COMPANY_LIST_TEXT.search(text)
            or DIRECTORY_TEXT.search(text) or _lists_many_businesses(text)):
        return "directory"
    if (_matches(domain, NON_SUPPLIER_DOMAINS) or ARTICLE_HINT.search(parts.path) or ARTICLE_HINT.search(title)
            or LISTICLE.search(path_words) or LISTICLE.search(text)):
        return "article"
    return "supplier_site"


# ---------------------------------------------------------------------------
# Queries and Brave access
# ---------------------------------------------------------------------------

def build_queries(product: str, location: str) -> list[str]:
    """Generic, showroom/store and (where known) district queries, interleaved so early ones vary."""
    _, profile = _profile(location)
    general = [
        f"{product} suppliers in {location}",
        f"{product} showroom {location}",
        f"{product} store {location}",
    ]
    if FURNITURE_WORDS.search(product):
        general.append(f"office furniture store {location}")
    general += [f"buy {product} {location} price delivery", f"{product} bulk order company {location}"]
    districts = [f"{product} {d} {location}" for d in (profile or {}).get("districts", [])]
    merged: list[str] = []
    for i in range(max(len(general), len(districts))):
        merged += general[i : i + 1] + districts[i : i + 1]
    return merged


def _retry_wait(err: urllib.error.HTTPError) -> float:
    try:
        return min(float(err.headers.get("Retry-After", RETRY_WAIT)), MAX_RETRY_WAIT)
    except (TypeError, ValueError, AttributeError):
        return RETRY_WAIT


class RateLimits:
    """The plan's own per-second limit, read from Brave's X-RateLimit-Limit header ('1, 15000' = 1/s, 15000/month)."""

    def __init__(self) -> None:
        self.per_second: int | None = None

    def update(self, headers: Any) -> None:
        try:
            first = str(headers.get("X-RateLimit-Limit", "")).split(",")[0].strip()
            if first.isdigit() and int(first) > 0:
                self.per_second = int(first)
        except AttributeError:
            pass


def brave_search(
    query: str,
    key: str,
    timeout: int = 15,
    sleep: Callable[[float], None] = time.sleep,
    limits: RateLimits | None = None,
) -> list[dict[str, str]]:
    """One Brave web search; retries once after an HTTP 429. Errors never contain the key.

    limits, if given, learns the plan's per-second limit from the response headers.
    """
    request = urllib.request.Request(
        f"{BRAVE_URL}?{urllib.parse.urlencode({'q': query, 'count': 10})}",
        headers={"Accept": "application/json", "X-Subscription-Token": key},
    )
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                if limits is not None:
                    limits.update(getattr(resp, "headers", None))
                data = json.load(resp)
            break
        except urllib.error.HTTPError as err:
            if err.code == 429 and attempt == 1:
                sleep(_retry_wait(err))
                continue
            raise SearchError(f"Brave API returned HTTP {err.code}", err.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise SearchError("could not reach Brave API (network or timeout)") from None
        except ValueError:
            raise SearchError("Brave API returned an unreadable response") from None
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "description": r.get("description", "")}
        for r in data.get("web", {}).get("results", [])
        if r.get("url")
    ]


def paced(search_fn: SearchFn, min_interval: float = MIN_QUERY_INTERVAL, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> SearchFn:
    """Wrap a search function so consecutive calls are at least min_interval seconds apart."""
    last: list[float | None] = [None]

    def wrapper(query: str) -> list[dict[str, str]]:
        if last[0] is not None:
            wait = min_interval - (clock() - last[0])
            if wait > 0:
                sleep(wait)
        try:
            return search_fn(query)
        finally:
            last[0] = clock()

    return wrapper


class QueryRunner:
    """Runs search queries with spacing, and two at a time only when the plan's limit allows it.

    per_second() returns the plan's per-second limit once known (from Brave's response headers), else None.
    Until it is known, or if it is below PARALLEL_WORKERS, or after any HTTP 429, queries run one at a time.
    """

    def __init__(
        self,
        search_fn: SearchFn,
        per_second: Callable[[], int | None] = lambda: None,
        spacing: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        workers: int = PARALLEL_WORKERS,
    ) -> None:
        self._search, self._per_second, self._spacing = search_fn, per_second, spacing
        self._sleep, self._clock, self._workers = sleep, clock, workers
        self._last: float | None = None
        self.rate_limited = False
        self.parallel_batches = 0

    def width(self) -> int:
        limit = self._per_second()
        return self._workers if limit is not None and limit >= self._workers and not self.rate_limited else 1

    def _wait(self) -> None:
        if self._last is not None:
            wait = self._spacing - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)

    def _call(self, query: str) -> Any:
        try:
            return self._search(query)
        except SearchError as err:
            if err.status == 429:
                self.rate_limited = True
            return err

    def search(self, query: str) -> list[dict[str, str]]:
        self._wait()
        outcome = self._call(query)
        self._last = self._clock()
        if isinstance(outcome, SearchError):
            raise outcome
        return outcome

    def search_many(self, queries: list[str]) -> list[Any]:
        """Outcomes in order: a result list, or the SearchError for that query."""
        out: list[Any] = []
        start = 0
        while start < len(queries):
            batch = queries[start : start + self.width()]
            start += len(batch)
            self._wait()
            if len(batch) > 1:
                with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                    out += list(pool.map(self._call, batch))
                self.parallel_batches += 1
            else:
                out.append(self._call(batch[0]))
            self._last = self._clock()
        return out


# ---------------------------------------------------------------------------
# Records, candidates, names
# ---------------------------------------------------------------------------

def make_record(value: str, source_url: str, retrieved_at: str, query: str, rank: int, snippet: str, **extra: Any) -> dict[str, Any]:
    return {
        "value": value,
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "query": query,
        "rank": rank,
        "snippet": snippet,
        **extra,
    }


def extract_candidates(page_url: str, html: str) -> list[dict[str, str]]:
    """Outbound links on a directory/article page that look like a supplier's own website."""
    page_domain = normalize_domain(page_url)
    seen: set[str] = set()
    found: list[dict[str, str]] = []
    for href, text in parse_html(html)["links"]:
        link = resolve_link(page_url, href)
        if not link or FILE_SUFFIX.search(urllib.parse.urlsplit(link).path):
            continue
        domain = normalize_domain(link)
        if domain == page_domain or domain in seen or is_third_party(domain):
            continue
        seen.add(domain)
        generic = not (3 <= len(text) <= 80) or bool(GENERIC_ANCHOR.match(text))
        found.append(
            {
                "name": domain if generic else text,
                "name_source": "domain" if generic else "anchor_text",
                "domain": domain,
                "site_url": site_origin(link) + "/",
                "link": link,
            }
        )
        if len(found) >= MAX_CANDIDATES_PER_PAGE:
            break
    return found


def name_key(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower().replace("&", " and ")))


def _plausible_name(text: str) -> str | None:
    text = re.sub(r"\s*\(.*?\)\s*$", "", re.sub(r"^\s*\d+[.)]\s*", "", text)).strip(" -:–")
    words = text.split()
    if not (2 <= len(words) <= 6) or len(text) > 60 or text.endswith(("?", ".", ":")) or NAME_REJECT.match(text):
        return None
    capitalized = sum(1 for w in words if w[:1].isupper())
    return text if capitalized * 2 >= len(words) else None


def extract_named_businesses(page_url: str, html: str) -> list[str]:
    """Business-looking names on a directory/article page: profile-link text and h2-h4 headings."""
    parsed = parse_html(html)
    page_domain = normalize_domain(page_url)
    names: dict[str, str] = {}
    for href, text in parsed["links"]:
        link = resolve_link(page_url, href)
        if link and normalize_domain(link) == page_domain and PROFILE_HREF.search(urllib.parse.urlsplit(link).path + "/"):
            plausible = _plausible_name(text)
            if plausible:
                names.setdefault(name_key(plausible), plausible)
    for heading in parsed["headings"]:
        plausible = _plausible_name(heading)
        if plausible:
            names.setdefault(name_key(plausible), plausible)
    return list(names.values())[:MAX_NAMED_PER_PAGE]


def _name_tokens(name: str) -> list[str]:
    tokens = [t for t in name_key(name).split() if len(t) >= 3]
    distinctive = [t for t in tokens if t not in GENERIC_NAME_WORDS]
    return distinctive or tokens


def name_matches_site(name: str, url: str, title: str) -> bool:
    """Does a search result plausibly belong to this business? Guards against wrong-site resolution."""
    tokens = _name_tokens(name)
    if not tokens:
        return False
    core = normalize_domain(url).split(".")[0].replace("-", "")
    if any(t in core for t in tokens):
        return True
    return all(t in name_key(title) for t in tokens)


# ---------------------------------------------------------------------------
# Location fit
# ---------------------------------------------------------------------------

def location_fit(location: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Rank from search text only: 0 local, 1 nearby, 2 states delivery there, 3 other/unknown."""
    text = " ".join(f"{r.get('value', '')} {r.get('snippet', '')} {r.get('source_url', '')}" for r in records).lower()
    local, nearby = location_terms(location)
    local_alt = "|".join(re.escape(t.lower()) for t in local)
    delivery_re = re.compile(rf"{DELIVERY_CONTEXT}[^.;]{{0,80}}?\b(?:{local_alt})\b")
    stripped = delivery_re.sub(" ", text)  # "delivers to Abuja" or "available in Lagos, Abuja" is not a base city

    def hit(terms: list[str]) -> str | None:
        return next((t for t in terms if re.search(rf"\b{re.escape(t.lower())}\b", stripped)), None)

    if (term := hit(local)):
        return {"rank": 0, "label": "local", "matched": term}
    if (term := hit(nearby)):
        return {"rank": 1, "label": "nearby", "matched": term}
    if delivery_re.search(text):
        return {"rank": 2, "label": "states delivery there", "matched": None}
    return {"rank": 3, "label": "other or unknown", "matched": None}


# ---------------------------------------------------------------------------
# Search run
# ---------------------------------------------------------------------------

class _Collector:
    """Accumulates one run's results (internal to run_search)."""

    def __init__(self) -> None:
        self.suppliers: dict[str, dict[str, Any]] = {}
        self.needs_review: dict[str, dict[str, Any]] = {}
        self.third_party: list[dict[str, Any]] = []
        self.overseas: dict[str, dict[str, Any]] = {}  # known overseas retailers and manufacturers: counted apart, never suppliers
        self.price_benchmarks: list[dict[str, Any]] = []
        self.unresolved: list[dict[str, Any]] = []
        self.named: dict[str, dict[str, Any]] = {}
        self.queries: list[dict[str, Any]] = []
        self.known_names: set[str] = set()
        self.pages_read: set[str] = set()  # directory/article pages already read this run
        self.duplicates_removed = 0  # results that were already-known own-site suppliers
        self.raw_results: dict[str, list[dict[str, str]]] = {}  # query -> results, so a cut-off run can resume without asking Brave again

    def add(self, target: dict[str, dict[str, Any]], domain: str, site_url: str, record: dict[str, Any]) -> None:
        if target is self.suppliers and domain in target:
            self.duplicates_removed += 1
        entry = target.setdefault(domain, {"domain": domain, "site_url": site_url, "records": []})
        entry["records"].append(record)
        self.known_names.add(name_key(str(record["value"])))


def _record_query(c: _Collector, query: str, phase: str, retrieved_at: str, outcome: Any) -> list[dict[str, str]]:
    if isinstance(outcome, SearchError):
        c.queries.append({"query": query, "phase": phase, "retrieved_at": retrieved_at, "state": "FAILED", "error": str(outcome), "result_count": 0})
        return []
    c.queries.append({"query": query, "phase": phase, "retrieved_at": retrieved_at, "state": "OK", "result_count": len(outcome)})
    c.raw_results[query] = outcome
    return outcome


def _run_query(c: _Collector, runner: QueryRunner, query: str, phase: str) -> tuple[list[dict[str, str]], str]:
    retrieved_at = now_iso()
    try:
        outcome: Any = runner.search(query)
    except SearchError as err:
        outcome = err
    return _record_query(c, query, phase, retrieved_at, outcome), retrieved_at


def _read_listing_page(c: _Collector, fetcher: Fetcher, base: dict[str, Any], source_class: str) -> None:
    """Fetch a directory/article page: own-site links become suppliers/needs_review; other names queue for lookup."""
    url, query, rank, snippet = base["source_url"], base["query"], base["rank"], base["snippet"]
    page = fetcher.fetch(url)
    if page["state"] != OK:
        c.unresolved.append({**base, "fetch_state": page["state"], "reason": page.get("error", "")})
        return
    candidates = extract_candidates(url, page["html"])
    for cand in candidates:
        matched = cand["name_source"] == "anchor_text"
        record = make_record(
            cand["name"], url, page["retrieved_at"], query, rank, snippet,
            source_class=f"{source_class}_link" if matched else "needs_review",
            name_source=cand["name_source"], resolved_link=cand["link"],
        )
        if not matched:
            record = {**record, "reason": "link text is not a business name; cannot match to a business"}
        c.add(c.suppliers if matched else c.needs_review, cand["domain"], cand["site_url"], record)
    names = extract_named_businesses(url, page["html"])
    for name in names:
        c.named.setdefault(name_key(name), {"name": name, "source_url": url, "source_class": source_class, "query": query, "rank": rank, "snippet": snippet})
    if not candidates and not names:
        c.unresolved.append({**base, "fetch_state": OK, "reason": "no outbound supplier website link or business name found"})


def _handle_result(c: _Collector, fetcher: Fetcher, rank: int, result: dict[str, str], query: str, retrieved_at: str) -> None:
    url, title, snippet = result["url"], result.get("title", ""), result.get("description", "")
    source_class = classify(url, title, snippet)
    base = make_record(title or url, url, retrieved_at, query, rank, snippet, source_class=source_class, title=title)
    if source_class == "supplier_site":
        domain = normalize_domain(url)
        if is_known_overseas(domain):
            c.overseas.setdefault(domain, {"domain": domain, "source_url": url, "title": title, "reason": "a known overseas retailer or manufacturer"})
            return
        c.add(c.suppliers, domain, site_origin(url) + "/", {**base, "value": domain})
        return
    c.third_party.append(base)
    if source_class == "marketplace":
        prices = find_prices(f"{title} {snippet}")
        if prices:
            c.price_benchmarks.append({
                "value": prices[0], "all_prices": prices, "label": PRICE_BENCHMARK_LABEL,
                "source_url": url, "source_domain": normalize_domain(url), "title": title,
                "retrieved_at": retrieved_at, "date": retrieved_at[:10], "query": query, "rank": rank, "snippet": snippet,
            })
    if source_class in ("directory", "article") and url not in c.pages_read:
        c.pages_read.add(url)
        _read_listing_page(c, fetcher, base, source_class)


def _likely_nigerian(supplier: dict[str, Any]) -> bool:
    """A guess from the search results (the supplier's own pages are read later by check_suppliers)."""
    text = " ".join(f"{r.get('value', '')} {r.get('title', '')} {r.get('snippet', '')}" for r in supplier["records"])
    return search_text_is_nigerian(supplier["domain"], text)


def _enough(c: _Collector, location: str, target: int, local_target: int) -> bool:
    """The discovery target is about relevant (likely Nigerian) own-site suppliers; once met, discovery stops."""
    return sum(1 for s in c.suppliers.values() if _likely_nigerian(s)) >= target


def _resolve_named(c: _Collector, runner: QueryRunner, location: str, allowance: int, after: Callable[[], None] | None = None) -> None:
    """One targeted '<business> <location>' query per named business with no own site found."""
    for key, info in list(c.named.items()):
        if key in c.known_names:
            continue
        if allowance <= 0:
            c.unresolved.append({**info, "reason": "query budget used up before this name was looked up"})
            continue
        allowance -= 1
        query = f"{info['name']} {location}"
        results, retrieved_at = _run_query(c, runner, query, "resolution")
        if after:
            after()
        hit = next(
            ((rank, r) for rank, r in enumerate(results, start=1)
             if classify(r["url"], r.get("title", ""), r.get("description", "")) == "supplier_site" and name_matches_site(info["name"], r["url"], r.get("title", ""))),
            None,
        )
        if not hit:
            c.unresolved.append({**info, "reason": "no own site found for this business"})
            continue
        rank, result = hit
        record = make_record(
            info["name"], result["url"], retrieved_at, query, rank, result.get("description", ""),
            source_class="resolved_by_search", resolved_from=info["source_url"], title=result.get("title", ""),
        )
        c.add(c.suppliers, normalize_domain(result["url"]), site_origin(result["url"]) + "/", record)


def _social_only_leads(c: _Collector) -> list[dict[str, Any]]:
    return [{**r, "status": SOCIAL_ONLY_STATUS} for r in c.third_party if r["source_class"] in ("social", "marketplace")]


def _listing_leads(c: _Collector) -> list[dict[str, Any]]:
    return [{**r, "status": LISTING_STATUS} for r in c.third_party if r["source_class"] in LISTING_CLASSES]


def _short_list(leads: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Name, link and kind only, one per page: offered to the buyer on request, never presented as suppliers."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for lead in leads:
        if lead["source_url"] not in seen:
            seen.add(lead["source_url"])
            out.append({"name": str(lead["value"]), "link": lead["source_url"], "kind": lead["source_class"]})
    return out


def build_region_queries(product: str, regions: list[str]) -> list[str]:
    """Queries for each supplier region, interleaved so every region is searched early: base, showroom, then districts."""
    per_region: list[list[str]] = []
    for region in regions:
        _, profile = _profile(region)
        per_region.append([f"{product} suppliers in {region}", f"{product} showroom {region}"]
                          + [f"{product} {district} {region}" for district in (profile or {}).get("districts", [])])
    merged: list[str] = []
    for i in range(max((len(q) for q in per_region), default=0)):
        merged += [q[i] for q in per_region if i < len(q)]
    return merged


def _search_nearby(
    c: _Collector, runner: QueryRunner, fetcher: Fetcher, product: str, cities: list[str], budget: int, after: Callable[[], None] | None = None
) -> tuple[list[str], str]:
    """Up to `budget` queries, one per nearby town; stops at the first that adds no new supplier. Returns (towns, why it stopped)."""
    tried: list[str] = []
    reason = ""
    for city in cities[:MAX_NEARBY_QUERIES]:
        if len(tried) >= budget:
            return tried, "no query budget left"
        before = set(c.suppliers)
        query = f"{product} suppliers {city}"
        results, retrieved_at = _run_query(c, runner, query, "nearby")
        tried.append(city)
        for rank, result in enumerate(results, start=1):
            _handle_result(c, fetcher, rank, result, query, retrieved_at)
        if after:
            after()
        if set(c.suppliers) == before:
            return tried, f"the query for {city} added no new supplier"
    reason = "tried every available town" if len(tried) == len(cities) else f"reached the limit of {MAX_NEARBY_QUERIES} towns"
    return tried, reason


def count_unsupported(records: list[dict[str, Any]]) -> int:
    """Records that would be a claim without a source: no source_url or no retrieved_at."""
    return sum(1 for r in records if not r.get("source_url") or not r.get("retrieved_at"))


def search_summary_rows(summary: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        ("stage seconds", seconds_text(summary["stage_seconds"])),
        (summary["queries_label"], f"{summary['queries_used']} of {summary['query_cap']}"
         + (f" ({summary['queries_in_parallel_batches']} pair(s) run two at a time)" if summary["queries_in_parallel_batches"] else "")
         + (f" ({summary['queries_from_cache']} answered from the earlier partial run)" if summary.get("queries_from_cache") else "")),
        ("supplier regions", ", ".join(summary["supplier_regions"]) or "the delivery city only"),
        ("nearby towns queried", ", ".join(summary["nearby_cities_searched"]) or f"none ({summary['nearby_status']})"),
        ("own-site suppliers", str(summary["own_site_suppliers"])),
        ("leads only (not suppliers)", str(summary["leads_only"])),
        ("duplicates removed", str(summary["duplicates_removed"])),
        ("chair sellers (fit yes)", "not checked yet (run check_suppliers.py)"),
        ("suppliers with a price", "not checked yet (run check_suppliers.py)"),
        ("unsupported claims", str(summary["unsupported_claims"])),
        ("total time", f"{summary['stage_seconds']['total']:.1f} s"),
    ]


def _assemble_report(
    c: _Collector, product: str, location: str, mode: str, total: int, fetcher: Fetcher, target: int, local_target: int,
    regions: list[str], nearby: dict[str, Any], stages: dict[str, float], runner: QueryRunner, status: str, phase: str,
) -> dict[str, Any]:
    demo = mode == "demo"
    city_terms = location_terms(location)[0]
    ranked = []
    for s in c.suppliers.values():
        fit = location_fit(location, s["records"])
        text = " ".join(f"{r.get('value', '')} {r.get('title', '')} {r.get('snippet', '')}" for r in s["records"])
        ranked.append({**s, "likely_nigerian": _likely_nigerian(s), "location_fit": fit, "mentions_regions": regions_in(text, regions or [location]),
                       "delivery": delivery_label(city_terms, s["domain"], fit["rank"], None, s["records"])})
    suppliers = sorted(ranked, key=lambda s: (s["location_fit"]["rank"], min(r["rank"] for r in s["records"])))
    leads, listing_leads = _social_only_leads(c), _listing_leads(c)
    short_leads = _short_list(leads + listing_leads)
    fits = [s["location_fit"]["rank"] for s in suppliers]
    counts = {label: fits.count(i) for i, label in enumerate(("local", "nearby", "states_delivery_there", "other_or_unknown"))}
    city = location.strip().title()
    queries_label = "Demo queries read from fixtures (no Brave call)" if demo else "Brave queries used"
    elapsed = stages.get("total", 0.0)
    run_summary = {
        "mode": mode,
        "stage_seconds": {name: round(secs, 2) for name, secs in stages.items()},
        "queries_label": queries_label,
        "queries_used": len(c.queries),
        "query_cap": total,
        "queries_in_parallel_batches": runner.parallel_batches,
        "supplier_regions": regions,
        "nearby_cities_searched": nearby["towns_queried"],
        "nearby_status": nearby["stopped_because"],
        "own_site_suppliers": len(suppliers),
        "relevant_suppliers_so_far": sum(1 for s in suppliers if s["likely_nigerian"]),
        "discovery_target": target,
        "overseas_excluded": len(c.overseas),
        "leads_only": len(short_leads),
        "duplicates_removed": c.duplicates_removed,
        "chair_sellers_product_fit_yes": None,
        "suppliers_with_a_price": None,
        "unsupported_claims": count_unsupported([r for s in suppliers for r in s["records"]]),
        "note": "Product fit, prices and 'based in' are checked by check_suppliers.py. This summary is for the output, not for chat.",
    }
    region_text = f" Supplier regions searched: {', '.join(regions)}." if regions else ""
    if nearby["towns_queried"]:
        nearby_text = f" Nearby towns queried: {', '.join(nearby['towns_queried'])} ({nearby['stopped_because']})."
    elif nearby["requested"]:
        nearby_text = f" Nearby towns were asked for but none was queried ({nearby['stopped_because']})."
    else:
        nearby_text = ""
    queries_text = (
        f"Demo queries read from fixtures: {len(c.queries)} of {total} (no Brave call)."
        if demo else f"Brave queries used: {len(c.queries)} of {total}."
    )
    return {
        "mode": mode,
        "status": status,
        "progress": {"phase": phase, "queries_done": len(c.queries), "queries_cap": total, "suppliers_so_far": len(suppliers), "elapsed_seconds": round(elapsed, 1)},
        "data_source": DEMO_NOTICE if demo else "live Brave Search results and live supplier pages",
        "request": {"product": product, "location": location, "supplier_regions": regions},
        "generated_at": now_iso(),
        "summary": (DEMO_SUMMARY_PREFIX if demo else "") + (
            f"{len(suppliers)} own-site suppliers found ({counts['local']} mention {city}, {counts['nearby']} mention a nearby town, "
            f"{counts['states_delivery_there']} say they deliver to {city}, {counts['other_or_unknown']} other; "
            "'based in' needs an address and is checked later); "
            f"{len(short_leads)} leads only (directories, listing pages, articles, marketplaces, contact databases, social: never counted as suppliers); "
            f"{len(c.price_benchmarks)} marketplace price benchmarks (not supplier prices); "
            f"{len(c.unresolved)} names or pages not resolved. The list is not padded.{region_text}{nearby_text} "
            f"{queries_text}"
        ),
        "run_summary": run_summary,
        "overseas_excluded": list(c.overseas.values()),
        "nearby_search": nearby,
        "queries": c.queries,
        "suppliers": suppliers,
        "needs_review": [v for k, v in c.needs_review.items() if k not in c.suppliers],
        "social_only_leads": leads,
        "listing_leads": listing_leads,
        "leads_short_list": short_leads,
        "price_benchmarks": c.price_benchmarks,
        "third_party_sources": c.third_party,
        "unresolved": c.unresolved,
        "raw_results": c.raw_results,
        "stats": {
            "queries_run": len(c.queries),
            "query_cap": total,
            "distinct_own_site_suppliers": len(suppliers),
            "by_location_fit": counts,
            "social_only_leads": len(leads),
            "leads_only": len(short_leads),
            "pages_fetched": fetcher.pages_used,
            "target": target,
            "local_target": local_target,
            "nearby_queries": len(nearby["towns_queried"]),
        },
    }


def run_search(
    product: str,
    location: str,
    search_fn: SearchFn,
    fetcher: Fetcher,
    max_queries: int = MAX_TOTAL_QUERIES,
    target: int = DEFAULT_TARGET,
    local_target: int = LOCAL_TARGET,
    mode: str = "live",
    runner: QueryRunner | None = None,
    allow_elsewhere: bool = False,
    nearby_cities: list[str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    supplier_regions: list[str] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """mode: 'live' (Brave) or 'demo' (saved fixtures, labelled as such in the output).

    supplier_regions: where suppliers may be based (default: the delivery city only). Each region gets queries
    (base, showroom, then district names) inside the query cap. location stays the delivery city.
    allow_elsewhere: the buyer widened the area. Up to 3 nearby towns are queried, stopping at the first query
    that adds no new supplier; the output lists exactly which towns were queried and why it stopped.
    on_progress: called with a partial report after every query, so a cut-off run leaves usable results.
    """
    demo = mode == "demo"
    runner = runner or QueryRunner(search_fn)
    c = _Collector()
    total = min(max_queries, MAX_TOTAL_QUERIES)
    reserve = min(RESOLUTION_RESERVE, total // 3)
    regions = [r.strip() for r in (supplier_regions or []) if r.strip()]
    stages: dict[str, float] = {}
    nearby: dict[str, Any] = {"requested": bool(allow_elsewhere), "towns_available": [], "towns_queried": [], "stopped_because": "not asked for (no --allow-elsewhere)"}
    began = mark = clock()

    def snapshot(status: str, phase: str) -> dict[str, Any]:
        stages_now = {**stages, "total": clock() - began}
        return _assemble_report(c, product, location, mode, total, fetcher, target, local_target, regions, nearby, stages_now, runner, status, phase)

    def progress(phase: str) -> Callable[[], None]:
        return lambda: on_progress(snapshot("partial", phase)) if on_progress else None

    queries = (build_region_queries(product, regions) if regions else build_queries(product, location))[: total - reserve]
    index = 0
    first_round = len(regions)  # every chosen region gets its base query before the target can stop discovery
    while index < len(queries) and (index < first_round or not _enough(c, location, target, local_target)):
        batch = queries[index : index + runner.width()]
        index += len(batch)
        retrieved_at = now_iso()
        for query, outcome in zip(batch, runner.search_many(batch)):
            results = _record_query(c, query, "discovery", retrieved_at, outcome)
            for rank, result in enumerate(results, start=1):
                _handle_result(c, fetcher, rank, result, query, retrieved_at)
        progress("discovery")()
    stages["discovery"], mark = clock() - mark, clock()

    cities = nearby_cities or list((_profile(location)[1] or {}).get("cities", []))
    nearby["towns_available"] = cities
    if allow_elsewhere and regions:
        nearby["stopped_because"] = "supplier regions were given; they replace the nearby-town search"
    elif allow_elsewhere and not cities:
        nearby["stopped_because"] = f"no nearby towns are known for {location}; pass --nearby-cities"
    elif allow_elsewhere:
        nearby["towns_queried"], nearby["stopped_because"] = _search_nearby(
            c, runner, fetcher, product, cities, min(MAX_NEARBY_QUERIES, total - len(c.queries)), progress("nearby towns"))
    stages["nearby_cities"], mark = clock() - mark, clock()

    _resolve_named(c, runner, location, total - len(c.queries), progress("name lookups"))
    stages["name_lookups"] = clock() - mark
    final = snapshot("complete", "done")
    if on_progress:
        on_progress(final)
    return final


def _write_json(path: str, data: dict[str, Any]) -> None:
    """Atomic write, so a cut-off run never leaves a half-written file."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    os.replace(temp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--product", required=True)
    parser.add_argument("--location", required=True, help="the buyer's delivery city")
    parser.add_argument("--supplier-regions", help="comma-separated regions where suppliers may be based, e.g. Lagos,Abuja,Kaduna (default: the delivery city)")
    parser.add_argument("--max-queries", type=int, default=MAX_TOTAL_QUERIES)
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET)
    parser.add_argument("--max-page-fetches", type=int, default=DEFAULT_PAGE_FETCHES)
    parser.add_argument("--allow-elsewhere", action="store_true", help="the buyer widened the area: also query up to 3 nearby towns")
    parser.add_argument("--nearby-cities", help="comma-separated nearby towns to try (default: a short list for known cities)")
    parser.add_argument("--out", help="write JSON here (also written after every query, so a cut-off run leaves partial results)")
    parser.add_argument("--resume", action="store_true", help="continue a cut-off run from the partial --out file without repeating its queries")
    parser.add_argument("--progress", help="progress file (default: next to --out, as <name>.progress.json); print it with progress.py")
    parser.add_argument("--replay", help="replay a saved live run from this bundle (or set REPLAY_FILE): no Brave call, no site fetched; see save_replay.py")
    parser.add_argument("--replay-speed", type=float, help="replay speed-up for the stage timings (default 5, or REPLAY_SPEED)")
    args = parser.parse_args(argv)
    replay = replay_path(args.replay)
    if replay:
        try:
            bundle = load_bundle(replay)
        except ValueError as err:
            print(f"Error: {err}", file=sys.stderr)
            return 2
        print(notice(bundle), file=sys.stderr)
        report = replay_search(bundle, ProgressFile(args.progress or progress_path(args.out), "scout_search"), replay_speed(args.replay_speed))
        if args.out:
            _write_json(args.out, report)
        else:
            sys.stdout.reconfigure(encoding="utf-8")
            print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    cache: dict[str, list[dict[str, str]]] = {}
    if args.resume and args.out and os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as handle:
            previous = json.load(handle)
        if previous.get("status") == "complete":
            print(f"{args.out} is already a complete run; delete it to search again.", file=sys.stderr)
            return 0
        cache = previous.get("raw_results", {})
        print(f"Resuming: {len(cache)} queries from the partial run are reused.", file=sys.stderr)
    demo = demo_requested()
    key = ""
    if not demo:
        try:
            key = load_secret("BRAVE_API_KEY")
        except SecretError as err:
            print(f"Note: no Brave key ({err}); switching to demo mode.", file=sys.stderr)
            demo = True
    if demo and not demo_available():
        print("Error: no Brave key and no demo fixtures found. Set BRAVE_API_KEY (or ~/.procuremate/secrets.env), "
              "or run from a full clone of the repo to use demo mode.", file=sys.stderr)
        return 2
    cache_hits = [0]
    if demo:
        print(DEMO_NOTICE, file=sys.stderr)
        live: SearchFn = make_search()
        limits = None
    else:
        limits = RateLimits()  # learns the plan's per-second limit from the first response
        budget = Budget()
        if not args.resume:
            budget.start_task()  # a new search starts a new task budget (shared with Vera's tools)
        live = guarded(lambda q: brave_search(q, key, limits=limits), budget, QueryCache(), "scout_search", SearchError)

    def search(query: str) -> list[dict[str, str]]:
        if query in cache:
            cache_hits[0] += 1
            return cache[query]
        return live(query)

    runner = QueryRunner(search, per_second=(lambda: limits.per_second) if limits else (lambda: None), spacing=0.0 if demo else MIN_QUERY_INTERVAL)
    last: dict[str, Any] = {}

    progress_file = ProgressFile(args.progress or progress_path(args.out), "scout_search")

    def save(partial: dict[str, Any]) -> None:
        last.clear()
        last.update(partial)
        if args.out:
            _write_json(args.out, partial)
        p = partial.get("progress", {})        # The cap is known but the search can stop early at its target, so the total is a maximum, never a promise.
        progress_file.update("searching", p.get("queries_done", 0), p.get("queries_cap"), p.get("suppliers_so_far", 0),
                             interrupted=partial.get("status") == "interrupted", phase=1, total_is_maximum=True)

    started = time.monotonic()
    network = None if demo else dns_check()
    if network and network["warning"]:
        print(f"Warning: {network['warning']}", file=sys.stderr)
    try:
        report = run_search(
            args.product, args.location, search_fn=runner.search, runner=runner,
            fetcher=Fetcher(opener=make_opener(), max_pages=args.max_page_fetches) if demo else polite_fetcher(args.max_page_fetches),
            max_queries=args.max_queries, target=args.target, mode="demo" if demo else "live", allow_elsewhere=args.allow_elsewhere,
            nearby_cities=[x.strip() for x in args.nearby_cities.split(",") if x.strip()] if args.nearby_cities else None,
            supplier_regions=[x.strip() for x in args.supplier_regions.split(",") if x.strip()] if args.supplier_regions else None,
            on_progress=save,
        )
    except BaseException:
        if last:
            save({**last, "status": "interrupted"})  # keep what was found; --resume continues from here
            print(f"Interrupted after {time.monotonic() - started:.0f} s. Partial results are in {args.out}; run again with --resume.", file=sys.stderr)
        raise
    report["run_summary"]["queries_from_cache"] = cache_hits[0]
    if not demo:
        report["run_summary"]["brave_budget"] = budget.summary()
        print_budget(budget)
    progress_file.update("searching", len(report["queries"]), len(report["queries"]), len(report["suppliers"]), finished=True, phase=1)
    if args.out:
        _write_json(args.out, report)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(report, indent=2, ensure_ascii=False))
    print(format_run_summary("demo" if demo else "live", search_summary_rows(report["run_summary"])), file=sys.stderr)
    return 0 if any(q["state"] == "OK" for q in report["queries"]) else 1


if __name__ == "__main__":
    sys.exit(main())
