from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlparse

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    WebAppInfo,
)

from anistream_telegram.config import Config
from anistream_telegram.core import CoreService
from anistream_telegram.database import Database


LOGGER = logging.getLogger(__name__)


class SearchFlow(StatesGroup):
    waiting_query = State()


class WhitelistMiddleware(BaseMiddleware):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or not await self.database.is_allowed(int(user.id)):
            return None
        return await handler(event, data)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔎 Search", callback_data="menu:search"),
                InlineKeyboardButton(
                    text="▶️ Continue Watching",
                    callback_data="menu:continue",
                ),
            ],
            [InlineKeyboardButton(text="❓ Help", callback_data="menu:help")],
        ]
    )


HELP_TEXT = (
    "AniStream Telegram lets you search enabled providers and watch inside a secure "
    "Telegram Mini App.\n\n"
    "• Search: find a title, source, season, language and episode.\n"
    "• Continue Watching: resume your saved episode and position.\n"
    "• /cancel: cancel the current search.\n\n"
    "Access is private and tied to your Telegram account."
)


class BotHandlers:
    def __init__(self, config: Config, database: Database, core: CoreService) -> None:
        self.config = config
        self.database = database
        self.core = core
        self.router = Router(name="anistream")
        middleware = WhitelistMiddleware(database)
        self.router.message.middleware(middleware)
        self.router.callback_query.middleware(middleware)
        self._register()

    def _register(self) -> None:
        self.router.message.register(self.start, CommandStart())
        self.router.message.register(self.help_command, Command("help"))
        self.router.message.register(self.cancel, Command("cancel"))
        self.router.callback_query.register(self.search_prompt, F.data == "menu:search")
        self.router.callback_query.register(self.main_menu, F.data == "menu:main")
        self.router.callback_query.register(
            self.continue_watching,
            F.data == "menu:continue",
        )
        self.router.callback_query.register(self.help_callback, F.data == "menu:help")
        self.router.callback_query.register(
            self.select_result,
            F.data.startswith("result:"),
        )
        self.router.callback_query.register(
            self.show_search_results,
            F.data.startswith("search-results:"),
        )
        self.router.callback_query.register(
            self.select_variant,
            F.data.startswith("variant:"),
        )
        self.router.callback_query.register(
            self.show_variants,
            F.data.startswith("variants:"),
        )
        self.router.callback_query.register(
            self.episode_page,
            F.data.startswith("episodes:"),
        )
        self.router.callback_query.register(
            self.select_episode,
            F.data.startswith("watch:"),
        )
        self.router.callback_query.register(
            self.select_continue,
            F.data.startswith("continue:"),
        )
        self.router.message.register(self.search_query, SearchFlow.waiting_query)

    async def start(self, message: Message, state: FSMContext) -> None:
        if message.chat.type != "private":
            return
        await state.clear()
        await message.answer(
            "Welcome to AniStream. What would you like to watch?",
            reply_markup=main_keyboard(),
        )

    async def help_command(self, message: Message) -> None:
        if message.chat.type == "private":
            await message.answer(HELP_TEXT, reply_markup=main_keyboard())

    async def help_callback(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await callback.message.answer(HELP_TEXT, reply_markup=main_keyboard())

    async def main_menu(self, callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        if callback.message:
            await callback.message.answer(
                "What would you like to watch?",
                reply_markup=main_keyboard(),
            )

    async def cancel(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Cancelled.", reply_markup=main_keyboard())

    async def search_prompt(self, callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.set_state(SearchFlow.waiting_query)
        if callback.message:
            await callback.message.answer(
                "Send the title you want to watch. Use /cancel to stop.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="⬅ Back to menu",
                                callback_data="menu:main",
                            )
                        ]
                    ]
                ),
            )

    async def search_query(self, message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        if not 2 <= len(query) <= 120:
            await message.answer("Enter a title between 2 and 120 characters.")
            return
        await state.clear()
        status = await message.answer("Searching enabled providers…")
        try:
            results, errors = await self.core.search(query)
        except Exception:
            LOGGER.exception("Provider search failed")
            await status.edit_text("Search failed temporarily. Please try again.")
            return
        if not results:
            await status.edit_text(
                "No result was found.",
                reply_markup=main_keyboard(),
            )
            return
        search_payload = {"query": query, "results": results[:20]}
        selection_id = await self.database.create_selection(
            message.from_user.id,
            "search_results",
            search_payload,
            ttl_seconds=1200,
        )
        if errors:
            LOGGER.warning("Partial provider search errors: %s", errors)
        await self._render_search_results(status, selection_id, search_payload)

    async def _render_search_results(
        self,
        message: Message,
        selection_id: str,
        payload: dict[str, Any],
    ) -> None:
        buttons: list[list[InlineKeyboardButton]] = []
        for index, item in enumerate(payload["results"]):
            label = f"{item['title']} · {item['provider_name']}"[:60]
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"result:{selection_id}:{index}",
                    )
                ]
            )
        buttons.append(
            [InlineKeyboardButton(text="⬅ Back to menu", callback_data="menu:main")]
        )
        await message.edit_text(
            f"Results for “{payload['query']}”. Choose a provider:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    async def show_search_results(self, callback: CallbackQuery) -> None:
        await callback.answer()
        selection_id = (callback.data or "").split(":", 1)[-1]
        payload = await self.database.get_selection(
            selection_id,
            callback.from_user.id,
            kind="search_results",
        )
        if payload is None:
            await self._expired(callback)
            return
        if callback.message:
            await self._render_search_results(callback.message, selection_id, payload)

    async def select_result(self, callback: CallbackQuery) -> None:
        await callback.answer()
        parts = (callback.data or "").split(":")
        search_selection_id: str | None = None
        if len(parts) == 3 and parts[2].isdigit():
            search_selection_id = parts[1]
            search_payload = await self.database.get_selection(
                search_selection_id,
                callback.from_user.id,
                kind="search_results",
            )
            index = int(parts[2])
            if (
                search_payload is None
                or index < 0
                or index >= len(search_payload.get("results", []))
            ):
                await self._expired(callback)
                return
            payload = search_payload["results"][index]
        else:
            # Compatibility with buttons created before breadcrumb navigation.
            selection_id = (callback.data or "").split(":", 1)[-1]
            payload = await self.database.get_selection(
                selection_id,
                callback.from_user.id,
                kind="search_result",
            )
            if payload is None:
                await self._expired(callback)
                return
        if callback.message:
            await callback.message.edit_text("Loading seasons and languages…")
        try:
            variants = await self.core.variants(payload["provider_id"], payload["url"])
        except Exception:
            LOGGER.exception("Variant loading failed")
            await self._error(callback, "This catalogue could not be loaded.")
            return
        variant_payload = {
            "variants": variants[:40],
            "search_selection_id": search_selection_id,
        }
        variant_selection_id = await self.database.create_selection(
            callback.from_user.id,
            "variant_list",
            variant_payload,
            ttl_seconds=1200,
        )
        if len(variants) == 1:
            await self._load_catalogue(
                callback,
                variants[0],
                variant_selection_id=variant_selection_id,
            )
            return
        await self._render_variants(callback, variant_selection_id, variant_payload)

    async def _render_variants(
        self,
        callback: CallbackQuery,
        selection_id: str,
        payload: dict[str, Any],
    ) -> None:
        buttons: list[list[InlineKeyboardButton]] = []
        for index, variant in enumerate(payload["variants"]):
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=str(variant["name"])[:60],
                        callback_data=f"variant:{selection_id}:{index}",
                    )
                ]
            )
        search_selection_id = payload.get("search_selection_id")
        if search_selection_id:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="⬅ Back to results",
                        callback_data=f"search-results:{search_selection_id}",
                    )
                ]
            )
        else:
            buttons.append(
                [InlineKeyboardButton(text="⬅ Back to menu", callback_data="menu:main")]
            )
        if callback.message:
            await callback.message.edit_text(
                "Choose a season and language:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )

    async def show_variants(self, callback: CallbackQuery) -> None:
        await callback.answer()
        selection_id = (callback.data or "").split(":", 1)[-1]
        payload = await self.database.get_selection(
            selection_id,
            callback.from_user.id,
            kind="variant_list",
        )
        if payload is None:
            await self._expired(callback)
            return
        await self._render_variants(callback, selection_id, payload)

    async def select_variant(self, callback: CallbackQuery) -> None:
        await callback.answer()
        parts = (callback.data or "").split(":")
        variant_selection_id: str | None = None
        if len(parts) == 3 and parts[2].isdigit():
            variant_selection_id = parts[1]
            variant_payload = await self.database.get_selection(
                variant_selection_id,
                callback.from_user.id,
                kind="variant_list",
            )
            index = int(parts[2])
            if (
                variant_payload is None
                or index < 0
                or index >= len(variant_payload.get("variants", []))
            ):
                await self._expired(callback)
                return
            payload = variant_payload["variants"][index]
        else:
            # Compatibility with buttons created before breadcrumb navigation.
            selection_id = (callback.data or "").split(":", 1)[-1]
            payload = await self.database.get_selection(
                selection_id,
                callback.from_user.id,
                kind="variant",
            )
            if payload is None:
                await self._expired(callback)
                return
        if callback.message:
            await callback.message.edit_text("Loading episodes…")
        await self._load_catalogue(
            callback,
            payload,
            variant_selection_id=variant_selection_id,
        )

    async def _load_catalogue(
        self,
        callback: CallbackQuery,
        variant: dict[str, Any],
        *,
        variant_selection_id: str | None = None,
    ) -> None:
        try:
            catalogue = await self.core.catalogue(
                str(variant["provider_id"]),
                str(variant["url"]),
            )
        except Exception:
            LOGGER.exception("Catalogue loading failed")
            await self._error(callback, "The episode list could not be loaded.")
            return
        selection_payload = {
            "catalogue": catalogue,
            "variant_selection_id": variant_selection_id,
        }
        selection_id = await self.database.create_selection(
            callback.from_user.id,
            "catalogue",
            selection_payload,
            ttl_seconds=1200,
        )
        await self._show_episode_page(
            callback,
            selection_id,
            catalogue,
            0,
            variant_selection_id=variant_selection_id,
        )

    async def episode_page(self, callback: CallbackQuery) -> None:
        await callback.answer()
        parts = (callback.data or "").split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            await self._expired(callback)
            return
        selection_payload = await self.database.get_selection(
            parts[1],
            callback.from_user.id,
            kind="catalogue",
        )
        if selection_payload is None:
            await self._expired(callback)
            return
        catalogue, variant_selection_id = self._catalogue_context(selection_payload)
        await self._show_episode_page(
            callback,
            parts[1],
            catalogue,
            int(parts[2]),
            variant_selection_id=variant_selection_id,
        )

    @staticmethod
    def _catalogue_context(
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        catalogue = payload.get("catalogue")
        if isinstance(catalogue, dict):
            parent = payload.get("variant_selection_id")
            return catalogue, str(parent) if parent else None
        # Compatibility with catalogue selections created before navigation.
        return payload, None

    async def _show_episode_page(
        self,
        callback: CallbackQuery,
        selection_id: str,
        catalogue: dict[str, Any],
        page: int,
        *,
        variant_selection_id: str | None = None,
    ) -> None:
        total = max(1, int(catalogue["total_episodes"]))
        page_size = 20
        max_page = (total - 1) // page_size
        page = max(0, min(max_page, page))
        start = page * page_size + 1
        end = min(total, start + page_size - 1)
        rows: list[list[InlineKeyboardButton]] = []
        current: list[InlineKeyboardButton] = []
        for episode in range(start, end + 1):
            current.append(
                InlineKeyboardButton(
                    text=str(episode),
                    callback_data=f"watch:{selection_id}:{episode}",
                )
            )
            if len(current) == 5:
                rows.append(current)
                current = []
        if current:
            rows.append(current)
        navigation: list[InlineKeyboardButton] = []
        if page > 0:
            navigation.append(
                InlineKeyboardButton(
                    text="←",
                    callback_data=f"episodes:{selection_id}:{page - 1}",
                )
            )
        if page < max_page:
            navigation.append(
                InlineKeyboardButton(
                    text="→",
                    callback_data=f"episodes:{selection_id}:{page + 1}",
                )
            )
        if navigation:
            rows.append(navigation)
        if variant_selection_id:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="⬅ Back to seasons",
                        callback_data=f"variants:{variant_selection_id}",
                    )
                ]
            )
        else:
            rows.append(
                [InlineKeyboardButton(text="⬅ Back to menu", callback_data="menu:main")]
            )
        text = (
            f"{catalogue['title']}\n"
            f"{catalogue['provider_name']} · {catalogue['season']} · "
            f"{catalogue['language_label']}\n\nChoose an episode:"
        )
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )

    async def select_episode(self, callback: CallbackQuery) -> None:
        await callback.answer()
        parts = (callback.data or "").split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            await self._expired(callback)
            return
        selection_payload = await self.database.get_selection(
            parts[1],
            callback.from_user.id,
            kind="catalogue",
        )
        if selection_payload is None:
            await self._expired(callback)
            return
        catalogue, _ = self._catalogue_context(selection_payload)
        episode = int(parts[2])
        if not 1 <= episode <= int(catalogue["total_episodes"]):
            await self._expired(callback)
            return
        await self._send_watch_button(
            callback,
            catalogue,
            episode,
            back_callback=f"episodes:{parts[1]}:{(episode - 1) // 20}",
        )

    async def continue_watching(self, callback: CallbackQuery) -> None:
        await callback.answer()
        entries = await self.database.continue_watching(callback.from_user.id)
        if not entries:
            if callback.message:
                await callback.message.answer(
                    "Your watch history is empty.",
                    reply_markup=main_keyboard(),
                )
            return
        buttons: list[list[InlineKeyboardButton]] = []
        for entry in entries:
            selection_id = await self.database.create_selection(
                callback.from_user.id,
                "continue",
                entry,
            )
            catalogue = entry["catalogue"]
            label = (
                f"▶ {catalogue['title']} · E{entry['resume_episode']} · "
                f"{catalogue['provider_name']}"
            )[:60]
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"continue:{selection_id}",
                    )
                ]
            )
        if callback.message:
            buttons.append(
                [InlineKeyboardButton(text="⬅ Back to menu", callback_data="menu:main")]
            )
            await callback.message.answer(
                "Continue watching:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )

    async def select_continue(self, callback: CallbackQuery) -> None:
        await callback.answer()
        selection_id = (callback.data or "").split(":", 1)[-1]
        entry = await self.database.get_selection(
            selection_id,
            callback.from_user.id,
            kind="continue",
        )
        if entry is None:
            await self._expired(callback)
            return
        await self._send_watch_button(
            callback,
            entry["catalogue"],
            int(entry["resume_episode"]),
            start_position=float(entry.get("position", 0.0)),
            back_callback="menu:continue",
        )

    async def _send_watch_button(
        self,
        callback: CallbackQuery,
        catalogue: dict[str, Any],
        episode: int,
        start_position: float = 0.0,
        back_callback: str | None = None,
    ) -> None:
        navigation: list[list[InlineKeyboardButton]] = []
        if back_callback:
            navigation.append(
                [
                    InlineKeyboardButton(
                        text="⬅ Back",
                        callback_data=back_callback,
                    )
                ]
            )
        navigation.append(
            [InlineKeyboardButton(text="⌂ Main menu", callback_data="menu:main")]
        )
        if urlparse(self.config.public_base_url).scheme.casefold() != "https":
            if callback.message:
                await callback.message.answer(
                    f"{catalogue['title']} · Episode {episode}\n\n"
                    "The episode is ready, but Telegram only accepts HTTPS Mini App "
                    "URLs. Start an HTTPS tunnel, set PUBLIC_BASE_URL to that URL, "
                    "set COOKIE_SECURE=true, then restart the bot.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=navigation),
                )
            return
        if start_position <= 0:
            start_position = await self.database.episode_position(
                callback.from_user.id,
                catalogue,
                episode,
            )
        ticket = await self.database.create_launch_ticket(
            callback.from_user.id,
            {
                "catalogue": catalogue,
                "episode": episode,
                "start_position": max(0.0, start_position),
            },
        )
        url = f"{self.config.public_base_url}/app/?launch={quote(ticket)}"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"▶ Watch episode {episode}",
                        web_app=WebAppInfo(url=url),
                    )
                ],
                *navigation,
            ]
        )
        if callback.message:
            await callback.message.answer(
                f"{catalogue['title']} · Episode {episode}\n"
                "This private launch link expires shortly.",
                reply_markup=keyboard,
            )

    async def _expired(self, callback: CallbackQuery) -> None:
        if callback.message:
            await callback.message.answer(
                "This selection expired. Start a new search.",
                reply_markup=main_keyboard(),
            )

    async def _error(self, callback: CallbackQuery, message: str) -> None:
        if callback.message:
            await callback.message.edit_text(message, reply_markup=main_keyboard())
