from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql

from anistream_telegram.database import (
    POSTGRES_MIGRATION_LOCK_ID,
    Database,
    SchemaMigration,
    acquire_schema_migration_lock,
)


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


async def test_sqlite_enforces_foreign_keys(tmp_path: Path) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        async with database.engine.connect() as connection:
            result = await connection.exec_driver_sql("PRAGMA foreign_keys")
            assert result.scalar_one() == 1
    finally:
        await database.close()


async def test_schema_versions_are_recorded_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    database = await database_at(path)
    try:
        async with database.sessions() as session:
            versions = list(
                await session.scalars(
                    select(SchemaMigration.version).order_by(
                        SchemaMigration.version
                    )
                )
            )
        assert versions == [1, 2, 3, 4]
        await database.initialize()
        async with database.sessions() as session:
            assert list(
                await session.scalars(select(SchemaMigration.version))
            ) == [1, 2, 3, 4]
    finally:
        await database.close()


def test_postgresql_migrations_take_a_transaction_advisory_lock() -> None:
    statements: list[str] = []
    connection = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        exec_driver_sql=statements.append,
    )

    acquire_schema_migration_lock(connection)

    assert statements == [
        f"SELECT pg_advisory_xact_lock({POSTGRES_MIGRATION_LOCK_ID})"
    ]


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


async def test_launch_ticket_is_atomic_under_concurrent_sqlite_exchange(
    tmp_path: Path,
) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        ticket = await database.create_launch_ticket(123, {"episode": 3})
        results = await asyncio.gather(
            database.exchange_launch_ticket(ticket, 123),
            database.exchange_launch_ticket(ticket, 123),
        )
        assert results.count({"episode": 3}) == 1
        assert results.count(None) == 1
    finally:
        await database.close()


async def test_bootstrap_does_not_reenable_a_revoked_user(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    database = await database_at(path)
    try:
        await database.bootstrap_allowed_user(123)
        assert await database.is_allowed(123) is True
        await database.set_allowed(123, False)
        assert await database.is_allowed(123) is False
    finally:
        await database.close()

    restarted = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    try:
        await restarted.initialize((123,))
        assert await restarted.is_allowed(123) is False
    finally:
        await restarted.close()


async def test_autoplay_defaults_on_and_persists_per_user(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    database = await database_at(path)
    try:
        assert await database.autoplay_enabled(123) is True
        assert await database.toggle_autoplay(123) is False
        assert await database.autoplay_enabled(123) is False
        assert await database.autoplay_enabled(999) is True
    finally:
        await database.close()

    reopened = await database_at(path)
    try:
        assert await reopened.autoplay_enabled(123) is False
        assert await reopened.set_autoplay_enabled(123, True) is True
        assert await reopened.autoplay_enabled(123) is True
    finally:
        await reopened.close()


async def test_provider_preferences_default_on_and_persist_per_user(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    provider_ids = ("anime_sama", "french_stream")
    database = await database_at(path)
    try:
        assert await database.provider_states(123, provider_ids) == {
            "anime_sama": True,
            "french_stream": True,
        }
        assert await database.enabled_provider_ids(123, provider_ids) == provider_ids
        assert (
            await database.toggle_provider_enabled(123, "anime_sama")
            is False
        )
        assert await database.enabled_provider_ids(123, provider_ids) == (
            "french_stream",
        )
        assert await database.enabled_provider_ids(999, provider_ids) == provider_ids
    finally:
        await database.close()

    reopened = await database_at(path)
    try:
        assert await reopened.provider_states(123, provider_ids) == {
            "anime_sama": False,
            "french_stream": True,
        }
        assert (
            await reopened.toggle_provider_enabled(123, "anime_sama")
            is True
        )
        assert await reopened.enabled_provider_ids(123, provider_ids) == provider_ids
    finally:
        await reopened.close()


async def test_selection_payload_updates_are_user_and_kind_scoped(
    tmp_path: Path,
) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        selection_id = await database.create_selection(
            123,
            "search_results",
            {"query": "example", "message_ids": []},
        )
        updated = {"query": "example", "message_ids": [{"message_id": 7}]}

        assert (
            await database.update_selection_payload(
                selection_id,
                999,
                updated,
                kind="search_results",
            )
            is False
        )
        assert (
            await database.update_selection_payload(
                selection_id,
                123,
                updated,
                kind="other",
            )
            is False
        )
        assert (
            await database.update_selection_payload(
                selection_id,
                123,
                updated,
                kind="search_results",
            )
            is True
        )
        assert await database.get_selection(
            selection_id,
            123,
            kind="search_results",
        ) == updated
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
        assert await database.saved_episode_position(123, catalogue(), 2) is None
        await database.record_progress(123, catalogue(), 2, 88.5, 1500, False)
        entries = await database.continue_watching(123)
        assert entries[0]["next_episode"] == 2
        await database.record_progress(123, catalogue(), 4, 240.0, 1500, False)
        entries = await database.continue_watching(123)
        assert entries[0]["next_episode"] == 4
        assert entries[0]["resume_episode"] == 4
        assert (
            await database.saved_episode_position(123, catalogue(), 2)
            == 88.5
        )
        assert await database.episode_position(123, catalogue(), 2) == 88.5
        assert await database.episode_position(123, catalogue(), 4) == 240.0
    finally:
        await database.close()


async def test_continue_watching_loads_progress_in_one_bounded_query(
    tmp_path: Path,
) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        for episode_number in range(1, 6):
            item = {**catalogue(), "url": f"https://provider.example/{episode_number}"}
            await database.record_progress(
                123,
                item,
                episode_number,
                30.0,
                1500.0,
                False,
            )
        statements: list[str] = []

        def before_cursor_execute(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(
            database.engine.sync_engine,
            "before_cursor_execute",
            before_cursor_execute,
        )
        try:
            assert len(await database.continue_watching(123)) == 5
        finally:
            event.remove(
                database.engine.sync_engine,
                "before_cursor_execute",
                before_cursor_execute,
            )
        assert len(statements) == 2
    finally:
        await database.close()


async def test_stale_progress_event_cannot_overwrite_newer_position(
    tmp_path: Path,
) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        assert await database.record_progress(
            123,
            catalogue(),
            6,
            331.0,
            1435.0,
            False,
            observed_at_ms=2_000,
            event_sequence=2,
        )
        assert not await database.record_progress(
            123,
            catalogue(),
            6,
            0.0,
            1435.0,
            False,
            observed_at_ms=1_000,
            event_sequence=3,
        )
        assert await database.episode_position(123, catalogue(), 6) == 331.0

        # A genuinely newer backward seek must still be accepted; ordering is
        # based on the observation, never on taking the maximum position.
        assert await database.record_progress(
            123,
            catalogue(),
            6,
            120.0,
            1435.0,
            False,
            observed_at_ms=3_000,
            event_sequence=4,
        )
        assert await database.episode_position(123, catalogue(), 6) == 120.0
    finally:
        await database.close()


async def test_stale_incomplete_event_cannot_undo_completion(
    tmp_path: Path,
) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        assert await database.record_progress(
            123,
            catalogue(),
            12,
            1500.0,
            1500.0,
            True,
            observed_at_ms=2_000,
            event_sequence=2,
        )
        assert not await database.record_progress(
            123,
            catalogue(),
            12,
            600.0,
            1500.0,
            False,
            observed_at_ms=1_000,
            event_sequence=1,
        )
        entry = (await database.continue_watching(123))[0]
        assert entry["status"] == "completed"
    finally:
        await database.close()


async def test_concurrent_sqlite_progress_creation_is_serialized(
    tmp_path: Path,
) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        results = await asyncio.gather(
            database.record_progress(
                123,
                catalogue(),
                6,
                100.0,
                1500.0,
                False,
                observed_at_ms=1_000,
                event_sequence=1,
            ),
            database.record_progress(
                123,
                catalogue(),
                6,
                200.0,
                1500.0,
                False,
                observed_at_ms=2_000,
                event_sequence=2,
            ),
        )
        assert results == [True, True]
        assert await database.episode_position(123, catalogue(), 6) == 200.0
    finally:
        await database.close()


async def test_skip_forward_then_rewind_resumes_last_interrupted_episode(
    tmp_path: Path,
) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        await database.record_progress(123, catalogue(), 8, 420.0, 1500, False)
        await database.record_progress(123, catalogue(), 2, 95.0, 1500, False)

        entry = (await database.continue_watching(123))[0]
        assert entry["next_episode"] == 8
        assert entry["last_played_episode"] == 2
        assert entry["resume_episode"] == 2
        assert entry["position"] == 95.0
        assert await database.episode_position(123, catalogue(), 8) == 420.0
    finally:
        await database.close()


async def test_completed_rewatch_returns_to_forward_continuation(tmp_path: Path) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        await database.record_progress(123, catalogue(), 7, 0, 1500, True)
        await database.record_progress(123, catalogue(), 2, 300.0, 1500, False)
        interrupted = (await database.continue_watching(123))[0]
        assert interrupted["resume_episode"] == 2
        assert interrupted["next_episode"] == 8

        await database.record_progress(123, catalogue(), 2, 1500, 1500, True)
        resumed = (await database.continue_watching(123))[0]
        assert resumed["resume_episode"] == 8
        assert resumed["next_episode"] == 8
        assert resumed["position"] == 0.0
    finally:
        await database.close()


async def test_selected_episode_at_zero_is_the_interruption_point(tmp_path: Path) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        await database.record_progress(123, catalogue(), 7, 0, 1500, True)
        await database.record_progress(123, catalogue(), 2, 0, 0, False)

        entry = (await database.continue_watching(123))[0]
        assert entry["next_episode"] == 8
        assert entry["last_played_episode"] == 2
        assert entry["resume_episode"] == 2
        assert entry["position"] == 0.0
    finally:
        await database.close()


async def test_completed_season_returns_to_active_during_partial_rewatch(
    tmp_path: Path,
) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        await database.record_progress(123, catalogue(), 12, 1500, 1500, True)
        await database.record_progress(123, catalogue(), 1, 120.0, 1500, False)

        entry = (await database.continue_watching(123))[0]
        assert entry["status"] == "in_progress"
        assert entry["next_episode"] == 12
        assert entry["resume_episode"] == 1
        assert entry["position"] == 120.0
    finally:
        await database.close()


async def test_old_playback_generation_cannot_overwrite_new_player(
    tmp_path: Path,
) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        old = await database.create_playback(
            123,
            catalogue(),
            6,
            media_url="https://cdn.example/old.mp4",
            media_headers={},
            media_kind="mp4",
            source_name="Old",
            ttl_seconds=600,
        )
        assert await database.record_progress(
            123,
            catalogue(),
            6,
            300,
            1500,
            False,
            observed_at_ms=2_000,
            event_sequence=1,
            playback_id=old.id,
            playback_generation=old.generation,
        )
        new = await database.create_playback(
            123,
            catalogue(),
            6,
            media_url="https://cdn.example/new.mp4",
            media_headers={},
            media_kind="mp4",
            source_name="New",
            ttl_seconds=600,
        )
        assert new.generation == old.generation + 1
        assert await database.record_progress(
            123,
            catalogue(),
            6,
            600,
            1500,
            False,
            observed_at_ms=100,
            event_sequence=1,
            playback_id=new.id,
            playback_generation=new.generation,
        )
        assert not await database.record_progress(
            123,
            catalogue(),
            6,
            120,
            1500,
            False,
            observed_at_ms=3_000,
            event_sequence=2,
            playback_id=old.id,
            playback_generation=old.generation,
        )
        assert await database.episode_position(123, catalogue(), 6) == 600
    finally:
        await database.close()


async def test_stale_player_cannot_recreate_a_removed_continue_entry(
    tmp_path: Path,
) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        playback = await database.create_playback(
            123,
            catalogue(),
            6,
            media_url="https://cdn.example/old.mp4",
            media_headers={},
            media_kind="mp4",
            source_name="Old",
            ttl_seconds=600,
        )
        await database.record_progress(
            123,
            catalogue(),
            6,
            300,
            1500,
            False,
        )
        assert await database.remove_from_continue_watching(
            123,
            catalogue(),
        )

        accepted = await database.record_progress(
            123,
            catalogue(),
            6,
            400,
            1500,
            False,
            observed_at_ms=4_000,
            event_sequence=2,
            playback_id=playback.id,
            playback_generation=playback.generation,
        )

        assert accepted is False
        assert await database.continue_watching(123) == []
    finally:
        await database.close()


def test_progress_locks_active_generation_for_the_postgresql_transaction() -> None:
    statement = Database._active_playback_for_update(123, "identity")
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "FROM active_playbacks" in sql
    assert "FOR UPDATE" in sql


async def test_progress_uses_the_remove_restart_lock_order() -> None:
    statements: list[str] = []

    class RecordingSession:
        async def scalar(self, statement: Any) -> Any:
            sql = str(statement.compile(dialect=postgresql.dialect()))
            statements.append(sql)
            if "FROM watch_states" in sql:
                return SimpleNamespace()
            return None

    state, active = await Database._lock_playback_progress_scope(
        RecordingSession(),  # type: ignore[arg-type]
        123,
        "identity",
    )

    assert state is not None
    assert active is None
    assert len(statements) == 2
    assert "FROM watch_states" in statements[0]
    assert "FOR UPDATE" in statements[0]
    assert "FROM active_playbacks" in statements[1]
    assert "FOR UPDATE" in statements[1]


async def test_remove_continue_entry_is_user_scoped_and_clears_episode_progress(
    tmp_path: Path,
) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        other_catalogue = {
            **catalogue(),
            "title": "Other Series",
            "url": "https://provider.example/catalogue/other/season-1/vf/",
        }
        await database.record_progress(123, catalogue(), 2, 88.5, 1500, False)
        await database.record_progress(123, other_catalogue, 4, 120.0, 1500, False)
        await database.record_progress(999, catalogue(), 3, 45.0, 1500, False)

        assert await database.remove_from_continue_watching(123, catalogue()) is True
        assert await database.remove_from_continue_watching(123, catalogue()) is False

        own_entries = await database.continue_watching(123)
        assert [entry["catalogue"]["title"] for entry in own_entries] == [
            "Other Series"
        ]
        assert await database.episode_position(123, catalogue(), 2) == 0.0

        other_user_entries = await database.continue_watching(999)
        assert other_user_entries[0]["catalogue"]["title"] == "Example"
        assert await database.episode_position(999, catalogue(), 3) == 45.0
    finally:
        await database.close()


async def test_completed_filter_and_restart_reset_progress_for_only_one_user(
    tmp_path: Path,
) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        await database.record_progress(123, catalogue(), 4, 240.0, 1500, False)
        await database.record_progress(999, catalogue(), 3, 45.0, 1500, False)

        assert len(
            await database.continue_watching(123, status="in_progress")
        ) == 1
        assert await database.continue_watching(123, status="completed") == []

        await database.record_progress(123, catalogue(), 12, 1500, 1500, True)
        assert await database.continue_watching(123, status="in_progress") == []
        completed = await database.continue_watching(123, status="completed")
        assert completed[0]["status"] == "completed"

        assert await database.restart_watch_entry(123, catalogue()) is True
        restarted = await database.continue_watching(123, status="in_progress")
        assert restarted[0]["status"] == "in_progress"
        assert restarted[0]["next_episode"] == 1
        assert restarted[0]["resume_episode"] == 1
        assert restarted[0]["position"] == 0.0
        assert await database.episode_position(123, catalogue(), 4) == 0.0
        assert await database.episode_position(123, catalogue(), 12) == 0.0

        other_user = await database.continue_watching(999)
        assert other_user[0]["resume_episode"] == 3
        assert await database.episode_position(999, catalogue(), 3) == 45.0
    finally:
        await database.close()


async def test_cast_grant_is_playback_bound_and_revoked_with_whitelist(
    tmp_path: Path,
) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        await database.set_allowed(123, True)
        playback = await database.create_playback(
            123,
            catalogue(),
            3,
            media_url="https://cdn.example/video.mp4",
            media_headers={},
            media_kind="mp4",
            source_name="Test",
            ttl_seconds=600,
        )
        other = await database.create_playback(
            123,
            catalogue(),
            4,
            media_url="https://cdn.example/video-4.mp4",
            media_headers={},
            media_kind="mp4",
            source_name="Test",
            ttl_seconds=600,
        )
        grant = await database.create_cast_grant(playback, ttl_seconds=300)

        assert await database.get_cast_playback(grant, playback.id) is not None
        assert await database.get_cast_playback(grant, other.id) is None
        await database.set_allowed(123, False)
        assert await database.get_cast_playback(grant, playback.id) is None
    finally:
        await database.close()


async def test_prepared_playback_and_manifest_are_user_bound(
    tmp_path: Path,
) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        body = b"#EXTM3U\nsegment.ts\n"
        playback = await database.create_playback(
            123,
            catalogue(),
            4,
            media_url="https://cdn.example/master.m3u8",
            media_headers={},
            media_kind="hls",
            source_name="Test",
            ttl_seconds=300,
            prefetched_playlist=body,
            prefetched_playlist_url="https://cdn.example/path/master.m3u8",
            prepared=True,
            preferred_source_index=1,
            source_index=0,
            source_count=2,
        )

        assert await database.consume_playback_manifest(playback.id, 999) is None
        assert await database.consume_playback_manifest(playback.id, 123) == (
            body,
            "https://cdn.example/path/master.m3u8",
        )
        assert (
            await database.activate_prepared_playback(
                playback.id,
                999,
                expected_episode=4,
                expected_preferred_source_index=1,
                expected_catalogue_payload=catalogue(),
                ttl_seconds=600,
            )
            is None
        )
        activated = await database.activate_prepared_playback(
            playback.id,
            123,
            expected_episode=4,
            expected_preferred_source_index=1,
            expected_catalogue_payload=catalogue(),
            ttl_seconds=600,
        )
        assert activated is not None
        activated_playback, prepared = activated
        assert activated_playback.id == playback.id
        assert prepared.source_index == 0
        assert prepared.source_count == 2
        assert (
            await database.activate_prepared_playback(
                playback.id,
                123,
                expected_episode=4,
                expected_preferred_source_index=1,
                expected_catalogue_payload=catalogue(),
                ttl_seconds=600,
            )
            is None
        )
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


async def test_media_authorization_uses_one_joined_database_query(
    tmp_path: Path,
) -> None:
    database = await database_at(tmp_path / "db.sqlite")
    try:
        await database.set_allowed(123, True)
        raw, _ = await database.create_web_session(123, {}, ttl_seconds=600)
        playback = await database.create_playback(
            123,
            catalogue(),
            1,
            media_url="https://cdn.example/video.mp4",
            media_headers={},
            media_kind="mp4",
            source_name="Test",
            ttl_seconds=600,
        )
        statements: list[str] = []

        def before_cursor_execute(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(
            database.engine.sync_engine,
            "before_cursor_execute",
            before_cursor_execute,
        )
        try:
            authorized = await database.get_media_playback(raw, playback.id)
        finally:
            event.remove(
                database.engine.sync_engine,
                "before_cursor_execute",
                before_cursor_execute,
            )
        assert authorized is not None
        assert authorized[1].id == playback.id
        assert len(statements) == 1
    finally:
        await database.close()
