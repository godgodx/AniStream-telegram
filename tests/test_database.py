from __future__ import annotations

from pathlib import Path

from anistream_telegram.database import Database


def catalogue() -> dict:
    return {
        "provider_id": "test",
        "provider_name": "Test Provider",
        "title": "Example",
        "url": "https://provider.example/catalogue/example/season-1/vf/",
        "season": "Season 1",
        "language_code": "vf",
        "language_label": "VF",
        "total_episodes": 12,
        "episodes": [],
    }


async def database_at(path: Path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    await database.initialize()
    return database


async def test_whitelist_and_one_time_launch_ticket(tmp_path: Path) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        assert not await database.is_allowed(123)
        await database.set_allowed(123, True)
        assert await database.is_allowed(123)
        ticket = await database.create_launch_ticket(123, {"episode": 3})
        assert await database.exchange_launch_ticket(ticket, 999) is None
        assert await database.exchange_launch_ticket(ticket, 123) == {"episode": 3}
        assert await database.exchange_launch_ticket(ticket, 123) is None
    finally:
        await database.close()


async def test_rewatch_does_not_move_continuation_backwards(tmp_path: Path) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        await database.record_progress(123, catalogue(), 4, 0, 1500, True)
        entries = await database.continue_watching(123)
        assert entries[0]["next_episode"] == 5

        await database.record_progress(123, catalogue(), 1, 0, 1500, True)
        entries = await database.continue_watching(123)
        assert entries[0]["next_episode"] == 5
        assert entries[0]["last_played_episode"] == 1
    finally:
        await database.close()


async def test_positions_are_stored_per_episode(tmp_path: Path) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        await database.record_progress(123, catalogue(), 2, 88.5, 1500, False)
        entries = await database.continue_watching(123)
        assert entries[0]["next_episode"] == 2
        await database.record_progress(123, catalogue(), 4, 240.0, 1500, False)
        entries = await database.continue_watching(123)
        assert entries[0]["next_episode"] == 4
        assert await database.episode_position(123, catalogue(), 2) == 88.5
        assert await database.episode_position(123, catalogue(), 4) == 240.0
    finally:
        await database.close()


async def test_disabled_user_loses_existing_web_session(tmp_path: Path) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        await database.set_allowed(123, True)
        raw, _ = await database.create_web_session(123, {}, ttl_seconds=600)
        assert await database.get_web_session(raw) is not None
        await database.set_allowed(123, False)
        assert await database.get_web_session(raw) is None
    finally:
        await database.close()
