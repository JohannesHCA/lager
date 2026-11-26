import io
import json
from datetime import date

import pytest
from conftest import FakeClaudeCli
from PIL import Image

from lager.extraction import (MAX_IMAGE_EDGE, SCHEMA, ExtractionError, Extractor,
                              UnsupportedAttachment, parse_document, prepare_input,
                              xlsx_to_text)
from lager.inventory import parse_products

VALID = {
    "dokumenttype": "slutseddel", "dato_fra": "2026-09-04", "dato_til": "2026-09-06",
    "stand": "Food Festival Aarhus",
    "linjer": [{"originaltekst": "Svensk ost 338.85", "antal": 338.85, "enhed": "",
                "varenr": "101", "sikkerhed": 0.97, "note": ""}],
}


# --- JSON-validering -----------------------------------------------------

def test_valid_json_is_parsed():
    doc = parse_document(json.dumps(VALID))
    assert doc.dokumenttype == "slutseddel"
    assert doc.linjer[0].antal == 338.85
    assert doc.linjer[0].varenr == "101"


@pytest.mark.parametrize("text, fejl", [
    ("ikke json", "ikke gyldig JSON"),
    ("[]", "ikke et JSON-objekt"),
    (json.dumps({**VALID, "dokumenttype": "kvittering"}), "ukendt dokumenttype"),
    (json.dumps({**VALID, "linjer": "x"}), "'linjer'"),
    (json.dumps({**VALID, "linjer": [{"antal": 1, "sikkerhed": 1}]}), "originaltekst"),
    (json.dumps({**VALID, "linjer": [{"originaltekst": "x", "antal": "mange",
                                      "sikkerhed": 1}]}), "ikke et tal"),
])
def test_invalid_json_is_rejected(text, fejl):
    with pytest.raises(ValueError, match=fejl):
        parse_document(text)


def test_lenient_values_are_normalised():
    raw = {**VALID, "dokumenttype": "Leverance", "dato_fra": "4/9", "linjer": [
        {"originaltekst": "15 kg Wrångeback", "antal": "15,5", "enhed": "kg", "varenr": 101,
         "sikkerhed": 1.7, "note": ""},
        {"originaltekst": "Alle små olivenolier", "antal": None, "enhed": "", "varenr": "",
         "sikkerhed": 0.3, "note": "uklart antal"},
    ]}
    doc = parse_document(json.dumps(raw))
    assert doc.dokumenttype == "leverance"
    assert doc.dato_fra == ""                      # ugyldig dato -> tom (mailens dato bruges)
    assert doc.linjer[0].antal == 15.5 and doc.linjer[0].varenr == "101"
    assert doc.linjer[0].sikkerhed == 1.0          # klippes til 0-1
    assert doc.linjer[1].antal is None and doc.linjer[1].varenr is None


def test_schema_follows_structured_output_rules():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])
    walk(SCHEMA)


# --- kald til Claude Code og genforsøg ---------------------------------------

def make_extractor(replies):
    cli = FakeClaudeCli(replies)
    products = parse_products([{"Varenr": 101, "Navn": "Svensk ost", "Aliaser": "Wrångebäck"}])
    return Extractor(products, date(2026, 11, 18), run=cli, claude_bin="claude"), cli


def test_cli_call_uses_haiku_max_effort_schema_and_read_only_tools():
    ex, cli = make_extractor([VALID])
    doc = ex.extract("seddel.pdf", "application/pdf", b"%PDF-1.4")
    assert doc.stand == "Food Festival Aarhus"
    assert cli.arg(0, "--model") == "claude-haiku-5-5"
    assert cli.arg(0, "--effort") == "max"
    assert json.loads(cli.arg(0, "--json-schema")) == SCHEMA
    assert "101 | Svensk ost | Wrångebäck" in cli.arg(0, "--system-prompt")
    assert cli.arg(0, "--tools") == "Read" and "--restricted" in cli.calls[0]["cmd"]
    assert "--bare" not in cli.calls[0]["cmd"]          # --bare slår OAuth-login fra
    assert cli.calls[0]["files"] == ["bilag.pdf"]       # kun bilaget ligger i arbejdsmappen
    assert "bilag.pdf" in cli.calls[0]["input"]


def test_text_result_with_code_fence_is_accepted():
    ex, _ = make_extractor(["```json\n" + json.dumps(VALID) + "\n```"])
    assert ex.extract("seddel.pdf", "application/pdf", b"%PDF").dokumenttype == "slutseddel"


def test_invalid_json_is_retried_once():
    ex, cli = make_extractor(["{ødelagt", VALID])
    assert ex.extract("seddel.pdf", "application/pdf", b"%PDF").dokumenttype == "slutseddel"
    assert len(cli.calls) == 2
    assert "Dit forrige svar kunne ikke bruges" in cli.calls[1]["input"]


def test_gives_up_after_two_invalid_answers():
    ex, cli = make_extractor(["nope", "stadig nope"])
    with pytest.raises(ExtractionError, match="ugyldig JSON to gange") as info:
        ex.extract("seddel.pdf", "application/pdf", b"%PDF")
    assert len(cli.calls) == 2 and info.value.transient is False


def test_login_or_quota_errors_are_retried_later():
    ex, _ = make_extractor([{"raw": {"type": "result", "subtype": "success", "is_error": True,
                                     "result": "Failed to authenticate"}}])
    with pytest.raises(ExtractionError, match="Failed to authenticate") as info:
        ex.extract("seddel.pdf", "application/pdf", b"%PDF")
    assert info.value.transient is True


def test_excel_is_sent_as_text_without_files():
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.append(["Lihmskov", 125])
    buf = io.BytesIO()
    wb.save(buf)
    ex, cli = make_extractor([VALID])
    ex.extract("slutseddel.xlsx", "", buf.getvalue())
    assert cli.calls[0]["files"] == [] and "Lihmskov\t125" in cli.calls[0]["input"]


# --- forberedelse af vedhæftninger -----------------------------------------

def _jpeg(width, height) -> bytes:
    import os
    img = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def test_large_images_are_downscaled(tmp_path):
    intro = prepare_input("foto.HEIC.jpg", "image/jpeg", _jpeg(4000, 3000), str(tmp_path))
    assert "bilag.jpg" in intro
    img = Image.open(tmp_path / "bilag.jpg")
    assert max(img.size) == MAX_IMAGE_EDGE and img.format == "JPEG"


def test_tiny_images_such_as_signature_logos_are_skipped(tmp_path):
    buf = io.BytesIO()
    Image.new("RGB", (60, 20), "white").save(buf, "PNG")
    assert prepare_input("logo.png", "image/png", buf.getvalue(), str(tmp_path)) is None


def test_excel_is_read_as_text():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Varer", "Antal", "Indkøbspris"])
    ws.append(["Svensk ost", 338.85, 21.3])
    ws.append(["Lihmskov", 125, 73])
    buf = io.BytesIO()
    wb.save(buf)
    text = xlsx_to_text(buf.getvalue())
    assert "Svensk ost\t338.85\t21.3" in text
    assert "Lihmskov\t125\t73" in text


def test_unsupported_file_type(tmp_path):
    with pytest.raises(UnsupportedAttachment):
        prepare_input("noter.docx", "application/vnd.openxmlformats-officedocument."
                      "wordprocessingml.document", b"PK", str(tmp_path))


def test_token_is_cleaned_of_line_breaks_from_terminal_copy():
    from lager.extraction import cli_env
    env = cli_env({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-abc\r\ndef \n", "PATH": "x"})
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-abcdef"
    assert env["PATH"] == "x"
