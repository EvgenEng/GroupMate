"""Задачи по расписанию: погода, дни рождения, напоминания о заказах.

Всё отправляется через Messenger — сразу в группу Telegram и в беседу ВКонтакте.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.context import AppContext
from app.core import birthdays as bd
from app.core.holidays import holiday_text, holidays_to_remind, weekly_text
from app.scheduler import Scheduler
from app.services import weather_reports
from app.services.llm import LLMError

logger = logging.getLogger(__name__)


async def _once_per_day(ctx: AppContext, name: str, now: datetime) -> bool:
    """Защита от повторной отправки, если бот перезапустили в ту же минуту."""
    key = f"job:{name}:last"
    today = now.date().isoformat()
    if await ctx.db.kv_get(key) == today:
        return False
    await ctx.db.kv_set(key, today)
    return True


# ── погода ────────────────────────────────────────────────
async def weather_morning(ctx: AppContext, now: datetime, force: bool = False) -> None:
    if not force and not await _once_per_day(ctx, "weather_morning", now):
        return
    text = await weather_reports.morning_report(ctx.weather, ctx.db, now.date())
    if text:
        await ctx.messenger.to_groups(text)


async def weather_evening(ctx: AppContext, now: datetime, force: bool = False) -> None:
    if not force and not await _once_per_day(ctx, "weather_evening", now):
        return
    text = await weather_reports.evening_report(ctx.weather, now.date())
    if text:
        await ctx.messenger.to_groups(text)


async def weather_alerts(ctx: AppContext, now: datetime) -> None:
    text = await weather_reports.check_changes(ctx.weather, ctx.db, now)
    if text:
        await ctx.messenger.to_groups(text)


# ── дни рождения ──────────────────────────────────────────
async def make_greeting(ctx: AppContext, people: list[bd.Birthday]) -> str:
    if ctx.llm.enabled:
        try:
            text = await ctx.llm.one_shot(bd.greeting_prompt(people), max_tokens=500, temperature=1.0)
            return "🎉🎂🎉\n\n" + bd.insert_mentions(text, people)
        except LLMError as exc:
            logger.warning("Поздравление от LLM не получилось, беру шаблон: %s", exc)
    return bd.fallback_greeting(people)


async def birthdays(ctx: AppContext, now: datetime) -> None:
    if not await _once_per_day(ctx, "birthdays", now):
        return
    items = await ctx.db.list_birthdays()
    today = now.date()
    people = [b for b in items if bd.is_today(b, today)]
    s = ctx.settings
    if people and (s.vk_chat_peer_id or s.group_chat_id):   # общий пост — только если есть группа или беседа
        await ctx.messenger.to_groups(await make_greeting(ctx, people))
    for person in people:                                   # личное поздравление каждому именинному покупателю
        if not person.user_id:
            continue
        key = f"bdgreet:{person.platform}:{person.user_id}:{today.year}"   # раз в год на человека
        if await ctx.db.kv_get(key):
            continue
        await ctx.db.kv_set(key, {"sent": now.timestamp()})
        if await ctx.messenger.greet_birthday(person, bd.personal_greeting(person)):
            logger.info("Личное поздравление отправлено: %s %s", person.platform, person.user_id)
    # Владельцу и админам — подсказка о завтрашних именинниках (успеть с подарком 🎁).
    tomorrow = [b for b in items if bd.is_today(b, today + timedelta(days=1))]
    if tomorrow:
        names = ", ".join(b.mention() for b in tomorrow)
        await ctx.messenger.to_admins(f"🎁 Завтра день рождения: {names}")


# ── мягкие напоминания о заказах ──────────────────────────
async def order_reminder(ctx: AppContext, now: datetime) -> None:
    if not ctx.catalog.products or not await _once_per_day(ctx, "order_reminder", now):
        return
    due = holidays_to_remind(ctx.holidays, now.date())
    if due:  # праздник важнее обычного напоминания; одно сообщение в день, без спама
        holiday, left = min(due, key=lambda x: x[1])
        await ctx.messenger.to_groups(holiday_text(holiday, left), order_button=True)
    elif now.weekday() in ctx.settings.order_reminder_weekdays:
        await ctx.messenger.to_groups(weekly_text(), order_button=True)


# ── напоминание о брошенной корзине и просьба оценить заказ (ВКонтакте) ──
CART_REMINDER_MAX_AGE_DAYS = 3   # корзине старше этого срока уже не напоминаем — человек, скорее всего, передумал
CART_REMINDER_PAUSE_DAYS = 7     # одному человеку не чаще раза в неделю


async def cart_reminders(ctx: AppContext, now: datetime) -> None:
    ts = now.timestamp()
    s = ctx.settings
    for user_id, last in await ctx.db.stale_carts("vk", ts - s.cart_reminder_hours * 3600,
                                                  ts - CART_REMINDER_MAX_AGE_DAYS * 86400):
        key = f"cartrem:vk:{user_id}"
        state = await ctx.db.kv_get(key, {}) or {}
        if state.get("sent") and ts - state["sent"] < CART_REMINDER_PAUSE_DAYS * 86400:
            continue
        await ctx.db.kv_set(key, {"sent": ts, "cart_ts": last})   # отметка до отправки: не пробуем повторно каждые полчаса
        if await ctx.messenger.remind_cart(user_id):
            logger.info("Напоминание о корзине отправлено: ВК %s", user_id)


async def review_requests(ctx: AppContext, now: datetime) -> None:
    ts = now.timestamp()
    days = ctx.settings.review_request_days
    # окно в неделю: старые заказы (до включения функции) не поднимаем
    for order in await ctx.db.orders_to_review("vk", ts - days * 86400, ts - (days + 7) * 86400):
        key = f"review:ask:{order.id}"
        if await ctx.db.kv_get(key):
            continue
        await ctx.db.kv_set(key, {"sent": ts})
        if await ctx.messenger.ask_review(order):
            logger.info("Просьба об оценке заказа №%s отправлена", order.id)


def register(scheduler: Scheduler, ctx: AppContext) -> None:
    s = ctx.settings
    if s.weather_enabled:
        scheduler.daily("Погода утром", s.weather_morning, lambda now: weather_morning(ctx, now))
        if s.weather_evening_enabled:
            scheduler.daily("Погода вечером", s.weather_evening, lambda now: weather_evening(ctx, now))
        if s.weather_alerts_enabled:
            scheduler.every("Изменения погоды", s.weather_alerts_every_min,
                            lambda now: weather_alerts(ctx, now),
                            hours=(s.weather_alerts_from, s.weather_alerts_to))
    scheduler.daily("Дни рождения", s.birthday_time, lambda now: birthdays(ctx, now))
    if s.order_reminders_enabled:
        # Каждый день проверяем праздники; обычное напоминание — только в выбранные дни недели.
        scheduler.daily("Напоминание о заказах", s.order_reminder_time, lambda now: order_reminder(ctx, now))
    if s.cart_reminders_enabled:
        scheduler.every("Напоминание о корзине", 30, lambda now: cart_reminders(ctx, now), hours=(10, 20))
    if s.review_requests_enabled:
        scheduler.every("Просьба об оценке заказа", 60, lambda now: review_requests(ctx, now), hours=(11, 19))
