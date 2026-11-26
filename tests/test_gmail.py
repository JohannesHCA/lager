import base64

import pytest

from lager.gmail import is_authentic, parse_message
from lager.sheets import arg_separator, low_stock_rule_formula, stock_formula


def test_formulas_use_semicolons_in_danish_sheets():
    assert arg_separator("da_DK") == ";" and arg_separator("en_US") == ","
    danish = stock_formula(";")
    assert "SUMIF('Bevægelser'!D2:D;A2:A;'Bevægelser'!E2:E)" in danish
    assert "," not in danish
    assert low_stock_rule_formula(";") == '=AND($A2<>"";ISNUMBER($J2);$L2<$J2)'


@pytest.mark.parametrize("headers, ok", [
    (["mx.google.com; dkim=pass header.i=@example.com header.s=x; spf=pass "
      "smtp.mailfrom=elias@example.com; dmarc=pass (p=NONE) header.from=example.com"], True),
    (["mx.google.com;\n       spf=pass (google.com: domain of elias@example.com designates 1.2.3.4) "
      "smtp.mailfrom=elias@example.com"], True),
    (["mx.google.com; dkim=pass header.i=@mail.example.com"], True),
    (["mx.google.com; dkim=pass header.i=@evil.com; spf=fail smtp.mailfrom=x@evil.com; "
      "dmarc=fail header.from=example.com"], False),
    (["mx.google.com; dkim=pass header.i=@notexample.com"], False),
    (["evil.example; dkim=pass header.i=@example.com; dmarc=pass header.from=example.com"], False),
    ([], False),
])
def test_is_authentic(headers, ok):
    assert is_authentic(headers, "elias@example.com") is ok


def test_parse_message_finds_nested_attachments():
    raw = {
        "id": "abc", "threadId": "t1", "labelIds": ["INBOX"], "internalDate": "1763290800000",
        "payload": {
            "headers": [{"name": "From", "value": "Elias <Elias@example.com>"},
                        {"name": "Subject", "value": "Re: [Lager] Uge 47"},
                        {"name": "Message-ID", "value": "<x@example.com>"},
                        {"name": "Authentication-Results", "value": "mx.google.com; dkim=pass"}],
            "parts": [
                {"mimeType": "multipart/alternative", "filename": "", "parts": [
                    {"mimeType": "text/plain", "filename": "", "body": {"data": "aGVq"}}]},
                {"mimeType": "application/pdf", "filename": "seddel.pdf",
                 "body": {"attachmentId": "A1"}},
                {"mimeType": "image/png", "filename": "logo.png",
                 "body": {"data": base64.urlsafe_b64encode(b"png").decode()}},
            ],
        },
    }
    mail = parse_message(raw, lambda att_id: b"%PDF" if att_id == "A1" else b"")
    assert mail.sender == "elias@example.com"
    assert mail.subject == "Re: [Lager] Uge 47"
    assert mail.date == "2025-11-16"
    assert [(a.filename, a.data) for a in mail.attachments] == [
        ("seddel.pdf", b"%PDF"), ("logo.png", b"png")]
    assert mail.is_system is False


def test_new_product_rows_skip_the_stock_formula_column():
    from lager.sheets import HEADERS, PRODUCTS, Sheets
    sheets = Sheets.__new__(Sheets)
    sheets.headers = {PRODUCTS: HEADERS[PRODUCTS]}
    sheets._sheet_ids, sheets._row_counts = {PRODUCTS: 7}, {PRODUCTS: 1000}
    reqs = sheets.product_row_requests([{"Varenr": 104, "Navn": "Arty", "Noter": "auto"}], 5)
    ranges = [r["updateCells"]["range"] for r in reqs]
    stock = HEADERS[PRODUCTS].index("Aktuel beholdning")
    assert [(r["startColumnIndex"], r["endColumnIndex"]) for r in ranges] == [
        (0, stock), (stock + 1, len(HEADERS[PRODUCTS]))]
    assert all(r["startRowIndex"] == 4 and r["sheetId"] == 7 for r in ranges)
    first = reqs[0]["updateCells"]["rows"][0]["values"]
    assert first[0] == {"userEnteredValue": {"numberValue": 104}}
    assert reqs[1]["updateCells"]["rows"][0]["values"][-1] == {"userEnteredValue": {"stringValue": "auto"}}


class _Call:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class _FakeValuesApi:
    def __init__(self, column_a):
        self.column_a, self.updates = column_a, []

    def values(self):
        return self

    def get(self, **kw):
        return _Call({"values": self.column_a})

    def batchUpdate(self, spreadsheetId, body):
        self.updates.append(body)
        return _Call({})


def test_update_products_overwrites_only_given_columns():
    from lager.sheets import HEADERS, PRODUCTS, Sheets, col_letter
    sheets = Sheets.__new__(Sheets)
    sheets.id, sheets.headers = "S", {PRODUCTS: HEADERS[PRODUCTS]}
    sheets.api = _FakeValuesApi([["Varenr"], [1021], [], [1022]])
    missing = sheets.update_products([
        {"Varenr": 1021, "Aliaser": "Wrångebäck, Tegel", "Startbeholdning": 526.75,
         "Aktuel beholdning": 999},
        {"Varenr": 1022, "Navn": "Søblå", "Startbeholdning": ""},
        {"Varenr": 9999, "Navn": "Findes ikke"},
    ])
    assert missing == ["9999"]
    cells = {d["range"]: d["values"][0][0] for d in sheets.api.updates[0]["data"]}
    col = lambda h: col_letter(HEADERS[PRODUCTS].index(h))
    assert cells == {f"'Produkter'!{col('Aliaser')}2": "Wrångebäck, Tegel",
                     f"'Produkter'!{col('Startbeholdning')}2": 526.75,
                     f"'Produkter'!{col('Navn')}4": "Søblå",
                     f"'Produkter'!{col('Startbeholdning')}4": ""}
    assert sheets.api.updates[0]["valueInputOption"] == "RAW"


def test_delete_products_removes_rows_bottom_up():
    from lager.sheets import PRODUCTS, Sheets

    class Api(_FakeValuesApi):
        def batchUpdate(self, spreadsheetId, body):
            self.updates.append(body)
            return _Call({})

    sheets = Sheets.__new__(Sheets)
    sheets.id, sheets._sheet_ids = "S", {PRODUCTS: 7}
    sheets.api = Api([["Varenr"], [1007], [1008], [1009]])
    assert sheets.delete_products(["1007", "1009", "42"]) == ["42"]
    ranges = [r["deleteDimension"]["range"] for r in sheets.api.updates[0]["requests"]]
    assert [(r["startIndex"], r["endIndex"]) for r in ranges] == [(3, 4), (1, 2)]
