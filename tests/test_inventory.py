import pytest
from conftest import PRODUCT_ROWS, sale_doc

from lager.extraction import Document, Line
from lager.inventory import (below_minimum, build_receipt, convert, normalize_unit,
                             parse_omregning, parse_products, plan_approvals, plan_document,
                             stock_levels)

PRODUCTS = parse_products(PRODUCT_ROWS)


def plan(doc, threshold=0.85):
    return plan_document(doc, filename="f.pdf", source="gmail:m1/f.pdf#abc", line_prefix="m1-abc",
                         products=PRODUCTS, threshold=threshold, fallback_date="2026-11-16",
                         now="2026-11-18 10:00")


# --- enheder og omregning ----------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Kasser", "kasse"), ("fl.", "flaske"), ("X", "stk"), (" KG ", "kg"), ("100 g", "100 g"),
])
def test_normalize_unit(raw, expected):
    assert normalize_unit(raw) == expected


def test_parse_omregning_handles_danish_decimal_comma():
    assert parse_omregning("kasse=6, kg=2,5; Palle = 40") == {"kasse": 6, "kg": 2.5, "palle": 40}


@pytest.mark.parametrize("antal, enhed, varenr, expected", [
    (338.85, "", "101", 338.85),     # slutseddel: tæller allerede i 100 g
    (15, "kg", "101", 150),          # leverance i kg -> 100 g
    (4, "kasser", "103", 24),        # kasser -> flasker
    (18, "flasker", "102", 18),
    (110, "X", "102", 110),          # 'stk'/'x' svarer til flasker
    (2, "palle", "102", None),       # ukendt enhed -> til gennemgang
])
def test_convert(antal, enhed, varenr, expected):
    assert convert(antal, enhed, PRODUCTS[varenr]) == expected


# --- matching-regler ---------------------------------------------------------

def test_sale_lines_are_subtracted_and_unsure_lines_reviewed():
    res = plan(sale_doc())
    assert res.move_type == "Salg"
    assert res.dato == "2026-09-04 – 2026-09-06"
    assert [(m["Varenr"], m["Antal"]) for m in res.moves] == [(101, -338.85), (102, -125)]
    assert len(res.reviews) == 1
    review = res.reviews[0]
    assert review["Originaltekst"] == "Arty 5" and review["Status"] == "Ny"
    assert "Ingen sikker vare" in review["Årsag"]
    assert review["Linje-id"] == "m1-abc-3"


def test_delivery_adds_and_converts_units():
    doc = Document("leverance", "", "", "Leverandør A", [
        Line("15 kg. Wrångeback (2 hele)", 15, "kg", "101", 0.92),
        Line("4 kasser Signature", 4, "kasser", "103", 0.99),
    ])
    res = plan(doc)
    assert res.dato == "2026-11-16"   # ingen dato på bilaget -> mailens dato
    assert [(m["Varenr"], m["Antal"], m["Type"]) for m in res.moves] == [
        (101, 150, "Leverance"), (103, 24, "Leverance")]


@pytest.mark.parametrize("line, reason", [
    (Line("Lihmskov 125", 125, "", "102", 0.84), "Lav sikkerhed"),
    (Line("Ny vare 3", 3, "", "999", 0.99), "findes ikke i Produkter"),
    (Line("Alle små olivenolier", None, "", "102", 0.99), "Antal mangler"),
    (Line("Lihmskov -2", -2, "", "102", 0.99), "0 eller negativt"),
    (Line("2 paller Lihmskov", 2, "palle", "102", 0.99), "kan ikke omregnes"),
])
def test_lines_that_go_to_review(line, reason):
    res = plan(Document("leverance", "", "", "", [line]))
    assert res.moves == []
    assert reason in res.reviews[0]["Årsag"]


def test_threshold_is_configurable():
    doc = Document("slutseddel", "", "", "", [Line("Lihmskov 1", 1, "", "102", 0.7)])
    assert plan(doc, threshold=0.85).moves == []
    assert len(plan(doc, threshold=0.6).moves) == 1


def test_unknown_document_type_never_changes_stock():
    res = plan(Document("ukendt", "", "", "", [Line("Lihmskov 3", 3, "", "102", 0.99)]))
    assert res.moves == [] and len(res.reviews) == 1


# --- beholdning og kvittering -------------------------------------------------

def test_stock_and_minimum():
    levels = stock_levels(PRODUCTS, [{"Varenr": 102, "Antal": -125}, {"Varenr": "101", "Antal": 10}])
    assert levels == {"101": 410, "102": 5, "103": 10}
    assert [(p.varenr, lvl) for p, lvl in below_minimum(PRODUCTS, levels)] == [("102", 5)]


def test_receipt_lists_moves_reviews_errors_and_low_stock():
    res = plan(sale_doc())
    levels = stock_levels(PRODUCTS, res.moves)
    text = build_receipt([res], errors=[("foto.heic", "billedet kunne ikke åbnes")], notes=[],
                         products=PRODUCTS, low_stock=below_minimum(PRODUCTS, levels),
                         sheet_url="https://sheet")
    assert "Slutseddel · Food Festival Aarhus · 2026-09-04 – 2026-09-06" in text
    assert "-338,85 100 g  Svensk ost (101)" in text
    assert "-125 flaske  Lihmskov (102)" in text
    assert "\"Arty 5\" – Ingen sikker vare" in text
    assert "foto.heic: billedet kunne ikke åbnes" in text
    assert "Lihmskov (102): 5 flaske (minimum 20)" in text


# --- godkendelse fra "Til gennemgang" ----------------------------------------

def test_approved_review_lines_are_booked_exactly_once():
    review = [
        (2, {"Type": "Salg", "Antal": 5, "Enhed": "", "Foreslået varenr": "", "Status": "Godkendt",
             "Godkendt varenr": 103, "Godkendt antal": "", "Linje-id": "m1-abc-3"}),
        (3, {"Type": "Leverance", "Antal": 2, "Enhed": "palle", "Status": "Godkendt",
             "Godkendt varenr": 102, "Godkendt antal": "", "Linje-id": "m1-abc-4"}),
        (4, {"Type": "Salg", "Status": "Ny", "Linje-id": "m1-abc-5"}),
    ]
    sources: set[str] = set()
    moves, updates = plan_approvals(review, PRODUCTS, sources, "nu")
    assert [(m["Varenr"], m["Antal"], m["Kilde"]) for m in moves] == [
        (103, -5, "gennemgang:m1-abc-3")]
    assert updates[0] == (2, "Bogført")
    assert updates[1][0] == 3 and updates[1][1].startswith("Fejl: udfyld Godkendt antal")

    # Genkørsel (fx hvis statusopdateringen fejlede): ingen ny bevægelse.
    moves_again, updates_again = plan_approvals(review[:1], PRODUCTS, sources, "nu")
    assert moves_again == [] and updates_again == [(2, "Bogført")]


# --- nye varer oprettes automatisk ---------------------------------------------

def fresh_products():
    return parse_products(PRODUCT_ROWS)


def plan_new(doc, products, threshold=0.85):
    return plan_document(doc, filename="seddel.jpg", source="gmail:m1/seddel.jpg#abc",
                         line_prefix="m1-abc", products=products, threshold=threshold,
                         fallback_date="2026-11-26", now="2026-11-26 15:00")


def test_new_product_is_created_and_booked():
    products = fresh_products()
    doc = Document("slutseddel", "", "", "Julemarked", [
        Line("Fransk Kastanjecreme 8", 8, "", None, 0.95, varenavn="Fransk kastanjecreme",
             ny_vare=True),
        Line("Kastanjecreme 2", 2, "", None, 0.95, varenavn="fransk  KASTANJECREME", ny_vare=True),
    ])
    res = plan_new(doc, products)
    assert res.new_products == [{"Varenr": 104, "Navn": "Fransk kastanjecreme", "Enhed": "",
                                 "Noter": "Oprettet automatisk 2026-11-26 fra seddel.jpg"}]
    assert [(m["Varenr"], m["Antal"]) for m in res.moves] == [(104, -8), (104, -2)]
    assert "104" in products and res.reviews == []


def test_known_name_or_alias_is_reused_instead_of_duplicated():
    products = fresh_products()
    doc = Document("leverance", "", "", "", [
        Line("15 kg Wrångeback", 15, "kg", None, 0.95, varenavn="Wrångeback", ny_vare=True)])
    res = plan_new(doc, products)
    assert res.new_products == [] and res.moves[0]["Varenr"] == 101 and res.moves[0]["Antal"] == 150


@pytest.mark.parametrize("line, reason", [
    (Line("Ost?", 3, "", None, 0.95, varenavn="Ost", ny_vare=False), "Ingen sikker vare"),
    (Line("Arty 5", 5, "", None, 0.5, varenavn="Arty", ny_vare=True), "for usikker"),
    (Line("Arty", None, "", None, 0.95, varenavn="Arty", ny_vare=True), "for usikker"),
])
def test_unsure_new_products_still_go_to_review(line, reason):
    products = fresh_products()
    res = plan_new(Document("slutseddel", "", "", "", [line]), products)
    assert res.new_products == [] and res.moves == [] and reason in res.reviews[0]["Årsag"]
    assert len(products) == 3


def test_new_product_number_follows_highest_existing():
    from lager.inventory import next_varenr
    assert next_varenr(fresh_products()) == "104"
    assert next_varenr({}) == "1001"
