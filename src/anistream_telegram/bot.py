from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlparse

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    WebAppInfo,
)

from anistream_telegram.config import Config
from anistream_telegram.core import CoreService
from anistream_telegram.database import Database
from anistream_telegram.limits import CapacityExceeded, SlidingWindowLimiter


LOGGER = logging.getLogger(__name__)


class SearchFlow(StatesGroup):
    waiting_query = State()


class PublicIdCommandFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return (
            message.chat.type == "private"
            and (message.text or "").strip().casefold() == "/id"
        )


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
                InlineKeyboardButton(
                    text="🔎 Search",
                    callback_data="menu:search",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="▶ Continue watching",
                    callback_data="menu:continue",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Watch list",
                    callback_data="menu:watchlist",
                ),
                InlineKeyboardButton(
                    text="⚙ Settings",
                    callback_data="menu:settings",
                ),
            ],
            [InlineKeyboardButton(text="❔ Help", callback_data="menu:help")],
        ]
    )


MAIN_MENU_TEXT = "🎬 AniStream\n\nWhat would you like to watch?"
BUTTON_TEXT_LIMIT = 60


def button_label_with_suffix(
    title: object,
    suffix: object,
    *,
    limit: int = BUTTON_TEXT_LIMIT,
) -> str:
    clean_title = " ".join(str(title).split())
    clean_suffix = " ".join(str(suffix).split())
    separator = " · "
    full_label = f"{clean_title}{separator}{clean_suffix}"
    if len(full_label) <= limit:
        return full_label
    available = limit - len(separator) - len(clean_suffix)
    if available <= 1:
        return clean_suffix[:limit]
    shortened = clean_title[: available - 1].rstrip()
    return f"{shortened}…{separator}{clean_suffix}"


def button_label(title: object, *, limit: int = BUTTON_TEXT_LIMIT) -> str:
    clean_title = " ".join(str(title).split())
    if len(clean_title) <= limit:
        return clean_title
    return clean_title[: max(1, limit - 1)].rstrip() + "…"


def is_anonymous_provider_alias(value: object) -> bool:
    alias = str(value).strip()
    if alias == "Provider":
        return True
    number = alias.removeprefix("Provider ")
    return number.isdigit() and int(number) > 0


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="‹ Back to menu",
                    callback_data="menu:main",
                )
            ]
        ]
    )


def watchlist_keyboard(
    entries: list[dict[str, Any]],
    *,
    manage: bool = False,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for entry in entries:
        entry_id = int(entry["id"])
        title = button_label_with_suffix(
            f"🗑 {entry['title']}" if manage else f"🔎 {entry['title']}",
            "Remove" if manage else "Search",
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=title,
                    callback_data=(
                        f"watchlist:delete:{entry_id}"
                        if manage
                        else f"watchlist:search:{entry_id}"
                    ),
                    style="danger" if manage else None,
                )
            ]
        )
    if manage:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="✓ Done",
                    callback_data="watchlist:open",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="🏠 Main menu",
                    callback_data="menu:main",
                ),
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=buttons)
    if entries:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="⚙ Manage list",
                    callback_data="watchlist:manage",
                ),
                InlineKeyboardButton(
                    text="‹ Back",
                    callback_data="menu:main",
                ),
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=buttons)
    buttons.append(
        [
            InlineKeyboardButton(
                text="❔ How to add",
                callback_data="watchlist:help",
            ),
            InlineKeyboardButton(
                text="🏠 Main menu",
                callback_data="menu:main",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def watchlist_added_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⭐ Open Watch list",
                    callback_data="watchlist:open",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⌂ Main menu",
                    callback_data="menu:main",
                )
            ],
        ]
    )


def settings_keyboard(autoplay_enabled: bool) -> InlineKeyboardMarkup:
    toggle = InlineKeyboardButton(
        text=(
            "✅ Autoplay next episode · On"
            if autoplay_enabled
            else "Autoplay next episode · Off"
        ),
        callback_data="settings:autoplay",
        style="success" if autoplay_enabled else None,
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [toggle],
            [
                InlineKeyboardButton(
                    text="🧩 Manage providers",
                    callback_data="settings:providers",
                )
            ],
            [
                InlineKeyboardButton(
                    text="‹ Back to menu",
                    callback_data="menu:main",
                )
            ],
        ]
    )


def provider_management_keyboard(
    profiles: tuple[dict[str, Any], ...],
    states: dict[str, bool],
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for index, profile in enumerate(profiles):
        provider_id = str(profile["provider_id"])
        alias = str(profile["provider_alias"])
        enabled = states.get(provider_id, True)
        buttons.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"✅ {alias} · On"
                        if enabled
                        else f"{alias} · Off"
                    ),
                    callback_data=f"settings:provider:{index}",
                    style="success" if enabled else None,
                )
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton(
                text="‹ Back to settings",
                callback_data="menu:settings",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def provider_required_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🧩 Manage providers",
                    callback_data="settings:providers",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="‹ Back to menu",
                    callback_data="menu:main",
                )
            ],
        ]
    )


def cancel_search_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="‹ Cancel",
                    callback_data="menu:main",
                )
            ]
        ]
    )


HELP_TEXT = (
    "❔ AniStream help\n\n"
    "🔎 Search\n"
    "Find a title, then choose its provider, season, language and episode.\n\n"
    "▶ Continue watching\n"
    "Resume a saved episode from your last position.\n\n"
    "⭐ Watch list\n"
    "Save a title with /watchlist <title>, then search it again in one tap.\n\n"
    "🔒 Private access\n"
    "Playback is tied to your Telegram account and opens in the secure Mini App.\n\n"
    "Use /cancel at any time while entering a search."
)


class BotHandlers:
    def __init__(self, config: Config, database: Database, core: CoreService) -> None:
        self.config = config
        self.database = database
        self.core = core
        self.provider_limiter = SlidingWindowLimiter(10, 60)
        self.router = Router(name="anistream")
        self.public_router = Router(name="anistream-public")
        self.protected_router = Router(name="anistream-protected")
        middleware = WhitelistMiddleware(database)
        self.protected_router.message.middleware(middleware)
        self.protected_router.callback_query.middleware(middleware)
        self._register()
        self.router.include_router(self.public_router)
        self.router.include_router(self.protected_router)

    def _register(self) -> None:
        self.public_router.message.register(
            self.id_command,
            PublicIdCommandFilter(),
        )
        self.protected_router.message.register(self.start, CommandStart())
        self.protected_router.message.register(self.help_command, Command("help"))
        self.protected_router.message.register(
            self.watchlist_command,
            Command("watchlist"),
        )
        self.protected_router.message.register(self.cancel, Command("cancel"))
        self.protected_router.callback_query.register(
            self.search_prompt,
            F.data == "menu:search",
        )
        self.protected_router.callback_query.register(
            self.main_menu,
            F.data == "menu:main",
        )
        self.protected_router.callback_query.register(
            self.continue_watching,
            F.data == "menu:continue",
        )
        self.protected_router.callback_query.register(
            self.watchlist,
            (F.data == "menu:watchlist") | (F.data == "watchlist:open"),
        )
        self.protected_router.callback_query.register(
            self.manage_watchlist,
            F.data == "watchlist:manage",
        )
        self.protected_router.callback_query.register(
            self.watchlist_help,
            F.data == "watchlist:help",
        )
        self.protected_router.callback_query.register(
            self.delete_watchlist_entry,
            F.data.startswith("watchlist:delete:"),
        )
        self.protected_router.callback_query.register(
            self.search_watchlist_entry,
            F.data.startswith("watchlist:search:"),
        )
        self.protected_router.callback_query.register(
            self.settings,
            F.data == "menu:settings",
        )
        self.protected_router.callback_query.register(
            self.toggle_autoplay,
            F.data == "settings:autoplay",
        )
        self.protected_router.callback_query.register(
            self.manage_providers,
            F.data == "settings:providers",
        )
        self.protected_router.callback_query.register(
            self.toggle_provider,
            F.data.startswith("settings:provider:"),
        )
        self.protected_router.callback_query.register(
            self.help_callback,
            F.data == "menu:help",
        )
        self.protected_router.callback_query.register(
            self.select_result,
            F.data.startswith("result:"),
        )
        self.protected_router.callback_query.register(
            self.show_search_results,
            F.data.startswith("search-results:"),
        )
        self.protected_router.callback_query.register(
            self.search_results_menu,
            F.data.startswith("search-menu:"),
        )
        self.protected_router.callback_query.register(
            self.select_variant,
            F.data.startswith("variant:"),
        )
        self.protected_router.callback_query.register(
            self.show_variants,
            F.data.startswith("variants:"),
        )
        self.protected_router.callback_query.register(
            self.episode_page,
            F.data.startswith("episodes:"),
        )
        self.protected_router.callback_query.register(
            self.select_episode,
            F.data.startswith("watch:"),
        )
        self.protected_router.callback_query.register(
            self.manage_continue_watching,
            F.data == "continue:manage",
        )
        self.protected_router.callback_query.register(
            self.completed_watching,
            F.data == "continue:completed",
        )
        self.protected_router.callback_query.register(
            self.select_completed,
            F.data.startswith("continue:completed-entry:"),
        )
        self.protected_router.callback_query.register(
            self.confirm_restart,
            F.data.startswith("continue:restart:"),
        )
        self.protected_router.callback_query.register(
            self.restart_entry,
            F.data.startswith("continue:restart-confirm:"),
        )
        self.protected_router.callback_query.register(
            self.confirm_continue_removal,
            F.data.startswith("continue:remove:"),
        )
        self.protected_router.callback_query.register(
            self.remove_continue_entry,
            F.data.startswith("continue:delete:"),
        )
        self.protected_router.callback_query.register(
            self.select_continue,
            F.data.startswith("continue:"),
        )
        self.protected_router.message.register(
            self.search_query,
            SearchFlow.waiting_query,
        )

    async def id_command(self, message: Message) -> None:
        if message.chat.type != "private" or message.from_user is None:
            return
        user_id = str(message.from_user.id)
        await message.answer(
            "🪪 Your Telegram ID\n\n"
            f"{user_id}\n\n"
            "Send this number to the AniStream administrator to request access.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📋 Copy ID",
                            copy_text=CopyTextButton(text=user_id),
                            style="primary",
                        )
                    ]
                ]
            ),
        )

    def _provider_alias(self, payload: dict[str, Any]) -> str:
        alias = str(payload.get("provider_alias", "")).strip()
        if is_anonymous_provider_alias(alias):
            return alias
        resolver = getattr(self.core, "provider_alias", None)
        if callable(resolver):
            alias = str(resolver(str(payload.get("provider_id", "")))).strip()
            if is_anonymous_provider_alias(alias):
                return alias
        return "Provider"

    def _provider_profiles(self) -> tuple[dict[str, Any], ...]:
        resolver = getattr(self.core, "provider_profiles", None)
        if not callable(resolver):
            return ()
        profiles: list[dict[str, Any]] = []
        for raw in resolver():
            provider_id = str(raw.get("provider_id", "")).strip()
            if not provider_id:
                continue
            alias = self._provider_alias(
                {
                    "provider_id": provider_id,
                    "provider_alias": raw.get("provider_alias", ""),
                }
            )
            content_types = tuple(
                str(item).strip()
                for item in raw.get("content_types", ())
                if str(item).strip()
            )
            languages = tuple(
                str(item).strip()
                for item in raw.get("languages", ())
                if str(item).strip()
            )
            profiles.append(
                {
                    "provider_id": provider_id,
                    "provider_alias": alias,
                    "content_types": content_types or ("Not specified",),
                    "languages": languages or ("Not specified",),
                }
            )
        return tuple(profiles)

    @staticmethod
    def _provider_profile_text(profile: dict[str, Any]) -> str:
        return (
            f"🎞 {profile['provider_alias']}\n"
            f"🎬 Content · {' · '.join(profile['content_types'])}\n"
            f"🌐 Language · {' · '.join(profile['languages'])}"
        )

    @staticmethod
    async def _replace_callback_message(
        callback: CallbackQuery,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        if not callback.message:
            return
        try:
            await callback.message.edit_text(text, reply_markup=reply_markup)
        except TelegramBadRequest as exc:
            # Old duplicate menu messages can still be clicked after a deployment.
            # Treat an already-current panel as a successful navigation.
            if "message is not modified" not in str(exc).casefold():
                raise

    @staticmethod
    async def _edit_flow_panel(
        message: Message,
        flow_data: dict[str, Any],
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message:
        chat_id = flow_data.get("panel_chat_id")
        message_id = flow_data.get("panel_message_id")
        if isinstance(chat_id, int) and isinstance(message_id, int):
            try:
                await message.bot.delete_message(
                    chat_id=chat_id,
                    message_id=message_id,
                )
            except TelegramBadRequest:
                LOGGER.info("Previous search panel could not be removed")
        return await message.answer(text, reply_markup=reply_markup)

    async def start(self, message: Message, state: FSMContext) -> None:
        if message.chat.type != "private":
            return
        await state.clear()
        await message.answer(
            MAIN_MENU_TEXT,
            reply_markup=main_keyboard(),
        )

    async def help_command(self, message: Message) -> None:
        if message.chat.type == "private":
            await message.answer(HELP_TEXT, reply_markup=back_to_menu_keyboard())

    async def help_callback(self, callback: CallbackQuery) -> None:
        await callback.answer()
        await self._replace_callback_message(
            callback,
            HELP_TEXT,
            back_to_menu_keyboard(),
        )

    async def watchlist_command(self, message: Message, state: FSMContext) -> None:
        if message.chat.type != "private" or message.from_user is None:
            return
        await state.clear()
        parts = (message.text or "").split(maxsplit=1)
        title = parts[1].strip() if len(parts) == 2 else ""
        if len(title) >= 2 and (title[0], title[-1]) in {
            ('"', '"'),
            ("“", "”"),
            ("«", "»"),
        }:
            title = title[1:-1].strip()
        clean_title, _ = self.database.normalize_watchlist_title(title)
        if not 2 <= len(clean_title) <= 120:
            await message.answer(
                "⭐ Add to Watch list\n\n"
                "Use /watchlist followed by a movie or series title.\n\n"
                "Example:\n/watchlist Tokyo Ghoul",
                reply_markup=watchlist_added_keyboard(),
            )
            return

        result = await self.database.add_to_watchlist(
            message.from_user.id,
            clean_title,
        )
        if result == "forbidden":
            return
        if result == "full":
            text = (
                "⚠ Your Watch list is full.\n\n"
                "Open Manage and remove a title before adding another."
            )
        elif result == "exists":
            text = f"⭐ “{clean_title}” is already in your Watch list."
        else:
            text = f"✅ “{clean_title}” was added to your Watch list."
        await message.answer(text, reply_markup=watchlist_added_keyboard())

    async def watchlist(self, callback: CallbackQuery) -> None:
        await callback.answer()
        await self._render_watchlist(callback)

    async def manage_watchlist(self, callback: CallbackQuery) -> None:
        await callback.answer()
        await self._render_watchlist(callback, manage=True)

    async def watchlist_help(self, callback: CallbackQuery) -> None:
        await callback.answer(
            "Send /watchlist followed by a title. Example: /watchlist Tokyo Ghoul",
            show_alert=True,
        )

    async def _render_watchlist(
        self,
        callback: CallbackQuery,
        *,
        manage: bool = False,
    ) -> None:
        entries = await self.database.list_watchlist(callback.from_user.id)
        if manage:
            text = (
                "🗑 Manage Watch list\n\n"
                "Choose a title to remove:"
                if entries
                else
                "🗑 Manage Watch list\n\nNo saved titles to remove."
            )
        else:
            text = (
                "⭐ Watch list\n\nChoose a title to search:"
                if entries
                else
                "⭐ Watch list\n\n"
                "No saved titles yet. Add one anytime with /watchlist."
            )
        await self._replace_callback_message(
            callback,
            text,
            watchlist_keyboard(entries, manage=manage),
        )

    async def delete_watchlist_entry(self, callback: CallbackQuery) -> None:
        try:
            entry_id = int((callback.data or "").rsplit(":", 1)[1])
            if entry_id <= 0:
                raise ValueError
        except (IndexError, ValueError):
            await callback.answer("This Watch list entry is invalid.", show_alert=True)
            return
        removed = await self.database.remove_from_watchlist(
            callback.from_user.id,
            entry_id,
        )
        await callback.answer(
            "Removed from Watch list." if removed else "Already removed."
        )
        await self._render_watchlist(callback, manage=True)

    async def search_watchlist_entry(self, callback: CallbackQuery) -> None:
        try:
            entry_id = int((callback.data or "").rsplit(":", 1)[1])
            if entry_id <= 0:
                raise ValueError
        except (IndexError, ValueError):
            await callback.answer("This Watch list entry is invalid.", show_alert=True)
            return
        entry = await self.database.get_watchlist_entry(
            callback.from_user.id,
            entry_id,
        )
        if entry is None or callback.message is None:
            await callback.answer(
                "This title is no longer in your Watch list.",
                show_alert=True,
            )
            return

        profiles = self._provider_profiles()
        provider_ids = tuple(str(profile["provider_id"]) for profile in profiles)
        enabled_provider_ids = await self.database.enabled_provider_ids(
            callback.from_user.id,
            provider_ids,
        )
        if not enabled_provider_ids:
            await callback.answer(
                "Enable at least one provider before searching.",
                show_alert=True,
            )
            await self._render_provider_settings(callback, profiles=profiles)
            return
        if not await self.provider_limiter.allow(str(callback.from_user.id)):
            await callback.answer(
                "Too many provider requests. Please wait a minute.",
                show_alert=True,
            )
            return

        await callback.answer()
        query = str(entry["title"])
        await callback.message.edit_text(f"🔎 Searching for “{query}”…")
        await self._execute_search(
            callback.message,
            callback.from_user.id,
            query,
            enabled_provider_ids,
        )

    async def settings(self, callback: CallbackQuery) -> None:
        await callback.answer()
        await self._render_settings(callback)

    async def toggle_autoplay(self, callback: CallbackQuery) -> None:
        enabled = await self.database.toggle_autoplay(callback.from_user.id)
        await callback.answer(
            "Autoplay enabled." if enabled else "Autoplay disabled."
        )
        await self._render_settings(callback, autoplay_enabled=enabled)

    async def manage_providers(self, callback: CallbackQuery) -> None:
        await callback.answer()
        await self._render_provider_settings(callback)

    async def toggle_provider(self, callback: CallbackQuery) -> None:
        profiles = self._provider_profiles()
        try:
            index = int((callback.data or "").rsplit(":", 1)[1])
            if index < 0:
                raise IndexError
            profile = profiles[index]
        except (IndexError, TypeError, ValueError):
            await callback.answer(
                "This provider is no longer available.",
                show_alert=True,
            )
            return

        enabled = await self.database.toggle_provider_enabled(
            callback.from_user.id,
            str(profile["provider_id"]),
        )
        await callback.answer(
            f"{profile['provider_alias']} "
            f"{'enabled' if enabled else 'disabled'}.",
        )
        await self._render_provider_settings(callback, profiles=profiles)

    async def _render_provider_settings(
        self,
        callback: CallbackQuery,
        *,
        profiles: tuple[dict[str, Any], ...] | None = None,
    ) -> None:
        profiles = profiles if profiles is not None else self._provider_profiles()
        provider_ids = tuple(str(profile["provider_id"]) for profile in profiles)
        states = await self.database.provider_states(
            callback.from_user.id,
            provider_ids,
        )
        cards = "\n\n".join(
            self._provider_profile_text(profile)
            for profile in profiles
        )
        await self._replace_callback_message(
            callback,
            "⚙ Settings › Providers\n\n"
            "Choose which providers AniStream searches. "
            "All providers are enabled by default."
            + (f"\n\n{cards}" if cards else "\n\nNo providers are available."),
            provider_management_keyboard(profiles, states),
        )

    async def _render_settings(
        self,
        callback: CallbackQuery,
        *,
        autoplay_enabled: bool | None = None,
    ) -> None:
        if autoplay_enabled is None:
            autoplay_enabled = await self.database.autoplay_enabled(
                callback.from_user.id
            )
        await self._replace_callback_message(
            callback,
            "⚙ Settings\n\n"
            "Autoplay next episode\n"
            "Automatically start the next episode when the current one ends.\n\n"
            "Providers\n"
            "Choose which catalogues are included in your searches.\n\n"
            "These preferences are saved to your Telegram account.",
            settings_keyboard(autoplay_enabled),
        )

    async def main_menu(self, callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        await self._replace_callback_message(
            callback,
            MAIN_MENU_TEXT,
            main_keyboard(),
        )

    async def cancel(self, message: Message, state: FSMContext) -> None:
        flow_data = await state.get_data()
        await state.clear()
        await self._edit_flow_panel(
            message,
            flow_data,
            MAIN_MENU_TEXT,
            main_keyboard(),
        )

    async def search_prompt(self, callback: CallbackQuery, state: FSMContext) -> None:
        profiles = self._provider_profiles()
        provider_ids = tuple(str(profile["provider_id"]) for profile in profiles)
        enabled_provider_ids = await self.database.enabled_provider_ids(
            callback.from_user.id,
            provider_ids,
        )
        if not enabled_provider_ids:
            await callback.answer(
                "Enable at least one provider before searching.",
                show_alert=True,
            )
            await state.clear()
            await self._render_provider_settings(callback, profiles=profiles)
            return

        await callback.answer()
        await state.set_state(SearchFlow.waiting_query)
        if callback.message:
            await state.update_data(
                panel_chat_id=callback.message.chat.id,
                panel_message_id=callback.message.message_id,
            )
        await self._replace_callback_message(
            callback,
            "🔎 Search\n\n"
            "Send the title you want to watch.\n\n"
            "💡 Check the spelling carefully so providers can find the "
            "right movie or series.",
            cancel_search_keyboard(),
        )

    async def search_query(self, message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        flow_data = await state.get_data()
        if not 2 <= len(query) <= 120:
            panel = await self._edit_flow_panel(
                message,
                flow_data,
                "🔎 Search\n\nEnter a title between 2 and 120 characters.",
                cancel_search_keyboard(),
            )
            await state.update_data(
                panel_chat_id=panel.chat.id,
                panel_message_id=panel.message_id,
            )
            return
        profiles = self._provider_profiles()
        provider_ids = tuple(str(profile["provider_id"]) for profile in profiles)
        enabled_provider_ids = await self.database.enabled_provider_ids(
            message.from_user.id,
            provider_ids,
        )
        if not enabled_provider_ids:
            await state.clear()
            await self._edit_flow_panel(
                message,
                flow_data,
                "⚠ No providers are enabled.\n\n"
                "Open Manage providers and enable at least one provider.",
                provider_required_keyboard(),
            )
            return
        if not await self.provider_limiter.allow(str(message.from_user.id)):
            await state.clear()
            await self._edit_flow_panel(
                message,
                flow_data,
                "⚠ Too many provider requests.\n\nPlease wait a minute and try again.",
                main_keyboard(),
            )
            return
        await state.clear()
        status = await self._edit_flow_panel(
            message,
            flow_data,
            f"🔎 Searching for “{query}”…",
        )
        await self._execute_search(
            status,
            message.from_user.id,
            query,
            enabled_provider_ids,
        )

    async def _execute_search(
        self,
        status: Message,
        user_id: int,
        query: str,
        enabled_provider_ids: tuple[str, ...],
    ) -> None:
        try:
            results, errors = await self.core.search(
                query,
                actor_key=user_id,
                provider_ids=enabled_provider_ids,
            )
        except CapacityExceeded:
            await status.edit_text(
                "⌛ Another provider request is already running.\n\nPlease try again shortly.",
                reply_markup=main_keyboard(),
            )
            return
        except Exception:
            LOGGER.exception("Provider search failed")
            await status.edit_text(
                "⚠ Search is temporarily unavailable.\n\nPlease try again.",
                reply_markup=main_keyboard(),
            )
            return
        if not results:
            await status.edit_text(
                f"🔎 No results for “{query}”.\n\nTry another title.",
                reply_markup=main_keyboard(),
            )
            return
        search_payload = {"query": query, "results": results[:20]}
        selection_id = await self.database.create_selection(
            user_id,
            "search_results",
            search_payload,
            ttl_seconds=1200,
        )
        if errors:
            LOGGER.warning("Partial provider search errors: %s", errors)
        await self._render_search_results(
            status,
            selection_id,
            search_payload,
            user_id,
        )

    async def _render_search_results(
        self,
        message: Message,
        selection_id: str,
        payload: dict[str, Any],
        user_id: int,
        provider_id: str | None = None,
    ) -> None:
        groups: dict[str, tuple[str, list[tuple[int, dict[str, Any]]]]] = {}
        for index, item in enumerate(payload["results"]):
            item_provider_id = str(item.get("provider_id", ""))
            if provider_id is not None and item_provider_id != provider_id:
                continue
            alias = self._provider_alias(item)
            group = groups.setdefault(item_provider_id, (alias, []))
            group[1].append((index, item))

        def provider_order(
            entry: tuple[str, tuple[str, list[tuple[int, dict[str, Any]]]]],
        ) -> tuple[int, str]:
            alias = entry[1][0]
            number = alias.removeprefix("Provider ")
            return (int(number) if number.isdigit() else 10_000, alias)

        ordered_groups = sorted(groups.items(), key=provider_order)
        profiles = {
            str(profile["provider_id"]): profile
            for profile in self._provider_profiles()
        }
        rendered_messages: list[Message] = []
        for group_index, (item_provider_id, (alias, results)) in enumerate(
            ordered_groups
        ):
            buttons = [
                [
                    InlineKeyboardButton(
                        text=button_label(item["title"]),
                        callback_data=f"result:{selection_id}:{index}",
                    )
                ]
                for index, item in results
            ]
            if group_index == len(ordered_groups) - 1:
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text="‹ Back to menu",
                            callback_data=f"search-menu:{selection_id}",
                        )
                    ]
                )
            profile = profiles.get(
                item_provider_id,
                {
                    "provider_alias": alias,
                    "content_types": ("Not specified",),
                    "languages": ("Not specified",),
                },
            )
            text = (
                f"🔎 Results for “{payload['query']}”\n\n"
                f"{self._provider_profile_text(profile)}"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            if group_index == 0:
                await message.edit_text(text, reply_markup=keyboard)
                rendered_messages.append(message)
            else:
                rendered_messages.append(
                    await message.answer(text, reply_markup=keyboard)
                )

        payload["message_ids"] = [
            {
                "chat_id": item.chat.id,
                "message_id": item.message_id,
            }
            for item in rendered_messages
        ]
        await self.database.update_selection_payload(
            selection_id,
            user_id,
            payload,
            kind="search_results",
        )

    @staticmethod
    async def _delete_search_siblings(
        callback: CallbackQuery,
        payload: dict[str, Any],
    ) -> None:
        if callback.message is None:
            return
        current = (callback.message.chat.id, callback.message.message_id)
        for item in payload.get("message_ids", []):
            if not isinstance(item, dict):
                continue
            chat_id = item.get("chat_id")
            message_id = item.get("message_id")
            if (
                not isinstance(chat_id, int)
                or not isinstance(message_id, int)
                or (chat_id, message_id) == current
            ):
                continue
            try:
                await callback.bot.delete_message(
                    chat_id=chat_id,
                    message_id=message_id,
                )
            except TelegramBadRequest:
                LOGGER.info("A sibling search result block was already removed")

    async def show_search_results(self, callback: CallbackQuery) -> None:
        await callback.answer()
        parts = (callback.data or "").split(":", 2)
        selection_id = parts[1] if len(parts) >= 2 else ""
        provider_id = parts[2] if len(parts) == 3 else None
        payload = await self.database.get_selection(
            selection_id,
            callback.from_user.id,
            kind="search_results",
        )
        if payload is None:
            await self._expired(callback)
            return
        if callback.message:
            await self._delete_search_siblings(callback, payload)
            await self._render_search_results(
                callback.message,
                selection_id,
                payload,
                callback.from_user.id,
                provider_id,
            )

    async def search_results_menu(self, callback: CallbackQuery) -> None:
        await callback.answer()
        selection_id = (callback.data or "").split(":", 1)[-1]
        payload = await self.database.get_selection(
            selection_id,
            callback.from_user.id,
            kind="search_results",
        )
        if payload is not None:
            await self._delete_search_siblings(callback, payload)
        await self._replace_callback_message(
            callback,
            MAIN_MENU_TEXT,
            main_keyboard(),
        )

    async def select_result(self, callback: CallbackQuery) -> None:
        if not await self.provider_limiter.allow(str(callback.from_user.id)):
            await callback.answer(
                "Too many provider requests. Please wait a minute.",
                show_alert=True,
            )
            return
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
            await self._delete_search_siblings(callback, search_payload)
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
            await callback.message.edit_text("⏳ Loading seasons and languages…")
        try:
            variants = await self.core.variants(
                payload["provider_id"],
                payload["url"],
                actor_key=callback.from_user.id,
            )
        except CapacityExceeded:
            await self._error(
                callback,
                "Another provider request is already running. Please try again shortly.",
            )
            return
        except Exception:
            LOGGER.exception("Variant loading failed")
            await self._error(callback, "This catalogue could not be loaded.")
            return
        variant_payload = {
            "variants": variants[:40],
            "search_selection_id": search_selection_id,
            "search_provider_id": str(payload.get("provider_id", "")),
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
        search_provider_id = str(payload.get("search_provider_id", ""))
        if search_selection_id:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="‹ Back to results",
                        callback_data=(
                            f"search-results:{search_selection_id}:{search_provider_id}"
                            if search_provider_id
                            else f"search-results:{search_selection_id}"
                        ),
                    )
                ]
            )
        else:
            buttons.append(
                [InlineKeyboardButton(text="‹ Back to menu", callback_data="menu:main")]
            )
        if callback.message:
            provider_alias = (
                self._provider_alias(payload["variants"][0])
                if payload["variants"]
                else "Provider"
            )
            await callback.message.edit_text(
                f"🎞 {provider_alias}\n\nChoose a season and language:",
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
        if not await self.provider_limiter.allow(str(callback.from_user.id)):
            await callback.answer(
                "Too many provider requests. Please wait a minute.",
                show_alert=True,
            )
            return
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
            await callback.message.edit_text("⏳ Loading episodes…")
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
                actor_key=callback.from_user.id,
            )
        except CapacityExceeded:
            await self._error(
                callback,
                "Another provider request is already running. Please try again shortly.",
            )
            return
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
                    text="‹ Previous",
                    callback_data=f"episodes:{selection_id}:{page - 1}",
                )
            )
        if page < max_page:
            navigation.append(
                InlineKeyboardButton(
                    text="Next ›",
                    callback_data=f"episodes:{selection_id}:{page + 1}",
                )
            )
        if navigation:
            rows.append(navigation)
        if variant_selection_id:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="‹ Back to seasons",
                        callback_data=f"variants:{variant_selection_id}",
                    )
                ]
            )
        else:
            rows.append(
                [InlineKeyboardButton(text="‹ Back to menu", callback_data="menu:main")]
            )
        text = (
            f"🎬 {catalogue['title']}\n"
            f"{self._provider_alias(catalogue)} · {catalogue['season']} · "
            f"{catalogue['language_label']}\n\n"
            f"Episodes {start}–{end} of {total}\n"
            "Choose an episode:"
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
        await self._render_continue_watching(callback, manage=False)

    async def manage_continue_watching(self, callback: CallbackQuery) -> None:
        await callback.answer()
        await self._render_continue_watching(callback, manage=True)

    async def _render_continue_watching(
        self,
        callback: CallbackQuery,
        *,
        manage: bool,
    ) -> None:
        user_id = callback.from_user.id
        if manage:
            entries = await self.database.continue_watching(user_id)
            completed_entries: list[dict[str, Any]] = []
        else:
            entries = await self.database.continue_watching(
                user_id,
                status="in_progress",
            )
            completed_entries = await self.database.continue_watching(
                user_id,
                status="completed",
            )
        if not entries and not completed_entries:
            await self._replace_callback_message(
                callback,
                "📭 Nothing to resume yet\n\nStart watching a title and it will appear here.",
                back_to_menu_keyboard(),
            )
            return
        buttons: list[list[InlineKeyboardButton]] = []
        for entry in entries:
            selection_id = await self.database.create_selection(
                user_id,
                "continue_remove" if manage else "continue",
                entry,
            )
            catalogue = entry["catalogue"]
            if manage:
                details = " · ".join(
                    value
                    for value in (
                        str(catalogue.get("season", "")),
                        str(catalogue.get("language_label", "")),
                        self._provider_alias(catalogue),
                    )
                    if value
                )
                label = button_label_with_suffix(
                    f"🗑 {catalogue['title']}",
                    details,
                )
                callback_data = f"continue:remove:{selection_id}"
            else:
                label = button_label_with_suffix(
                    f"▶ {catalogue['title']}",
                    f"E{entry['resume_episode']} · "
                    f"{self._provider_alias(catalogue)}",
                )
                callback_data = f"continue:{selection_id}"
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=callback_data,
                        style="danger" if manage else None,
                    )
                ]
            )
        if manage:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="✓ Done",
                        callback_data="menu:continue",
                        style="primary",
                    ),
                    InlineKeyboardButton(
                        text="🏠 Main menu",
                        callback_data="menu:main",
                    ),
                ]
            )
            text = (
                "⚙ Manage Continue Watching\n\n"
                "Choose an entry to remove from your list:"
            )
        else:
            if completed_entries:
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=f"✓ Completed ({len(completed_entries)})",
                            callback_data="continue:completed",
                            style="success",
                        )
                    ]
                )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="⚙ Manage list",
                        callback_data="continue:manage",
                    ),
                    InlineKeyboardButton(
                        text="‹ Back",
                        callback_data="menu:main",
                    ),
                ]
            )
            if entries:
                text = "▶ Continue watching\n\nChoose a title to resume:"
            else:
                text = (
                    "▶ Continue watching\n\n"
                    "No series are currently in progress.\n"
                    "Your completed series are saved separately."
                )
        await self._replace_callback_message(
            callback,
            text,
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    async def completed_watching(self, callback: CallbackQuery) -> None:
        await callback.answer()
        await self._render_completed_watching(callback)

    async def _render_completed_watching(self, callback: CallbackQuery) -> None:
        entries = await self.database.continue_watching(
            callback.from_user.id,
            status="completed",
        )
        if not entries:
            await self._replace_callback_message(
                callback,
                "✓ Completed\n\nNo completed series yet.",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="‹ Back to Continue Watching",
                                callback_data="menu:continue",
                            )
                        ]
                    ]
                ),
            )
            return
        buttons: list[list[InlineKeyboardButton]] = []
        for entry in entries:
            selection_id = await self.database.create_selection(
                callback.from_user.id,
                "continue_completed",
                entry,
            )
            catalogue = entry["catalogue"]
            details = " · ".join(
                value
                for value in (
                    str(catalogue.get("season", "")),
                    str(catalogue.get("language_label", "")),
                    self._provider_alias(catalogue),
                )
                if value
            )
            label = button_label_with_suffix(
                f"✓ {catalogue['title']}",
                details,
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"continue:completed-entry:{selection_id}",
                        style="success",
                    )
                ]
            )
        buttons.append(
            [
                InlineKeyboardButton(
                    text="‹ Continue Watching",
                    callback_data="menu:continue",
                ),
                InlineKeyboardButton(
                    text="🏠 Main menu",
                    callback_data="menu:main",
                ),
            ]
        )
        await self._replace_callback_message(
            callback,
            "✓ Completed\n\nChoose a series to view its options:",
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    async def select_completed(self, callback: CallbackQuery) -> None:
        await callback.answer()
        selection_id = (callback.data or "").rsplit(":", 1)[-1]
        entry = await self.database.get_selection(
            selection_id,
            callback.from_user.id,
            kind="continue_completed",
        )
        if entry is None:
            await self._expired(callback)
            return
        catalogue = entry["catalogue"]
        details = " · ".join(
            value
            for value in (
                self._provider_alias(catalogue),
                str(catalogue.get("season", "")),
                str(catalogue.get("language_label", "")),
            )
            if value
        )
        detail_line = f"\n{details}" if details else ""
        await self._replace_callback_message(
            callback,
            f"✓ Completed\n\n{catalogue['title']}{detail_line}\n\n"
            "You finished this series.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="↻ Restart from episode 1",
                            callback_data=(
                                f"continue:restart:completed:{selection_id}"
                            ),
                            style="primary",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="‹ Back to Completed",
                            callback_data="continue:completed",
                        )
                    ],
                ]
            ),
        )

    @staticmethod
    def _restart_selection_kind(source: str) -> str | None:
        return {
            "active": "continue",
            "completed": "continue_completed",
        }.get(source)

    async def confirm_restart(self, callback: CallbackQuery) -> None:
        await callback.answer()
        parts = (callback.data or "").split(":")
        if len(parts) != 4:
            await self._expired(callback)
            return
        source, selection_id = parts[2], parts[3]
        selection_kind = self._restart_selection_kind(source)
        if selection_kind is None:
            await self._expired(callback)
            return
        entry = await self.database.get_selection(
            selection_id,
            callback.from_user.id,
            kind=selection_kind,
        )
        if entry is None:
            await self._expired(callback)
            return
        catalogue = entry["catalogue"]
        cancel_callback = (
            f"continue:{selection_id}"
            if source == "active"
            else f"continue:completed-entry:{selection_id}"
        )
        await self._replace_callback_message(
            callback,
            f"↻ Restart {catalogue['title']}?\n\n"
            "Every saved episode position will be cleared and the series will "
            "restart from episode 1.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="↻ Restart",
                            callback_data=(
                                f"continue:restart-confirm:{source}:{selection_id}"
                            ),
                            style="danger",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="‹ Cancel",
                            callback_data=cancel_callback,
                        )
                    ],
                ]
            ),
        )

    async def restart_entry(self, callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) != 4:
            await callback.answer("This selection has expired.")
            await self._expired(callback)
            return
        source, selection_id = parts[2], parts[3]
        selection_kind = self._restart_selection_kind(source)
        if selection_kind is None:
            await callback.answer("This selection has expired.")
            await self._expired(callback)
            return
        entry = await self.database.get_selection(
            selection_id,
            callback.from_user.id,
            kind=selection_kind,
        )
        if entry is None:
            await callback.answer("This selection has expired.")
            await self._expired(callback)
            return
        restarted = await self.database.restart_watch_entry(
            callback.from_user.id,
            entry["catalogue"],
        )
        if not restarted:
            await callback.answer("This entry no longer exists.")
            await self._render_continue_watching(callback, manage=False)
            return
        await callback.answer("Restarted from episode 1.")
        await self._send_watch_button(
            callback,
            entry["catalogue"],
            1,
            start_position=0.0,
            back_callback="menu:continue",
        )

    async def confirm_continue_removal(self, callback: CallbackQuery) -> None:
        await callback.answer()
        selection_id = (callback.data or "").rsplit(":", 1)[-1]
        entry = await self.database.get_selection(
            selection_id,
            callback.from_user.id,
            kind="continue_remove",
        )
        if entry is None:
            await self._expired(callback)
            return
        catalogue = entry["catalogue"]
        details = " · ".join(
            value
            for value in (
                self._provider_alias(catalogue),
                str(catalogue.get("season", "")),
                str(catalogue.get("language_label", "")),
            )
            if value
        )
        detail_line = f"\n{details}" if details else ""
        await self._replace_callback_message(
            callback,
            "🗑 Remove from Continue Watching?\n\n"
            f"{catalogue['title']}{detail_line}\n\n"
            "This clears every saved episode position for this entry.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🗑 Remove",
                            callback_data=f"continue:delete:{selection_id}",
                            style="danger",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="‹ Cancel",
                            callback_data="continue:manage",
                        )
                    ],
                ]
            ),
        )

    async def remove_continue_entry(self, callback: CallbackQuery) -> None:
        selection_id = (callback.data or "").rsplit(":", 1)[-1]
        entry = await self.database.get_selection(
            selection_id,
            callback.from_user.id,
            kind="continue_remove",
        )
        if entry is None:
            await callback.answer("This selection has expired.")
            await self._expired(callback)
            return
        removed = await self.database.remove_from_continue_watching(
            callback.from_user.id,
            entry["catalogue"],
        )
        await callback.answer(
            "Removed from Continue Watching." if removed else "Already removed."
        )
        await self._render_continue_watching(callback, manage=True)

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
            restart_callback=f"continue:restart:active:{selection_id}",
        )

    async def _send_watch_button(
        self,
        callback: CallbackQuery,
        catalogue: dict[str, Any],
        episode: int,
        start_position: float = 0.0,
        back_callback: str | None = None,
        restart_callback: str | None = None,
    ) -> None:
        navigation: list[list[InlineKeyboardButton]] = []
        if back_callback:
            navigation.append(
                [
                    InlineKeyboardButton(
                        text="‹ Back",
                        callback_data=back_callback,
                    ),
                    InlineKeyboardButton(
                        text="🏠 Main menu",
                        callback_data="menu:main",
                    ),
                ]
            )
        else:
            navigation.append(
                [InlineKeyboardButton(text="🏠 Main menu", callback_data="menu:main")]
            )
        if urlparse(self.config.public_base_url).scheme.casefold() != "https":
            await self._replace_callback_message(
                callback,
                f"⚠ {catalogue['title']} · Episode {episode}\n\n"
                "The player requires a production HTTPS URL. Configure "
                "PUBLIC_BASE_URL and COOKIE_SECURE, then restart the bot.",
                InlineKeyboardMarkup(inline_keyboard=navigation),
            )
            return
        if start_position <= 0:
            start_position = await self.database.episode_position(
                callback.from_user.id,
                catalogue,
                episode,
            )
        launch_payload: dict[str, Any] = {
            "catalogue": catalogue,
            "episode": episode,
            "start_position": max(0.0, start_position),
        }
        if callback.message:
            chat_id = getattr(getattr(callback.message, "chat", None), "id", None)
            message_id = getattr(callback.message, "message_id", None)
            if isinstance(chat_id, int) and isinstance(message_id, int):
                # These values are created from the authenticated Telegram
                # callback, stored server-side in the one-time ticket, and
                # removed before the playback session is created.
                launch_payload["_menu_chat_id"] = chat_id
                launch_payload["_menu_message_id"] = message_id
        ticket = await self.database.create_launch_ticket(
            callback.from_user.id,
            launch_payload,
        )
        url = f"{self.config.public_base_url}/app/?launch={quote(ticket)}"
        actions = [
            [
                InlineKeyboardButton(
                    text=f"▶ Watch episode {episode}",
                    web_app=WebAppInfo(url=url),
                    style="success",
                )
            ]
        ]
        if restart_callback:
            actions.append(
                [
                    InlineKeyboardButton(
                        text="↻ Restart from episode 1",
                        callback_data=restart_callback,
                        style="primary",
                    )
                ]
            )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[*actions, *navigation]
        )
        await self._replace_callback_message(
            callback,
            f"🎬 {catalogue['title']}\n"
            f"Episode {episode}\n\n"
            "🔒 Your private player is ready. This launch link expires shortly.",
            keyboard,
        )

    async def _expired(self, callback: CallbackQuery) -> None:
        await self._replace_callback_message(
            callback,
            "⌛ This selection has expired.\n\nStart a new search to continue.",
            main_keyboard(),
        )

    async def _error(self, callback: CallbackQuery, message: str) -> None:
        if callback.message:
            await callback.message.edit_text(message, reply_markup=main_keyboard())
