from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import suppress

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update

from anistream_telegram.bot import BotHandlers
from anistream_telegram.config import Config
from anistream_telegram.core import CoreService
from anistream_telegram.database import Database
from anistream_telegram.media import MediaGateway
from anistream_telegram.web import CONFIG_KEY, WebRoutes, error_boundary, security_headers


LOGGER = logging.getLogger(__name__)
DATABASE_KEY = web.AppKey("database", Database)
CORE_KEY = web.AppKey("core", CoreService)
MEDIA_KEY = web.AppKey("media", MediaGateway)
BOT_KEY = web.AppKey("bot", Bot)
DISPATCHER_KEY = web.AppKey("dispatcher", Dispatcher)
BACKGROUND_UPDATES_KEY = web.AppKey("background_updates", set)
POLLING_TASK_KEY = web.AppKey("polling_task", object)
MAINTENANCE_TASK_KEY = web.AppKey("maintenance_task", object)


def build_application(config: Config) -> web.Application:
    database = Database(config.database_url)
    core = CoreService(
        user_agent=config.anime_sama_user_agent,
        cf_clearance=config.anime_sama_cf_clearance,
    )
    media = MediaGateway(config, database)
    bot = Bot(config.bot_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    handlers = BotHandlers(config, database, core)
    dispatcher.include_router(handlers.router)

    app = web.Application(
        middlewares=[error_boundary, security_headers],
        client_max_size=512 * 1024,
    )
    app[CONFIG_KEY] = config
    app[DATABASE_KEY] = database
    app[CORE_KEY] = core
    app[MEDIA_KEY] = media
    app[BOT_KEY] = bot
    app[DISPATCHER_KEY] = dispatcher
    app[BACKGROUND_UPDATES_KEY] = set()
    app[POLLING_TASK_KEY] = None
    app[MAINTENANCE_TASK_KEY] = None

    async def webhook(request: web.Request) -> web.Response:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied, config.webhook_secret):
            raise web.HTTPForbidden(text="Invalid Telegram webhook secret")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Telegram webhook must be JSON")
        try:
            payload = await request.json()
            update = Update.model_validate(payload, context={"bot": bot})
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text="Malformed Telegram update") from exc

        async def process() -> None:
            try:
                await dispatcher.feed_update(bot, update)
            except Exception:
                LOGGER.exception("Telegram update processing failed")

        task = asyncio.create_task(process(), name=f"telegram-update-{update.update_id}")
        app[BACKGROUND_UPDATES_KEY].add(task)
        task.add_done_callback(app[BACKGROUND_UPDATES_KEY].discard)
        return web.json_response({"ok": True})

    app.router.add_post(config.webhook_path, webhook)
    WebRoutes(
        config,
        database,
        core,
        media,
        bot=bot,
        background_tasks=app[BACKGROUND_UPDATES_KEY],
    ).register(app)

    async def startup(_: web.Application) -> None:
        await database.initialize(config.allowed_users)
        await media.start()

        async def maintenance() -> None:
            while True:
                await asyncio.sleep(900)
                try:
                    await database.cleanup()
                except Exception:
                    LOGGER.exception("Database cleanup failed")

        app[MAINTENANCE_TASK_KEY] = asyncio.create_task(
            maintenance(),
            name="database-maintenance",
        )
        if config.run_mode == "webhook":
            await bot.set_webhook(
                config.webhook_url,
                allowed_updates=dispatcher.resolve_used_update_types(),
                secret_token=config.webhook_secret,
                drop_pending_updates=False,
                max_connections=20,
            )
            LOGGER.info("Telegram webhook configured")
        else:
            app[POLLING_TASK_KEY] = asyncio.create_task(
                dispatcher.start_polling(
                    bot,
                    allowed_updates=dispatcher.resolve_used_update_types(),
                ),
                name="telegram-polling",
            )
            LOGGER.info("Telegram long polling started")

    async def cleanup(_: web.Application) -> None:
        maintenance_task = app[MAINTENANCE_TASK_KEY]
        if maintenance_task is not None:
            maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance_task
        polling_task = app[POLLING_TASK_KEY]
        if polling_task is not None:
            polling_task.cancel()
            with suppress(asyncio.CancelledError):
                await polling_task
        background = tuple(app[BACKGROUND_UPDATES_KEY])
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        await dispatcher.storage.close()
        await bot.session.close()
        await media.close()
        await database.close()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


def main() -> None:
    config = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    host = os.getenv("BIND_HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    web.run_app(
        build_application(config),
        host=host,
        port=port,
        # Launch and media tokens travel in URLs. Keep raw access logging disabled
        # unless the reverse proxy is explicitly configured to redact them.
        access_log=None,
    )


if __name__ == "__main__":
    main()
