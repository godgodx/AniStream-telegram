from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

from anistream_telegram.bot import (
    MAIN_MENU_TEXT,
    BotHandlers,
    PublicIdCommandFilter,
    WhitelistMiddleware,
    button_label,
    button_label_with_suffix,
    main_keyboard,
    settings_keyboard,
    sleep_mode_keyboard,
    watchlist_keyboard,
)
from anistream_telegram.limits import SlidingWindowLimiter


def handler(public_base_url: str) -> BotHandlers:
    instance = object.__new__(BotHandlers)
    instance.config = SimpleNamespace(public_base_url=public_base_url)
    instance.database = SimpleNamespace(
        episode_position=AsyncMock(return_value=0.0),
        create_launch_ticket=AsyncMock(return_value="launch-ticket"),
        autoplay_enabled=AsyncMock(return_value=True),
        toggle_autoplay=AsyncMock(return_value=False),
        sleep_mode=AsyncMock(return_value=(False, 3)),
        toggle_sleep_mode=AsyncMock(return_value=True),
        set_sleep_mode_episodes=AsyncMock(side_effect=lambda _user_id, count: count),
        provider_states=AsyncMock(
            side_effect=lambda _user_id, provider_ids: {
                provider_id: True for provider_id in provider_ids
            }
        ),
        enabled_provider_ids=AsyncMock(
            side_effect=lambda _user_id, provider_ids: provider_ids
        ),
        toggle_provider_enabled=AsyncMock(return_value=False),
        update_selection_payload=AsyncMock(return_value=True),
        normalize_watchlist_title=lambda title: (
            " ".join(title.split()),
            " ".join(title.split()).casefold(),
        ),
        add_to_watchlist=AsyncMock(return_value="added"),
        list_watchlist=AsyncMock(return_value=[]),
        get_watchlist_entry=AsyncMock(return_value=None),
        remove_from_watchlist=AsyncMock(return_value=False),
    )
    instance.core = SimpleNamespace(
        provider_alias=lambda _provider_id: "Provider 1",
        provider_profiles=lambda: (
            {
                "provider_id": "anime_sama",
                "provider_alias": "Provider 1",
                "content_types": ("Anime",),
                "languages": ("French",),
            },
            {
                "provider_id": "french_stream",
                "provider_alias": "Provider 2",
                "content_types": ("Movies", "Series", "Anime"),
                "languages": ("French",),
            },
        ),
    )
    instance.provider_limiter = SlidingWindowLimiter(1_000, 60)
    return instance


def callback() -> SimpleNamespace:
    return SimpleNamespace(
        answer=AsyncMock(),
        bot=SimpleNamespace(delete_message=AsyncMock()),
        from_user=SimpleNamespace(id=123),
        message=SimpleNamespace(
            answer=AsyncMock(),
            edit_text=AsyncMock(),
            chat=SimpleNamespace(id=456),
            message_id=789,
        ),
    )


def telegram_message(text: str, chat_type: str = "private") -> MagicMock:
    event = MagicMock(spec=Message)
    event.text = text
    event.chat = SimpleNamespace(type=chat_type)
    return event


@pytest.mark.asyncio
async def test_unlisted_user_is_always_blocked_by_protected_middleware() -> None:
    database = SimpleNamespace(is_allowed=AsyncMock(return_value=False))
    middleware = WhitelistMiddleware(database)
    next_handler = AsyncMock(return_value="handled")
    data = {"event_from_user": SimpleNamespace(id=987654321)}

    blocked_events = [
        telegram_message("/id"),
        telegram_message("/id please"),
        telegram_message("/id\n/start"),
        telegram_message("/id@another_bot"),
        telegram_message("id"),
        telegram_message("/start"),
        telegram_message("/watchlist Tokyo Ghoul"),
        telegram_message("/id", chat_type="group"),
        MagicMock(spec=CallbackQuery),
    ]
    for event in blocked_events:
        next_handler.reset_mock()
        assert await middleware(next_handler, event, data) is None
        next_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_id_filter_accepts_only_exact_private_command() -> None:
    public_filter = PublicIdCommandFilter()

    assert await public_filter(telegram_message("/id")) is True
    assert await public_filter(telegram_message("/ID ")) is True
    assert await public_filter(telegram_message("/id please")) is False
    assert await public_filter(telegram_message("/id\n/start")) is False
    assert await public_filter(telegram_message("/id@another_bot")) is False
    assert await public_filter(telegram_message("id")) is False
    assert await public_filter(telegram_message("/id", chat_type="group")) is False


@pytest.mark.asyncio
async def test_whitelisted_user_still_reaches_commands_and_callbacks() -> None:
    database = SimpleNamespace(is_allowed=AsyncMock(return_value=True))
    middleware = WhitelistMiddleware(database)
    next_handler = AsyncMock(return_value="handled")
    data = {"event_from_user": SimpleNamespace(id=123)}

    assert (
        await middleware(next_handler, telegram_message("/start"), data)
        == "handled"
    )
    next_handler.reset_mock()
    assert (
        await middleware(next_handler, MagicMock(spec=CallbackQuery), data)
        == "handled"
    )


def test_only_id_handler_is_registered_on_public_router() -> None:
    handlers = BotHandlers(
        SimpleNamespace(public_base_url="https://watch.example"),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    public_messages = [
        item.callback.__name__ for item in handlers.public_router.message.handlers
    ]
    assert public_messages == ["id_command"]
    assert handlers.router.message.handlers == []
    assert handlers.router.callback_query.handlers == []
    assert handlers.public_router.callback_query.handlers == []
    protected_messages = {
        item.callback.__name__ for item in handlers.protected_router.message.handlers
    }
    assert {
        "start",
        "help_command",
        "watchlist_command",
        "cancel",
        "search_query",
    } <= protected_messages
    protected_callbacks = {
        item.callback.__name__
        for item in handlers.protected_router.callback_query.handlers
    }
    assert {
        "search_prompt",
        "continue_watching",
        "watchlist",
        "manage_watchlist",
        "watchlist_help",
        "delete_watchlist_entry",
        "search_watchlist_entry",
        "settings",
        "toggle_autoplay",
        "sleep_mode_settings",
        "toggle_sleep_mode",
        "set_sleep_mode_episodes",
        "manage_providers",
        "toggle_provider",
        "select_episode",
        "select_continue",
    } <= protected_callbacks


@pytest.mark.asyncio
async def test_id_command_returns_only_own_id_with_copy_button() -> None:
    handlers = handler("https://watch.example")
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=987654321),
        answer=AsyncMock(),
    )

    await handlers.id_command(message)

    text = message.answer.await_args.args[0]
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    button = keyboard.inline_keyboard[0][0]
    assert "987654321" in text
    assert button.text == "📋 Copy ID"
    assert button.copy_text.text == "987654321"
    assert button.callback_data is None
    assert button.web_app is None


def test_main_keyboard_has_clear_native_action_hierarchy() -> None:
    keyboard = main_keyboard()

    assert len(keyboard.inline_keyboard) == 3
    search, resume = keyboard.inline_keyboard[0]
    assert search.text == "🔎 Search"
    assert search.style == "primary"
    assert resume.text == "▶ Continue watching"
    assert resume.style == "primary"
    watch_list, settings = keyboard.inline_keyboard[1]
    assert watch_list.text == "⭐ Watch list"
    assert watch_list.callback_data == "menu:watchlist"
    assert settings.text == "⚙ Settings"
    assert settings.callback_data == "menu:settings"
    assert keyboard.inline_keyboard[2][0].text == "❔ Help"


def test_settings_keyboard_reflects_autoplay_state() -> None:
    enabled = settings_keyboard(True).inline_keyboard[0][0]
    disabled = settings_keyboard(False).inline_keyboard[0][0]

    assert enabled.text == "✅ Autoplay next episode · On"
    assert enabled.style == "success"
    assert disabled.text == "Autoplay next episode · Off"
    assert disabled.style is None
    assert enabled.callback_data == disabled.callback_data == "settings:autoplay"
    manage = settings_keyboard(True).inline_keyboard[2][0]
    assert manage.text == "🧩 Manage providers"
    assert manage.callback_data == "settings:providers"


def test_settings_keyboard_summarizes_sleep_mode_state() -> None:
    enabled = settings_keyboard(True, True, 3).inline_keyboard[1][0]
    disabled = settings_keyboard(True, False, 3).inline_keyboard[1][0]

    assert enabled.text == "🌙 Sleep mode · 3 episodes"
    assert enabled.style == "success"
    assert disabled.text == "🌙 Sleep mode · Off"
    assert disabled.style is None
    assert enabled.callback_data == disabled.callback_data == "settings:sleep"
    assert settings_keyboard(True, True, 1).inline_keyboard[1][0].text == (
        "🌙 Sleep mode · 1 episode"
    )


def test_sleep_mode_keyboard_marks_toggle_and_selected_episode_count() -> None:
    keyboard = sleep_mode_keyboard(True, 3)

    assert keyboard.inline_keyboard[0][0].text == "✅ Sleep mode · On"
    assert keyboard.inline_keyboard[0][0].callback_data == "settings:sleep-toggle"
    choices = [button for row in keyboard.inline_keyboard[1:3] for button in row]
    selected = next(button for button in choices if button.callback_data.endswith(":3"))
    assert selected.text == "✓ 3 episodes"
    assert selected.style == "success"
    assert keyboard.inline_keyboard[-1][0].callback_data == "menu:settings"


def test_long_button_title_preserves_anonymous_provider_suffix() -> None:
    label = button_label_with_suffix(
        "Violet Evergarden : Éternité et la Poupée de Souvenirs Automatiques",
        "Provider 2",
    )

    assert len(label) <= 60
    assert label.endswith(" · Provider 2")
    assert "…" in label


def test_search_result_title_uses_full_width_without_provider_suffix() -> None:
    label = button_label(
        "Violet Evergarden : Éternité et la Poupée de Souvenirs Automatiques"
    )

    assert len(label) <= 60
    assert "Provider" not in label
    assert label.endswith("…")


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
async def test_watch_list_button_replaces_menu_with_saved_titles() -> None:
    handlers = handler("https://watch.example")
    handlers.database.list_watchlist = AsyncMock(
        return_value=[
            {"id": 7, "title": "Tokyo Ghoul", "created_at": "2026-07-30"}
        ]
    )
    event = callback()

    await handlers.watchlist(event)

    event.answer.assert_awaited_once()
    handlers.database.list_watchlist.assert_awaited_once_with(123)
    event.message.answer.assert_not_awaited()
    event.message.edit_text.assert_awaited_once()
    text = event.message.edit_text.await_args.args[0]
    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert text.startswith("⭐ Watch list")
    assert keyboard.inline_keyboard[0][0].text == "🔎 Tokyo Ghoul"
    assert keyboard.inline_keyboard[0][0].callback_data == "watchlist:search:7"
    manage, back = keyboard.inline_keyboard[1]
    assert manage.text == "⚙ Manage list"
    assert manage.callback_data == "watchlist:manage"
    assert back.text == "‹ Back"
    assert back.callback_data == "menu:main"


def test_watchlist_manage_keyboard_is_clear_and_user_scoped_by_id() -> None:
    keyboard = watchlist_keyboard(
        [{"id": 42, "title": "A very long title " * 8}],
        manage=True,
    )

    remove = keyboard.inline_keyboard[0][0]
    assert remove.text.startswith("🗑 ")
    assert remove.text.endswith(" · Remove")
    assert len(remove.text) <= 60
    assert remove.callback_data == "watchlist:delete:42"
    assert remove.style == "danger"
    done, menu = keyboard.inline_keyboard[1]
    assert done.text == "✓ Done"
    assert done.style == "primary"
    assert done.callback_data == "watchlist:open"
    assert menu.text == "🏠 Main menu"
    assert menu.callback_data == "menu:main"


def test_empty_watchlist_uses_a_balanced_two_action_footer() -> None:
    keyboard = watchlist_keyboard([])

    assert len(keyboard.inline_keyboard) == 1
    help_button, menu = keyboard.inline_keyboard[0]
    assert help_button.text == "❔ How to add"
    assert help_button.callback_data == "watchlist:help"
    assert menu.text == "🏠 Main menu"
    assert menu.callback_data == "menu:main"


@pytest.mark.asyncio
async def test_empty_watchlist_keeps_command_on_a_short_dedicated_line() -> None:
    handlers = handler("https://watch.example")
    handlers.database.list_watchlist = AsyncMock(return_value=[])
    event = callback()

    await handlers.watchlist(event)

    assert event.message.edit_text.await_args.args[0] == (
        "⭐ Watch list\n\n"
        "No saved titles yet.\n\n"
        "Use /watchlist to add one."
    )


@pytest.mark.asyncio
async def test_watchlist_help_keeps_the_command_example_in_one_alert() -> None:
    handlers = handler("https://watch.example")
    event = callback()

    await handlers.watchlist_help(event)

    event.answer.assert_awaited_once_with(
        "Send /watchlist followed by a title. Example: /watchlist Tokyo Ghoul",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_watchlist_command_adds_a_clean_title_and_clears_search_state() -> None:
    handlers = handler("https://watch.example")
    message = SimpleNamespace(
        text="/watchlist  «  Tokyo   Ghoul  » ",
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=123),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())

    await handlers.watchlist_command(message, state)

    state.clear.assert_awaited_once()
    handlers.database.add_to_watchlist.assert_awaited_once_with(123, "Tokyo Ghoul")
    assert "was added" in message.answer.await_args.args[0]
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == "watchlist:open"


@pytest.mark.asyncio
async def test_watchlist_command_requires_a_title_without_provider_work() -> None:
    handlers = handler("https://watch.example")
    message = SimpleNamespace(
        text="/watchlist",
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=123),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())

    await handlers.watchlist_command(message, state)

    handlers.database.add_to_watchlist.assert_not_awaited()
    assert "/watchlist Tokyo Ghoul" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_manage_watchlist_removes_only_the_clicked_entry() -> None:
    handlers = handler("https://watch.example")
    handlers.database.remove_from_watchlist = AsyncMock(return_value=True)
    handlers.database.list_watchlist = AsyncMock(return_value=[])
    event = callback()
    event.data = "watchlist:delete:42"

    await handlers.delete_watchlist_entry(event)

    handlers.database.remove_from_watchlist.assert_awaited_once_with(123, 42)
    event.answer.assert_awaited_once_with("Removed from Watch list.")
    text = event.message.edit_text.await_args.args[0]
    assert text.startswith("🗑 Manage Watch list")


@pytest.mark.asyncio
async def test_watchlist_title_runs_the_existing_search_pipeline() -> None:
    handlers = handler("https://watch.example")
    handlers.database.get_watchlist_entry = AsyncMock(
        return_value={"id": 7, "title": "Tokyo Ghoul"}
    )
    handlers.database.create_selection = AsyncMock(return_value="selection")
    handlers.core.search = AsyncMock(
        return_value=(
            [
                {
                    "title": "Tokyo Ghoul",
                    "provider_id": "anime_sama",
                    "provider_alias": "Provider 1",
                    "url": "https://example.test/tokyo-ghoul",
                }
            ],
            [],
        )
    )
    event = callback()
    event.data = "watchlist:search:7"

    await handlers.search_watchlist_entry(event)

    handlers.database.get_watchlist_entry.assert_awaited_once_with(123, 7)
    handlers.core.search.assert_awaited_once_with(
        "Tokyo Ghoul",
        actor_key=123,
        provider_ids=("anime_sama", "french_stream"),
    )
    handlers.database.create_selection.assert_awaited_once()
    assert "Results for" in event.message.edit_text.await_args_list[-1].args[0]


@pytest.mark.asyncio
async def test_settings_panel_reads_saved_autoplay_state() -> None:
    handlers = handler("https://watch.example")
    handlers.database.autoplay_enabled = AsyncMock(return_value=False)
    handlers.database.sleep_mode = AsyncMock(return_value=(True, 4))
    event = callback()

    await handlers.settings(event)

    event.answer.assert_awaited_once()
    handlers.database.autoplay_enabled.assert_awaited_once_with(123)
    handlers.database.sleep_mode.assert_awaited_once_with(123)
    text = event.message.edit_text.await_args.args[0]
    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert text.startswith("⚙ Settings")
    assert keyboard.inline_keyboard[0][0].text.endswith("· Off")
    assert keyboard.inline_keyboard[1][0].text == "🌙 Sleep mode · 4 episodes"
    assert keyboard.inline_keyboard[2][0].callback_data == "settings:providers"
    assert keyboard.inline_keyboard[3][0].callback_data == "menu:main"


@pytest.mark.asyncio
async def test_settings_toggle_persists_and_refreshes_panel() -> None:
    handlers = handler("https://watch.example")
    handlers.database.toggle_autoplay = AsyncMock(return_value=False)
    event = callback()

    await handlers.toggle_autoplay(event)

    handlers.database.toggle_autoplay.assert_awaited_once_with(123)
    event.answer.assert_awaited_once_with("Autoplay disabled.")
    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].text.endswith("· Off")


@pytest.mark.asyncio
async def test_sleep_mode_panel_explains_autoplay_dependency() -> None:
    handlers = handler("https://watch.example")
    handlers.database.autoplay_enabled = AsyncMock(return_value=False)
    handlers.database.sleep_mode = AsyncMock(return_value=(True, 3))
    event = callback()

    await handlers.sleep_mode_settings(event)

    event.answer.assert_awaited_once()
    text = event.message.edit_text.await_args.args[0]
    assert "Autoplay is currently off" in text
    assert "after 3 consecutive episodes" in text
    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].text == "✅ Sleep mode · On"


@pytest.mark.asyncio
async def test_sleep_mode_toggle_persists_and_refreshes_panel() -> None:
    handlers = handler("https://watch.example")
    handlers.database.toggle_sleep_mode = AsyncMock(return_value=True)
    handlers.database.sleep_mode = AsyncMock(return_value=(True, 3))
    event = callback()

    await handlers.toggle_sleep_mode(event)

    handlers.database.toggle_sleep_mode.assert_awaited_once_with(123)
    event.answer.assert_awaited_once_with("Sleep mode enabled.")
    assert event.message.edit_text.await_args.args[0].startswith("🌙 Sleep mode")


@pytest.mark.asyncio
async def test_sleep_mode_episode_count_is_validated_and_saved() -> None:
    handlers = handler("https://watch.example")
    event = callback()
    event.data = "settings:sleep-count:5"

    await handlers.set_sleep_mode_episodes(event)

    handlers.database.set_sleep_mode_episodes.assert_awaited_once_with(123, 5)
    event.answer.assert_awaited_once_with("Sleep mode will pause after 5 episodes.")

    handlers.database.set_sleep_mode_episodes.reset_mock()
    event.answer.reset_mock()
    event.data = "settings:sleep-count:99"
    await handlers.set_sleep_mode_episodes(event)
    handlers.database.set_sleep_mode_episodes.assert_not_awaited()
    event.answer.assert_awaited_once_with(
        "Choose between 1 and 6 episodes.",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_provider_settings_are_anonymous_clear_and_enabled_by_default() -> None:
    handlers = handler("https://watch.example")
    event = callback()

    await handlers.manage_providers(event)

    event.answer.assert_awaited_once()
    handlers.database.provider_states.assert_awaited_once_with(
        123,
        ("anime_sama", "french_stream"),
    )
    text = event.message.edit_text.await_args.args[0]
    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert "Provider 1" in text
    assert "Content · Anime" in text
    assert "Provider 2" in text
    assert "Content · Movies · Series · Anime" in text
    assert text.count("Language · French") == 2
    assert "Anime-Sama" not in text
    assert "French Stream" not in text
    assert keyboard.inline_keyboard[0][0].text == "✅ Provider 1 · On"
    assert keyboard.inline_keyboard[1][0].text == "✅ Provider 2 · On"
    assert keyboard.inline_keyboard[2][0].callback_data == "menu:settings"


@pytest.mark.asyncio
async def test_provider_toggle_is_user_scoped_and_refreshes_the_panel() -> None:
    handlers = handler("https://watch.example")
    handlers.database.toggle_provider_enabled = AsyncMock(return_value=False)
    handlers.database.provider_states = AsyncMock(
        return_value={"anime_sama": False, "french_stream": True}
    )
    event = callback()
    event.data = "settings:provider:0"

    await handlers.toggle_provider(event)

    handlers.database.toggle_provider_enabled.assert_awaited_once_with(
        123,
        "anime_sama",
    )
    event.answer.assert_awaited_once_with("Provider 1 disabled.")
    keyboard = event.message.edit_text.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].text == "Provider 1 · Off"
    assert keyboard.inline_keyboard[1][0].text == "✅ Provider 2 · On"


@pytest.mark.asyncio
async def test_invalid_provider_toggle_does_not_reach_the_database() -> None:
    handlers = handler("https://watch.example")
    event = callback()
    event.data = "settings:provider:-1"

    await handlers.toggle_provider(event)

    handlers.database.toggle_provider_enabled.assert_not_awaited()
    event.answer.assert_awaited_once_with(
        "This provider is no longer available.",
        show_alert=True,
    )


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
    assert "Check the spelling carefully" in event.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_search_redirects_to_provider_settings_when_all_are_disabled() -> None:
    handlers = handler("https://watch.example")
    handlers.database.enabled_provider_ids = AsyncMock(return_value=())
    event = callback()
    state = SimpleNamespace(
        clear=AsyncMock(),
        set_state=AsyncMock(),
        update_data=AsyncMock(),
    )

    await handlers.search_prompt(event, state)

    event.answer.assert_awaited_once_with(
        "Enable at least one provider before searching.",
        show_alert=True,
    )
    state.clear.assert_awaited_once()
    state.set_state.assert_not_awaited()
    text = event.message.edit_text.await_args.args[0]
    assert text.startswith("⚙ Settings › Providers")


@pytest.mark.asyncio
async def test_search_results_are_sent_below_query_and_grouped_by_provider() -> None:
    handlers = handler("https://watch.example")
    handlers.core = SimpleNamespace(
        provider_alias=lambda _provider_id: "Provider 2",
        provider_profiles=lambda: (
            {
                "provider_id": "anime-sama",
                "provider_alias": "Provider 2",
                "content_types": ("Anime",),
                "languages": ("French",),
            },
            {
                "provider_id": "french-stream",
                "provider_alias": "Provider 1",
                "content_types": ("Movies", "Series", "Anime"),
                "languages": ("French",),
            },
        ),
        search=AsyncMock(
            return_value=(
                [
                    {
                        "title": (
                            "Violet Evergarden : Éternité et la Poupée "
                            "de Souvenirs Automatiques"
                        ),
                        "provider_name": "Anime-Sama",
                        "provider_id": "anime-sama",
                        "provider_alias": "Provider 2",
                        "url": "https://example.test/tokyo-ghoul",
                    },
                    {
                        "title": "Violet Evergarden",
                        "provider_name": "French Stream",
                        "provider_id": "french-stream",
                        "provider_alias": "Provider 1",
                        "url": "https://example.test/violet-evergarden",
                    }
                ],
                [],
            )
        )
    )
    handlers.database.create_selection = AsyncMock(return_value="search-selection")
    panel = MagicMock(spec=Message)
    panel.edit_text = AsyncMock()
    panel.chat = SimpleNamespace(id=456)
    panel.message_id = 790
    second_panel = MagicMock(spec=Message)
    second_panel.chat = SimpleNamespace(id=456)
    second_panel.message_id = 791
    panel.answer = AsyncMock(return_value=second_panel)
    telegram_bot = SimpleNamespace(
        delete_message=AsyncMock(),
    )
    message = SimpleNamespace(
        text="Tokyo Ghoul",
        bot=telegram_bot,
        from_user=SimpleNamespace(id=123),
        answer=AsyncMock(return_value=panel),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={"panel_chat_id": 456, "panel_message_id": 789}
        ),
        clear=AsyncMock(),
    )

    await handlers.search_query(message, state)

    telegram_bot.delete_message.assert_awaited_once_with(
        chat_id=456,
        message_id=789,
    )
    message.answer.assert_awaited_once()
    assert "Searching for" in message.answer.await_args.args[0]
    panel.edit_text.assert_awaited_once()
    assert "Results for" in panel.edit_text.await_args.args[0]
    assert "Provider 1" in panel.edit_text.await_args.args[0]
    assert "Content · Movies · Series · Anime" in panel.edit_text.await_args.args[0]
    assert "Language · French" in panel.edit_text.await_args.args[0]
    first_keyboard = panel.edit_text.await_args.kwargs["reply_markup"]
    assert first_keyboard.inline_keyboard[0][0].text == "Violet Evergarden"
    assert "Provider" not in first_keyboard.inline_keyboard[0][0].text

    panel.answer.assert_awaited_once()
    assert "Provider 2" in panel.answer.await_args.args[0]
    second_keyboard = panel.answer.await_args.kwargs["reply_markup"]
    result_label = second_keyboard.inline_keyboard[0][0].text
    assert "Provider" not in result_label
    assert len(result_label) <= 60
    handlers.core.search.assert_awaited_once_with(
        "Tokyo Ghoul",
        actor_key=123,
        provider_ids=("anime-sama", "french-stream"),
    )
    handlers.database.update_selection_payload.assert_awaited_once()
    saved_payload = handlers.database.update_selection_payload.await_args.args[2]
    assert saved_payload["message_ids"] == [
        {"chat_id": 456, "message_id": 790},
        {"chat_id": 456, "message_id": 791},
    ]


@pytest.mark.asyncio
async def test_search_rate_limit_rejects_before_provider_work() -> None:
    handlers = handler("https://watch.example")
    handlers.provider_limiter = SlidingWindowLimiter(1, 60)
    assert await handlers.provider_limiter.allow("123") is True
    handlers.core = SimpleNamespace(
        provider_alias=lambda _provider_id: "Provider 1",
        provider_profiles=lambda: (
            {
                "provider_id": "anime_sama",
                "provider_alias": "Provider 1",
                "content_types": ("Anime",),
                "languages": ("French",),
            },
        ),
        search=AsyncMock(),
    )
    panel = MagicMock(spec=Message)
    panel.chat = SimpleNamespace(id=456)
    panel.message_id = 790
    telegram_bot = SimpleNamespace(delete_message=AsyncMock())
    message = SimpleNamespace(
        text="Tokyo Ghoul",
        bot=telegram_bot,
        from_user=SimpleNamespace(id=123),
        answer=AsyncMock(return_value=panel),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={"panel_chat_id": 456, "panel_message_id": 789}
        ),
        clear=AsyncMock(),
    )

    await handlers.search_query(message, state)

    handlers.core.search.assert_not_awaited()
    state.clear.assert_awaited_once()
    assert "Too many provider requests" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_search_menu_removes_sibling_provider_blocks() -> None:
    handlers = handler("https://watch.example")
    handlers.database.get_selection = AsyncMock(
        return_value={
            "query": "Tokyo Ghoul",
            "results": [],
            "message_ids": [
                {"chat_id": 456, "message_id": 788},
                {"chat_id": 456, "message_id": 789},
            ],
        }
    )
    event = callback()
    event.data = "search-menu:search-selection"

    await handlers.search_results_menu(event)

    event.bot.delete_message.assert_awaited_once_with(
        chat_id=456,
        message_id=788,
    )
    event.message.edit_text.assert_awaited_once()
    assert event.message.edit_text.await_args.args[0] == MAIN_MENU_TEXT


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
    launch_payload = handlers.database.create_launch_ticket.await_args.args[1]
    assert launch_payload["_menu_chat_id"] == 456
    assert launch_payload["_menu_message_id"] == 789
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
    text = event.message.edit_text.await_args.args[0]
    assert "Provider 1 · Season 1 · VF" in text
    assert "Anime-Sama" not in text
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
    assert keyboard.inline_keyboard[0][0].text.endswith(" · Provider 1")
    assert "Anime-Sama" not in keyboard.inline_keyboard[0][0].text
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
    assert completed_entry.text == (
        "✓ Tokyo Ghoul · Season 1 · VF · Provider 1"
    )
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
    assert remove_button.text == (
        "🗑 Tokyo Ghoul · Season 1 · VF · Provider 1"
    )
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
