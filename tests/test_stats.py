"""Lokationer og statistik pr. lokation/varegruppe."""
import json

from conftest import FakeExtractor, FakeGmail, FakeSheets, make_config, make_mail

from lager.extraction import Document, Line, build_system_prompt, parse_document
from lager.inventory import (UNKNOWN_LOCATION, compute_stats, match_location, parse_locations,
                             parse_products)
from lager.main import run_import_locations, run_process
from lager.sheets import LOCATIONS, MOVES

LOCATION_ROWS = [
    {"Lokation": "Torvehallerne", "Gruppe": "", "Andre navne": "Torvehallerne KBH"},
    {"Lokation": "Amagercentret", "Gruppe": "", "Andre navne": "Amager Centret"},
    {"Lokation": "Plantorama Amager", "Gruppe": "Plantorama", "Andre navne": ""},
    {"Lokation": "Plantorama Odense", "Gruppe": "Plantorama", "Andre navne": ""},
]
LOCS = parse_locations(LOCATION_ROWS)

PRODUCTS = parse_products([
    {"Varenr": 1, "Navn": "Svensk ost", "Kategori": "Ost", "Enhed": "100 g", "Salgspris": 30},
    {"Varenr": 2, "Navn": "Cognac", "Kategori": "Spiritus", "Enhed": "stk", "Salgspris": 400},
    {"Varenr": 3, "Navn": "Kunsten", "Kategori": "", "Enhed": "stk"},
])


def sale(varenr, antal, lokation):
    return {"Type": "Salg", "Varenr": varenr, "Antal": -antal, "Lokation": lokation}


def test_location_matching_keeps_chain_store_and_mall_apart():
    assert match_location("Plantorama Amager", "", LOCS) == "Plantorama Amager"
    assert match_location("Amager Centret", "", LOCS) == "Amagercentret"
    assert match_location("Stand i Amager", "Amagercentret", LOCS) == "Amagercentret"
    assert match_location("Food Festival Aarhus", "Aarhus", LOCS) == ""   # ukendt forslag


def test_prompt_lists_locations_and_document_carries_location():
    prompt = build_system_prompt({}, LOCS)
    assert "Plantorama Amager | Plantorama |" in prompt
    doc = parse_document(json.dumps({
        "dokumenttype": "slutseddel", "dato_fra": "", "dato_til": "", "stand": "Amager Centret",
        "lokation": "Amagercentret", "linjer": []}))
    assert doc.lokation == "Amagercentret"


def test_stats_revenue_shares_groups_and_unknown_location():
    moves = [sale(1, 10, "Torvehallerne"), sale(2, 1, "Torvehallerne"),
             sale(2, 2, "Plantorama Amager"), sale(1, 20, "Plantorama Odense"),
             sale(3, 5, ""), {"Type": "Leverance", "Varenr": 2, "Antal": 50, "Lokation": ""}]
    rows = compute_stats(PRODUCTS, moves, LOCS, "2026-11-26 16:00")
    as_text = [[str(c) for c in r] for r in rows]
    header_idx = next(i for i, r in enumerate(rows) if r and r[0] == "Varegruppe")
    header = rows[header_idx]
    assert header[1:] == ["Torvehallerne", "Amagercentret", "Plantorama Amager",
                          "Plantorama Odense", UNKNOWN_LOCATION, "Plantorama i alt", "I alt"]
    by_cat = {r[0]: dict(zip(header[1:], r[1:])) for r in rows[header_idx + 1:header_idx + 4]}
    assert by_cat["Ost"]["Torvehallerne"] == 300 and by_cat["Ost"]["Plantorama i alt"] == 600
    assert by_cat["Spiritus"]["Plantorama Amager"] == 800
    assert by_cat["I alt"]["I alt"] == 300 + 400 + 800 + 600
    assert any("Uden salgspris" in r[0] and "Kunsten" in r[0] for r in as_text if r)
    share_idx = next(i for i, r in enumerate(rows) if r and r[0].startswith("Andel"))
    shares = {r[0]: dict(zip(header[1:], r[1:])) for r in rows[share_idx + 2:share_idx + 4]}
    assert shares["Ost"]["Torvehallerne"] == 42.9 and shares["Spiritus"]["Torvehallerne"] == 57.1
    qty_idx = next(i for i, r in enumerate(rows) if r and r[0] == "Vare")
    names = [r[0] for r in rows[qty_idx + 1:]]
    assert names == ["Svensk ost", "Cognac", "Kunsten"]           # Ost, Spiritus, uden gruppe


def test_stats_without_sales_say_so():
    rows = compute_stats(PRODUCTS, [], LOCS, "nu")
    assert ["Ingen salg endnu"] in rows


def test_sales_get_location_and_stats_are_written(cfg):
    gmail, sheets = FakeGmail(), FakeSheets(products=[
        {"Varenr": 2, "Navn": "Cognac", "Kategori": "Spiritus", "Salgspris": 400}])
    sheets.rows[LOCATIONS] = list(LOCATION_ROWS)
    gmail.add_mail(make_mail())
    doc = Document("slutseddel", "", "", "Amager Centret", [Line("Cognac 2", 2, "", "2", 0.95)],
                   lokation="Amagercentret")
    run_process(cfg, gmail, sheets, lambda *a: FakeExtractor({"slutseddel.pdf": doc}),
                dry_run=False)
    assert sheets.rows[MOVES][0]["Lokation"] == "Amagercentret"
    assert "Lokation: Amagercentret" in gmail.sent[0]["body"]
    assert sheets.stats and sheets.stats[0][0].startswith("Statistik over salg")


def test_unknown_location_is_explained_in_receipt(cfg):
    gmail, sheets = FakeGmail(), FakeSheets(products=[{"Varenr": 2, "Navn": "Cognac"}])
    sheets.rows[LOCATIONS] = list(LOCATION_ROWS)
    gmail.add_mail(make_mail())
    doc = Document("slutseddel", "", "", "Julemarked Roskilde",
                   [Line("Cognac 2", 2, "", "2", 0.95)])
    run_process(cfg, gmail, sheets, lambda *a: FakeExtractor({"slutseddel.pdf": doc}),
                dry_run=False)
    assert sheets.rows[MOVES][0]["Lokation"] == ""
    assert "Lokation: ikke fundet" in gmail.sent[0]["body"]


def test_import_locations_skips_existing():
    sheets = FakeSheets()
    sheets.rows[LOCATIONS] = [{"Lokation": "Torvehallerne", "Gruppe": "", "Andre navne": ""}]
    table = "Lokation\tGruppe\tAndre navne\ntorvehallerne\t\t\nPlantorama Odense\tPlantorama\t\n"
    assert run_import_locations(sheets, table, dry_run=False) == 0
    assert [r["Lokation"] for r in sheets.rows[LOCATIONS]] == ["Torvehallerne",
                                                               "Plantorama Odense"]


def test_config_is_unchanged_by_locations():
    assert make_config().emails == ("elias@example.com",)
