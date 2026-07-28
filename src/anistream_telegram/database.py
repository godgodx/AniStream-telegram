from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    delete,
    event,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from anistream.models import MAX_PREFETCHED_PLAYLIST_BYTES


def enable_sqlite_foreign_keys(
    dbapi_connection: Any,
    _connection_record: Any,
) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def utcnow() -> datetime:
    # Keep UTC naive consistently because SQLite drops timezone information.
    return datetime.now(UTC).replace(tzinfo=None)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Base(DeclarativeBase):
    pass


class AllowedUser(Base):
    __tablename__ = "allowed_users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class UserPreference(Base):
    __tablename__ = "user_preferences"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    autoplay_next: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class UserProviderPreference(Base):
    __tablename__ = "user_provider_preferences"

    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
    )
    provider_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class EphemeralSelection(Base):
    __tablename__ = "ephemeral_selections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)


class LaunchTicket(Base):
    __tablename__ = "launch_tickets"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)


class WebSession(Base):
    __tablename__ = "web_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class PlaybackSession(Base):
    __tablename__ = "playback_sessions"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    catalogue_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    episode: Mapped[int] = mapped_column(Integer, nullable=False)
    media_url: Mapped[str] = mapped_column(Text, nullable=False)
    media_headers: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    media_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)


class PlaybackManifest(Base):
    __tablename__ = "playback_manifests"

    playback_id: Mapped[str] = mapped_column(
        ForeignKey("playback_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)


class PreparedPlayback(Base):
    __tablename__ = "prepared_playbacks"

    playback_id: Mapped[str] = mapped_column(
        ForeignKey("playback_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    preferred_source_index: Mapped[int] = mapped_column(Integer, nullable=False)
    source_index: Mapped[int] = mapped_column(Integer, nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)


class ActivePlayback(Base):
    __tablename__ = "active_playbacks"

    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
    )
    identity_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    playback_id: Mapped[str] = mapped_column(
        ForeignKey("playback_sessions.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    episode: Mapped[int] = mapped_column(Integer, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class CastGrant(Base):
    __tablename__ = "cast_grants"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    playback_id: Mapped[str] = mapped_column(
        ForeignKey("playback_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)


class WatchState(Base):
    __tablename__ = "watch_states"
    __table_args__ = (
        UniqueConstraint(
            "telegram_user_id",
            "identity_hash",
            name="uq_watch_state_user_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    catalogue_url: Mapped[str] = mapped_column(Text, nullable=False)
    identity_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    catalogue_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    next_episode: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_played_episode: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="in_progress", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, nullable=False)


class EpisodeProgress(Base):
    __tablename__ = "episode_progress"
    __table_args__ = (
        UniqueConstraint(
            "watch_state_id",
            "episode",
            name="uq_episode_progress_state_episode",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watch_state_id: Mapped[int] = mapped_column(
        ForeignKey("watch_states.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    episode: Mapped[int] = mapped_column(Integer, nullable=False)
    position_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class EpisodeProgressCursor(Base):
    """Latest accepted client observation for one episode.

    Keeping ordering metadata separately avoids altering the existing
    episode_progress table on deployed PostgreSQL databases. create_all()
    creates this table during the normal application startup.
    """

    __tablename__ = "episode_progress_cursors"

    watch_state_id: Mapped[int] = mapped_column(
        ForeignKey("watch_states.id", ondelete="CASCADE"),
        primary_key=True,
    )
    episode: Mapped[int] = mapped_column(Integer, primary_key=True)
    observed_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class PlaybackProgressCursor(Base):
    __tablename__ = "playback_progress_cursors"

    playback_id: Mapped[str] = mapped_column(
        ForeignKey("playback_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    observed_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


SCHEMA_MIGRATIONS = (
    (1, "baseline schema"),
    (2, "ordered progress cursors"),
    (3, "active playback generations"),
    (4, "per-playback progress cursors"),
)
POSTGRES_MIGRATION_LOCK_ID = 4_712_958_475_264_321_857


def acquire_schema_migration_lock(connection: Any) -> None:
    """Serialize schema adoption across concurrently starting PostgreSQL replicas."""

    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            f"SELECT pg_advisory_xact_lock({POSTGRES_MIGRATION_LOCK_ID})"
        )


def apply_schema_migrations(connection: Any) -> None:
    """Apply ordered, additive migrations on SQLite and PostgreSQL.

    Version 1 adopts existing installations by creating any missing baseline
    tables. Later versions are intentionally explicit even though create_all()
    also makes a fresh database current in one pass.
    """

    acquire_schema_migration_lock(connection)
    SchemaMigration.__table__.create(connection, checkfirst=True)
    applied = set(
        connection.execute(select(SchemaMigration.version)).scalars()
    )
    for version, name in SCHEMA_MIGRATIONS:
        if version in applied:
            continue
        if version == 1:
            Base.metadata.create_all(connection)
        elif version == 2:
            EpisodeProgressCursor.__table__.create(
                connection,
                checkfirst=True,
            )
        elif version == 3:
            ActivePlayback.__table__.create(
                connection,
                checkfirst=True,
            )
        elif version == 4:
            PlaybackProgressCursor.__table__.create(
                connection,
                checkfirst=True,
            )
        else:  # pragma: no cover - guards future registry mistakes.
            raise RuntimeError(f"unknown schema migration {version}")
        connection.execute(
            insert(SchemaMigration).values(
                version=version,
                name=name,
                applied_at=utcnow(),
            )
        )


class Database:
    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        if url.startswith("sqlite"):
            event.listen(
                self.engine.sync_engine,
                "connect",
                enable_sqlite_foreign_keys,
            )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _insert_do_nothing(
        session: AsyncSession,
        model: type[Base],
        values: dict[str, Any],
        index_elements: tuple[str, ...],
    ):
        dialect = session.bind.dialect.name if session.bind is not None else ""
        if dialect == "postgresql":
            statement = postgresql_insert(model).values(**values)
        elif dialect == "sqlite":
            statement = sqlite_insert(model).values(**values)
        else:
            return insert(model).values(**values)
        return statement.on_conflict_do_nothing(
            index_elements=index_elements
        )

    async def initialize(self, bootstrap_users: tuple[int, ...] = ()) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(apply_schema_migrations)
        for user_id in bootstrap_users:
            await self.bootstrap_allowed_user(user_id)
        await self.cleanup()

    async def close(self) -> None:
        await self.engine.dispose()

    async def set_allowed(self, user_id: int, enabled: bool) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                self._insert_do_nothing(
                    session,
                    AllowedUser,
                    {
                        "telegram_user_id": user_id,
                        "enabled": bool(enabled),
                        "created_at": utcnow(),
                    },
                    ("telegram_user_id",),
                )
            )
            await session.execute(
                update(AllowedUser)
                .where(AllowedUser.telegram_user_id == user_id)
                .values(enabled=bool(enabled))
            )

    async def bootstrap_allowed_user(self, user_id: int) -> None:
        """Create an initial allow entry without overriding a persisted revocation."""
        async with self.sessions.begin() as session:
            await session.execute(
                self._insert_do_nothing(
                    session,
                    AllowedUser,
                    {
                        "telegram_user_id": user_id,
                        "enabled": True,
                        "created_at": utcnow(),
                    },
                    ("telegram_user_id",),
                )
            )

    async def is_allowed(self, user_id: int) -> bool:
        async with self.sessions() as session:
            item = await session.get(AllowedUser, user_id)
            return bool(item and item.enabled)

    async def list_allowed(self) -> list[int]:
        async with self.sessions() as session:
            result = await session.scalars(
                select(AllowedUser.telegram_user_id)
                .where(AllowedUser.enabled.is_(True))
                .order_by(AllowedUser.telegram_user_id)
            )
            return list(result)

    async def autoplay_enabled(self, user_id: int) -> bool:
        async with self.sessions() as session:
            preference = await session.get(UserPreference, user_id)
            return True if preference is None else bool(preference.autoplay_next)

    async def set_autoplay_enabled(self, user_id: int, enabled: bool) -> bool:
        async with self.sessions.begin() as session:
            now = utcnow()
            await session.execute(
                self._insert_do_nothing(
                    session,
                    UserPreference,
                    {
                        "telegram_user_id": user_id,
                        "autoplay_next": bool(enabled),
                        "updated_at": now,
                    },
                    ("telegram_user_id",),
                )
            )
            await session.execute(
                update(UserPreference)
                .where(UserPreference.telegram_user_id == user_id)
                .values(
                    autoplay_next=bool(enabled),
                    updated_at=now,
                )
            )
        return bool(enabled)

    async def toggle_autoplay(self, user_id: int) -> bool:
        async with self.sessions.begin() as session:
            await session.execute(
                self._insert_do_nothing(
                    session,
                    UserPreference,
                    {
                        "telegram_user_id": user_id,
                        "autoplay_next": True,
                        "updated_at": utcnow(),
                    },
                    ("telegram_user_id",),
                )
            )
            preference = await session.scalar(
                select(UserPreference)
                .where(UserPreference.telegram_user_id == user_id)
                .with_for_update()
            )
            if preference is None:  # pragma: no cover - insert/select invariant.
                raise RuntimeError("autoplay preference could not be created")
            preference.autoplay_next = not preference.autoplay_next
            preference.updated_at = utcnow()
            return bool(preference.autoplay_next)

    async def provider_states(
        self,
        user_id: int,
        provider_ids: tuple[str, ...],
    ) -> dict[str, bool]:
        normalized = tuple(dict.fromkeys(str(item) for item in provider_ids))
        if not normalized:
            return {}
        async with self.sessions() as session:
            result = await session.scalars(
                select(UserProviderPreference).where(
                    UserProviderPreference.telegram_user_id == user_id,
                    UserProviderPreference.provider_id.in_(normalized),
                )
            )
            saved = {item.provider_id: bool(item.enabled) for item in result}
        return {
            provider_id: saved.get(provider_id, True)
            for provider_id in normalized
        }

    async def enabled_provider_ids(
        self,
        user_id: int,
        provider_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        states = await self.provider_states(user_id, provider_ids)
        return tuple(
            provider_id
            for provider_id in provider_ids
            if states.get(provider_id, True)
        )

    async def toggle_provider_enabled(
        self,
        user_id: int,
        provider_id: str,
    ) -> bool:
        async with self.sessions.begin() as session:
            await session.execute(
                self._insert_do_nothing(
                    session,
                    UserProviderPreference,
                    {
                        "telegram_user_id": user_id,
                        "provider_id": provider_id,
                        "enabled": True,
                        "updated_at": utcnow(),
                    },
                    ("telegram_user_id", "provider_id"),
                )
            )
            preference = await session.scalar(
                select(UserProviderPreference)
                .where(
                    UserProviderPreference.telegram_user_id == user_id,
                    UserProviderPreference.provider_id == provider_id,
                )
                .with_for_update()
            )
            if preference is None:  # pragma: no cover - insert/select invariant.
                raise RuntimeError("provider preference could not be created")
            preference.enabled = not preference.enabled
            preference.updated_at = utcnow()
            return bool(preference.enabled)

    async def create_selection(
        self,
        user_id: int,
        kind: str,
        payload: dict[str, Any],
        *,
        ttl_seconds: int = 600,
    ) -> str:
        selection_id = secrets.token_urlsafe(9)
        async with self.sessions.begin() as session:
            session.add(
                EphemeralSelection(
                    id=selection_id,
                    telegram_user_id=user_id,
                    kind=kind,
                    payload=payload,
                    expires_at=utcnow() + timedelta(seconds=ttl_seconds),
                )
            )
        return selection_id

    async def get_selection(
        self,
        selection_id: str,
        user_id: int,
        *,
        kind: str | None = None,
    ) -> dict[str, Any] | None:
        async with self.sessions() as session:
            item = await session.get(EphemeralSelection, selection_id)
            if (
                item is None
                or item.telegram_user_id != user_id
                or item.expires_at <= utcnow()
                or (kind is not None and item.kind != kind)
            ):
                return None
            return dict(item.payload)

    async def update_selection_payload(
        self,
        selection_id: str,
        user_id: int,
        payload: dict[str, Any],
        *,
        kind: str,
    ) -> bool:
        async with self.sessions.begin() as session:
            item = await session.get(
                EphemeralSelection,
                selection_id,
                with_for_update=True,
            )
            if (
                item is None
                or item.telegram_user_id != user_id
                or item.expires_at <= utcnow()
                or item.kind != kind
            ):
                return False
            item.payload = payload
            return True

    async def create_launch_ticket(
        self,
        user_id: int,
        payload: dict[str, Any],
        *,
        ttl_seconds: int = 120,
    ) -> str:
        raw = secrets.token_urlsafe(32)
        async with self.sessions.begin() as session:
            session.add(
                LaunchTicket(
                    token_hash=token_hash(raw),
                    telegram_user_id=user_id,
                    payload=payload,
                    expires_at=utcnow() + timedelta(seconds=ttl_seconds),
                )
            )
        return raw

    async def exchange_launch_ticket(
        self,
        raw_token: str,
        user_id: int,
    ) -> dict[str, Any] | None:
        async with self.sessions.begin() as session:
            consumed_at = utcnow()
            payload = await session.scalar(
                update(LaunchTicket)
                .where(
                    LaunchTicket.token_hash == token_hash(raw_token),
                    LaunchTicket.telegram_user_id == user_id,
                    LaunchTicket.consumed_at.is_(None),
                    LaunchTicket.expires_at > consumed_at,
                )
                .values(consumed_at=consumed_at)
                .returning(LaunchTicket.payload)
            )
            if payload is None:
                return None
            return dict(payload)

    async def create_web_session(
        self,
        user_id: int,
        payload: dict[str, Any],
        *,
        ttl_seconds: int,
    ) -> tuple[str, str]:
        raw = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        async with self.sessions.begin() as session:
            session.add(
                WebSession(
                    token_hash=token_hash(raw),
                    telegram_user_id=user_id,
                    csrf_token=csrf,
                    payload=payload,
                    expires_at=utcnow() + timedelta(seconds=ttl_seconds),
                )
            )
        return raw, csrf

    async def get_web_session(self, raw_token: str) -> WebSession | None:
        if not raw_token:
            return None
        async with self.sessions() as session:
            item = await session.get(WebSession, token_hash(raw_token))
            if item is None or item.expires_at <= utcnow():
                return None
            allowed = await session.get(AllowedUser, item.telegram_user_id)
            if allowed is None or not allowed.enabled:
                return None
            return item

    async def get_media_playback(
        self,
        raw_token: str,
        playback_id: str,
    ) -> tuple[WebSession, PlaybackSession] | None:
        """Authorize one media request with a single joined SQL statement."""

        if not raw_token:
            return None
        now = utcnow()
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(WebSession, PlaybackSession)
                    .join(
                        AllowedUser,
                        AllowedUser.telegram_user_id
                        == WebSession.telegram_user_id,
                    )
                    .join(
                        PlaybackSession,
                        PlaybackSession.telegram_user_id
                        == WebSession.telegram_user_id,
                    )
                    .where(
                        WebSession.token_hash == token_hash(raw_token),
                        WebSession.expires_at > now,
                        AllowedUser.enabled.is_(True),
                        PlaybackSession.id == playback_id,
                        PlaybackSession.expires_at > now,
                    )
                )
            ).first()
            if row is None:
                return None
            return row[0], row[1]

    async def delete_web_session(self, raw_token: str) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                delete(WebSession).where(WebSession.token_hash == token_hash(raw_token))
            )

    async def update_web_session_payload(
        self,
        raw_token: str,
        user_id: int,
        payload: dict[str, Any],
    ) -> bool:
        if not raw_token:
            return False
        async with self.sessions.begin() as session:
            item = await session.get(WebSession, token_hash(raw_token), with_for_update=True)
            if (
                item is None
                or item.telegram_user_id != user_id
                or item.expires_at <= utcnow()
            ):
                return False
            item.payload = payload
            item.last_seen_at = utcnow()
            return True

    async def create_playback(
        self,
        user_id: int,
        catalogue_payload: dict[str, Any],
        episode: int,
        *,
        media_url: str,
        media_headers: dict[str, str],
        media_kind: str,
        source_name: str,
        ttl_seconds: int,
        prefetched_playlist: bytes = b"",
        prefetched_playlist_url: str = "",
        prepared: bool = False,
        preferred_source_index: int = 0,
        source_index: int = 0,
        source_count: int = 1,
    ) -> PlaybackSession:
        if len(prefetched_playlist) > MAX_PREFETCHED_PLAYLIST_BYTES:
            raise ValueError("prefetched playlist is too large")
        if bool(prefetched_playlist) != bool(prefetched_playlist_url):
            raise ValueError("prefetched playlist metadata is incomplete")
        expires_at = utcnow() + timedelta(seconds=ttl_seconds)
        item = PlaybackSession(
            id=secrets.token_urlsafe(18),
            telegram_user_id=user_id,
            catalogue_payload=catalogue_payload,
            episode=episode,
            media_url=media_url,
            media_headers=media_headers,
            media_kind=media_kind,
            source_name=source_name[:128],
            expires_at=expires_at,
        )
        async with self.sessions.begin() as session:
            session.add(item)
            # These tables deliberately use lightweight foreign-key columns
            # without ORM relationships. Materialize the parent first so
            # PostgreSQL cannot schedule either child INSERT ahead of it.
            await session.flush()
            if prefetched_playlist:
                session.add(
                    PlaybackManifest(
                        playback_id=item.id,
                        body=prefetched_playlist,
                        base_url=prefetched_playlist_url,
                        expires_at=expires_at,
                    )
                )
            if prepared:
                session.add(
                    PreparedPlayback(
                        playback_id=item.id,
                        telegram_user_id=user_id,
                        preferred_source_index=preferred_source_index,
                        source_index=source_index,
                        source_count=source_count,
                        expires_at=expires_at,
                    )
                )
            else:
                generation = await self._activate_playback_in_session(
                    session,
                    item,
                )
                setattr(item, "generation", generation)
        return item

    async def _activate_playback_in_session(
        self,
        session: AsyncSession,
        playback: PlaybackSession,
    ) -> int:
        try:
            _, _, identity = self.catalogue_identity(
                dict(playback.catalogue_payload)
            )
        except (KeyError, TypeError, ValueError):
            return 0
        now = utcnow()
        # Progress, remove, restart, and activation all serialize on the watch
        # state before touching ActivePlayback. Besides preventing lock-order
        # inversions, this lets an in-flight save from the previous player
        # commit before a replacement becomes active and reloads its position.
        await session.scalar(
            self._watch_state_for_update(
                playback.telegram_user_id,
                identity,
            )
        )
        active_insert = self._insert_do_nothing(
            session,
            ActivePlayback,
            {
                "telegram_user_id": playback.telegram_user_id,
                "identity_hash": identity,
                "playback_id": playback.id,
                "episode": playback.episode,
                "generation": 1,
                "updated_at": now,
            },
            ("telegram_user_id", "identity_hash"),
        ).returning(ActivePlayback.identity_hash)
        inserted = await session.scalar(active_insert)
        if inserted is not None:
            return 1
        active = await session.scalar(
            select(ActivePlayback)
            .where(
                ActivePlayback.telegram_user_id
                == playback.telegram_user_id,
                ActivePlayback.identity_hash == identity,
            )
            .with_for_update()
        )
        if active is None:  # pragma: no cover - insert/select invariant.
            raise RuntimeError("active playback could not be created")
        active.playback_id = playback.id
        active.episode = playback.episode
        active.generation += 1
        active.updated_at = now
        return active.generation

    async def get_playback(self, playback_id: str, user_id: int) -> PlaybackSession | None:
        async with self.sessions() as session:
            item = await session.get(PlaybackSession, playback_id)
            if (
                item is None
                or item.telegram_user_id != user_id
                or item.expires_at <= utcnow()
            ):
                return None
            return item

    async def consume_playback_manifest(
        self,
        playback_id: str,
        user_id: int,
    ) -> tuple[bytes, str] | None:
        now = utcnow()
        async with self.sessions.begin() as session:
            playback = await session.get(PlaybackSession, playback_id)
            if (
                playback is None
                or playback.telegram_user_id != user_id
                or playback.expires_at <= now
            ):
                return None
            manifest = await session.scalar(
                select(PlaybackManifest)
                .where(
                    PlaybackManifest.playback_id == playback_id,
                    PlaybackManifest.expires_at > now,
                )
                .with_for_update()
            )
            if manifest is None:
                return None
            body = bytes(manifest.body)
            base_url = manifest.base_url
            await session.delete(manifest)
            return body, base_url

    async def activate_prepared_playback(
        self,
        playback_id: str,
        user_id: int,
        *,
        expected_episode: int,
        expected_preferred_source_index: int,
        expected_catalogue_payload: dict[str, Any],
        ttl_seconds: int,
    ) -> tuple[PlaybackSession, PreparedPlayback] | None:
        now = utcnow()
        async with self.sessions.begin() as session:
            prepared = await session.scalar(
                delete(PreparedPlayback)
                .where(
                    PreparedPlayback.playback_id == playback_id,
                    PreparedPlayback.telegram_user_id == user_id,
                    PreparedPlayback.expires_at > now,
                )
                .returning(PreparedPlayback)
            )
            if prepared is None:
                return None
            playback = await session.get(PlaybackSession, playback_id)
            if (
                playback is None
                or playback.telegram_user_id != user_id
                or playback.episode != expected_episode
                or dict(playback.catalogue_payload) != expected_catalogue_payload
                or playback.expires_at <= now
                or prepared.preferred_source_index
                != expected_preferred_source_index
            ):
                return None
            playback.expires_at = now + timedelta(seconds=ttl_seconds)
            generation = await self._activate_playback_in_session(
                session,
                playback,
            )
            setattr(playback, "generation", generation)
            return playback, prepared

    async def create_cast_grant(
        self,
        playback: PlaybackSession,
        *,
        ttl_seconds: int = 7200,
    ) -> str:
        raw = secrets.token_urlsafe(32)
        expires_at = min(
            playback.expires_at,
            utcnow() + timedelta(seconds=max(60, ttl_seconds)),
        )
        async with self.sessions.begin() as session:
            session.add(
                CastGrant(
                    token_hash=token_hash(raw),
                    playback_id=playback.id,
                    telegram_user_id=playback.telegram_user_id,
                    expires_at=expires_at,
                )
            )
        return raw

    async def get_cast_playback(
        self,
        raw_token: str,
        playback_id: str,
    ) -> PlaybackSession | None:
        if not raw_token or len(raw_token) > 128:
            return None
        now = utcnow()
        async with self.sessions() as session:
            playback = await session.scalar(
                select(PlaybackSession)
                .join(
                    CastGrant,
                    CastGrant.playback_id == PlaybackSession.id,
                )
                .join(
                    AllowedUser,
                    AllowedUser.telegram_user_id
                    == CastGrant.telegram_user_id,
                )
                .where(
                    CastGrant.token_hash == token_hash(raw_token),
                    CastGrant.playback_id == playback_id,
                    CastGrant.expires_at > now,
                    AllowedUser.enabled.is_(True),
                    PlaybackSession.telegram_user_id
                    == CastGrant.telegram_user_id,
                    PlaybackSession.expires_at > now,
                )
            )
            if playback is None:
                return None
            return playback

    @staticmethod
    def catalogue_identity(payload: dict[str, Any]) -> tuple[str, str, str]:
        provider_id = str(payload["provider_id"])
        catalogue_url = str(payload["url"])
        identity = hashlib.sha256(
            f"{provider_id}:{catalogue_url.rstrip('/').casefold()}".encode("utf-8")
        ).hexdigest()
        return provider_id, catalogue_url, identity

    @staticmethod
    def _active_playback_for_update(
        user_id: int,
        identity: str,
    ) -> Any:
        return (
            select(ActivePlayback)
            .where(
                ActivePlayback.telegram_user_id == user_id,
                ActivePlayback.identity_hash == identity,
            )
            .with_for_update()
        )

    @staticmethod
    def _watch_state_for_update(
        user_id: int,
        identity: str,
    ) -> Any:
        return (
            select(WatchState)
            .where(
                WatchState.telegram_user_id == user_id,
                WatchState.identity_hash == identity,
            )
            .with_for_update()
        )

    @classmethod
    async def _lock_playback_progress_scope(
        cls,
        session: AsyncSession,
        user_id: int,
        identity: str,
    ) -> tuple[WatchState | None, ActivePlayback | None]:
        """Lock progress rows in the same order as remove/restart operations."""

        state = await session.scalar(
            cls._watch_state_for_update(user_id, identity)
        )
        if state is None:
            return None, None
        active = await session.scalar(
            cls._active_playback_for_update(user_id, identity)
        )
        return state, active

    async def record_progress(
        self,
        user_id: int,
        catalogue_payload: dict[str, Any],
        episode: int,
        position: float,
        duration: float,
        completed: bool,
        *,
        observed_at_ms: int | None = None,
        event_sequence: int | None = None,
        playback_id: str | None = None,
        playback_generation: int | None = None,
    ) -> bool:
        if (observed_at_ms is None) != (event_sequence is None):
            raise ValueError("progress ordering metadata must be supplied together")
        provider_id, catalogue_url, identity = self.catalogue_identity(catalogue_payload)
        total = max(1, int(catalogue_payload.get("total_episodes", 1)))
        episode = max(1, min(total, int(episode)))
        now = utcnow()
        async with self.sessions.begin() as session:
            state_insert = self._insert_do_nothing(
                session,
                WatchState,
                {
                    "telegram_user_id": user_id,
                    "provider_id": provider_id,
                    "catalogue_url": catalogue_url,
                    "identity_hash": identity,
                    "catalogue_payload": catalogue_payload,
                    "next_episode": episode,
                    "last_played_episode": episode,
                    "status": "in_progress",
                    "updated_at": now,
                },
                ("telegram_user_id", "identity_hash"),
            ).returning(WatchState.id)
            inserted_state = (await session.scalar(state_insert)) is not None
            state: WatchState | None
            if playback_id is not None:
                # remove/restart lock WatchState before deleting
                # ActivePlayback. Use the same order here to avoid a
                # PostgreSQL deadlock while retaining the generation lock
                # until the progress transaction commits.
                state, active = await self._lock_playback_progress_scope(
                    session,
                    user_id,
                    identity,
                )
                if (
                    state is None
                    or active is None
                    or active.playback_id != playback_id
                    or (
                        playback_generation is not None
                        and active.generation != playback_generation
                    )
                    or active.episode != episode
                ):
                    # A stale player may race just after "remove". If this
                    # transaction provisionally recreated the missing state,
                    # undo it before accepting the rejection.
                    if inserted_state and state is not None:
                        await session.delete(state)
                    return False
            else:
                state = await session.scalar(
                    self._watch_state_for_update(user_id, identity)
                )
            if state is None:  # pragma: no cover - insert/select invariant.
                raise RuntimeError("watch state could not be created")

            if (
                playback_id is not None
                and observed_at_ms is not None
                and event_sequence is not None
            ):
                playback_cursor_insert = self._insert_do_nothing(
                    session,
                    PlaybackProgressCursor,
                    {
                        "playback_id": playback_id,
                        "observed_at_ms": observed_at_ms,
                        "event_sequence": event_sequence,
                        "updated_at": now,
                    },
                    ("playback_id",),
                ).returning(PlaybackProgressCursor.playback_id)
                inserted_playback_cursor = (
                    await session.scalar(playback_cursor_insert)
                ) is not None
                playback_cursor = await session.get(
                    PlaybackProgressCursor,
                    playback_id,
                    with_for_update=True,
                )
                if (
                    not inserted_playback_cursor
                    and playback_cursor is not None
                    and event_sequence <= playback_cursor.event_sequence
                ):
                    return False
                if playback_cursor is None:  # pragma: no cover
                    raise RuntimeError("playback progress cursor could not be created")
                if event_sequence > playback_cursor.event_sequence:
                    playback_cursor.observed_at_ms = observed_at_ms
                    playback_cursor.event_sequence = event_sequence
                    playback_cursor.updated_at = now
            elif observed_at_ms is not None and event_sequence is not None:
                cursor_insert = self._insert_do_nothing(
                    session,
                    EpisodeProgressCursor,
                    {
                        "watch_state_id": state.id,
                        "episode": episode,
                        "observed_at_ms": observed_at_ms,
                        "event_sequence": event_sequence,
                        "updated_at": now,
                    },
                    ("watch_state_id", "episode"),
                ).returning(EpisodeProgressCursor.watch_state_id)
                inserted_cursor = (
                    await session.scalar(cursor_insert)
                ) is not None
                cursor = await session.scalar(
                    select(EpisodeProgressCursor)
                    .where(
                        EpisodeProgressCursor.watch_state_id == state.id,
                        EpisodeProgressCursor.episode == episode,
                    )
                    .with_for_update()
                )
                if (
                    not inserted_cursor
                    and cursor is not None
                    and event_sequence <= cursor.event_sequence
                ):
                    return False
                if cursor is None:  # pragma: no cover - insert/select invariant.
                    raise RuntimeError("progress cursor could not be created")
                if event_sequence > cursor.event_sequence:
                    cursor.observed_at_ms = observed_at_ms
                    cursor.event_sequence = event_sequence
                    cursor.updated_at = now

            progress: EpisodeProgress | None = None
            if (
                playback_id is not None
                and observed_at_ms is None
                and not completed
                and position <= 0.25
            ):
                # Compatibility for a Mini App that was already open during
                # deployment. Legacy clients have no sequence metadata, and
                # Telegram's iOS WebView may report zero while detaching the
                # video. Reject only that ambiguous legacy close event. New
                # clients carry a sequence and may intentionally seek to zero.
                progress = await session.scalar(
                    select(EpisodeProgress)
                    .where(
                        EpisodeProgress.watch_state_id == state.id,
                        EpisodeProgress.episode == episode,
                    )
                    .with_for_update()
                )
                if (
                    progress is not None
                    and float(progress.position_seconds or 0.0) >= 5.0
                ):
                    return False

            state.catalogue_payload = catalogue_payload
            state.last_played_episode = episode
            state.updated_at = now
            if completed:
                state.next_episode = max(state.next_episode, min(total, episode + 1))
                final_episode_completed = episode == total
                if not final_episode_completed:
                    final_episode_completed = bool(
                        await session.scalar(
                            select(EpisodeProgress.completed).where(
                                EpisodeProgress.watch_state_id == state.id,
                                EpisodeProgress.episode == total,
                            )
                        )
                    )
                state.status = (
                    "completed" if final_episode_completed else "in_progress"
                )
            else:
                state.next_episode = max(state.next_episode, episode)
                # A partial replay is an active interruption point. It must
                # reappear in Continue Watching even when the season had
                # previously been completed.
                state.status = "in_progress"

            await session.execute(
                self._insert_do_nothing(
                    session,
                    EpisodeProgress,
                    {
                        "watch_state_id": state.id,
                        "episode": episode,
                        "position_seconds": 0.0,
                        "duration_seconds": 0.0,
                        "completed": False,
                        "updated_at": now,
                    },
                    ("watch_state_id", "episode"),
                )
            )
            if progress is None:
                progress = await session.scalar(
                    select(EpisodeProgress)
                    .where(
                        EpisodeProgress.watch_state_id == state.id,
                        EpisodeProgress.episode == episode,
                    )
                    .with_for_update()
                )
            if progress is None:  # pragma: no cover - insert/select invariant.
                raise RuntimeError("episode progress could not be created")
            progress.position_seconds = 0.0 if completed else position
            progress.duration_seconds = max(float(progress.duration_seconds or 0.0), duration)
            # Opening or replaying an episode makes it the active interruption
            # point again, even if that episode was completed in the past.
            progress.completed = completed
            progress.updated_at = now
            return True

    async def continue_watching(
        self,
        user_id: int,
        limit: int = 20,
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            statement = select(WatchState).where(
                WatchState.telegram_user_id == user_id
            )
            if status is not None:
                statement = statement.where(WatchState.status == status)
            states = list(
                await session.scalars(
                    statement.order_by(WatchState.updated_at.desc()).limit(limit)
                )
            )
            state_ids = [state.id for state in states]
            progress_by_key: dict[tuple[int, int], EpisodeProgress] = {}
            if state_ids:
                progress_rows = await session.scalars(
                    select(EpisodeProgress).where(
                        EpisodeProgress.watch_state_id.in_(state_ids)
                    )
                )
                progress_by_key = {
                    (item.watch_state_id, item.episode): item
                    for item in progress_rows
                }
            output: list[dict[str, Any]] = []
            for state in states:
                last_progress = progress_by_key.get(
                    (state.id, state.last_played_episode)
                )
                if last_progress is not None and not last_progress.completed:
                    resume_episode = state.last_played_episode
                    position = float(last_progress.position_seconds or 0.0)
                else:
                    resume_episode = state.next_episode
                    next_progress = progress_by_key.get(
                        (state.id, state.next_episode)
                    )
                    position = float(
                        next_progress.position_seconds
                        if next_progress is not None
                        else 0.0
                    )
                output.append(
                    {
                        "catalogue": dict(state.catalogue_payload),
                        "next_episode": state.next_episode,
                        "last_played_episode": state.last_played_episode,
                        "resume_episode": resume_episode,
                        "position": position,
                        "status": state.status,
                        "updated_at": state.updated_at.isoformat(),
                    }
                )
            return output

    async def remove_from_continue_watching(
        self,
        user_id: int,
        catalogue_payload: dict[str, Any],
    ) -> bool:
        _, _, identity = self.catalogue_identity(catalogue_payload)
        async with self.sessions.begin() as session:
            state = await session.scalar(
                self._watch_state_for_update(user_id, identity)
            )
            if state is None:
                return False
            # Delete explicitly as well as relying on ON DELETE CASCADE. SQLite
            # does not enforce foreign-key cascades unless its PRAGMA is enabled.
            await session.execute(
                delete(EpisodeProgress).where(
                    EpisodeProgress.watch_state_id == state.id
                )
            )
            await session.execute(
                delete(EpisodeProgressCursor).where(
                    EpisodeProgressCursor.watch_state_id == state.id
                )
            )
            await session.execute(
                delete(ActivePlayback).where(
                    ActivePlayback.telegram_user_id == user_id,
                    ActivePlayback.identity_hash == identity,
                )
            )
            await session.delete(state)
            return True

    async def restart_watch_entry(
        self,
        user_id: int,
        catalogue_payload: dict[str, Any],
    ) -> bool:
        _, _, identity = self.catalogue_identity(catalogue_payload)
        async with self.sessions.begin() as session:
            state = await session.scalar(
                self._watch_state_for_update(user_id, identity)
            )
            if state is None:
                return False
            await session.execute(
                delete(EpisodeProgress).where(
                    EpisodeProgress.watch_state_id == state.id
                )
            )
            await session.execute(
                delete(EpisodeProgressCursor).where(
                    EpisodeProgressCursor.watch_state_id == state.id
                )
            )
            await session.execute(
                delete(ActivePlayback).where(
                    ActivePlayback.telegram_user_id == user_id,
                    ActivePlayback.identity_hash == identity,
                )
            )
            state.catalogue_payload = catalogue_payload
            state.next_episode = 1
            state.last_played_episode = 1
            state.status = "in_progress"
            state.updated_at = utcnow()
            return True

    async def episode_position(
        self,
        user_id: int,
        catalogue_payload: dict[str, Any],
        episode: int,
    ) -> float:
        value = await self.saved_episode_position(
            user_id,
            catalogue_payload,
            episode,
        )
        return float(value or 0.0)

    async def saved_episode_position(
        self,
        user_id: int,
        catalogue_payload: dict[str, Any],
        episode: int,
    ) -> float | None:
        _, _, identity = self.catalogue_identity(catalogue_payload)
        async with self.sessions() as session:
            value = await session.scalar(
                select(EpisodeProgress.position_seconds)
                .join(WatchState, WatchState.id == EpisodeProgress.watch_state_id)
                .where(
                    WatchState.telegram_user_id == user_id,
                    WatchState.identity_hash == identity,
                    EpisodeProgress.episode == episode,
                )
            )
            return None if value is None else float(value)

    async def cleanup(self) -> None:
        now = utcnow()
        async with self.sessions.begin() as session:
            await session.execute(
                delete(EphemeralSelection).where(EphemeralSelection.expires_at <= now)
            )
            await session.execute(delete(LaunchTicket).where(LaunchTicket.expires_at <= now))
            await session.execute(delete(WebSession).where(WebSession.expires_at <= now))
            await session.execute(delete(CastGrant).where(CastGrant.expires_at <= now))
            await session.execute(
                delete(PlaybackManifest).where(PlaybackManifest.expires_at <= now)
            )
            await session.execute(
                delete(PreparedPlayback).where(PreparedPlayback.expires_at <= now)
            )
            await session.execute(
                delete(PlaybackSession).where(PlaybackSession.expires_at <= now)
            )
