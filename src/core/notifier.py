"""Notifications – sends you messages when games are claimed or errors occur.

Supports two notification systems:
  - Discord webhooks (set DISCORD_WEBHOOK in your .env file)
  - Apprise (supports Telegram, Slack, Email, ntfy, and 80+ other services)

If both DISCORD_WEBHOOK and NOTIFY are configured, notifications are sent to
BOTH services in parallel. If neither is set, notifications are silently
skipped (the bot still works fine).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
import apprise

from src.core.config import cfg

logger = logging.getLogger("fgc.notifier")


async def send_discord(
    message: str,
    *,
    screenshot_path: Path | None = None,
    username: str = "Free Games Claimer",
) -> None:
    """Send a message (and optional screenshot) to a Discord webhook."""
    webhook_url = cfg.discord_webhook
    if not webhook_url:
        logger.debug("DISCORD_WEBHOOK not set – skipping Discord notification.")
        return

    # Discord enforces a 2000-character limit per message.
    # Split long messages into chunks so nothing gets dropped.
    MAX_LEN = 2000
    chunks = []
    if len(message) <= MAX_LEN:
        chunks = [message]
    else:
        # Split on newline boundaries to keep formatting intact
        current = ""
        for line in message.split("\n"):
            # +1 accounts for the newline we'll re-add
            if len(current) + len(line) + 1 > MAX_LEN:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)

    async with httpx.AsyncClient(timeout=30) as client:
        for i, chunk in enumerate(chunks):
            data = {"content": chunk, "username": username}
            # Attach the screenshot only to the first chunk
            if i == 0 and screenshot_path and screenshot_path.exists():
                files = {"file": (screenshot_path.name, screenshot_path.read_bytes(), "image/png")}
                resp = await client.post(webhook_url, data=data, files=files)
            else:
                resp = await client.post(webhook_url, json=data)

            if resp.status_code not in (200, 204):
                logger.warning("Discord webhook returned %s: %s", resp.status_code, resp.text)
            else:
                logger.info("Discord notification sent (%d/%d).", i + 1, len(chunks))


async def send_apprise(message: str, *, title: str | None = None) -> None:
    """Send a notification via any Apprise-supported service."""
    notify_url = cfg.notify_url
    if not notify_url:
        logger.debug("NOTIFY not set – skipping Apprise notification.")
        return

    ap = apprise.Apprise()
    ap.add(notify_url)

    # apprise is sync – run in executor to avoid blocking the loop
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: ap.notify(body=message, title=title or "Free Games Claimer"),
    )
    # debug, not info: apprise already logs each target — avoids a duplicate-looking line.
    logger.debug("Apprise notification sent.")


async def notify(
    message: str,
    *,
    screenshot_path: Path | None = None,
    title: str | None = None,
) -> None:
    """Unified notification dispatcher — sends to ALL configured services in parallel."""
    tasks = []

    if cfg.discord_webhook:
        tasks.append(send_discord(message, screenshot_path=screenshot_path))
    if cfg.notify_url:
        tasks.append(send_apprise(message, title=title))

    if not tasks:
        logger.debug("No notification service configured.")
        return

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.exception("Failed to send notification", exc_info=result)


def format_game_list(games: list[dict]) -> str:
    """Format a list of ``{title, url, status}`` dicts into a readable string."""
    lines: list[str] = []
    for g in games:
        url = g.get("url", "")
        title = g.get("title", "Unknown")
        status = g.get("status", "?")
        lines.append(f"• **[{title}]({url})** — {status}")
    return "\n".join(lines)

