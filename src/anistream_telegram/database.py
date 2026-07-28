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
    String,
    Text,
    UniqueConstraint,
    delete,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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


class Database:
    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self, bootstrap_users: tuple[int, ...] = ()) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        for user_id in bootstrap_users:
            await self.bootstrap_allowed_user(user_id)
        await self.cleanup()

    async def close(self) -> None:
        await self.engine.dispose()

    async def set_allowed(self, user_id: int, enabled: bool) -> None:
        async with self.sessions.begin() as session:
            item = await session.get(AllowedUser, user_id)
            if item is None:
                session.add(AllowedUser(telegram_user_id=user_id, enabled=enabled))
            else:
                item.enabled = enabled

    async def bootstrap_allowed_user(self, user_id: int) -> None:
        """Create an initial allow entry without overriding a persisted revocation."""
        async with self.sessions.begin() as session:
            item = await session.get(AllowedUser, user_id)
            if item is None:
                session.add(AllowedUser(telegram_user_id=user_id, enabled=True))

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
            preference = await session.get(UserPreference, user_id)
            if preference is None:
                preference = UserPreference(
                    telegram_user_id=user_id,
                    autoplay_next=bool(enabled),
                )
                session.add(preference)
            else:
                preference.autoplay_next = bool(enabled)
                preference.updated_at = utcnow()
        return bool(enabled)

    async def toggle_autoplay(self, user_id: int) -> bool:
        async with self.sessions.begin() as session:
            preference = await session.scalar(
                select(UserPreference)
                .where(UserPreference.telegram_user_id == user_id)
                .with_for_update()
            )
            if preference is None:
                preference = UserPreference(
                    telegram_user_id=user_id,
                    autoplay_next=False,
                )
                session.add(preference)
            else:
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
            preference = await session.scalar(
                select(UserProviderPreference)
                .where(
                    UserProviderPreference.telegram_user_id == user_id,
                    UserProviderPreference.provider_id == provider_id,
                )
                .with_for_update()
            )
            if preference is None:
                preference = UserProviderPreference(
                    telegram_user_id=user_id,
                    provider_id=provider_id,
                    enabled=False,
                )
                session.add(preference)
            else:
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
            item = await session.get(LaunchTicket, token_hash(raw_token), with_for_update=True)
            if (
                item is None
                or item.telegram_user_id != user_id
                or item.consumed_at is not None
                or item.expires_at <= utcnow()
            ):
                return None
            item.consumed_at = utcnow()
            return dict(item.payload)

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
    ) -> PlaybackSession:
        item = PlaybackSession(
            id=secrets.token_urlsafe(18),
            telegram_user_id=user_id,
            catalogue_payload=catalogue_payload,
            episode=episode,
            media_url=media_url,
            media_headers=media_headers,
            media_kind=media_kind,
            source_name=source_name[:128],
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        )
        async with self.sessions.begin() as session:
            session.add(item)
        return item

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
            grant = await session.get(CastGrant, token_hash(raw_token))
            if (
                grant is None
                or grant.playback_id != playback_id
                or grant.expires_at <= now
            ):
                return None
            allowed = await session.get(AllowedUser, grant.telegram_user_id)
            if allowed is None or not allowed.enabled:
                return None
            playback = await session.get(PlaybackSession, playback_id)
            if (
                playback is None
                or playback.telegram_user_id != grant.telegram_user_id
                or playback.expires_at <= now
            ):
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

    async def record_progress(
        self,
        user_id: int,
        catalogue_payload: dict[str, Any],
        episode: int,
        position: float,
        duration: float,
        completed: bool,
    ) -> None:
        provider_id, catalogue_url, identity = self.catalogue_identity(catalogue_payload)
        total = max(1, int(catalogue_payload.get("total_episodes", 1)))
        episode = max(1, min(total, int(episode)))
        now = utcnow()
        async with self.sessions.begin() as session:
            state = await session.scalar(
                select(WatchState)
                .where(
                    WatchState.telegram_user_id == user_id,
                    WatchState.identity_hash == identity,
                )
                .with_for_update()
            )
            if state is None:
                state = WatchState(
                    telegram_user_id=user_id,
                    provider_id=provider_id,
                    catalogue_url=catalogue_url,
                    identity_hash=identity,
                    catalogue_payload=catalogue_payload,
                    next_episode=episode,
                    last_played_episode=episode,
                )
                session.add(state)
                await session.flush()
            state.catalogue_payload = catalogue_payload
            state.last_played_episode = episode
            state.updated_at = now
            if completed:
                state.next_episode = max(state.next_episode, min(total, episode + 1))
                if episode == total:
                    state.status = "completed"
            else:
                state.next_episode = max(state.next_episode, episode)

            progress = await session.scalar(
                select(EpisodeProgress)
                .where(
                    EpisodeProgress.watch_state_id == state.id,
                    EpisodeProgress.episode == episode,
                )
                .with_for_update()
            )
            if progress is None:
                progress = EpisodeProgress(watch_state_id=state.id, episode=episode)
                session.add(progress)
            progress.position_seconds = 0.0 if completed else position
            progress.duration_seconds = max(float(progress.duration_seconds or 0.0), duration)
            # Opening or replaying an episode makes it the active interruption
            # point again, even if that episode was completed in the past.
            progress.completed = completed
            progress.updated_at = now

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
            output: list[dict[str, Any]] = []
            for state in states:
                last_progress = await session.scalar(
                    select(EpisodeProgress).where(
                        EpisodeProgress.watch_state_id == state.id,
                        EpisodeProgress.episode == state.last_played_episode,
                    )
                )
                if last_progress is not None and not last_progress.completed:
                    resume_episode = state.last_played_episode
                    position = float(last_progress.position_seconds or 0.0)
                else:
                    resume_episode = state.next_episode
                    position = float(
                        await session.scalar(
                            select(EpisodeProgress.position_seconds).where(
                                EpisodeProgress.watch_state_id == state.id,
                                EpisodeProgress.episode == state.next_episode,
                            )
                        )
                        or 0.0
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
                select(WatchState)
                .where(
                    WatchState.telegram_user_id == user_id,
                    WatchState.identity_hash == identity,
                )
                .with_for_update()
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
                select(WatchState)
                .where(
                    WatchState.telegram_user_id == user_id,
                    WatchState.identity_hash == identity,
                )
                .with_for_update()
            )
            if state is None:
                return False
            await session.execute(
                delete(EpisodeProgress).where(
                    EpisodeProgress.watch_state_id == state.id
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
                delete(PlaybackSession).where(PlaybackSession.expires_at <= now)
            )
