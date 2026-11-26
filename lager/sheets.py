"""Google Sheets: oprettelse af ark, samlet læsning og atomiske batch-skrivninger."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import TIMEZONE

log = logging.getLogger(__name__)

PRODUCTS = "Produkter"
MOVES = "Bevægelser"
REVIEW = "Til gennemgang"
PROCESSED = "Behandlede mails"
LOCATIONS = "Lokationer"
STATS = "Statistik"  # skrives helt om af systemet; ingen fast overskrift

HEADERS = {
    PRODUCTS: ["Varenr", "Navn", "Aliaser", "Kategori", "Enhed", "Omregning", "Kostpris",
               "Salgspris", "Leverandør", "Minimumsbeholdning", "Startbeholdning",
               "Aktuel beholdning", "Noter"],
    MOVES: ["Tidspunkt", "Dato", "Type", "Varenr", "Antal", "Kilde", "Bilagstype",
            "Sikkerhed", "Status", "Stand/Leverandør", "Originaltekst", "Lokation"],
    REVIEW: ["Tidspunkt", "Dato", "Type", "Stand/Leverandør", "Originaltekst", "Antal", "Enhed",
             "Foreslået varenr", "Sikkerhed", "Årsag", "Kilde", "Status", "Godkendt varenr",
             "Godkendt antal", "Linje-id", "Lokation"],
    PROCESSED: ["Message-id", "Tidspunkt", "Afsender", "Emne", "Resultat"],
    LOCATIONS: ["Lokation", "Gruppe", "Andre navne"],
}
# Ark der skal findes, men som ikke har en fast overskriftsrække.
EXTRA_SHEETS = [STATS]
REVIEW_STATUSES = ["Ny", "Godkendt", "Afvist", "Bogført", "Erstattet"]


def col_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _col(sheet: str, header: str) -> str:
    return col_letter(HEADERS[sheet].index(header))


# Sprog med decimalkomma (fx dansk) bruger ';' mellem argumenter i formler.
_COMMA_DECIMAL = ("da", "de", "fr", "es", "it", "nl", "sv", "nb", "no", "nn", "fi", "pt", "pl",
                  "cs", "sk", "ru", "tr", "is", "et", "lv", "lt", "hu", "ro", "hr", "sl", "el",
                  "uk", "bg", "id", "vi")


def arg_separator(locale: str) -> str:
    return ";" if (locale or "").split("_")[0].lower() in _COMMA_DECIMAL else ","


def stock_formula(sep: str = ",") -> str:
    """Aktuel beholdning = Startbeholdning + SUMIF over Bevægelser (som array-formel i overskriften)."""
    a, k = _col(PRODUCTS, "Varenr"), _col(PRODUCTS, "Startbeholdning")
    d, e = _col(MOVES, "Varenr"), _col(MOVES, "Antal")
    # ';' mellem de to elementer i {…} betyder "ny række" i alle sprog.
    return (f'={{"Aktuel beholdning";ARRAYFORMULA(IF({a}2:{a}=""{sep}""{sep}'
            f'IF(ISNUMBER({k}2:{k}){sep}{k}2:{k}{sep}0)'
            f"+SUMIF('{MOVES}'!{d}2:{d}{sep}{a}2:{a}{sep}'{MOVES}'!{e}2:{e})))}}")


def low_stock_rule_formula(sep: str = ",") -> str:
    a, j, l = (_col(PRODUCTS, h) for h in ("Varenr", "Minimumsbeholdning", "Aktuel beholdning"))
    return f'=AND(${a}2<>""{sep}ISNUMBER(${j}2){sep}${l}2<${j}2)'


def cell(value) -> dict:
    """Værdi til Sheets-celle. Tekst skrives altid som tekst, så '=...' aldrig bliver en formel."""
    if value is None or value == "":
        return {}
    if isinstance(value, bool):
        return {"userEnteredValue": {"stringValue": "Ja" if value else "Nej"}}
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": str(value)}}


@dataclass
class SheetData:
    headers: dict[str, list[str]]
    products: list[dict]
    moves: list[dict]
    review: list[tuple[int, dict]]          # (rækkenummer i arket, række)
    processed_ids: set[str] = field(default_factory=set)
    locations: list[dict] = field(default_factory=list)


def _rows_to_dicts(rows: list[list]) -> tuple[list[str], list[dict]]:
    if not rows:
        return [], []
    header = [str(h).strip() for h in rows[0]]
    out = []
    for row in rows[1:]:
        if not any(v not in ("", None) for v in row):
            out.append(None)
            continue
        out.append({h: (row[i] if i < len(row) else "") for i, h in enumerate(header) if h})
    return header, out


class Sheets:
    def __init__(self, creds, sheet_id: str):
        from googleapiclient.discovery import build

        self.api = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
        self.id = sheet_id
        self.headers: dict[str, list[str]] = {}
        self._sheet_ids: dict[str, int] | None = None
        self._row_counts: dict[str, int] = {}
        self.next_product_row = 2

    @staticmethod
    def create(creds, title: str = "Lager") -> str:
        from googleapiclient.discovery import build

        api = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
        body = {"properties": {"title": title, "locale": "da_DK", "timeZone": TIMEZONE},
                "sheets": [{"properties": {"title": name}} for name in [*HEADERS, *EXTRA_SHEETS]]}
        return api.create(body=body, fields="spreadsheetId").execute()["spreadsheetId"]

    def sheet_ids(self) -> dict[str, int]:
        if self._sheet_ids is None:
            meta = self.api.get(spreadsheetId=self.id, fields=(
                "sheets(properties(sheetId,title,gridProperties(rowCount)))")).execute()
            self._sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"]
                               for s in meta.get("sheets", [])}
            self._row_counts = {s["properties"]["title"]:
                                s["properties"].get("gridProperties", {}).get("rowCount", 1000)
                                for s in meta.get("sheets", [])}
        return self._sheet_ids

    def row_count(self, name: str) -> int:
        self.sheet_ids()
        return self._row_counts.get(name, 1000)

    # --- setup ---------------------------------------------------------
    def setup(self, dry_run: bool = False) -> list[str]:
        """Opret manglende ark, overskrifter, formel og formatering. Returnerer en log.

        Kan køres igen når som helst: eksisterende data røres ikke, og formel og
        formatering sættes (igen), hvis de mangler eller er forkerte.
        """
        done: list[str] = []
        missing = [name for name in [*HEADERS, *EXTRA_SHEETS] if name not in self.sheet_ids()]
        if missing:
            done.append("Opretter ark: " + ", ".join(missing))
            if not dry_run:
                self.api.batchUpdate(spreadsheetId=self.id, body={"requests": [
                    {"addSheet": {"properties": {"title": name}}} for name in missing]}).execute()
                self._sheet_ids = None
        if dry_run and missing:
            return done + ["Overskrifter, formel og formatering skrives til de nye ark."]

        meta = self.api.get(spreadsheetId=self.id, fields=(
            "properties(locale),sheets(properties(sheetId,title),conditionalFormats)")).execute()
        sep = arg_separator(meta.get("properties", {}).get("locale", ""))
        has_rules = {s["properties"]["title"]: bool(s.get("conditionalFormats"))
                     for s in meta.get("sheets", [])}

        first_rows = self.api.values().batchGet(
            spreadsheetId=self.id, ranges=[f"'{name}'!1:1" for name in HEADERS]).execute()
        empty, extend = [], {}
        for name, vr in zip(HEADERS, first_rows.get("valueRanges", [])):
            existing = [str(h).strip() for h in (vr.get("values") or [[]])[0]]
            if not existing:
                empty.append(name)
                continue
            new_cols = [h for h in HEADERS[name] if h not in existing]
            if new_cols:  # nye kolonner i en senere version tilføjes bagerst
                extend[name] = (len(existing), new_cols)
        if empty:
            done.append("Skriver overskrifter i: " + ", ".join(empty))
        for name, (_, cols) in extend.items():
            done.append(f"Tilføjer kolonner i {name}: " + ", ".join(cols))
        if dry_run:
            return done + ["Formel og formatering kontrolleres og sættes om nødvendigt."]

        stock_col = _col(PRODUCTS, "Aktuel beholdning")
        data = [{"range": f"'{name}'!A1", "values": [HEADERS[name]]} for name in empty]
        data += [{"range": f"'{name}'!{col_letter(start)}1", "values": [cols]}
                 for name, (start, cols) in extend.items()]
        # Beholdningsformlen skrives altid, så en ødelagt eller forkert formel bliver rettet.
        data.append({"range": f"'{PRODUCTS}'!{stock_col}1", "values": [[stock_formula(sep)]]})
        self.api.values().batchUpdate(spreadsheetId=self.id, body={
            "valueInputOption": "USER_ENTERED", "data": data}).execute()

        ids = self.sheet_ids()
        requests = []
        for name in HEADERS:
            sid = ids[name]
            requests += [
                {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                                "fields": "userEnteredFormat.textFormat.bold"}},
                {"updateSheetProperties": {"properties": {"sheetId": sid,
                                                          "gridProperties": {"frozenRowCount": 1}},
                                           "fields": "gridProperties.frozenRowCount"}},
            ]
        if not has_rules.get(PRODUCTS):
            requests.append({"addConditionalFormatRule": {"index": 0, "rule": {
                "ranges": [{"sheetId": ids[PRODUCTS], "startRowIndex": 1}],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA", "values": [
                        {"userEnteredValue": low_stock_rule_formula(sep)}]},
                    "format": {"backgroundColor": {"red": 0.96, "green": 0.78, "blue": 0.76}}}}}})
        status_idx = HEADERS[REVIEW].index("Status")
        requests.append({"setDataValidation": {
            "range": {"sheetId": ids[REVIEW], "startRowIndex": 1,
                      "startColumnIndex": status_idx, "endColumnIndex": status_idx + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [
                {"userEnteredValue": s} for s in REVIEW_STATUSES]},
                "strict": False, "showCustomUi": True}}})
        self.api.batchUpdate(spreadsheetId=self.id, body={"requests": requests}).execute()

        check = self.api.values().get(spreadsheetId=self.id,
                                      range=f"'{PRODUCTS}'!{stock_col}1").execute()
        shown = (check.get("values") or [[""]])[0][0]
        if shown != "Aktuel beholdning":
            done.append(f"ADVARSEL: beholdningsformlen viser {shown!r} – tjek kolonne {stock_col}.")
        else:
            done.append("Beholdningsformel, formatering og statusliste er sat op.")
        return done

    # --- læsning -------------------------------------------------------
    def read_all(self) -> SheetData:
        names = [n for n in HEADERS if n != LOCATIONS or LOCATIONS in self.sheet_ids()]
        ranges = [f"'{name}'" for name in names]
        resp = self.api.values().batchGet(spreadsheetId=self.id, ranges=ranges,
                                          valueRenderOption="UNFORMATTED_VALUE").execute()
        parsed = {LOCATIONS: []}
        for name, vr in zip(names, resp.get("valueRanges", [])):
            header, rows = _rows_to_dicts(vr.get("values", []))
            if not header:
                raise SystemExit(f"Arket '{name}' mangler overskrifter – kør 'setup' først.")
            self.headers[name] = header
            parsed[name] = rows
        filled = [i for i, r in enumerate(parsed[PRODUCTS]) if r]
        self.next_product_row = (filled[-1] + 3) if filled else 2  # 1-baseret arkrække
        return SheetData(
            headers=dict(self.headers),
            products=[r for r in parsed[PRODUCTS] if r],
            moves=[r for r in parsed[MOVES] if r],
            review=[(i + 2, r) for i, r in enumerate(parsed[REVIEW]) if r],
            processed_ids={str(r.get("Message-id", "")).strip()
                           for r in parsed[PROCESSED] if r and r.get("Message-id")},
            locations=[r for r in parsed[LOCATIONS] if r],
        )

    # --- skrivning -----------------------------------------------------
    def product_row_requests(self, rows: list[dict], start_row: int) -> list[dict]:
        """updateCells for nye varer fra start_row (1-baseret), uden om kolonnen med
        beholdningsformlen, så dens array-formel ikke blokeres."""
        header = self.headers.get(PRODUCTS) or HEADERS[PRODUCTS]
        skip = header.index("Aktuel beholdning") if "Aktuel beholdning" in header else None
        sid = self.sheet_ids()[PRODUCTS]
        segments = [(0, skip), (skip + 1, len(header))] if skip is not None else [(0, len(header))]
        requests = []
        for lo, hi in segments:
            if lo >= hi:
                continue
            requests.append({"updateCells": {
                "range": {"sheetId": sid, "startRowIndex": start_row - 1,
                          "endRowIndex": start_row - 1 + len(rows),
                          "startColumnIndex": lo, "endColumnIndex": hi},
                "rows": [{"values": [cell(row.get(h)) for h in header[lo:hi]]} for row in rows],
                "fields": "userEnteredValue"}})
        return requests

    def append_atomic(self, rows_by_sheet: dict[str, list[dict]]) -> None:
        """Tilføj rækker til flere ark i ét batchUpdate-kald (alt eller intet)."""
        ids = self.sheet_ids()
        requests = []
        for name, rows in rows_by_sheet.items():
            if not rows:
                continue
            header = self.headers.get(name) or HEADERS[name]
            if name == PRODUCTS:
                # appendCells ville lande under formlens udfyldte kolonne (række 1000+).
                start = getattr(self, "next_product_row", 2)
                if start - 1 + len(rows) > self.row_count(PRODUCTS):
                    requests.append({"appendDimension": {"sheetId": ids[name], "dimension": "ROWS",
                                                         "length": len(rows) + 100}})
                    self._row_counts[PRODUCTS] = self.row_count(PRODUCTS) + len(rows) + 100
                requests += self.product_row_requests(rows, start)
                self.next_product_row = start + len(rows)
                continue
            requests.append({"appendCells": {
                "sheetId": ids[name],
                "rows": [{"values": [cell(row.get(h)) for h in header]} for row in rows],
                "fields": "userEnteredValue"}})
        if requests:
            self.api.batchUpdate(spreadsheetId=self.id, body={"requests": requests}).execute()

    def _product_rows(self) -> dict[str, int]:
        """Varenr -> rækkenummer (1-baseret) i Produkter."""
        col_a = self.api.values().get(spreadsheetId=self.id, range=f"'{PRODUCTS}'!A:A",
                                      valueRenderOption="UNFORMATTED_VALUE").execute()
        row_of: dict[str, int] = {}
        for i, value in enumerate(col_a.get("values", [])[1:], start=2):
            if value and value[0] not in ("", None):
                v = value[0]
                key = str(int(v)) if isinstance(v, float) and v.is_integer() else str(v).strip()
                row_of.setdefault(key, i)
        return row_of

    def delete_products(self, varenrs: list[str]) -> list[str]:
        """Slet hele rækken for hver vare. Returnerer varenr, der ikke blev fundet."""
        row_of = self._product_rows()
        rows = sorted({row_of[v] for v in varenrs if v in row_of}, reverse=True)
        sid = self.sheet_ids()[PRODUCTS]
        requests = [{"deleteDimension": {"range": {"sheetId": sid, "dimension": "ROWS",
                                                   "startIndex": r - 1, "endIndex": r}}}
                    for r in rows]  # nedefra, så rækkenumrene ikke flytter sig undervejs
        if requests:
            self.api.batchUpdate(spreadsheetId=self.id, body={"requests": requests}).execute()
        return [v for v in varenrs if v not in row_of]

    def update_products(self, rows: list[dict]) -> list[str]:
        """Overskriv kolonnerne i `rows` for varerne med samme Varenr. Returnerer ukendte varenr.

        Kolonnen 'Aktuel beholdning' (formlen) kan ikke overskrives.
        """
        header = self.headers.get(PRODUCTS) or HEADERS[PRODUCTS]
        row_of = self._product_rows()
        data, missing = [], []
        for row in rows:
            key = str(row.get("Varenr", "")).strip()
            if key not in row_of:
                missing.append(key)
                continue
            for name, value in row.items():
                if name in ("Varenr", "Aktuel beholdning") or name not in header:
                    continue
                col = col_letter(header.index(name))
                data.append({"range": f"'{PRODUCTS}'!{col}{row_of[key]}",
                             "values": [["" if value is None else value]]})
        if data:
            self.api.values().batchUpdate(spreadsheetId=self.id, body={
                "valueInputOption": "RAW", "data": data}).execute()
        return missing

    def write_stats(self, rows: list[list]) -> bool:
        """Skriv statistikken forfra i arket Statistik. False hvis arket ikke findes endnu."""
        if STATS not in self.sheet_ids():
            log.warning("Arket '%s' findes ikke – kør setup for at få statistik.", STATS)
            return False
        width = max((len(r) for r in rows), default=1)
        padded = [list(r) + [""] * (width - len(r)) for r in rows]
        self.api.values().clear(spreadsheetId=self.id, range=f"'{STATS}'").execute()
        self.api.values().update(spreadsheetId=self.id, range=f"'{STATS}'!A1",
                                 valueInputOption="RAW", body={"values": padded}).execute()
        return True

    def update_review_status(self, updates: list[tuple[int, str]]) -> None:
        if not updates:
            return
        header = self.headers.get(REVIEW) or HEADERS[REVIEW]
        col = col_letter(header.index("Status"))
        data = [{"range": f"'{REVIEW}'!{col}{row}", "values": [[status]]}
                for row, status in updates]
        self.api.values().batchUpdate(spreadsheetId=self.id, body={
            "valueInputOption": "RAW", "data": data}).execute()
