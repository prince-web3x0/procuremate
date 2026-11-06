"""Links that open a drafted email in the buyer's own mail app or Gmail. Nothing is ever sent from here.

Usage:
    python mail_link.py --to sales@example.ng --subject "Quote request" --body-file body.txt

Prints JSON with the mailto: link, the Gmail compose link, their lengths, and a warning when a link is too long
to rely on (then use the plain text block instead).
"""
import argparse
import json
import sys
import urllib.parse
from typing import Any

MAX_LINK_CHARS = 1800
GMAIL_COMPOSE = "https://mail.google.com/mail/?view=cm&fs=1"


def _quote(text: str) -> str:
    """Percent-encode everything but unreserved characters; spaces become %20 and line breaks %0D%0A."""
    return urllib.parse.quote(text.replace("\r\n", "\n").replace("\n", "\r\n"), safe="")


def build_links(to: str, subject: str, body: str, max_chars: int = MAX_LINK_CHARS) -> dict[str, Any]:
    """The two links for one email, plus a warning for any link longer than max_chars."""
    if not to or "@" not in to or any(c in to for c in " \r\n<>,;"):
        raise ValueError("the recipient must be a single email address")
    recipient = urllib.parse.quote(to, safe="@")
    mailto = f"mailto:{recipient}?subject={_quote(subject)}&body={_quote(body)}"
    gmail = f"{GMAIL_COMPOSE}&to={_quote(to)}&su={_quote(subject)}&body={_quote(body)}"
    too_long = [name for name, link in (("mailto", mailto), ("gmail", gmail)) if len(link) > max_chars]
    result: dict[str, Any] = {
        "to": to, "subject": subject, "mailto_link": mailto, "gmail_link": gmail,
        "mailto_length": len(mailto), "gmail_length": len(gmail), "too_long": too_long,
        "plain_text": f"To: {to}\nSubject: {subject}\n\n{body}",
        "sends_anything": False,
    }
    if too_long:
        result["warning"] = (f"The {' and '.join(too_long)} link is over about {max_chars} characters and may be cut off by a mail app or chat. "
                             "Offer the plain text block to copy instead.")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--to", required=True)
    parser.add_argument("--subject", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--body")
    group.add_argument("--body-file", help="read the body from this UTF-8 file")
    args = parser.parse_args(argv)
    if args.body is not None:
        body = args.body
    else:
        with open(args.body_file, encoding="utf-8") as handle:
            body = handle.read()
    try:
        result = build_links(args.to, args.subject, body)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 2
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
