"""Отправка сообщений сразу в обе сети: группа Telegram и беседа ВКонтакте,
уведомления менеджерам о заказах, сообщения покупателям.

Все тексты — HTML (как в Telegram); для ВКонтакте они переводятся в обычный текст.
Telegram-клавиатуры импортируются внутри функций, чтобы модуль работал и без aiogram
(например, если бот запущен только для ВКонтакте или в тестах).
"""
from __future__ import annotations

import logging
from html import escape

from app.config import Settings
from app.core.birthdays import order_on_birthday
from app.core.orders import (Catalog, OrderView, STATUS_FOR_CUSTOMER, build_lines, cart_total, fmt_qty,
                             format_final_message, format_manager_order, has_weight_items, money, order_has_weight)
from app.services.db import Database
from app.vk import keyboards as vk_kb
from app.vk.format import html_to_vk

logger = logging.getLogger(__name__)


class Messenger:
    def __init__(self, settings: Settings, db: Database, catalog: Catalog):
        self.settings = settings
        self.db = db
        self.catalog = catalog
        self.tg = None            # aiogram.Bot
        self.tg_username = ""
        self.vk = None            # app.vk.api.VkApi
        self.vk_screen_name = ""
        self._vk_resolved: dict[str, int | None] = {}

    # ── ссылки «оформить заказ» ──────────────────────────────
    def tg_order_url(self) -> str:
        return f"https://t.me/{self.tg_username}?start=order" if self.tg_username else ""

    def vk_order_url(self) -> str:
        name = self.vk_screen_name or f"club{self.settings.vk_group_id}"
        return f"https://vk.me/{name}"

    # ── кто менеджер ─────────────────────────────────────────
    async def tg_manager_ids(self) -> list[int]:
        ids = []
        for handle in self.settings.tg_managers:
            if handle.lstrip("-").isdigit():
                ids.append(int(handle))
            elif (uid := await self.db.user_id_by_username("tg", handle)) is not None:
                ids.append(uid)
            else:
                logger.warning("Менеджер @%s ещё не написал боту /start в Telegram — уведомления не дойдут", handle)
        return ids

    async def _vk_resolve(self, handle: str) -> int | None:
        if handle.isdigit():
            return int(handle)
        if handle.startswith("id") and handle[2:].isdigit():
            return int(handle[2:])
        if handle not in self._vk_resolved and self.vk is not None:
            try:
                res = await self.vk.call("utils.resolveScreenName", screen_name=handle)
                self._vk_resolved[handle] = res["object_id"] if res and res.get("type") == "user" else None
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не удалось найти пользователя ВК %s: %s", handle, exc)
                return None
        return self._vk_resolved.get(handle)

    async def vk_ids(self, handles: tuple[str, ...]) -> list[int]:
        ids = []
        for handle in handles:
            if (uid := await self._vk_resolve(handle)) is not None:
                ids.append(uid)
        return ids

    async def vk_manager_ids(self) -> list[int]:
        return await self.vk_ids(self.settings.vk_managers)

    async def vk_admin_ids(self) -> list[int]:
        return await self.vk_ids(self.settings.vk_admins)

    async def is_manager(self, platform: str, user_id: int) -> bool:
        if platform == "tg":
            return user_id == self.settings.owner_id or user_id in await self.tg_manager_ids()
        return user_id in await self.vk_manager_ids() or user_id in await self.vk_admin_ids()

    # ── низкоуровневая отправка ──────────────────────────────
    async def _tg_send(self, chat_id: int, html: str, markup=None) -> bool:
        if self.tg is None or not chat_id:
            return False
        try:
            await self.tg.send_message(chat_id, html, reply_markup=markup)
            return True
        except Exception as exc:  # noqa: BLE001 — человек мог заблокировать бота
            logger.warning("Telegram %s: не отправлено: %s", chat_id, exc)
            return False

    async def _vk_send(self, peer_id: int, html: str, keyboard: dict | None = None) -> bool:
        if self.vk is None or not peer_id:
            return False
        try:
            await self.vk.send(peer_id, html_to_vk(html), keyboard=keyboard)
            return True
        except Exception as exc:  # noqa: BLE001 — например, человек не разрешил сообщения от сообщества
            logger.warning("ВК %s: не отправлено: %s", peer_id, exc)
            return False

    async def to_user(self, platform: str, user_id: int, html: str) -> bool:
        if platform == "vk":
            return await self._vk_send(user_id, html)
        return await self._tg_send(user_id, html)

    # ── рассылки ─────────────────────────────────────────────
    async def to_groups(self, html: str, order_button: bool = False) -> None:
        """В группу Telegram и беседу ВКонтакте (какие настроены)."""
        tg_markup = None
        if order_button and self.tg is not None and self.tg_order_url():
            from app import keyboards as tg_kb
            tg_markup = tg_kb.order_button(self.tg_order_url())
        await self._tg_send(self.settings.group_chat_id, html, tg_markup)
        vk_keyboard = vk_kb.chat_catalog() if order_button else None
        await self._vk_send(self.settings.vk_chat_peer_id, html, vk_keyboard)

    async def to_admins(self, html: str) -> None:
        """Служебные напоминания: владельцу в Telegram и администраторам ВК."""
        await self._tg_send(self.settings.owner_id, html)
        for uid in await self.vk_admin_ids():
            await self._vk_send(uid, html)

    # ── заказы ───────────────────────────────────────────────
    async def manager_text(self, order: OrderView) -> str:
        """Текст заказа для менеджера (с днём рождения покупателя, если он известен)."""
        bd = await self.db.birthday_of(order.platform, order.user_id)
        today = bool(bd) and order_on_birthday(bd, order.created_at)   # заказ сделан в день рождения → пометка «подарок»
        return format_manager_order(order, self.catalog.currency, bd.date_text() if bd else None, today)

    async def notify_new_order(self, order: OrderView) -> int:
        """Шлёт заказ всем менеджерам с кнопками статусов. Возвращает, скольким дошло."""
        text = await self.manager_text(order)
        delivered = 0
        if self.tg is not None:
            from app import keyboards as tg_kb
            for uid in await self.tg_manager_ids():
                delivered += await self._tg_send(uid, text, tg_kb.owner_order_kb(order.id))
        for uid in await self.vk_manager_ids():
            delivered += await self._vk_send(uid, text, vk_kb.order_status(order.id, weigh=order_has_weight(order)))
        if not delivered:
            logger.error("Заказ №%s: ни одному менеджеру не удалось отправить уведомление!", order.id)
        return delivered

    async def notify_customer(self, order: OrderView) -> None:
        note = STATUS_FOR_CUSTOMER.get(order.status)
        if not note:
            return
        text = note.format(id=order.id)
        if order.platform == "vk" and order.status == "done":   # заказ выполнен — сразу предлагаем повторить
            await self._vk_send(order.user_id, text, vk_kb.repeat_button(order.id))
            return
        await self.to_user(order.platform, order.user_id, text)

    async def remind_cart(self, user_id: int) -> bool:
        """Мягкое напоминание о брошенной корзине (ВК). False — корзина уже пуста или сообщение не дошло."""
        lines = build_lines(await self.db.cart("vk", user_id), self.catalog)
        if not lines:
            return False
        approx = has_weight_items(l.product.unit for l in lines)
        rows = ["🛒 Кажется, вы не успели оформить заказ — товары ждут вас в корзине:", ""]
        rows += [f"• {escape(l.product.name)} — {fmt_qty(l.qty)} {escape(l.product.unit)}" for l in lines]
        rows += ["", f"Итого {'≈ ' if approx else ''}{money(cart_total(lines), self.catalog.currency)}", "",
                 "Если нужна помощь с выбором или остались вопросы — просто напишите, подскажем 🙂"]
        return await self._vk_send(user_id, "\n".join(rows), vk_kb.cart_reminder())

    async def ask_review(self, order: OrderView) -> bool:
        """Просьба оценить выданный заказ (ВК): пять кнопок-звёзд."""
        text = (f"💛 <b>Как вам заказ №{order.id}?</b>\n\n"
                "Надеемся, всё пришлось по вкусу! Оцените, пожалуйста, — нам очень важно ваше мнение.")
        return await self._vk_send(order.user_id, text, vk_kb.review_stars(order.id))

    async def notify_final(self, order: OrderView) -> bool:
        """Итог после взвешивания и реквизиты — покупателю. False — не дошло (например, закрыты сообщения)."""
        text = format_final_message(order, self.catalog.currency, self.settings.payment_details)
        return await self.to_user(order.platform, order.user_id, text)

    async def notify_handoff(self, html: str) -> int:
        """Вопрос покупателя, на который бот не смог ответить: всем менеджерам. Возвращает, скольким дошло."""
        delivered = 0
        for uid in await self.tg_manager_ids():
            delivered += await self._tg_send(uid, html)
        for uid in await self.vk_manager_ids():
            delivered += await self._vk_send(uid, html)
        if not delivered:
            logger.error("Вопрос покупателя некому передать: ни один менеджер не получил сообщение!")
        return delivered

    async def notify_market_order(self, html: str) -> None:
        """Заказ, оформленный через раздел «Товары» ВКонтакте."""
        for uid in await self.tg_manager_ids():
            await self._tg_send(uid, html)
        for uid in await self.vk_manager_ids():
            await self._vk_send(uid, html)
