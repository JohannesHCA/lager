"""Generér en Google refresh token til lagersystemet. Køres ÉN gang lokalt.

    python scripts/get_refresh_token.py sti/til/client_secret.json

Browseren åbner; log ind med den Google-konto, systemet skal køre på, og godkend.
Scriptet udskriver GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET og GOOGLE_REFRESH_TOKEN,
som du lægger i GitHub Secrets. Gem dem ikke i repoet, og slet client_secret-filen bagefter
(eller læg den uden for repo-mappen).
"""
import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    with open(sys.argv[1], encoding="utf-8") as fh:
        client_config = json.load(fh)
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    # prompt=consent sikrer, at Google altid udleverer en refresh token.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        sys.exit("Google returnerede ingen refresh token. Fjern appens adgang på "
                 "https://myaccount.google.com/permissions og prøv igen.")
    section = client_config.get("installed") or client_config.get("web") or {}
    print("\nLæg disse tre værdier i GitHub Secrets (Settings → Secrets and variables → Actions):\n")
    print(f"GOOGLE_CLIENT_ID      = {section.get('client_id')}")
    print(f"GOOGLE_CLIENT_SECRET  = {section.get('client_secret')}")
    print(f"GOOGLE_REFRESH_TOKEN  = {creds.refresh_token}")
    print("\nDel dem aldrig med andre, og commit dem aldrig.")


if __name__ == "__main__":
    main()
