# free-games-claimer-remaster

<p align="center">
  <img alt="logo-free-games-claimer" src="https://user-images.githubusercontent.com/493741/214588518-a4c89998-127e-4a8c-9b1e-ee4a9d075715.png" />
</p>

> **Not a fork** — a complete ground-up Python remaster inspired by [vogler/free-games-claimer](https://github.com/vogler/free-games-claimer). 
>
> ℹ️ **Are you coming from the original Node.js version?**  
> For a comprehensive, file-by-file breakdown of what changed, dropped features, stealth automation upgrades, and architectural differences, **please read [MODIFICATIONS.md](./MODIFICATIONS.md).**

Automatically claims free games on:

- <img alt="logo steam" src="https://store.steampowered.com/favicon.ico" width="20" align="middle" /> **Steam** — via [SteamDB](https://steamdb.info/upcoming/free/) scraping + [GamerPower API](https://www.gamerpower.com/api) (only *Free to Keep*, not *Play for Free*)
- <img alt="logo epic-games" src="https://github.com/user-attachments/assets/82e9e9bf-b6ac-4f20-91db-36d2c8429cb6" width="20" align="middle" /> **Epic Games Store** — weekly free games
- <img alt="logo prime-gaming" src="https://github.com/user-attachments/assets/7627a108-20c6-4525-a1d8-5d221ee89d6e" width="20" align="middle" /> **Amazon Prime Gaming** — monthly Prime Gaming catalogue + GOG key redemption
- <img alt="logo gog" src="https://github.com/user-attachments/assets/49040b50-ee14-4439-8e3c-e93cafd7c3a5" width="20" align="middle" /> **GOG** — periodic free giveaways

Runs as a Docker container with a built-in scheduler (every 12 hours). Login via **VNC in browser** or automated credentials.

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

# Notifications
DISCORD_WEBHOOK=https://discord.com/api/webhooks/...

# Run only specific stores (comma-separated)
# Leave commented to run ALL
# STORES=steam,prime,gog
```

### 2. Build and run

```bash
docker compose up -d --build
```

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
| `SHOW` | `1` | Show browser window (VNC). |
| `WIDTH` | `1280` | Browser/VNC screen width. |
| `HEIGHT` | `720` | Browser/VNC screen height. |
| `NOVNC_PORT` | `7080` | noVNC web access port. |
| `SCHEDULER_HOURS`| `12` | Hours interval for the built-in scheduler runs. |
| `VNC_LOGIN_TIMEOUT`| `180` | Seconds to wait for you to log in via VNC manually. |
| `EG_EMAIL` | | Epic Games login email. |
| `EG_PASSWORD` | | Epic Games login password. |
| `EG_OTPKEY` | | Epic Games 2FA OTP key. |
| `PG_EMAIL` | | Prime Gaming (Amazon) email. |
| `PG_PASSWORD` | | Prime Gaming password. |
| `PG_OTPKEY` | | Prime Gaming 2FA OTP key. |
| `GOG_EMAIL` | | GOG login email. |
| `GOG_PASSWORD` | | GOG login password. |
| `STEAM_USERNAME` | | Steam username. |
| `STEAM_PASSWORD` | | Steam password. |
| `STORES` | *(all)* | Comma-separated list of stores to run. |
| `DISCORD_WEBHOOK` | | Discord webhook URL for notifications. |
| `NOTIFY_SUMMARY` | `1` | Set to 0 to disable game claim summaries. |
| `NOTIFY_ERRORS` | `1` | Set to 0 to disable fatal error alerts. |
| `NOTIFY_CLAIM_FAILS`| `1` | Set to 0 to disable alerts for unclaimable games. |
| `NOTIFY_LOGIN_REQUEST`| `1` | Set to 0 to disable VNC login request pings. |

### Selective module execution

Run only specific stores using accepted module aliases (`steam`, `epic`, `prime`, `gog`, `amazon`):

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
├── docker-compose.yml      # Base App container configuration
├── Dockerfile              # Ubuntu + Chrome + TurboVNC + Python
├── MODIFICATIONS.md        # Technical explanation of the codebase overhaul
├── .env                    # User configurations
└── src/
    ├── core/               # Shared engine components
    │   ├── claimer.py      # BaseClaimer & CDP stealth patches
    │   ├── config.py       # Configuration loader
    │   ├── database.py     # SQLAlchemy models & SQLite engine
    │   └── notifier.py     # Modular Discord/apprise webhooks
    └── stores/             # Specific store modules
        ├── epic.py
        ├── prime.py
        ├── gog.py
        └── steam.py
```

### How it works

1. **Scheduler** (`main.py`) runs every 12 hours
2. Each store module **starts its own browser** with an isolated profile, securely recalling session cookies (`--restore-last-session`).
3. **Login detection** checks the page DOM (not just cookies/DB).
4. **Stealth profiles** are injected via `nodriver` directly via Official Chrome binaries before any page loads.
5. **Game discovery** utilizes specialized scrapers (like reading SteamDB to circumvent typical lists).
6. **Robust Database Storage** verifies historical success in `fgc.db` (SQLite) to block aggressive overlapping.
7. **Clean Notifications** dispatch to you dynamically based on the toggles configured in the `.env` settings.

---

## Notifications

Set `DISCORD_WEBHOOK` in `.env` for Discord notifications about claimed games and errors. Use the respective `NOTIFY_...=0` flags to silence notification subsets if they generate too much noise.

For other services, [apprise](https://github.com/caronc/apprise) natively supports sending to Telegram, Slack, Matrix and more — just set the `NOTIFY` variable!

---

## Troubleshooting

| Issue | Solution |
|---|---|
| Store not logging in | Open VNC (`http://localhost:7080`) and login manually. Your credentials or session logic persist beautifully after first login. |
| Steam game not detected | Check that the game is listed on [GamerPower](https://www.gamerpower.com/api/giveaways?platform=steam&type=game) or [SteamDB Free](https://steamdb.info/upcoming/free/). |
| Epic captcha | The stealth patches prevent 99% of captchas. If a rigorous manual prompt arrives, solve it once via VNC. |
| Prime Gaming "Sign in" loop | The module ignores fake Amazon "Passkey" banners to find the real login endpoint natively. |
| Container crashes on start | Check logs: `docker compose logs app --tail=50`. A clean restart purges `.X1-lock` bugs. |

---

## Credits

Inspired by [vogler/free-games-claimer](https://github.com/vogler/free-games-claimer) — the original Node.js project.
This remaster is a **completely independent rewrite** in Python, not a fork.

---

## License

[AGPL-3.0](./LICENSE)

---

[![Star History Chart](https://api.star-history.com/svg?repos=P-Adamiec/Free-Games-Claimer-Remaster&type=Date)](https://www.star-history.com/?repos=P-Adamiec%2FFree-Games-Claimer-Remaster&type=date&legend=bottom-right)

![Alt](https://repobeats.axiom.co/api/embed/5c6416eef2d3371808c7d1d50418546103b351f4.svg "Repobeats analytics image")

---

<p align="center">
<img alt="logo-fgc-remaster" src="logo.png" width="256" />
</p>
