"""Hele flowet med falske Gmail/Sheets/Haiku: idempotens, filtrering og fejlhåndtering."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from conftest import FakeExtractor, FakeGmail, FakeSheets, make_mail, sale_doc

from lager.config import LABEL_DONE, LABEL_REJECTED
from lager.extraction import ExtractionError
from lager.gmail import Attachment
from lager.main import run_process, run_weekly
from lager.sheets import MOVES, PROCESSED, REVIEW

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)


def run(cfg, gmail, sheets, extractor, dry_run=False):
    return run_process(cfg, gmail, sheets, lambda *a: extractor,
                       dry_run=dry_run, now=NOW)


def test_reply_is_booked_labelled_and_receipted(cfg):
    gmail, sheets = FakeGmail(), FakeSheets()
    gmail.add_mail(make_mail())
    ex = FakeExtractor({"slutseddel.pdf": sale_doc()})
    assert run(cfg, gmail, sheets, ex) == 0
    assert [m["Antal"] for m in sheets.rows[MOVES]] == [-338.85, -125]
    assert len(sheets.rows[REVIEW]) == 1
    assert sheets.rows[PROCESSED][0]["Message-id"] == "m1"
    assert sheets.append_calls == 1                       # ét atomisk kald
    assert LABEL_DONE in gmail.labels["m1"]
    assert gmail.sent[0]["thread_id"] == "t1"
    assert gmail.sent[0]["subject"] == "Re: [Lager] Uge 47"


def test_rerun_never_books_twice(cfg):
    gmail, sheets = FakeGmail(), FakeSheets()
    gmail.add_mail(make_mail())
    ex = FakeExtractor({"slutseddel.pdf": sale_doc()})
    run(cfg, gmail, sheets, ex)
    gmail.labels.clear()                                   # selv uden Gmail-label ...
    assert run(cfg, gmail, sheets, ex) == 0
    assert len(sheets.rows[MOVES]) == 2                    # ... skrives intet nyt
    assert len(ex.calls) == 1                              # og Haiku kaldes ikke igen
    assert LABEL_DONE in gmail.labels["m1"]                # labelen sættes igen
    assert len(gmail.sent) == 1


def test_same_attachment_in_a_new_mail_is_not_booked_again(cfg):
    gmail, sheets = FakeGmail(), FakeSheets()
    gmail.add_mail(make_mail("m1"))
    gmail.add_mail(make_mail("m2", thread="t2"))           # samme fil sendt igen
    ex = FakeExtractor({"slutseddel.pdf": sale_doc()})
    run(cfg, gmail, sheets, ex)
    assert len(sheets.rows[MOVES]) == 2
    assert len(ex.calls) == 1
    assert "allerede behandlet" in gmail.sent[1]["body"]


def test_only_replies_from_min_email_in_lager_threads(cfg):
    gmail, sheets = FakeGmail(), FakeSheets()
    gmail.add_mail(make_mail("fremmed", sender="svindel@example.com"))
    gmail.add_mail(make_mail("nytraad", thread="t9"), system_thread=False)
    gmail.add_mail(make_mail("andet-emne", subject="Re: Faktura"))
    ex = FakeExtractor({"slutseddel.pdf": sale_doc()})
    run(cfg, gmail, sheets, ex)
    assert ex.calls == [] and sheets.rows[MOVES] == [] and gmail.sent == []


def test_spoofed_sender_is_rejected_without_reading_attachments(cfg):
    gmail, sheets = FakeGmail(), FakeSheets()
    gmail.add_mail(make_mail(auth=["mx.google.com; dkim=fail; spf=softfail; dmarc=fail"]))
    ex = FakeExtractor({"slutseddel.pdf": sale_doc()})
    run(cfg, gmail, sheets, ex)
    assert ex.calls == [] and sheets.rows[MOVES] == []
    assert sheets.rows[PROCESSED][0]["Resultat"].startswith("Afvist")
    assert LABEL_REJECTED in gmail.labels["m1"] and gmail.sent == []


def test_one_failing_attachment_does_not_stop_the_rest(cfg):
    gmail, sheets = FakeGmail(), FakeSheets()
    gmail.add_mail(make_mail(attachments=[
        Attachment("slutseddel.pdf", "application/pdf", b"%PDF a"),
        Attachment("sløret.jpg", "image/jpeg", b"jpeg"),
    ]))
    ex = FakeExtractor({"slutseddel.pdf": sale_doc(),
                        "sløret.jpg": ExtractionError("billedet kunne ikke åbnes")})
    assert run(cfg, gmail, sheets, ex) == 0
    assert len(sheets.rows[MOVES]) == 2
    assert "sløret.jpg: billedet kunne ikke åbnes" in gmail.sent[0]["body"]


def test_temporary_api_outage_is_retried_later(cfg):
    gmail, sheets = FakeGmail(), FakeSheets()
    gmail.add_mail(make_mail())
    down = FakeExtractor({"slutseddel.pdf": ExtractionError("529 overloaded", transient=True)})
    assert run(cfg, gmail, sheets, down) == 1
    assert sheets.rows[PROCESSED] == [] and "m1" not in gmail.labels and gmail.sent == []
    up = FakeExtractor({"slutseddel.pdf": sale_doc()})
    assert run(cfg, gmail, sheets, up) == 0
    assert len(sheets.rows[MOVES]) == 2


def test_dry_run_writes_and_sends_nothing(cfg, capsys):
    gmail, sheets = FakeGmail(), FakeSheets()
    gmail.add_mail(make_mail())
    run(cfg, gmail, sheets, FakeExtractor({"slutseddel.pdf": sale_doc()}), dry_run=True)
    assert sheets.append_calls == 0 and gmail.labels == {} and gmail.sent == []
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "-338.85" in out


def test_approved_review_lines_are_booked_on_next_run(cfg):
    gmail, sheets = FakeGmail(), FakeSheets()
    gmail.add_mail(make_mail())
    run(cfg, gmail, sheets, FakeExtractor({"slutseddel.pdf": sale_doc()}))
    sheets.rows[REVIEW][0].update({"Status": "Godkendt", "Godkendt varenr": 103})
    run(cfg, gmail, sheets, FakeExtractor({}))
    run(cfg, gmail, sheets, FakeExtractor({}))
    approved = [m for m in sheets.rows[MOVES] if m["Kilde"].startswith("gennemgang:")]
    assert [(m["Varenr"], m["Antal"]) for m in approved] == [(103, -5)]
    assert sheets.rows[REVIEW][0]["Status"] == "Bogført"


# --- mandagsmailen -----------------------------------------------------------

def test_weekly_sends_only_monday_8_danish_time(cfg):
    gmail = FakeGmail()
    winter_06_utc = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)   # kl. 07 dansk tid
    winter_07_utc = datetime(2026, 11, 16, 7, 0, tzinfo=timezone.utc)   # kl. 08 dansk tid
    run_weekly(cfg, gmail, now=winter_06_utc, force=False, dry_run=False)
    assert gmail.sent == []
    run_weekly(cfg, gmail, now=winter_07_utc, force=False, dry_run=False)
    assert gmail.sent[0]["subject"] == "[Lager] Uge 47"
    assert "lørdag 14.11 og søndag 15.11" in gmail.sent[0]["body"]

    summer = FakeGmail()
    run_weekly(cfg, summer, now=datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc), force=False,
               dry_run=False)                                             # kl. 08 sommertid
    assert summer.sent[0]["subject"] == "[Lager] Uge 23"


def test_weekly_survives_github_delays(cfg):
    """23/11-fejlen: GitHub startede kørslerne kl. 18:44 og 20:07 dansk tid."""
    from lager.main import in_send_window
    cph = ZoneInfo("Europe/Copenhagen")
    assert in_send_window(datetime(2026, 11, 23, 20, 7, tzinfo=cph))      # mandag aften
    assert in_send_window(datetime(2026, 11, 24, 9, 0, tzinfo=cph))       # tirsdag (opsamling)
    assert not in_send_window(datetime(2026, 11, 23, 7, 59, tzinfo=cph))  # før kl. 8
    assert not in_send_window(datetime(2026, 11, 25, 9, 0, tzinfo=cph))   # onsdag

    gmail = FakeGmail()
    late = datetime(2026, 11, 23, 19, 7, tzinfo=timezone.utc)
    run_weekly(cfg, gmail, now=late, force=False, dry_run=False)
    assert gmail.sent[0]["subject"] == "[Lager] Uge 48"
    gmail.already_sent.add("[Lager] Uge 48")
    run_weekly(cfg, gmail, now=datetime(2026, 11, 24, 11, 17, tzinfo=timezone.utc), force=False,
               dry_run=False)                                             # tirsdag: ikke igen
    assert len(gmail.sent) == 1


def test_weekly_is_not_sent_twice(cfg):
    gmail = FakeGmail()
    gmail.already_sent.add("[Lager] Uge 47")
    run_weekly(cfg, gmail, now=datetime(2026, 11, 16, 7, 5, tzinfo=timezone.utc), force=False,
               dry_run=False)
    assert gmail.sent == []
    run_weekly(cfg, gmail, now=datetime(2026, 11, 18, 12, tzinfo=timezone.utc), force=True,
               dry_run=False)                                             # manuel kørsel
    assert len(gmail.sent) == 1


# --- flere modtagere ---------------------------------------------------------

OWNER = "ejer@gmail.com"


def test_parse_emails():
    from lager.config import parse_emails
    assert parse_emails("Elias@example.com, ejer@gmail.com; elias@example.com") == (
        "elias@example.com", "ejer@gmail.com")


def test_weekly_mail_and_receipt_go_to_all_addresses():
    from conftest import make_config
    cfg = make_config(emails=("elias@example.com", OWNER))
    gmail, sheets = FakeGmail(), FakeSheets()
    run_weekly(cfg, gmail, now=datetime(2026, 11, 16, 7, 0, tzinfo=timezone.utc), force=False,
               dry_run=False)
    assert gmail.sent[0]["to"] == "elias@example.com, ejer@gmail.com"
    gmail.add_mail(make_mail())
    run(cfg, gmail, sheets, FakeExtractor({"slutseddel.pdf": sale_doc()}))
    assert gmail.sent[1]["to"] == "elias@example.com, ejer@gmail.com"


def test_owner_can_reply_from_the_system_account():
    from conftest import make_config
    cfg = make_config(emails=("elias@example.com", OWNER))
    gmail, sheets = FakeGmail(), FakeSheets()
    # Svar sendt fra systemets egen Gmail: SENT-label, ingen Authentication-Results.
    gmail.add_mail(make_mail(sender=OWNER, auth=[], labels=["SENT", "INBOX"]))
    run(cfg, gmail, sheets, FakeExtractor({"slutseddel.pdf": sale_doc()}))
    assert len(sheets.rows[MOVES]) == 2


def test_forged_owner_address_from_outside_is_rejected():
    from conftest import make_config
    cfg = make_config(emails=("elias@example.com", OWNER))
    gmail, sheets = FakeGmail(), FakeSheets()
    gmail.add_mail(make_mail(sender=OWNER, auth=["mx.google.com; dkim=fail; spf=fail"]))
    ex = FakeExtractor({"slutseddel.pdf": sale_doc()})
    run(cfg, gmail, sheets, ex)
    assert ex.calls == [] and sheets.rows[MOVES] == []


def test_new_products_are_written_once_across_mails(cfg):
    from lager.extraction import Document, Line
    from lager.sheets import PRODUCTS
    doc = Document("slutseddel", "", "", "Julemarked",
                   [Line("Arty 5", 5, "", None, 0.95, varenavn="Arty", ny_vare=True)])
    gmail, sheets = FakeGmail(), FakeSheets()
    gmail.add_mail(make_mail("m1", attachments=[Attachment("a.jpg", "image/jpeg", b"a")]))
    gmail.add_mail(make_mail("m2", thread="t2",
                             attachments=[Attachment("b.jpg", "image/jpeg", b"b")]))
    run(cfg, gmail, sheets, FakeExtractor({"a.jpg": doc, "b.jpg": doc}))
    assert [p["Navn"] for p in sheets.rows[PRODUCTS] if p.get("Navn") == "Arty"] == ["Arty"]
    assert [m["Antal"] for m in sheets.rows[MOVES]] == [-5, -5]
    assert "Nye varer oprettet" in gmail.sent[0]["body"]


# --- genbehandling -------------------------------------------------------------

def test_reprocess_reads_mail_again_and_replaces_old_review_lines(cfg):
    from lager.extraction import Document, Line
    from lager.sheets import PRODUCTS
    gmail, sheets = FakeGmail(), FakeSheets(products=[])
    gmail.add_mail(make_mail("abc123"))
    old = Document("slutseddel", "", "", "Marked", [Line("Arty 5", 5, "", None, 0.9)])
    run(cfg, gmail, sheets, FakeExtractor({"slutseddel.pdf": old}))
    assert len(sheets.rows[REVIEW]) == 1 and sheets.rows[MOVES] == []

    new = Document("slutseddel", "", "", "Marked",
                   [Line("Arty 5", 5, "", None, 0.9, varenavn="Arty", ny_vare=True)])
    ex = FakeExtractor({"slutseddel.pdf": new})
    assert run_process(cfg, gmail, sheets, lambda *a: ex, dry_run=False, now=NOW,
                       reprocess_id="abc123") == 0
    assert ex.calls == ["slutseddel.pdf"]                   # læst igen trods kendt hash
    assert [m["Antal"] for m in sheets.rows[MOVES]] == [-5]
    assert [p["Navn"] for p in sheets.rows[PRODUCTS]] == ["Arty"]
    assert sheets.rows[REVIEW][0]["Status"] == "Erstattet"
    assert sheets.rows[PROCESSED][-1]["Resultat"].startswith("Genbehandlet")

    # En gang til ville trække varerne to gange: afvises.
    again = FakeExtractor({"slutseddel.pdf": new})
    assert run_process(cfg, gmail, sheets, lambda *a: again, dry_run=False, now=NOW,
                       reprocess_id="abc123") == 1
    assert again.calls == [] and len(sheets.rows[MOVES]) == 1


def test_reprocess_rejects_bad_ids(cfg):
    gmail, sheets = FakeGmail(), FakeSheets()
    assert run_process(cfg, gmail, sheets, lambda *a: None, dry_run=False, now=NOW,
                       reprocess_id="abc; rm -rf /") == 1


# --- import af varer -------------------------------------------------------------

def test_import_adds_new_products_and_skips_existing():
    from lager.main import run_import
    from lager.sheets import PRODUCTS
    sheets = FakeSheets()          # har allerede 101 Svensk ost, 102 Lihmskov, 103 Signature
    table = ("Varenr\tNavn\tAliaser\tEnhed\tStartbeholdning\n"
             "1001\tPommeau\t\tstk\t35\n"
             "1002\tlihmskov\t\tstk\t37\n"            # samme navn som 102
             "103\tNoget andet\t\tstk\t1\n"           # samme varenr som Signature
             "1004\tSvensk ost\tWrångebäck\t100 g\t226,75\n")
    assert run_import(sheets, table, dry_run=False) == 0
    added = sheets.rows[PRODUCTS][3:]
    assert [(p["Varenr"], p["Navn"], p["Startbeholdning"]) for p in added] == [(1001, "Pommeau", 35)]


def test_import_dry_run_writes_nothing(capsys):
    from lager.main import run_import
    sheets = FakeSheets(products=[])
    run_import(sheets, "Varenr\tNavn\n1001\tPommeau\n", dry_run=True)
    assert sheets.append_calls == 0 and "Pommeau" in capsys.readouterr().out


def test_products_with_movements_cannot_be_deleted():
    from lager.main import run_delete
    sheets = FakeSheets()
    sheets.rows[MOVES].append({"Varenr": 102, "Antal": -1, "Kilde": "x"})
    sheets.delete_products = lambda v: (_ for _ in ()).throw(AssertionError("må ikke slette"))
    assert run_delete(sheets, "Varenr\n102\n", dry_run=False) == 1
