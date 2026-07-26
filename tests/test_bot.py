from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from anistream_telegram.bot import BotHandlers


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
        from_user=SimpleNamespace(id=123),
        message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()),
    )


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
    event.message.answer.assert_awaited_once()
    text = event.message.answer.await_args.args[0]
    keyboard = event.message.answer.await_args.kwargs["reply_markup"]
    assert "only accepts HTTPS" in text
    assert keyboard.inline_keyboard[0][0].callback_data == "episodes:selection:0"
    assert keyboard.inline_keyboard[1][0].callback_data == "menu:main"


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
    keyboard = event.message.answer.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].web_app.url == (
        "https://watch.example/app/?launch=launch-ticket"
    )
    assert keyboard.inline_keyboard[1][0].callback_data == "episodes:selection:0"
    assert keyboard.inline_keyboard[2][0].callback_data == "menu:main"


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
    assert keyboard.inline_keyboard[-1][0].text == "⬅ Back to seasons"
    assert keyboard.inline_keyboard[-1][0].callback_data == (
        "variants:variant-selection"
    )
