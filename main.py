"""Точка входа: python main.py"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app import jobs
from app.config import load_settings
from app.context import AppContext
from app.core.holidays import load_holidays
from app.core.orders import Catalog
from app.handlers import admin, birthdays, chat, common, group, inline, orders
from app.scheduler import Scheduler
from app.services.db import Database
from app.services.llm import LLMClient
from app.services.weather import WeatherService

COMMANDS = [
    BotCommand(command="weather", description="🌤 Погода на сегодня"),
    BotCommand(command="tomorrow", description="🌙 Погода на завтра"),
    BotCommand(command="catalog", description="🛍 Каталог и заказ"),
    BotCommand(command="cart", description="🛒 Корзина"),
    BotCommand(command="myorders", description="🧾 Мои заказы"),
    BotCommand(command="mybd", description="🎂 Записать свой день рождения"),
    BotCommand(command="birthdays", description="🎉 Ближайшие дни рождения"),
    BotCommand(command="help", description="ℹ️ Что умеет бот"),
]


async def run() -> None:
    settings = load_settings()
    if not settings.bot_token:
        sys.exit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и заполните его.")

    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None
    bot = Bot(settings.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()

    ctx = AppContext(
        settings=settings,
        db=Database(settings.db_path),
        llm=LLMClient(settings.llm_providers, settings.llm_system_prompt),
        weather=WeatherService(settings.weather_locations, settings.timezone),
        catalog=Catalog.load(settings.catalog_path),
        holidays=load_holidays(settings.holidays_path),
        bot_username=me.username or "",
    )

    dp = Dispatcher(storage=MemoryStorage())
    dp["ctx"] = ctx
    # Порядок важен: разговор с нейросетью — последним, чтобы не перехватывал остальное.
    dp.include_routers(admin.router, orders.router, common.router, birthdays.router,
                       group.router, inline.router, chat.router)

    scheduler = Scheduler(settings.timezone)
    jobs.register(scheduler, bot, ctx)
    scheduler.start()

    await bot.set_my_commands(COMMANDS)
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот @%s запущен. Нейросеть: %s. Товаров в каталоге: %d.", me.username,
                 ", ".join(p.model for p in settings.llm_providers) or "выключена", len(ctx.catalog.products))
    if settings.owner_id:
        try:
            await bot.send_message(settings.owner_id, "✅ Бот запущен и готов к работе.")
        except Exception as exc:  # noqa: BLE001 — владелец мог ещё не написать боту /start
            logging.warning("Не удалось написать владельцу: %s", exc)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await scheduler.stop()
        ctx.db.close()
        await bot.session.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
