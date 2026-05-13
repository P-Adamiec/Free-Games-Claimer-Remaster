"""Notifications – sends you messages when games are claimed or errors occur.

Supports two notification systems:
  - Discord webhooks (set DISCORD_WEBHOOK in your .env file)
  - Apprise (supports Telegram, Slack, Email, and 80+ other services)

Discord is tried first. If no Discord webhook is configured, it falls back to Apprise.
If neither is set, notifications are silently skipped (the bot still works fine).
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

    async with httpx.AsyncClient(timeout=30) as client:
        data = {"content": message, "username": username}
        files = None
        if screenshot_path and screenshot_path.exists():
            files = {"file": (screenshot_path.name, screenshot_path.read_bytes(), "image/png")}
            resp = await client.post(webhook_url, data=data, files=files)
        else:
            resp = await client.post(webhook_url, json=data)

        if resp.status_code not in (200, 204):
            logger.warning("Discord webhook returned %s: %s", resp.status_code, resp.text)
        else:
            logger.info("Discord notification sent.")


async def send_apprise(message: str, *, title: str | None = None) -> None:
    """Send a notification via any Apprise-supported service (fallback)."""
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
    logger.info("Apprise notification sent.")


async def notify(
    message: str,
    *,
    screenshot_path: Path | None = None,
    title: str | None = None,
) -> None:
    """Unified notification dispatcher – tries Discord first, then Apprise."""
    try:
        if cfg.discord_webhook:
            await send_discord(message, screenshot_path=screenshot_path)
        elif cfg.notify_url:
            await send_apprise(message, title=title)
        else:
            logger.debug("No notification service configured.")
    except Exception:
        logger.exception("Failed to send notification")


def format_game_list(games: list[dict]) -> str:
    """Format a list of ``{title, url, status}`` dicts into a readable string."""
    lines: list[str] = []
    for g in games:
        url = g.get("url", "")
        title = g.get("title", "Unknown")
        status = g.get("status", "?")
        lines.append(f"• **[{title}]({url})** — {status}")
    return "\n".join(lines)
