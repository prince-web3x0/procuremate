"""What a supplier sells: product terms, chair types, product fit, and listed prices from its own pages.

Only text already fetched (or search results already returned) is used. Nothing is invented: a price that is
not on a page is not reported, and a chair that is not stated as an office chair does not count as one.
For an office-chair request only office or ergonomic chairs count as a fit; dining, leisure and other chairs
are reported separately, with their prices labelled "listed price, different chair type".
"""
import re
import urllib.parse
from typing import Any

from common import PRICE_RE, currency_of, find_prices, normalize_domain, parse_html, resolve_link

EXCERPT_LEN = 200
MAX_PRICE_ITEMS = 12
SUPPLIER_PRICE_LABEL = "listed price (supplier site, not a quote)"
OTHER_TYPE_LABEL = "listed price, different chair type"
GENERIC_PRODUCT_WORDS = {
    "office", "ergonomic", "executive", "adjustable", "bulk", "new", "used", "cheap", "quality", "good",
    "for", "in", "and", "the", "of", "with", "a", "an", "modern", "home",
}
# Lines that are fees or thresholds, not a product's price.
PRICE_SKIP = re.compile(r"deliver|shipping|installation|\bvat\b|\btax\b|minimum|\babove\b|\bover\b|discount|\bsave\b", re.I)
PRICE_NAME_NOISE = re.compile(r"^(price|from|now|sale|only|our price|buy now|add to cart)[:\s\-]*", re.I)
PRICE_NAME_TAIL = re.compile(r"[\s:\-\u2013|]*\b(?:from|only|now|at|price|starting at)\b[\s:\-\u2013|]*$", re.I)
LABEL_ONLY = re.compile(r"^(?:(?:original|regular|current|sale|special|new|old)\s+)?price(?:\s+(?:was|is))?$|^(?:was|now|from)$", re.I)
VALID_NOTE = re.compile(r"valid for|per piece|per chair|one chair|price for|each", re.I)
_P = r"(?:" + PRICE_RE.pattern + r")"
CURRENT_PRICE = re.compile(r"(?:current price is|now|sale price|special price)[:\s]*(" + _P + ")", re.I)
ORIGINAL_PRICE = re.compile(r"(?:original price was|regular price|old price|was)[:\s]*(" + _P + ")", re.I)

OFFICE_WORDS = re.compile(
    r"\b(office|ergonomic|executive|task|swivel|mesh|manager|managerial|visitor|reception|conference|boardroom|computer|orthop\w+|staff|secretary|clerical|workstation)\b",
    re.I,
)
STUDENT_WORDS = re.compile(
    r"\b(students?|classroom|school|lecture|university|campus|pupils?|kindergarten|nursery)\b|\bdesk\s*(?:and|&|with|\+)\s*chairs?\b|\bwriting (?:tablet|pad)\b",
    re.I,
)
DINING_WORDS = re.compile(r"\bdining\b", re.I)
LEISURE_WORDS = re.compile(r"\b(leisure|lounge|accent|arm ?chairs?|recliner|relax\w*|occasional|rocking|bean ?bag|patio|garden|outdoor)\b", re.I)
WANTS_OFFICE = re.compile(r"\b(office|ergonomic|executive|task|swivel|desk)\b", re.I)


def _excerpt(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= EXCERPT_LEN else text[: EXCERPT_LEN - 1] + "\u2026"


def _item(value: str, page: dict[str, Any], excerpt: str) -> dict[str, str]:
    return {"value": value, "source_url": page["url"], "retrieved_at": page["retrieved_at"], "excerpt": _excerpt(excerpt)}


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "xes", "sses")):
        return word[:-2]
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def product_terms(product: str) -> list[str]:
    """Head nouns of what the buyer wants: 'ergonomic office chairs' -> ['chair']."""
    words = re.findall(r"[a-z]+", product.lower())
    kept = [w for w in words if w not in GENERIC_PRODUCT_WORDS] or words
    return [_singular(w) for w in kept]


def term_pattern(terms: list[str]) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")(?:s|es)?\b", re.I)


def chair_type(text: str) -> str:
    """student | office | dining | leisure | other (a chair that is not stated as one of those).

    Only the text given is read (one product card's name), never the rest of the page. A student or classroom
    desk-and-chair set is not an office chair even if its name says 'ergonomic'.
    """
    if STUDENT_WORDS.search(text):
        return "student"
    if OFFICE_WORDS.search(text):
        return "office"
    if DINING_WORDS.search(text):
        return "dining"
    if LEISURE_WORDS.search(text):
        return "leisure"
    return "other"


def wanted_chair_type(product: str | None) -> str | None:
    """'office' when the request is for office/ergonomic chairs; None when any chair will do."""
    if not product or product_terms(product)[:1] != ["chair"]:
        return None
    return "office" if WANTS_OFFICE.search(product) else None


def _own_domain(url: str, domain: str) -> bool:
    host = normalize_domain(url)
    return bool(domain) and (host == domain or host.endswith("." + domain))


def _url_words(url: str) -> str:
    """URL path and query as plain words: '/shop/office-chair-x?cat=chairs' -> ' shop office chair x cat chairs'."""
    parts = urllib.parse.urlsplit(url)
    return re.sub(r"[-_/+=&.]", " ", urllib.parse.unquote(f"{parts.path} {parts.query}"))


def _fit_from_pages(pages: list[dict[str, Any]], domain: str, pattern: re.Pattern[str], term: str) -> list[dict[str, str]]:
    """Product evidence in fetched pages: the site's own navigation URLs first, then link text, headings, title, text."""
    found: list[dict[str, str]] = []
    for page in pages:
        parsed = parse_html(page["html"])
        for href, text in parsed["links"]:
            link = resolve_link(page["url"], href)
            if link and _own_domain(link, domain or normalize_domain(page["url"])) and pattern.search(_url_words(link)):
                found.append({**_item(term, {**page, "url": link}, f"navigation link {text or href} -> {link}"),
                              "evidence_source": "navigation link", "chair_type": chair_type(f"{text} {_url_words(link)}")})
                break
        else:
            spots = [("link", t) for _, t in parsed["links"] if t] + [("heading", h) for h in parsed["headings"]]
            spots += [("page title", parsed["title"])] + [("page text", line) for line in parsed["lines"] if len(line) < 300]
            for kind, text in spots:
                if pattern.search(text):
                    found.append({**_item(term, page, f"{kind}: {text}"), "evidence_source": "page content", "chair_type": chair_type(text)})
                    break
    return found


def _fit_from_search(records: list[dict[str, Any]] | None, domain: str, pattern: re.Pattern[str], term: str) -> list[dict[str, str]]:
    """Product evidence in Brave results already returned for this domain (URL, title or snippet). No new queries."""
    found: list[dict[str, str]] = []
    for record in records or []:
        url = record.get("source_url", "")
        if not url or not _own_domain(url, domain):
            continue
        for kind, text in (("URL", _url_words(url)), ("title", record.get("title", "")), ("snippet", re.sub(r"<[^>]+>", "", record.get("snippet", "")))):
            if pattern.search(text):
                found.append({
                    "value": term, "source_url": url, "retrieved_at": record.get("retrieved_at", ""),
                    "excerpt": _excerpt(f"search result {kind}: {text.strip()}"), "evidence_source": "search result", "chair_type": chair_type(text),
                })
                break
    return found


def search_fit_evidence(records: list[dict[str, Any]] | None, domain: str, product: str | None) -> list[dict[str, str]]:
    """Product evidence in a supplier's Brave records only (no pages fetched)."""
    terms = product_terms(product or "")
    return _fit_from_search(records, domain, term_pattern(terms), terms[0]) if terms else []


def _cards_as_evidence(pages: list[dict[str, Any]], product: str | None) -> list[dict[str, str]]:
    out = []
    for item in extract_prices(pages, product):
        if item["chair_type"]:
            out.append({"value": "chair", "source_url": item["product_url"] or item["source_url"], "retrieved_at": item["retrieved_at"],
                        "excerpt": _excerpt(f"product: {item['product_name']} ({item['chair_type']} chair)"),
                        "evidence_source": "product card", "chair_type": item["chair_type"]})
    return out


def product_fit(
    pages: list[dict[str, Any]],
    fields: dict[str, Any],
    attempts: list[dict[str, Any]],
    product: str | None,
    records: list[dict[str, Any]] | None = None,
    domain: str = "",
) -> dict[str, Any]:
    """Is the requested product on the site? yes / no / unclear.

    Evidence comes from pages already fetched, the site's own navigation URLs, product cards, and Brave results
    already returned for the same domain. For an office-chair request only office or ergonomic chairs give "yes":
    a chair not stated as an office chair is "unclear", and dining or leisure chairs only is "no".
    """
    terms = product_terms(product or "")
    base: dict[str, Any] = {"state": "unclear", "term": terms[0] if terms else None, "evidence": [], "categories_seen": []}
    if not terms:
        return {**base, "reason": "no product term in the request"}
    pattern = term_pattern(terms)
    evidence = _fit_from_pages(pages, domain, pattern, terms[0]) + _fit_from_search(records, domain, pattern, terms[0])
    wanted = wanted_chair_type(product)
    if wanted:
        evidence += _cards_as_evidence(pages, product)
    unique = list({e["source_url"]: e for e in evidence}.values())
    categories = [i["value"] for i in fields["product_categories"].get("items", [])]
    if wanted:
        office = [e for e in unique if e["chair_type"] == wanted]
        generic = [e for e in unique if e["chair_type"] == "other"]
        others = sorted({e["chair_type"] for e in unique if e["chair_type"] in ("dining", "leisure", "student")})
        types_seen = {**base, "categories_seen": categories, "chair_types_seen": sorted({e["chair_type"] for e in unique})}
        if office:
            return {**types_seen, "state": "yes", "evidence": office[:3], "reason": f"office chair page found on the supplier's own domain: {office[0]['source_url']}"}
        if generic:
            return {**types_seen, "evidence": generic[:3], "reason": f"chairs found but not stated as office chairs: {generic[0]['source_url']}"}
        if others:
            return {**types_seen, "state": "no", "reason": f"no office chairs on the pages checked; shows {' and '.join(others)} chairs only"}
    elif unique:
        return {**base, "state": "yes", "evidence": unique[:3], "categories_seen": categories, "reason": f"'{terms[0]}' page found on the supplier's own domain: {unique[0]['source_url']}"}
    if not pages:
        return {**base, "reason": "no page could be fetched and the search results show no matching page"}
    product_page_read = any(a.get("role") == "product" and a.get("state") == "OK" for a in attempts)
    if categories or product_page_read:
        seen = f"shows {', '.join(categories[:5])} only" if categories else "no catalogue entries for it on the pages read"
        return {**base, "state": "no", "categories_seen": categories, "reason": f"no '{terms[0]}' on the pages checked; {seen}"}
    return {**base, "reason": "no category or product pages could be read, so the catalogue is unknown"}


# --- listed prices -----------------------------------------------------------------------------

def _flatten_ld(nodes: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in nodes if isinstance(nodes, list) else [nodes]:
        if isinstance(node, dict):
            out.append(node)
            out.extend(_flatten_ld(node.get("@graph", [])))
    return out


def _ld_products(parsed: dict[str, Any]) -> list[tuple[str, str]]:
    """(product name, price text) pairs from JSON-LD Product offers."""
    out: list[tuple[str, str]] = []
    for node in _flatten_ld(parsed["jsonld"]):
        kind = node.get("@type", "")
        if "Product" not in (" ".join(kind) if isinstance(kind, list) else str(kind)):
            continue
        offers = node.get("offers")
        for offer in offers if isinstance(offers, list) else [offers]:
            if isinstance(offer, dict) and offer.get("price") not in (None, ""):
                currency = str(offer.get("priceCurrency") or "").strip()
                out.append((str(node.get("name") or "UNKNOWN"), f"{currency} {offer['price']}".strip()))
    return out


def _clean_name(text: str) -> str:
    return PRICE_NAME_TAIL.sub("", PRICE_NAME_NOISE.sub("", text)).strip(" :-\u2013|\u2022\u00b7\u00a0")


def _price_name(line: str, price_text: str, previous: str) -> str:
    name = _clean_name(line.split(price_text, 1)[0])
    if len(name) < 3 or LABEL_ONLY.match(name):
        name = _clean_name(previous) if previous and len(previous) <= 80 else ""
    return name[:80] if len(name) >= 3 and not LABEL_ONLY.match(name) else "UNKNOWN"


def _current_and_original(line: str, prices: list[str]) -> tuple[str | None, str | None]:
    """Current price and original price. Never guesses: two unlabelled prices, or only an old price, give no current price."""
    current, original = CURRENT_PRICE.search(line), ORIGINAL_PRICE.search(line)
    if current:
        cur = current.group(1).strip()
        old = original.group(1).strip() if original and original.group(1).strip() != cur else None
        return cur, old
    if len(prices) == 1 and not original:
        return prices[0], None
    return None, None


def extract_prices(pages: list[dict[str, Any]], product: str | None) -> list[dict[str, Any]]:
    """Prices the supplier's own pages show: current price, original price if shown, product name and URL, chair type.

    Never invented. For an office-chair request, a dining or leisure chair is kept but labelled
    "listed price, different chair type" and does not match the request.
    """
    terms = product_terms(product) if product else []
    pattern = term_pattern(terms) if terms else None
    wanted = wanted_chair_type(product)
    items: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add(page: dict[str, Any], name: str, current: str, original: str | None, url: str, excerpt: str, note: str = "") -> None:
        ctype = chair_type(name) if re.search(r"\bchairs?\b", name, re.I) else None
        named = bool(pattern and pattern.search(name))
        matches = named and (wanted is None or ctype == wanted)
        different = named and bool(wanted) and ctype is not None and ctype != wanted
        key = (name.lower(), re.sub(r"\D", "", current), page["url"])
        items.setdefault(key, {
            **_item(current, page, excerpt), "currency": currency_of(current), "product_name": name, "product_url": url,
            "original_price": original, "chair_type": ctype, "label": OTHER_TYPE_LABEL if different else SUPPLIER_PRICE_LABEL,
            "matches_product": matches, "note": note,
        })

    for page in pages:
        parsed = parse_html(page["html"])
        for name, price in _ld_products(parsed):
            add(page, name, price, None, page["url"], f"JSON-LD product offer: {name} {price}")
        link_for = {text.casefold(): resolve_link(page["url"], href) or "" for href, text in parsed["links"] if text}
        lines = parsed["lines"]
        for i, line in enumerate(lines):
            if len(line) > 300 or PRICE_SKIP.search(line):
                continue
            prices = find_prices(line)
            current, original = _current_and_original(line, prices) if prices else (None, None)
            if not current:
                continue
            name = _price_name(line, prices[0], lines[i - 1] if i else "")
            note = lines[i + 1] if i + 1 < len(lines) and VALID_NOTE.search(lines[i + 1]) and len(lines[i + 1]) < 120 else ""
            add(page, name, current, original, link_for.get(name.casefold(), ""), line, note)
    return sorted(items.values(), key=lambda it: not it["matches_product"])
