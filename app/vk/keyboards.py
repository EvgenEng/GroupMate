"""Клавиатуры ВКонтакте.

Ограничения ВК для инлайн-клавиатуры: до 6 рядов и до 10 кнопок, подпись до 40 символов,
payload до 255 символов. Поэтому товары показываем страницами по 4.
"""
from __future__ import annotations

import json

from app.core.orders import Catalog, CartLine, STATUSES, fmt_qty

LABEL_MAX = 40
PAGE_SIZE = 4
INLINE_MAX_ROWS = 6
INLINE_MAX_BUTTONS = 10


def _label(text: str) -> str:
    return text if len(text) <= LABEL_MAX else text[:LABEL_MAX - 1] + "…"


def _payload(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def callback(label: str, data: dict, color: str | None = None) -> dict:
    button = {"action": {"type": "callback", "label": _label(label), "payload": _payload(data)}}
    if color:
        button["color"] = color
    return button


def text(label: str, data: dict | None = None, color: str | None = None) -> dict:
    action = {"type": "text", "label": _label(label)}
    if data:
        action["payload"] = _payload(data)
    button = {"action": action}
    if color:
        button["color"] = color
    return button


def link(label: str, url: str) -> dict:
    return {"action": {"type": "open_link", "label": _label(label), "link": url}}


def inline(rows: list[list[dict]]) -> dict:
    return {"inline": True, "buttons": [row for row in rows if row]}


# ── главное меню в личке (обычная клавиатура под полем ввода) ──
MENU_COMMANDS = {
    "🛍 Каталог": "catalog",
    "🛒 Корзина": "cart",
    "🌤 Погода": "weather",
    "🌙 Погода на завтра": "tomorrow",
    "🧾 Мои заказы": "myorders",
    "🎂 Мой день рождения": "mybd_help",
    "ℹ️ Помощь": "help",
}


def main_menu() -> dict:
    labels = list(MENU_COMMANDS)
    rows = [labels[0:2], labels[2:4], labels[4:6], labels[6:7]]
    return {"one_time": False, "buttons": [
        [text(label, {"cmd": MENU_COMMANDS[label]}, "primary" if MENU_COMMANDS[label] == "catalog" else None)
         for label in row] for row in rows]}


# ── магазин ──────────────────────────────────────────────────
def categories(catalog: Catalog) -> dict:
    cats = catalog.visible_categories()[:INLINE_MAX_ROWS - 1]
    rows = [[callback(c.title, {"a": "cat", "v": c.id, "p": 0})] for c in cats]
    rows.append([callback("🛒 Корзина", {"a": "cart"})])
    return inline(rows)


def products(catalog: Catalog, category_id: str, page: int) -> dict:
    items = catalog.in_category(category_id)
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    rows = [[callback(f"{p.name} · {int(p.price)} ₽", {"a": "prod", "v": p.id})] for p in chunk]
    nav = []
    if page > 0:
        nav.append(callback("◀️", {"a": "cat", "v": category_id, "p": page - 1}))
    if page < pages - 1:
        nav.append(callback(f"Ещё ▶️ ({page + 2}/{pages})", {"a": "cat", "v": category_id, "p": page + 1}))
    rows.append(nav)
    rows.append([callback("⬅️ Разделы", {"a": "cats"}), callback("🛒 Корзина", {"a": "cart"})])
    return inline(rows)


def product(catalog: Catalog, product_id: str, qty: float) -> dict:
    p = catalog.product(product_id)
    back = {"a": "cat", "v": p.category, "p": page_of(catalog, product_id)} if p else {"a": "cats"}
    if qty:
        first = [callback("➖", {"a": "sub", "v": product_id}),
                 callback(f"🛒 {fmt_qty(qty)} {p.unit}", {"a": "cart"}, "positive"),
                 callback("➕", {"a": "add", "v": product_id})]
    else:
        first = [callback("➕ В корзину", {"a": "add", "v": product_id}, "positive")]
    return inline([first, [callback("⬅️ Назад", back), callback("🛒 Корзина", {"a": "cart"})]])


def page_of(catalog: Catalog, product_id: str) -> int:
    p = catalog.product(product_id)
    if not p:
        return 0
    ids = [x.id for x in catalog.in_category(p.category)]
    return ids.index(product_id) // PAGE_SIZE if product_id in ids else 0


CART_EDITABLE_LINES = 3


def cart(lines: list[CartLine]) -> dict:
    rows = []
    for line in lines[:CART_EDITABLE_LINES]:
        name = line.product.name
        rows.append([callback(f"➖ {name}", {"a": "sub", "v": line.product.id, "c": 1}),
                     callback("➕", {"a": "add", "v": line.product.id, "c": 1})])
    if lines:
        rows.append([callback("✅ Оформить заказ", {"a": "checkout"}, "positive")])
        rows.append([callback("🗑 Очистить", {"a": "clear"}, "negative"), callback("📋 Каталог", {"a": "cats"})])
    else:
        rows.append([callback("📋 Каталог", {"a": "cats"}, "primary")])
    return inline(rows)


def comment_skip() -> dict:
    return inline([[text("Без комментария", {"cmd": "no_comment"})],
                   [callback("❌ Отменить оформление", {"a": "co_cancel"}, "negative")]])


def phone_cancel() -> dict:
    return inline([[callback("❌ Отменить оформление", {"a": "co_cancel"}, "negative")]])


def confirm() -> dict:
    return inline([[callback("✅ Подтвердить", {"a": "co_ok"}, "positive"),
                    callback("❌ Отмена", {"a": "co_cancel"}, "negative")]])


def order_status(order_id: int) -> dict:
    keys = ["accepted", "ready", "done", "cancelled"]
    buttons = [callback(STATUSES[k], {"a": "ost", "o": order_id, "s": k}) for k in keys]
    return inline([buttons[:2], buttons[2:]])


def order_link(url: str, label: str = "🛍 Сделать заказ") -> dict:
    return inline([[link(label, url)]])


def cart_hint(lines: list[CartLine], currency: str) -> str:
    """Подсказка под корзиной, если кнопками можно поправить не все позиции."""
    if len(lines) <= CART_EDITABLE_LINES:
        return ""
    return "\n\nИзменить количество остальных позиций можно в карточке товара (📋 Каталог)."


def count_buttons(keyboard: dict) -> tuple[int, int]:
    rows = keyboard["buttons"]
    return len(rows), sum(len(r) for r in rows)

