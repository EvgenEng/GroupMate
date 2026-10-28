"""Telegram: каталог → корзина → оформление → уведомление менеджерам → статусы."""
from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from app import keyboards as kb
from app.context import AppContext
from app.core.orders import STATUSES, catalog_intro, checkout_comment_hint, format_cart, format_order, \
    format_product

logger = logging.getLogger(__name__)
router = Router(name="orders")

PLATFORM = "tg"
PHONE_RE = re.compile(r"^\+?[\d\s\-()]{7,20}$")


class Checkout(StatesGroup):
    phone = State()
    comment = State()
    confirm = State()


async def show_catalog(message: Message, ctx: AppContext) -> None:
    if not ctx.catalog.products:
        await message.answer("Каталог пока пуст — скоро здесь появятся товары 🙂")
        return
    await message.answer(catalog_intro(ctx.catalog), reply_markup=kb.categories_kb(ctx.catalog))


@router.message(Command("catalog", "order", "zakaz"))
async def catalog_cmd(message: Message, ctx: AppContext) -> None:
    if message.chat.type != "private":
        # В группе корзины не собираем — зовём в личку.
        await message.reply("Заказы оформляются в личных сообщениях со мной 👇",
                            reply_markup=kb.order_button(ctx.deep_link("order")))
        return
    await show_catalog(message, ctx)


async def _edit(call: CallbackQuery, text: str, markup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:  # «message is not modified» — не ошибка
        if "not modified" not in str(exc):
            raise


async def _cart_view(user_id: int, ctx: AppContext):
    lines = await ctx.orders.lines(PLATFORM, user_id)
    return format_cart(lines, ctx.catalog.currency), kb.cart_kb(lines)


async def _product_view(user_id: int, product_id: str, ctx: AppContext):
    product = ctx.catalog.product(product_id)
    qty = await ctx.orders.qty(PLATFORM, user_id, product_id)
    return (format_product(product, ctx.catalog.currency, qty),
            kb.product_kb(product.id, product.category, qty, product.unit))


@router.callback_query(kb.Shop.filter())
async def shop_callback(call: CallbackQuery, callback_data: kb.Shop, ctx: AppContext, state: FSMContext) -> None:
    user_id = call.from_user.id
    action, value = callback_data.action, callback_data.value
    catalog = ctx.catalog

    if action == "cats":
        await _edit(call, catalog_intro(catalog), kb.categories_kb(catalog))
    elif action == "cat":
        category = catalog.category(value)
        title = category.title if category else "Раздел"
        await _edit(call, f"📋 <b>{title}</b>\n\nВыберите товар:", kb.products_kb(catalog, value))
    elif action == "prod":
        if not catalog.product(value):
            await call.answer("Товар не найден", show_alert=True)
            return
        await _edit(call, *await _product_view(user_id, value, ctx))
    elif action in {"add", "sub"}:
        qty = await ctx.orders.change(PLATFORM, user_id, value, 1 if action == "add" else -1)
        if qty is None:
            await call.answer("Товар не найден", show_alert=True)
            return
        await call.answer("Добавлено в корзину" if action == "add" else ("Убрано" if not qty else "Готово"))
        # Обновляем то сообщение, где нажали: карточку товара или корзину.
        if call.message.text and call.message.text.startswith("🛒"):
            await _edit(call, *await _cart_view(user_id, ctx))
        else:
            await _edit(call, *await _product_view(user_id, value, ctx))
        return
    elif action == "cart":
        await _edit(call, *await _cart_view(user_id, ctx))
    elif action == "clear":
        await ctx.orders.clear(PLATFORM, user_id)
        await _edit(call, *await _cart_view(user_id, ctx))
    elif action == "checkout":
        if not await ctx.orders.lines(PLATFORM, user_id):
            await call.answer("Корзина пуста", show_alert=True)
            return
        await state.set_state(Checkout.phone)
        await call.message.answer("📞 Оставьте, пожалуйста, телефон для связи — кнопкой ниже или текстом.",
                                  reply_markup=kb.contact_kb())
    await call.answer()


@router.message(Command("cart"), F.chat.type == "private")
async def cart_cmd(message: Message, ctx: AppContext) -> None:
    text, markup = await _cart_view(message.from_user.id, ctx)
    await message.answer(text, reply_markup=markup)


@router.message(Command("cancel"), StateFilter(Checkout))
@router.message(F.text == "❌ Отмена", StateFilter(Checkout))
async def checkout_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Оформление отменено. Корзина сохранена — /cart", reply_markup=ReplyKeyboardRemove())


@router.message(Checkout.phone)
async def checkout_phone(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if message.contact:
        phone = message.contact.phone_number
    elif message.text and PHONE_RE.match(message.text.strip()):
        phone = message.text.strip()
    else:
        await message.answer("Не похоже на номер 🤔 Отправьте кнопкой или напишите, например: +7 999 123-45-67")
        return
    await state.update_data(phone=phone)
    await state.set_state(Checkout.comment)
    await message.answer(checkout_comment_hint(ctx.catalog), reply_markup=kb.skip_kb())


@router.message(Checkout.comment, F.text)
async def checkout_comment(message: Message, state: FSMContext, ctx: AppContext) -> None:
    comment = "" if message.text == "Без комментария" else message.text.strip()[:500]
    await state.update_data(comment=comment)
    lines = await ctx.orders.lines(PLATFORM, message.from_user.id)
    if not lines:
        await state.clear()
        await message.answer("Корзина опустела 🤷 Загляните в /catalog", reply_markup=ReplyKeyboardRemove())
        return
    await state.set_state(Checkout.confirm)
    await message.answer(format_cart(lines, ctx.catalog.currency) + "\n\nВсё верно?", reply_markup=kb.confirm_kb())


@router.message(Checkout.confirm, F.text == "✅ Подтвердить")
async def checkout_confirm(message: Message, state: FSMContext, ctx: AppContext) -> None:
    data = await state.get_data()
    user = message.from_user
    await state.clear()
    order = await ctx.orders.place(PLATFORM, user.id, user.full_name, user.username,
                                   data.get("phone", ""), data.get("comment", ""))
    if not order:
        await message.answer("Корзина пуста 🤷", reply_markup=ReplyKeyboardRemove())
        return
    await message.answer(f"🎉 Заказ №{order.id} оформлен! Мы скоро свяжемся с вами.\n\n"
                         + format_order(order, ctx.catalog.currency), reply_markup=ReplyKeyboardRemove())
    await ctx.messenger.notify_new_order(order)


@router.message(Checkout.confirm)
async def checkout_confirm_other(message: Message) -> None:
    await message.answer("Нажмите «✅ Подтвердить» или «❌ Отмена».", reply_markup=kb.confirm_kb())


@router.callback_query(kb.OrderStatus.filter())
async def order_status(call: CallbackQuery, callback_data: kb.OrderStatus, ctx: AppContext) -> None:
    if not await ctx.messenger.is_manager(PLATFORM, call.from_user.id):
        await call.answer("Только для менеджеров", show_alert=True)
        return
    order = await ctx.orders.set_status(callback_data.order_id, callback_data.status)
    if not order:
        await call.answer("Заказ не найден", show_alert=True)
        return
    await _edit(call, format_order(order, ctx.catalog.currency, for_owner=True), kb.owner_order_kb(order.id))
    await call.answer(f"Статус: {STATUSES[callback_data.status]}")
    await ctx.messenger.notify_customer(order)


@router.message(Command("myorders"), F.chat.type == "private")
async def my_orders(message: Message, ctx: AppContext) -> None:
    orders = await ctx.db.orders_of(PLATFORM, message.from_user.id)
    if not orders:
        await message.answer("Заказов пока нет. Каталог — /catalog")
        return
    await message.answer("\n\n".join(format_order(o, ctx.catalog.currency) for o in orders))


@router.message(Command("orders"), F.chat.type == "private")
async def all_orders(message: Message, ctx: AppContext) -> None:
    if not await ctx.messenger.is_manager(PLATFORM, message.from_user.id):
        return
    orders = await ctx.db.recent_orders()
    if not orders:
        await message.answer("Открытых заказов нет 👌")
        return
    for o in orders:
        await message.answer(format_order(o, ctx.catalog.currency, for_owner=True),
                             reply_markup=kb.owner_order_kb(o.id))
