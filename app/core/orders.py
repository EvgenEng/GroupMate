"""Каталог, корзина и тексты заказов. Без Telegram/ВК и БД — легко тестировать.

Тексты собираются в HTML (как в Telegram). Для ВКонтакте их переводит в обычный
текст app/vk/format.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

PLATFORM_NAMES = {"tg": "Telegram", "vk": "ВКонтакте"}


@dataclass(frozen=True)
class Category:
    id: str
    title: str


@dataclass(frozen=True)
class Product:
    id: str
    category: str
    name: str
    price: float
    unit: str = "шт"
    description: str = ""
    available: bool = True
    step: float = 1.0  # шаг заказа: 1 шт, 0.5 кг и т.п.

    @property
    def by_weight(self) -> bool:
        return self.unit in {"кг", "г"}


@dataclass
class Shop:
    name: str = ""
    about: str = ""
    lead_time: str = ""
    footnote: str = ""
    birthday_offer: str = ""   # текст-предложение указать день рождения после заказа


@dataclass
class Catalog:
    currency: str
    categories: list[Category]
    products: list[Product]
    shop: Shop = field(default_factory=Shop)

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        if not path.exists():
            return cls(currency="₽", categories=[], products=[])
        data = json.loads(path.read_text(encoding="utf-8"))
        shop = data.get("shop", {})
        return cls(
            currency=data.get("currency", "₽"),
            categories=[Category(c["id"], c["title"]) for c in data.get("categories", [])],
            products=[Product(
                id=str(p["id"]), category=p.get("category", ""), name=p["name"],
                price=float(p.get("price", 0)), unit=p.get("unit", "шт"),
                description=p.get("description", ""), available=p.get("available", True),
                step=float(p.get("step", 1)) or 1.0,
            ) for p in data.get("products", [])],
            shop=Shop(name=shop.get("name", ""), about=shop.get("about", ""),
                      lead_time=shop.get("lead_time", ""), footnote=shop.get("footnote", ""),
                      birthday_offer=shop.get("birthday_offer", "")),
        )

    def product(self, product_id: str) -> Product | None:
        return next((p for p in self.products if p.id == product_id), None)

    def in_category(self, category_id: str) -> list[Product]:
        return [p for p in self.products if p.category == category_id and p.available]

    def category(self, category_id: str) -> Category | None:
        return next((c for c in self.categories if c.id == category_id), None)

    def visible_categories(self) -> list[Category]:
        return [c for c in self.categories if self.in_category(c.id)]


def money(value: float, currency: str = "₽") -> str:
    whole = int(round(value))
    text = f"{whole:,}".replace(",", " ")
    return f"{text} {currency}"


def fmt_qty(qty: float) -> str:
    """1.0 → «1», 1.5 → «1,5»."""
    if abs(qty - round(qty)) < 1e-9:
        return str(int(round(qty)))
    return f"{qty:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def price_label(p: Product, currency: str) -> str:
    return f"{money(p.price, currency)}/{p.unit}"


def catalog_intro(catalog: Catalog) -> str:
    shop = catalog.shop
    rows = []
    if shop.name:
        rows.append(f"🛍 <b>{escape(shop.name)} — каталог</b>")
    else:
        rows.append("🛍 <b>Каталог</b>")
    if shop.about:
        rows += ["", escape(shop.about)]
    if shop.lead_time:
        rows += ["", f"⏳ {escape(shop.lead_time)}"]
    rows += ["", "Выберите раздел:"]
    return "\n".join(rows)


@dataclass
class CartLine:
    product: Product
    qty: float

    @property
    def total(self) -> float:
        return self.product.price * self.qty


def build_lines(cart: dict[str, float], catalog: Catalog) -> list[CartLine]:
    lines = []
    for pid, qty in cart.items():
        product = catalog.product(pid)
        if product and qty > 0:
            lines.append(CartLine(product, qty))
    return lines


def cart_total(lines: list[CartLine]) -> float:
    return sum(line.total for line in lines)


def has_weight_items(units) -> bool:
    return any(u in {"кг", "г"} for u in units)


def _total_row(total: float, currency: str, approx: bool) -> list[str]:
    if approx:
        return [f"Итого: <b>≈ {money(total, currency)}</b>",
                "<i>Вес примерный — точная сумма после взвешивания.</i>"]
    return [f"Итого: <b>{money(total, currency)}</b>"]


def format_cart(lines: list[CartLine], currency: str) -> str:
    if not lines:
        return "🛒 Корзина пуста. Загляните в каталог 🙂"
    rows = ["🛒 <b>Ваша корзина</b>", ""]
    for i, line in enumerate(lines, 1):
        p = line.product
        rows.append(f"{i}. {escape(p.name)} — {fmt_qty(line.qty)} {escape(p.unit)} × "
                    f"{money(p.price, currency)} = <b>{money(line.total, currency)}</b>")
    rows.append("")
    rows += _total_row(cart_total(lines), currency, has_weight_items(l.product.unit for l in lines))
    return "\n".join(rows)


def format_product(p: Product, currency: str, in_cart: float = 0) -> str:
    rows = [f"<b>{escape(p.name)}</b>", f"💰 {money(p.price, currency)} / {escape(p.unit)}"]
    if p.description:
        rows += ["", escape(p.description)]
    if p.by_weight:
        rows += ["", f"⚖️ Заказ кратно {fmt_qty(p.step)} {escape(p.unit)}. Вес примерный — сумма по факту."]
    if in_cart:
        rows += ["", f"🛒 В корзине: {fmt_qty(in_cart)} {escape(p.unit)} ≈ {money(in_cart * p.price, currency)}"]
    return "\n".join(rows)


STATUSES = {
    "new": "🆕 Новый",
    "accepted": "✅ Принят",
    "ready": "📦 Готов",
    "done": "🏁 Выполнен",
    "cancelled": "❌ Отменён",
}

STATUS_FOR_CUSTOMER = {
    "accepted": "✅ Ваш заказ №{id} принят в работу! Мы свяжемся с вами, чтобы уточнить детали.",
    "ready": "📦 Ваш заказ №{id} готов! Скоро свяжемся, чтобы договориться о передаче.",
    "done": "🏁 Заказ №{id} выполнен. Спасибо, что выбираете нас! Приятного аппетита 💛",
    "cancelled": "❌ Заказ №{id} отменён. Если это ошибка — просто напишите нам.",
}


@dataclass
class OrderView:
    id: int
    customer_name: str
    username: str | None
    user_id: int
    phone: str
    comment: str
    items: list[tuple[str, float, float, str]]  # name, qty, price, unit
    total: float
    status: str
    created_at: str
    platform: str = "tg"


def customer_link(platform: str, user_id: int, name: str, username: str | None = None) -> str:
    """HTML-ссылка на профиль. Для ВК app/vk/format.py превратит её в упоминание [id|Имя]."""
    name = escape(name or "Покупатель")
    if platform == "vk":
        return f'<a href="https://vk.ru/id{user_id}">{name}</a>'
    link = f'<a href="tg://user?id={user_id}">{name}</a>'
    return f"{link} (@{escape(username)})" if username else link


def format_order(o: OrderView, currency: str, for_owner: bool = False) -> str:
    rows = [f"🧾 <b>Заказ №{o.id}</b> — {STATUSES.get(o.status, o.status)}", ""]
    for name, qty, price, unit in o.items:
        rows.append(f"• {escape(name)} — {fmt_qty(qty)} {escape(unit)} × {money(price, currency)}")
    rows.append("")
    rows += _total_row(o.total, currency, has_weight_items(unit for *_, unit in o.items))
    if for_owner:
        rows += ["", f"👤 {customer_link(o.platform, o.user_id, o.customer_name, o.username)} "
                     f"· {PLATFORM_NAMES.get(o.platform, o.platform)}",
                 f"📞 {escape(o.phone)}"]
    if o.comment:
        rows.append(f"💬 {escape(o.comment)}")
    rows.append(f"🕒 {escape(o.created_at)}")
    return "\n".join(rows)


def format_manager_order(o: OrderView, currency: str, birthday: str | None = None,
                         birthday_today: bool = False) -> str:
    """Заказ для менеджера: позиции и количество, ФИО, телефон."""
    rows = [f"🔔 <b>Заказ №{o.id}</b> — {STATUSES.get(o.status, o.status)}"]
    if birthday_today:
        rows.append("🎁 <b>Именинник!</b> Заказ в день рождения — положите вкусный подарок и поздравьте 🎉")
    rows.append("")
    for name, qty, _price, unit in o.items:
        rows.append(f"• {escape(name)} — {fmt_qty(qty)} {escape(unit)}")
    approx = has_weight_items(unit for *_, unit in o.items)
    rows += ["", f"💰 {'≈ ' if approx else ''}{money(o.total, currency)}", "",
             f"👤 {customer_link(o.platform, o.user_id, o.customer_name, o.username)}",
             f"📞 {escape(o.phone)}"]
    if birthday:
        rows.append(f"🎂 День рождения: {escape(birthday)}")
    if o.comment:
        rows.append(f"💬 {escape(o.comment)}")
    rows.append(f"🕒 {escape(o.created_at)} · {PLATFORM_NAMES.get(o.platform, o.platform)}")
    return "\n".join(rows)


def checkout_comment_hint(catalog: Catalog) -> str:
    text = ("💬 Напишите пожелания к заказу: к какому дню нужен, как удобнее получить, "
            "нарезка и т.п. Или нажмите «Без комментария».")
    if catalog.shop.lead_time:
        text += f"\n\n⏳ {escape(catalog.shop.lead_time)}"
    return text
