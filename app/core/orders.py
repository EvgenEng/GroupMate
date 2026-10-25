"""Каталог, корзина и тексты заказов. Без Telegram и БД — легко тестировать."""
from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from pathlib import Path


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


@dataclass
class Catalog:
    currency: str
    categories: list[Category]
    products: list[Product]

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        if not path.exists():
            return cls(currency="₽", categories=[], products=[])
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            currency=data.get("currency", "₽"),
            categories=[Category(c["id"], c["title"]) for c in data.get("categories", [])],
            products=[Product(
                id=str(p["id"]), category=p.get("category", ""), name=p["name"],
                price=float(p.get("price", 0)), unit=p.get("unit", "шт"),
                description=p.get("description", ""), available=p.get("available", True),
            ) for p in data.get("products", [])],
        )

    def product(self, product_id: str) -> Product | None:
        return next((p for p in self.products if p.id == product_id), None)

    def in_category(self, category_id: str) -> list[Product]:
        return [p for p in self.products if p.category == category_id and p.available]

    def category(self, category_id: str) -> Category | None:
        return next((c for c in self.categories if c.id == category_id), None)


def money(value: float, currency: str = "₽") -> str:
    whole = int(round(value))
    text = f"{whole:,}".replace(",", " ")
    return f"{text} {currency}"


@dataclass
class CartLine:
    product: Product
    qty: int

    @property
    def total(self) -> float:
        return self.product.price * self.qty


def build_lines(cart: dict[str, int], catalog: Catalog) -> list[CartLine]:
    lines = []
    for pid, qty in cart.items():
        product = catalog.product(pid)
        if product and qty > 0:
            lines.append(CartLine(product, qty))
    return lines


def cart_total(lines: list[CartLine]) -> float:
    return sum(line.total for line in lines)


def format_cart(lines: list[CartLine], currency: str) -> str:
    if not lines:
        return "🛒 Корзина пуста. Загляните в каталог — /catalog"
    rows = ["🛒 <b>Ваша корзина</b>", ""]
    for i, line in enumerate(lines, 1):
        p = line.product
        rows.append(f"{i}. {escape(p.name)} — {line.qty} {escape(p.unit)} × "
                    f"{money(p.price, currency)} = <b>{money(line.total, currency)}</b>")
    rows.append("")
    rows.append(f"Итого: <b>{money(cart_total(lines), currency)}</b>")
    return "\n".join(rows)


def format_product(p: Product, currency: str, in_cart: int = 0) -> str:
    rows = [f"<b>{escape(p.name)}</b>", f"💰 {money(p.price, currency)} / {escape(p.unit)}"]
    if p.description:
        rows += ["", escape(p.description)]
    if in_cart:
        rows += ["", f"🛒 В корзине: {in_cart} {escape(p.unit)}"]
    return "\n".join(rows)


STATUSES = {
    "new": "🆕 Новый",
    "accepted": "✅ Принят",
    "ready": "📦 Готов",
    "done": "🏁 Выполнен",
    "cancelled": "❌ Отменён",
}

STATUS_FOR_CUSTOMER = {
    "accepted": "✅ Ваш заказ №{id} принят! Мы свяжемся с вами для уточнения деталей.",
    "ready": "📦 Ваш заказ №{id} готов!",
    "done": "🏁 Заказ №{id} выполнен. Спасибо, что выбираете нас! 💛",
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
    items: list[tuple[str, int, float, str]]  # name, qty, price, unit
    total: float
    status: str
    created_at: str


def format_order(o: OrderView, currency: str, for_owner: bool = False) -> str:
    rows = [f"🧾 <b>Заказ №{o.id}</b> — {STATUSES.get(o.status, o.status)}", ""]
    for name, qty, price, unit in o.items:
        rows.append(f"• {escape(name)} — {qty} {escape(unit)} × {money(price, currency)}")
    rows += ["", f"Итого: <b>{money(o.total, currency)}</b>"]
    if for_owner:
        who = f'<a href="tg://user?id={o.user_id}">{escape(o.customer_name)}</a>'
        if o.username:
            who += f" (@{escape(o.username)})"
        rows += ["", f"👤 {who}", f"📞 {escape(o.phone)}"]
    if o.comment:
        rows.append(f"💬 {escape(o.comment)}")
    rows.append(f"🕒 {escape(o.created_at)}")
    return "\n".join(rows)
