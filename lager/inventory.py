"""Ren lagerlogik uden API-kald: produkter, enheder, matching-regler, idempotens og kvittering."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .extraction import Document

log = logging.getLogger(__name__)

SALE, DELIVERY, CORRECTION = "Salg", "Leverance", "Korrektion"
DOC_TYPE_TO_MOVE = {"slutseddel": SALE, "leverance": DELIVERY}

UNIT_ALIASES = {
    "stk": "stk", "styk": "stk", "styks": "stk", "x": "stk", "pcs": "stk", "st": "stk",
    "fl": "flaske", "flaske": "flaske", "flasker": "flaske", "flsk": "flaske",
    "kasse": "kasse", "kasser": "kasse", "ks": "kasse", "krt": "kasse", "karton": "kasse",
    "kartoner": "kasse", "kolli": "kasse",
    "kg": "kg", "kilo": "kg", "kilogram": "kg",
    "g": "g", "gr": "g", "gram": "g",
    "glas": "glas", "dåse": "dåse", "dåser": "dåse", "bøtte": "bøtte", "bøtter": "bøtte",
    "pk": "pakke", "pakke": "pakke", "pakker": "pakke", "pose": "pose", "poser": "pose",
}
# Enheder der alle betyder "ét styk" og derfor kan bruges 1:1 for hinanden.
PIECE_UNITS = {"stk", "flaske", "glas", "dåse", "bøtte", "pakke", "pose"}


def normalize_unit(unit) -> str:
    text = re.sub(r"\s+", " ", str(unit or "").strip().lower()).rstrip(".")
    return UNIT_ALIASES.get(text, text)


def parse_number(value) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def clean_id(value) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value if value is not None else "").strip()


def sheet_varenr(varenr: str):
    """Varenr skrives som tal, hvis det er et rent tal, så SUMIF matcher Produkter-arket."""
    return int(varenr) if varenr.isdigit() and not varenr.startswith("0") and len(varenr) < 16 \
        else varenr


def parse_omregning(text) -> dict[str, float]:
    """'kasse=6, kg=2,5' -> {'kasse': 6.0, 'kg': 2.5}"""
    out = {}
    for key, value in re.findall(r"([^=;,\d][^=;,]*?)\s*=\s*(\d+(?:[.,]\d+)?)", str(text or "")):
        unit = normalize_unit(key)
        if unit:
            out[unit] = float(value.replace(",", "."))
    return out


@dataclass
class Product:
    varenr: str
    navn: str
    aliaser: list[str]
    enhed: str
    omregning: dict[str, float]
    minimum: float | None
    start: float
    kategori: str = ""
    salgspris: float | None = None


def parse_products(rows: list[dict]) -> dict[str, Product]:
    products: dict[str, Product] = {}
    for row in rows:
        varenr = clean_id(row.get("Varenr"))
        if not varenr:
            continue
        if varenr in products:
            log.warning("Varenr %s står flere gange i Produkter – bruger den første", varenr)
            continue
        products[varenr] = Product(
            varenr=varenr,
            navn=str(row.get("Navn") or "").strip(),
            aliaser=[a.strip() for a in str(row.get("Aliaser") or "").split(",") if a.strip()],
            enhed=str(row.get("Enhed") or "").strip(),
            omregning=parse_omregning(row.get("Omregning")),
            minimum=parse_number(row.get("Minimumsbeholdning")),
            start=parse_number(row.get("Startbeholdning")) or 0.0,
            kategori=str(row.get("Kategori") or "").strip(),
            salgspris=parse_number(row.get("Salgspris")),
        )
    return products


# --- lokationer ------------------------------------------------------------------

@dataclass
class Location:
    navn: str
    gruppe: str
    andre_navne: list[str]


def parse_locations(rows: list[dict]) -> list[Location]:
    out, seen = [], set()
    for row in rows:
        name = str(row.get("Lokation") or "").strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        out.append(Location(name, str(row.get("Gruppe") or "").strip(),
                            [a.strip() for a in str(row.get("Andre navne") or "").split(",")
                             if a.strip()]))
    return out


def match_location(stand: str, suggested: str, locations: list[Location]) -> str:
    """Lokationens navn for en stand: Claudes forslag, hvis det findes i listen, ellers et
    præcist navne-match. Tom streng hvis standen ikke kan kobles sikkert."""
    by_key = {}
    for loc in locations:
        for name in [loc.navn, *loc.andre_navne]:
            by_key.setdefault(" ".join(name.casefold().split()), loc.navn)
    for candidate in (suggested, stand):
        key = " ".join(str(candidate or "").casefold().split())
        if key in by_key:
            return by_key[key]
    return ""


def convert(antal: float, enhed: str, product: Product) -> float | None:
    """Omregn til produktets grundenhed. None hvis enheden ikke kan omregnes."""
    unit, base = normalize_unit(enhed), normalize_unit(product.enhed)
    if unit in ("", base):
        return antal
    if unit in product.omregning:
        return antal * product.omregning[unit]
    if unit in PIECE_UNITS and (base in PIECE_UNITS or base == ""):
        return antal
    return None


def stock_levels(products: dict[str, Product], moves: list[dict]) -> dict[str, float]:
    levels = {v: p.start for v, p in products.items()}
    for move in moves:
        varenr = clean_id(move.get("Varenr"))
        qty = parse_number(move.get("Antal"))
        if varenr in levels and qty is not None:
            levels[varenr] += qty
    return levels


def below_minimum(products: dict[str, Product], levels: dict[str, float]) -> list[tuple[Product, float]]:
    return [(p, levels.get(v, 0.0)) for v, p in sorted(products.items())
            if p.minimum is not None and levels.get(v, 0.0) < p.minimum]


# --- idempotens ----------------------------------------------------------

def attachment_source(msg_id: str, filename: str, digest: str) -> str:
    return f"gmail:{msg_id}/{filename}#{digest}"


def known_digests(*row_lists: list[dict]) -> set[str]:
    """Hash af alle vedhæftninger, der allerede står i Bevægelser eller Til gennemgang."""
    digests = set()
    for rows in row_lists:
        for row in rows:
            source = str(row.get("Kilde") or "")
            if source.startswith("gmail:") and "#" in source:
                digests.add(source.rsplit("#", 1)[1])
    return digests


def known_sources(rows: list[dict]) -> set[str]:
    return {str(r.get("Kilde") or "") for r in rows}


# --- planlægning af rækker ----------------------------------------------

@dataclass
class DocResult:
    filename: str
    move_type: str            # Salg / Leverance / "" hvis ukendt
    dato: str
    stand: str
    moves: list[dict] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)
    new_products: list[dict] = field(default_factory=list)   # rækker til arket Produkter
    lokation: str = ""


def fmt_dates(start: str, end: str) -> str:
    if start and end and start != end:
        return f"{start} – {end}"
    return start or end


def fmt_qty(value: float, signed: bool = False) -> str:
    text = f"{value:+.3f}" if signed else f"{value:.3f}"
    return text.rstrip("0").rstrip(".").replace(".", ",")


def _name_key(name: str) -> str:
    return " ".join(str(name or "").casefold().split())


def find_by_name(products: dict[str, Product], name: str) -> Product | None:
    """Vare hvis navn eller et af aliaserne er præcis det samme (uden forskel på store/små bogstaver)."""
    key = _name_key(name)
    if not key:
        return None
    for product in products.values():
        if key == _name_key(product.navn) or key in {_name_key(a) for a in product.aliaser}:
            return product
    return None


def next_varenr(products: dict[str, Product]) -> str:
    numbers = [int(v) for v in products if v.isdigit()]
    return str(max(numbers) + 1) if numbers else "1001"


def plan_document(doc: Document, *, filename: str, source: str, line_prefix: str,
                  products: dict[str, Product], threshold: float, fallback_date: str,
                  now: str, create_products: bool = True, lokation: str = "") -> DocResult:
    """Fordel linjerne på bevægelser og gennemgang.

    Nye varer (som Claude er sikker på ikke findes) oprettes i `products` og i
    result.new_products, og bogføres med det samme, når create_products er slået til.
    """
    move_type = DOC_TYPE_TO_MOVE.get(doc.dokumenttype, "")
    result = DocResult(filename, move_type, fmt_dates(doc.dato_fra, doc.dato_til) or fallback_date,
                       doc.stand)
    result.lokation = lokation if move_type == SALE else ""
    for i, line in enumerate(doc.linjer, 1):
        reason, qty = "", None
        product = products.get(line.varenr or "")
        if line.varenr is None and line.varenavn:
            # Samme navn som en eksisterende (eller netop oprettet) vare: brug den, ingen dublet.
            product = find_by_name(products, line.varenavn)
        creatable = (create_products and product is None and line.varenr is None
                     and line.ny_vare and bool(line.varenavn) and bool(move_type)
                     and line.antal is not None and line.antal > 0
                     and line.sikkerhed >= threshold)
        if creatable:
            product = Product(varenr=next_varenr(products), navn=line.varenavn, aliaser=[],
                              enhed=normalize_unit(line.enhed), omregning={}, minimum=None,
                              start=0.0)
            products[product.varenr] = product
            result.new_products.append({
                "Varenr": sheet_varenr(product.varenr), "Navn": product.navn,
                "Enhed": product.enhed,
                "Noter": f"Oprettet automatisk {now[:10]} fra {filename}",
            })
        if not move_type:
            reason = "Bilagstypen kunne ikke bestemmes"
        elif product is None and line.varenr is None:
            reason = ("Ingen sikker vare fundet i produktlisten" if not line.ny_vare
                      else "Ny vare, men for usikker til at oprette automatisk")
        elif product is None:
            reason = f"Varenr {line.varenr} findes ikke i Produkter"
        elif line.antal is None:
            reason = "Antal mangler"
        elif line.antal <= 0:
            reason = "Antal er 0 eller negativt"
        elif line.sikkerhed < threshold:
            reason = f"Lav sikkerhed ({line.sikkerhed:.2f} < {threshold:.2f})"
        else:
            qty = convert(line.antal, line.enhed, product)
            if qty is None:
                unit = normalize_unit(line.enhed)
                reason = (f"Enheden '{line.enhed}' kan ikke omregnes til "
                          f"'{product.enhed or 'ingen enhed'}' – tilføj fx '{unit}=6' i Omregning")
        if line.note and reason:
            reason += f" (note: {line.note})"

        if reason:
            result.reviews.append({
                "Tidspunkt": now, "Dato": result.dato, "Type": move_type or "Ukendt",
                "Stand/Leverandør": doc.stand, "Originaltekst": line.originaltekst,
                "Lokation": result.lokation,
                "Antal": line.antal, "Enhed": line.enhed,
                "Foreslået varenr": sheet_varenr(line.varenr) if line.varenr else "",
                "Sikkerhed": round(line.sikkerhed, 2), "Årsag": reason, "Kilde": source,
                "Status": "Ny", "Linje-id": f"{line_prefix}-{i}",
            })
        else:
            signed = round(-qty if move_type == SALE else qty, 3)
            result.moves.append({
                "Tidspunkt": now, "Dato": result.dato, "Type": move_type,
                "Varenr": sheet_varenr(product.varenr), "Antal": signed, "Kilde": source,
                "Bilagstype": doc.dokumenttype, "Sikkerhed": round(line.sikkerhed, 2),
                "Status": "Bogført", "Stand/Leverandør": doc.stand,
                "Originaltekst": line.originaltekst, "Lokation": result.lokation,
            })
    return result


def plan_approvals(review: list[tuple[int, dict]], products: dict[str, Product],
                   sources: set[str], now: str) -> tuple[list[dict], list[tuple[int, str]]]:
    """Linjer med Status 'Godkendt' i Til gennemgang -> bevægelser + nye statusser."""
    moves, updates = [], []
    for row_no, row in review:
        if str(row.get("Status") or "").strip().lower() != "godkendt":
            continue
        line_id = str(row.get("Linje-id") or f"række-{row_no}").strip()
        source = f"gennemgang:{line_id}"
        if source in sources:  # allerede bogført i en tidligere kørsel
            updates.append((row_no, "Bogført"))
            continue
        varenr = clean_id(row.get("Godkendt varenr")) or clean_id(row.get("Foreslået varenr"))
        product = products.get(varenr)
        if product is None:
            updates.append((row_no, f"Fejl: varenr '{varenr}' findes ikke i Produkter"))
            continue
        qty = parse_number(row.get("Godkendt antal"))
        if qty is None:
            raw = parse_number(row.get("Antal"))
            qty = convert(raw, str(row.get("Enhed") or ""), product) if raw is not None else None
        if qty is None:
            updates.append((row_no, "Fejl: udfyld Godkendt antal (i produktets enhed)"))
            continue
        move_type = str(row.get("Type") or "").strip()
        if move_type not in (SALE, DELIVERY):
            updates.append((row_no, "Fejl: Type skal være Salg eller Leverance"))
            continue
        qty = abs(qty)
        moves.append({
            "Tidspunkt": now, "Dato": row.get("Dato", ""), "Type": move_type,
            "Varenr": sheet_varenr(product.varenr), "Antal": round(-qty if move_type == SALE else qty, 3),
            "Kilde": source, "Bilagstype": "gennemgang", "Sikkerhed": 1,
            "Status": "Bogført", "Stand/Leverandør": row.get("Stand/Leverandør", ""),
            "Originaltekst": row.get("Originaltekst", ""), "Lokation": row.get("Lokation", ""),
        })
        sources.add(source)
        updates.append((row_no, "Bogført"))
    return moves, updates


# --- kvittering ----------------------------------------------------------

def build_receipt(results: list[DocResult], *, errors: list[tuple[str, str]],
                  notes: list[str], products: dict[str, Product],
                  low_stock: list[tuple[Product, float]], sheet_url: str) -> str:
    out = ["Hej", "", "Her er kvitteringen for de bilag, du sendte.", ""]

    def name(varenr) -> str:
        p = products.get(clean_id(varenr))
        return f"{p.navn} ({p.varenr})" if p else str(varenr)

    def unit(varenr) -> str:
        p = products.get(clean_id(varenr))
        return f" {p.enhed}" if p and p.enhed else ""

    for res in results:
        label = {SALE: "Slutseddel", DELIVERY: "Leverance"}.get(res.move_type, "Ukendt bilag")
        heading = " · ".join(x for x in (label, res.stand, res.dato) if x)
        out += [f"== {heading} ==", f"Fil: {res.filename}"]
        if res.move_type == SALE:
            out.append(f"Lokation: {res.lokation}" if res.lokation else
                       f"Lokation: ikke fundet – tilføj '{res.stand}' under Andre navne i arket "
                       "Lokationer (eller som ny lokation), så salget kommer med i statistikken.")
        if res.new_products:
            out.append("Nye varer oprettet i Produkter (tjek navn, enhed og startbeholdning):")
            out += [f"  {p['Navn']} ({p['Varenr']})" + (f", enhed: {p['Enhed']}" if p["Enhed"] else "")
                    for p in res.new_products]
        if res.moves:
            out.append("Trukket fra lageret:" if res.move_type == SALE else "Lagt til lageret:")
            out += [f"  {fmt_qty(m['Antal'], signed=True)}{unit(m['Varenr'])}  {name(m['Varenr'])}"
                    for m in res.moves]
        if res.reviews:
            out.append("Til gennemgang (ikke bogført):")
            out += [f"  \"{r['Originaltekst']}\" – {r['Årsag']}" for r in res.reviews]
        if not res.moves and not res.reviews:
            out.append("Ingen varelinjer fundet.")
        out.append("")

    if errors:
        out.append("== Fejl ==")
        out += [f"  {filename}: {message}" for filename, message in errors]
        out += ["Svar på denne mail med de fejlede filer igen, så forsøger systemet på ny.", ""]
    if notes:
        out.append("== Bemærkninger ==")
        out += [f"  {n}" for n in notes]
        out.append("")
    if low_stock:
        out.append("== Under minimumsbeholdning ==")
        out += [f"  {p.navn} ({p.varenr}): {fmt_qty(level)}{' ' + p.enhed if p.enhed else ''} "
                f"(minimum {fmt_qty(p.minimum)})" for p, level in low_stock]
        out.append("")
    if any(r.reviews for r in results):
        out += ["Linjer til gennemgang står i arket \"Til gennemgang\". Sæt Status til "
                "\"Godkendt\" (og evt. Godkendt varenr/antal), så bogføres de ved næste kørsel.", ""]
    out += [f"Lager: {sheet_url}", "", "Venlig hilsen", "Lagersystemet"]
    return "\n".join(out)


# --- statistik -------------------------------------------------------------------

UNKNOWN_LOCATION = "(ukendt lokation)"
NO_CATEGORY = "Uden varegruppe"


def compute_stats(products: dict[str, Product], moves: list[dict], locations: list[Location],
                  now: str) -> list[list]:
    """Tabeller til arket Statistik: salg pr. lokation og varegruppe/vare."""
    sales = [m for m in moves if str(m.get("Type", "")).strip() == SALE]
    loc_names = [loc.navn for loc in locations]
    group_of = {loc.navn: loc.gruppe for loc in locations}

    def loc_of(move) -> str:
        name = str(move.get("Lokation") or "").strip()
        return name if name in group_of else UNKNOWN_LOCATION

    columns = list(loc_names)
    if any(loc_of(m) == UNKNOWN_LOCATION for m in sales):
        columns.append(UNKNOWN_LOCATION)
    groups = [g for g in dict.fromkeys(loc.gruppe for loc in locations)
              if g and sum(1 for loc in locations if loc.gruppe == g) > 1]
    header = [*columns, *[f"{g} i alt" for g in groups], "I alt"]

    qty: dict[str, dict[str, float]] = {}
    kr: dict[str, dict[str, float]] = {}
    unpriced: set[str] = set()
    for move in sales:
        varenr = clean_id(move.get("Varenr"))
        amount = -(parse_number(move.get("Antal")) or 0.0)
        loc = loc_of(move)
        qty.setdefault(varenr, {}).setdefault(loc, 0.0)
        qty[varenr][loc] += amount
        product = products.get(varenr)
        category = product.kategori if product and product.kategori else NO_CATEGORY
        if product and product.salgspris is not None:
            kr.setdefault(category, {}).setdefault(loc, 0.0)
            kr[category][loc] += amount * product.salgspris
        else:
            unpriced.add(product.navn if product else varenr)

    def with_totals(values: dict[str, float]) -> list:
        row = [round(values.get(c, 0.0), 2) for c in columns]
        row += [round(sum(v for loc, v in values.items() if group_of.get(loc) == g), 2)
                for g in groups]
        row.append(round(sum(values.values()), 2))
        return row

    out: list[list] = [[f"Statistik over salg – opdateret {now}"],
                       ["Opdateres automatisk ved hver kørsel. Ret ikke i arket; "
                        "ændringer overskrives."], []]

    out.append(["Omsætning (kr.) pr. varegruppe og lokation"])
    out.append(["Varegruppe", *header])
    totals: dict[str, float] = {}
    if kr:
        for category in sorted(kr):
            out.append([category, *with_totals(kr[category])])
            for loc, v in kr[category].items():
                totals[loc] = totals.get(loc, 0.0) + v
        out.append(["I alt", *with_totals(totals)])
    else:
        out.append(["Ingen salg med salgspris endnu"])
    if unpriced:
        out.append(["Uden salgspris (ikke med i kroner): " + ", ".join(sorted(unpriced))])
    out.append([])

    out.append(["Andel af lokationens omsætning (%) pr. varegruppe"])
    out.append(["Varegruppe", *header])
    if kr:
        col_totals = dict(zip(header, with_totals(totals)))
        for category in sorted(kr):
            values = dict(zip(header, with_totals(kr[category])))
            out.append([category, *[round(100 * values[h] / col_totals[h], 1)
                                    if col_totals[h] else "" for h in header]])
    out.append([])

    out.append(["Antal solgt pr. vare og lokation (i varens enhed)"])
    out.append(["Vare", "Varegruppe", "Enhed", *header])
    if qty:
        def sort_key(varenr):
            p = products.get(varenr)
            return ((p.kategori or "~") if p else "~", p.navn if p else varenr)

        for varenr in sorted(qty, key=sort_key):
            p = products.get(varenr)
            out.append([p.navn if p else varenr, (p.kategori if p else "") or NO_CATEGORY,
                        p.enhed if p else "", *with_totals(qty[varenr])])
    else:
        out.append(["Ingen salg endnu"])
    return out
