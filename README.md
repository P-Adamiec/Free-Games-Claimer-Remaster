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
- <img alt="logo unity" src="https://www.google.com/s2/favicons?domain=unity.com&sz=64" width="20" align="middle" /> **Unity Asset Store** – the weekly free [Publisher of the Week](https://assetstore.unity.com/publisher-sale) asset, coupon and all (opt-in, add `unity` to `STORES`)
- <img alt="logo prime-gaming" src="https://github.com/user-attachments/assets/7627a108-20c6-4525-a1d8-5d221ee89d6e" width="20" align="middle" /> **Amazon Prime Gaming** – monthly Prime Gaming catalogue + GOG key redemption
- <img alt="logo gog" src="https://github.com/user-attachments/assets/49040b50-ee14-4439-8e3c-e93cafd7c3a5" width="20" align="middle" /> **GOG** – periodic free giveaways
- <img alt="logo ubisoft" src="https://www.ubisoft.com/favicon.ico" width="20" align="middle" /> **Ubisoft** – free game giveaways from [ubisoft.com/games/free](https://www.ubisoft.com/en-us/games/free) (giveaways only, never trials, demos or free weekends)
- <img alt="logo aliexpress" src="https://www.aliexpress.com/favicon.ico" width="20" align="middle" /> **AliExpress** – automated daily check-in that collects coins, using a real-device mobile fingerprint to stay undetected and reading the balance from the coin API. The coin page sometimes arrives empty; the bot gives it one more approach, then reports it and moves on instead of retrying for half an hour (see [Troubleshooting](#troubleshooting))

**GamerPower API**, asked once at the start of every run, finds giveaways the stores themselves do not advertise, then hands each one to the matching store above. It also reaches these sites, which have no store module of their own and are chosen in `STORES` like any other store (still under development):
- <img alt="logo fanatical" src="https://www.fanatical.com/favicon.ico" width="20" align="middle" /> **Fanatical** – auto-bypasses cookie banners and hooks Steam accounts to grab weekly PC drops. (almost ready)
- <img alt="logo itchio" src="https://itch.io/favicon.ico" width="20" align="middle" /> **Itch.io** – DRM-free indie giveaways, claimed to your library and verified there (add `itchio` to `STORES`)
- <img alt="logo indiegala" src="https://www.indiegala.com/favicon.ico" width="20" align="middle" /> **IndieGala** – free Steam keys & DRM-free games (not ready yet)
- <img alt="logo alienware" src="https://www.alienwarearena.com/favicon.ico" width="20" align="middle" /> **Alienware Arena** – (Notify-only) ARP point giveaways (not ready yet)

> [!TIP]
> **There is more free stuff out there than the storefronts show you.** Epic advertises two games a week
> on its front page, but GamerPower regularly lists half a dozen more that are free right now on the very
> same account, and other stores are no different. You get those for free: `STORES=steam` claims the Steam
> giveaways GamerPower lists, in the same browser session, and leaves the Epic ones alone. The extra sites
> (Fanatical, Itch.io, IndieGala, Alienware Arena) each need an account, so they run only when you name
> them: `STORES=steam,itchio`.

Runs as a Docker container with a built-in scheduler (every 12 hours by default, with optional fixed
daily run times). Login via **VNC in browser** or automated credentials.

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
# Leave commented to run the defaults (everything except unity and the sites
# that need an account of their own: itchio, fanatical, indiegala, alienware)
# STORES=steam,prime,gog
```

### 2. Run container

```bash
docker compose up -d
```

> 💡 **Want to test experimental development features?** Add `FGC_TAG=dev` to your `.env` file before running Docker to automatically download our pre-release build!

### 3. Login (first run)

Open **http://localhost:7080** in your browser to access the VNC session.

Each store will wait for you to login manually on the first run if you don't supply credentials.
After that, session cookies are natively restored using persistent browser profiles!

It waits `VNC_LOGIN_TIMEOUT` seconds for you, three minutes by default. If nobody answers, that one
store is left alone for the rest of the run and the summary says so, so a bot running while you sleep
does not sit on a login screen for hours. The next run tries again.

### 4. Monitor

To see what the bot is doing in real-time regardless of your current terminal folder, inspect the
container directly:
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
| `VNC_LOGIN_TIMEOUT`| `180` | Seconds the bot waits for **you** at any manual step: signing in, a code from e-mail or SMS, approving 2FA, a captcha, or Unity's checkout form. A window nobody answers ends that one store's manual steps for the rest of the run, and the summary says what it skipped. Raise it if you are not usually sitting at the computer. |
| `TIMEOUT` | `60` | Advanced: seconds to wait for a page element before giving up. |
| `EMAIL` | | Default login email used by ALL stores unless a store-specific `*_EMAIL` overrides it. |
| `PASSWORD` | | Default login password used by ALL stores unless a store-specific `*_PASSWORD` overrides it. |
| `EG_EMAIL` | | Epic Games login email. |
| `EG_PASSWORD` | | Epic Games login password. |
| `EG_OTP_KEY` | | Epic Games authenticator (TOTP) key, auto-filled. Email/SMS codes are entered manually via VNC. |
| `EG_OTP_CODES` | | Comma-separated Epic recovery codes from the authenticator setup. Spent one at a time, used ones are written to `data/used_epic_codes.txt`. |
| `EG_PARENTALPIN` | | Epic Games Parental Controls PIN. |
| `EG_MOBILE` | `true` | Also claim Epic's weekly free mobile game (claimed on the same store page as the PC games). |
| `EG_MOBILE_PLATFORMS` | `android,ios` | Which mobile versions to claim, Android and iOS are separate items of the same game. |
| `PG_EMAIL` | | Prime Gaming (Amazon) email. |
| `PG_PASSWORD` | | Prime Gaming password. |
| `PG_OTP_KEY` | | Prime Gaming authenticator (TOTP) key. |
| `PG_FORCE_CHECK_COLLECTED` | `0` | Force re-check already marked 'claimed' games. |
| `PG_REDEEM` | `0` | Try to redeem keys automatically on external stores. |
| `GOG_EMAIL` | | GOG login email. |
| `GOG_PASSWORD` | | GOG login password. |
| `GOG_NEWSLETTER` | `0` | Keep newsletter sub after claiming (1 = keep). |
| `GOG_FORCE_REDEEM` | `0` | Force re-redeem old GOG codes from Prime Gaming. |
| `GOG_OTP_KEY` | | GOG authenticator (TOTP) secret, auto-filled. Codes sent by e-mail are entered manually via VNC. |
| `GOG_OTP_CODES` | | Comma-separated GOG recovery codes, tried after the secret. Used ones are written to `data/used_gog_codes.txt`. |
| `STEAM_USERNAME` | | Steam username. |
| `STEAM_PASSWORD` | | Steam password. |
| `FAB_ACCEPT_EULA` | `true` | Let the bot accept Fab's licence agreement and the EU right-of-withdrawal waiver, both required to claim. Set to `false` to stop before them. Fab reuses `EG_EMAIL` / `EG_PASSWORD` / `EG_OTP_KEY`. |
| `UNITY_EMAIL` | | Unity Asset Store login email. |
| `UNITY_PASSWORD` | | Unity Asset Store password. |
| `UNITY_ACCEPT_TOS` | `true` | Accept the Asset Store EULA and the 14-day withdrawal waiver at Unity's checkout. Set to `false` to stop before them and be notified instead. |
| `UBI_EMAIL` | | Ubisoft Connect login email. |
| `UBI_PASSWORD` | | Ubisoft Connect password. |
| `UBI_OTP_KEY` | | Ubisoft authenticator (TOTP) secret, for accounts with two-step verification. |
| `AE_EMAIL` | | AliExpress login email. |
| `AE_PASSWORD` | | AliExpress login password. |
| `AE_MIN_COINS` | `2` | Skip the daily check-in when it offers fewer coins than this (protects against the 1-coin bot-flag state). |
| `AE_FLAG_RETRIES` | `3` | How many times to wait and re-approach the coin page when the offer is capped. |
| `AE_FLAG_WAIT` | `480` | Seconds to wait between retries (kept above AliExpress' ~7-min penalty so one wait clears it). |
| `AE_PAGE_RETRIES` | `4` | How many extra approaches to make when the coin page loads but renders nothing. AliExpress serves it empty most of the time (measured: one usable page in eight looks), so each retry is a real chance at the daily check-in. `0` gives up on the first look. |
| `STORES` | *(see note)* | Comma-separated list of stores to run. Empty runs `steam`, `epic`, `fab`, `prime`, `gog`, `ubisoft`, `aliexpress`. Add `unity`, `itchio`, `fanatical`, `indiegala` or `alienware` to switch one on. GamerPower is asked once per run and its finds go to the store they belong to, so `steam` also claims the Steam giveaways it lists. |
| `RESET_DB_GAMES` | `false` | Retroactively erase any database claims recorded within the last 7 days upon execution. Assists in clearing false positives. |
| `GP_CLAIM_DLC` | `false` | Also process GamerPower's in-game DLC giveaways. Off by default: most need an account in that specific game, and they are the bulk of the feed. |
| `FANATICAL_ENABLE`| `false`| Replaced by `STORES=...,fanatical`. Still honoured for now. |
| `FANATICAL_EMAIL` | | Fanatical account email. |
| `FANATICAL_PASSWORD`| | Fanatical account password. |
| `ALIENWARE_ENABLE`| `false`| Replaced by `STORES=...,alienware`. Still honoured for now. |
| `ITCHIO_ENABLE`| `false`| Replaced by `STORES=...,itchio`. Still honoured for now. |
| `ITCHIO_EMAIL` | | Itch.io account email. |
| `ITCHIO_PASSWORD` | | Itch.io account password. |
| `ITCHIO_OTP_KEY` | | Itch.io authenticator (TOTP) secret, auto-filled. |
| `ITCHIO_OTP_CODES` | | Comma-separated Itch.io recovery codes, tried after the secret. Used ones are recorded in `data/used_itchio_codes.txt` so none is sent twice. |
| `INDIEGALA_ENABLE`| `false`| Replaced by `STORES=...,indiegala`. Still honoured for now. |
| `INDIEGALA_EMAIL` | | IndieGala account email. |
| `INDIEGALA_PASSWORD`| | IndieGala account password. |
| `GP_UNKNOWN_STORES`| `false`| Reserved. Opening a site nobody mapped is not built yet, so this stays off whatever you set. |
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
| `NOTIFY_MISSING_BASE`| `true` | Set to false to drop DLC entries that failed because you do not own the base game. They repeat every run and you can only fix them by buying the base game. |
| `NOTIFY_DOWNLOAD_ONLY`| `true` | Set to false to drop Itch.io giveaways that are only a download, with nothing to claim onto your account. Reported once by default, then silent. |
| `NOTIFY_UPDATES` | `true` | Check GitHub for a newer release (at startup, then at most once a day) and notify you once per version. Set to false to disable the check entirely, no request is made. |
| `NOTIFY_LOGIN_REQUEST`| `true` | Set to false to disable VNC login request pings. (Applies to all services) |
| `NOTIFY_SKIP_STORES` | | Comma-separated store keys whose notifications are silenced (they still run/claim). Accepts aliases (`ae`, `amazon`, `gp`). Example: `aliexpress`. |

### Two-factor sign-in

Nothing here is required. By default the bot stops at a code screen and asks you to finish it in the
browser over VNC, waiting `VNC_LOGIN_TIMEOUT` seconds for you.

If you want it to get through 2FA on its own there are two ways, and they are not equivalent:

- **An authenticator secret** (`*_OTP_KEY`) is the key behind the QR code you scanned into your phone,
  offered as "manual entry key" during setup. The bot becomes another authenticator app and never runs
  out of codes. What it costs: your password and your second factor then sit in the same `.env` on the
  same machine, so whoever reads that file has both.
- **Recovery codes** (`*_OTP_CODES`) are one-shot. The bot spends one per sign-in and writes it to
  `data/used_*_codes.txt`, so a stolen file costs a limited number of logins instead of permanent
  access, and you can see which ones were used. They do run out, and then it is back to VNC.

Set either, both, or neither. Filling one in is all it takes to switch it on. Every store follows the
same order: **two codes from the secret, then one recovery code, then you over VNC.** The second code is
never the same digits as the first, because a code refused inside its 30-second window stays refused.

| Store | Authenticator secret | Recovery codes | Spent codes |
|---|---|---|---|
| Epic (and Fab) | `EG_OTP_KEY` | `EG_OTP_CODES` | `data/used_epic_codes.txt` |
| GOG | `GOG_OTP_KEY` | `GOG_OTP_CODES` | `data/used_gog_codes.txt` |
| Itch.io | `ITCHIO_OTP_KEY` | `ITCHIO_OTP_CODES` | `data/used_itchio_codes.txt` |
| Prime Gaming | `PG_OTP_KEY` | Amazon does not issue any | |
| Ubisoft | `UBI_OTP_KEY` | not supported yet | |
| Steam, AliExpress, everything else | | | codes are typed by you over VNC |

Either way this is usually a one-time event per store: on the code screen the bot ticks the store's
"remember this browser" box, so it stops being asked on that profile until the profile is deleted.

## Unity, one-time setup

Unity is opt-in (`STORES=...,unity`) and its checkout refuses the free coupon until your Unity account
carries a complete billing address. That is the one thing the bot cannot invent for you, so on the first
claim it opens the checkout, pings you the same way it does for a login, and waits as long as
`VNC_LOGIN_TIMEOUT` says, three minutes by default:

1. Open the VNC session and fill in what it names as missing: first name, last name, address, postal
code and city.
2. Answer **"Are you exempt from paying consumption tax?"** with **No**, unless you genuinely have a
tax number. With one on the account, the bot leaves that section alone.
3. Leave the page as it is, the bot applies the coupon and finishes the claim in the same session.

Unity keeps this on your account, so every later week runs unattended. Missing the window is not a
failure: the asset is reported as `skipped:setup`, the rest of the run carries on, and the next run tries
again (each giveaway lasts a week).

> [!NOTE]
> **The claim is written for Unity's checkout in English**, which is what the bot's browser profile asks
> for. It finds the coupon box, the total and the confirm button by their English labels. If your account
> forces another language onto the checkout, the bot cannot read the amount, so it refuses to confirm the
> order and reports the asset as not claimed. It never pays in that state: the only thing it ever confirms
> is a total of exactly zero. Set the Asset Store language back to English and the claim works again.

---

### Scheduler

The application supports three scheduling modes: running on a recurring interval
(`SCHEDULER_HOURS`), running at specific daily clock times (`SCHEDULER_FIXED_TIMES`), or combining
both.

### Scheduling Modes & Interaction

1. **Interval-Only Mode (Default)**: Runs periodically every `n` hours.
   ```ini
   SCHEDULER_HOURS=12
   SCHEDULER_FIXED_TIMES=
   ```
   - `SCHEDULER_HOURS` accepts any positive number (e.g. `1`, `12`, `24`, `48`, `72`).
   - The timer counts exactly `SCHEDULER_HOURS` from when the container/application started.

2. **Fixed Daily Times Mode**: Runs *only* at specific wall-clock times every day (ideal for timing
drop windows like 17:00 Epic Games releases). To use only fixed daily times without interval runs,
set `SCHEDULER_HOURS=0`.
   ```ini
   SCHEDULER_HOURS=0
   SCHEDULER_TIMEZONE=Europe/Berlin
   SCHEDULER_FIXED_TIMES=17:00,21:30
   ```
   - `SCHEDULER_FIXED_TIMES` accepts comma-separated 24-hour `HH:MM` strings.
   - `SCHEDULER_TIMEZONE` specifies the IANA timezone used for matching these times (`UTC`, `Europe/Berlin`, `America/New_York`), automatically accounting for Daylight Saving Time transitions.

3. **Combined Mode**: Runs *both* every `SCHEDULER_HOURS` **and** at each `SCHEDULER_FIXED_TIMES`
independently.
   ```ini
   SCHEDULER_HOURS=24
   SCHEDULER_TIMEZONE=Europe/Berlin
   SCHEDULER_FIXED_TIMES=17:00
   ```

> [!NOTE]
> By default, `RUN_ON_STARTUP=true` is enabled, so the bot always performs **one initial check immediately on startup**, regardless of whether you configure `SCHEDULER_HOURS` or `SCHEDULER_FIXED_TIMES`. Set `RUN_ON_STARTUP=false` if you want it to wait until the first scheduled trigger.

### Selective module execution

Run only specific stores using accepted module aliases (`steam`, `epic`, `prime`/`amazon`, `gog`,
`ubisoft`/`ubi`, `fab`, `unity`, `aliexpress`/`ae`, `itchio`/`itch`, `fanatical`, `indiegala`,
`alienware`):

```bash
# Method 1: Via environment variable (recommended)
# Edit .env: STORES=steam,amazon

# Method 2: Temporary execution via Docker Compose
STORES=epic,gog docker compose up -d

# Method 3: One-off immediate run inside Docker (ignores scheduler)
docker compose run --rm app python main.py steam gog --once
```

To skip a store, simply exclude it from the `STORES` list. Any unrecognized settings are now
reported at startup rather than being ignored silently.

> [!NOTE]
> **What happens in one run, in order.** GamerPower is asked first, once, and only when this run has a
> store that can use the answer. Then each big store runs: it claims what it finds itself and, at the end
> of the same browser session, the giveaways GamerPower found for it. Then any GOG keys waiting from Prime
> Gaming are redeemed. Last come the sites with no module of their own (Itch.io, Fanatical, IndieGala,
> Alienware Arena), all in one browser window.
>
> So `STORES=steam` also claims the Steam giveaways GamerPower lists, and `STORES=prime` sends GamerPower
> no request at all, because nothing in that run could use it.
>
> Giveaways that hand out a Steam key (like Fanatical or IndieGala) are handled differently: the key is
> saved to your database and sent via notifications, but the bot will not sign in to Steam to redeem it.


---

## Architecture

```
free-games-claimer-remaster/
├── main.py                 # Entry point + scheduler + CLI + run summary
├── docker-compose.yml      # Container configuration
├── Dockerfile              # Debian bookworm-slim + Chrome/Chromium + TurboVNC + noVNC
├── docker-entrypoint.sh    # Starts the virtual display, VNC and the bot
├── requirements.txt        # Python dependencies
├── CHANGELOG.md            # What changed in every release
├── MODIFICATIONS.md        # Codebase overhaul technical reference
├── WINDOWS_BEGINNER_GUIDE.md
├── .env                    # Your local configuration (gitignored)
├── .env.example            # Configuration template
├── data/                   # Everything the bot keeps (Docker volume)
│   ├── fgc.db              # SQLite database of what was already claimed
│   ├── browser/<store>/    # One persistent Chrome profile per store
│   └── screenshots/<store>/# Screenshots taken on failures
├── src/
│   ├── version.py          # Version string
│   ├── core/               # Shared engine components
│   │   ├── claimer.py      # BaseClaimer: browser launch, login waits, notifications
│   │   ├── config.py       # Typed configuration loader (.env → Python)
│   │   ├── database.py     # SQLAlchemy models & SQLite engine
│   │   ├── notifier.py     # Modular Discord/Apprise webhooks
│   │   ├── selection.py    # Which stores this run covers (GamerPower reads it)
│   │   ├── run_state.py    # What this run learned: which store is waiting for you
│   │   ├── updates.py      # Tells you when a newer release is published
│   │   └── url_security.py # Hostname checks for redirects (never substring matching)
│   └── stores/             # Store-specific claiming modules
│       ├── epic.py         # Epic Games Store
│       ├── prime.py        # Amazon Prime Gaming
│       ├── gog.py          # GOG (+ GOG code redemption from Prime)
│       ├── steam.py        # Steam (SteamDB scraping)
│       ├── epic_fab.py     # Fab limited-time free assets (shares Epic's session)
│       ├── unity.py        # Unity Asset Store weekly free asset
│       ├── ubisoft.py      # Ubisoft giveaways (ubisoft.com/games/free)
│       ├── aliexpress.py   # AliExpress check-in & coin collecting
│       ├── epic_mobile.py  # Epic's weekly free Android/iOS game (detection only)
│       └── gamerpower.py   # GamerPower API (Fanatical, Itch.io, IndieGala, Alienware)
└── tests/                  # Fast unit tests for pure logic (no browser, no accounts)
```

### How it works

1. **Scheduler** (`main.py`) supports recurring interval timers (`SCHEDULER_HOURS`), fixed daily
drop windows (`SCHEDULER_FIXED_TIMES`), combined execution, and initial startup checks
(`RUN_ON_STARTUP`).
2. Each store module **starts its own browser** with an isolated profile, securely recalling session
cookies (`--restore-last-session`). A first tab that arrives late no longer takes the store down,
and each profile is marked as cleanly closed before every start. Two exceptions save a login: Fab
rides Epic's profile, and a GamerPower find is claimed inside the session the store already opened,
rather than in a second browser of its own.
3. **Login detection** checks the page DOM (not just cookies/DB).
4. **Fingerprint** is the one nodriver's patched Chrome produces by itself, because a hand-written
desktop spoof did not match the real container and started summoning captchas (see CHANGELOG 1.4).
Only AliExpress overrides it, injecting one coherent real-device Android fingerprint
(`browserforge`) over the Chrome DevTools Protocol.
5. **Game discovery** prefers each store's own data over scraping the page: Epic's promotions API,
Ubisoft's embedded news feed, Fab's free-content blade, the GamerPower API, and SteamDB for Steam.
6. **Store selection** (`STORES`) is published to the run, so a GamerPower find is only claimed when
this run includes the store it belongs to.
7. **A claim counts only when the store agrees.** After the checkout the bot reads the product page
or the account's own list back (Epic, Fab, Unity, Itch.io) and reports `claimed` only then,
otherwise it says so instead of guessing. `fgc.db` (SQLite) remembers the outcome so runs do not
trip over each other.
8. **Clean Notifications** dispatch to you dynamically based on the toggles configured in the `.env`
settings, and a daily update check tells you when a newer release is out (`NOTIFY_UPDATES`).
9. **Two-factor sign-in follows one rule everywhere.** With an authenticator secret set
(`*_OTP_KEY`) the bot sends two codes, the second one different from the first because a refused
code stays refused inside its 30-second window. Then a recovery code from `*_OTP_CODES` if you gave
it any, and only then the screen is left to you over VNC.
10. **Your settings are read back to you at startup.** Anything in `.env` the bot does not
understand is named in the log instead of being ignored, and account names are masked
(`p***@gmail.com`) so a log is safe to share.

---

## Notifications

Both Discord and Apprise can be configured simultaneously, notifications are sent to ALL configured
services in parallel via async dispatch.

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
| GamerPower missing games | Itch.io, IndieGala, Alienware Arena and Fanatical each need an account there, so they run only when you name them: `STORES=...,itchio,indiegala`. Their old `{STORE}_ENABLE=true` switches still work for one more release. Giveaways for Steam, Epic or GOG are claimed by those stores themselves, so they are skipped when the store is not in your `STORES` list. |
| Unity coupon not applied | Unity blocks the coupon while its checkout form is incomplete, see [5. Unity, first run only](#unity-one-time-setup). Two other reasons it stops on purpose: a checkout rendered in a language other than English, and `UNITY_ACCEPT_TOS=false`, which halts right before the EULA. |
| AliExpress coins not collected | The coin page sometimes loads as an empty shell. The bot tries once more (`AE_PAGE_RETRIES`), then reports it and moves on rather than retrying for half an hour. It has been seen working again on a later run; collect in the mobile app if it persists. |
| Epic captcha | The stealth patches prevent 99% of captchas. EU 'Right of withdrawal' overlays are automatically accepted. If a rigorous manual prompt arrives, solve it once via VNC. |
| False positive claims | Set `RESET_DB_GAMES=true` in your `.env`, reboot the container, and the bot will forget the last 7 days of claims, allowing the logic to try claiming them again. |
| Setting seems to be ignored | The bot names every setting it does not read at startup, for example an invented `STEAM_ENABLE`, and every value that cannot mean what it says, for example `DRYRUN=maybe`, which counts as false. Passwords, e-mail addresses and webhook URLs are masked in that message. |
| A store was skipped, saying it waited for you | It asked you for something in the browser (a sign-in, a code, a captcha) and nobody answered within `VNC_LOGIN_TIMEOUT`. That store stops asking for the rest of the run, so it does not sit on a login screen for hours or keep re-opening it, and the summary lists what it skipped. Raise the timeout, or let the next run pick it up. |
| Container crashes on start | Check logs: `docker compose logs app --tail=50`. A clean restart purges `.X1-lock` bugs. |

### Sessions, profiles and the data volume

The bot keeps three things outside the container: one browser profile per store, which holds your
logins, `fgc.db`, which records what has already been claimed, and `used_*_codes.txt`, the list of
recovery codes already spent. All of it lives in the `fgc_data` volume. **The program itself is not in
there**, it comes with the image, so deleting the volume never updates anything. It only makes you sign
in again.

Itch.io, Fanatical, IndieGala and Alienware Arena share one profile called `base`, because one browser
claims them all in a row. Fab rides Epic's profile for the same reason.

See what is stored:

```bash
docker compose run --rm --entrypoint bash app -c "ls /fgc/data/browser"
```

Reset one store, Epic in this example, when that store keeps failing in a way that looks like
leftover state: it will not sign in although the session should be valid, or its browser will not
start at all. There is nothing that wears out, so this is a repair, not maintenance, and it is not
worth doing on a schedule:

```bash
docker compose down
docker compose run --rm --entrypoint bash app -c "rm -rf /fgc/data/browser/epic"
docker compose up -d
```

You sign in to that one store again through VNC and everything else stays as it was.

Start completely fresh, logins and claim history included:

```bash
docker compose down -v
docker compose up -d
```

Keep this one for when you are truly stuck. You lose every saved login and the record of what was
already claimed, so the bot walks back through titles it had marked as done. Epic deserves a warning of
its own: it judges how established a browser profile looks, so a brand new profile is exactly when it
starts asking for captchas. Keep that profile if you can.

### Something is not working, what to send us

The normal log shows only what you act on: which store is running, who is signed in, what was found and
what was claimed, plus every warning and error. All the diagnostic detail is still there, one switch away:

1. **Check you are on the newest code first**, the fix may already exist. `docker logs fgc-remaster
| head -20` prints the version in the banner. To try the development build, set `FGC_TAG=dev` in
`.env`, then `docker compose pull` and `docker compose up -d`.
2. Set `DEBUG=true` in `.env` and restart (`docker compose up -d`), then reproduce the problem.
3. Collect the log. Go to the folder that holds your `docker-compose.yml` and run `docker logs
fgc-remaster --tail 500 > fgc.log.txt`. **The file appears in that folder, next to
`docker-compose.yml`**, and you drag it into the GitHub comment box. Pasting the terminal output of
the command itself sends us nothing, the log is inside the file. Prefer pasting? Run `docker logs
fgc-remaster --tail 100` and put the output between triple backticks. Account names are masked for
you (`p***@gmail.com`), so either way it is safe to share.
4. **Keep that file, then try it clean.** Leftover state explains a surprising share of these:
   ```bash
   docker compose down -v     # stops the bot and throws away logins and claim history
   docker compose pull        # takes the newest image
   docker compose up -d       # starts fresh, sign in again through VNC
   ```
   This is step two, never step one: `-v` deletes the volume, so every store asks you to sign in again
   and the bot re-checks titles it had already claimed. If the problem is gone, that was it. If it comes
   back, make a second log the same way as above and send **both**, the one from before and the one from
   after. Two logs of the same fault from two clean starts say far more than one.
5. Look in the `data/` folder for what the bot saw:
   - `data/screenshots/<store>/`, screenshots taken at every failure,
   - `data/ae_coin_api.json`, raw AliExpress check-in responses (streak, coins),
   - `data/steamdb_dump.html`, the SteamDB page as the bot parsed it (written only with `DEBUG=true`),
   - `data/*_fail.html`, page snapshots from failed logins/check-ins.
6. Watch it live if it is still running: open `http://localhost:7080` (noVNC) and take over the browser.

A good bug report is: what you expected, what happened, the `DEBUG=true` log around the failure, and the
matching screenshot. `DEBUG=true` covers what the bot itself did; only add `DEBUG_LIBS=true` if you are
asked for the raw network or browser traffic, because that turns one run into tens of thousands of lines.
Please switch both off again once the problem is solved.

---

## Credits

Inspired by [vogler/free-games-claimer](https://github.com/vogler/free-games-claimer) – the original
Node.js project.
This remaster is a **completely independent rewrite** in Python, not a fork.

---

## License

[AGPL-3.0](./LICENSE)

---

## Analytics

[![Star History Chart](https://api.star-history.com/svg?repos=P-Adamiec/Free-Games-Claimer-Remaster&type=Date)](https://www.star-history.com/?repos=P-Adamiec%2FFree-Games-Claimer-Remaster&type=date&legend=bottom-right)

---

<p align="center">
<img alt="logo-fgc-remaster" src="logo.png" width="256" />
</p>
