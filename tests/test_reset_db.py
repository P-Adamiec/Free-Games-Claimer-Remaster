"""RESET_DB_GAMES forgets the last week's claims, but never a code or key still to redeem."""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import main
from src.core.database import Base, ClaimedGame


def _row(game_id, code=None, days_ago=1):
    return ClaimedGame(store="prime", user="u", game_id=game_id, title=game_id, status="claimed", code=code,
                       created_at=datetime.now(timezone.utc) - timedelta(days=days_ago))


def _reset(tmp_path, rows):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fgc.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add_all(rows)
            await session.commit()
        removed = await main.reset_recent_games(factory)
        async with factory() as session:
            left = sorted(r.game_id for r in (await session.execute(select(ClaimedGame))).scalars())
        await engine.dispose()
        return removed, left

    return asyncio.run(run())


def test_only_recent_rows_without_a_code_are_forgotten(tmp_path):
    removed, left = _reset(tmp_path, [
        _row("recent"), _row("recent-empty-code", code=""),
        _row("gog-code", code="ABCDE-FGHIJ-KLMNO"), _row("old", days_ago=30),
    ])
    assert removed == 2 and left == ["gog-code", "old"]


def test_a_steam_key_waiting_for_activation_survives(tmp_path):
    key = _row("fanatical-game", code="AAAAA-BBBBB-CCCCC")
    key.store, key.extra = "fanatical", '{"external_store": "steam"}'
    removed, left = _reset(tmp_path, [key])
    assert removed == 0 and left == ["fanatical-game"]
