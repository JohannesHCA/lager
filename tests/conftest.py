"""Fælles test-hjælpere: falske udgaver af Gmail, Sheets og Haiku (ingen netværk)."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lager.config import Config  # noqa: E402
from lager.extraction import Document, Line  # noqa: E402
from lager.gmail import Attachment, Mail  # noqa: E402
from lager.sheets import HEADERS, LOCATIONS, MOVES, PROCESSED, PRODUCTS, REVIEW, SheetData  # noqa: E402

ELIAS = "elias@example.com"
GOOD_AUTH = ["mx.google.com; dkim=pass header.i=@example.com header.s=s1; "
             "spf=pass smtp.mailfrom=elias@example.com; dmarc=pass header.from=example.com"]


def make_config(**kw) -> Config:
    base = dict(google_client_id="x", google_client_secret="x",
                google_refresh_token="x", sheet_id="SHEET", emails=(ELIAS,))
    base.update(kw)
    return Config(**base)


PRODUCT_ROWS = [
    {"Varenr": 101, "Navn": "Svensk ost", "Aliaser": "Wrångebäck, Wrångeback, Søblå",
     "Enhed": "100 g", "Omregning": "kg=10", "Minimumsbeholdning": 50, "Startbeholdning": 400},
    {"Varenr": 102, "Navn": "Lihmskov", "Aliaser": "", "Enhed": "flaske", "Omregning": "kasse=6",
     "Minimumsbeholdning": 20, "Startbeholdning": 130},
    {"Varenr": 103, "Navn": "Signature", "Aliaser": "", "Enhed": "flaske",
     "Omregning": "kasse=6", "Minimumsbeholdning": "", "Startbeholdning": 10},
]


class FakeSheets:
    def __init__(self, products=None):
        self.rows = {PRODUCTS: copy.deepcopy(products if products is not None else PRODUCT_ROWS),
                     MOVES: [], REVIEW: [], PROCESSED: [], LOCATIONS: []}
        self.append_calls = 0
        self.stats: list[list] | None = None
        self.status_updates: list[tuple[int, str]] = []

    def read_all(self) -> SheetData:
        return SheetData(
            headers={k: list(v) for k, v in HEADERS.items()},
            products=copy.deepcopy(self.rows[PRODUCTS]),
            moves=copy.deepcopy(self.rows[MOVES]),
            review=[(i + 2, copy.deepcopy(r)) for i, r in enumerate(self.rows[REVIEW])],
            processed_ids={r["Message-id"] for r in self.rows[PROCESSED]},
            locations=copy.deepcopy(self.rows[LOCATIONS]),
        )

    def write_stats(self, rows):
        self.stats = rows
        return True

    def append_atomic(self, rows_by_sheet):
        self.append_calls += 1
        for name, rows in rows_by_sheet.items():
            self.rows[name] += copy.deepcopy(rows)

    def update_review_status(self, updates):
        self.status_updates += updates
        for row_no, status in updates:
            self.rows[REVIEW][row_no - 2]["Status"] = status


class FakeGmail:
    def __init__(self):
        self.mails: dict[str, Mail] = {}
        self.system_threads: set[str] = set()
        self.labels: dict[str, set[str]] = {}
        self.sent: list[dict] = []
        self.already_sent: set[str] = set()

    def add_mail(self, mail: Mail, system_thread: bool = True) -> None:
        self.mails[mail.id] = mail
        if system_thread:
            self.system_threads.add(mail.thread_id)

    def find_candidates(self, senders, days):
        return [m.id for m in self.mails.values() if m.sender in senders]

    def get_message(self, msg_id):
        return copy.deepcopy(self.mails[msg_id])

    def thread_started_by_system(self, thread_id):
        return thread_id in self.system_threads

    def has_label(self, mail, name):
        return name in self.labels.get(mail.id, set())

    def add_label(self, msg_id, name):
        self.labels.setdefault(msg_id, set()).add(name)

    def send(self, to, subject, body, *, thread_id=None, in_reply_to=None):
        self.sent.append(dict(to=to, subject=subject, body=body, thread_id=thread_id))
        return f"sent-{len(self.sent)}"

    def sent_recently(self, subject, days=6):
        return subject in self.already_sent


class FakeExtractor:
    """Returnerer et fast Document (eller kaster en fejl) pr. filnavn."""

    def __init__(self, by_filename: dict):
        self.by_filename = by_filename
        self.calls: list[str] = []

    def extract(self, filename, mime, data):
        self.calls.append(filename)
        result = self.by_filename[filename]
        if isinstance(result, Exception):
            raise result
        return result


def make_mail(msg_id="m1", attachments=None, sender=ELIAS, subject="Re: [Lager] Uge 47",
              auth=None, thread="t1", labels=None) -> Mail:
    return Mail(id=msg_id, thread_id=thread, subject=subject, sender=sender,
                message_id_header=f"<{msg_id}@example.com>", date="2026-11-16",
                labels=labels or ["INBOX"],
                auth_results=GOOD_AUTH if auth is None else auth, is_system=False,
                attachments=attachments if attachments is not None
                else [Attachment("slutseddel.pdf", "application/pdf", b"%PDF-1 aarhus")])


def sale_doc() -> Document:
    return Document("slutseddel", "2026-09-04", "2026-09-06", "Food Festival Aarhus", [
        Line("Svensk ost 338.85", 338.85, "", "101", 0.97),
        Line("Lihmskov 125", 125, "", "102", 0.95),
        Line("Arty 5", 5, "", None, 0.2),
    ])


class FakeClaudeCli:
    """Efterligner subprocess.run(['claude', '-p', ...]) og husker kaldene.

    Hvert svar er enten et dict (structured_output), en tekst (result) eller et helt
    resultat-objekt med nøglen 'raw'.
    """

    def __init__(self, replies: list):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, cmd, *, input, cwd, **kwargs):
        import os
        self.calls.append({"cmd": cmd, "input": input, "files": sorted(os.listdir(cwd))})
        reply = self.replies.pop(0)
        if isinstance(reply, dict) and "raw" in reply:
            result = reply["raw"]
        elif isinstance(reply, dict):
            result = {"type": "result", "subtype": "success", "is_error": False,
                      "result": "", "structured_output": reply}
        else:
            result = {"type": "result", "subtype": "success", "is_error": False, "result": reply}
        stdout = "[claude-code:info] advarsel\n" + json.dumps(result)
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    def arg(self, call: int, flag: str):
        cmd = self.calls[call]["cmd"]
        return cmd[cmd.index(flag) + 1]


@pytest.fixture
def cfg():
    return make_config()
