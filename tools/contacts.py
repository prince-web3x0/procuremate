"""Phone numbers and emails from a supplier's own pages, with where each was found.

Reads visible text, tel: / mailto: / WhatsApp links, protected-email markup and JSON-LD. Every number is
normalized to +234 form (a leading zero after the country code is dropped), the original text is kept, and
numbers are de-duplicated by the normalized value, so one number written two ways is one number.
Nothing is invented: a missing number or email is recorded as missing, with the page it was missing from.
JavaScript-only contact details are reported as a limitation (no headless browser is used).
"""
import re
import urllib.parse
from typing import Any

from common import parse_html

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
EMAIL_REJECT = re.compile(r"\.(png|jpe?g|gif|webp|svg)$|example\.|sentry|wixpress", re.I)
# Nigerian formats: (+234) 0907..., +234 907..., 0907-878-8888, 0907.878.8888; or another country's +code.
PHONE_TEXT = re.compile(r"(?<![\w.])(?:\(?\+?\s*234\)?|\+\d{1,3}|0)[\s().\-]*\d(?:[\s().\-]*\d){7,12}(?!\d)")
WHATSAPP_NUMBER = re.compile(r"(?:wa\.me/|[?&]phone=)\+?(\d{8,15})", re.I)
EXCERPT_LEN = 200
LITTLE_TEXT_WORDS = 60
MANY_SCRIPTS = 4

FOUND_IN_TEXT = "visible text"
FOUND_IN_TEL = "tel link"
FOUND_IN_WHATSAPP = "WhatsApp link"
FOUND_IN_DATA = "structured data"
FOUND_IN_MAILTO = "mailto link"
FOUND_IN_PROTECTED = "protected email (decoded)"


def normalize_phone(raw: str, from_link: bool = False) -> str | None:
    """+234 form for Nigerian numbers (any common spelling), +<digits> for other countries, else None."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("234"):
        rest = digits[3:]
        if rest.startswith("0") and len(rest) == 11:
            rest = rest[1:]
        return "+234" + rest if len(rest) == 10 and rest[0] in "789" else None
    if digits.startswith("0") and len(digits) == 11 and digits[1] in "789":
        return "+234" + digits[1:]
    if from_link and len(digits) == 10 and digits[0] in "789":
        return "+234" + digits
    if ("+" in raw or from_link) and 8 <= len(digits) <= 15:
        return "+" + digits
    return None


def _excerpt(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= EXCERPT_LEN else text[: EXCERPT_LEN - 1] + "…"


class _Found:
    """De-duplicated values: one entry per normalized value, remembering every spelling and place."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def add(self, key: str, original: str, found_in: str, page: dict[str, Any], excerpt: str) -> None:
        item = self.items.get(key)
        if item is None:
            self.items[key] = {
                "value": key, "original": original.strip(), "variants": [original.strip()], "found_in": [found_in],
                "source_url": page["url"], "retrieved_at": page["retrieved_at"], "excerpt": _excerpt(excerpt), "seen_on": [page["url"]],
            }
            return
        if original.strip() not in item["variants"]:
            item["variants"].append(original.strip())
        if found_in not in item["found_in"]:
            item["found_in"].append(found_in)
        if page["url"] not in item["seen_on"]:
            item["seen_on"].append(page["url"])


def _ld_values(nodes: Any, keys: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for node in nodes if isinstance(nodes, list) else [nodes]:
        if not isinstance(node, dict):
            continue
        for key in keys:
            value = node.get(key)
            out += [str(v) for v in (value if isinstance(value, list) else [value]) if isinstance(v, (str, int)) and str(v).strip()]
        out += _ld_values(node.get("@graph", []), keys) + _ld_values(node.get("contactPoint", []), keys)
    return out


def extract_contacts(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """pages: dicts with url, retrieved_at, html. Returns phones, emails and notes (limitations / missing contact)."""
    phones, emails = _Found(), _Found()
    notes: list[dict[str, str]] = []
    form_pages: list[str] = []
    js_pages: list[str] = []
    for page in pages:
        parsed = parse_html(page["html"])
        for href, text in parsed["links"]:
            low = href.strip().lower()
            if low.startswith("tel:"):
                norm = normalize_phone(urllib.parse.unquote(href[4:]), from_link=True)
                if norm:
                    phones.add(norm, urllib.parse.unquote(href[4:]), FOUND_IN_TEL, page, f"tel link: {href[4:]} {text}")
            elif low.startswith("mailto:"):
                address = urllib.parse.unquote(href[7:].split("?")[0]).strip()
                if EMAIL_RE.fullmatch(address) and not EMAIL_REJECT.search(address):
                    emails.add(address.lower(), address, FOUND_IN_MAILTO, page, f"mailto link: {address}")
            elif "wa.me/" in low or "whatsapp.com/send" in low or "api.whatsapp.com" in low:
                found = WHATSAPP_NUMBER.search(href)
                if found:
                    phones.add(normalize_phone(found.group(1), from_link=True) or "+" + found.group(1), found.group(1), FOUND_IN_WHATSAPP, page, f"WhatsApp link: {href}")
                else:
                    notes.append({"kind": "contact via WhatsApp link", "source_url": page["url"], "note": "a WhatsApp link without a number in it"})
        for raw in _ld_values(parsed["jsonld"], ("telephone", "phone")):
            norm = normalize_phone(raw)
            if norm:
                phones.add(norm, raw, FOUND_IN_DATA, page, f"structured data telephone: {raw}")
        for raw in _ld_values(parsed["jsonld"], ("email",)):
            if EMAIL_RE.fullmatch(raw.strip()) and not EMAIL_REJECT.search(raw):
                emails.add(raw.strip().lower(), raw.strip(), FOUND_IN_DATA, page, f"structured data email: {raw}")
        for address in parsed["cf_emails"]:
            if EMAIL_RE.fullmatch(address):
                emails.add(address.lower(), address, FOUND_IN_PROTECTED, page, f"protected email on the page: {address}")
        for line in parsed["lines"]:
            if len(line) > 400:
                continue
            for match in PHONE_TEXT.finditer(line):
                norm = normalize_phone(match.group())
                if norm:
                    phones.add(norm, match.group().strip(), FOUND_IN_TEXT, page, line)
            for address in EMAIL_RE.findall(line):
                if not EMAIL_REJECT.search(address):
                    emails.add(address.lower(), address, FOUND_IN_TEXT, page, line)
        if parsed["forms"]:
            form_pages.append(page["url"])
        if len(" ".join(parsed["lines"]).split()) < LITTLE_TEXT_WORDS and parsed["scripts"] >= MANY_SCRIPTS:
            js_pages.append(page["url"])

    first_page = pages[0]["url"] if pages else ""
    if not phones.items and not emails.items:
        notes += [{"kind": "contact via form", "source_url": u, "note": "the page has a form and no phone or email was found"} for u in form_pages[:1]]
        notes += [{"kind": "may need JavaScript", "source_url": u,
                   "note": "the page shows little text and loads scripts; contact details may need JavaScript (not checked: no headless browser is used)"} for u in js_pages[:1]]
    if not phones.items:
        notes.append({"kind": "no phone found", "source_url": first_page, "note": "no phone number appears as text, in a tel/WhatsApp link or in structured data"})
    if not emails.items:
        notes.append({"kind": "no email found", "source_url": first_page, "note": "no email address appears as text, in a mailto link or in structured data"})
    return {"phones": list(phones.items.values()), "emails": list(emails.items.values()), "notes": notes}
