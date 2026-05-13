# Changelog

All notable changes to this project will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [1.0] - 2026-05-13

### Architecture
- Complete rewrite from Node.js (Playwright) to **Python 3 + nodriver** for stealth browser automation
- ACID-compliant **SQLite** database via SQLAlchemy replaces volatile `.json` file writes
- Object-oriented `BaseClaimer` class — all store modules inherit unified browser management
- **APScheduler** cron-based scheduling replaces shell-level `sleep` loops
- Multi-arch Docker support: `linux/amd64` (Google Chrome) + `linux/arm64` (Chromium)

### Store Modules
- **Steam** (`src/stores/steam.py`) — Entirely new auto-claimer (original JS only scraped profiles)
  - Queries GamerPower API + SteamDB for free-to-keep games
  - Claim button priority: `add_to_account` → discount form → `freeGameBtn` fallback
  - Automatic Steam Guard / 2FA login support
- **Epic Games** (`src/stores/epic.py`) — Headful nodriver bypasses hCaptcha checkpoints
- **Prime Gaming** (`src/stores/prime.py`)
  - URL slug-based platform detection (`-gog/dp/`, `-epic/dp/`, `-legacy/dp/`, `-aga/dp/`)
  - Direct navigation to detail pages — no more "Could not click" failures
  - Export codes to `prime-gaming.json` alongside SQLite
  - Automatic GOG code extraction and forwarding to GOG module for redemption
  - Account-linked platforms (Epic, Amazon) correctly identified and skipped
- **GOG** (`src/stores/gog.py`) — Direct auth page navigation, session persistence via `--restore-last-session`
  - Automatic redemption of GOG codes from Prime Gaming
  - Redemption guard: only triggers when pending codes exist or `GOG_FORCE_REDEEM` is set

### Infrastructure
- **VNC login fallback**: Configurable timeout for manual browser login via noVNC
- **Discord/Apprise notifications**: Granular `.env` triggers, game list formatting
- **Typed configuration** (`src/core/config.py`): Strict `.env` parsing into Python `Config` class
- **Startup banner**: Displays version and author on every launch

### Removed from Original
- ❌ `aliexpress.js` — Out of scope (not gaming)
- ❌ `unrealengine.js` — Out of scope
- ❌ `steam-games.js` — Only scraped profiles, never claimed games
