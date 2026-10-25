"""Задачи по расписанию: погода, дни рождения, напоминания о заказах."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot

from app import keyboards as kb
from app.context import AppContext
from app.core import birthdays as bd
from app.core.holidays import holiday_text, holidays_to_remind, weekly_text
from app.scheduler import Scheduler
from app.services import weather_reports
from app.services.llm import LLMError

logger = logging.getLogger(__name__)


async def _to_group(bot: Bot, ctx: AppContext, text: str, **kwargs) -> bool:
    if not ctx.settings.group_chat_id:
        logger.warning("GROUP_CHAT_ID не задан — сообщение в группу не отправлено")
        return False
    await bot.send_message(ctx.settings.group_chat_id, text, **kwargs)
    return True


async def _once_per_day(ctx: AppContext, name: str, now: datetime) -> bool:
    """Защита от повторной отправки, если бот перезапустили в ту же минуту."""
    key = f"job:{name}:last"
    today = now.date().isoformat()
    if await ctx.db.kv_get(key) == today:
        return False
    await ctx.db.kv_set(key, today)
    return True


# ── погода ────────────────────────────────────────────────
async def weather_morning(bot: Bot, ctx: AppContext, now: datetime, force: bool = False) -> None:
    if not force and not await _once_per_day(ctx, "weather_morning", now):
        return
    text = await weather_reports.morning_report(ctx.weather, ctx.db, now.date())
    if text:
        await _to_group(bot, ctx, text)


async def weather_evening(bot: Bot, ctx: AppContext, now: datetime, force: bool = False) -> None:
    if not force and not await _once_per_day(ctx, "weather_evening", now):
        return
    text = await weather_reports.evening_report(ctx.weather, now.date())
    if text:
        await _to_group(bot, ctx, text)


async def weather_alerts(bot: Bot, ctx: AppContext, now: datetime) -> None:
    text = await weather_reports.check_changes(ctx.weather, ctx.db, now)
    if text:
        await _to_group(bot, ctx, text)


# ── дни рождения ──────────────────────────────────────────
async def make_greeting(ctx: AppContext, people: list[bd.Birthday]) -> str:
    if ctx.llm.enabled:
        try:
            text = await ctx.llm.one_shot(bd.greeting_prompt(people), max_tokens=500, temperature=1.0)
            return "🎉🎂🎉\n\n" + bd.insert_mentions(text, people)
        except LLMError as exc:
            logger.warning("Поздравление от LLM не получилось, беру шаблон: %s", exc)
    return bd.fallback_greeting(people)


async def birthdays(bot: Bot, ctx: AppContext, now: datetime) -> None:
    if not await _once_per_day(ctx, "birthdays", now):
        return
    items = await ctx.db.list_birthdays()
    today = now.date()
    people = [b for b in items if bd.is_today(b, today)]
    if people:
        await _to_group(bot, ctx, await make_greeting(ctx, people))
    # Владельцу — подсказка о завтрашних именинниках (успеть с подарком 🎁).
    tomorrow = [b for b in items if bd.is_today(b, today + timedelta(days=1))]
    if tomorrow and ctx.settings.owner_id:
        names = ", ".join(b.mention() for b in tomorrow)
        await bot.send_message(ctx.settings.owner_id, f"🎁 Завтра день рождения: {names}")


# ── мягкие напоминания о заказах ──────────────────────────
async def order_reminder(bot: Bot, ctx: AppContext, now: datetime) -> None:
    if not ctx.catalog.products or not await _once_per_day(ctx, "order_reminder", now):
        return
    markup = kb.order_button(ctx.deep_link("order"))
    due = holidays_to_remind(ctx.holidays, now.date())
    if due:  # праздник важнее обычного напоминания; одно сообщение в день, без спама
        holiday, left = min(due, key=lambda x: x[1])
        await _to_group(bot, ctx, holiday_text(holiday, left), reply_markup=markup)
    elif now.weekday() in ctx.settings.order_reminder_weekdays:
        await _to_group(bot, ctx, weekly_text(), reply_markup=markup)


def register(scheduler: Scheduler, bot: Bot, ctx: AppContext) -> None:
    s = ctx.settings
    if s.weather_enabled:
        scheduler.daily("Погода утром", s.weather_morning, lambda now: weather_morning(bot, ctx, now))
        scheduler.daily("Погода вечером", s.weather_evening, lambda now: weather_evening(bot, ctx, now))
        if s.weather_alerts_enabled:
            scheduler.every("Изменения погоды", s.weather_alerts_every_min,
                            lambda now: weather_alerts(bot, ctx, now),
                            hours=(s.weather_alerts_from, s.weather_alerts_to))
    scheduler.daily("Дни рождения", s.birthday_time, lambda now: birthdays(bot, ctx, now))
    if s.order_reminders_enabled:
        # Каждый день проверяем праздники; обычное напоминание — только в выбранные дни недели.
        scheduler.daily("Напоминание о заказах", s.order_reminder_time, lambda now: order_reminder(bot, ctx, now))
