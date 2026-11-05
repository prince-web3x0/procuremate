"""Cross-checks for one supplier domain: consistency, on-site registration details, independent listing, domain age.

Usage:
    python cross_check.py supplier.example [--out file.json]
    DEMO_MODE=1 python cross_check.py abuja-seating.example     # saved fixtures: no key, no network

Fetches the supplier's pages (robots.txt respected, page-capped), runs at most 2 Brave searches for an
independent public page, and one RDAP lookup. No logins, no social-site scraping.
Every field has state, value, items[{value, source_url, retrieved_at, excerpt}] and an evidence label.
Signals are not proof: a new domain or a missing listing is never evidence of fraud.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from typing import Any, Callable

from common import OK, Fetcher, PageCache, default_cache_dir, polite_fetcher, SecretError, load_secret, normalize_domain, now_iso, parse_html
from budget import BUDGET_REACHED, Budget, QueryCache, guarded, print_line as print_budget
from replay import load_bundle, notice, replay_cross_check, replay_path
from demo import DEMO_NOTICE, demo_domains, demo_requested, make_opener, make_rdap, make_search
from scout_search import SearchError, SearchFn, brave_search, classify, paced
from site_check import collect_pages, extract_fields

MAX_SEARCHES = 2
MAX_INDEPENDENT_PAGES = 3
MAX_ITEMS = 5
RDAP_BOOTSTRAP = "https://data.iana.org/rdap/dns.json"
RDAP_TIMEOUT = 10
NEW_DOMAIN_DAYS = 365
DISCLAIMER = "Signals only: a new domain, a missing listing or a mismatch is never proof of fraud."

LEGAL_WORDS = {
    "limited", "ltd", "enterprises", "enterprise", "nig", "nigeria", "plc", "company", "co", "inc",
    "llc", "ventures", "global", "services", "international", "intl", "and",
}
ADDRESS_STOP = {
    "nigeria", "street", "road", "avenue", "close", "crescent", "drive", "way", "plaza", "floor",
    "opposite", "beside", "off", "state", "the", "and",
}
SECOND_LEVELS = {"com", "co", "org", "net", "gov", "edu", "ac"}
RC_RE = re.compile(r"\b(RC|BN)\s*(?:no\.?|number|#)?\s*[:.\-]?\s*(\d{5,8})\b", re.IGNORECASE)
LEGAL_NAME_RE = re.compile(r"([A-Z][\w&.'\-]*(?: [A-Z&][\w&.'\-]*){0,5} (?:Limited|Ltd\.?|Enterprises?|Ventures))\b")
FOOTER_HINT = re.compile(r"©|copyright|registered|company number|\brc\b|\bbn\b", re.IGNORECASE)
PHONE_IN_TEXT = re.compile(r"\+?\d[\d\s\-().]{8,}\d")

GetJson = Callable[[str], Any]


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower().replace("&", " and "))


def brand_tokens(name: str) -> frozenset[str]:
    """Brand words with legal suffixes dropped. For internal site consistency only; CAC matching is stricter."""
    return frozenset(t for t in _tokens(name) if t not in LEGAL_WORDS)


def brand_same(a: str, b: str) -> bool:
    ta, tb = brand_tokens(a), brand_tokens(b)
    if not ta or not tb:
        return False
    return ta == tb or (min(len(ta), len(tb)) >= 2 and (ta <= tb or tb <= ta))


def phone_key(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return digits[-10:] if len(digits) >= 10 else digits


def address_tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in _tokens(text) if len(t) >= 3 and t not in ADDRESS_STOP)


def address_same(a: str, b: str) -> bool:
    ta, tb = address_tokens(a), address_tokens(b)
    shared = ta & tb
    return bool(ta and tb) and len(shared) >= 2 and len(shared) / min(len(ta), len(tb)) >= 0.6


def _field(state: str, value: str, items: list[dict[str, str]], label: str, reason: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"state": state, "value": value, "evidence_label": label, "items": items[:MAX_ITEMS]}
    if reason:
        out["reason"] = reason
    return out


def _unknown(reason: str) -> dict[str, Any]:
    return _field("UNKNOWN", "UNKNOWN", [], "UNKNOWN", reason)


# ---------------------------------------------------------------------------
# (i) consistency across the pages already fetched
# ---------------------------------------------------------------------------

def _consistency(items: list[dict[str, str]], same: Callable[[str, str], bool], differ_state: str) -> dict[str, Any]:
    if not items:
        return _unknown("not found on the fetched pages")
    groups: list[list[dict[str, str]]] = []
    for item in items:
        for group in groups:
            if same(group[0]["value"], item["value"]):
                group.append(item)
                break
        else:
            groups.append([item])
    pages = {u for i in items for u in [i["source_url"], *i.get("seen_on", [])]}  # a number merged across pages counts on each
    if len(groups) > 1:
        return _field(differ_state, " vs ".join(g[0]["value"] for g in groups), items, "CONFLICTING" if differ_state == "CONFLICTING" else "SOURCE_REPORTED")
    if len(pages) >= 2:
        return _field("CONSISTENT", items[0]["value"], items, "SOURCE_REPORTED")
    return _field("SINGLE_SOURCE", items[0]["value"], items, "SOURCE_REPORTED", "seen on one page only")


def _email_domain_note(emails: list[dict[str, str]], domain: str) -> dict[str, Any]:
    if not emails:
        return _unknown("no public email found")
    own = [e for e in emails if normalize_domain(e["value"].split("@")[-1]) == domain]
    state = "OWN_DOMAIN" if own else "OTHER_DOMAIN"
    return _field(state, emails[0]["value"], emails, "SOURCE_REPORTED", "" if own else "email domain differs from the website (free mail is common for small shops)")


def internal_consistency(fields: dict[str, Any], domain: str) -> dict[str, Any]:
    def items(name: str) -> list[dict[str, str]]:
        return list(fields[name].get("items", []))

    names = items("business_name")
    structured = [n for n in names if not n.get("excerpt", "").startswith("page title:")]
    # <title> text is often a slogan ("Furniture Store | Shop Online"); only use it when nothing better exists
    return {
        "name": _consistency(structured or names, brand_same, "CONFLICTING"),
        "address": _consistency(items("address"), address_same, "CONFLICTING"),
        "phone": _consistency(items("phone"), lambda a, b: phone_key(a) == phone_key(b), "MULTIPLE_VALUES"),
        "email": _email_domain_note(items("email"), domain),
    }


# ---------------------------------------------------------------------------
# (ii) registered name / RC / BN on the site
# ---------------------------------------------------------------------------

def registration_on_site(pages: list[dict[str, Any]]) -> dict[str, Any]:
    names: dict[str, dict[str, str]] = {}
    numbers: dict[str, dict[str, str]] = {}
    for page in pages:
        for line in parse_html(page["html"])["lines"]:
            if len(line) > 400 or not FOOTER_HINT.search(line):
                continue
            item = {"source_url": page["url"], "retrieved_at": page["retrieved_at"], "excerpt": line[:200]}
            for kind, number in RC_RE.findall(line):
                numbers.setdefault(f"{kind.upper()} {number}", {"value": f"{kind.upper()} {number}", **item})
            for match in LEGAL_NAME_RE.findall(line):
                names.setdefault(match.lower(), {"value": match, **item})
    note = "reported by the site itself; not checked against any register"
    return {
        "registered_name": _field("FOUND", next(iter(names.values()))["value"], list(names.values()), "SOURCE_REPORTED", note) if names else _unknown("no registered company name found in footer, terms or about text"),
        "rc_bn_number": _field("FOUND", next(iter(numbers.values()))["value"], list(numbers.values()), "SOURCE_REPORTED", note) if numbers else _unknown("no RC/BN number found on the fetched pages"),
    }


# ---------------------------------------------------------------------------
# (iii) independent public page
# ---------------------------------------------------------------------------

def _text_match(text: str, name: str, phones: list[str], addresses: list[str]) -> dict[str, bool]:
    name_ok = bool(brand_tokens(name)) and brand_tokens(name) <= frozenset(_tokens(text))
    found_phones = {phone_key(m) for m in PHONE_IN_TEXT.findall(text)}
    phone_ok = any(phone_key(p) in found_phones for p in phones if len(phone_key(p)) >= 10)
    addr_tokens = frozenset(_tokens(text))
    addr_ok = any(
        len(address_tokens(a)) >= 2 and len(address_tokens(a) & addr_tokens) / len(address_tokens(a)) >= 0.7
        for a in addresses
    )
    return {"name": name_ok, "phone": phone_ok, "address": addr_ok}


def _independent_queries(name: str, phones: list[str], addresses: list[str]) -> list[str]:
    queries = []
    if phones:
        queries.append(f'"{name}" {phones[0]}')
    if addresses:
        queries.append(f'"{name}" {addresses[0][:60]}')
    return queries[:MAX_SEARCHES]


def independent_listing(
    fields: dict[str, Any], domain: str, search_fn: SearchFn | None, page_fetcher: Fetcher
) -> dict[str, Any]:
    """Another public page (not the supplier's own, not social) showing the same name AND phone or address."""
    names = fields["business_name"].get("items", [])
    phones = [i["value"] for i in fields["phone"].get("items", [])]
    addresses = [i["value"] for i in fields["address"].get("items", [])]
    if not names:
        return _unknown("no business name to search for")
    if not (phones or addresses):
        return _unknown("no phone or address on the site to match against")
    if search_fn is None:
        return _field("FAILED", "UNKNOWN", [], "UNKNOWN", "search not available (no API key)")
    name = names[0]["value"]
    agreements: list[dict[str, str]] = []
    partial: list[dict[str, str]] = []
    seen: set[str] = set()
    errors = 0
    stopped = False  # the task's Brave budget ran out
    for query in _independent_queries(name, phones, addresses):
        try:
            results = search_fn(query)
        except SearchError as err:
            errors += 1
            stopped = stopped or BUDGET_REACHED in str(err)
            continue
        for result in results:
            host = normalize_domain(result["url"])
            if host == domain or host.endswith("." + domain) or host in seen or classify(result["url"], result.get("title", "")) == "social":
                continue
            seen.add(host)
            item = _judge_result(result, name, phones, addresses, page_fetcher)
            if item and item["agreement"]:
                agreements.append(item)
            elif item:
                partial.append(item)
    if agreements:
        return _field("FOUND", f"{agreements[0]['agreement']} on {normalize_domain(agreements[0]['source_url'])}", agreements, "CORROBORATED")
    if partial:
        return _field("PARTIAL", "name seen, phone/address not matched", partial, "UNKNOWN", "a page uses the name but not the same phone or address")
    if stopped:
        return _field("FAILED", "UNKNOWN", [], "UNKNOWN", f"{BUDGET_REACHED}: the independent-listing search was skipped")
    reason = "searches failed" if errors == MAX_SEARCHES else "no independent page found with the same name and phone/address (a missing listing is a signal, not proof)"
    return _field("FAILED" if errors == MAX_SEARCHES else "UNKNOWN", "UNKNOWN", [], "UNKNOWN", reason)


def _judge_result(result: dict[str, str], name: str, phones: list[str], addresses: list[str], page_fetcher: Fetcher) -> dict[str, str] | None:
    snippet = f"{result.get('title', '')} {result.get('description', '')}"
    match, excerpt, retrieved_at = _text_match(snippet, name, phones, addresses), snippet, now_iso()
    if match["name"] and not (match["phone"] or match["address"]):
        page = page_fetcher.fetch(result["url"])  # robots.txt respected; page budget applies
        if page["state"] == OK:
            text = " ".join(parse_html(page["html"])["lines"])
            match, retrieved_at = _text_match(text, name, phones, addresses), page["retrieved_at"]
            excerpt = text[:200]
    if not match["name"]:
        return None
    both = [k for k in ("phone", "address") if match[k]]
    return {
        "value": result.get("title", result["url"]),
        "source_url": result["url"],
        "retrieved_at": retrieved_at,
        "excerpt": re.sub(r"\s+", " ", excerpt).strip()[:200],
        "agreement": "name+" + "+".join(both) if both else "",
    }


# ---------------------------------------------------------------------------
# (iv) domain age via RDAP
# ---------------------------------------------------------------------------

def registrable_domain(domain: str) -> str:
    labels = normalize_domain(domain).split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVELS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _http_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/rdap+json, application/json", "User-Agent": "ProcureMateScout/0.1"})
    with urllib.request.urlopen(request, timeout=RDAP_TIMEOUT) as resp:
        return json.load(resp)


def _cached_http_json(url: str) -> Any:
    """_http_json with a 24-hour disk cache shared by every agent of the user (the IANA bootstrap list and RDAP records).

    Only successful answers are kept; a failed lookup is retried next time.
    """
    cache = PageCache(default_cache_dir().with_name("rdap"))
    hit = cache.get(url)
    if hit is not None:
        return hit
    data = _http_json(url)
    cache.put(url, data)
    return data


def domain_age(domain: str, get_json: GetJson = _cached_http_json, today: date | None = None) -> dict[str, Any]:
    """Registration date from the public RDAP service for the TLD, if IANA lists one; otherwise UNKNOWN."""
    registrable = registrable_domain(domain)
    tld = registrable.rsplit(".", 1)[-1]
    try:
        bootstrap = get_json(RDAP_BOOTSTRAP)
        base = next((urls[0] for tlds, urls in bootstrap.get("services", []) if tld in tlds and urls), None)
        if not base:
            return _unknown(f"no public RDAP service is listed for .{tld}")
        url = f"{base.rstrip('/')}/domain/{registrable}"
        record = get_json(url)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError, AttributeError, TypeError):
        return _unknown("RDAP lookup failed or was unavailable")
    registered = next((e.get("eventDate") for e in record.get("events", []) if e.get("eventAction") == "registration"), None)
    if not registered:
        return _unknown("RDAP record has no registration date")
    try:
        when = datetime.fromisoformat(registered.replace("Z", "+00:00")).astimezone(timezone.utc).date()
    except ValueError:
        return _unknown("RDAP registration date was unreadable")
    days = ((today or date.today()) - when).days
    value = f"registered {when.isoformat()} ({days / 365.25:.1f} years ago)"
    item = {"value": value, "source_url": url, "retrieved_at": now_iso(), "excerpt": f"RDAP event registration {registered}"}
    found = _field("FOUND", value, [item], "SOURCE_REPORTED", "recently registered (a signal, not proof)" if days < NEW_DOMAIN_DAYS else "")
    return {**found, "years": round(days / 365.25, 1)}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def signals_from(checks: dict[str, Any]) -> list[str]:
    out: list[str] = []
    cons = checks["internal_consistency"]
    for name in ("name", "address"):
        if cons[name]["state"] == "CONFLICTING":
            out.append(f"{name} differs across the site's pages: {cons[name]['value']}")
    if cons["phone"]["state"] == "MULTIPLE_VALUES":
        out.append("several different phone numbers are listed")
    if checks["registration_on_site"]["registered_name"]["state"] == "UNKNOWN" and checks["registration_on_site"]["rc_bn_number"]["state"] == "UNKNOWN":
        out.append("the site shows no registered company name or RC/BN number")
    listing = checks["independent_listing"]
    if listing["state"] == "UNKNOWN":
        out.append("no independent page found with the same name and phone/address")
    elif listing["state"] == "FOUND":
        out.append(f"independent page agrees: {listing['value']}")
    age = checks["domain_age"]
    if age["state"] == "FOUND" and age.get("reason"):
        out.append(f"domain {age['value']}")
    return out


def cross_check(
    domain: str,
    fetcher: Fetcher,
    search_fn: SearchFn | None,
    page_fetcher: Fetcher,
    get_json: GetJson = _cached_http_json,
    today: date | None = None,
) -> dict[str, Any]:
    domain = normalize_domain(domain)
    attempts, pages = collect_pages(domain, fetcher)
    return cross_check_pages(domain, attempts, pages, search_fn, page_fetcher, get_json, today)


def cross_check_pages(
    domain: str,
    attempts: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    search_fn: SearchFn | None,
    page_fetcher: Fetcher,
    get_json: GetJson = _cached_http_json,
    today: date | None = None,
    age: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cross-checks over pages that were already fetched (no refetch of the supplier's site).

    age: an already-looked-up domain_age result (RDAP, not a Brave query), to avoid asking twice.
    """
    domain = normalize_domain(domain)
    fields = extract_fields(pages)
    age = age if age is not None else domain_age(domain, get_json, today)
    if not pages:
        failed = _field("FAILED", "UNKNOWN", [], "UNKNOWN", "no page could be fetched")
        checks = {
            "internal_consistency": {k: failed for k in ("name", "address", "phone", "email")},
            "registration_on_site": {"registered_name": failed, "rc_bn_number": failed},
            "independent_listing": failed,
            "domain_age": age,
        }
    else:
        checks = {
            "internal_consistency": internal_consistency(fields, domain),
            "registration_on_site": registration_on_site(pages),
            "independent_listing": independent_listing(fields, domain, search_fn, page_fetcher),
            "domain_age": age,
        }
    return {
        "domain": domain,
        "checked_at": now_iso(),
        "pages": attempts,
        "checks": checks,
        "signals": signals_from(checks) if pages else ["site could not be fetched"],
        "note": DISCLAIMER,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("domain")
    parser.add_argument("--out", help="write JSON here instead of stdout")
    parser.add_argument("--replay", help="read this domain's saved cross-check from a replay bundle (or set REPLAY_FILE): no Brave call, no site fetched")
    args = parser.parse_args(argv)
    replay = replay_path(args.replay)
    if replay:
        try:
            bundle = load_bundle(replay)
            report = replay_cross_check(bundle, normalize_domain(args.domain))
        except ValueError as err:
            print(f"Error: {err}", file=sys.stderr)
            return 2
        print(notice(bundle), file=sys.stderr)
        text = json.dumps(report, indent=2, ensure_ascii=False)
        if args.out:
            os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(text)
        else:
            sys.stdout.reconfigure(encoding="utf-8")
            print(text)
        return 0
    # Demo mode: DEMO_MODE=1, or no key when the domain is one of the demo fixtures.
    demo = demo_requested()
    search_fn: SearchFn | None = None
    if not demo:
        try:
            key = load_secret("BRAVE_API_KEY")
            task_budget = Budget()
            search_fn = guarded(paced(lambda q: brave_search(q, key)), task_budget, QueryCache(), "cross_check", SearchError)
        except SecretError as err:
            if normalize_domain(args.domain) in demo_domains():
                demo = True
            else:
                print(f"Warning: independent-listing check skipped ({err})", file=sys.stderr)
    if demo:
        if not demo_domains():
            print("Error: demo mode needs the demo/ fixtures from a full clone of the repo.", file=sys.stderr)
            return 2
        print(DEMO_NOTICE, file=sys.stderr)
        report = cross_check(
            args.domain, Fetcher(opener=make_opener(), max_pages=5), make_search(),
            Fetcher(opener=make_opener(), max_pages=MAX_INDEPENDENT_PAGES), make_rdap(),
        )
        report = {"mode": "demo", "data_source": DEMO_NOTICE, **report}
    else:
        report = cross_check(args.domain, polite_fetcher(5), search_fn, polite_fetcher(MAX_INDEPENDENT_PAGES))
        report = {"mode": "live", "data_source": "live supplier pages, Brave Search and RDAP", **report}
        if search_fn is not None:
            report["brave_budget"] = task_budget.summary()
            print_budget(task_budget)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0 if report["pages"] and report["pages"][0]["state"] == OK else 1


if __name__ == "__main__":
    sys.exit(main())
