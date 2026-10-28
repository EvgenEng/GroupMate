"""Команды владельца для проверки без ожидания расписания."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app import jobs
from app.context import AppContext

router = Router(name="admin")


@router.message(Command("test_morning"))
async def test_morning(message: Message, ctx: AppContext) -> None:
    if ctx.is_owner(message.from_user.id):
        await jobs.weather_morning(ctx, ctx.now(), force=True)
        await message.answer("✅ Утренняя сводка отправлена в группу и беседу.")


@router.message(Command("test_evening"))
async def test_evening(message: Message, ctx: AppContext) -> None:
    if ctx.is_owner(message.from_user.id):
        await jobs.weather_evening(ctx, ctx.now(), force=True)
        await message.answer("✅ Вечерняя сводка отправлена в группу и беседу.")


@router.message(Command("test_bd"))
async def test_birthday(message: Message, ctx: AppContext) -> None:
    """Показывает владельцу пример поздравления (себе в чат, не в группу)."""
    if not ctx.is_owner(message.from_user.id):
        return
    from app.core.birthdays import Birthday
    u = message.from_user
    me = Birthday(id=0, name=u.first_name, day=1, month=1, user_id=u.id)
    await message.answer(await jobs.make_greeting(ctx, [me]))
