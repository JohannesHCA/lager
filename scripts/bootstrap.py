"""Engangsopsætning: Google-login, regneark og GitHub Secrets i én kørsel.

    .venv\\Scripts\\python scripts\\bootstrap.py "STI\\TIL\\client_secret_....json" --min-email kollega@firma.dk

Hvad scriptet gør:
  1. Åbner Googles login i din browser. Log ind med den Google-konto, systemet skal køre på.
  2. Opretter regnearket "Lager" (eller bruger --sheet-id) med de fire ark.
  3. Gemmer GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN, SHEET_ID og
     MIN_EMAIL som GitHub Secrets i repoet via GitHub CLI (gh).

Ingen hemmelige værdier skrives på skærmen eller på disk.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lager.gmail import SCOPES  # noqa: E402


def fail(message: str) -> None:
    sys.exit(f"\nFEJL: {message}")


def gh(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], input=input_text, capture_output=True, text=True,
                          encoding="utf-8", cwd=ROOT)


def detect_repo() -> str:
    result = gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    if result.returncode != 0 or not result.stdout.strip():
        fail("kunne ikke finde GitHub-repoet. Angiv det med --repo EJER/NAVN.")
    return result.stdout.strip()


def set_secret(repo: str, name: str, value: str) -> None:
    result = gh("secret", "set", name, "--repo", repo, input_text=value)
    if result.returncode != 0:
        fail(f"kunne ikke gemme secret {name}: {result.stderr.strip()}")
    print(f"  ✓ {name}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("client_secret_json", help="JSON-filen hentet fra Google Cloud (OAuth-klient)")
    parser.add_argument("--min-email", required=True,
                        help="adresse(r), der får mandagsmailen og må svare med bilag, "
                             "kommasepareret (bliver til MIN_EMAIL)")
    parser.add_argument("--repo", help="GitHub-repo som EJER/NAVN (standard: dette repo)")
    parser.add_argument("--sheet-id", help="brug et eksisterende regneark i stedet for at oprette et")
    args = parser.parse_args()

    if not shutil.which("gh"):
        fail("GitHub CLI (gh) er ikke installeret.")
    if gh("auth", "status").returncode != 0:
        fail("GitHub CLI er ikke logget ind. Kør 'gh auth login' først.")
    repo = args.repo or detect_repo()
    from lager.config import parse_emails

    emails = parse_emails(args.min_email)
    if not emails or any("@" not in e for e in emails):
        fail("--min-email skal være en eller flere e-mailadresser (kommasepareret).")

    path = Path(args.client_secret_json).expanduser()
    if not path.is_file():
        fail(f"filen findes ikke: {path}")
    client_config = json.loads(path.read_text(encoding="utf-8"))
    section = client_config.get("installed") or client_config.get("web") or {}
    if not section.get("client_id") or not section.get("client_secret"):
        fail("JSON-filen ligner ikke en OAuth-klient fra Google Cloud.")

    print(f"Repo: {repo}")
    print("\n1/3  Åbner Googles login i din browser …")
    print("     Log ind med systemets Google-konto. Ved advarslen om at appen ikke er")
    print("     verificeret: klik 'Avanceret' → 'Gå til Lager', og tillad adgangen.")
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent",
                                  open_browser=True)
    if not creds.refresh_token:
        fail("Google returnerede ingen refresh token. Fjern appens adgang på "
             "https://myaccount.google.com/permissions og prøv igen.")
    account = build("gmail", "v1", credentials=creds,
                    cache_discovery=False).users().getProfile(userId="me").execute()
    print(f"     Logget ind som {account['emailAddress']}")

    # Gem Google-adgangen først, så den ikke går tabt, hvis regnearkstrinnet fejler.
    print(f"\n2/3  Gemmer GitHub Secrets i {repo} …")
    set_secret(repo, "GOOGLE_CLIENT_ID", section["client_id"])
    set_secret(repo, "GOOGLE_CLIENT_SECRET", section["client_secret"])
    set_secret(repo, "GOOGLE_REFRESH_TOKEN", creds.refresh_token)
    set_secret(repo, "MIN_EMAIL", ", ".join(emails))

    from lager.sheets import Sheets

    print("\n3/3  Regneark …")
    sheet_id = args.sheet_id
    if not sheet_id:
        sheet_id = Sheets.create(creds)
        print(f"     Oprettede 'Lager': https://docs.google.com/spreadsheets/d/{sheet_id}")
    set_secret(repo, "SHEET_ID", sheet_id)
    for line in Sheets(creds, sheet_id).setup():
        print(f"     {line}")

    print("\nFærdig. Tilbage mangler kun Claude-tokenen:")
    print("  claude setup-token")
    print(f"  gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo {repo}")
    print("(den sidste kommando beder dig indsætte tokenen; den vises ikke)")
    print(f"\nRegneark: https://docs.google.com/spreadsheets/d/{sheet_id}")
    print("Du kan nu slette client_secret-JSON-filen fra Overførsler.")


if __name__ == "__main__":
    main()
