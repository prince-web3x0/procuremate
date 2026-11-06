"""Site check: read-only look at one supplier domain (homepage + at most 4 more pages).

Usage:
    python site_check.py supplier.example [--product "office chairs"] [--max-pages 5] [--out file.json]

Fetches the homepage, then at most one product, contact, about and delivery page (same domain),
respecting robots.txt, with timeouts. Missing data is reported as UNKNOWN, never guessed.
Every found value carries source_url, retrieved_at and a short excerpt. With --product it also reports
product_fit (yes / no / unclear, from the pages already fetched) and lists prices the supplier's own
pages show (currency, product name, source); no price is ever calculated or invented.
"""
import argparse
import json
import re
import sys
import urllib.parse
from typing import Any

from contacts import extract_contacts
from products import (  # noqa: F401  (re-exported for callers and tests)
    MAX_PRICE_ITEMS,
    extract_prices,
    product_fit,
    product_terms,
    search_fit_evidence,
    term_pattern as _term_pattern,
)
from common import (
    polite_fetcher,
    NETWORK_ERROR,
    OK,
    Fetcher,
    currency_of,
    find_prices,
    normalize_domain,
    now_iso,
    parse_html,
    resolve_link,
)

MAX_PAGES = 5
EXCERPT_LEN = 200
MAX_ITEMS = 5
ROLE_PATTERNS = (
    ("contact", re.compile(r"contact", re.I)),
    ("about", re.compile(r"about", re.I)),
    ("delivery", re.compile(r"deliver|shipping|dispatch", re.I)),
    ("product", re.compile(r"product|shop|catalog|collection|categor|store", re.I)),
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
EMAIL_REJECT = re.compile(r"\.(png|jpe?g|gif|webp|svg)$|example\.|sentry|wixpress", re.I)
PHONE_RE = re.compile(r"(?:\+?234|0)[\s\-]?[789][01]\d[\s\-]?\d{3}[\s\-]?\d{4}|\+\d{1,3}[\s\-]\d{2,4}[\s\-]\d{3,4}[\s\-]\d{3,4}")
ADDRESS_LABEL = re.compile(r"^(?:our |head |registered )?(?:office |business |showroom |store )?address\s*[:\-–]\s*(.{10,200})$", re.I)
DELIVERY_RE = re.compile(r"\b(deliver\w*|shipping|ships?|dispatch\w*)\b", re.I)
BULK_RE = re.compile(
    r"\b(bulk (?:orders?|purchas\w*|pric\w*|discounts?|buy\w*|suppl\w*|quantit\w*)|in bulk|wholesale|"
    r"trade (?:orders?|prices?)|minimum order|moq|corporate orders?|volume discount)\b",
    re.I,
)
CATEGORY_HREF = re.compile(r"categor|collection|product", re.I)
GENERIC_LABELS = {"shop", "products", "all products", "home", "view all", "read more", "shop now", "store", "collections", "categories", "uncategorised", "uncategorized"}
ORG_TYPE = re.compile(r"organization|business|store|corporation|company", re.I)
TITLE_SPLIT = re.compile(r"\s+[|–—-]\s+")


def _excerpt(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= EXCERPT_LEN else text[: EXCERPT_LEN - 1] + "…"


def _item(value: str, page: dict[str, Any], excerpt: str) -> dict[str, str]:
    return {
        "value": value,
        "source_url": page["url"],
        "retrieved_at": page["retrieved_at"],
        "excerpt": _excerpt(excerpt),
    }


def _field(items: list[dict[str, Any]], failed: bool, limit: int = MAX_ITEMS) -> dict[str, Any]:
    if items:
        return {"state": "FOUND", "evidence_label": "SOURCE_REPORTED", "items": items[:limit]}
    reason = "no page could be fetched" if failed else "not found on the fetched pages"
    return {"state": "FAILED" if failed else "UNKNOWN", "evidence_label": "UNKNOWN", "value": "UNKNOWN", "items": [], "reason": reason}


def select_links(base_url: str, html: str, domain: str) -> dict[str, str]:
    """First same-domain link for each of contact / about / delivery / product."""
    chosen: dict[str, str] = {}
    for href, text in parse_html(html)["links"]:
        link = resolve_link(base_url, href)
        if not link or normalize_domain(link) != domain:
            continue
        for role, pattern in ROLE_PATTERNS:
            if role not in chosen and pattern.search(link.split("//", 1)[-1] + " " + text) and link not in chosen.values():
                chosen[role] = link
                break
    return chosen


def _flatten_ld(nodes: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in nodes if isinstance(nodes, list) else [nodes]:
        if isinstance(node, dict):
            out.append(node)
            out.extend(_flatten_ld(node.get("@graph", [])))
    return out


def _ld_orgs(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    orgs = []
    for node in _flatten_ld(parsed["jsonld"]):
        kind = node.get("@type", "")
        kind = " ".join(kind) if isinstance(kind, list) else str(kind)
        if ORG_TYPE.search(kind):
            orgs.append(node)
    return orgs


def _lines_matching(parsed: dict[str, Any], pattern: re.Pattern[str]) -> list[str]:
    sentences: list[str] = []
    for line in parsed["lines"]:
        if len(line) > 600:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            if 15 <= len(sentence) <= 250 and pattern.search(sentence) and sentence not in sentences:
                sentences.append(sentence)
    return sentences


def extract_fields(pages: list[dict[str, Any]], product: str | None = None) -> dict[str, Any]:
    """pages: dicts with url, retrieved_at, html (only successfully fetched pages)."""
    names: list[dict[str, str]] = []
    addresses: list[dict[str, str]] = []
    categories: dict[str, dict[str, str]] = {}
    delivery: dict[str, dict[str, str]] = {}
    bulk: dict[str, dict[str, str]] = {}

    for page in pages:
        parsed = parse_html(page["html"])
        for org in _ld_orgs(parsed):
            if org.get("name"):
                names.append(_item(str(org["name"]), page, "JSON-LD organization name"))
            addr = org.get("address")
            if isinstance(addr, dict):
                joined = ", ".join(str(addr[k]) for k in ("streetAddress", "addressLocality", "addressRegion", "addressCountry") if addr.get(k))
                if joined:
                    addresses.append(_item(joined, page, "JSON-LD address: " + joined))
            elif isinstance(addr, str) and addr.strip():
                addresses.append(_item(addr.strip(), page, "JSON-LD address: " + addr))
        if parsed["meta"].get("og:site_name"):
            names.append(_item(parsed["meta"]["og:site_name"], page, "og:site_name: " + parsed["meta"]["og:site_name"]))
        if parsed["title"]:
            names.append(_item(TITLE_SPLIT.split(parsed["title"])[0].strip(), page, "page title: " + parsed["title"]))

        for href, text in parsed["links"]:
            if CATEGORY_HREF.search(href) and 2 <= len(text) <= 40 and text.lower() not in GENERIC_LABELS:
                categories.setdefault(text.lower(), _item(text, page, f"navigation link: {text}"))

        for line in parsed["lines"]:
            label = ADDRESS_LABEL.match(line)
            if label:
                addresses.append(_item(label.group(1).strip(), page, line))
        for block in parsed["address_blocks"]:
            addresses.append(_item(block, page, "<address> element: " + block))
        for sentence in _lines_matching(parsed, DELIVERY_RE):
            delivery.setdefault(sentence, _item(sentence, page, sentence))
        for sentence in _lines_matching(parsed, BULK_RE):
            bulk.setdefault(sentence, _item(sentence, page, sentence))

    failed = not pages
    contacts = extract_contacts(pages)
    notes = contacts["notes"]
    return {
        "business_name": _field(names, failed),
        "email": {**_field(contacts["emails"], failed), "notes": [n for n in notes if n["kind"] != "no phone found"]},
        "phone": {**_field(contacts["phones"], failed), "notes": [n for n in notes if n["kind"] != "no email found"],
                  "multiple_numbers": len(contacts["phones"]) > 1},
        "address": _field(addresses, failed),
        "product_categories": _field(list(categories.values()), failed),
        "delivery_statements": _field(list(delivery.values()), failed),
        "bulk_claims": _field(list(bulk.values()), failed),
        "prices": _field(extract_prices(pages, product), failed, MAX_PRICE_ITEMS),
    }


def collect_pages(domain: str, fetcher: Fetcher) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch homepage + contact/about/delivery/product pages. Returns (attempts, ok_pages)."""
    domain = normalize_domain(domain)
    attempts: list[dict[str, Any]] = []
    ok_pages: list[dict[str, Any]] = []

    def record(role: str, result: dict[str, Any]) -> None:
        attempts.append({k: v for k, v in {**result, "role": role}.items() if k != "html"})
        if result["state"] == OK:
            ok_pages.append({"url": result.get("final_url", result["url"]), "retrieved_at": result["retrieved_at"], "html": result["html"]})

    home = fetcher.fetch(f"https://{domain}/")
    if home["state"] == NETWORK_ERROR:
        home = fetcher.fetch(f"http://{domain}/")
    record("homepage", home)

    if home["state"] == OK:
        links = select_links(ok_pages[0]["url"], home["html"], domain)
        for role in ("contact", "about", "delivery", "product"):
            if role in links:
                record(role, fetcher.fetch(links[role]))
    return attempts, ok_pages


def site_report(
    domain: str,
    attempts: list[dict[str, Any]],
    ok_pages: list[dict[str, Any]],
    product: str | None = None,
    records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """records: this supplier's Brave search records (already returned), used for product fit only."""
    fields = extract_fields(ok_pages, product)
    report: dict[str, Any] = {
        "domain": normalize_domain(domain),
        "checked_at": now_iso(),
        "pages": attempts,
        "fields": fields,
    }
    if product:
        report["product_fit"] = product_fit(ok_pages, fields, attempts, product, records, normalize_domain(domain))
    return report


def check_site(domain: str, fetcher: Fetcher, product: str | None = None) -> dict[str, Any]:
    attempts, ok_pages = collect_pages(domain, fetcher)
    return site_report(domain, attempts, ok_pages, product)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("domain")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--product", help="what the buyer wants (e.g. 'office chairs'); adds product fit and matching prices")
    parser.add_argument("--out", help="write JSON here instead of stdout")
    args = parser.parse_args(argv)
    report = check_site(args.domain, polite_fetcher(min(args.max_pages, MAX_PAGES)), args.product)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0 if report["pages"] and report["pages"][0]["state"] == OK else 1


if __name__ == "__main__":
    sys.exit(main())
