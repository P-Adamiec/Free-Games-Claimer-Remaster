"""Epic Games mobile giveaways – finds the weekly free Android/iOS games.

Detection only: the games themselves are claimed by ``epic.py`` on the very same
desktop product pages (store.epicgames.com/en-US/p/<slug>), so this module owns
no browser, no session and no login of its own. Cloudflare rejects plain HTTP
clients on this API, so the caller passes in a fetcher that runs inside the
already-open store page.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("fgc.epic")

# Epic's mobile storefront listing (the desktop free-games API does not include mobile giveaways).
DISCOVER_API = (
    "https://egs-platform-service.store.epicgames.com/api/v2/public/discover/home"
    "?count=10&country=US&locale=en&platform={platform}&start=0&store=EGS"
)

PRODUCT_URL = "https://store.epicgames.com/en-US/p/{slug}"

VALID_PLATFORMS = ("android", "ios")

# Shown in logs and notifications, Android and iOS are separate items of the same game.
PLATFORM_LABELS = {"android": "Android", "ios": "iOS"}


def _is_claimable_free(content: dict) -> bool:
    """True when the offer is currently a 0-price 'Claim', not the paid entry that follows the promo."""
    for entry in content.get("purchase") or []:
        if not isinstance(entry, dict):
            continue
        price = (entry.get("price") or {}).get("decimalPrice")
        if entry.get("purchaseType") == "Claim" and price == 0:
            return True
    return False


def parse_free_games(data: dict, platform: str = "") -> list[dict]:
    """Pull the free-game section out of one discover-home response."""
    label = PLATFORM_LABELS.get(platform, "")
    games: list[dict] = []
    for section in (data or {}).get("data") or []:
        if section.get("type") != "freeGame":
            continue
        for offer in section.get("offers") or []:
            content = offer.get("content") or {}
            slug = (content.get("mapping") or {}).get("slug")
            if not slug:
                continue
            if not _is_claimable_free(content):
                logger.debug("Epic mobile: skipping '%s', no active free Claim offer", slug)
                continue
            title = content.get("title") or "Unknown"
            games.append({
                "title": f"{title} ({label})" if label else title,
                "url": PRODUCT_URL.format(slug=slug),
                "platform": platform,
                "label": label,
            })
    return games


async def fetch_mobile_free_games(fetch_json, platforms: list[str] | None = None) -> list[dict]:
    """Find Epic's currently free mobile games. Never raises, returns [] on failure.

    ``fetch_json(url)`` must perform the request from inside the Epic store page.
    """
    # An explicit empty list means "no platforms", only None falls back to all of them.
    requested = list(VALID_PLATFORMS) if platforms is None else platforms
    wanted = [p for p in requested if p in VALID_PLATFORMS]
    if not wanted:
        return []

    free_games: list[dict] = []
    seen: set[str] = set()
    for platform in wanted:
        try:
            data = await fetch_json(DISCOVER_API.format(platform=platform))
            found = parse_free_games(data, platform)
        except Exception as exc:
            logger.warning("Epic mobile (%s) detection failed: %s", platform, exc)
            continue
        logger.debug("Epic mobile (%s): %s", platform, [g["url"] for g in found] or "no free game")
        for game in found:
            if game["url"] in seen:
                continue
            seen.add(game["url"])
            free_games.append(game)
    return free_games
