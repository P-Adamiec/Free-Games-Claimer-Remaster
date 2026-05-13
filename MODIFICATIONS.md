# Comprehensive Architectural & Security Review: Python Remaster vs Original JS

This document serves as a 100% transparent, file-by-file technical breakdown of the `fgc-remaster` (Python) project compared to the original `free-games-claimer` (Node.js). 

If you are inspecting this codebase for security reasons, migrating your deployment, or simply wanting to understand how the internal logic was rewritten to defeat modern bot protections—this document covers every addition, modification, and omission without any marketing fluff.

---

## 1. Executive Summary & Security Posture

The original project relied on **Node.js** and **Playwright/Patchright**. While effective initially, advanced bot-protection services like Cloudflare Turnstile, hCaptcha, and Epic's in-house security have become exceptionally good at fingerprinting Playwright's Chrome DevTools Protocol (CDP) signatures.

The **Python Remaster** was built from the ground up to solve two critical flaws:
1. **Automation Detection**: The core was migrated to **Python 3** using [`nodriver`](https://github.com/ultrafunkamsterdam/nodriver), an ultra-stealthy CDP-based asyncio library that commands official Chrome binaries without leaving typical automation fingerprints.
2. **Data Corruption**: Volatile, highly concurrent `.json` file-writes were replaced with an ACID-compliant asynchronous **SQLite** database via `SQLAlchemy`.

**There are no obfuscated binaries, no hidden tracking telemetry, and no unauthorized outbound requests.** All web requests either fetch target URLs (Epic, GOG, Amazon, SteamDB, GamerPower) or dispatch JSON payloads exactly to your configured Discord/Apprise Webhooks.

---

## 2. What was DROPPED? (File Omissions)

The following scripts from the original `free-games-claimer-dev` were deemed out-of-scope for a core gaming claimer or misleading, and were **deleted**:

- ❌ `aliexpress.js`: The original project contained an AliExpress coin scraper. This has been removed to keep the focus strictly on gaming.
- ❌ `unrealengine.js`: Removed. Claiming free Unreal Engine marketplace assets is outside the scope of consumer gaming.
- ❌ `steam-games.js` (Original): The original JS project contained a file named `steam-games.js`, **but it never claimed games.** It only scraped your public Steam profile to read playtime hours and achievements. It was deleted and completely replaced with a real auto-claimer.
- ❌ `package.json` / `package-lock.json` / `eslint.config.js`: Entire Node.js ecosystem files removed in favor of Python's `requirements.txt`.

---

## 3. What was ADDED & REWRITTEN? (File Additions)

### A. The Core Engine (`src/core/`)
Instead of duplicating browser launch logic in every single store file (as the JS version did), the Remaster uses a strictly Object-Oriented approach.
- 🟢 `claimer.py`: Contains the `BaseClaimer` class. Every store inherits from this. It standardizes the `nodriver` Chromium launch flags (crucially passing `--restore-last-session`), manages the directory mapping for persistent profiles (`/data/browser/`), and handles unified teardown. 
- 🟢 `database.py`: A new `SQLAlchemy` async ORM layout managing an `fgc.db` SQLite database. It checks if a `(store, user, game_id)` tuple already exists before querying the web, preventing data-race conditions on multiple threads. It also exports backwards-compatible JSON (`prime-gaming.json`, etc.) on exit for legacy users.
- 🟢 `config.py`: Replaces the old `config.js`. It explicitly strictly parses your `.env` file into a typed Python `Config` class.
- 🟢 `notifier.py`: A native web-request wrapper utilizing `httpx` and `apprise` to dispatch Discord messages. It reads granular `.env` triggers (`NOTIFY_SUMMARY=1`, `NOTIFY_ERRORS=1`, etc.) to prevent the bot from spamming your server with CAPTCHA alerts.

### B. The Store Modules (`src/stores/`)
- 🟢 `epic.py`: Rewritten to force strict `headful` mode using `nodriver`. This flawlessly mimics a real human user opening an Epic Games tab, almost entirely bypassing the aggressive hCaptcha checkpoints that halted the Node.js version.
- 🟢 `prime.py`: Fixed critical DOM selector bugs from the JS version. Amazon recently injected "Sign in with Passkey" prompts which trapped the old script in an infinite loop. The Python port explicitly targets the standard form. Additionally, it intelligently differentiates between natively claimed Amazon games and external keys (e.g., extracting GOG activation codes).
- 🟢 `gog.py`: Streamlined the giveaway banner parsing logic. Most importantly, it binds natively to Chromium's `--restore-last-session` boot flag. This prevents Docker restarts from wiping out your ephemeral `gog-al` session cookies (which previously caused random logouts).
- 🟢 `steam.py` (**Completely New Feature**): Replacing the useless original script, this entirely new module actively searches for "100% Free to Keep" titles. It queries the `GamerPower` API and stealthily scrapes `SteamDB` (using the browser to bypass Cloudflare), then logs into Steam to finalize 100% discount purchases securely.

### C. Docker Infrastructure
- 🟢 `Dockerfile`: Migrated the base image to `ubuntu:noble` pulling `python3` and `google-chrome-stable`. The stack still includes `TurboVNC`, `VirtualGL`, and `noVNC` pointing to `localhost:7080` so you can visually verify or solve captchas exactly as before.
- 🟢 `docker-entrypoint.sh`: Heavily upgraded the container startup protocol. If you brutally stop the container, Chromium leaves behind `SingletonLock` and TurboVNC leaves `/tmp/.X1-lock`. On next boot, these would block the display and Chromium from starting. The shell script now elegantly scrubs orphaned locks before spinning up the X-server. Useless legacy `xauth` footprint diagnostics were also silenced.

---

## 4. Verification Check
If you wish to verify the integrity of the project:
1. Examine `requirements.txt` — you will find standard, highly-vetted open-source libraries (`nodriver`, `sqlalchemy`, `aiosqlite`, `httpx`, `apprise`, `tenacity`).
2. Search the codebase for execution tools (`os.system`, `subprocess`, `eval`, `exec`) — you will find them strictly bounded to safe operations or entirely absent inside core logic.
3. Review outbound requests (e.g., search for `httpx.AsyncClient`) — you will see it only contacts target gaming stores, the SteamDB/GamerPower APIs, and exactly the webhook domain you provide in your `.env`.
