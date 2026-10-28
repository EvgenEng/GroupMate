"""Кнопки и callback-данные."""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.core.orders import Catalog, CartLine, STATUSES, fmt_qty


class Shop(CallbackData, prefix="shop"):
    action: str          # cats | cat | prod | add | sub | cart | clear | checkout
    value: str = ""


class OrderStatus(CallbackData, prefix="ost"):
    order_id: int
    status: str


def order_button(url: str, text: str = "🛍 Сделать заказ") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])


def welcome_keyboard(order_url: str, help_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Сделать заказ", url=order_url)],
        [InlineKeyboardButton(text="ℹ️ Что умеет бот", url=help_url)],
    ])


def categories_kb(catalog: Catalog) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in catalog.categories:
        if catalog.in_category(c.id):
            kb.button(text=c.title, callback_data=Shop(action="cat", value=c.id))
    kb.button(text="🛒 Корзина", callback_data=Shop(action="cart"))
    kb.adjust(1)
    return kb.as_markup()


def products_kb(catalog: Catalog, category_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in catalog.in_category(category_id):
        kb.button(text=f"{p.name} — {int(p.price)} {catalog.currency}/{p.unit}",
                  callback_data=Shop(action="prod", value=p.id))
    kb.button(text="⬅️ Категории", callback_data=Shop(action="cats"))
    kb.button(text="🛒 Корзина", callback_data=Shop(action="cart"))
    kb.adjust(*([1] * len(catalog.in_category(category_id))), 2)
    return kb.as_markup()


def product_kb(product_id: str, category_id: str, in_cart: float, unit: str = "шт") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if in_cart:
        kb.button(text="➖", callback_data=Shop(action="sub", value=product_id))
        kb.button(text=f"🛒 {fmt_qty(in_cart)} {unit}", callback_data=Shop(action="cart"))
        kb.button(text="➕", callback_data=Shop(action="add", value=product_id))
    else:
        kb.button(text="➕ В корзину", callback_data=Shop(action="add", value=product_id))
    kb.button(text="⬅️ Назад", callback_data=Shop(action="cat", value=category_id))
    kb.button(text="🛒 Корзина", callback_data=Shop(action="cart"))
    kb.adjust(3 if in_cart else 1, 2)
    return kb.as_markup()


def cart_kb(lines: list[CartLine]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for line in lines:
        kb.button(text=f"➖ {line.product.name}", callback_data=Shop(action="sub", value=line.product.id))
        kb.button(text="➕", callback_data=Shop(action="add", value=line.product.id))
    rows = [2] * len(lines)
    if lines:
        kb.button(text="✅ Оформить заказ", callback_data=Shop(action="checkout"))
        kb.button(text="🗑 Очистить", callback_data=Shop(action="clear"))
        rows += [1, 1]
    kb.button(text="📋 Каталог", callback_data=Shop(action="cats"))
    rows.append(1)
    kb.adjust(*rows)
    return kb.as_markup()


def owner_order_kb(order_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for status in ("accepted", "ready", "done", "cancelled"):
        kb.button(text=STATUSES[status], callback_data=OrderStatus(order_id=order_id, status=status))
    kb.adjust(2)
    return kb.as_markup()


def contact_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
        input_field_placeholder="Или напишите телефон текстом",
    )


def skip_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Без комментария")]],
                               resize_keyboard=True, one_time_keyboard=True)


def confirm_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="✅ Подтвердить"), KeyboardButton(text="❌ Отмена")]],
                               resize_keyboard=True, one_time_keyboard=True)
