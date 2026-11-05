"""Money, budget fit and the short shortlist card lines. Listed prices only: nothing is quoted, added up or guessed.

The per-chair share is the budget divided by the quantity, shown only when both are known, and it is
described as including delivery. A supplier's listed price is compared with it, never turned into a delivered total.
"""
import re
from typing import Any

NAIRA = "₦"
CHAIR_WORDS = {"office": "office chairs", "dining": "dining chairs", "leisure": "leisure chairs", "student": "student desk-and-chair sets"}
STYLE_WORDS = re.compile(r"\b(executive|ergonomic|mesh|task|visitor|conference|boss|manager|gaming|standing|l-shaped|corner|height-adjustable|adjustable|computer|folding|modular)\b", re.IGNORECASE)
MONEY_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(m|mn|million|k|thousand)?\b", re.IGNORECASE)
MULTIPLIER = {"m": 1_000_000, "mn": 1_000_000, "million": 1_000_000, "k": 1_000, "thousand": 1_000}


def parse_money(text: str | None) -> int | None:
    """'₦2.5m', '2,500,000', 'N500k', '2 million' -> naira as an int. None if there is no readable amount."""
    if not text:
        return None
    match = MONEY_RE.search(str(text))
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    value = round(number * MULTIPLIER.get((match.group(2) or "").lower(), 1))
    return value if value > 0 else None


def naira(amount: int) -> str:
    return f"{NAIRA}{amount:,}"


def per_chair_share(budget: int | None, quantity: int | None) -> int | None:
    """Budget divided by quantity, only when both are known."""
    if not budget or not quantity or quantity <= 0:
        return None
    return round(budget / quantity)


def price_number(price_text: str) -> int | None:
    """'₦345,000' -> 345000 (kobo after a decimal point are dropped)."""
    match = re.search(r"\d[\d,]*", price_text)
    return int(match.group().replace(",", "")) if match else None


def lowest_listed_price(items: list[dict[str, Any]], noun: str = "chair") -> dict[str, Any] | None:
    """The lowest naira price among items that match the product (right chair type). None if there is none."""
    candidates = []
    for item in items:
        number = price_number(item["value"]) if item.get("matches_product") and item.get("currency") == "NGN" else None
        if number:
            candidates.append((number, item))
    if not candidates:
        return None
    number, item = min(candidates, key=lambda pair: pair[0])
    return {"number": number, "text": naira(number), "chair_type": chair_words(item, noun), "product_name": item.get("product_name"),
            "source_url": item.get("source_url"), "label": item.get("label")}


def chair_words(item: dict[str, Any], noun: str = "chair") -> str:
    """'executive chairs', 'standing desks': up to two style words from the product name plus the product noun.

    Only for chairs does the chair type (office, dining, leisure) name the kind when the name has no style word.
    """
    kind = item.get("chair_type") or ""
    if noun == "chair" and kind == "student":  # a student set is never described by style words such as "ergonomic"
        return CHAIR_WORDS["student"]
    styles = list(dict.fromkeys(m.lower() for m in STYLE_WORDS.findall(item.get("product_name") or "")))[:2]
    if styles:
        return f"{' '.join(styles)} {noun}s"
    if noun != "chair":
        return f"{noun}s"
    return CHAIR_WORDS.get(kind, "chair type unclear")  # never borrow a type from elsewhere on the page


def budget_fit(lowest: dict[str, Any] | None, share: int | None) -> str:
    """'within', 'over' or 'unknown' (no listed price, or no per-chair share)."""
    if not lowest or not share:
        return "unknown"
    return "within" if lowest["number"] <= share else "over"


def price_phrase(lowest: dict[str, Any] | None) -> str:
    return f"from {lowest['text']}, {lowest['chair_type']}" if lowest else "no price"


def card_lines(domain: str, city: str, based_region: str | None, delivery_status: str, lowest: dict[str, Any] | None, share: int | None,
               phone: str | None, email: str | None, noun: str = "chair") -> list[str]:
    """The first three lines of a shortlist card (Scout adds Vera's one-sentence line). Missing fields read 'not found'."""
    delivery = {"confirmed": "confirmed", "claim": "says it delivers"}.get(delivery_status, "not stated")
    lines = [f"{domain} ({based_region or 'no address found'}; delivery to {city}: {delivery})"]
    if lowest:
        fit = budget_fit(lowest, share)
        compare = f", {fit} the {naira(share)} per-{noun} share, including delivery" if share and fit != "unknown" else ""
        lines.append(f"Price: from {lowest['text']} listed per {noun} ({lowest['chair_type']}){compare}")
    else:
        lines.append("Price: no price found")
    mail = email or (f"no email found; call or WhatsApp {phone}" if phone else "email not found")
    lines.append(f"Contact: {phone or 'phone not found'}, {mail}")
    return lines
