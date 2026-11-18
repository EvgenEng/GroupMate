"""Ввод промокода: проверка, защита от повторов и подбора, запись в базу. Общее для Telegram и ВК."""
from __future__ import annotations

from datetime import date

from app.core.promo import PromoBook, PromoResult, normalize_code
from app.services.db import Database

FAIL_CAP = 10   # неверных попыток в сутки на человека


class PromoService:
    def __init__(self, db: Database, book: PromoBook):
        self.db = db
        self.book = book

    def known(self, raw: str) -> bool:
        """Текст — это один из наших промокодов (даже выключенный). Нужно, чтобы понять «просто написал код»."""
        return self.book.find(raw) is not None

    def label(self, key: str) -> str:
        """Как показывать код в заказе: так, как он написан в config/promo.json."""
        promo = self.book.codes.get(key)
        return promo.code if promo else key

    async def redeem(self, platform: str, user_id: int, raw: str) -> PromoResult:
        if not normalize_code(raw):
            return PromoResult("empty")
        day = date.today().isoformat()
        if await self.db.promo_fails(platform, user_id, day) >= FAIL_CAP:
            return PromoResult("blocked")
        promo = self.book.find(raw)
        if promo is None:
            await self.db.promo_fail_hit(platform, user_id, day)
            return PromoResult("unknown")
        if not promo.active:
            return PromoResult("inactive", promo)
        if promo.first_order_only and await self.db.has_orders(platform, user_id):
            return PromoResult("not_first", promo)
        if not await self.db.promo_activate(promo.key, platform, user_id):
            return PromoResult("used", promo)
        return PromoResult("ok", promo)
