from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message

from anistream_telegram.bot import MAIN_MENU_TEXT, BotHandlers, main_keyboard


def handler(public_base_url: str) -> BotHandlers:
    instance = object.__new__(BotHandlers)
    instance.config = SimpleNamespace(public_base_url=public_base_url)
    instance.database = SimpleNamespace(
        episode_position=AsyncMock(return_value=0.0),
        create_launch_ticket=AsyncMock(return_value="launch-ticket"),
    )
    return instance


def callback() -> SimpleNamespace:
    return SimpleNamespace(
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=123),
        message=SimpleNamespace(
            answer=AsyncMock(),
            edit_text=AsyncMock(),
            chat=SimpleNamespace(id=456),
            message_id=789,
        ),
    )


def test_main_keyboard_has_clear_native_action_hierarchy() -> None:
    keyboard = main_keyboard()

    assert len(keyboard.inline_keyboard) == 2
    search, resume = keyboard.inline_keyboard[0]
    assert search.text == "🔎 Search"
    assert search.style == "primary"
    assert resume.text == "▶ Continue watching"
    assert resume.style == "primary"
    assert keyboard.inline_keyboard[1][0].text == "❔ Help"


@pytest.mark.asyncio
async def test_main_menu_replaces_the_clicked_message() -> None:
    handlers = handler("https://watch.example")
    event = callback()
    state = SimpleNamespace(clear=AsyncMock())

    await handlers.main_menu(event, state)

    event.answer.assert_awaited_once()
    state.clear.assert_awaited_once()
    event.message.answer.assert_not_awaited()
    event.message.edit_text.assert_awaited_once()
    assert event.message.edit_text.await_args.args[0] == MAIN_MENU_TEXT


@pytest.mark.asyncio
async def test_search_prompt_replaces_menu_and_remembers_panel() -> None:
    handlers = handler("https://watch.example")
    event = callback()
    state = SimpleNamespace(
        set_state=AsyncMock(),
        update_data=AsyncMock(),
    )

    await handlers.search_prompt(event, state)

    event.message.answer.assert_not_awaited()
    event.message.edit_text.assert_awaited_once()
    state.update_data.assert_awaited_once_with(
        panel_chat_id=456,
        panel_message_id=789,
    )
    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].text == "‹ Cancel"


@pytest.mark.asyncio
async def test_search_result_reuses_the_existing_panel() -> None:
    handlers = handler("https://watch.example")
    handlers.core = SimpleNamespace(
        search=AsyncMock(
            return_value=(
                [
                    {
                        "title": "Tokyo Ghoul",
                        "provider_name": "Anime-Sama",
                        "provider_id": "anime-sama",
                        "url": "https://example.test/tokyo-ghoul",
                    }
                ],
                [],
            )
        )
    )
    handlers.database.create_selection = AsyncMock(return_value="search-selection")
    panel = MagicMock(spec=Message)
    panel.edit_text = AsyncMock()
    telegram_bot = SimpleNamespace(
        edit_message_text=AsyncMock(return_value=panel),
    )
    message = SimpleNamespace(
        text="Tokyo Ghoul",
        bot=telegram_bot,
        from_user=SimpleNamespace(id=123),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={"panel_chat_id": 456, "panel_message_id": 789}
        ),
        clear=AsyncMock(),
    )

    await handlers.search_query(message, state)

    message.answer.assert_not_awaited()
    telegram_bot.edit_message_text.assert_awaited_once()
    assert telegram_bot.edit_message_text.await_args.kwargs["message_id"] == 789
    panel.edit_text.assert_awaited_once()
    assert "Results for" in panel.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_http_watch_selection_explains_https_requirement() -> None:
    handlers = handler("http://127.0.0.1:8080")
    event = callback()

    await handlers._send_watch_button(
        event,
        {"title": "Tokyo Ghoul"},
        3,
        back_callback="episodes:selection:0",
    )

    handlers.database.create_launch_ticket.assert_not_awaited()
    event.message.answer.assert_not_awaited()
    event.message.edit_text.assert_awaited_once()
    text = event.message.edit_text.await_args.args[0]
    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert "requires a production HTTPS URL" in text
    assert keyboard.inline_keyboard[0][0].callback_data == "episodes:selection:0"
    assert keyboard.inline_keyboard[0][1].callback_data == "menu:main"


@pytest.mark.asyncio
async def test_https_watch_selection_builds_web_app_and_back_button() -> None:
    handlers = handler("https://watch.example")
    event = callback()
    catalogue = {
        "title": "Tokyo Ghoul",
        "provider_id": "anime-sama",
        "url": "https://example.test/catalogue",
    }

    await handlers._send_watch_button(
        event,
        catalogue,
        3,
        back_callback="episodes:selection:0",
    )

    handlers.database.create_launch_ticket.assert_awaited_once()
    event.message.answer.assert_not_awaited()
    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].web_app.url == (
        "https://watch.example/app/?launch=launch-ticket"
    )
    assert keyboard.inline_keyboard[0][0].style == "success"
    assert keyboard.inline_keyboard[1][0].callback_data == "episodes:selection:0"
    assert keyboard.inline_keyboard[1][1].callback_data == "menu:main"


@pytest.mark.asyncio
async def test_episode_page_has_back_to_variant_selection() -> None:
    handlers = handler("https://watch.example")
    event = callback()
    catalogue = {
        "title": "Tokyo Ghoul",
        "provider_name": "Anime-Sama",
        "season": "Season 1",
        "language_label": "VF",
        "total_episodes": 12,
    }

    await handlers._show_episode_page(
        event,
        "catalogue-selection",
        catalogue,
        0,
        variant_selection_id="variant-selection",
    )

    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[-1][0].text == "‹ Back to seasons"
    assert keyboard.inline_keyboard[-1][0].callback_data == (
        "variants:variant-selection"
    )


@pytest.mark.asyncio
async def test_empty_continue_view_replaces_the_menu() -> None:
    handlers = handler("https://watch.example")
    handlers.database.continue_watching = AsyncMock(return_value=[])
    event = callback()

    await handlers.continue_watching(event)

    event.answer.assert_awaited_once()
    event.message.answer.assert_not_awaited()
    event.message.edit_text.assert_awaited_once()
    assert "Nothing to resume yet" in event.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_continue_view_and_watch_card_stay_in_one_message() -> None:
    handlers = handler("https://watch.example")
    entry = {
        "catalogue": {
            "title": "Tokyo Ghoul",
            "provider_name": "Anime-Sama",
        },
        "resume_episode": 3,
    }
    handlers.database.continue_watching = AsyncMock(
        side_effect=lambda _user_id, **kwargs: (
            [] if kwargs.get("status") == "completed" else [entry]
        )
    )
    handlers.database.create_selection = AsyncMock(return_value="resume-selection")
    event = callback()

    await handlers.continue_watching(event)

    event.message.answer.assert_not_awaited()
    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == (
        "continue:resume-selection"
    )
    assert keyboard.inline_keyboard[-1][0].text == "⚙ Manage list"
    assert keyboard.inline_keyboard[-1][1].text == "‹ Back"


@pytest.mark.asyncio
async def test_completed_series_are_separated_and_clearly_labelled() -> None:
    handlers = handler("https://watch.example")
    entry = {
        "catalogue": {
            "title": "Tokyo Ghoul",
            "provider_name": "Anime-Sama",
            "season": "Season 1",
            "language_label": "VF",
        },
        "resume_episode": 12,
        "status": "completed",
    }
    handlers.database.continue_watching = AsyncMock(
        side_effect=lambda _user_id, **kwargs: (
            [entry] if kwargs.get("status") == "completed" else []
        )
    )
    handlers.database.create_selection = AsyncMock(return_value="completed-selection")
    handlers.database.get_selection = AsyncMock(return_value=entry)
    event = callback()

    await handlers.continue_watching(event)

    text = event.message.edit_text.await_args.args[0]
    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert "No series are currently in progress" in text
    completed_button = keyboard.inline_keyboard[0][0]
    assert completed_button.text == "✓ Completed (1)"
    assert completed_button.style == "success"

    event.message.edit_text.reset_mock()
    await handlers.completed_watching(event)

    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    completed_entry = keyboard.inline_keyboard[0][0]
    assert completed_entry.text == "✓ Tokyo Ghoul · Season 1 · VF"
    assert completed_entry.style == "success"
    assert completed_entry.callback_data == (
        "continue:completed-entry:completed-selection"
    )

    event.message.edit_text.reset_mock()
    event.data = "continue:completed-entry:completed-selection"
    await handlers.select_completed(event)

    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert "You finished this series" in event.message.edit_text.await_args.args[0]
    assert keyboard.inline_keyboard[0][0].callback_data == (
        "continue:restart:completed:completed-selection"
    )


@pytest.mark.asyncio
async def test_active_continue_card_offers_restart_from_episode_one() -> None:
    handlers = handler("https://watch.example")
    catalogue = {
        "title": "Tokyo Ghoul",
        "provider_id": "anime-sama",
        "url": "https://example.test/tokyo-ghoul",
    }
    handlers.database.get_selection = AsyncMock(
        return_value={
            "catalogue": catalogue,
            "resume_episode": 5,
            "position": 120.0,
        }
    )
    event = callback()
    event.data = "continue:resume-selection"

    await handlers.select_continue(event)

    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].text == "▶ Watch episode 5"
    restart = keyboard.inline_keyboard[1][0]
    assert restart.text == "↻ Restart from episode 1"
    assert restart.callback_data == "continue:restart:active:resume-selection"
    assert restart.style == "primary"


@pytest.mark.asyncio
async def test_restart_requires_confirmation_then_resets_and_opens_episode_one() -> None:
    handlers = handler("https://watch.example")
    catalogue = {
        "title": "Tokyo Ghoul",
        "provider_id": "anime-sama",
        "url": "https://example.test/tokyo-ghoul",
    }
    handlers.database.get_selection = AsyncMock(
        return_value={"catalogue": catalogue}
    )
    handlers.database.restart_watch_entry = AsyncMock(return_value=True)
    event = callback()
    event.data = "continue:restart:completed:completed-selection"

    await handlers.confirm_restart(event)

    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert "Every saved episode position will be cleared" in (
        event.message.edit_text.await_args.args[0]
    )
    assert keyboard.inline_keyboard[0][0].style == "danger"
    assert keyboard.inline_keyboard[0][0].callback_data == (
        "continue:restart-confirm:completed:completed-selection"
    )
    assert keyboard.inline_keyboard[1][0].callback_data == (
        "continue:completed-entry:completed-selection"
    )

    event.answer.reset_mock()
    event.message.edit_text.reset_mock()
    event.data = "continue:restart-confirm:completed:completed-selection"
    await handlers.restart_entry(event)

    handlers.database.restart_watch_entry.assert_awaited_once_with(123, catalogue)
    event.answer.assert_awaited_once_with("Restarted from episode 1.")
    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].text == "▶ Watch episode 1"


@pytest.mark.asyncio
async def test_manage_continue_view_uses_explicit_danger_actions() -> None:
    handlers = handler("https://watch.example")
    handlers.database.continue_watching = AsyncMock(
        return_value=[
            {
                "catalogue": {
                    "title": "Tokyo Ghoul",
                    "provider_name": "Anime-Sama",
                    "season": "Season 1",
                    "language_label": "VF",
                },
                "resume_episode": 3,
            }
        ]
    )
    handlers.database.create_selection = AsyncMock(return_value="remove-selection")
    event = callback()

    await handlers.manage_continue_watching(event)

    event.answer.assert_awaited_once()
    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    remove_button = keyboard.inline_keyboard[0][0]
    assert remove_button.text == "🗑 Tokyo Ghoul · Season 1 · VF"
    assert remove_button.callback_data == "continue:remove:remove-selection"
    assert remove_button.style == "danger"
    assert keyboard.inline_keyboard[-1][0].text == "✓ Done"


@pytest.mark.asyncio
async def test_continue_removal_requires_confirmation() -> None:
    handlers = handler("https://watch.example")
    entry = {
        "catalogue": {
            "title": "Tokyo Ghoul",
            "provider_name": "Anime-Sama",
            "season": "Season 1",
            "language_label": "VF",
        }
    }
    handlers.database.get_selection = AsyncMock(return_value=entry)
    event = callback()
    event.data = "continue:remove:remove-selection"

    await handlers.confirm_continue_removal(event)

    handlers.database.get_selection.assert_awaited_once_with(
        "remove-selection",
        123,
        kind="continue_remove",
    )
    text = event.message.edit_text.await_args.args[0]
    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert "clears every saved episode position" in text
    assert keyboard.inline_keyboard[0][0].style == "danger"
    assert keyboard.inline_keyboard[0][0].callback_data == (
        "continue:delete:remove-selection"
    )
    assert keyboard.inline_keyboard[1][0].callback_data == "continue:manage"


@pytest.mark.asyncio
async def test_confirmed_continue_removal_is_user_scoped_and_refreshes_list() -> None:
    handlers = handler("https://watch.example")
    catalogue = {
        "title": "Tokyo Ghoul",
        "provider_id": "anime-sama",
        "url": "https://example.test/tokyo-ghoul",
    }
    handlers.database.get_selection = AsyncMock(
        return_value={"catalogue": catalogue}
    )
    handlers.database.remove_from_continue_watching = AsyncMock(return_value=True)
    handlers.database.continue_watching = AsyncMock(return_value=[])
    event = callback()
    event.data = "continue:delete:remove-selection"

    await handlers.remove_continue_entry(event)

    handlers.database.remove_from_continue_watching.assert_awaited_once_with(
        123,
        catalogue,
    )
    event.answer.assert_awaited_once_with("Removed from Continue Watching.")
    assert "Nothing to resume yet" in event.message.edit_text.await_args.args[0]
