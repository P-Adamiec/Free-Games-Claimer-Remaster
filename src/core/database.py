"""Database – keeps track of which games have already been claimed.

This file manages the SQLite database (stored as /fgc/data/fgc.db inside Docker).
Every time a game is successfully claimed, a record is saved here so the bot
knows not to try claiming it again on the next run.

The database stores: game title, which store it came from, who claimed it,
any redemption codes, and timestamps.
"""

from datetime import datetime, timezone

from sqlalchemy import String, DateTime, Text, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.core.config import cfg


# ---------------------------------------------------------------------------
# Engine & session factory
# ---------------------------------------------------------------------------

engine = create_async_engine(cfg.database_url, echo=cfg.debug)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """Shared declarative base for all models."""


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ClaimedGame(Base):
    """A single row in the database representing one claimed game.
    
    Each game is uniquely identified by (store + user + game_id).
    If a game already exists in the database, we skip it on the next run.
    """

    __tablename__ = "claimed_games"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    store: Mapped[str] = mapped_column(String(32), index=True, comment="epic, gog, prime, steam")
    user: Mapped[str] = mapped_column(String(128), index=True, comment="Account display name")
    game_id: Mapped[str] = mapped_column(String(256), index=True, comment="Store-specific game identifier")
    title: Mapped[str] = mapped_column(String(512))
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="unknown", comment="claimed, existed, failed, …")
    code: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="Redemption code (GOG / external)")
    extra: Mapped[str | None] = mapped_column(Text, nullable=True, comment="JSON blob for misc data")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<ClaimedGame store={self.store!r} title={self.title!r} status={self.status!r}>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Create all tables if they don't exist yet."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_or_create(
    session: AsyncSession,
    *,
    store: str,
    user: str,
    game_id: str,
    title: str,
    url: str | None = None,
    status: str = "unknown",
    code: str | None = None,
) -> tuple[ClaimedGame, bool]:
    """Return existing row or insert a new one.  Returns ``(obj, created)``."""
    from sqlalchemy import select

    stmt = select(ClaimedGame).where(
        ClaimedGame.store == store,
        ClaimedGame.user == user,
        ClaimedGame.game_id == game_id,
    )
    result = await session.execute(stmt)
    obj = result.scalar_one_or_none()
    if obj is not None:
        return obj, False

    obj = ClaimedGame(
        store=store,
        user=user,
        game_id=game_id,
        title=title,
        url=url,
        status=status,
        code=code,
    )
    session.add(obj)
    await session.flush()
    return obj, True
