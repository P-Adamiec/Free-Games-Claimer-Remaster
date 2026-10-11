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

- **Bereikbaarheid voor Bjorn** — nu alleen op het thuisnetwerk. Of het via de reverse proxy en een
  hostnaam moet, met HTTPS, is nog niet besloten.
- **Eerste logins** — een aantal winkels vraagt de eerste keer een handmatige login of 2FA via noVNC (poort 7080).
  Dat moet Bjorn zelf doen, of Sjoerd samen met Bjorn.

## 2026-10-11

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
