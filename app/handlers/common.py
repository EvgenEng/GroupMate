"""Старт, справка, погода по запросу, служебные команды."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from app.context import AppContext
from app.handlers.orders import show_catalog
from app.services import weather_reports

router = Router(name="common")

HELP = (
    "👋 <b>Что я умею</b>\n\n"
    "🌤 <b>Погода</b> — утром присылаю прогноз на день по Москве и области "
    "и предупреждаю, если погода резко меняется. По запросу: /weather или слово «погода».\n\n"
    "🎂 <b>Дни рождения</b> — поздравляю участников группы. Сообщите мне свою дату: "
    "<code>/mybd 15.04</code>. Ближайшие праздники: /birthdays\n\n"
    "🛍 <b>Заказы</b> — /catalog, корзина: /cart, мои заказы: /myorders\n\n"
    "💬 <b>Поболтать</b> — напишите мне в личку или упомяните меня в группе.\n\n"
    "⚡️ <b>Инлайн-режим</b> — в любом чате наберите <code>@{bot} ваш вопрос?</code> "
    "(со знаком вопроса в конце), и я подставлю ответ. А <code>@{bot}</code> без текста — погода и каталог."
)

OWNER_HELP = (
    "\n\n🔧 <b>Команды владельца</b>\n"
    "/bd_add ДД.ММ Имя — добавить ДР (ответом на сообщение человека — с упоминанием)\n"
    "/bd_list — все дни рождения, /bd_del ID — удалить\n"
    "/orders — открытые заказы (владелец и менеджеры)\n"
    "/test_morning, /test_evening — прислать сводку погоды в группу сейчас\n"
    "/test_bd — пример поздравления (вам в личку)\n"
    "/id — показать ID чата и пользователя"
)


def help_text(ctx: AppContext, user_id: int | None) -> str:
    text = HELP.format(bot=ctx.bot_username)
    return text + OWNER_HELP if ctx.is_owner(user_id) else text


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, ctx: AppContext) -> None:
    user = message.from_user
    await ctx.db.touch_user("tg", user.id, user.first_name, user.username)
    if command.args == "order" and message.chat.type == "private":
        await show_catalog(message, ctx)
        return
    await message.answer(f"Привет, {user.first_name}! 🙌\n\n" + help_text(ctx, user.id))


@router.message(Command("help"))
async def help_cmd(message: Message, ctx: AppContext) -> None:
    await message.answer(help_text(ctx, message.from_user.id))


@router.message(Command("id"))
async def show_id(message: Message) -> None:
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>\n"
                         f"Ваш ID: <code>{message.from_user.id}</code>")


async def _weather_answer(message: Message, ctx: AppContext) -> None:
    text = await weather_reports.now_report(ctx.weather, ctx.today())
    await message.answer(text or "😔 Не получилось загрузить прогноз, попробуйте чуть позже.")


@router.message(Command("weather", "pogoda"))
async def weather_cmd(message: Message, ctx: AppContext) -> None:
    await _weather_answer(message, ctx)


@router.message(F.text.lower().in_({"погода", "погода?", "какая погода", "какая погода?"}))
async def weather_word(message: Message, ctx: AppContext) -> None:
    await _weather_answer(message, ctx)
