"""Дни рождения: самостоятельная запись, ручное добавление владельцем, список ближайших."""
from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.context import AppContext
from app.core.birthdays import days_word, parse_day_month, upcoming

router = Router(name="birthdays")


@router.message(Command("mybd"))
async def my_birthday(message: Message, command: CommandObject, ctx: AppContext) -> None:
    parsed = parse_day_month(command.args or "")
    if not parsed:
        await message.answer("Напишите дату так: <code>/mybd 15.04</code> (день.месяц) 🎂")
        return
    user = message.from_user
    await ctx.db.add_birthday(user.full_name, parsed[0], parsed[1], user.id, user.username)
    await message.answer(f"🎂 Записал! В {parsed[0]:02d}.{parsed[1]:02d} поздравлю вас в группе 🎉")


@router.message(Command("birthdays", "bd"))
async def nearest(message: Message, ctx: AppContext) -> None:
    items = await ctx.db.list_birthdays()
    if not items:
        await message.answer("Пока никто не записал день рождения. Это можно сделать командой "
                             "<code>/mybd ДД.ММ</code> 🎂")
        return
    rows = ["🎂 <b>Ближайшие дни рождения</b>", ""]
    for b, left in upcoming(items, ctx.today(), limit=5):
        rows.append(f"• {escape(b.name)} — {b.date_text()} ({days_word(left)})")
    await message.answer("\n".join(rows))


@router.message(Command("bd_add"))
async def bd_add(message: Message, command: CommandObject, ctx: AppContext) -> None:
    if not ctx.is_owner(message.from_user.id):
        return
    parts = (command.args or "").split(maxsplit=1)
    parsed = parse_day_month(parts[0]) if parts else None
    target = message.reply_to_message.from_user if message.reply_to_message else None
    name = parts[1].strip() if len(parts) > 1 else (target.full_name if target else "")
    if not parsed or not name:
        await message.answer("Формат: <code>/bd_add 15.04 Иван Петров</code>\n"
                             "Можно ответом на сообщение человека — тогда он будет упомянут ссылкой.")
        return
    bd_id = await ctx.db.add_birthday(name, parsed[0], parsed[1],
                                      target.id if target else None, target.username if target else None)
    await message.answer(f"✅ Добавлено (ID {bd_id}): {escape(name)} — {parsed[0]:02d}.{parsed[1]:02d}")


@router.message(Command("bd_list"), F.chat.type == "private")
async def bd_list(message: Message, ctx: AppContext) -> None:
    if not ctx.is_owner(message.from_user.id):
        return
    items = await ctx.db.list_birthdays()
    if not items:
        await message.answer("Список пуст.")
        return
    rows = [f"<code>{b.id}</code> · {b.day:02d}.{b.month:02d} · {b.mention()}" for b in items]
    await message.answer("🎂 <b>Все дни рождения</b>\n\n" + "\n".join(rows))


@router.message(Command("bd_del"))
async def bd_del(message: Message, command: CommandObject, ctx: AppContext) -> None:
    if not ctx.is_owner(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: <code>/bd_del ID</code> (ID смотрите в /bd_list)")
        return
    ok = await ctx.db.delete_birthday(int(arg))
    await message.answer("🗑 Удалено." if ok else "Такого ID нет.")
