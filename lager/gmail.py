"""Gmail: OAuth, søgning, vedhæftninger, labels og afsendelse."""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import parseaddr

from .config import SYSTEM_HEADER, TAG

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",  # læse, label og sende
    "https://www.googleapis.com/auth/spreadsheets",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"


def credentials(cfg):
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None,
        refresh_token=cfg.google_refresh_token,
        token_uri=TOKEN_URI,
        client_id=cfg.google_client_id,
        client_secret=cfg.google_client_secret,
        scopes=SCOPES,
    )


@dataclass
class Attachment:
    filename: str
    mime: str
    data: bytes


@dataclass
class Mail:
    id: str
    thread_id: str
    subject: str
    sender: str           # kun selve adressen, med små bogstaver
    message_id_header: str
    date: str             # ÅÅÅÅ-MM-DD (Gmails modtagelsestidspunkt)
    labels: list[str]
    auth_results: list[str]
    is_system: bool       # mailen er sendt af systemet selv
    attachments: list[Attachment] = field(default_factory=list)


def sender_address(from_header: str) -> str:
    return parseaddr(from_header or "")[1].strip().lower()


def is_authentic(auth_results: list[str], email: str) -> bool:
    """Sand hvis Gmails egen Authentication-Results bekræfter afsenderens domæne.

    Kun headere tilføjet af mx.google.com tælles, så en afsender ikke kan
    indsætte sin egen 'dkim=pass' længere nede i mailen.
    """
    domain = email.rsplit("@", 1)[-1].lower()

    def same_domain(value: str) -> bool:
        value = value.lower().lstrip("@").rsplit("@", 1)[-1]
        return value == domain or value.endswith("." + domain)

    for header in auth_results:
        text = " ".join(header.split()).lower()
        if not text.startswith("mx.google.com"):
            continue
        m = re.search(r"dmarc=pass[^;]*header\.from=([\w.-]+)", text)
        if m and same_domain(m.group(1)):
            return True
        for m in re.finditer(r"dkim=pass[^;]*?header\.[id]=(@?[\w.-]+)", text):
            if same_domain(m.group(1)):
                return True
        m = re.search(r"spf=pass[^;]*smtp\.mailfrom=([\w.@+-]+)", text)
        if m and same_domain(m.group(1)):
            return True
    return False


def parse_message(raw: dict, fetch_attachment) -> Mail:
    """Omsæt et Gmail API-message (format=full) til Mail. fetch_attachment(id) -> bytes."""
    payload = raw.get("payload", {})
    headers: dict[str, list[str]] = {}
    for h in payload.get("headers", []):
        headers.setdefault(h["name"].lower(), []).append(h["value"])

    def first(name: str) -> str:
        return headers.get(name.lower(), [""])[0]

    attachments: list[Attachment] = []

    def walk(part: dict) -> None:
        for sub in part.get("parts", []) or []:
            walk(sub)
        filename = part.get("filename") or ""
        body = part.get("body", {}) or {}
        if not filename:
            return
        if body.get("attachmentId"):
            data = fetch_attachment(body["attachmentId"])
        elif body.get("data"):
            data = base64.urlsafe_b64decode(body["data"])
        else:
            return
        attachments.append(Attachment(filename, (part.get("mimeType") or "").lower(), data))

    walk(payload)

    from datetime import datetime, timezone

    internal_ms = int(raw.get("internalDate", "0") or 0)
    date = datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc).date().isoformat()
    return Mail(
        id=raw["id"],
        thread_id=raw.get("threadId", ""),
        subject=first("Subject"),
        sender=sender_address(first("From")),
        message_id_header=first("Message-ID") or first("Message-Id"),
        date=date,
        labels=list(raw.get("labelIds", [])),
        auth_results=headers.get("authentication-results", []),
        is_system=bool(headers.get(SYSTEM_HEADER.lower())),
        attachments=attachments,
    )


class Gmail:
    def __init__(self, creds):
        from googleapiclient.discovery import build

        self.api = build("gmail", "v1", credentials=creds, cache_discovery=False).users()
        self._label_ids: dict[str, str] | None = None

    # --- læsning -------------------------------------------------------
    def find_candidates(self, senders: tuple[str, ...], days: int) -> list[str]:
        """Id'er på mails fra afsenderne med [Lager] i emnet og vedhæftninger, ældste først."""
        from_any = "{" + " ".join(f"from:{s}" for s in senders) + "}"  # {…} = OR i Gmail-søgning
        query = f'{from_any} subject:"{TAG}" has:attachment newer_than:{days}d'
        ids: list[str] = []
        token = None
        while True:
            resp = self.api.messages().list(userId="me", q=query, pageToken=token,
                                            maxResults=100).execute()
            ids += [m["id"] for m in resp.get("messages", [])]
            token = resp.get("nextPageToken")
            if not token:
                break
        return list(reversed(ids))

    def get_message(self, msg_id: str) -> Mail:
        raw = self.api.messages().get(userId="me", id=msg_id, format="full").execute()

        def fetch(att_id: str) -> bytes:
            att = self.api.messages().attachments().get(
                userId="me", messageId=msg_id, id=att_id).execute()
            return base64.urlsafe_b64decode(att["data"])

        return parse_message(raw, fetch)

    def thread_started_by_system(self, thread_id: str) -> bool:
        """Sand hvis tråden indeholder en mail, vi selv har sendt med [Lager]-tagget."""
        thread = self.api.threads().get(userId="me", id=thread_id, format="metadata",
                                        metadataHeaders=["Subject"]).execute()
        for msg in thread.get("messages", []):
            subject = next((h["value"] for h in msg.get("payload", {}).get("headers", [])
                            if h["name"].lower() == "subject"), "")
            if "SENT" in msg.get("labelIds", []) and TAG in subject:
                return True
        return False

    def sent_recently(self, subject: str, days: int = 6) -> bool:
        resp = self.api.messages().list(
            userId="me", q=f'in:sent subject:"{subject}" newer_than:{days}d').execute()
        for m in resp.get("messages", []):
            msg = self.api.messages().get(userId="me", id=m["id"], format="metadata",
                                          metadataHeaders=["Subject"]).execute()
            headers = msg.get("payload", {}).get("headers", [])
            if any(h["name"].lower() == "subject" and h["value"].strip() == subject
                   for h in headers):
                return True
        return False

    # --- labels --------------------------------------------------------
    def _label_id(self, name: str) -> str:
        if self._label_ids is None:
            labels = self.api.labels().list(userId="me").execute().get("labels", [])
            self._label_ids = {lab["name"]: lab["id"] for lab in labels}
        if name not in self._label_ids:
            parent = name.split("/")[0]
            for label in ([parent] if "/" in name else []) + [name]:
                if label not in self._label_ids:
                    created = self.api.labels().create(userId="me", body={
                        "name": label, "labelListVisibility": "labelShow",
                        "messageListVisibility": "show"}).execute()
                    self._label_ids[label] = created["id"]
        return self._label_ids[name]

    def has_label(self, mail: Mail, name: str) -> bool:
        return self._label_id(name) in mail.labels

    def add_label(self, msg_id: str, name: str) -> None:
        self.api.messages().modify(userId="me", id=msg_id,
                                   body={"addLabelIds": [self._label_id(name)]}).execute()

    # --- afsendelse ----------------------------------------------------
    def send(self, to: str, subject: str, body: str, *, thread_id: str | None = None,
             in_reply_to: str | None = None) -> str:
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        msg[SYSTEM_HEADER] = "1"
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)
        request = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if thread_id:
            request["threadId"] = thread_id
        return self.api.messages().send(userId="me", body=request).execute()["id"]
