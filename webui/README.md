# Webinterface (fork)

Een eenvoudige, met wachtwoord beveiligde website bij Free Games Claimer Remaster.
Iedere gebruiker heeft een **eigen bot** (eigen container, eigen winkelaccounts) en kan op de site:

- winkels aan- en uitzetten en de inloggegevens invullen;
- de planning, meldingen (Discord / Apprise) en een paar andere opties instellen;
- zien wat de bot gedaan heeft: status, laatste en volgende run, geclaimde games per winkel,
  problemen van de laatste run en het logboek;
- op **Nu uitvoeren** drukken en in de **Takenlijst** zien wanneer de run aan de beurt is;
- het **scherm van de bot** openen (noVNC) om zelf in te loggen bij een winkel.

Alles hieronder gebruikt voorbeeldnamen (`user1`, `user2`, `<server-ip>`, `games.example.org`).

## Hoe het in elkaar zit

```
internet ──https──> Nginx Proxy Manager ──http──> webui (poort 8090, de enige gepubliceerde poort)
                                                    │  leest/schrijft bestanden in de volumes
                                                    │  proxyt noVNC (http + websocket) na inloggen
                     ┌──────────────────────────────┼──────────────────────────────┐
                 fgc-user1 (bot)                fgc-user2 (bot)                     ...
                 volume fgc_data_user1           volume fgc_data_user2
                         └──────── gedeeld volume fgc_queue: takenlijst (jobs.db) ────────┘
```

- **Eén bot per persoon.** De bot kan maar één set inloggegevens per winkel aan, dus elke persoon
  krijgt een eigen container `fgc-<naam>` met een eigen datavolume en een eigen VNC-wachtwoord.
  Elke bot is de ongewijzigde upstream-service `app` (via `extends`), alleen met een eigen volume,
  zonder gepubliceerde poorten.
- **Instellingen** schrijft de webinterface naar `data/config.env` in het volume van die bot. Dat
  bestand leest de bot zelf al (upstream `src/core/config.py`). Omdat de bot zijn instellingen alleen
  bij het opstarten leest, vraagt de webinterface na *Opslaan* om een herstart; de bot stopt dan
  zichzelf **tussen twee runs** en Docker (`restart: unless-stopped`) start hem opnieuw.
- **Geen Docker-socket.** De webcontainer heeft geen toegang tot Docker. Hij praat met de bots
  alleen via kleine bestanden in hun volume (`data/webui/`): `status.json` en `bot.log` (van de
  bot) en `request-restart.json` (van de webinterface), plus de gedeelde takenlijst. Een
  docker-socket(-proxy) die containers mag herstarten of aanmaken is in feite root op de server;
  dat risico is zo helemaal vermeden. Bots toevoegen gebeurt daarom op de server met `bots.sh`.
- **Takenlijst met workers.** Elke run is een *taak* in een gedeelde takenlijst: een SQLite-bestand
  (`jobs.db`, WAL-modus) in het volume `fgc_queue`, dat alle bots en de webinterface mounten.
  - De eigen planning van een bot en *Nu uitvoeren* **zetten alleen een taak in de rij**. Per bot
    staat er hooguit één taak te wachten (een tweede aanvraag wordt genegeerd).
  - Een bot start pas een run als hij een taak heeft **geclaimd**. Claimen gebeurt in één
    schrijftransactie (`BEGIN IMMEDIATE`), dus twee bots kunnen nooit dezelfde laatste plek pakken.
  - Er lopen hooguit `FGC_WORKERS` runs tegelijk (standaard **1**; elke run is een Chrome).
    `FGC_WORKERS=auto` rekent het uit: **floor((geheugen − 1 GB) / 1,5 GB)**, minimaal 1 en
    maximaal 4. "Geheugen" is de cgroup-limiet van de container als die er is, anders `MemTotal`.
    Voorbeeld: 4 GB → 2, 8 GB → 4.
  - Volgorde: **wie het eerst komt** (FIFO over alle bots). Een bot die al bezig is of al een
    minuut geen teken van leven gaf, houdt de rij niet op.
  - Een lopende taak krijgt elke 15 seconden een heartbeat. Hoort de takenlijst 2 minuten niets
    (bot gecrasht of gestopt), dan wordt de taak *mislukt* en komt de plek vrij. Duurt een run
    langer dan `FGC_RUN_TIMEOUT_MINUTES` (standaard 180), dan wordt hij *te lang bezig* en herstart
    de bot zichzelf. Een bot die opnieuw start, markeert een afgebroken run van zichzelf als mislukt.
  - Een taak die 24 uur blijft wachten (bot niet bereikbaar) vervalt. Afgeronde taken blijven
    30 dagen bewaard (maximaal 1000).
  - Daardoor mogen meerdere bots gewoon dezelfde planning hebben. Elke nieuwe bot krijgt dezelfde
    standaard: dagelijks om 06:00 en 18:00 (Europe/Amsterdam), niet direct bij het opstarten.
  - Op de pagina **Takenlijst** ziet een gebruiker de eigen taken (plek in de rij, hoe lang bezig,
    geschiedenis met resultaat: aantal geclaimd/mislukt en meldingen). De beheerder ziet de taken
    van alle bots en het aantal workers, en kan wachtende taken annuleren. Een gebruiker kan
    een eigen wachtende taak ook annuleren.
- **noVNC via de webinterface.** De noVNC-poorten van de bots worden niet gepubliceerd. Onder
  `/vnc/` toont de webinterface het scherm van **jouw eigen** bot: de pagina én de websocket
  controleren je login (en de Origin), en verbinden alleen met de bot die aan jouw account
  gekoppeld is. Het VNC-wachtwoord blijft daarnaast bestaan (staat op je overzicht).

### Wat er aan upstream-bestanden is veranderd (makkelijk mergen)

Alle eigen code staat in `webui/`. Daarbuiten, gemarkeerd met `[fork:webui]`:

| Bestand | Wijziging |
|---|---|
| `main.py` | één klein blok: na `scheduler.start()` wordt `webui.bridge.attach(...)` aangeroepen |
| `.env.example` | sectie *WEB INTERFACE* met de nieuwe variabelen |
| `.gitignore`, `.dockerignore` | gegenereerde override en VNC-wachtwoorden buiten git en image |
| `tests/test_docs_env.py` | de nieuwe variabelen gelden als Docker-instellingen |

`docker-compose.yml` is **niet** aangepast. `bots.sh` schrijft een `docker-compose.override.yml`,
die Docker Compose automatisch meeleest (voorbeeld: `webui/compose.example.yml`).

## Installatie (server met Docker)

Nodig: Docker met Docker Compose **2.24 of nieuwer** (`docker compose version`).

1. Code ophalen en `.env` maken:

   ```sh
   git clone https://github.com/sjkanon/Free-Games-Claimer-Remaster.git
   cd Free-Games-Claimer-Remaster
   cp .env.example .env
   ```

2. In `.env` minstens dit invullen (haal het `#` weg):

   ```sh
   WEBUI_ADMIN_USER=user1
   WEBUI_ADMIN_PASSWORD=een-lang-wachtwoord   # minstens 10 tekens
   WEBUI_PUBLIC_URL=https://games.example.org # voor links in meldingen
   WEBUI_TRUSTED_PROXIES=<ip-van-npm>         # zie Nginx Proxy Manager hieronder
   ```

   Winkelaccounts horen **niet** in `.env`: die vul je in de webinterface in.

3. Een bot per persoon maken; dit start ook alles (`docker compose up -d --build`):

   ```sh
   ./webui/bots.sh add user1
   ./webui/bots.sh add user2
   ```

4. Open `https://games.example.org` (of `http://<server-ip>:8090`, zie *Zonder HTTPS*), log in als
   `user1` en ga naar **Beheer**: maak daar het account voor `user2` aan en koppel bot `user2`.
   De eerste beheerder wordt automatisch aan de bot met dezelfde naam gekoppeld.
   Haal daarna `WEBUI_ADMIN_PASSWORD` weg uit `.env`; het wordt alleen gebruikt zolang er nog
   geen accounts zijn.

Zonder accounts én zonder `WEBUI_ADMIN_USER`/`WEBUI_ADMIN_PASSWORD` weigert de webinterface te
starten, met een duidelijke melding in `docker compose logs webui`.

### Later nog iemand toevoegen of weghalen

```sh
./webui/bots.sh add user3      # nieuwe bot, daarna in Beheer een account aanmaken en koppelen
./webui/bots.sh remove user3   # container weg; het datavolume blijft bewaard
./webui/bots.sh list
```

`bots.sh` bewaart de lijst en per bot een env-bestand met het VNC-wachtwoord in `webui/bots/`
(alleen leesbaar voor de eigenaar, niet in git, niet in een image).

### Bijwerken

```sh
git pull && docker compose up -d --build
```

## Nginx Proxy Manager (NPM)

TLS eindigt in NPM; NPM praat gewoon http met de webinterface.

1. **Hosts → Proxy Hosts → Add Proxy Host**
   - Domain Names: `games.example.org`
   - Scheme: `http`, Forward Hostname/IP: `<server-ip>`, Forward Port: `8090`
   - Zet **Websockets Support** aan (nodig voor het scherm van de bot, `/vnc/websockify`).
   - Block Common Exploits: aan.
2. Tabblad **SSL**: vraag een Let's Encrypt-certificaat aan, zet **Force SSL** en **HTTP/2** aan
   (HSTS mag ook aan).
3. Zet in `.env` `WEBUI_TRUSTED_PROXIES=<ip-van-npm>` (het adres waarmee NPM de server bereikt;
   meerdere met komma's). Alleen van dat adres worden `X-Forwarded-For` en `X-Forwarded-Proto`
   geloofd: zo ziet de webinterface het echte IP (voor de inlogbeveiliging) en weet hij dat de
   verbinding https is. Daarna: `docker compose up -d`.
4. Laat poort 8090 op de server/firewall alleen open voor NPM (bijvoorbeeld `WEBUI_BIND` op het
   LAN-adres zetten, of een firewallregel). Andere poorten publiceert de stack niet.

## Beveiliging

- Accounts staan in `users.json` in het volume `webui_data`, met **argon2**-wachtwoordhashes.
  Rollen: *beheerder* (ziet alle bots en accounts, maakt accounts, reset wachtwoorden) en
  *gebruiker* (alleen de eigen bot). Een beheerder ziet **geen** winkelgegevens van anderen.
- Sessiecookie: ondertekend, `HttpOnly`, `Secure`, `SameSite=Lax`, 12 uur geldig. Een
  wachtwoordreset logt bestaande sessies uit. De sleutel staat in `webui_data/secret.key`
  (of zet `WEBUI_SECRET_KEY`).
- Alle formulieren hebben een CSRF-token; de VNC-websocket controleert de Origin.
- Inlogpogingen worden per IP-adres én per account afgeremd: na 3 fouten groeit de wachttijd
  (tot 30 s per poging), na 10 fouten binnen 15 minuten wordt geweigerd. Dit wordt in het
  geheugen bijgehouden en is na een herstart van de webcontainer weer leeg.
- Wachtwoorden en sleutels van winkels zijn **alleen-schrijven**: de site toont alleen
  "ingesteld"; leeg laten houdt de oude waarde, "wissen" haalt hem weg. `config.env` heeft
  bestandsrechten 600 en wordt atomair geschreven.
- Headers: Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`,
  HSTS achter https.
- De webcontainer draait met een alleen-lezen bestandssysteem, zonder Docker-toegang, met
  alleen de rechten CHOWN/DAC_OVERRIDE/FOWNER (om bestanden in de botvolumes te mogen schrijven).

## Zonder HTTPS (alleen eigen netwerk)

Cookies zijn standaard alleen voor https. Wil je de site zonder proxy op je eigen netwerk
gebruiken via `http://<server-ip>:8090`, zet dan `WEBUI_COOKIE_SECURE=false` in `.env`.
Niet doen als de site via internet bereikbaar is.

## Instellingen in `.env` (webinterface)

| Variabele | Standaard | Betekenis |
|---|---|---|
| `WEBUI_ADMIN_USER` / `WEBUI_ADMIN_PASSWORD` | – | eerste beheerder, alleen zolang er geen accounts zijn |
| `WEBUI_PORT` | `8090` | poort van de webinterface op de server |
| `WEBUI_BIND` | `0.0.0.0` | adres waarop die poort luistert |
| `WEBUI_PUBLIC_URL` | – | publiek https-adres; `bots.sh` zet hiermee de VNC-link in meldingen |
| `WEBUI_TRUSTED_PROXIES` | `127.0.0.1` | IP('s) van de reverse proxy |
| `WEBUI_COOKIE_SECURE` | `true` | `false` alleen voor http op je eigen netwerk |
| `WEBUI_SECRET_KEY` | gegenereerd | sleutel voor sessiecookies (32+ tekens) |
| `WEBUI_TIMEZONE` | `Europe/Amsterdam` | tijdzone voor datums op de site |
| `FGC_WORKERS` | `1` | aantal runs tegelijk over alle bots, of `auto` (zie Takenlijst) |
| `FGC_RUN_TIMEOUT_MINUTES` | `180` | maximale duur van één run; daarna *te lang bezig* en herstart die bot |

`WEBUI_PUBLIC_URL` wordt gelezen als je een bot toevoegt; staat hij er pas later in, voeg de regel
`VNC_URL=https://games.example.org/vnc` toe aan `webui/bots/<naam>.env` en draai `docker compose up -d`.

## Wanneer moet je via het scherm inloggen?

De bot logt zelf in met de gegevens uit Instellingen. Soms lukt dat niet alleen:
de eerste keer bij een winkel, een captcha, een code per e-mail of sms, of een nieuwe
apparaat-bevestiging. De bot wacht dan een paar minuten (*Wachttijd voor handmatig inloggen*) en
stuurt een melding als je die hebt ingesteld. Open dan op het overzicht **Scherm van de bot
openen**, voer het VNC-wachtwoord in (staat op het overzicht) en rond het inloggen af. Daarna
blijft de bot meestal lang ingelogd. Tip: druk eerst op *Nu uitvoeren* en open dan het scherm.

## Bijwerken vanaf de versie met `fgc_runlock`

De eerste versie liet bots op elkaar wachten met een `flock` in het volume `fgc_runlock`. Die is
vervangen door de takenlijst (volume `fgc_queue`). De override moet daarom opnieuw gemaakt worden:

```sh
git pull
./webui/bots.sh generate
docker compose up -d --build --remove-orphans --wait webui
docker compose up -d --build --remove-orphans
docker volume rm <project>_fgc_runlock     # optioneel, wordt niet meer gebruikt
```

De takenlijst (`jobs.db`) wordt vanzelf aangemaakt; er is geen migratie nodig. Bestaande bots houden
hun huidige planning (de oude, gespreide tijden); dat mag zo blijven of je zet ze in Instellingen
gelijk. Wil je meer dan één run tegelijk, zet dan `FGC_WORKERS` in `.env` en draai daarna
`docker compose up -d`.

## Bestaande installatie met één bot

De upstream-service `app` (volume `fgc_data`) wordt met de override uitgeschakeld (profiel
`single`). Wil je de gegevens van die bot (sessies, geclaimde games) meenemen naar bijvoorbeeld
`user1`, kopieer dan na `./webui/bots.sh add user1 --no-up` de inhoud van het oude volume:

```sh
docker run --rm -v <project>_fgc_data:/from -v <project>_fgc_data_user1:/to debian:bookworm-slim cp -a /from/. /to/
docker compose up -d --build
```

Instellingen uit de oude `.env` (EG_EMAIL enz.) moet je daarna in de webinterface invullen;
`.env` wordt niet meer aan de bots doorgegeven.

## Ontwikkelen en testen

```sh
pip install -r requirements.txt -r webui/requirements.txt
python -m pytest tests/test_webui_*.py
FGC_BOTS=user1 FGC_BOTS_DIR=/tmp/bots WEBUI_DATA_DIR=/tmp/webui WEBUI_COOKIE_SECURE=false \
  WEBUI_ADMIN_USER=user1 WEBUI_ADMIN_PASSWORD=testtesttest \
  uvicorn webui.app.main:create_app --factory --port 8090
```

## Licentie

Net als de rest van het project valt de webinterface onder de **AGPL-3.0** (zie `LICENSE`).
Omdat de site via een netwerk gebruikt wordt, moet de broncode beschikbaar zijn voor de
gebruikers; de link staat onderaan elke pagina. Pas die link aan als je een eigen, gewijzigde
versie draait.
