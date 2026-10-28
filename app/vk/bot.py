"""Бот сообщества ВКонтакте.

В личных сообщениях сообщества: меню, каталог с корзиной, оформление заказа,
погода, дни рождения, разговор с нейросетью.
В беседе: погода, поздравления, напоминания (их шлёт планировщик через Messenger),
приветствие новичков, ответы на упоминания и команды.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from html import escape

from app import jobs
from app.context import AppContext
from app.core import birthdays as bd
from app.core.orders import STATUSES, catalog_intro, checkout_comment_hint, fmt_qty, format_cart, \
    format_order, format_product
from app.services import weather_reports
from app.services.llm import LLMError
from app.vk import keyboards as kb
from app.vk.api import VkApiError, is_chat
from app.vk.format import html_to_vk

logger = logging.getLogger(__name__)

PLATFORM = "vk"
PHONE_RE = re.compile(r"^\+?[\d\s\-()]{7,20}$")

# Команды словами. Целое сообщение должно совпасть с фразой (или начинаться с «/»).
PHRASES = {
    "start": {"начать", "start", "старт", "меню"},
    "help": {"help", "помощь", "команды", "что ты умеешь"},
    "weather": {"погода", "weather", "pogoda", "какая погода", "погода сегодня"},
    "tomorrow": {"tomorrow", "завтра", "погода завтра", "погода на завтра", "какая погода завтра"},
    "catalog": {"catalog", "каталог", "заказ", "order", "заказать", "сделать заказ"},
    "cart": {"cart", "корзина"},
    "myorders": {"myorders", "мои заказы"},
    "mybd_help": {"мой день рождения", "мой др"},
    "birthdays": {"birthdays", "bd", "дни рождения", "ближайшие дни рождения"},
    "id": {"id"},
    "orders": {"orders", "заказы"},
}
ARG_COMMANDS = {"mybd": "mybd", "др": "mybd", "bd_add": "bd_add", "bd_del": "bd_del"}
ADMIN_COMMANDS = {"test_morning", "test_evening", "test_bd", "bd_list", "bd_sync", "bd_add", "bd_del"}

WELCOMES = [
    "Привет, {name}! 👋 Добро пожаловать в беседу — располагайтесь как дома.",
    "{name}, рады видеть! 🎉 Теперь вы с нами.",
    "Ура, у нас пополнение! 🙌 {name}, добро пожаловать!",
]

LIMIT_TEXTS = [
    "На сегодня я выговорился 😅 Давайте продолжим завтра!",
    "Лимит разговоров на сегодня исчерпан. Погода, заказы и дни рождения работают как обычно 🙂",
]


def resolve_command(text: str, payload: dict | None) -> tuple[str | None, str]:
    """Текст или кнопка → (команда, аргумент)."""
    if payload:
        if payload.get("command") == "start":  # кнопка «Начать»
            return "start", ""
        if "cmd" in payload:
            return payload["cmd"], ""
    clean = text.strip()
    slash = clean.startswith("/")
    body = clean.lstrip("/").strip()
    low = body.lower().rstrip("!?.")
    for cmd, phrases in PHRASES.items():
        if low in phrases:
            return cmd, ""
    first, _, rest = body.partition(" ")
    first_low = first.lower()
    if first_low in ARG_COMMANDS:
        cmd = ARG_COMMANDS[first_low]
        # «др …» без слэша считаем командой, только если дальше дата или «удалить»
        if slash or first_low != "др" or bd.parse_day_month(rest.strip()) or rest.strip().lower() == "удалить":
            return cmd, rest.strip()
    if slash and first_low in ADMIN_COMMANDS | set(PHRASES):
        return first_low, rest.strip()
    return None, ""


class VkBot:
    def __init__(self, ctx: AppContext, api):
        self.ctx = ctx
        self.api = api
        self.group_id = ctx.settings.vk_group_id
        self.checkout: dict[int, dict] = {}   # оформление заказа: user_id → {"step": …, …}
        self._users: dict[int, dict] = {}
        self._greeted: dict[int, float] = {}
        self._tasks: set[asyncio.Task] = set()

    # ── запуск ────────────────────────────────────────────────
    async def start(self) -> None:
        try:
            info = await self.api.call("groups.getById", group_id=self.group_id)
            group = info["groups"][0] if isinstance(info, dict) else info[0]
            self.ctx.messenger.vk_screen_name = group.get("screen_name", "")
            logger.info("ВК: сообщество «%s» (vk.ru/%s)", group.get("name"), group.get("screen_name"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ВК: не удалось получить данные сообщества: %s", exc)

    async def run(self) -> None:
        await self.start()
        logger.info("ВК-бот запущен")
        async for update in self.api.listen():
            await self.handle(update)

    async def handle(self, update: dict) -> None:
        try:
            kind, obj = update.get("type"), update.get("object", {})
            if kind == "message_new":
                await self.on_message(obj.get("message", obj))
            elif kind == "message_event":
                await self.on_event(obj)
            elif kind == "message_allow":
                await self.cmd_start(obj["user_id"])
            elif kind == "market_order_new":
                await self.on_market_order(obj)
        except Exception:  # noqa: BLE001 — одно сломанное событие не должно ронять бота
            logger.exception("ВК: ошибка при обработке события %s", update.get("type"))

    # ── отправка ──────────────────────────────────────────────
    async def send(self, peer_id: int, html: str, keyboard: dict | None = None) -> None:
        try:
            await self.api.send(peer_id, html_to_vk(html), keyboard=keyboard)
        except VkApiError as exc:
            logger.warning("ВК: не отправлено в %s: %s", peer_id, exc)

    async def edit_or_send(self, peer_id: int, cmid: int | None, html: str, keyboard: dict | None) -> None:
        if cmid:
            try:
                await self.api.edit(peer_id, cmid, html_to_vk(html), keyboard=keyboard)
                return
            except VkApiError as exc:
                logger.debug("ВК: редактирование не удалось (%s), отправляю новое", exc)
        await self.send(peer_id, html, keyboard)

    # ── люди ──────────────────────────────────────────────────
    async def user(self, user_id: int) -> dict:
        if user_id not in self._users:
            try:
                res = await self.api.call("users.get", user_ids=user_id, fields="bdate,screen_name")
                self._users[user_id] = res[0] if res else {}
            except VkApiError as exc:
                logger.warning("ВК: users.get %s: %s", user_id, exc)
                return {}
        return self._users[user_id]

    async def full_name(self, user_id: int) -> str:
        u = await self.user(user_id)
        return " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or f"id{user_id}"

    @staticmethod
    def mention(user_id: int, name: str) -> str:
        return f'<a href="https://vk.ru/id{user_id}">{escape(name)}</a>'

    async def is_admin(self, user_id: int) -> bool:
        return user_id in await self.ctx.messenger.vk_admin_ids()

    async def is_manager(self, user_id: int) -> bool:
        return await self.ctx.messenger.is_manager(PLATFORM, user_id)

    # ── входящие сообщения ────────────────────────────────────
    def _strip_mention(self, text: str) -> tuple[str, bool]:
        """Убирает обращение к сообществу: «[club123|@gastrodomru] погода» → «погода»."""
        pattern = rf"^\s*\[club{self.group_id}\|[^\]]*\][\s,]*"
        new = re.sub(pattern, "", text)
        if new != text:
            return new, True
        screen = self.ctx.messenger.vk_screen_name
        for tag in (f"@{screen}", f"@club{self.group_id}"):
            if screen and text.lower().startswith(tag.lower()):
                return text[len(tag):].lstrip(" ,"), True
        return text, False

    async def on_message(self, msg: dict) -> None:
        peer_id, from_id = msg.get("peer_id", 0), msg.get("from_id", 0)
        if from_id <= 0:
            return  # сообщения других сообществ и ботов
        if msg.get("action"):
            await self.on_action(peer_id, from_id, msg["action"])
            return
        chat = is_chat(peer_id)
        text, mentioned = self._strip_mention(msg.get("text", "") or "")
        reply = msg.get("reply_message") or {}
        addressed = not chat or mentioned or reply.get("from_id") == -self.group_id
        try:
            payload = json.loads(msg["payload"]) if msg.get("payload") else None
        except (TypeError, ValueError):
            payload = None

        if not chat:
            await self.on_first_contact(from_id)
            state = self.checkout.get(from_id)
            menu_press = bool(payload) and (payload.get("command") == "start"
                                            or payload.get("cmd") not in (None, "no_comment"))
            if state and menu_press:
                self.checkout.pop(from_id, None)  # ушли в меню — оформление прерываем, корзина остаётся
            elif state:
                await self.checkout_input(from_id, text, payload)
                return

        cmd, arg = resolve_command(text, payload)
        if cmd:
            await self.dispatch(cmd, arg, peer_id, from_id, chat, reply)
        elif addressed and text.strip():
            self._spawn(self.talk(peer_id, from_id, text.strip(), chat))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def on_first_contact(self, user_id: int) -> None:
        u = await self.user(user_id)
        first_time = await self.ctx.db.touch_user(PLATFORM, user_id, u.get("first_name", ""), u.get("screen_name"))
        if not first_time:
            return
        # День рождения из профиля, если человек его не скрыл.
        parsed = bd.parse_vk_bdate(u.get("bdate"))
        if parsed and not await self.ctx.db.birthday_of(PLATFORM, user_id):
            await self.ctx.db.add_birthday(await self.full_name(user_id), parsed[0], parsed[1], PLATFORM,
                                           user_id, u.get("screen_name"))
            await self.send(user_id, f"🎂 Вижу в профиле день рождения {parsed[0]} {bd.MONTHS_RU[parsed[1] - 1]} — "
                                     f"обязательно поздравлю в беседе! Если не нужно, напишите «др удалить».")

    async def on_action(self, peer_id: int, from_id: int, action: dict) -> None:
        kind = action.get("type")
        if kind not in {"chat_invite_user", "chat_invite_user_by_link"}:
            return
        member = action.get("member_id") or from_id
        if member == -self.group_id:
            await self.send(peer_id, "👋 Привет! Я бот сообщества. Буду присылать сюда погоду, поздравлять "
                                     "с днями рождения и напоминать о заказах.\n\nАдминистратору: напишите здесь "
                                     "/id и впишите номер беседы в настройки бота (VK_CHAT_PEER_ID).\n"
                                     "Чтобы я видел команды без упоминания, сделайте меня администратором беседы.")
            return
        if member <= 0 or not self.ctx.settings.welcome_enabled:
            return
        target = self.ctx.settings.vk_chat_peer_id
        if target and peer_id != target:
            return
        name = self.mention(member, (await self.user(member)).get("first_name", "друг"))
        text = (random.choice(WELCOMES).format(name=name)
                + "\n\nЯ бот сообщества: по утрам рассказываю о погоде, поздравляю с днём рождения "
                  "и принимаю заказы в личных сообщениях сообщества 🛍")
        await self.send(peer_id, text, kb.order_link(self.ctx.messenger.vk_order_url()))

    # ── команды ───────────────────────────────────────────────
    async def dispatch(self, cmd: str, arg: str, peer_id: int, user_id: int, chat: bool, reply: dict) -> None:
        if cmd in {"start", "help"}:
            await (self.cmd_help(peer_id) if chat else self.cmd_start(user_id, force=cmd == "help"))
        elif cmd == "weather":
            text = await weather_reports.now_report(self.ctx.weather, self.ctx.today())
            await self.send(peer_id, text or "😔 Не получилось загрузить прогноз, попробуйте чуть позже.")
        elif cmd == "tomorrow":
            text = await weather_reports.evening_report(self.ctx.weather, self.ctx.today())
            await self.send(peer_id, text or "😔 Не получилось загрузить прогноз, попробуйте чуть позже.")
        elif cmd in {"catalog", "cart", "myorders"} and chat:
            await self.send(peer_id, "Заказы оформляются в личных сообщениях сообщества 👇",
                            kb.order_link(self.ctx.messenger.vk_order_url()))
        elif cmd == "catalog":
            await self.show_catalog(user_id)
        elif cmd == "cart":
            await self.show_cart(user_id, None)
        elif cmd == "myorders":
            orders = await self.ctx.db.orders_of(PLATFORM, user_id)
            text = ("\n\n".join(format_order(o, self.ctx.catalog.currency) for o in orders)
                    if orders else "Заказов пока нет. Загляните в каталог 🛍")
            await self.send(user_id, text)
        elif cmd == "mybd_help":
            await self.cmd_mybd_help(peer_id, user_id)
        elif cmd == "mybd":
            await self.cmd_mybd(peer_id, user_id, arg)
        elif cmd == "birthdays":
            await self.cmd_birthdays(peer_id)
        elif cmd == "id":
            await self.send(peer_id, f"ID этого чата (peer_id): {peer_id}\nВаш ID ВКонтакте: {user_id}")
        elif cmd == "orders":
            await self.cmd_orders(peer_id, user_id)
        elif cmd in ADMIN_COMMANDS:
            if await self.is_admin(user_id):
                await self.admin_command(cmd, arg, peer_id, reply)
        elif cmd == "no_comment":
            pass  # кнопка «Без комментария» вне оформления — игнорируем

    async def cmd_start(self, user_id: int, force: bool = False) -> None:
        now = asyncio.get_running_loop().time()
        if not force and now - self._greeted.get(user_id, -1e9) < 30:
            return  # «Начать» и «разрешил сообщения» часто приходят вместе — здороваемся один раз
        self._greeted[user_id] = now
        name = (await self.user(user_id)).get("first_name", "")
        shop = self.ctx.catalog.shop
        lines = [f"Здравствуйте{', ' + escape(name) if name else ''}! 👋"]
        if shop.name:
            lines.append(f"Я бот {escape(shop.name)}. {escape(shop.about)}")
        lines += ["", "Что умею:",
                  "🛍 Каталог и заказ — прямо здесь, кнопками",
                  "🌤 Погода в Москве и области — сегодня и на завтра",
                  "🎂 Поздравляю с днём рождения в беседе",
                  "💬 Могу просто поболтать — пишите что угодно",
                  "", "Меню — на кнопках внизу 👇"]
        await self.send(user_id, "\n".join(lines), kb.main_menu())

    async def cmd_help(self, peer_id: int) -> None:
        await self.send(peer_id, "👋 Я бот сообщества.\n\n"
                                 "В беседе: «погода», «погода завтра», «дни рождения», «каталог» — "
                                 "или обратитесь ко мне через @, и я отвечу.\n"
                                 "Заказы и запись дня рождения — в личных сообщениях сообщества 👇",
                        kb.order_link(self.ctx.messenger.vk_order_url(), "💬 Написать боту"))

    async def cmd_mybd_help(self, peer_id: int, user_id: int) -> None:
        mine = await self.ctx.db.birthday_of(PLATFORM, user_id)
        text = (f"🎂 Ваш день рождения у меня записан: {mine.date_text()}.\n\n" if mine
                else "🎂 Вашего дня рождения пока нет в списке.\n\n")
        text += "Записать или поменять: напишите «др 15.04» (день.месяц).\nУбрать: «др удалить»."
        await self.send(peer_id, text)

    async def cmd_mybd(self, peer_id: int, user_id: int, arg: str) -> None:
        if arg.lower() == "удалить":
            ok = await self.ctx.db.delete_birthday_of(PLATFORM, user_id)
            await self.send(peer_id, "🗑 Удалил вашу дату." if ok else "Вашей даты и так нет в списке.")
            return
        parsed = bd.parse_day_month(arg)
        if not parsed:
            await self.cmd_mybd_help(peer_id, user_id)
            return
        u = await self.user(user_id)
        await self.ctx.db.add_birthday(await self.full_name(user_id), parsed[0], parsed[1], PLATFORM,
                                       user_id, u.get("screen_name"))
        await self.send(peer_id, f"🎂 Записал! {parsed[0]} {bd.MONTHS_RU[parsed[1] - 1]} поздравлю вас в беседе 🎉")

    async def cmd_birthdays(self, peer_id: int) -> None:
        items = await self.ctx.db.list_birthdays()
        if not items:
            await self.send(peer_id, "Пока никто не записал день рождения. Напишите сообществу «др 15.04» 🎂")
            return
        rows = ["🎂 <b>Ближайшие дни рождения</b>", ""]
        for b, left in bd.upcoming(items, self.ctx.today(), limit=5):
            rows.append(f"• {escape(b.name)} — {b.date_text()} ({bd.days_word(left)})")
        await self.send(peer_id, "\n".join(rows))

    async def cmd_orders(self, peer_id: int, user_id: int) -> None:
        if is_chat(peer_id) or not await self.is_manager(user_id):
            return
        orders = await self.ctx.db.recent_orders()
        if not orders:
            await self.send(peer_id, "Открытых заказов нет 👌")
            return
        for o in orders:
            await self.send(peer_id, format_order(o, self.ctx.catalog.currency, for_owner=True),
                            kb.order_status(o.id))

    async def admin_command(self, cmd: str, arg: str, peer_id: int, reply: dict) -> None:
        ctx = self.ctx
        if cmd == "test_morning":
            await jobs.weather_morning(ctx, ctx.now(), force=True)
            await self.send(peer_id, "✅ Утренняя сводка отправлена в группу и беседу.")
        elif cmd == "test_evening":
            await jobs.weather_evening(ctx, ctx.now(), force=True)
            await self.send(peer_id, "✅ Вечерняя сводка отправлена в группу и беседу.")
        elif cmd == "test_bd":
            me = bd.Birthday(id=0, name="Тестовый Именинник", day=1, month=1)
            await self.send(peer_id, await jobs.make_greeting(ctx, [me]))
        elif cmd == "bd_list":
            items = await ctx.db.list_birthdays()
            rows = [f"{b.id} · {b.day:02d}.{b.month:02d} · {b.mention()}" for b in items]
            await self.send(peer_id, "🎂 Все дни рождения:\n\n" + "\n".join(rows) if rows else "Список пуст.")
        elif cmd == "bd_del":
            ok = arg.isdigit() and await ctx.db.delete_birthday(int(arg))
            await self.send(peer_id, "🗑 Удалено." if ok else "Формат: /bd_del ID (ID смотрите в /bd_list)")
        elif cmd == "bd_add":
            parts = arg.split(maxsplit=1)
            parsed = bd.parse_day_month(parts[0]) if parts else None
            target = reply.get("from_id") if reply and reply.get("from_id", 0) > 0 else None
            name = parts[1].strip() if len(parts) > 1 else (await self.full_name(target) if target else "")
            if not parsed or not name:
                await self.send(peer_id, "Формат: /bd_add 15.04 Иван Петров\n"
                                         "Можно ответом на сообщение человека — тогда он будет упомянут.")
                return
            bd_id = await ctx.db.add_birthday(name, parsed[0], parsed[1], PLATFORM, target)
            await self.send(peer_id, f"✅ Добавлено (ID {bd_id}): {escape(name)} — {parsed[0]:02d}.{parsed[1]:02d}")
        elif cmd == "bd_sync":
            await self.bd_sync(peer_id)

    async def bd_sync(self, peer_id: int) -> None:
        """Импорт дней рождения участников беседы из их профилей (если дата не скрыта)."""
        chat_peer = self.ctx.settings.vk_chat_peer_id or (peer_id if is_chat(peer_id) else 0)
        if not chat_peer:
            await self.send(peer_id, "Сначала укажите беседу в настройках (VK_CHAT_PEER_ID).")
            return
        try:
            res = await self.api.call("messages.getConversationMembers", peer_id=chat_peer, fields="bdate,screen_name")
        except VkApiError as exc:
            await self.send(peer_id, f"Не получилось получить участников беседы ({exc}). "
                                     "Сделайте сообщество администратором беседы.")
            return
        added = 0
        for p in res.get("profiles", []):
            parsed = bd.parse_vk_bdate(p.get("bdate"))
            if parsed and not await self.ctx.db.birthday_of(PLATFORM, p["id"]):
                name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
                await self.ctx.db.add_birthday(name, parsed[0], parsed[1], PLATFORM, p["id"], p.get("screen_name"))
                added += 1
        total = len(res.get("profiles", []))
        await self.send(peer_id, f"🎂 Готово: добавлено {added} дат из профилей ({total} участников проверено). "
                                 "У кого дата скрыта — пусть напишут сообществу «др ДД.ММ».")

    # ── нейросеть ─────────────────────────────────────────────
    async def talk(self, peer_id: int, user_id: int, text: str, chat: bool) -> None:
        ctx = self.ctx
        if not ctx.llm.enabled:
            if not chat:
                await self.send(peer_id, "Поболтать пока не могу — нейросеть не подключена. "
                                         "Но каталог и погода работают! 🙂", kb.main_menu())
            return
        if not await ctx.llm_allowed(PLATFORM, user_id, await self.is_admin(user_id)):
            await self.send(peer_id, random.choice(LIMIT_TEXTS))
            return
        name = (await self.user(user_id)).get("first_name", "")
        try:
            await self.api.call("messages.setActivity", peer_id=peer_id, type="typing")
        except Exception:  # noqa: BLE001 — индикатор не важен
            pass
        try:
            answer = await ctx.llm.chat(f"vk:{peer_id}", f"{name}: {text}",
                                        extra_context=f"Сегодня {ctx.today().strftime('%d.%m.%Y')}.")
        except LLMError as exc:
            logger.warning("ВК LLM: %s", exc)
            await self.send(peer_id, "🤖 Что-то я задумался и не смог ответить. Попробуйте ещё раз чуть позже.")
            return
        prefix = f"{self.mention(user_id, name)}, " if chat and name else ""
        await self.send(peer_id, prefix + escape(answer))

    # ── магазин ───────────────────────────────────────────────
    async def show_catalog(self, user_id: int, cmid: int | None = None) -> None:
        if not self.ctx.catalog.products:
            await self.send(user_id, "Каталог пока пуст — скоро здесь появятся товары 🙂")
            return
        await self.edit_or_send(user_id, cmid, catalog_intro(self.ctx.catalog), kb.categories(self.ctx.catalog))

    async def show_cart(self, user_id: int, cmid: int | None) -> None:
        lines = await self.ctx.orders.lines(PLATFORM, user_id)
        text = format_cart(lines, self.ctx.catalog.currency) + kb.cart_hint(lines, self.ctx.catalog.currency)
        await self.edit_or_send(user_id, cmid, text, kb.cart(lines))

    async def show_product(self, user_id: int, product_id: str, cmid: int | None) -> None:
        product = self.ctx.catalog.product(product_id)
        qty = await self.ctx.orders.qty(PLATFORM, user_id, product_id)
        await self.edit_or_send(user_id, cmid, format_product(product, self.ctx.catalog.currency, qty),
                                kb.product(self.ctx.catalog, product_id, qty))

    async def on_event(self, obj: dict) -> None:
        user_id, peer_id = obj.get("user_id"), obj.get("peer_id")
        event_id, cmid = obj.get("event_id"), obj.get("conversation_message_id")
        payload = obj.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        action, value = payload.get("a"), payload.get("v", "")
        snackbar = None
        catalog = self.ctx.catalog

        if action == "ost":
            snackbar = await self.change_status(user_id, peer_id, cmid, payload)
        elif is_chat(peer_id):
            snackbar = "Каталог и корзина — в личных сообщениях сообщества"
        elif action == "cats":
            await self.show_catalog(user_id, cmid)
        elif action == "cat":
            category = catalog.category(value)
            title = category.title if category else "Раздел"
            await self.edit_or_send(user_id, cmid, f"📋 <b>{escape(title)}</b>\n\nВыберите товар:",
                                    kb.products(catalog, value, int(payload.get("p", 0))))
        elif action == "prod":
            if catalog.product(value):
                await self.show_product(user_id, value, cmid)
            else:
                snackbar = "Товар не найден"
        elif action in {"add", "sub"}:
            qty = await self.ctx.orders.change(PLATFORM, user_id, value, 1 if action == "add" else -1)
            if qty is None:
                snackbar = "Товар не найден"
            else:
                p = catalog.product(value)
                snackbar = f"В корзине: {fmt_qty(qty)} {p.unit}" if qty else "Убрано из корзины"
                if payload.get("c"):
                    await self.show_cart(user_id, cmid)
                else:
                    await self.show_product(user_id, value, cmid)
        elif action == "cart":
            await self.show_cart(user_id, cmid)
        elif action == "clear":
            await self.ctx.orders.clear(PLATFORM, user_id)
            await self.show_cart(user_id, cmid)
        elif action == "checkout":
            if await self.ctx.orders.lines(PLATFORM, user_id):
                self.checkout[user_id] = {"step": "phone"}
                await self.send(user_id, "📞 Напишите, пожалуйста, телефон для связи — например, +7 999 123-45-67",
                                kb.phone_cancel())
            else:
                snackbar = "Корзина пуста"
        elif action == "co_ok":
            await self.finish_checkout(user_id)
        elif action == "co_cancel":
            self.checkout.pop(user_id, None)
            await self.send(user_id, "Оформление отменено. Корзина сохранена 🛒", kb.main_menu())
        try:
            await self.api.answer_event(event_id, user_id, peer_id, snackbar)
        except VkApiError as exc:
            logger.debug("ВК: ответ на нажатие: %s", exc)

    async def checkout_input(self, user_id: int, text: str, payload: dict | None) -> None:
        state = self.checkout[user_id]
        text = text.strip()
        if text.lower() in {"отмена", "отменить", "/cancel"}:
            self.checkout.pop(user_id, None)
            await self.send(user_id, "Оформление отменено. Корзина сохранена 🛒", kb.main_menu())
            return
        if state["step"] == "phone":
            if not PHONE_RE.match(text):
                await self.send(user_id, "Не похоже на номер 🤔 Напишите, например: +7 999 123-45-67", kb.phone_cancel())
                return
            state.update(step="comment", phone=text)
            await self.send(user_id, checkout_comment_hint(self.ctx.catalog), kb.comment_skip())
        elif state["step"] == "comment":
            skip = (payload or {}).get("cmd") == "no_comment" or text == "Без комментария"
            state.update(step="confirm", comment="" if skip else text[:500])
            lines = await self.ctx.orders.lines(PLATFORM, user_id)
            if not lines:
                self.checkout.pop(user_id, None)
                await self.send(user_id, "Корзина опустела 🤷 Загляните в каталог.", kb.main_menu())
                return
            await self.send(user_id, format_cart(lines, self.ctx.catalog.currency) + "\n\nВсё верно?", kb.confirm())
        else:
            await self.send(user_id, "Нажмите «✅ Подтвердить» или «❌ Отмена» под сообщением с заказом.", kb.confirm())

    async def finish_checkout(self, user_id: int) -> None:
        state = self.checkout.pop(user_id, None)
        if not state or state.get("step") != "confirm":
            return
        u = await self.user(user_id)
        order = await self.ctx.orders.place(PLATFORM, user_id, await self.full_name(user_id), u.get("screen_name"),
                                            state.get("phone", ""), state.get("comment", ""))
        if not order:
            await self.send(user_id, "Корзина пуста 🤷", kb.main_menu())
            return
        await self.send(user_id, f"🎉 Заказ №{order.id} оформлен! Мы скоро свяжемся с вами.\n\n"
                        + format_order(order, self.ctx.catalog.currency), kb.main_menu())
        await self.ctx.messenger.notify_new_order(order)

    async def change_status(self, user_id: int, peer_id: int, cmid: int | None, payload: dict) -> str:
        if not await self.is_manager(user_id):
            return "Только для менеджеров"
        status = payload.get("s")
        if status not in STATUSES:
            return "Неизвестный статус"
        order = await self.ctx.orders.set_status(int(payload.get("o", 0)), status)
        if not order:
            return "Заказ не найден"
        await self.edit_or_send(peer_id, cmid, format_order(order, self.ctx.catalog.currency, for_owner=True),
                                kb.order_status(order.id))
        await self.ctx.messenger.notify_customer(order)
        return f"Статус: {STATUSES[status]}"

    # ── заказ через раздел «Товары» ВКонтакте ─────────────────
    async def on_market_order(self, obj: dict) -> None:
        order = obj.get("order", obj)
        user_id = order.get("user_id", 0)
        number = order.get("display_order_id") or order.get("id")
        total = (order.get("total_price") or {}).get("text", "")
        rows = [f"🛒 <b>Новый заказ через «Товары» ВКонтакте</b> №{escape(str(number))}", ""]
        if user_id:
            rows.append(f"👤 {self.mention(user_id, await self.full_name(user_id))}")
        recipient = order.get("recipient") or {}
        if recipient.get("phone"):
            rows.append(f"📞 {escape(recipient['phone'])}")
        if total:
            rows.append(f"💰 {escape(total)}")
        if order.get("comment"):
            rows.append(f"💬 {escape(order['comment'])}")
        rows += ["", "Подробности — в сообществе: Управление → Товары → Заказы."]
        await self.ctx.messenger.notify_market_order("\n".join(rows))
