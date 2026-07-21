"""Free Games Claimer Remaster – main entry point.

This is the central "brain" of the application. When the Docker container starts,
this file is the first thing that runs. Here is what it does:

  1. Prints a startup banner with the version number and author.
  2. Initialises the SQLite database (creates tables if they don't exist).
  3. Starts a scheduler that automatically runs the claiming process every X hours.
  4. On each run, it goes through each enabled store (Steam, Epic, Prime, GOG)
     and tries to claim any free games available.
  5. After all stores are done, it checks if there are any GOG codes from
     Prime Gaming that still need to be redeemed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.core.config import cfg
from src.core.database import init_db
from src.stores.aliexpress import claim_aliexpress
from src.stores.epic import claim_epic
from src.stores.gamerpower import claim_gamerpower
from src.stores.gog import claim_gog
from src.stores.prime import claim_prime
from src.stores.steam import claim_steam
from src.core.notifier import notify
from src.version import __version__, __author__, __repo__, __contributors__

# ---------------------------------------------------------------------------
# Logging – user-friendly by default, verbose only on errors
# ---------------------------------------------------------------------------
from rich.logging import RichHandler
from rich.markup import escape
from rich.console import Console

# This filter automatically adds the store name (e.g. "[Steam]", "[Epic]")
# in front of every log message, so you can easily tell which module is talking.
class StorePrefixFilter(logging.Filter):
    def filter(self, record):
        if record.name.startswith("fgc."):
            store = record.name.split(".")[-1]
            if store in ("epic", "steam", "gog", "prime", "aliexpress"):
                store_map = {"gog": "GOG", "epic": "Epic", "steam": "Steam", "prime": "Prime", "aliexpress": "AliExpress"}
                prefix = escape(f"[{store_map[store]}]")
                # Prepend to the message template
                record.msg = f"{prefix} {record.msg}"
        return True

handler = RichHandler(
    console=Console(width=500),
    rich_tracebacks=True,
    show_path=False,       # hide file:line references
    show_level=True,
    show_time=True,        # Re-enabled per user request
    markup=True,
)
handler.addFilter(StorePrefixFilter())

logging.basicConfig(
    level=logging.DEBUG if cfg.debug else logging.INFO,
    format="%(message)s",
    handlers=[handler],
)
logger = logging.getLogger("fgc")

# Silence verbose third-party loggers
logging.getLogger("nodriver").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("uc").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Store registry – canonical name → (display name, coroutine function)
# ---------------------------------------------------------------------------

# Registry of all available store claimers.
# Each entry maps a short name to a (display name, function) pair.
# When the scheduler runs, it loops through these and calls each function.
ALL_CLAIMERS: dict[str, tuple[str, object]] = {
    "steam":      ("Steam",        claim_steam),
    "epic":       ("Epic Games",   claim_epic),
    "prime":      ("Prime Gaming", claim_prime),
    "gog":        ("GOG",          claim_gog),
    "gamerpower": ("GamerPower",   claim_gamerpower),
    "aliexpress": ("AliExpress",   claim_aliexpress),
}

# Display name (e.g. "Prime Gaming") → canonical store key (e.g. "prime").
_DISPLAY_TO_KEY: dict[str, str] = {disp: key for key, (disp, _) in ALL_CLAIMERS.items()}


def _store_key(name: str) -> str:
    """Map a display name or key to the canonical store key."""
    return _DISPLAY_TO_KEY.get(name, (name or "").lower())


# Accepted aliases → canonical name
_ALIASES: dict[str, str] = {
    "steam":         "steam",
    "steam-games":   "steam",
    "epic":          "epic",
    "epic-games":    "epic",
    "epicgames":     "epic",
    "prime":         "prime",
    "prime-gaming":  "prime",
    "primegaming":   "prime",
    "amazon":        "prime",
    "gog":           "gog",
    "gamerpower":    "gamerpower",
    "gp":            "gamerpower",
    "aliexpress":    "aliexpress",
    "ae":            "aliexpress",
}

_FIXED_TIME_RE = re.compile(r"^\d{2}:\d{2}$")
_CLAIM_JOB_OPTIONS = {
    "max_instances": 1,
    "coalesce": True,
    "misfire_grace_time": 1800,
}
_claim_run_lock = asyncio.Lock()


def _parse_fixed_times(raw: str) -> list[tuple[int, int]]:
    """Parse SCHEDULER_FIXED_TIMES as comma-separated HH:MM values."""
    if not raw.strip():
        return []

    fixed_times: list[tuple[int, int]] = []
    invalid: list[str] = []
    seen: set[tuple[int, int]] = set()

    for value in (part.strip() for part in raw.split(",")):
        if not value:
            continue

        if not _FIXED_TIME_RE.fullmatch(value):
            invalid.append(value)
            continue

        hour, minute = (int(part) for part in value.split(":", 1))
        if hour > 23 or minute > 59:
            invalid.append(value)
            continue

        key = (hour, minute)
        if key in seen:
            continue

        seen.add(key)
        fixed_times.append(key)

    if invalid:
        logger.warning(
            "Ignoring invalid SCHEDULER_FIXED_TIMES value(s): %s. "
            "Use comma-separated HH:MM times, for example 07:30,17:05,21:30.",
            ", ".join(invalid),
        )

    return fixed_times


def _scheduler_timezone() -> ZoneInfo:
    """Return the configured scheduler timezone or fail with a clear message."""
    try:
        return ZoneInfo(cfg.scheduler_timezone)
    except ZoneInfoNotFoundError as exc:
        logger.error(
            "Invalid SCHEDULER_TIMEZONE '%s'. Use an IANA timezone name "
            "such as UTC, Europe/Berlin, America/New_York, or Asia/Tokyo.",
            cfg.scheduler_timezone,
        )
        raise SystemExit(2) from exc


def _resolve_stores(raw: list[str]) -> list[str]:
    """Resolve a list of user-provided store names to canonical keys."""
    resolved = []
    for name in raw:
        key = _ALIASES.get(name.lower().strip())
        if key is None:
            logger.warning("Unknown store '%s' – ignoring. Valid: %s",
                           name, ", ".join(ALL_CLAIMERS.keys()))
            continue
        if key not in resolved:
            resolved.append(key)
    return resolved


def _get_active_claimers() -> list[tuple[str, object]]:
    """Determine which claimers to run based on CLI args / STORES env var.

    Priority:
      1. CLI positional args  (e.g.  ``python main.py steam prime``)
      2. ``STORES`` env var   (e.g.  ``STORES=steam,prime``)
      3. All stores           (default)
    """
    # Collect positional args (skip flags like --once)
    cli_stores = [a for a in sys.argv[1:] if not a.startswith("-")]

    if cli_stores:
        selected = _resolve_stores(cli_stores)
    elif cfg.stores:
        selected = _resolve_stores([s for s in cfg.stores.split(",") if s.strip()])
    else:
        selected = ["steam", "epic", "prime", "gog", "aliexpress"]

    return [(ALL_CLAIMERS[k][0], ALL_CLAIMERS[k][1]) for k in selected if k in ALL_CLAIMERS]


def _print_banner() -> None:
    """Print startup banner with version and author info."""
    commit = os.getenv("COMMIT", "")[:8]
    branch = os.getenv("BRANCH", "")
    build_info = f"  ({branch}@{commit})" if commit else ""

    W = 60  # inner width between ║ chars
    lines = [
        f"  Free Games Claimer Remaster  v{__version__}{build_info}",
        f"  by {__author__}",
        f"  {__repo__}",
    ]
    if __contributors__:
        contrib_str = ", ".join(__contributors__)
        lines.extend([
            "",
            f"  Special thanks to project contributors: {contrib_str}",
        ])
    print(f"\n╔{'═' * W}╗")
    for line in lines:
        print(f"║{line.ljust(W)}║")
    print(f"╚{'═' * W}╝\n")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run_claimers() -> None:
    """Run selected claimers sequentially (they each open their own browser)."""
    claimers = _get_active_claimers()

    if not claimers:
        logger.warning("No valid stores selected. Nothing to do.")
        return

    store_names = [name for name, _ in claimers]
    logger.info("🎮 Starting claiming run… %s", ", ".join(store_names))

    aggregated_results = []

    for name, func in claimers:
        try:
            res = await func()
            if isinstance(res, dict) and res.get("games"):
                aggregated_results.append(res)
        except Exception:
            logger.exception("✗ %s crashed", name)
            if cfg.store_notify_enabled(_store_key(name)):
                await notify(f"{name} claimer crashed with an unhandled exception. Check logs.")

    # After standard claimers finish, check for pending GOG codes from Prime Gaming.
    # Only run if there are actually codes with status="claimed" waiting,
    # or if GOG_FORCE_REDEEM is explicitly enabled.
    if "GOG" not in store_names:
        logger.debug("Skipping pending GOG codes redemption as 'gog' is not in STORES.")
    else:
        try:
            from src.core.database import async_session, ClaimedGame
            from sqlalchemy import select
            
            # Quick check: are there any pending GOG codes at all?
            has_pending = False
            async with async_session() as session:
                if cfg.gog_force_redeem:
                    has_pending = True  # Force mode: always check
                else:
                    stmt = select(ClaimedGame).where(
                        ClaimedGame.status == "claimed",
                        ClaimedGame.code.isnot(None),
                        ClaimedGame.code != ""
                    ).limit(1)
                    result = await session.execute(stmt)
                    has_pending = result.scalars().first() is not None
            
            if has_pending:
                from src.stores.gog import GOGClaimer
                gog = GOGClaimer()
                await gog.redeem_pending_codes()
                if gog.notify_games:
                    gog_entry = next((e for e in aggregated_results if e["store"] == "GOG"), None)
                    if gog_entry:
                        gog_entry["games"].extend(gog.notify_games)
                    else:
                        aggregated_results.append({"store": "GOG", "user": gog.user, "games": gog.notify_games})
            else:
                logger.debug("No pending GOG codes to redeem.")
        except Exception:
            logger.exception("Failed to run post-claim GOG code redemption")

    # Final Summary Notification
    if cfg.notify_summary and aggregated_results:
        from src.core.notifier import format_game_list
        msg_parts = []
        for result in aggregated_results:
            # Skip stores whose notifications are silenced (NOTIFY_SKIP_STORES).
            if not cfg.store_notify_enabled(_store_key(result.get("store", ""))):
                continue
            # Filter out games that were "existed" or "already redeemed", unless it's a dry run
            relevant_games = [
                g for g in result["games"]
                if "status" in g 
                and "exist" not in g["status"].lower() 
                and "already" not in g["status"].lower()
                and ("skip" not in g["status"].lower() or "dry run" in g["status"].lower())
            ]
            
            if not relevant_games:
                continue
                
            header = f"**{result['store']}** ({result['user']}):" if result.get('user') else f"**{result['store']}**:"
            msg_parts.append(f"{header}\n{format_game_list(relevant_games)}")
            
        if msg_parts:
            final_msg = "\n\n".join(msg_parts)
            if cfg.dryrun:
                final_msg = "🛑 **DRY RUN SUMMARY — Games Remaining to be Claimed:**\n\n" + final_msg
            await notify(final_msg)

    logger.info("✔ Claiming run complete.")


async def run_claimers_scheduled() -> None:
    """Run claimers from scheduler jobs without overlapping executions."""
    if _claim_run_lock.locked():
        logger.warning("A claiming run is already in progress; skipping this scheduled trigger.")
        return

    async with _claim_run_lock:
        await run_claimers()


async def main() -> None:
    """Initialise DB and either run once or start the scheduler."""
    _print_banner()
    await init_db()
    logger.info("Database ready.")

    if cfg.reset_db_games:
        try:
            from datetime import datetime, timedelta, timezone
            from src.core.database import async_session, ClaimedGame
            from sqlalchemy import delete
            
            seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
            
            async with async_session() as session:
                stmt = delete(ClaimedGame).where(ClaimedGame.created_at >= seven_days_ago)
                res = await session.execute(stmt)
                if res.rowcount > 0:
                    logger.info("Reset %d entry(s) from the last 7 days from history.", res.rowcount)
                else:
                    logger.debug("DB reset requested, but no entries found from the last 7 days.")
                await session.commit()
        except Exception as e:
            logger.error("Failed to reset DB games: %s", e)

    # Send a test notification if NOTIFY_TEST=true (for verifying notification setup)
    if cfg.notify_test:
        logger.info("🔔 NOTIFY_TEST=true — sending test notification...")
        services = ", ".join(filter(None, [
            "Discord" if cfg.discord_webhook else None,
            "Apprise" if cfg.notify_url else None,
        ])) or "⚠️ None configured"
        test_msg = (
            "🔔 **Free Games Claimer — Test Notification**\n\n"
            "✅ If you see this message, your notification setup is working correctly!\n\n"
            f"**Version:** v{__version__}\n"
            f"**Services:** {services}"
        )
        await notify(test_msg)
        logger.info("✅ Test notification dispatched! Check your configured services. "
                     "Set NOTIFY_TEST=0 in your .env to disable this on future restarts.")

    # If --once flag is set, run a single pass and exit
    if "--once" in sys.argv:
        await run_claimers()
        return

    # Otherwise start the scheduler
    fixed_times = _parse_fixed_times(cfg.scheduler_fixed_times)
    fixed_timezone = _scheduler_timezone() if fixed_times else None

    scheduler = AsyncIOScheduler(job_defaults=_CLAIM_JOB_OPTIONS)
    if cfg.scheduler_hours > 0:
        scheduler.add_job(
            run_claimers_scheduled,
            trigger=CronTrigger(hour=f"*/{cfg.scheduler_hours}"),  # every X hours
            id="claim_all",
            name="Claim free games",
            replace_existing=True,
        )
    else:
        logger.info("Interval scheduler disabled because SCHEDULER_HOURS=%s.", cfg.scheduler_hours)

    for hour, minute in fixed_times:
        scheduler.add_job(
            run_claimers_scheduled,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=fixed_timezone),
            id=f"claim_fixed_{hour:02d}_{minute:02d}",
            name=f"Claim free games at {hour:02d}:{minute:02d}",
            replace_existing=True,
        )

    # Delay slightly to ensure TurboVNC/X11 is fully initialized BEFORE starting Chrome
    logger.info("Waiting for virtual display to initialize...")
    await asyncio.sleep(3)

    # Also run immediately on startup
    if cfg.run_on_startup:
        scheduler.add_job(
            run_claimers_scheduled,
            id="claim_all_startup",
            name="Initial claiming run",
            replace_existing=True,
        )
    else:
        logger.info("Initial claiming run disabled by RUN_ON_STARTUP=false.")

    scheduler.start()
    interval_text = (
        f"runs every {cfg.scheduler_hours} hours"
        if cfg.scheduler_hours > 0
        else "interval disabled"
    )
    fixed_text = (
        "fixed daily times: "
        + ", ".join(f"{hour:02d}:{minute:02d}" for hour, minute in fixed_times)
        + f" ({cfg.scheduler_timezone})"
        if fixed_times
        else "no fixed daily times configured"
    )
    logger.info("Scheduler active - %s; %s.", interval_text, fixed_text)

    try:
        # Keep the event loop alive
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down…")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
