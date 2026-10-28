"""Логика заказов, общая для Telegram и ВКонтакте: корзина, оформление, статусы."""
from __future__ import annotations

from app.core.orders import Catalog, CartLine, OrderView, build_lines, cart_total
from app.services.db import Database

MAX_QTY = {"кг": 20, "г": 5000}  # разумный предел на одну позицию


class OrderService:
    def __init__(self, db: Database, catalog: Catalog):
        self.db = db
        self.catalog = catalog

    async def lines(self, platform: str, user_id: int) -> list[CartLine]:
        return build_lines(await self.db.cart(platform, user_id), self.catalog)

    async def qty(self, platform: str, user_id: int, product_id: str) -> float:
        return (await self.db.cart(platform, user_id)).get(product_id, 0.0)

    async def change(self, platform: str, user_id: int, product_id: str, direction: int) -> float | None:
        """+1 / −1 шаг товара. None — товара нет в каталоге."""
        product = self.catalog.product(product_id)
        if not product:
            return None
        return await self.db.cart_change(platform, user_id, product_id, direction * product.step,
                                         max_qty=MAX_QTY.get(product.unit, 99))

    async def clear(self, platform: str, user_id: int) -> None:
        await self.db.cart_clear(platform, user_id)

    async def place(self, platform: str, user_id: int, name: str, username: str | None,
                    phone: str, comment: str) -> OrderView | None:
        lines = await self.lines(platform, user_id)
        if not lines:
            return None
        items = [(l.product.name, l.qty, l.product.price, l.product.unit) for l in lines]
        order_id = await self.db.create_order(platform, user_id, name, username, phone, comment,
                                              items, cart_total(lines))
        await self.db.cart_clear(platform, user_id)
        return await self.db.order(order_id)

    async def set_status(self, order_id: int, status: str) -> OrderView | None:
        order = await self.db.order(order_id)
        if not order:
            return None
        await self.db.set_order_status(order_id, status)
        order.status = status
        return order
