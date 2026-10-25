"""Группа: приветствие новичков."""
from __future__ import annotations

import random
from html import escape

from aiogram import F, Router
from aiogram.filters import IS_MEMBER, IS_NOT_MEMBER, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated, Message, User

from app import keyboards as kb
from app.context import AppContext

router = Router(name="group")

WELCOMES = [
    "Привет, {name}! 👋 Добро пожаловать в группу — располагайтесь как дома.",
    "{name}, рады видеть! 🎉 Теперь вы с нами — заходите в разговоры смелее.",
    "Ура, у нас пополнение! 🙌 {name}, добро пожаловать!",
]

TAIL = ("\n\nЯ местный бот: по утрам рассказываю о погоде, поздравляю с днём рождения "
        "и принимаю заказы. Запишите свою дату рождения в личке: /mybd ДД.ММ 🎂")

_recent: dict[tuple[int, int], bool] = {}  # чтобы не здороваться дважды (два вида апдейтов)


async def _welcome(chat_id: int, user: User, send, ctx: AppContext) -> None:
    if not ctx.settings.welcome_enabled or user.is_bot:
        return
    if ctx.settings.group_chat_id and chat_id != ctx.settings.group_chat_id:
        return  # здороваемся только в «своей» группе
    key = (chat_id, user.id)
    if key in _recent:
        return
    _recent[key] = True
    if len(_recent) > 500:
        _recent.clear()
    name = f'<a href="tg://user?id={user.id}">{escape(user.first_name)}</a>'
    text = random.choice(WELCOMES).format(name=name) + TAIL
    await send(text, reply_markup=kb.welcome_keyboard(ctx.deep_link("order"), ctx.deep_link("help")))


@router.chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def on_join(event: ChatMemberUpdated, ctx: AppContext) -> None:
    # Этот апдейт приходит, только если бот — администратор группы.
    await _welcome(event.chat.id, event.new_chat_member.user,
                   lambda text, **kw: event.bot.send_message(event.chat.id, text, **kw), ctx)


@router.message(F.new_chat_members)
async def on_join_message(message: Message, ctx: AppContext) -> None:
    # Запасной вариант: служебное сообщение «X вступил(а) в группу».
    for user in message.new_chat_members:
        await _welcome(message.chat.id, user, message.answer, ctx)
