"""Konfiguration fra miljøvariabler (GitHub Secrets i Actions, .env-fri lokalt)."""
from __future__ import annotations

import os
from dataclasses import dataclass

TAG = "[Lager]"
SYSTEM_HEADER = "X-Lager-System"  # sættes på alle mails, systemet selv sender
LABEL_DONE = "Lager/Behandlet"
LABEL_REJECTED = "Lager/Afvist"
TIMEZONE = "Europe/Copenhagen"
MODEL = os.environ.get("CLAUDE_MODEL", "").strip() or "claude-haiku-5-5"
EFFORT = "max"  # højeste effort-niveau for Claude Haiku 5.5 (default er "medium")


def parse_emails(text: str) -> tuple[str, ...]:
    """'Kollega@Firma.dk, din@gmail.com' -> ('kollega@firma.dk', 'din@gmail.com')"""
    out: list[str] = []
    for part in text.replace(";", ",").split(","):
        email = part.strip().lower()
        if email and email not in out:
            out.append(email)
    return tuple(out)


@dataclass(frozen=True)
class Config:
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str
    sheet_id: str
    emails: tuple[str, ...]  # MIN_EMAIL: får mails og må svare med bilag (kommasepareret)
    threshold: float = 0.85
    require_auth: bool = True
    lookback_days: int = 21

    @property
    def sheet_url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.sheet_id}"

    @classmethod
    def from_env(cls, *, need_sheet: bool = True, need_claude: bool = True,
                 need_min_email: bool = True) -> "Config":
        required = ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"]
        if need_sheet:
            required.append("SHEET_ID")
        if need_min_email:
            required.append("MIN_EMAIL")
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        # Claude Code logger ind med abonnements-tokenen (eller alternativt en API-nøgle).
        if need_claude and not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
                                or os.environ.get("ANTHROPIC_API_KEY", "").strip()):
            missing.append("CLAUDE_CODE_OAUTH_TOKEN")
        if missing:
            raise SystemExit("Mangler miljøvariabler: " + ", ".join(missing))

        def env(name: str, default: str = "") -> str:
            return os.environ.get(name, default).strip()

        return cls(
            google_client_id=env("GOOGLE_CLIENT_ID"),
            google_client_secret=env("GOOGLE_CLIENT_SECRET"),
            google_refresh_token=env("GOOGLE_REFRESH_TOKEN"),
            sheet_id=env("SHEET_ID"),
            emails=parse_emails(env("MIN_EMAIL")),
            threshold=float(env("SIKKERHEDSTAERSKEL", "0.85").replace(",", ".")),
            require_auth=env("KRAEV_AFSENDERTJEK", "ja").lower() not in ("nej", "no", "false", "0"),
            lookback_days=int(env("SOEG_DAGE_TILBAGE", "21")),
        )
