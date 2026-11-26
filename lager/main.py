"""Kommandolinje: setup | weekly | process   (alle med --dry-run)

    python -m lager.main setup
    python -m lager.main weekly [--force] [--dry-run]
    python -m lager.main process [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import inventory
from .config import LABEL_DONE, LABEL_REJECTED, TAG, TIMEZONE, Config
from .extraction import ExtractionError
from .gmail import is_authentic
from .sheets import LOCATIONS, MOVES, PROCESSED, PRODUCTS, REVIEW

log = logging.getLogger("lager")
TZ = ZoneInfo(TIMEZONE)
DAYS = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]


# --- mandagsmailen ---------------------------------------------------------

def weekly_subject(local: datetime) -> str:
    return f"{TAG} Uge {local.isocalendar().week}"


def weekly_body(local: datetime) -> str:
    monday = local.date() - timedelta(days=local.weekday())
    saturday, sunday = monday - timedelta(days=2), monday - timedelta(days=1)
    week = local.isocalendar().week
    return (
        "Hej\n\n"
        f"Det er tid til ugens lageropdatering (uge {week}).\n\n"
        "Svar på denne mail og vedhæft:\n"
        f"  1. Slutsedler fra weekendens salg (lørdag {saturday:%d.%m} og søndag {sunday:%d.%m}) "
        "– én slutseddel pr. stand/event.\n"
        "  2. Følgesedler for nye leverancer, der er kommet siden sidst.\n\n"
        "Du kan vedhæfte fotos, PDF'er eller Excel-filer – gerne flere i samme svar. "
        "Svar i denne tråd, så systemet kan finde bilagene. Du får en kvittering i tråden, "
        "når de er behandlet (normalt inden for en time).\n\n"
        "Er der hverken salg eller leverancer i denne uge, behøver du ikke svare.\n\n"
        "Venlig hilsen\nLagersystemet\n"
    )


def in_send_window(local: datetime) -> bool:
    """Mandag fra kl. 8 til og med tirsdag (dansk tid).

    GitHub kan forsinke planlagte kørsler med mange timer eller springe dem over, så
    alle kørsler i vinduet forsøger, og 'allerede sendt'-tjekket sikrer kun én mail pr. uge.
    """
    return (local.weekday() == 0 and local.hour >= 8) or local.weekday() == 1


def run_weekly(cfg: Config, gmail, *, now: datetime, force: bool, dry_run: bool) -> int:
    local = now.astimezone(TZ)
    if not force and not in_send_window(local):
        log.info("Springer over: klokken er %s dansk tid (sender mandag fra kl. 8).",
                 local.strftime("%A %H:%M"))
        return 0
    subject = weekly_subject(local)
    if not force and gmail.sent_recently(subject):
        log.info("'%s' er allerede sendt – sender ikke igen.", subject)
        return 0
    body = weekly_body(local)
    if dry_run:
        print(f"[dry-run] Ville sende til {', '.join(cfg.emails)}: {subject}\n\n{body}")
        return 0
    gmail.send(", ".join(cfg.emails), subject, body)
    log.info("Sendt '%s' til %s", subject, ", ".join(cfg.emails))
    return 0


# --- behandling af svar ------------------------------------------------------

def run_process(cfg: Config, gmail, sheets, make_extractor, *, dry_run: bool,
                now: datetime | None = None, reprocess_id: str | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    now_str = now.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    data = sheets.read_all()
    products = inventory.parse_products(data.products)
    locations = inventory.parse_locations(data.locations)
    moves_so_far = list(data.moves)
    review_so_far = [row for _, row in data.review]
    failures = 0

    # 1) Linjer, du har godkendt i "Til gennemgang", bogføres.
    approved, updates = inventory.plan_approvals(
        data.review, products, inventory.known_sources(data.moves), now_str)
    if approved or updates:
        if dry_run:
            print(f"[dry-run] Godkendte linjer: {len(approved)} bevægelser, statusser: {updates}")
        else:
            sheets.append_atomic({MOVES: approved})
            sheets.update_review_status(updates)
            log.info("Bogførte %d godkendte linjer fra Til gennemgang.", len(approved))
        moves_so_far += approved

    if reprocess_id:
        code = run_reprocess(reprocess_id, cfg, gmail, sheets, data, products, moves_so_far,
                             review_so_far, make_extractor, now, now_str, dry_run, locations)
        update_stats(sheets, products, moves_so_far, locations, now_str, dry_run)
        return code

    # 2) Nye svar på mandagsmailen.
    extractor = None
    for msg_id in gmail.find_candidates(cfg.emails, cfg.lookback_days):
        try:
            mail = gmail.get_message(msg_id)
            if mail.id in data.processed_ids:
                if not dry_run and not gmail.has_label(mail, LABEL_DONE):
                    gmail.add_label(mail.id, LABEL_DONE)
                continue
            if gmail.has_label(mail, LABEL_DONE) or gmail.has_label(mail, LABEL_REJECTED):
                continue
            if mail.is_system or mail.sender not in cfg.emails or TAG not in mail.subject:
                continue
            if not gmail.thread_started_by_system(mail.thread_id):
                log.info("Ignorerer %s: ikke et svar i en [Lager]-tråd.", mail.id)
                continue
            # Svar fra systemets egen konto har SENT-label og kan ikke forfalskes udefra.
            if cfg.require_auth and "SENT" not in mail.labels \
                    and not is_authentic(mail.auth_results, mail.sender):
                reject(mail, sheets, gmail, now_str, dry_run,
                       "Afvist: afsenderen kunne ikke verificeres (DKIM/SPF/DMARC)")
                data.processed_ids.add(mail.id)
                continue

            if extractor is None:
                extractor = make_extractor(products, now.astimezone(TZ).date(), locations)
            outcome = process_mail(mail, cfg, sheets, gmail, extractor, products,
                                   moves_so_far, review_so_far, now_str, dry_run,
                                   locations=locations)
            if outcome == "retry":
                failures += 1
                continue
            data.processed_ids.add(mail.id)
        except Exception:  # én fejlende mail må ikke stoppe de andre
            log.exception("Fejl under behandling af mail %s", msg_id)
            failures += 1
    update_stats(sheets, products, moves_so_far, locations, now_str, dry_run)
    return 1 if failures else 0


def update_stats(sheets, products, moves, locations, now_str, dry_run) -> None:
    """Skriv arket Statistik forfra. En fejl her må aldrig stoppe bogføringen."""
    try:
        rows = inventory.compute_stats(products, moves, locations, now_str)
        if dry_run:
            print(f"[dry-run] Statistik: {len(rows)} rækker ville blive skrevet")
            return
        sheets.write_stats(rows)
    except Exception:  # noqa: BLE001
        log.exception("Statistikken kunne ikke opdateres")


def run_reprocess(mail_id, cfg, gmail, sheets, data, products, moves_so_far, review_so_far,
                  make_extractor, now, now_str, dry_run, locations=()) -> int:
    """Læs en allerede behandlet mail igen (fx efter en ny funktion).

    Kun tilladt, hvis intet fra mailen er bogført endnu – ellers ville varer blive trukket
    to gange. Mailens gamle linjer i "Til gennemgang" får status "Erstattet".
    """
    if not re.fullmatch(r"[0-9a-f]{6,32}", mail_id or ""):
        log.error("Ugyldigt Gmail message-id: %r", mail_id)
        return 1
    mail = gmail.get_message(mail_id)
    if mail.sender not in cfg.emails or TAG not in mail.subject:
        log.error("%s er ikke et [Lager]-svar fra en adresse i MIN_EMAIL.", mail_id)
        return 1
    prefix = f"gmail:{mail.id}/"
    old_reviews = [(n, r) for n, r in data.review if str(r.get("Kilde", "")).startswith(prefix)]
    approved_sources = {f"gennemgang:{r.get('Linje-id')}" for _, r in old_reviews}
    booked = [m for m in moves_so_far if str(m.get("Kilde", "")).startswith(prefix)
              or m.get("Kilde") in approved_sources]
    if booked:
        log.error("%s har allerede %d bogførte linjer – genbehandler ikke, så intet trækkes to "
                  "gange. Ret i stedet med Korrektion-rækker.", mail_id, len(booked))
        return 1
    replaced = [(n, "Erstattet") for n, r in old_reviews
                if str(r.get("Status") or "Ny").strip().lower() == "ny"]

    extractor = make_extractor(products, now.astimezone(TZ).date(), locations)
    outcome = process_mail(mail, cfg, sheets, gmail, extractor, products, moves_so_far,
                           review_so_far, now_str, dry_run, reprocess=True, locations=locations)
    if outcome == "retry":
        return 1
    if dry_run:
        print(f"[dry-run] {len(replaced)} gamle linjer i Til gennemgang ville få status Erstattet")
    else:
        sheets.update_review_status(replaced)
        log.info("%s genbehandlet; %d gamle gennemgangslinjer erstattet.", mail_id, len(replaced))
    return 0


def reject(mail, sheets, gmail, now_str: str, dry_run: bool, reason: str) -> None:
    row = {"Message-id": mail.id, "Tidspunkt": now_str, "Afsender": mail.sender,
           "Emne": mail.subject, "Resultat": reason}
    if dry_run:
        print(f"[dry-run] {mail.id}: {reason}")
        return
    sheets.append_atomic({PROCESSED: [row]})
    gmail.add_label(mail.id, LABEL_REJECTED)
    log.warning("%s: %s", mail.id, reason)


def process_mail(mail, cfg, sheets, gmail, extractor, products, moves_so_far, review_so_far,
                 now_str, dry_run, reprocess: bool = False, locations=()) -> str:
    # Ved genbehandling er mailens egne bilag allerede kendt; dem skal vi læse igen.
    seen = set() if reprocess else inventory.known_digests(moves_so_far, review_so_far)
    results: list[inventory.DocResult] = []
    errors: list[tuple[str, str]] = []
    notes: list[str] = []
    transient = 0

    for att in mail.attachments:
        digest = hashlib.sha256(att.data).hexdigest()[:12]
        if digest in seen:
            notes.append(f"{att.filename}: allerede behandlet tidligere – ikke bogført igen.")
            continue
        try:
            doc = extractor.extract(att.filename, att.mime, att.data)
        except ExtractionError as exc:
            log.warning("%s: %s kunne ikke læses: %s", mail.id, att.filename, exc)
            errors.append((att.filename, str(exc)))
            transient += exc.transient
            continue
        except Exception as exc:  # noqa: BLE001 - én vedhæftning må ikke stoppe resten
            log.exception("Uventet fejl i %s", att.filename)
            errors.append((att.filename, f"uventet fejl ({exc})"))
            continue
        if doc is None:
            continue  # fx et lille logo i mailsignaturen
        if doc.dokumenttype == "ukendt" and not doc.linjer:
            notes.append(f"{att.filename}: ikke genkendt som slutseddel eller følgeseddel.")
            continue
        seen.add(digest)
        results.append(inventory.plan_document(
            doc, filename=att.filename,
            source=inventory.attachment_source(mail.id, att.filename, digest),
            line_prefix=f"{mail.id}-{digest}", products=products, threshold=cfg.threshold,
            fallback_date=mail.date, now=now_str,
            lokation=inventory.match_location(doc.stand, doc.lokation, list(locations))))

    if transient and not results:
        log.warning("%s: midlertidig fejl på alle bilag – prøver igen ved næste kørsel.", mail.id)
        return "retry"

    new_moves = [m for r in results for m in r.moves]
    new_reviews = [r_ for r in results for r_ in r.reviews]
    new_products = [p for r in results for p in r.new_products]
    summary = (f"{len(new_moves)} bogført, {len(new_reviews)} til gennemgang, "
               + (f"{len(new_products)} nye varer, " if new_products else "") +
               f"{len(errors)} fejl" + (f", {len(notes)} bemærkninger" if notes else ""))
    processed_row = {"Message-id": mail.id, "Tidspunkt": now_str, "Afsender": mail.sender,
                     "Emne": mail.subject,
                     "Resultat": ("Genbehandlet: " if reprocess else "") + summary}

    levels = inventory.stock_levels(products, moves_so_far + new_moves)
    low = inventory.below_minimum(products, levels)
    receipt = inventory.build_receipt(results, errors=errors, notes=notes, products=products,
                                      low_stock=low, sheet_url=cfg.sheet_url)
    send_receipt = bool(results or errors or notes)  # ikke for mails med kun et signaturlogo

    if dry_run:
        print(f"\n[dry-run] Mail {mail.id} '{mail.subject}': {summary}")
        for row in new_moves:
            print("  Bevægelse:", json.dumps(row, ensure_ascii=False))
        for row in new_reviews:
            print("  Til gennemgang:", json.dumps(row, ensure_ascii=False))
        for row in new_products:
            print("  Ny vare:", json.dumps(row, ensure_ascii=False))
        if send_receipt:
            print("\n[dry-run] Kvittering:\n" + receipt)
        return "done"

    # Nye varer, bevægelser, gennemgangslinjer og registreringen af mailen skrives i ét
    # atomisk kald, så en genkørsel enten ser det hele eller intet af det.
    sheets.append_atomic({PRODUCTS: new_products, MOVES: new_moves, REVIEW: new_reviews,
                          PROCESSED: [processed_row]})
    moves_so_far += new_moves
    review_so_far += new_reviews
    log.info("%s: %s", mail.id, summary)
    gmail.add_label(mail.id, LABEL_DONE)
    if send_receipt:
        subject = mail.subject if mail.subject.lower().startswith("re:") else f"Re: {mail.subject}"
        gmail.send(", ".join(cfg.emails), subject, receipt, thread_id=mail.thread_id,
                   in_reply_to=mail.message_id_header or None)
    return "done"


# --- import af varer -----------------------------------------------------------

def parse_product_table(text: str) -> list[dict]:
    """Tabulatorsepareret tekst med overskrifter fra arket Produkter -> rækker."""
    import csv
    import io

    reader = csv.DictReader(io.StringIO(text.strip()), delimiter="	")
    rows = []
    for raw in reader:
        row = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
        for key in ("Kostpris", "Salgspris", "Minimumsbeholdning", "Startbeholdning"):
            number = inventory.parse_number(row.get(key))
            if number is not None:
                row[key] = int(number) if number.is_integer() else number
        if row.get("Varenr"):
            row["Varenr"] = inventory.sheet_varenr(row["Varenr"])
        rows.append(row)
    return rows


def run_import(sheets, text: str, *, dry_run: bool) -> int:
    """Tilføj varer til Produkter. Varer med et varenr eller navn, der allerede findes, springes over."""
    data = sheets.read_all()
    products = inventory.parse_products(data.products)
    new, skipped = [], []
    for row in parse_product_table(text):
        varenr = inventory.clean_id(row.get("Varenr"))
        name = row.get("Navn", "")
        if not varenr or not name:
            skipped.append(f"{varenr or '?'} {name or '?'}: mangler Varenr eller Navn")
        elif varenr in products or inventory.find_by_name(products, name):
            skipped.append(f"{varenr} {name}: findes allerede")
        else:
            new.append(row)
            products[varenr] = inventory.parse_products([row])[varenr]
    for line in skipped:
        log.warning("Springer over: %s", line)
    if dry_run:
        for row in new:
            print("[dry-run] Ny vare:", json.dumps(row, ensure_ascii=False))
        return 0
    sheets.append_atomic({PRODUCTS: new})
    log.info("Importerede %d varer til Produkter (%d sprunget over).", len(new), len(skipped))
    return 0


def run_import_locations(sheets, text: str, *, dry_run: bool) -> int:
    """Tilføj lokationer (kolonner Lokation, Gruppe, Andre navne). Eksisterende springes over."""
    import csv
    import io

    data = sheets.read_all()
    known = {loc.navn.casefold() for loc in inventory.parse_locations(data.locations)}
    new = []
    for raw in csv.DictReader(io.StringIO(text.strip()), delimiter="\t"):
        row = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
        name = row.get("Lokation", "")
        if not name or name.casefold() in known:
            log.warning("Springer over lokation: %r", name)
            continue
        known.add(name.casefold())
        new.append(row)
    if dry_run:
        for row in new:
            print("[dry-run] Ny lokation:", json.dumps(row, ensure_ascii=False))
        return 0
    sheets.append_atomic({LOCATIONS: new})
    log.info("Tilføjede %d lokationer.", len(new))
    return 0


def run_update(sheets, text: str, *, dry_run: bool) -> int:
    """Ret eksisterende varer: kolonnerne i tabellen overskrives for varer med samme Varenr."""
    sheets.read_all()
    rows = parse_product_table(text)
    if any("Aktuel beholdning" in r for r in rows):
        log.warning("Kolonnen 'Aktuel beholdning' er en formel og bliver ikke ændret.")
    if dry_run:
        for row in rows:
            print("[dry-run] Ret vare:", json.dumps(row, ensure_ascii=False))
        return 0
    missing = sheets.update_products(rows)
    for varenr in missing:
        log.warning("Varenr %s findes ikke i Produkter – ikke ændret.", varenr)
    log.info("Rettede %d varer.", len(rows) - len(missing))
    return 1 if missing else 0


def run_delete(sheets, text: str, *, dry_run: bool) -> int:
    """Slet varer (kolonnen Varenr i tabellen). Kun varer uden bevægelser kan slettes."""
    data = sheets.read_all()
    wanted = [inventory.clean_id(r.get("Varenr")) for r in parse_product_table(text)]
    used = {inventory.clean_id(m.get("Varenr")) for m in data.moves}
    blocked = [v for v in wanted if v in used]
    if blocked:
        log.error("Har bevægelser og slettes ikke (ret med alias + Korrektion i stedet): %s",
                  ", ".join(blocked))
        return 1
    if dry_run:
        print("[dry-run] Ville slette varer:", ", ".join(wanted))
        return 0
    missing = sheets.delete_products(wanted)
    for varenr in missing:
        log.warning("Varenr %s findes ikke i Produkter.", varenr)
    log.info("Slettede %d varer.", len(wanted) - len(missing))
    return 0


# --- setup -------------------------------------------------------------------

def run_setup(*, dry_run: bool) -> int:
    import os

    from .gmail import credentials
    from .sheets import Sheets

    cfg = Config.from_env(need_sheet=False, need_claude=False, need_min_email=False)
    creds = credentials(cfg)
    sheet_id = cfg.sheet_id
    if not sheet_id:
        if dry_run:
            print("[dry-run] SHEET_ID er ikke sat – ville oprette et nyt regneark 'Lager'.")
            return 0
        sheet_id = Sheets.create(creds)
        print(f"Oprettede nyt regneark: https://docs.google.com/spreadsheets/d/{sheet_id}")
        print(f"Gem dette som GitHub Secret SHEET_ID:  {sheet_id}")
    for line in Sheets(creds, sheet_id).setup(dry_run=dry_run):
        print(("[dry-run] " if dry_run else "") + line)
    if not os.environ.get("SHEET_ID"):
        print("Husk at sætte SHEET_ID, før du kører 'process'.")
    return 0


# --- indgang -----------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lager", description="Lagersystem")
    parser.add_argument("kommando", choices=["setup", "weekly", "process", "importer"])
    parser.add_argument("--dry-run", action="store_true",
                        help="vis hvad der ville ske, uden at skrive eller sende noget")
    parser.add_argument("--force", action="store_true",
                        help="weekly: send nu, uanset tidspunkt og om den allerede er sendt")
    parser.add_argument("--genbehandl", metavar="MESSAGE_ID",
                        help="process: læs én allerede behandlet mail igen (Gmail message-id)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.kommando == "setup":
        return run_setup(dry_run=args.dry_run)

    if args.kommando == "importer":
        from .gmail import credentials
        from .sheets import Sheets

        text = os.environ.get("IMPORT_PRODUKTER", "")
        if not text.strip():
            raise SystemExit("IMPORT_PRODUKTER er tom – indsæt en tabel med overskrifter.")
        cfg = Config.from_env(need_claude=False, need_min_email=False)
        sheets = Sheets(credentials(cfg), cfg.sheet_id)
        mode = os.environ.get("IMPORT_TILSTAND", "tilføj").strip().lower()
        if mode == "ret":
            return run_update(sheets, text, dry_run=args.dry_run)
        if mode == "slet":
            return run_delete(sheets, text, dry_run=args.dry_run)
        if mode == "lokationer":
            return run_import_locations(sheets, text, dry_run=args.dry_run)
        return run_import(sheets, text, dry_run=args.dry_run)

    from .gmail import Gmail, credentials

    if args.kommando == "weekly":
        cfg = Config.from_env(need_sheet=False, need_claude=False)
        return run_weekly(cfg, Gmail(credentials(cfg)), now=datetime.now(timezone.utc),
                          force=args.force, dry_run=args.dry_run)

    from .extraction import Extractor
    from .sheets import Sheets

    cfg = Config.from_env()
    creds = credentials(cfg)
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if token and not token.startswith("sk-ant-oat"):
        log.warning("CLAUDE_CODE_OAUTH_TOKEN ligner ikke en token fra 'claude setup-token' "
                    "(den skal starte med sk-ant-oat). Lav en ny og gem den som secret igen.")

    def make_extractor(products, today: date, locations=()):
        return Extractor(products, today, locations=locations)

    return run_process(cfg, Gmail(creds), Sheets(creds, cfg.sheet_id), make_extractor,
                       dry_run=args.dry_run, reprocess_id=args.genbehandl)


if __name__ == "__main__":
    sys.exit(main())
