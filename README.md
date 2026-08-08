# free-games-claimer-remaster

<p align="center">
  <img alt="logo-free-games-claimer" src="https://user-images.githubusercontent.com/493741/214588518-a4c89998-127e-4a8c-9b1e-ee4a9d075715.png" />
</p>

> **Not a fork** – a complete ground-up Python remaster inspired by [vogler/free-games-claimer](https://github.com/vogler/free-games-claimer). 
>
> ℹ️ **Are you coming from the original Node.js version?**  
> For a comprehensive, file-by-file breakdown of what changed, dropped features, stealth automation upgrades, and architectural differences, **please read [MODIFICATIONS.md](./MODIFICATIONS.md).**

Automatically claims free games on:

- <img alt="logo steam" src="https://store.steampowered.com/favicon.ico" width="20" align="middle" /> **Steam** – via [SteamDB](https://steamdb.info/upcoming/free/) scraping (only *Free to Keep*, not *Play for Free*)
- <img alt="logo epic-games" src="https://github.com/user-attachments/assets/82e9e9bf-b6ac-4f20-91db-36d2c8429cb6" width="20" align="middle" /> **Epic Games Store** – weekly free games, including the weekly free Android/iOS mobile game (`EG_MOBILE`)
- <img alt="logo fab" src="https://www.google.com/s2/favicons?domain=fab.com&sz=64" width="20" align="middle" /> **Fab** – Epic's asset marketplace
- <img alt="logo prime-gaming" src="https://github.com/user-attachments/assets/7627a108-20c6-4525-a1d8-5d221ee89d6e" width="20" align="middle" /> **Amazon Prime Gaming** – monthly Prime Gaming catalogue + GOG key redemption
- <img alt="logo gog" src="https://github.com/user-attachments/assets/49040b50-ee14-4439-8e3c-e93cafd7c3a5" width="20" align="middle" /> **GOG** – periodic free giveaways
- <img alt="logo ubisoft" src="https://www.ubisoft.com/favicon.ico" width="20" align="middle" /> **Ubisoft** – free game giveaways from [ubisoft.com/games/free](https://www.ubisoft.com/en-us/games/free) (giveaways only, never trials, demos or free weekends)
- <img alt="logo aliexpress" src="https://www.aliexpress.com/favicon.ico" width="20" align="middle" /> **AliExpress** – automated daily check-in that collects coins, using a real-device mobile fingerprint to stay undetected and reading the balance from the coin API

**Gamerpower API** routing to indirect stores (Still under development):
- <img alt="logo fanatical" src="https://www.fanatical.com/favicon.ico" width="20" align="middle" /> **Fanatical** – auto-bypasses cookie banners and hooks Steam accounts to grab weekly PC drops.
- <img alt="logo itchio" src="https://itch.io/favicon.ico" width="20" align="middle" /> **Itch.io** – DRM-free indie giveaways
- <img alt="logo indiegala" src="https://www.indiegala.com/favicon.ico" width="20" align="middle" /> **IndieGala** – free Steam keys & DRM-free games
- <img alt="logo alienware" src="https://www.alienwarearena.com/favicon.ico" width="20" align="middle" /> **Alienware Arena** – (Notify-only) ARP point giveaways

Runs as a Docker container with a built-in scheduler (every 12 hours by default, with optional fixed daily run times). Login via **VNC in browser** or automated credentials.

---

## Quick start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose

> 🟢 **New to Docker on Windows?** Read the step-by-step [**WINDOWS_BEGINNER_GUIDE.md**](./WINDOWS_BEGINNER_GUIDE.md) to set up Docker Desktop, optimize RAM limits, and use Dockhand to deploy this flawlessly.

### 1. Clone and configure

```bash
git clone https://github.com/P-Adamiec/free-games-claimer-remaster.git
cd free-games-claimer-remaster
cp .env.example .env  # or edit the existing .env
```

Edit `.env` with your credentials:

```ini
# Epic Games
EG_EMAIL=your@email.com
EG_PASSWORD=your_password

# Prime Gaming (Amazon)
PG_EMAIL=your@email.com
PG_PASSWORD=your_password

# GOG
GOG_EMAIL=your@email.com
GOG_PASSWORD=your_password

# Steam
STEAM_USERNAME=your_username
STEAM_PASSWORD=your_password

# Ubisoft
UBI_EMAIL=your@email.com
UBI_PASSWORD=your_password

# AliExpress
AE_EMAIL=your@email.com
AE_PASSWORD=your_password

# Notifications
DISCORD_WEBHOOK=https://discord.com/api/webhooks/...

# Run only specific stores (comma-separated)
# Leave commented to run the defaults (everything except gamerpower)
# STORES=steam,prime,gog
```

### 2. Run container

```bash
docker compose up -d
```

> 💡 **Want to test experimental development features?** Add `FGC_TAG=dev` to your `.env` file before running Docker to automatically download our pre-release build!

### 3. Login (first run)

Open **http://localhost:7080** in your browser to access the VNC session.

Each store will wait for you to login manually on the first run if you don't supply credentials. After that, session cookies are natively restored using persistent browser profiles!

### 4. Monitor

To see what the bot is doing in real-time regardless of your current terminal folder, inspect the container directly:
```bash
docker logs -f fgc-remaster
```

---

## Configuration

Options are set via environment variables in `.env`:

| Variable | Default | Description |
|---|---|---|
| `FGC_TAG` | `latest` | Docker image tag to run (set to `dev` to test experimental pre-release builds). |
| `SHOW` | `1` | Show browser window (VNC). |
| `WIDTH` | `1280` | Browser/VNC screen width. |
| `HEIGHT` | `720` | Browser/VNC screen height. |
| `NOVNC_PORT` | `7080` | noVNC web access port. |
| `VNC_IP` | `localhost`| Host for VNC notification links. Alerts include a one-click `http://<VNC_IP>:<NOVNC_PORT>/?autoconnect=true`. |
| `VNC_URL` | | Full public noVNC address for notification links, e.g. `https://fgc.example.tld`. Use it behind a reverse proxy: it keeps your scheme and drops the port, replacing `VNC_IP` and `NOVNC_PORT` in the link (`NOVNC_PORT` still publishes the container port). |
| `VNC_PASSWORD` | | Optional password for VNC access (empty = no password). |
| `SCHEDULER_HOURS`| `12` | Interval in hours between automatic claiming runs. Accepts any positive number (for example: `1`, `12`, `24`, `72`). Set `0` to disable interval runs. |
| `SCHEDULER_TIMEZONE` | `UTC` | IANA timezone used for fixed daily scheduler times. |
| `SCHEDULER_FIXED_TIMES` | | Optional comma-separated daily run times in 24-hour `HH:MM` format (`17:00,21:30`). Set `SCHEDULER_HOURS=0` if you want *only* these fixed daily times without interval runs. |
| `RUN_ON_STARTUP` | `true` | Run once immediately when the container/application starts. |
| `VNC_LOGIN_TIMEOUT`| `180` | Seconds to wait for you to log in via VNC manually. |
| `TIMEOUT` | `60` | Advanced: seconds to wait for a page element before giving up. |
| `EMAIL` | | Default login email used by ALL stores unless a store-specific `*_EMAIL` overrides it. |
| `PASSWORD` | | Default login password used by ALL stores unless a store-specific `*_PASSWORD` overrides it. |
| `EG_EMAIL` | | Epic Games login email. |
| `EG_PASSWORD` | | Epic Games login password. |
| `EG_OTPKEY` | | Epic Games authenticator (TOTP) key, auto-filled. Email/SMS codes are entered manually via VNC. |
| `EG_PARENTALPIN` | | Epic Games Parental Controls PIN. |
| `EG_MOBILE` | `true` | Also claim Epic's weekly free mobile game (claimed on the same store page as the PC games). |
| `EG_MOBILE_PLATFORMS` | `android,ios` | Which mobile versions to claim, Android and iOS are separate items of the same game. |
| `PG_EMAIL` | | Prime Gaming (Amazon) email. |
| `PG_PASSWORD` | | Prime Gaming password. |
| `PG_OTPKEY` | | Prime Gaming 2FA OTP key. |
| `PG_FORCE_CHECK_COLLECTED` | `0` | Force re-check already marked 'claimed' games. |
| `PG_REDEEM` | `0` | Try to redeem keys automatically on external stores. |
| `PG_CLAIMDLC` | `0` | Try claiming DLCs as well (experimental). |
| `GOG_EMAIL` | | GOG login email. |
| `GOG_PASSWORD` | | GOG login password. |
| `GOG_NEWSLETTER` | `0` | Keep newsletter sub after claiming (1 = keep). |
| `GOG_FORCE_REDEEM` | `0` | Force re-redeem old GOG codes from Prime Gaming. |
| `GOG_OTP_ENABLE` | `false`| Use backup codes for GOG 2FA. |
| `GOG_OTP_CODES` | | Comma-separated list of GOG backup codes. |
| `STEAM_USERNAME` | | Steam username. |
| `STEAM_PASSWORD` | | Steam password. |
| `FAB_ACCEPT_EULA` | `true` | Let the bot accept Fab's licence agreement and the EU right-of-withdrawal waiver, both required to claim. Set to `false` to stop before them. Fab reuses `EG_EMAIL` / `EG_PASSWORD` / `EG_OTPKEY`. |
| `UBI_EMAIL` | | Ubisoft Connect login email. |
| `UBI_PASSWORD` | | Ubisoft Connect password. |
| `UBI_OTPKEY` | | Ubisoft authenticator (TOTP) secret, for accounts with two-step verification. |
| `AE_EMAIL` | | AliExpress login email. |
| `AE_PASSWORD` | | AliExpress login password. |
| `AE_MIN_COINS` | `2` | Skip the daily check-in when it offers fewer coins than this (protects against the 1-coin bot-flag state). |
| `AE_FLAG_RETRIES` | `3` | How many times to wait and re-approach the coin page when the offer is capped. |
| `AE_FLAG_WAIT` | `480` | Seconds to wait between retries (kept above AliExpress' ~7-min penalty so one wait clears it). |
| `STORES` | *(see note)* | Comma-separated list of stores to run. Empty runs `steam`, `epic`, `fab`, `prime`, `gog`, `ubisoft`, `aliexpress`; add `gamerpower` here to enable it too. |
| `RESET_DB_GAMES` | `false` | Retroactively erase any database claims recorded within the last 7 days upon execution. Assists in clearing false positives. |
| `FANATICAL_ENABLE`| `false`| Enable Fanatical claiming via GamerPower. |
| `FANATICAL_EMAIL` | | Fanatical account email. |
| `FANATICAL_PASSWORD`| | Fanatical account password. |
| `ALIENWARE_ENABLE`| `false`| Enable Alienware Arena notifications via GamerPower. |
| `ITCHIO_ENABLE`| `false`| Enable Itch.io claiming via GamerPower. |
| `ITCHIO_EMAIL` | | Itch.io account email. |
| `ITCHIO_PASSWORD` | | Itch.io account password. |
| `INDIEGALA_ENABLE`| `false`| Enable IndieGala claiming via GamerPower. |
| `INDIEGALA_EMAIL` | | IndieGala account email. |
| `INDIEGALA_PASSWORD`| | IndieGala account password. |
| `UNKNOWN_STORES_ENABLE`| `false`| Open unsupported external stores for manual claiming via VNC. |
| `BROWSER_DIR` | `data/browser` | Browser profile directory (persists cookies/sessions). |
| `SCREENSHOTS_DIR` | `data/screenshots` | Directory where debug/failure screenshots are saved. |
| `DEBUG` | `true` | Shows verbose actions the bot takes. |
| `DEBUG_LIBS` | `false` | Adds the internals of the libraries the bot uses (every CDP frame sent to Chrome, HTTP handshakes, SQL queries). Only turn this on if a bug report asks for it. |
| `DRYRUN`| `false` | Simulate a run without claiming games. Detects available giveaways and sends a summary report. |
| `DISCORD_WEBHOOK` | | Discord webhook URL for notifications. |
| `NOTIFY` | | Apprise URL(s) for Telegram, Slack, ntfy, etc. Multiple services can be separated by commas. |
| `NOTIFY_TEST` | `false` | Send a test notification on startup to verify your setup works. |
| `NOTIFY_SUMMARY` | `true` | Set to false to disable game claim summaries. (Applies to all services) |
| `NOTIFY_ERRORS` | `true` | Set to false to disable fatal error alerts. (Applies to all services) |
| `NOTIFY_CLAIM_FAILS`| `false` | Set to true to also report games that could not be claimed (e.g. a free DLC without the base game) in alerts and the run summary. (Applies to all services) |
| `NOTIFY_ALREADY_CLAIMED`| `false` | Set to true to also list games you already own and check-ins already collected today. By default the summary shows only what actually changed in that run. |
| `NOTIFY_UPDATES` | `true` | Check GitHub for a newer release (at startup, then at most once a day) and notify you once per version. Set to false to disable the check entirely, no request is made. |
| `NOTIFY_LOGIN_REQUEST`| `true` | Set to false to disable VNC login request pings. (Applies to all services) |
| `NOTIFY_SKIP_STORES` | | Comma-separated store keys whose notifications are silenced (they still run/claim). Accepts aliases (`ae`, `amazon`, `gp`). Example: `aliexpress`. |

### Scheduler

The application supports three scheduling modes: running on a recurring interval (`SCHEDULER_HOURS`), running at specific daily clock times (`SCHEDULER_FIXED_TIMES`), or combining both.

### Scheduling Modes & Interaction

1. **Interval-Only Mode (Default)**: Runs periodically every `n` hours.
   ```ini
   SCHEDULER_HOURS=12
   SCHEDULER_FIXED_TIMES=
   ```
   - `SCHEDULER_HOURS` accepts any positive number (e.g. `1`, `12`, `24`, `48`, `72`).
   - The timer counts exactly `SCHEDULER_HOURS` from when the container/application started.

2. **Fixed Daily Times Mode**: Runs *only* at specific wall-clock times every day (ideal for timing drop windows like 17:00 Epic Games releases). To use only fixed daily times without interval runs, set `SCHEDULER_HOURS=0`.
   ```ini
   SCHEDULER_HOURS=0
   SCHEDULER_TIMEZONE=Europe/Berlin
   SCHEDULER_FIXED_TIMES=17:00,21:30
   ```
   - `SCHEDULER_FIXED_TIMES` accepts comma-separated 24-hour `HH:MM` strings.
   - `SCHEDULER_TIMEZONE` specifies the IANA timezone used for matching these times (`UTC`, `Europe/Berlin`, `America/New_York`), automatically accounting for Daylight Saving Time transitions.

3. **Combined Mode**: Runs *both* every `SCHEDULER_HOURS` **and** at each `SCHEDULER_FIXED_TIMES` independently.
   ```ini
   SCHEDULER_HOURS=24
   SCHEDULER_TIMEZONE=Europe/Berlin
   SCHEDULER_FIXED_TIMES=17:00
   ```

> [!NOTE]
> By default, `RUN_ON_STARTUP=true` is enabled, so the bot always performs **one initial check immediately on startup**, regardless of whether you configure `SCHEDULER_HOURS` or `SCHEDULER_FIXED_TIMES`. Set `RUN_ON_STARTUP=false` if you want it to wait until the first scheduled trigger.

### Selective module execution

Run only specific stores using accepted module aliases (`steam`, `epic`, `prime`, `gog`, `amazon`, `ubisoft`/`ubi`, `fab`, `aliexpress`/`ae`, `gamerpower`/`gp`):

```bash
# Method 1: Via environment variable (recommended)
# Edit .env: STORES=steam,amazon

# Method 2: Temporary execution via Docker Compose
STORES=epic,gog docker compose up -d

# Method 3: One-off immediate run inside Docker (ignores scheduler)
docker compose run --rm app python main.py steam gog --once
```

---

## Architecture

```
free-games-claimer-remaster/
├── main.py                 # Entry point + scheduler + CLI
├── docker-compose.yml      # Container configuration
├── Dockerfile              # Ubuntu + Chrome + TurboVNC + Python
├── MODIFICATIONS.md        # Codebase overhaul technical reference
├── WINDOWS_BEGINNER_GUIDE.md
├── .env                    # Your local configuration (gitignored)
├── .env.example            # Configuration template
└── src/
    ├── version.py          # Version string
    ├── core/               # Shared engine components
    │   ├── claimer.py      # BaseClaimer & CDP stealth patches
    │   ├── config.py       # Typed configuration loader (.env → Python)
    │   ├── database.py     # SQLAlchemy models & SQLite engine
    │   └── notifier.py     # Modular Discord/Apprise webhooks
    └── stores/             # Store-specific claiming modules
        ├── epic.py         # Epic Games Store
        ├── prime.py        # Amazon Prime Gaming
        ├── gog.py          # GOG (+ GOG code redemption from Prime)
        ├── steam.py        # Steam (SteamDB scraping)
        ├── epic_fab.py     # Fab limited-time free assets (shares Epic's session)
        ├── ubisoft.py      # Ubisoft giveaways (ubisoft.com/games/free)
        ├── aliexpress.py   # AliExpress check-in & coin collecting
        ├── epic_mobile.py  # Epic's weekly free Android/iOS game (detection only)
        └── gamerpower.py   # GamerPower API (Fanatical, Itch.io, IndieGala, Alienware)
└── tests/                  # Fast unit tests for pure logic (no browser, no accounts)
```

### How it works

1. **Scheduler** (`main.py`) supports recurring interval timers (`SCHEDULER_HOURS`), fixed daily drop windows (`SCHEDULER_FIXED_TIMES`), combined execution, and initial startup checks (`RUN_ON_STARTUP`).
2. Each store module **starts its own browser** with an isolated profile, securely recalling session cookies (`--restore-last-session`).
3. **Login detection** checks the page DOM (not just cookies/DB).
4. **Stealth profiles** are injected via Chrome DevTools Protocol (`CDP`) `addScriptToEvaluateOnNewDocument` right before document navigation, bypassing typical `page.evaluate` fingerprint detectors.
5. **Game discovery** utilizes specialized scrapers (like reading SteamDB to circumvent typical lists).
6. **Robust Database Storage** verifies historical success in `fgc.db` (SQLite) to block aggressive overlapping.
7. **Clean Notifications** dispatch to you dynamically based on the toggles configured in the `.env` settings.

---

## Notifications

Both Discord and Apprise can be configured simultaneously, notifications are sent to ALL configured services in parallel via async dispatch.

- **Discord**: Set `DISCORD_WEBHOOK` in `.env`.
- **Apprise (Telegram, Slack, Email, ntfy, etc.)**: Set `NOTIFY` in `.env`. You can provide multiple URLs separated by commas (e.g. `NOTIFY=ntfy://topic, tgram://token/id`).
- **Fine-Tune Filtering**: Use `NOTIFY_SUMMARY=false`, `NOTIFY_ERRORS=false`, etc., to silence specific notification subsets across all services globally.
- **Testing**: Set `NOTIFY_TEST=true` to receive a test notification whenever the container starts.

---

## Troubleshooting

| Issue | Solution |
|---|---|
| Store not logging in | Open VNC (`http://localhost:7080`) and login manually. Your credentials or session logic persist beautifully after first login. |
| Steam game not detected | Check that the game is listed on [SteamDB Free](https://steamdb.info/upcoming/free/). |
| GamerPower missing games | Certain platforms (Itch.io, IndieGala, Alienware, Fanatical) require explicit `{STORE}_ENABLE=true` toggles in configuration to activate their respective handlers. |
| Epic captcha | The stealth patches prevent 99% of captchas. EU 'Right of withdrawal' overlays are automatically accepted. If a rigorous manual prompt arrives, solve it once via VNC. |
| False positive claims | Set `RESET_DB_GAMES=true` in your `.env`, reboot the container, and the bot will forget the last 7 days of claims, allowing the logic to try claiming them again. |
| Container crashes on start | Check logs: `docker compose logs app --tail=50`. A clean restart purges `.X1-lock` bugs. |

### Something is not working, what to send us

The normal log shows only what you act on: which store is running, who is signed in, what was found and
what was claimed, plus every warning and error. All the diagnostic detail is still there, one switch away:

1. Set `DEBUG=true` in `.env` and restart (`docker compose up -d`), then reproduce the problem.
2. Collect the log: `docker logs fgc-remaster --tail 500 > fgc.log` (remove your e-mail address if it appears).
3. Look in the `data/` folder for what the bot saw:
   - `data/screenshots/<store>/`, screenshots taken at every failure,
   - `data/ae_coin_api.json`, raw AliExpress check-in responses (streak, coins),
   - `data/steamdb_dump.html`, the SteamDB page as the bot parsed it (written only with `DEBUG=true`),
   - `data/*_fail.html`, page snapshots from failed logins/check-ins.
4. Watch it live if it is still running: open `http://localhost:7080` (noVNC) and take over the browser.

A good bug report is: what you expected, what happened, the `DEBUG=true` log around the failure, and the
matching screenshot. `DEBUG=true` covers what the bot itself did; only add `DEBUG_LIBS=true` if you are
asked for the raw network or browser traffic, because that turns one run into tens of thousands of lines.
Please switch both off again once the problem is solved.

---

## Credits

Inspired by [vogler/free-games-claimer](https://github.com/vogler/free-games-claimer) – the original Node.js project.
This remaster is a **completely independent rewrite** in Python, not a fork.

---

## License

[AGPL-3.0](./LICENSE)

---

## Analytics

[![Star History Chart](https://api.star-history.com/svg?repos=P-Adamiec/Free-Games-Claimer-Remaster&type=Date)](https://www.star-history.com/?repos=P-Adamiec%2FFree-Games-Claimer-Remaster&type=date&legend=bottom-right)

![Alt](https://repobeats.axiom.co/api/embed/5c6416eef2d3371808c7d1d50418546103b351f4.svg "Repobeats analytics image")

---

<p align="center">
<img alt="logo-fgc-remaster" src="logo.png" width="256" />
</p>
