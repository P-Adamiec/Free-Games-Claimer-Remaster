# free-games-claimer-remaster

<p align="center">
  <img alt="logo-free-games-claimer" src="https://github.com/user-attachments/assets/6d749140-c2a2-45d4-ac73-70b16e74a757" />
</p>

> **Not a fork** – a complete ground-up Python remaster inspired by [vogler/free-games-claimer](https://github.com/vogler/free-games-claimer).
>
> ℹ️ **Are you coming from the original Node.js version?**  
> For a comprehensive, file-by-file breakdown of what changed, dropped features, stealth automation upgrades, and architectural differences, **please read [MODIFICATIONS.md](./MODIFICATIONS.md).**

Automatically claims free games on:

- <img alt="logo steam" src="https://store.steampowered.com/favicon.ico" width="20" align="middle" /> **Steam** – paid games and DLC that are free to keep for a while, found through GamerPower and Steam's own store search
- <img alt="logo epic-games" src="https://github.com/user-attachments/assets/82e9e9bf-b6ac-4f20-91db-36d2c8429cb6" width="20" align="middle" /> **Epic Games Store** – weekly free games, including the weekly free Android/iOS mobile game (`EG_MOBILE`)
- <img alt="logo fab" src="https://www.google.com/s2/favicons?domain=fab.com&sz=64" width="20" align="middle" /> **Fab** – Epic's asset marketplace, its limited-time free assets on your Epic account (opt-in, add `fab` to `STORES`)
- <img alt="logo unity" src="https://www.google.com/s2/favicons?domain=unity.com&sz=64" width="20" align="middle" /> **Unity Asset Store** – the weekly free [Publisher of the Week](https://assetstore.unity.com/publisher-sale) asset, coupon and all (opt-in, add `unity` to `STORES`)
- <img alt="logo prime-gaming" src="https://github.com/user-attachments/assets/7627a108-20c6-4525-a1d8-5d221ee89d6e" width="20" align="middle" /> **Amazon Prime Gaming** – monthly Prime Gaming catalogue + GOG, Microsoft and Legacy Games key redemption
- <img alt="logo gog" src="https://github.com/user-attachments/assets/49040b50-ee14-4439-8e3c-e93cafd7c3a5" width="20" align="middle" /> **GOG** – periodic free giveaways
- <img alt="logo microsoft" src="https://www.google.com/s2/favicons?domain=xbox.com&sz=64" width="20" align="middle" /> **Microsoft Store** – Prime Gaming code redemption + paid games while they are free to keep
- <img alt="logo microsoft-rewards" src="https://www.google.com/s2/favicons?domain=rewards.bing.com&sz=64" width="20" align="middle" /> **Microsoft Rewards** – the daily points: the daily set, the one-visit tasks, punch cards and Bing searches up to the day's limit, streak protection kept on, and the points claimed before they expire. When Microsoft shows its bot warning on the dashboard, the bot stops everything else on that account for the run and only claims what was already earned, and a check from Bing ends that day's searches. Microsoft's terms do not count searches made by a bot and can close the Rewards account, so this is at your own risk (opt-in, add `msrewards` to `STORES`, see the [Disclaimer](./DISCLAIMER.md))
- <img alt="logo legacy-games" src="https://www.google.com/s2/favicons?domain=legacygames.com&sz=64" width="20" align="middle" /> **Legacy Games** – redeems the Legacy Games codes Prime Gaming hands out into your Legacy Games account before they expire, with the newsletter left unticked
- <picture><source media="(prefers-color-scheme: dark)" srcset="https://cdn.simpleicons.org/ubisoft/ffffff" /><img alt="logo ubisoft" src="https://cdn.simpleicons.org/ubisoft/000000" width="20" align="middle" /></picture> **Ubisoft** – free game giveaways from [ubisoft.com/games/free](https://www.ubisoft.com/en-us/games/free) (giveaways only, never trials, demos or free weekends)
- <img alt="logo aliexpress" src="https://www.google.com/s2/favicons?domain=aliexpress.com&sz=32" width="20" align="middle" /> **AliExpress** – automated daily check-in that collects coins, using a real-device mobile fingerprint to stay undetected and reading the balance from the coin API. Accounts that live on a country site (for example it.aliexpress.com) are recognised automatically. The coin page often arrives empty; the bot walks back to it up to `AE_PAGE_RETRIES` times (four by default), then reports it and moves on (see [Troubleshooting](#troubleshooting))

**GamerPower API**, asked once per run when a store in that run can use the answer, finds giveaways the stores themselves do not advertise, then hands each one to the matching store above. It also reaches these sites, which have no store module of their own and are chosen in `STORES` like any other store:
- <img alt="logo fanatical" src="https://www.fanatical.com/favicon.ico" width="20" align="middle" /> **Fanatical** – free games, claimed through Fanatical's free checkout and confirmed in your Fanatical orders; the Steam key is then activated on your Steam account when `steam` is in `STORES`, and a key Fanatical does not show at the claim is looked for in Order History & Keys at most every 20 hours, five times, then left in your Fanatical library; when Fanatical e-mails a code to reveal the key, the bot stops looking (add `fanatical` to `STORES`, see the warning below)
- <img alt="logo itchio" src="https://itch.io/favicon.ico" width="20" align="middle" /> **Itch.io** – DRM-free indie giveaways, claimed to your library and verified there; a giveaway itch.io hands out as downloads only, such as a free bundle, is reported once and looked at again every three days (add `itchio` to `STORES`)
- <img alt="logo indiegala" src="https://www.indiegala.com/favicon.ico" width="20" align="middle" /> **IndieGala** – free DRM-free games, claimed to your library and verified there (add `indiegala` to `STORES`, see the note below)
- <img alt="logo alienware" src="https://www.alienwarearena.com/favicon.ico" width="20" align="middle" /> **Alienware Arena** – notify only: key giveaways cost ARP points and solve a captcha, so the bot tells you about them and you claim them yourself (add `alienware` to `STORES`)

> [!NOTE]
> **Your feedback helps with IndieGala.** Its login page always shows a captcha, so the bot types your e-mail and password and asks you over VNC to tick the box. IndieGala then keeps you signed in for 14 days, and the bot is built to hold on to that sign-in for all of them, so this happens at most once every two weeks and only when there is an IndieGala giveaway. Two-factor sign-in is not supported for IndieGala. Please tell us in the issues how it works for you, and bear with this store while it settles in.

> [!WARNING]
> **Before you add `fanatical` to `STORES`, connect your Steam account on Fanatical once:** sign in at fanatical.com and link Steam under [Linked accounts](https://www.fanatical.com/en/account/linked-accounts) (a limited Steam account does not count). Some giveaways require it and the bot does not do it for you; when one finds Steam unlinked, the summary says so, and once you link it the next run claims it.
>
> **Fanatical has claimed two real giveaways so far, and your feedback helps.** Both went end to end: the newsletter step, Fanatical's free checkout, the key, and its activation on Steam. Before the second checkout Fanatical once showed a deals page without the cart total and the bot asked you; 1.19 reads the header's total there instead, which no real giveaway has tried yet. A giveaway that asks for the newsletter is handled by the bot (it subscribes you, then unsubscribes you again afterwards unless `FANATICAL_NEWSLETTER=true`). A Steam wishlist or a partner link step is asked of you over VNC; those have not been tried on a real giveaway yet, nor has a giveaway that sold out. If the bot meets one on your account, please tell us in the issues how it went and attach the log from that run (`DEBUG=true` helps most).

> [!TIP]
> **There is more free stuff out there than the storefronts show you.** Epic advertises two games a week on its front page, but GamerPower regularly lists half a dozen more that are free right now on the very same account, and other stores are no different. You get those for free: `STORES=steam` claims the Steam giveaways GamerPower lists, in the same browser session, and leaves the Epic ones alone. The extra sites (Fanatical, Itch.io, IndieGala, Alienware Arena) run only when you name them: `STORES=steam,itchio`. The first three claim with your account there; Alienware Arena giveaways cost ARP points and a captcha, so the bot only tells you about them.

Runs as a Docker container with a built-in scheduler (every 12 hours by default, with optional fixed daily run times). Login via **VNC in browser** or automated credentials.

> [!CAUTION]
> **You run it at your own risk.** Several of these stores forbid bots in their terms, and a store that notices can restrict or close the account. Read the [Disclaimer](./DISCLAIMER.md) before you start.

---

## Quick start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- A machine with an x86-64 processor (the image uses Google Chrome) or an ARM64 one such as a Raspberry Pi (it uses Chromium)

> 🟢 **New to Docker on Windows?** Read the step-by-step [**WINDOWS_BEGINNER_GUIDE.md**](./WINDOWS_BEGINNER_GUIDE.md) to set up Docker Desktop, optimize RAM limits, and use Dockhand to deploy this flawlessly.

### 1. Clone and configure

```bash
git clone https://github.com/P-Adamiec/free-games-claimer-remaster.git
cd free-games-claimer-remaster
cp .env.example .env
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
# Leave commented to run the defaults (everything except fab, unity, msrewards and the sites
# with no module of their own: itchio, fanatical, indiegala, alienware)
# STORES=steam,prime,gog
```

> 💡 **Password with a `$` in it?** Wrap it in single quotes, for example `EG_PASSWORD='abc$def'`. Docker Compose treats `$name` in unquoted and double-quoted values as a variable and would cut your password short.

### 2. Run container

```bash
docker compose up -d
```

> 💡 **Want to test experimental development features?** Add `FGC_TAG=dev` to your `.env` file before running Docker to automatically download our pre-release build!

### 3. Login (first run)

Open **http://localhost:7080** in your browser to access the VNC session.

Each store will wait for you to login manually on the first run if you don't supply credentials. After that, session cookies are natively restored using persistent browser profiles!

It waits `VNC_LOGIN_TIMEOUT` seconds for you, three minutes by default. If nobody answers, that one store is left alone for the rest of the run and the summary says so, so a bot running while you sleep does not sit on a login screen for hours. The next run tries again.

### 4. Monitor

To see what the bot is doing in real-time regardless of your current terminal folder, inspect the container directly:
```bash
docker logs -f fgc-remaster
```

### 5. Update

When a newer release is out, the bot tells you (`NOTIFY_UPDATES`). To take it, run this in the folder with your `docker-compose.yml`:

```bash
docker compose pull && docker compose up -d
```

---

## Configuration

Options are set via environment variables in `.env`:

| Variable | Default | Description |
|---|---|---|
| `FGC_TAG` | `latest` | Docker image tag to run (set to `dev` to test experimental pre-release builds). |
| `PUID` | | Run the bot as this user id instead of root, so files in a mounted data folder belong to you on the host. Use what `id -u` prints there. With `cap_drop: ALL` in your compose, add `cap_add: [CHOWN, SETUID, SETGID]`, which the switch needs. |
| `PGID` | | Group id to go with `PUID` (what `id -g` prints). Defaults to `PUID`. |
| `SHOW` | `1` | Show browser window (VNC). |
| `WIDTH` | `1280` | Browser/VNC screen width. |
| `HEIGHT` | `720` | Browser/VNC screen height. |
| `NOVNC_PORT` | `7080` | noVNC web access port. noVNC uses this port both inside the container and on your machine. |
| `VNC_IP` | `localhost`| Host for VNC notification links. Alerts include a one-click `http://<VNC_IP>:<NOVNC_PORT>/?autoconnect=true`. A public address is masked in the log. |
| `VNC_URL` | | Full public noVNC address for notification links, e.g. `https://fgc.example.tld`. Use it behind a reverse proxy: it keeps your scheme and drops the port, replacing `VNC_IP` and `NOVNC_PORT` in the link (`NOVNC_PORT` still publishes the container port). The log shows `***` for a public address; notifications keep it whole. |
| `VNC_PASSWORD` | | Password for VNC access. Empty means no password: anyone who can reach the noVNC port can use the browser and every store it is signed in to, so set one unless only you can reach the machine. When `VNC_URL` or `VNC_IP` is a public address and the password is empty, the bot also notifies you at every start. That notice stays quiet when `VNC_URL` carries a proxy login (`user:password@`), and such a login then travels in every notification link (the log masks it). |
| `SCHEDULER_HOURS`| `12` | Interval in whole hours between automatic claiming runs (for example: `1`, `12`, `24`, `72`). Set `0` to turn interval runs off; with no `SCHEDULER_FIXED_TIMES` either, the container runs once and stops, see `RUN_ONCE`. |
| `SCHEDULER_TIMEZONE` | `UTC` | IANA timezone used for fixed daily scheduler times. |
| `TZ` | | The container's own clock, log times included. When set, the browser uses it too and `BROWSER_TIMEZONE` is skipped. |
| `BROWSER_TIMEZONE` | `auto` | The time zone the browser shows sites. `auto` looks up where your IP address is on the first run and remembers it for a month (`BROWSER_TIMEZONE_REFRESH`), one request to ipapi.co, or ipwho.is if that fails, which sees your IP address, because a browser on UTC next to, say, a Polish IP is a sign anti-bot checks look for. `off` keeps UTC, or name one yourself (`Europe/Warsaw`) and nothing is asked. A `TZ` you set for the container wins. It does not move the scheduler, which uses `SCHEDULER_TIMEZONE`. See [Time zone and your IP address](#time-zone-and-your-ip-address). |
| `BROWSER_TIMEZONE_REFRESH` | `month` | How often `auto` asks again: `run` (every run), `week` or `month`. The answer is kept in `data/browser_timezone.json`; delete that file to have it asked on the next run. Does nothing when you named a zone, chose `off` or set `TZ`, because then nothing is asked at all. |
| `SCHEDULER_FIXED_TIMES` | | Optional comma-separated daily run times in 24-hour `HH:MM` format (`17:00,21:30`). Set `SCHEDULER_HOURS=0` if you want *only* these fixed daily times without interval runs. |
| `RUN_ON_STARTUP` | `true` | Run once immediately when the container/application starts. |
| `RUN_ONCE` | `false` | Claim once, then stop the container (exit code 0), for scheduling it from outside with cron or Ofelia. `SCHEDULER_HOURS=0` with no fixed times does the same, since nothing is scheduled. Set `restart: "no"` in `docker-compose.yml` or Docker will start it again straight away. Such a run does not wait for a captcha nobody solved within `VNC_LOGIN_TIMEOUT`: the next start tries again. |
| `VNC_LOGIN_TIMEOUT`| `180` | Seconds the bot waits for **you** at any manual step: signing in, a code from e-mail or SMS, approving 2FA, a captcha, or Unity's checkout form. A window nobody answers ends that one store's manual steps for the rest of the run, and the summary says what it skipped. Raise it if you are not usually sitting at the computer. |
| `EMAIL` | | Default login email for the stores that sign in with an e-mail, unless a store-specific `*_EMAIL` overrides it. Steam signs in with `STEAM_USERNAME` only and Legacy Games with `LG_EMAIL` only. |
| `PASSWORD` | | Default login password for those stores, unless a store-specific `*_PASSWORD` overrides it. Legacy Games uses `LG_PASSWORD` only. Microsoft Rewards with its own `MS_REWARDS_EMAIL` falls back to this one. |
| `EG_EMAIL` | | Epic Games login email. |
| `EG_PASSWORD` | | Epic Games login password. |
| `EG_OTP_KEY` | | Epic Games authenticator (TOTP) key, auto-filled. Email/SMS codes are entered manually via VNC. |
| `EG_OTP_CODES` | | Comma-separated Epic recovery codes from the authenticator setup. Spent one at a time, used ones are written to `data/used_epic_codes.txt`. |
| `EG_PARENTALPIN` | | Epic Games Parental Controls PIN. |
| `EG_MOBILE` | `true` | Also claim Epic's weekly free mobile game (claimed on the same store page as the PC games). |
| `EG_MOBILE_PLATFORMS` | `android,ios` | Which mobile versions to claim, Android and iOS are separate items of the same game. |
| `EG_CLAIM_DLC` | `true` | Also claim the DLC giveaways GamerPower finds for Epic. Only DLC GamerPower gives a price for count, so codes for items inside a game stay out. |
| `EG_CAPTCHA_ASK` | `false` | A picture puzzle in Epic's checkout (also Fab's). `false`: the game, and the rest of that run's games, wait for the next run, and the summary says a captcha was not solved; a game or asset whose giveaway ends before the next run still asks you, so it is not lost (with `RUN_ONCE`, or an outside scheduler, every game with a known end counts as on its last run). `true`: you are asked over VNC to solve it, once per game, for example in December, when Epic's free game changes every day. With the built-in scheduler, a puzzle you were asked about and did not solve within `VNC_LOGIN_TIMEOUT` (it also expires after about two minutes) then waits for you: the notification and the summary say until when, and once you open noVNC (reload it if it is already open) the bot opens that checkout again, so you get a fresh puzzle. Without you, a game whose giveaway ends before the next run gets one more try by the bot itself. A single run (`RUN_ONCE`, Ofelia, cron) does not wait: its next start tries again. |
| `PG_EMAIL` | | Prime Gaming (Amazon) email. |
| `PG_PASSWORD` | | Prime Gaming password. |
| `PG_OTP_KEY` | | Prime Gaming authenticator (TOTP) key. |
| `PG_FORCE_CHECK_COLLECTED` | `false` | Open every game already marked 'claimed' again. A game whose code was not on the page yet is looked for again on its own, without this. |
| `LG_EMAIL` | | Legacy Games account email. With `LG_PASSWORD`, the Legacy Games codes from Prime Gaming are redeemed into that account in the same run (they expire within days). Without them the bot only says how many codes are waiting. The newsletter box is always unticked. |
| `LG_PASSWORD` | | Legacy Games account password. |
| `GOG_EMAIL` | | GOG login email. |
| `GOG_PASSWORD` | | GOG login password. |
| `GOG_NEWSLETTER` | `false` | Keep newsletter sub after claiming (`true` = keep). |
| `GOG_FORCE_REDEEM` | `false` | Force re-redeem old GOG codes from Prime Gaming. |
| `GOG_OTP_KEY` | | GOG authenticator (TOTP) secret, auto-filled. Codes sent by e-mail are entered manually via VNC. |
| `GOG_OTP_CODES` | | Comma-separated GOG recovery codes, tried after the secret. Used ones are written to `data/used_gog_codes.txt`. |
| `MS_EMAIL` | | Microsoft account email. |
| `MS_PASSWORD` | | Microsoft account password. |
| `MS_FORCE_REDEEM` | `false` | Force another pass over Microsoft codes from the last 60 days. |
| `MS_CLAIM_DLC` | `true` | Also claim the DLC giveaways GamerPower finds for Xbox. Only DLC GamerPower gives a price for count, so codes for items inside a game stay out. |
| `MS_OTP_KEY` | | Microsoft authenticator (TOTP) secret, auto-filled. E-mail codes and passkeys are done by you over VNC. |
| `MS_REWARDS_EMAIL` | `MS_EMAIL` | Microsoft Rewards account, only used when `msrewards` is in `STORES`. Leave the three `MS_REWARDS_` settings out to use the Microsoft Store account above. |
| `MS_REWARDS_PASSWORD` | `MS_PASSWORD` | Password of that account (`PASSWORD` when it has its own e-mail). |
| `MS_REWARDS_OTP_KEY` | `MS_OTP_KEY` (none when it has its own e-mail) | Authenticator (TOTP) secret of that account, auto-filled. |
| `STEAM_USERNAME` | | Steam username. |
| `STEAM_PASSWORD` | | Steam password. |
| `STEAM_CLAIM_DLC` | `true` | Also claim Steam DLC at 100% off, from GamerPower and from Steam's own search. A DLC whose base game you don't own is reported once, then again every three weeks while it stays free. `false` keeps Steam to full games. |
| `STEAM_SEARCH` | `true` | Also read Steam's own store search after GamerPower. `false` leaves Steam to what GamerPower finds, a safety switch should Steam ever mind the search. |
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
| `STORES` | *(see note)* | Comma-separated list of stores to run. Empty runs `steam`, `epic`, `prime`, `gog`, `microsoft`, `ubisoft`, `aliexpress` and `legacy`. Add `fab`, `unity`, `msrewards`, `itchio`, `fanatical`, `indiegala` or `alienware` to switch one on. Put `msrewards` last, its searches take several minutes. GamerPower is asked once per run and its finds go to the store they belong to, so `steam` also claims the Steam giveaways it lists. |
| `RESET_DB_GAMES` | `false` | Erase the database claims recorded within the last 7 days, at every start while it is on, to clear false positives. Codes and keys still waiting to be redeemed are kept. Set it back to `false` after one start. |
| `FANATICAL_EMAIL` | | Fanatical account email. |
| `FANATICAL_PASSWORD`| | Fanatical account password. |
| `FANATICAL_OTP_KEY` | | Fanatical authenticator (TOTP) secret, auto-filled. Fanatical offers no other kind of two-factor. |
| `FANATICAL_NEWSLETTER` | `false` | Keep the Fanatical newsletter a claim signed you up for (a giveaway that asks for it, or the pre-ticked box at checkout); `true` = keep. A subscription you had before stays. |
| `ITCHIO_EMAIL` | | Itch.io account email. |
| `ITCHIO_PASSWORD` | | Itch.io account password. |
| `ITCHIO_OTP_KEY` | | Itch.io authenticator (TOTP) secret, auto-filled. |
| `ITCHIO_OTP_CODES` | | Comma-separated Itch.io recovery codes, tried after the secret. Used ones are recorded in `data/used_itchio_codes.txt` so none is sent twice. |
| `INDIEGALA_EMAIL` | | IndieGala account email. |
| `INDIEGALA_PASSWORD`| | IndieGala account password. |
| `GP_UNKNOWN_STORES`| `false`| Reserved. Opening a site nobody mapped is not built yet, so this stays off whatever you set. |
| `BROWSER_DIR` | `data/browser` | Browser profile directory (persists cookies/sessions). |
| `SCREENSHOTS_DIR` | `data/screenshots` | Directory where debug/failure screenshots are saved. |
| `DEBUG` | `true` | Shows verbose actions the bot takes, and saves a screenshot of each failure and of each human check (Cloudflare, a captcha) in `data/screenshots/`, also one after a claim on Steam, Prime Gaming and the GamerPower sites (which share `screenshots/gamerpower/`), and Microsoft Rewards also at each stage (dashboard, its tasks, daily set, Earn page, punch cards, first and last search, claim panel), as JPEG named with the date and time; screenshots older than 90 days are deleted. |
| `DEBUG_LIBS` | `false` | Adds the internals of the libraries the bot uses (every CDP frame sent to Chrome, HTTP handshakes, SQL queries). Only turn this on if a bug report asks for it. Your passwords, sign-in names, notification tokens and game codes should be masked in these lines too. |
| `DEBUG_SCREENSHOTS` | `false` | Needs `DEBUG=true`. Send the screenshots of failures and human checks (also kept in `data/screenshots/`, the after-claim ones are not sent) with a notification, to Discord and to Apprise services that take attachments. A screenshot can show your account name, e-mail or a game code, so send it only to a channel that is yours alone. |
| `DRYRUN`| `false` | Simulate a run without claiming games. Detects available giveaways and sends a summary report. |
| `DISCORD_WEBHOOK` | | Discord webhook URL for notifications. |
| `DISCORD_SUPPRESS_EMBEDS` | `true` | Show the links in Discord notifications without preview cards, so a summary with many games stays short. `false` brings the cards back. |
| `NOTIFY` | | Apprise URL(s) for Telegram, Slack, ntfy, etc. Multiple services can be separated by commas. |
| `NOTIFY_TEST` | `false` | Send a test notification on startup to verify your setup works. |
| `NOTIFY_SUMMARY` | `true` | Set to false to disable game claim summaries. (Applies to all services) |
| `NOTIFY_ERRORS` | `true` | Set to false to disable fatal error alerts. (Applies to all services) |
| `NOTIFY_CLAIM_FAILS`| `true` | Lists games and check-ins that could not be claimed in the run summary, with the reason (e.g. a free DLC without the base game). Set to false to hide them. (Applies to all services) |
| `NOTIFY_ALREADY_CLAIMED`| `false` | Set to true to also list games you already own, check-ins already collected today and giveaways skipped on purpose (free to play, not a real giveaway). |
| `NOTIFY_MISSING_BASE`| `true` | DLC you cannot claim because you do not own its base game: reported once, then again every three weeks while it stays free. Set to false to never list them. Buying the base game lets the next run claim the DLC. |
| `NOTIFY_DOWNLOAD_ONLY`| `true` | Set to false to drop Itch.io giveaways that are only a download, with nothing to claim onto your account. Reported once by default, then silent. |
| `NOTIFY_UPDATES` | `true` | Check GitHub for a newer release (at startup, then at most once a day) and notify you once per version. Set to false to disable the check entirely, no request is made. |
| `NOTIFY_LOGIN_REQUEST`| `true` | Set to false to disable VNC login request pings and the captcha reminders. (Applies to all services) |
| `NOTIFY_SKIP_STORES` | | Comma-separated stores whose notifications are silenced (they still run/claim): the summary, VNC pings and error alerts. Takes every name `STORES` takes, aliases (`ae`, `ms`, `itch`) and the GamerPower sites included. Example: `aliexpress`. |

### Two-factor sign-in

Nothing here is required. By default the bot stops at a code screen and asks you to finish it in the browser over VNC, waiting `VNC_LOGIN_TIMEOUT` seconds for you.

If you want it to get through 2FA on its own there are two ways, and they are not equivalent:

- **An authenticator secret** (`*_OTP_KEY`) is the key behind the QR code you scanned into your phone, offered as "manual entry key" during setup. Spaces and `=` at the end are fine; a key the bot cannot read is named in the first lines of the log, and the bot then waits for you to type the code over VNC. The bot becomes another authenticator app and never runs out of codes. What it costs: your password and your second factor then sit in the same `.env` on the same machine, so whoever reads that file has both.
- **Recovery codes** (`*_OTP_CODES`) are one-shot. The bot spends one per sign-in and writes it to `data/used_*_codes.txt`, so a stolen file costs a limited number of logins instead of permanent access, and you can see which ones were used. They do run out, and then it is back to VNC.

Set either, both, or neither. Filling one in is all it takes to switch it on. Stores that take recovery codes follow the same order: **two codes from the secret, then one recovery code, then you over VNC.** Fab, Fanatical, Prime Gaming, Microsoft and Microsoft Rewards have no recovery codes to spend, so there it is the secret and then you over VNC. The second code is never the same digits as the first, because a code refused inside its 30-second window stays refused.

| Store | Authenticator secret | Recovery codes | Spent codes |
|---|---|---|---|
| Epic | `EG_OTP_KEY` | `EG_OTP_CODES` | `data/used_epic_codes.txt` |
| Fab | `EG_OTP_KEY` | Fab never spends a recovery code | |
| Fanatical | `FANATICAL_OTP_KEY` | Fanatical does not issue any | |
| GOG | `GOG_OTP_KEY` | `GOG_OTP_CODES` | `data/used_gog_codes.txt` |
| IndieGala | not supported | not supported | the login captcha is ticked by you over VNC |
| Itch.io | `ITCHIO_OTP_KEY` | `ITCHIO_OTP_CODES` | `data/used_itchio_codes.txt` |
| Microsoft | `MS_OTP_KEY` | Microsoft issues one recovery code for the account, not codes for signing in | |
| Microsoft Rewards | `MS_REWARDS_OTP_KEY` (`MS_OTP_KEY` when it is the Microsoft Store account) | none | |
| Prime Gaming | `PG_OTP_KEY` | Amazon does not issue any | |
| Ubisoft | `UBI_OTP_KEY` | not supported yet | |
| Steam, AliExpress, everything else | | | codes are typed by you over VNC |

Either way this is usually a one-time event per store: on the code screen of Epic, Prime Gaming and the Microsoft Store the bot ticks the "remember this browser" box, so it stops being asked on that profile until the profile is deleted. The other stores may ask again.

### Time zone and your IP address

Websites can see two things that should agree: the country your IP address is in, and the time zone your browser reports. A browser that says UTC while your IP address is in Poland is one of the small mismatches anti-bot checks look for, so the bot gives its browser the time zone you actually live in.

**If you set nothing,** the bot asks a free service on its first run which time zone your IP address is in: [ipapi.co](https://ipapi.co), or [ipwho.is](https://ipwho.is) when the first one does not answer. It saves the answer in `data/browser_timezone.json` and asks again only once a month, so a move or a VPN in another country is picked up within a month. That service sees your IP address, the same way every website you open does. Nothing else is sent: no account, no e-mail address, nothing about your games.

**If you would rather the bot asked nobody,** write your time zone in `.env` yourself. Pick one line:

| What you write in `.env` | What websites see | Times in your log | Asks ipapi.co? |
|---|---|---|---|
| nothing (the default, `auto`) | the time zone your IP address is in | UTC | yes, once a month |
| `BROWSER_TIMEZONE=Europe/Warsaw` | Polish time | UTC | no |
| `TZ=Europe/Warsaw` | Polish time | Polish time | no |
| `BROWSER_TIMEZONE=off` | UTC | UTC | no |

`BROWSER_TIMEZONE` with your own zone is the recommended choice: it changes only what websites see, and a misspelled name is reported as a warning in the log at the start of the run, after which the bot keeps UTC. `TZ` also moves the times in your log to your own time, and when both are set `TZ` wins, but a misspelled `TZ` falls back to UTC without a word. `off` asks nobody too, but it leaves exactly the mismatch this is meant to avoid.

**If you keep `auto`, you choose how often it asks** with `BROWSER_TIMEZONE_REFRESH`:

| What you write in `.env` | Asks ipapi.co | Good for |
|---|---|---|
| nothing, or `BROWSER_TIMEZONE_REFRESH=month` | on the first run, then once a month | most people: your country rarely changes |
| `BROWSER_TIMEZONE_REFRESH=week` | on the first run, then once a week | a laptop or NAS that moves between countries now and then |
| `BROWSER_TIMEZONE_REFRESH=run` | at the start of every run | a VPN that changes country often |

When a lookup fails, the bot keeps the zone it saved last time and tries again on the next run. To have it ask straight away, for example after moving, delete `data/browser_timezone.json`.

**How to write your time zone.** Use the name from the [list of tz database time zones](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones), written as `Region/City` with the city in English. Not an abbreviation such as `CET`, and not an offset such as `+02:00`: a name follows summer and winter time by itself, an offset does not. Some examples:

| You live in | Write |
|---|---|
| Poland | `Europe/Warsaw` |
| Germany | `Europe/Berlin` |
| United Kingdom | `Europe/London` |
| New York, USA | `America/New_York` |
| California, USA | `America/Los_Angeles` |
| Brazil (São Paulo) | `America/Sao_Paulo` |
| Japan | `Asia/Tokyo` |
| Sydney, Australia | `Australia/Sydney` |

Not sure which one is yours? Open [ipapi.co/timezone](https://ipapi.co/timezone/) in your own browser: the one line it shows is exactly what the bot would have used.

### Unity, one-time setup

Unity is opt-in (`STORES=...,unity`) and its checkout refuses the free coupon until your Unity account carries a complete billing address. That is the one thing the bot cannot invent for you, so on the first claim it opens the checkout, pings you the same way it does for a login, and waits as long as `VNC_LOGIN_TIMEOUT` says, three minutes by default:

1. Open the VNC session and fill in what it names as missing: first name, last name, address, postal code and city, and the state or province where Unity asks for one.
2. Answer **"Are you exempt from paying consumption tax?"** with **No**, unless you genuinely have a tax number. With one on the account, the bot leaves that section alone. Unity asks this only in some countries, US accounts do not see it.
3. Leave the page as it is, the bot applies the coupon and finishes the claim in the same session.

Unity keeps this on your account, so every later week runs unattended. Missing the window is not a failure: the asset is reported as `skipped:setup`, the rest of the run carries on, and the next run tries again (each giveaway lasts a week).

> [!NOTE]
> **The claim is written for Unity's checkout in English**, which is what the bot's browser profile asks for. It finds the coupon box, the total and the confirm button by their English labels. If your account forces another language onto the checkout, the bot cannot read the amount, so it refuses to confirm the order and reports the asset as not claimed. It never pays in that state: the only thing it ever confirms is a total of exactly zero. Set the Asset Store language back to English and the claim works again.

---

### Scheduler

The application supports three scheduling modes: running on a recurring interval (`SCHEDULER_HOURS`), running at specific daily clock times (`SCHEDULER_FIXED_TIMES`), or combining both.

### Scheduling Modes & Interaction

1. **Interval-Only Mode (Default)**: Runs periodically every `n` hours.
   ```ini
   SCHEDULER_HOURS=12
   SCHEDULER_FIXED_TIMES=
   ```
   - `SCHEDULER_HOURS` accepts whole hours (e.g. `1`, `12`, `24`, `48`, `72`). `0` turns interval runs off, and with no `SCHEDULER_FIXED_TIMES` the container runs once and stops (see `RUN_ONCE`).
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

Run only specific stores using accepted module aliases (`steam`, `epic`, `prime`/`amazon`, `gog`, `ubisoft`/`ubi`, `fab`, `unity`, `microsoft`/`ms`/`xbox`, `msrewards`/`ms-rewards`/`microsoft-rewards`/`rewards`/`bing-rewards`, `aliexpress`/`ae`, `legacygames`/`legacy`/`lg`, `itchio`/`itch`, `fanatical`, `indiegala`, `alienware`):

```bash
# Method 1: Via environment variable (recommended)
# Edit .env: STORES=steam,amazon

# Method 2: One-off immediate run of the named stores (ignores the scheduler)
docker compose stop      # two bots must not share the same browser profiles
docker compose run --rm --service-ports app python main.py steam gog --once
docker compose start
```

To skip a store, simply exclude it from the `STORES` list. Any unrecognized settings are reported at startup rather than being ignored silently.

> [!NOTE]
> **What happens in one run, in order.** GamerPower is asked first, once, and only when this run has a store that can use the answer. Then each big store runs: it claims what it finds itself and, at the end of the same browser session, the giveaways GamerPower found for it. Then any GOG and Microsoft keys waiting from Prime Gaming are redeemed, each only when its store is in this run, and Legacy Games codes when `legacy` is in this run and `LG_EMAIL` and `LG_PASSWORD` are set. Then come the sites with no module of their own (Itch.io, Fanatical, IndieGala, Alienware Arena), all in one browser window. Last, any Steam key a Fanatical giveaway handed out is activated on your Steam account, when Steam is in this run. Between the stores and after the summary, the run also checks for captchas it kept for you, or waits for you to open noVNC to solve them (see `EG_CAPTCHA_ASK`).
>
> So `STORES=steam` also claims the Steam giveaways GamerPower lists, and `STORES=prime` sends GamerPower no request at all, because nothing in that run could use it.
>
> Without `steam` in `STORES`, a Fanatical key stays in your Fanatical library and the summary says so. IndieGala adds its games to your IndieGala library.


---

## Architecture

```
free-games-claimer-remaster/
├── main.py                 # Entry point + scheduler + CLI + run summary
├── docker-compose.yml      # Container configuration
├── Dockerfile              # Debian trixie-slim + Chrome/Chromium + TurboVNC + noVNC
├── docker-entrypoint.sh    # Starts the virtual display, VNC and the bot
├── start-vnc.sh            # Starts the virtual screen and noVNC, again if the screen dies
├── requirements.txt        # Python dependencies
├── CHANGELOG.md            # What changed in every release
├── MODIFICATIONS.md        # Codebase overhaul technical reference
├── WINDOWS_BEGINNER_GUIDE.md
├── .env                    # Your local configuration (gitignored)
├── .env.example            # Configuration template
├── data/                   # Everything the bot keeps (Docker volume)
│   ├── fgc.db              # SQLite database of what was already claimed
│   ├── browser/<store>/    # One persistent Chrome profile per store
│   ├── screenshots/<store>/# Screenshots of failures, human checks and (with DEBUG) some claims
│   ├── human_checks.json   # Every human check of the last two weeks
│   ├── browser_timezone.json # The time zone looked up for your IP address
│   ├── update_check.json   # When GitHub was last asked for a newer release
│   ├── gamerpower_routes.json # Which store each GamerPower link belongs to
│   ├── prime-gaming.json   # Prime Gaming games and their codes
│   ├── ae_coin_api.json    # AliExpress check-in answers of the last run
│   └── TurboVNC.log        # What the virtual screen printed
├── src/
│   ├── version.py          # Version string
│   ├── core/               # Shared engine components
│   │   ├── claimer.py      # BaseClaimer: browser launch, login waits, notifications
│   │   ├── config.py       # Typed configuration loader (.env → Python)
│   │   ├── browser_timezone.py # The browser's time zone, matched to your IP address
│   │   ├── database.py     # SQLAlchemy models & SQLite engine
│   │   ├── display.py      # The virtual screen the browser draws on, and bringing it back
│   │   ├── notifier.py     # Modular Discord/Apprise webhooks
│   │   ├── redact.py       # Masks passwords, sign-in names, tokens and game codes in the log
│   │   ├── selection.py    # Which stores this run covers (GamerPower reads it)
│   │   ├── human_checks.py # Counts the human checks each store showed, kept two weeks in data/human_checks.json
│   │   ├── run_state.py    # What this run learned: which store is waiting for you
│   │   ├── updates.py      # Tells you when a newer release is published
│   │   └── url_security.py # Hostname checks for redirects (never substring matching)
│   └── stores/             # Store-specific claiming modules
│       ├── epic.py         # Epic Games Store
│       ├── prime.py        # Amazon Prime Gaming
│       ├── gog.py          # GOG (+ GOG code redemption from Prime)
│       ├── steam.py        # Steam (store search for 100% off, activating keys from Fanatical)
│       ├── epic_fab.py     # Fab limited-time free assets (shares Epic's session)
│       ├── unity.py        # Unity Asset Store weekly free asset
│       ├── ubisoft.py      # Ubisoft giveaways (ubisoft.com/games/free)
│       ├── microsoft.py    # Microsoft Store: Prime codes and paid games while free
│       ├── ms_rewards.py   # Microsoft Rewards: daily set, Bing searches, claiming points (opt-in)
│       ├── legacygames.py  # Legacy Games: redeeming the codes from Prime
│       ├── aliexpress.py   # AliExpress check-in & coin collecting
│       ├── epic_mobile.py  # Epic's weekly free Android/iOS game (detection only)
│       └── gamerpower.py   # GamerPower API (Fanatical, Itch.io, IndieGala, Alienware)
└── tests/                  # Fast unit tests for pure logic (no browser, no accounts)
```

### How it works

1. **Scheduler** (`main.py`) supports recurring interval timers (`SCHEDULER_HOURS`), fixed daily drop windows (`SCHEDULER_FIXED_TIMES`), combined execution, and initial startup checks (`RUN_ON_STARTUP`).
2. Each store module **starts its own browser** with an isolated profile, securely recalling session cookies (`--restore-last-session`). A first tab that arrives late does not take the store down, and each profile is marked as cleanly closed before every start. Some stores share a login: Fab rides Epic's profile, the four GamerPower sites (Itch.io, Fanatical, IndieGala, Alienware Arena) share one profile called `base`, Microsoft Rewards uses the Microsoft Store's profile when it is the same account (otherwise its own `msrewards`), and a GamerPower find is claimed inside the session the store already opened, rather than in a second browser of its own.
3. **Login detection** checks the page or the store's own account API (GOG asks its account API), not just cookies/DB.
4. **Fingerprint** is the one nodriver's patched Chrome produces by itself, because a hand-written desktop spoof did not match the real container and started summoning captchas (see CHANGELOG 1.4). Only AliExpress overrides it, injecting one coherent real-device Android fingerprint (`browserforge`) over the Chrome DevTools Protocol. The browser's time zone and the fonts in the image also shape what sites see.
5. **Game discovery** prefers each store's own data over scraping the page: Epic's promotions API, Ubisoft's embedded news feed, Fab's free-content blade, the GamerPower API, and Steam's own search for items at 100% off.
6. **Store selection** (`STORES`) is published to the run, so a GamerPower find is only claimed when this run includes the store it belongs to.
7. **A claim counts only when the store agrees.** After the checkout the bot reads the product page or the account's own list back (Epic, Fab, Steam, Microsoft, Unity, Ubisoft, Itch.io, IndieGala, Fanatical) and reports `claimed` only then, otherwise it says so instead of guessing. `fgc.db` (SQLite) remembers the outcome so runs do not trip over each other.
8. **Notifications** are sent according to the toggles in your `.env`, and a daily update check tells you when a newer release is out (`NOTIFY_UPDATES`).
9. **Two-factor sign-in follows one rule everywhere.** With an authenticator secret set (`*_OTP_KEY`) the bot sends two codes, the second one different from the first because a refused code stays refused inside its 30-second window. Then a recovery code from `*_OTP_CODES` if you gave it any, and only then the screen is left to you over VNC.
10. **Your settings are read back to you at startup.** Anything in `.env` the bot does not understand is named in the log instead of being ignored, and account names are masked (`p***@gmail.com`), like passwords, tokens, game codes and a public noVNC address, so a log is safe to share.

---

## Notifications

Both Discord and Apprise can be configured simultaneously, notifications are sent to ALL configured services at the same time.

- **Discord**: Set `DISCORD_WEBHOOK` in `.env`.
- **Apprise (Telegram, Slack, Email, ntfy, etc.)**: Set `NOTIFY` in `.env`. You can provide multiple URLs separated by commas (e.g. `NOTIFY=ntfy://topic, tgram://token/id`).
- **Long summaries**: a summary too long for one message arrives in several, cut between lines: up to 2000 characters each on Discord and 4000 through Apprise, under Telegram's limit.
- **Fine-Tune Filtering**: Use `NOTIFY_SUMMARY=false`, `NOTIFY_ERRORS=false`, etc., to silence specific notification subsets across all services globally.
- **What you are told about**: the summary after each run, including a "Waiting for you in noVNC" part for captchas the bot keeps for you; a captcha that needs you (`NOTIFY_LOGIN_REQUEST`); how a noVNC meeting went; "noVNC has no password" at start when its address is public and `VNC_PASSWORD` is empty; a store that "ran longer than … minutes and was stopped" (`NOTIFY_ERRORS`); and a newer release (`NOTIFY_UPDATES`).
- **Testing**: Set `NOTIFY_TEST=true` to receive a test notification whenever the container starts.

---

## Troubleshooting

| Issue | Solution |
|---|---|
| Store not logging in | Open VNC (`http://localhost:7080`) and login manually. Your session is kept after the first login. |
| Steam game not detected | Check that it shows at -100% in [Steam's search for free items on sale](https://store.steampowered.com/search/?maxprice=free&specials=1). An item Steam keeps out of its search is found only when GamerPower lists it. |
| GamerPower missing games | Itch.io, IndieGala, Fanatical and Alienware Arena run only when you name them (the first three claim with your account there, Alienware Arena giveaways are only passed on to you): `STORES=...,itchio,indiegala`. Giveaways for Steam, Epic, GOG or the Microsoft Store (Xbox) are claimed by those stores themselves, so they are skipped when the store is not in your `STORES` list. |
| Unity coupon not applied | Unity blocks the coupon while its checkout form is incomplete, see [Unity, one-time setup](#unity-one-time-setup). Two other reasons it stops on purpose: a checkout rendered in a language other than English, and `UNITY_ACCEPT_TOS=false`, which halts right before the EULA. |
| AliExpress coins not collected | The coin page often loads as an empty shell. The bot walks back to it up to `AE_PAGE_RETRIES` times (four by default), then reports it and moves on. It has been seen working again on a later run; collect in the mobile app if it persists. |
| Epic captcha | Epic asks now and then, sometimes inside the checkout window after "Add to library". A check that passes by itself (Cloudflare) is waited out; a picture puzzle leaves the game for the next run, which usually passes without one, and the summary says so. A game whose giveaway ends before that run, or any game with `EG_CAPTCHA_ASK=true`, asks you over VNC; if nobody solves it, with the built-in scheduler it waits for you to open noVNC until the time the notification gives, and the bot opens its checkout again once you connect. The EU right-of-withdrawal step is accepted for you. |
| A store asks for captchas too often | The log ends each run with "Human checks in the last 7 days" per store, and `data/human_checks.json` lists the last two weeks of every check the bot meets, sign-in captchas, AliExpress's slider and Bing's check included: when, which store, a puzzle or a check that passes by itself, and the page, with no account data. Attach that file to an issue. |
| False positive claims | Set `RESET_DB_GAMES=true` in your `.env`, reboot the container, and the bot will forget the last 7 days of claims, allowing the logic to try claiming them again. Set it back to `false` afterwards, or it does this at every start. |
| Setting seems to be ignored | The bot names every setting it does not read at startup, for example an invented `STEAM_ENABLE`, and every value that cannot mean what it says, for example `DRYRUN=maybe`, which counts as false. Passwords, e-mail addresses and webhook URLs are masked in that message. |
| A store was skipped, saying it waited for you | It asked you for something in the browser (a sign-in, a code, a captcha) and nobody answered within `VNC_LOGIN_TIMEOUT`. That store stops asking for the rest of the run, so it does not sit on a login screen for hours or keep re-opening it; after a captcha nobody solved, Epic and Fab open nothing more in that run, except, with the built-in scheduler, a game on its last run, and a game kept for you waits until you open noVNC (see `EG_CAPTCHA_ASK`). With `EG_CAPTCHA_ASK` off, a picture puzzle in Epic's checkout does the same without asking you first, except for a game whose giveaway ends before the next run, which still asks you. The summary's "Needed you" line says why and how many games, keys or codes wait for the next run. Raise the timeout, or let the next run pick it up. |
| "Waiting for you in noVNC until …" | An Epic or Fab captcha you were asked about was not solved in time, so the bot keeps that checkout for you. Open noVNC before that time (reload it if it is already open) and the bot opens the checkout again with a fresh puzzle. If you do not come, the game waits for the next run, or the bot tries once more by itself when the giveaway ends first. The run ends before the next one starts. The log says "Nobody came for '…' in noVNC" or "Trying once more by itself". |
| A store "ran longer than … minutes and was stopped" | A page in that store never finished. Each store gets an hour plus four times `VNC_LOGIN_TIMEOUT` (72 minutes by default), then the bot stops it, closes its browser and, with `NOTIFY_ERRORS`, sends you a notification; the other stores and the next run go on as usual. If it happens at every run, open an issue with the log. |
| "Bing asked for a check after N search(es)" | Bing showed a check, so Microsoft Rewards ends that day's searches and tries again tomorrow. If the log says Microsoft shows its bot warning on the dashboard, the bot does nothing else on that account for the run except claim the points already earned. |
| Container crashes on start | Check logs: `docker compose logs app --tail=50`. A clean restart purges `.X1-lock` bugs. |
| Browser will not start and VNC shows nothing | The virtual screen the browser draws on has died, which stops every store and leaves the noVNC page blank behind it. The bot starts that screen again by itself and carries on. When it cannot, it says so in one line and `data/TurboVNC.log` holds the reason. |

### Running on a NAS (TrueNAS Scale, Unraid, Synology)

The container runs Chrome on a screen of its own and expects to be root inside that container, which is what Docker does by default. NAS app templates often change it, so three things are worth checking:

- **Which user the app runs as.** The first line of the log says it: `Running as root(0), data folder is writable`. If the template sets a user for the container itself, the bot can neither save your sessions nor start the screen, which arrives as two faults that look unrelated. To run as your own user, leave the container as root and set `PUID` and `PGID` instead (on TrueNAS the apps user is usually `568`).
- **The memory limit.** Chrome dies the moment memory runs short, and from the outside that looks like a browser which never starts. Give the container 2 GB or more.
- **The port.** noVNC listens on `NOVNC_PORT` (7080 by default) both inside the container and on the NAS. If that port is taken there, set `NOVNC_PORT` to a free one.

### Hardening the container

The bot also runs with a read-only filesystem, as your own user and with every capability dropped except the three it needs at start to switch to that user. Add this to the `app` service in `docker-compose.yml` and set `PUID` and `PGID` in `.env`:

```yaml
    read_only: true
    tmpfs:
      - /tmp
    security_opt:
      - no-new-privileges=true
    cap_drop:
      - ALL
    cap_add:
      - CHOWN
      - SETUID
      - SETGID
```

Only the `fgc_data` volume and `/tmp`, which lives in memory, are written to, so your sign-ins survive restarts as before. Without `PUID` you can leave out `cap_add` as well.

### Sessions, profiles and the data volume

The bot keeps three main things outside the container: one browser profile per store, which holds your logins, `fgc.db`, which records what has already been claimed, and `used_*_codes.txt`, the list of recovery codes already spent. Small files sit beside them: the screenshots, `human_checks.json`, `browser_timezone.json`, `update_check.json`, `gamerpower_routes.json`, `prime-gaming.json`, `ae_coin_api.json` and `TurboVNC.log`. All of it lives in the `fgc_data` volume. **The program itself is not in there**, it comes with the image, so deleting the volume never updates anything. It only makes you sign in again.

Itch.io, Fanatical, IndieGala and Alienware Arena share one profile called `base`, because one browser claims them all in a row. Fab rides Epic's profile for the same reason, and Microsoft Rewards uses the Microsoft Store's profile when it is the same account, otherwise its own `msrewards` one.

See what is stored:

```bash
docker compose run --rm --entrypoint bash app -c "ls /fgc/data/browser"
```

Reset one store, Epic in this example, when that store keeps failing in a way that looks like leftover state: it will not sign in although the session should be valid, or its browser will not start at all. There is nothing that wears out, so this is a repair, not maintenance, and it is not worth doing on a schedule:

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

Keep this one for when you are truly stuck. You lose every saved login and the record of what was already claimed, so the bot walks back through titles it had marked as done. Epic deserves a warning of its own: it judges how established a browser profile looks, so a brand new profile is exactly when it starts asking for captchas. Keep that profile if you can.

### Something is not working, what to send us

The log already carries the bot's diagnostic detail, because `DEBUG` is on by default. With `DEBUG=false` it shows only what you act on: which store is running, who is signed in, what was found and claimed, plus every warning and error.

1. **Check you are on the newest code first**, the fix may already exist. `docker logs fgc-remaster | head -20` prints the version in the banner. To try the development build, set `FGC_TAG=dev` in `.env`, then `docker compose pull` and `docker compose up -d`.
2. If your `.env` has `DEBUG=false`, remove that line and restart (`docker compose up -d`), then reproduce the problem.
3. Collect the log. Go to the folder that holds your `docker-compose.yml` and run `docker logs fgc-remaster --tail 500 > fgc.log.txt` (with your container's name if you changed it, `docker ps` shows it). **The file appears in that folder, next to `docker-compose.yml`**, and you drag it into the GitHub comment box. Pasting the terminal output of the command itself sends us nothing, the log is inside the file. Prefer pasting? Run `docker logs fgc-remaster --tail 100` and put the output between triple backticks. Your passwords, two-factor secrets and notification tokens show as `***`, e-mail addresses as `p***@gmail.com`, other account names (such as your first name on Amazon) by their first letter, and game codes by their first five characters, so either way it should be safe to share, but please have a final look at it before sending.
4. **Keep that file, then try it clean.** Leftover state explains a surprising share of these:
   ```bash
   docker compose down -v     # stops the bot and throws away logins and claim history
   docker compose pull        # takes the newest image
   docker compose up -d       # starts fresh, sign in again through VNC
   ```
   This is step two, never step one: `-v` deletes the volume, so every store asks you to sign in again and the bot re-checks titles it had already claimed. If the problem is gone, that was it. If it comes back, make a second log the same way as above and send **both**, the one from before and the one from after. Two logs of the same fault from two clean starts say far more than one.
5. Look in the `data/` folder for what the bot saw. With the default `docker-compose.yml` it lives in Docker's `fgc_data` volume, and `docker cp fgc-remaster:/fgc/data/screenshots ./screenshots` copies the screenshots next to your `docker-compose.yml`:
   - `data/screenshots/<store>/`, screenshots taken at every failure and at every human check, and after a claim on Steam, Prime Gaming and the GamerPower sites (those share `screenshots/gamerpower/`), named with the date and time and kept 90 days (with `DEBUG=true`, the default). A Prime Gaming claim or a code redemption shows the code and the account on screen, so look before you attach one,
   - `data/ae_coin_api.json`, raw AliExpress check-in responses (streak, coins),
   - `data/TurboVNC.log`, everything the virtual screen printed, including why it refused to start,
   - `data/*_fail.html`, page snapshots from failed AliExpress logins and check-ins (only AliExpress writes these).
6. Watch it live if it is still running: open `http://localhost:7080` (noVNC) and take over the browser.

A good bug report is: what you expected, what happened, the `DEBUG=true` log around the failure, and the matching screenshot. `DEBUG=true` covers what the bot itself did; only add `DEBUG_LIBS=true` if you are asked for the raw network or browser traffic, because that turns one run into tens of thousands of lines. Please switch `DEBUG_LIBS` off again once the problem is solved. The [bug report form](https://github.com/P-Adamiec/Free-Games-Claimer-Remaster/issues/new?template=bug_report.yml) asks for exactly these.

---

## Support the project

This bot started as one of my personal automations for everyday tasks, and it is free for everyone to use. I put a lot of my own time into developing it, with the support of paid AI tools. If you like the project, your support helps cover those costs and the time it takes to keep it going.

<p align="center">
  <a href="https://www.buymeacoffee.com/padamiec"><img alt="Support the project on Buy Me a Coffee" src=".github/support-button.svg" height="50" /></a>
</p>

> [!IMPORTANT]
> I read and answer every issue and message personally. This is a free project that I develop on my own in my spare time, so replies and new features can take a while. Features that depend on services I don't use myself need some hands-on testing before they ship, and detailed reports and ideas speed that up a lot. If it seems I haven't quite understood your report, please bear with me and explain it once more in a little more detail. My setup and the apps I use every day are probably different from yours, and after a long day I can miss a detail too. That is why you are testers of this project as much as users, and together we keep making it better.
>
> Opening an issue? Pick the form that fits, [a bug, a feature idea, a new store or something else](https://github.com/P-Adamiec/Free-Games-Claimer-Remaster/issues/new/choose). Each one asks for what I need to understand it.

---

## Disclaimer

This project is not affiliated with any of the stores it works with. You run the bot at your own risk, and I take no responsibility for anything that happens to your accounts, games, keys or rewards. Several stores forbid bots in their terms: [DISCLAIMER.md](./DISCLAIMER.md) quotes each one and says what it covers.

---

## Credits

Inspired by [vogler/free-games-claimer](https://github.com/vogler/free-games-claimer) – the original Node.js project. This remaster is a **completely independent rewrite** in Python, not a fork.

---

## License

[AGPL-3.0](./LICENSE)

---

## Analytics

[![Star History Chart](https://api.star-history.com/svg?repos=P-Adamiec/Free-Games-Claimer-Remaster&type=Date)](https://www.star-history.com/?repos=P-Adamiec%2FFree-Games-Claimer-Remaster&type=date&legend=bottom-right)

---

<p align="center">
<img alt="logo-fgc-remaster" src="https://github.com/user-attachments/assets/e5b53324-b85a-428b-b76d-addc9ed38a5b" width="256" />
</p>
