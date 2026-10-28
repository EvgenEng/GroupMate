"""Точка входа: python main.py

Запускает Telegram-бота и/или ВК-бота (что настроено в .env) и общий планировщик.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from app import jobs
from app.config import load_settings
from app.context import AppContext, build_context
from app.scheduler import Scheduler

logger = logging.getLogger("main")


async def run_telegram(ctx: AppContext) -> None:
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.enums import ParseMode
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import BotCommand

    from app.handlers import admin, birthdays, chat, common, group, inline, orders

    s = ctx.settings
    session = AiohttpSession(proxy=s.telegram_proxy) if s.telegram_proxy else None
    bot = Bot(s.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    ctx.messenger.tg = bot
    ctx.messenger.tg_username = me.username or ""

    dp = Dispatcher(storage=MemoryStorage())
    dp["ctx"] = ctx
    # Порядок важен: разговор с нейросетью — последним, чтобы не перехватывал остальное.
    dp.include_routers(admin.router, orders.router, common.router, birthdays.router,
                       group.router, inline.router, chat.router)
    await bot.set_my_commands([
        BotCommand(command="weather", description="🌤 Погода на сегодня"),
        BotCommand(command="catalog", description="🛍 Каталог и заказ"),
        BotCommand(command="cart", description="🛒 Корзина"),
        BotCommand(command="myorders", description="🧾 Мои заказы"),
        BotCommand(command="mybd", description="🎂 Записать свой день рождения"),
        BotCommand(command="birthdays", description="🎉 Ближайшие дни рождения"),
        BotCommand(command="help", description="ℹ️ Что умеет бот"),
    ])
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Telegram: бот @%s запущен", me.username)
    if s.owner_id:
        try:
            await bot.send_message(s.owner_id, "✅ Бот запущен и готов к работе.")
        except Exception as exc:  # noqa: BLE001 — владелец мог ещё не написать боту /start
            logger.warning("Не удалось написать владельцу: %s", exc)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


async def run_vk(ctx: AppContext) -> None:
    from app.vk.api import VkApi
    from app.vk.bot import VkBot

    api = VkApi(ctx.settings.vk_token, ctx.settings.vk_group_id)
    ctx.messenger.vk = api
    try:
        await VkBot(ctx, api).run()
    finally:
        await api.close()


async def run() -> None:
    settings = load_settings()
    if not settings.tg_enabled and not settings.vk_enabled:
        sys.exit("Не настроен ни Telegram (BOT_TOKEN), ни ВКонтакте (VK_TOKEN + VK_GROUP_ID). "
                 "Скопируйте .env.example в .env и заполните его.")
    ctx = build_context(settings)
    logger.info("Нейросеть: %s. Товаров в каталоге: %d.",
                ", ".join(p.model for p in settings.llm_providers) or "выключена", len(ctx.catalog.products))

    scheduler = Scheduler(settings.timezone)
    jobs.register(scheduler, ctx)
    scheduler.start()

    bots = []
    if settings.tg_enabled:
        bots.append(run_telegram(ctx))
    if settings.vk_enabled:
        bots.append(run_vk(ctx))
    try:
        await asyncio.gather(*bots)
    finally:
        await scheduler.stop()
        ctx.db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
