# Lagersystem

Et lager i Google Sheets, der opdaterer sig selv ud fra mails:

1. **Hver mandag kl. 08:00** sender systemet en mail med emnet `[Lager] Uge NN` og beder om slutsedler fra weekendens salg og følgesedler fra nye leverancer.
2. Modtageren **svarer på mailen** og vedhæfter fotos, PDF'er eller Excel-filer.
3. **Hver time** finder systemet nye svar. Claude Haiku læser hvert bilag, og systemet gør derefter følgende:
   - lægger leverancer til lageret,
   - trækker solgte varer fra,
   - lægger usikre linjer i arket **Til gennemgang**,
   - sender en kvittering i samme tråd.

Alt kører i GitHub Actions. Du behøver ikke en server.

---

## Indhold

- [Opsætning (én gang)](#opsætning-én-gang)
- [Produkter, enheder og aliaser](#produkter-enheder-og-aliaser)
- [Lokationer og statistik](#lokationer-og-statistik)
- [Linjer til gennemgang](#linjer-til-gennemgang)
- [Rette fejl med Korrektion-rækker](#rette-fejl-med-korrektion-rækker)
- [Køre workflows manuelt](#køre-workflows-manuelt)
- [Køre lokalt og dry-run](#køre-lokalt-og-dry-run)
- [Sådan beskytter systemet lageret](#sådan-beskytter-systemet-lageret)
- [Fejlfinding](#fejlfinding)

---

## Opsætning (én gang)

Du skal bruge:

- den Google-konto, systemet skal køre på (dens Gmail sender og modtager mails, og den ejer regnearket),
- en GitHub-konto,
- et Claude-abonnement (Pro eller Max). Systemet kører på abonnementet, så der kræves ingen betalt API-nøgle,
- Python 3.11 eller nyere og Claude Code på din pc.

### 1. Claude-token (abonnement)

Åbn PowerShell og kør:

```powershell
claude setup-token
```

Log ind med den Claude-konto, systemet skal bruge. Kommandoen udskriver en token, der starter med `sk-ant-oat…` og holder i cirka et år. Gem den til trin 7 som `CLAUDE_CODE_OAUTH_TOKEN`.

Haiku-kaldene trækker af abonnementets forbrug. Med Haiku og få bilag om ugen er det meget lidt.

### 2. Google Cloud-projekt

1. Gå til <https://console.cloud.google.com> og log ind med systemets Google-konto.
2. Opret et nyt projekt, fx `lager`, via projektvælgeren øverst og **New project**.
3. Slå de to API'er til under **APIs & Services → Library**:
   - **Gmail API** → **Enable**
   - **Google Sheets API** → **Enable**

### 3. OAuth-samtykkeskærm

Åbn **APIs & Services → OAuth consent screen**. Siden kan også hedde **Google Auth Platform**.

1. **Branding:** Udfyld appnavn (fx `Lager`), support-e-mail og udvikler-e-mail.
2. **Audience:** Vælg **External**.
3. **Data access:** Vælg **Add or remove scopes** og tilføj de to scopes:
   - `https://www.googleapis.com/auth/gmail.modify`
   - `https://www.googleapis.com/auth/spreadsheets`
4. **Vigtigt: udgiv appen.** Gå til **Audience** og klik **Publish app**, så status skifter fra *Testing* til **In production**.

> ⚠️ **Refresh tokens udløber efter 7 dage, hvis appen står i "Testing".** Så holder systemet op med at virke efter en uge med fejlen `invalid_grant`. Sæt derfor appen til **In production**, *før* du laver din refresh token i trin 5. Har du lavet tokenen, mens appen stod i Testing, så lav en ny efter at have udgivet appen.
>
> Du behøver ikke sende appen til Googles verifikation, fordi kun du bruger den. Når du logger ind, viser Google en advarsel om, at appen ikke er verificeret. Klik **Avanceret → Gå til Lager (usikker)**. Det er din egen app.

### 4. OAuth-klient

1. Gå til **APIs & Services → Credentials** (eller **Clients**) og vælg **Create credentials → OAuth client ID**.
2. Vælg application type **Desktop app**, og giv klienten et navn, fx `lager-desktop`.
3. Klik **Download JSON** og gem filen *uden for* repo-mappen, fx i `Overførsler`. Den må aldrig committes.

### Genvej til trin 5–7: ét script

Når repoet ligger på GitHub, og GitHub CLI er logget ind (`gh auth login`), kan trin 5, 6 og 7.3 køres i én kommando:

```powershell
.venv\Scripts\python scripts\bootstrap.py "$HOME\Downloads\client_secret_XXXX.json" --min-email ADRESSEN-DER-SVARER
```

Scriptet gør tre ting:
1. Åbner Googles login.
2. Opretter regnearket.
3. Gemmer `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `SHEET_ID` og `MIN_EMAIL` som GitHub Secrets.

Ingen af værdierne bliver vist på skærmen. Bagefter mangler kun Claude-tokenen:

```powershell
gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo EJER/lager
```

Kommandoen beder dig indsætte tokenen fra `claude setup-token`.

### 5. Refresh token (lokalt)

Åbn PowerShell i repo-mappen og kør:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python scripts/get_refresh_token.py "$HOME\Downloads\client_secret_XXXX.json"
```

Browseren åbner. Log ind med systemets Google-konto og godkend adgangen. Scriptet udskriver derefter tre værdier: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` og `GOOGLE_REFRESH_TOKEN`.

### 6. Opret regnearket

Brug værdierne fra trin 5 i samme PowerShell-vindue:

```powershell
$env:GOOGLE_CLIENT_ID = "..."
$env:GOOGLE_CLIENT_SECRET = "..."
$env:GOOGLE_REFRESH_TOKEN = "..."
python -m lager.main setup
```

Kommandoen opretter regnearket **Lager** i systemkontoens Google Drive med de fire ark, formlen og formateringen. Til sidst udskriver den regnearkets id, som du skal gemme som `SHEET_ID`.

Har du allerede et regneark, så sæt `$env:SHEET_ID = "..."` før `setup`. Så tilføjes kun de ark, der mangler. Eksisterende data røres ikke.

### 7. GitHub-repo og Secrets

1. Opret et **privat** repo på GitHub (**New repository → Private**), fx `lager`.
2. Skub koden op fra repo-mappen:

   ```powershell
   git init
   git add .
   git commit -m "Lagersystem"
   git branch -M main
   git remote add origin https://github.com/DIT-BRUGERNAVN/lager.git
   git push -u origin main
   ```

3. Gå i repoet til **Settings → Secrets and variables → Actions → New repository secret** og opret:

   | Secret | Værdi |
   |---|---|
   | `CLAUDE_CODE_OAUTH_TOKEN` | tokenen fra trin 1 |
   | `GOOGLE_CLIENT_ID` | fra trin 5 |
   | `GOOGLE_CLIENT_SECRET` | fra trin 5 |
   | `GOOGLE_REFRESH_TOKEN` | fra trin 5 |
   | `SHEET_ID` | fra trin 6 |
   | `MIN_EMAIL` | den eller de adresser, der skal have mandagsmailen og kvitteringerne og må svare med bilag. Flere adresser adskilles med komma, fx `kollega@firma.dk, din@gmail.com`. |

4. Valgfrit: Under fanen **Variables** kan du oprette:
   - `SIKKERHEDSTAERSKEL`, fx `0.85` (standard). Linjer med lavere sikkerhed går til gennemgang.
   - `CLAUDE_MODEL`, hvis du vil bruge en anden model end `claude-haiku-5-5`. Aliaset `haiku` giver altid den nyeste Haiku.
5. Slet client_secret-JSON-filen fra trin 4, eller gem den et sikkert sted uden for repoet.

### 8. Test

1. Kør **weekly_request** manuelt med *dry_run* slået til (se [Køre workflows manuelt](#køre-workflows-manuelt)), og tjek loggen.
2. Kør den igen uden dry_run. Så kommer mandagsmailen frem.
3. Svar på mailen fra arbejdsmailen med en slutseddel vedhæftet.
4. Kør **process_replies** manuelt og tjek kvitteringen og arket.

---

## Produkter, enheder og aliaser

Arket **Produkter** har én række pr. vare:

| Kolonne | Betydning |
|---|---|
| **Varenr** | Unik nøgle, fx `101` eller `SO-1`. Ændr den aldrig, når der først er bevægelser på varen. |
| **Navn** | Navnet, som det oftest står på sedlerne. |
| **Aliaser** | Andre navne, kommasepareret. Systemet matcher både navn og aliaser. |
| **Enhed** | Grundenheden, lageret tælles i, fx `flaske`, `stk` eller `100 g`. |
| **Omregning** | Andre enheder omregnet til grundenheden, fx `kasse=6` eller `kg=10`. |
| **Minimumsbeholdning** | Rækken bliver rød, når beholdningen er under dette tal, og varen nævnes i kvitteringen. |
| **Startbeholdning** | Beholdningen den dag, du begynder at bruge systemet. |
| **Aktuel beholdning** | Udregnes automatisk. **Skriv aldrig i denne kolonne.** |
| Kategori, Kostpris, Salgspris, Leverandør, Noter | Til dig. Systemet bruger dem ikke. |

**Eksempel med svensk ost.** Alle svenske oste sælges som "Svensk ost" på slutsedlen, så de skal være *én* vare:

| Varenr | Navn | Aliaser | Enhed | Omregning |
|---|---|---|---|---|
| 101 | Svensk ost | Wrångebäck, Wrångeback, Søblå | 100 g | kg=10 |
| 102 | Lihmskov | | flaske | kasse=6 |

Systemet regner sådan med de to varer:

- En leverance på "15 kg Wrångeback" bliver til **+150** (15 kg × 10).
- Slutsedlens "Svensk ost 338,85" bliver til **−338,85**.
- "4 kasser Lihmskov" bliver til **+24** flasker.

`stk`, `flaske`, `glas`, `dåse`, `bøtte`, `pakke` og `x` regnes 1:1 for hinanden. Andre enheder kræver en omregning. Mangler den, går linjen til gennemgang, og kvitteringen fortæller dig, hvad du skal tilføje.

**Tilføj en vare:** Skriv en ny række i Produkter. Den er med fra næste kørsel.

**Nye varer oprettes automatisk.** Står der en vare på en seddel, som Claude er sikker på ikke findes i Produkter (heller ikke under et andet navn eller alias), gør systemet følgende:
- opretter varen i Produkter med næste ledige varenummer, et rent varenavn og enheden fra sedlen,
- skriver "Oprettet automatisk …" i Noter,
- bogfører antallet med det samme,
- nævner den nye vare i kvitteringen.

Tjek nye varer, og udfyld fx Startbeholdning, Minimumsbeholdning og Omregning. Er en ny vare i virkeligheden en vare, du allerede har:
1. Tilføj navnet som alias på den rigtige vare.
2. Flyt antallet med to Korrektion-rækker: minus på den nye vare og plus på den rigtige.
3. Slet den nye vare i Produkter.

**Tilføj et alias:** Kommer en vare igen og igen til gennemgang under et andet navn, så tilføj navnet under Aliaser.

---

## Lokationer og statistik

Arket **Lokationer** har én række pr. sted, I sælger:

| Kolonne | Betydning |
|---|---|
| **Lokation** | Det faste navn, fx `Plantorama Odense`. Statistikken bruger det. |
| **Gruppe** | Valgfri. Kæden eller typen, fx `Plantorama`. Har flere lokationer samme gruppe, får statistikken også en kolonne med gruppen samlet. |
| **Andre navne** | Andre navne, stedet kan stå under på slutsedlen, kommasepareret. |

Claude kobler standen på hver slutseddel til en lokation, og lokationen gemmes på salgslinjerne i Bevægelser. Kvitteringen viser, hvilken lokation salget er bogført på. Kan standen ikke kobles sikkert, bliver varerne stadig trukket fra lageret, men salget står under *(ukendt lokation)* i statistikken, og kvitteringen fortæller dig, hvad du skal tilføje i Lokationer. Kolonnen Lokation i Bevægelser må du gerne rette i bagefter, fordi den ikke påvirker beholdningen.

Arket **Statistik** bliver skrevet forfra ved hver kørsel og viser:
- **Omsætning (kr.) pr. varegruppe og lokation.** Det kræver, at *Kategori* (varegruppe) og *Salgspris* er udfyldt i Produkter.
- **Andel af lokationens omsætning pr. varegruppe (%).** Det er den bedste måde at sammenligne stande af forskellig størrelse.
- **Antal solgt pr. vare og lokation**, i varens egen enhed.

Ret ikke i arket Statistik, for dine ændringer bliver overskrevet ved næste kørsel. Vil du lave egne beregninger, så lav dem i et andet ark.

## Linjer til gennemgang

Følgende linjer bogføres **ikke**. De lægges i arket **Til gennemgang** i stedet:

- linjer, systemet ikke sikkert kan matche,
- linjer med lav sikkerhed,
- linjer uden antal,
- linjer med en ukendt enhed,
- linjer med en vare, som Claude ikke er sikker på, om findes i Produkter.

Varer, som Claude er sikker på er nye, oprettes og bogføres automatisk. Se [Produkter, enheder og aliaser](#produkter-enheder-og-aliaser).

Sådan behandler du en linje:

1. Opret varen i Produkter, hvis den mangler.
2. Udfyld **Godkendt varenr** og, hvis det er nødvendigt, **Godkendt antal**. Antallet skal være i produktets grundenhed og uden fortegn. Systemet sætter selv minus på salg.
3. Sæt **Status** til **Godkendt**.

Ved næste kørsel bogføres linjen, og status skifter til **Bogført**. Skriver systemet `Fejl: …` i Status, så ret det, der står, og sæt status til *Godkendt* igen. Linjer, du vil se bort fra, sætter du til **Afvist**.

---

## Rette fejl med Korrektion-rækker

**Bevægelser** er lagerets historik. Gamle rækker må **aldrig rettes eller slettes**. Du retter en fejl ved at tilføje en ny række nederst:

| Kolonne | Udfyld |
|---|---|
| Tidspunkt | nu, fx `2026-11-18 14:00` |
| Dato | den dato, rettelsen gælder |
| Type | `Korrektion` |
| Varenr | varens nummer |
| Antal | `+` lægger til, `-` trækker fra |
| Kilde | `manuel: kort begrundelse` |
| Status | `Bogført` |

**Eksempel:** 125 Lihmskov blev trukket, men det rigtige var 115. Tilføj en Korrektion med Antal `10`.

En optælling ved varetælling rettes på samme måde. Læg forskellen mellem optalt og *Aktuel beholdning* ind som en Korrektion.

---

## Køre workflows manuelt

1. Gå til fanen **Actions** i GitHub-repoet.
2. Vælg **weekly_request** eller **process_replies** i venstre side.
3. Klik **Run workflow**. Sæt eventuelt flueben i *dry_run* og klik **Run workflow** igen.

En manuel `weekly_request` sender mandagsmailen med det samme, uanset dag og om den allerede er sendt.

**Genbehandl en mail:** I `process_replies` kan du i feltet *genbehandl* skrive en mails Gmail message-id. Id'et står i kolonnen Message-id i arket Behandlede mails. Så læses mailens bilag igen, og mailens gamle linjer i Til gennemgang får status *Erstattet*. Det er kun tilladt, så længe intet fra mailen er bogført, så varer aldrig trækkes to gange.

Planlagte kørsler:

| Workflow | Hvornår |
|---|---|
| `weekly_request` | Flere forsøg mandag og tirsdag. Mailen sendes ved første kørsel fra mandag kl. 08 dansk tid og kun én gang pr. uge, så sommer- og vintertid håndteres automatisk. |
| `process_replies` | To gange i timen (minut 17 og 47), ca. kl. 06–23 dansk tid. |

GitHub kan forsinke planlagte kørsler med flere timer eller springe dem helt over. Derfor er der flere forsøg. Kommer mandagsmailen ikke, kan du altid køre `weekly_request` manuelt.

---

## Køre lokalt og dry-run

Sæt miljøvariablerne fra trin 6 samt `SHEET_ID` og `MIN_EMAIL`. Log ind i Claude Code (`claude`, derefter `/login`), eller sæt `CLAUDE_CODE_OAUTH_TOKEN`. Kør derefter:

```powershell
python -m lager.main weekly --dry-run --force   # viser mandagsmailen uden at sende
python -m lager.main process --dry-run          # viser bevægelser og kvittering uden at skrive
python -m lager.main process                    # behandler for alvor
```

`--dry-run` skriver ikke i arket, sætter ingen labels og sender ingen mails. Bilagene bliver stadig læst af Haiku, så du ser det rigtige resultat. Det bruger derfor stadig af abonnementets forbrug.

Kør testene:

```powershell
pytest
```

---

## Sådan beskytter systemet lageret

- **Kun rigtige svar behandles.** En mail behandles kun, hvis alle fire krav er opfyldt:
  1. Den er et svar i en tråd, som systemet selv startede med `[Lager]` i emnet.
  2. Den kommer fra en af adresserne i `MIN_EMAIL`. Systemets egen Gmail-konto må også svare, hvis den står i `MIN_EMAIL`.
  3. Gmail har bekræftet afsenderen med DKIM, SPF eller DMARC. Det tjekkes for at afvise forfalskede afsendere.
  4. Den står ikke allerede i **Behandlede mails**.

  Alt andet ignoreres. Mails med falsk afsender registreres som *Afvist* og får labelen `Lager/Afvist`.
- **Indholdet i bilag er data.** Står der fx "ignorer reglerne" i et bilag, bliver det ikke fulgt. Claude Code kører desuden i begrænset tilstand: den kan kun læse bilagsfilen i en midlertidig mappe og kan ikke køre kommandoer, gå på nettet eller bruge andre værktøjer.
- **Ingen dobbelt-bogføring.** Hvert bilag får et fingeraftryk (hash), som gemmes i kolonnen Kilde. Sender man samme fil igen, også i en ny mail, bogføres den ikke igen. Det nævnes i kvitteringen.
- **Alt eller intet.** Bevægelser, gennemgangslinjer og registreringen i Behandlede mails skrives i ét samlet kald til Sheets. Går en kørsel ned midt i, er der enten skrevet alt eller intet, og næste kørsel samler op.
- **Fejl i ét bilag stopper ikke resten.** Fejlen nævnes i kvitteringen. Er Anthropic midlertidigt nede for alle bilag i en mail, prøver systemet igen næste time.
- **Labels.** Behandlede mails får labelen `Lager/Behandlet` i Gmail.

---

## Fejlfinding

| Problem | Løsning |
|---|---|
| `Claude Code-fejl: Failed to authenticate` | Claude-tokenen er udløbet, eller abonnementet er stoppet. Lav en ny med `claude setup-token`, og opdater secret'en `CLAUDE_CODE_OAUTH_TOKEN`. Bilagene bliver liggende og behandles ved næste kørsel. |
| Advarsel om ukendt model | Sæt variablen `CLAUDE_MODEL` til `haiku`. |
| `invalid_grant` i loggen | Din refresh token er udløbet eller tilbagekaldt. Tjek, at OAuth-appen står i **In production** (trin 3), og lav en ny token (trin 5). |
| Svar bliver ikke behandlet | Er svaret sendt fra `MIN_EMAIL` og som svar i tråden, ikke som en ny mail? Har mailen en vedhæftning? Systemet ser 21 dage tilbage. |
| Mail registreret som "Afvist" | Arbejdsmailens domæne bestod hverken DKIM, SPF eller DMARC. Bed din mailudbyder slå DKIM til. I nødstilfælde kan du sætte variablen `KRAEV_AFSENDERTJEK` til `nej`. |
| Mange linjer til gennemgang | Tilføj aliaser og omregninger i Produkter, eller sænk `SIKKERHEDSTAERSKEL` en smule. |
| "Aktuel beholdning" viser en fejl | Der er skrevet i kolonnen, eller kolonner er flyttet. Slet indholdet under overskriften, og kør `setup` på et tomt ark for at se den oprindelige formel. |

**Omkostninger:** Systemet koster ikke ekstra:

- `process_replies` kører op til cirka 36 gange om dagen, svarende til højst omkring 1.100 GitHub Actions-minutter om måneden. Private repos har 2.000 gratis minutter om måneden.
- Haiku læser hvert bilag på den højeste effort-indstilling (`max`), og kaldet trækker af dit Claude-abonnement.
- Kørsler uden nye mails kalder slet ikke Claude.
