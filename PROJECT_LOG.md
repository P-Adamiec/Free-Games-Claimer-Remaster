# Projectlog

Nieuwste bovenaan. Per regel: wat er veranderd is en waarom; details staan in de commits, README's en issues.
Dit bestand hoort bij de fork `sjkanon/Free-Games-Claimer-Remaster`; upstream is
[P-Adamiec/Free-Games-Claimer-Remaster](https://github.com/P-Adamiec/Free-Games-Claimer-Remaster) (AGPL-3.0).

## Doel

De claimer op de eigen Proxmox-server laten draaien voor Bjorn, met een eigen website waarop Bjorn zonder Docker of
`.env` de eigen accounts invult en ziet wat er geclaimd is. Wijzigingen aan upstream-bestanden klein houden, zodat
`git merge upstream/main` makkelijk blijft.

## Open punten

Zie de [issues](https://github.com/sjkanon/Free-Games-Claimer-Remaster/issues) voor de takenlijst.

- **Eerste logins** — een aantal winkels vraagt de eerste keer een handmatige login of 2FA via het scherm van de bot (noVNC, op de website onder 'Scherm').
  Dat moet Bjorn zelf doen, of Sjoerd samen met Bjorn.

## 2026-10-12

- DLC waarvan je het basisspel niet hebt, telt niet meer als mislukt: op de website staat het als "Geen basisspel"
  (grijs), het telt niet mee in "mislukt" in de takenlijst en de waarschuwingen ervan staan niet bij de problemen van
  de run. Een melding erover staat standaard uit (`NOTIFY_MISSING_BASE`, aan te zetten onder de geavanceerde
  meldingen). Alles in `webui/`; de bot zelf slaat zulke DLC al over.
- Backup van de server geregeld en restore getest (#1). Issues #1–#6, #9 en #10 gesloten; open blijft #8 (upstream).

## 2026-10-11

- Twee upstream-fouten opgelost (gemarkeerd `[fork:fix]`, tests in `tests/test_fork_fixes.py`): Prime claimde
  door na een mislukte login (ook bij opnieuw inloggen midden in een run); GOG opende met `GOG_NEWSLETTER=false`
  alleen de abonnementenpagina zonder iets uit te vinken. Het uitvinken is nog niet live getest (geen GOG-claim
  gehad); de bot waarschuwt in het log als de opties niet gevonden worden.

- Inloghulp in de webui (`webui/app/help.py`): per winkel een uitklapblok "Hoe inloggen werkt" bij Instellingen
  en een nieuwe pagina 'Uitleg' (overzichtstabel, scherm van de bot, meldingen, sessies, authenticator-sleutel
  met stappen per winkel). Gebaseerd op de code van de bot en de helppagina's van de winkels zelf; waar die
  verschillen staat het erbij (Steam Guard zonder sleutel, Epic Authenticator, Fanatical- en Unity-codes).
- Takenlijst met workers in plaats van de `flock` en de gespreide tijden (#10). Elke run is een taak in een
  gedeelde SQLite-takenlijst (`webui/jobqueue.py`, volume `fgc_queue`, WAL, claimen met `BEGIN IMMEDIATE`).
  - De planning van een bot en 'Nu uitvoeren' zetten alleen een taak in de rij (hooguit één wachtende per bot);
    een bot runt pas na het claimen van een plek. Hooguit `FGC_WORKERS` runs tegelijk (standaard 1, `auto` =
    (geheugen − 1 GB) / 1,5 GB, max 4), FIFO over alle bots.
  - Heartbeat per lopende taak: een stille bot (2 min) geeft *mislukt*, langer dan `FGC_RUN_TIMEOUT_MINUTES`
    geeft *te lang bezig* plus herstart van de bot. Wachtende taken vervallen na 24 uur, geschiedenis 30 dagen.
  - Nieuwe pagina 'Takenlijst' (eigen taken; beheerder ziet alles, aantal workers en kan annuleren); de
    statusbadge komt uit de takenlijst. Nieuwe bots krijgen allemaal 06:00 en 18:00; de waarschuwingen over
    gelijke tijden in Beheer zijn weg.
  - Fix `bots.sh add`: stopte zonder melding als `WEBUI_PUBLIC_URL` niet in `.env` stond (`set -e`).
  - Uitrol: `./webui/bots.sh generate`, daarna `docker compose up -d --build` (webui eerst); volume
    `fgc_runlock` mag weg. 1506 tests groen.
- Uitgerold op de container: webui + bots `sjoerd` en `bjorn`; accounts `sjoerd` (admin) en `bjorn` aangemaakt.
  Login, 403 op Beheer voor een gewone gebruiker en de noVNC-pagina getest op het thuisnetwerk.
- Fix `bots.sh`: webui eerst starten. Een nieuwe bot las zijn config voordat de webui de gespreide planning had
  geschreven en draaide dan elke 12 uur plus direct bij het opstarten.
- Webinterface gebouwd (`webui/`, FastAPI): gebruikersaccounts (argon2, admin/gebruiker), instellingen per winkel,
  dashboard met geclaimde games (uit `fgc.db`), log, status en 'Nu uitvoeren', noVNC via de webui achter de login.
  - Instellingen gaan via `data/config.env` in het volume van de bot (leest upstream al); wachtwoorden alleen
    schrijven, bestand 600. Geen Docker-socket in de webcontainer: een kleine bridge in de bot (`webui/bridge.py`,
    één gemarkeerd blok in `main.py`) schrijft status/log en pakt run- en herstartverzoeken op via bestanden.
  - Eén bot-container per gebruiker via `webui/bots.sh add <naam>` (genereert een gitignored
    `docker-compose.override.yml`); alleen de webui-poort wordt gepubliceerd.
  - Wachtrij (#10): elke run pakt een gedeelde `flock`; een gecrashte bot laat het slot vanzelf los, een run
    langer dan `FGC_RUN_TIMEOUT_MINUTES` (180) herstart de bot. Standaardtijden per bot verschillen ook.
  - Achter Nginx Proxy Manager: trusted proxies, Secure-cookies, CSRF, CSP, login-throttling per IP en account.
  - Alle upstream-wijzigingen gemarkeerd met `[fork:webui]`; `docker-compose.yml` ongewijzigd. 1461 tests groen.

- Fork gemaakt van upstream v1.11 (`89f510e`), clone in `~/git/forks/Free-Games-Claimer-Remaster` met remote `upstream`.
- Proxmox: LXC-container aangemaakt (Debian 13, 2 cores, 3 GB RAM, 20 GB, unprivileged met nesting + keyctl
  voor Docker, start mee met de host). Docker 29.9 + Compose v5.6.
- Webinterface (`webui/`) in aanbouw: login met wachtwoord, instellingen per winkel, overzicht van geclaimde games
  en logs, knop naar noVNC.
- Meerdere gebruikers (#9): Bjorn en Sjoerd claimen elk op de eigen accounts. De bot kan één set accounts per
  container aan, dus één bot-container per gebruiker. De website krijgt gebruikersaccounts met rollen; de
  webcontainer mag geen containers aanmaken, een nieuwe gebruiker gaat via een script.
- Runs na elkaar (#10): elke bot draait een eigen Chrome (~1–1,5 GB tijdens een run). Eerst vaste tijden per
  gebruiker overwogen, maar een uitlopende run overlapt dan alsnog; daarom een wachtrij met één run tegelijk,
  ook voor 'Nu uitvoeren'.
