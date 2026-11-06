"""Mail links: mailto and Gmail compose links are encoded correctly and long ones are flagged. Nothing is sent."""
import json
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

import mail_link as ml

BODY = (
    "Hello,\n\nWe need 20 office chairs delivered to Abuja within 7 days.\n"
    "Please send:\n- unit price (₦) & VAT\n- delivery cost\n- lead time\n- your company registration number\n\nThank you"
)
SUBJECT = "Quote request: 20 office chairs & delivery to Abuja"


def _parts(link: str) -> dict[str, str]:
    query = link.split("?", 1)[1]
    return {k: v for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=True)}


def test_the_mailto_link_decodes_back_to_the_exact_text() -> None:
    out = ml.build_links("sales@seats-demo.example", SUBJECT, BODY)
    assert out["mailto_link"].startswith("mailto:sales@seats-demo.example?")
    parts = _parts(out["mailto_link"])
    assert parts["subject"] == SUBJECT and parts["body"].replace("\r\n", "\n") == BODY


def test_the_gmail_link_has_the_compose_form_and_decodes_back() -> None:
    out = ml.build_links("sales@seats-demo.example", SUBJECT, BODY)
    assert out["gmail_link"].startswith("https://mail.google.com/mail/?view=cm&fs=1&to=sales%40seats-demo.example&su=")
    parts = _parts(out["gmail_link"])
    assert parts["view"] == "cm" and parts["fs"] == "1" and parts["to"] == "sales@seats-demo.example"
    assert parts["su"] == SUBJECT and parts["body"].replace("\r\n", "\n") == BODY


def test_naira_ampersand_spaces_and_line_breaks_are_escaped() -> None:
    link = ml.build_links("a@b.ng", "x", BODY)["gmail_link"]
    assert "%E2%82%A6" in link  # the naira sign as UTF-8
    assert "%26" in link and "&amp" not in link  # an ampersand in the text cannot split the query
    assert "%0D%0A" in link and "\n" not in link and " " not in link
    assert link.count("&body=") == 1 and link.count("&su=") == 1


def test_non_ascii_text_survives() -> None:
    out = ml.build_links("a@b.ng", "Cotación – café ₦", "Olá, preço? Ṣé ó wà? 你好")
    assert _parts(out["mailto_link"])["body"] == "Olá, preço? Ṣé ó wà? 你好"
    assert _parts(out["gmail_link"])["su"] == "Cotación – café ₦"


def test_a_long_body_is_flagged_and_the_plain_text_is_offered() -> None:
    long_body = BODY + "\n" + "Please quote every colour and finish we may need. " * 60
    out = ml.build_links("sales@seats-demo.example", SUBJECT, long_body)
    assert out["too_long"] == ["mailto", "gmail"] and out["mailto_length"] > 1800 and out["gmail_length"] > 1800
    assert "plain text" in out["warning"] and out["plain_text"].endswith(long_body)


def test_a_normal_draft_has_no_warning() -> None:
    out = ml.build_links("sales@seats-demo.example", SUBJECT, BODY)
    assert out["too_long"] == [] and "warning" not in out and max(out["mailto_length"], out["gmail_length"]) < 1800
    assert out["plain_text"].startswith("To: sales@seats-demo.example\nSubject: ") and out["sends_anything"] is False


@pytest.mark.parametrize("bad", ["", "no-at-sign", "a@b.ng, c@d.ng", "a@b.ng\nbcc: x@y.ng", "<a@b.ng>"])
def test_only_a_single_plain_address_is_accepted(bad: str) -> None:
    with pytest.raises(ValueError):
        ml.build_links(bad, "s", "b")


def test_the_command_reads_the_body_from_a_file(tmp_path: Path, capsys: Any) -> None:
    body = tmp_path / "body.txt"
    body.write_text(BODY, encoding="utf-8")
    assert ml.main(["--to", "sales@seats-demo.example", "--subject", SUBJECT, "--body-file", str(body)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["to"] == "sales@seats-demo.example" and out["too_long"] == [] and "₦" in out["plain_text"]


def test_the_command_rejects_a_bad_address(capsys: Any) -> None:
    assert ml.main(["--to", "nope", "--subject", "s", "--body", "b"]) == 2
    assert "single email address" in capsys.readouterr().err
