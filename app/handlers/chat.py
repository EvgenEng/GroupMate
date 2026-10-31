"""Разговор с нейросетью: в личке — на любое сообщение, в группе — по упоминанию или ответу боту.

Этот роутер подключается последним, чтобы не перехватывать команды и оформление заказа.
"""
from __future__ import annotations

import logging
import random
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.types import Message

from app.context import AppContext
from app.core.knowledge import HANDOFF_FALLBACK, format_handoff, split_handoff
from app.services.llm import LLMError

logger = logging.getLogger(__name__)
router = Router(name="chat")

LIMIT_TEXTS = [
    "На сегодня я выговорился 😅 Давайте продолжим завтра!",
    "Лимит разговоров на сегодня исчерпан. Погода, заказы и дни рождения работают как обычно 🙂",
]


def _addressed_to_bot(message: Message, ctx: AppContext) -> str | None:
    """Возвращает текст вопроса, если сообщение адресовано боту, иначе None."""
    text = message.text or message.caption or ""
    if message.chat.type == "private":
        return text
    mention = f"@{ctx.bot_username}".lower()
    if ctx.bot_username and mention in text.lower():
        idx = text.lower().index(mention)
        return (text[:idx] + text[idx + len(mention):]).strip(" ,") or "Привет!"
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.username == ctx.bot_username:
        return text
    return None


@router.message(F.text & ~F.text.startswith("/"))
async def talk(message: Message, ctx: AppContext, bot: Bot) -> None:
    question = _addressed_to_bot(message, ctx)
    if question is None:
        return
    if not ctx.llm.enabled:
        if message.chat.type == "private":
            await message.answer("Поболтать пока не могу — нейросеть не подключена. "
                                 "Но погода (/weather) и заказы (/catalog) работают! 🙂")
        return
    if not await ctx.llm_allowed("tg", message.from_user.id, ctx.is_owner(message.from_user.id)):
        await message.reply(random.choice(LIMIT_TEXTS))
        return
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    who = message.from_user.first_name
    try:
        answer = await ctx.llm.chat(f"tg:{message.chat.id}", f"{who}: {question}",
                                    extra_context=f"Сегодня {ctx.today().strftime('%d.%m.%Y')}.")
    except LLMError as exc:
        logger.warning("LLM не ответила: %s", exc)
        await message.reply("🤖 Что-то я задумался и не смог ответить. Попробуйте ещё раз чуть позже.")
        return
    answer, needs_manager = split_handoff(answer)
    if needs_manager and not answer:
        answer = HANDOFF_FALLBACK
    await message.reply(escape(answer)[:4000])
    if needs_manager and message.chat.type == "private":
        user = message.from_user
        name = user.full_name + (f" (@{user.username})" if user.username else "")
        html = format_handoff(name, f"tg://user?id={user.id}", "", question, answer,
                              no_phone_hint="написать в Telegram по ссылке выше")
        if await ctx.messenger.notify_handoff(html):
            await message.answer("✅ Передал ваш вопрос менеджеру — он напишет вам здесь.")
