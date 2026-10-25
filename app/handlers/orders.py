"""Заказы: каталог → корзина → оформление → уведомление владельцу → статусы."""
from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from app import keyboards as kb
from app.context import AppContext
from app.core.orders import STATUS_FOR_CUSTOMER, STATUSES, build_lines, cart_total, format_cart, \
    format_order, format_product

logger = logging.getLogger(__name__)
router = Router(name="orders")

CATALOG_TITLE = "📋 <b>Каталог</b>\n\nВыберите раздел:"
PHONE_RE = re.compile(r"^\+?[\d\s\-()]{7,20}$")


class Checkout(StatesGroup):
    phone = State()
    comment = State()
    confirm = State()


async def show_catalog(message: Message, ctx: AppContext) -> None:
    if not ctx.catalog.products:
        await message.answer("Каталог пока пуст — скоро здесь появятся товары 🙂")
        return
    await message.answer(CATALOG_TITLE, reply_markup=kb.categories_kb(ctx.catalog))


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
    lines = build_lines(await ctx.db.cart(user_id), ctx.catalog)
    return format_cart(lines, ctx.catalog.currency), kb.cart_kb(lines)


@router.callback_query(kb.Shop.filter())
async def shop_callback(call: CallbackQuery, callback_data: kb.Shop, ctx: AppContext, state: FSMContext) -> None:
    user_id = call.from_user.id
    action, value = callback_data.action, callback_data.value
    catalog = ctx.catalog

    if action == "cats":
        await _edit(call, CATALOG_TITLE, kb.categories_kb(catalog))
    elif action == "cat":
        category = catalog.category(value)
        title = category.title if category else "Раздел"
        await _edit(call, f"📋 <b>{title}</b>\n\nВыберите товар:", kb.products_kb(catalog, value))
    elif action == "prod":
        product = catalog.product(value)
        if not product:
            await call.answer("Товар не найден", show_alert=True)
            return
        qty = (await ctx.db.cart(user_id)).get(product.id, 0)
        await _edit(call, format_product(product, catalog.currency, qty),
                    kb.product_kb(product.id, product.category, qty))
    elif action in {"add", "sub"}:
        product = catalog.product(value)
        if not product:
            await call.answer("Товар не найден", show_alert=True)
            return
        qty = await ctx.db.cart_change(user_id, product.id, 1 if action == "add" else -1)
        await call.answer(f"В корзине: {qty} {product.unit}" if qty else "Убрано из корзины")
        # Обновляем то сообщение, где нажали: карточку товара или корзину.
        if call.message.text and call.message.text.startswith("🛒"):
            text, markup = await _cart_view(user_id, ctx)
            await _edit(call, text, markup)
        else:
            await _edit(call, format_product(product, catalog.currency, qty),
                        kb.product_kb(product.id, product.category, qty))
        return
    elif action == "cart":
        text, markup = await _cart_view(user_id, ctx)
        await _edit(call, text, markup)
    elif action == "clear":
        await ctx.db.cart_clear(user_id)
        text, markup = await _cart_view(user_id, ctx)
        await _edit(call, text, markup)
    elif action == "checkout":
        lines = build_lines(await ctx.db.cart(user_id), catalog)
        if not lines:
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
async def checkout_phone(message: Message, state: FSMContext) -> None:
    if message.contact:
        phone = message.contact.phone_number
    elif message.text and PHONE_RE.match(message.text.strip()):
        phone = message.text.strip()
    else:
        await message.answer("Не похоже на номер 🤔 Отправьте кнопкой или напишите, например: +7 999 123-45-67")
        return
    await state.update_data(phone=phone)
    await state.set_state(Checkout.comment)
    await message.answer("💬 Есть пожелания? Например: к какому дню нужно, удобное время, детали. "
                         "Или нажмите «Без комментария».", reply_markup=kb.skip_kb())


@router.message(Checkout.comment, F.text)
async def checkout_comment(message: Message, state: FSMContext, ctx: AppContext) -> None:
    comment = "" if message.text == "Без комментария" else message.text.strip()[:500]
    await state.update_data(comment=comment)
    lines = build_lines(await ctx.db.cart(message.from_user.id), ctx.catalog)
    if not lines:
        await state.clear()
        await message.answer("Корзина опустела 🤷 Загляните в /catalog", reply_markup=ReplyKeyboardRemove())
        return
    await state.set_state(Checkout.confirm)
    await message.answer(format_cart(lines, ctx.catalog.currency) + "\n\nВсё верно?", reply_markup=kb.confirm_kb())


@router.message(Checkout.confirm, F.text == "✅ Подтвердить")
async def checkout_confirm(message: Message, state: FSMContext, ctx: AppContext, bot: Bot) -> None:
    data = await state.get_data()
    user = message.from_user
    lines = build_lines(await ctx.db.cart(user.id), ctx.catalog)
    await state.clear()
    if not lines:
        await message.answer("Корзина пуста 🤷", reply_markup=ReplyKeyboardRemove())
        return
    items = [(l.product.name, l.qty, l.product.price, l.product.unit) for l in lines]
    order_id = await ctx.db.create_order(user.id, user.full_name, user.username, data.get("phone", ""),
                                         data.get("comment", ""), items, cart_total(lines))
    await ctx.db.cart_clear(user.id)
    order = await ctx.db.order(order_id)
    await message.answer(f"🎉 Заказ №{order_id} оформлен! Мы скоро свяжемся с вами.\n\n"
                         + format_order(order, ctx.catalog.currency), reply_markup=ReplyKeyboardRemove())
    if ctx.settings.owner_id:
        try:
            await bot.send_message(ctx.settings.owner_id, "🔔 <b>Новый заказ!</b>\n\n"
                                   + format_order(order, ctx.catalog.currency, for_owner=True),
                                   reply_markup=kb.owner_order_kb(order_id))
        except Exception as exc:  # noqa: BLE001
            logger.error("Не удалось уведомить владельца о заказе %s: %s", order_id, exc)


@router.message(Checkout.confirm)
async def checkout_confirm_other(message: Message) -> None:
    await message.answer("Нажмите «✅ Подтвердить» или «❌ Отмена».", reply_markup=kb.confirm_kb())


@router.callback_query(kb.OrderStatus.filter())
async def order_status(call: CallbackQuery, callback_data: kb.OrderStatus, ctx: AppContext, bot: Bot) -> None:
    if not ctx.is_owner(call.from_user.id):
        await call.answer("Только для владельца", show_alert=True)
        return
    order = await ctx.db.order(callback_data.order_id)
    if not order:
        await call.answer("Заказ не найден", show_alert=True)
        return
    await ctx.db.set_order_status(order.id, callback_data.status)
    order.status = callback_data.status
    await _edit(call, format_order(order, ctx.catalog.currency, for_owner=True), kb.owner_order_kb(order.id))
    await call.answer(f"Статус: {STATUSES[callback_data.status]}")
    note = STATUS_FOR_CUSTOMER.get(callback_data.status)
    if note:
        try:
            await bot.send_message(order.user_id, note.format(id=order.id))
        except Exception as exc:  # noqa: BLE001 — клиент мог заблокировать бота
            logger.warning("Не удалось уведомить клиента о заказе %s: %s", order.id, exc)


@router.message(Command("myorders"), F.chat.type == "private")
async def my_orders(message: Message, ctx: AppContext) -> None:
    orders = await ctx.db.orders_of(message.from_user.id)
    if not orders:
        await message.answer("Заказов пока нет. Каталог — /catalog")
        return
    await message.answer("\n\n".join(format_order(o, ctx.catalog.currency) for o in orders))


@router.message(Command("orders"))
async def all_orders(message: Message, ctx: AppContext) -> None:
    if not ctx.is_owner(message.from_user.id) or message.chat.type != "private":
        return
    orders = await ctx.db.recent_orders()
    if not orders:
        await message.answer("Открытых заказов нет 👌")
        return
    for o in orders:
        await message.answer(format_order(o, ctx.catalog.currency, for_owner=True),
                             reply_markup=kb.owner_order_kb(o.id))
