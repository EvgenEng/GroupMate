"""Сценарии ВК-бота на фейковом API: без сети и без настоящего ВКонтакте."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app import jobs
from app.config import Settings, _handles
from app.context import build_context
from app.core.orders import Catalog
from app.vk import keyboards as kb
from app.vk.bot import VkBot, resolve_command
from app.vk.format import html_to_vk
from tests.test_core import FakeWeather, ROOT, make_payload

GROUP = 239343586
CHAT = 2_000_000_001
CUSTOMER, MANAGER, ADMIN, STRANGER = 100, 555, 777, 300

USERS = {
    CUSTOMER: {"id": CUSTOMER, "first_name": "Ольга", "last_name": "Смирнова", "bdate": "15.4", "screen_name": "olga"},
    MANAGER: {"id": MANAGER, "first_name": "Анна", "last_name": "Дин", "screen_name": "artdin93"},
    ADMIN: {"id": ADMIN, "first_name": "Евгений", "last_name": "Ч."},
    STRANGER: {"id": STRANGER, "first_name": "Пётр", "last_name": "П.", "bdate": "1.1.1990"},
}


class FakeApi:
    def __init__(self):
        self.sent: list[tuple[int, str, dict | None]] = []
        self.edits: list[tuple[int, int, str, dict | None]] = []
        self.snackbars: list[str | None] = []
        self.calls: list[str] = []

    async def call(self, method, **params):
        self.calls.append(method)
        if method == "users.get":
            uid = int(params["user_ids"])
            return [USERS.get(uid, {"id": uid, "first_name": "Гость"})]
        if method == "utils.resolveScreenName":
            found = [u["id"] for u in USERS.values() if u.get("screen_name") == params["screen_name"]]
            return {"type": "user", "object_id": found[0]} if found else []
        if method == "groups.getById":
            return {"groups": [{"id": GROUP, "name": "GASTRODOM", "screen_name": "gastrodomru"}]}
        if method == "messages.getConversationMembers":
            return {"items": [], "profiles": [USERS[STRANGER], USERS[MANAGER]]}
        return 1

    async def send(self, peer_id, text, keyboard=None, **extra):
        self.sent.append((peer_id, text, keyboard))

    async def edit(self, peer_id, cmid, text, keyboard=None):
        self.edits.append((peer_id, cmid, text, keyboard))

    async def answer_event(self, event_id, user_id, peer_id, text=None):
        self.snackbars.append(text)

    def texts_to(self, peer_id) -> list[str]:
        return [t for p, t, _ in self.sent if p == peer_id]


def message(peer_id, from_id, text="", payload=None, **extra):
    msg = {"peer_id": peer_id, "from_id": from_id, "text": text, "conversation_message_id": 1}
    if payload is not None:
        msg["payload"] = json.dumps(payload)
    msg.update(extra)
    return {"type": "message_new", "object": {"message": msg}}


def event(user_id, payload, peer_id=None, cmid=10):
    return {"type": "message_event", "object": {"user_id": user_id, "peer_id": peer_id or user_id,
                                                "event_id": "e", "payload": payload,
                                                "conversation_message_id": cmid}}


class VkTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        settings = Settings(
            bot_token="", owner_id=0, group_chat_id=0, telegram_proxy="", timezone="Europe/Moscow",
            db_path=Path(self.tmp.name) / "bot.db", catalog_path=ROOT / "config" / "catalog.json",
            holidays_path=ROOT / "config" / "holidays.json",
            vk_token="token", vk_group_id=GROUP, vk_chat_peer_id=CHAT,
            order_notify_vk=("artdin93",), vk_admins=(str(ADMIN),),
        )
        self.ctx = build_context(settings)
        today = self.ctx.today()
        self.ctx.weather = FakeWeather(make_payload(today, [2 + h * 0.3 for h in range(24)], [2] * 24, [10] * 24))
        self.api = FakeApi()
        self.ctx.messenger.vk = self.api
        self.bot = VkBot(self.ctx, self.api)
        asyncio.run(self.bot.start())

    def tearDown(self):
        self.ctx.db.close()
        self.tmp.cleanup()

    def run_all(self, *updates):
        async def go():
            for u in updates:
                await self.bot.handle(u)
            await asyncio.gather(*self.bot._tasks)
        asyncio.run(go())


class VkScenarioTests(VkTestCase):
    def test_start_button_greets_once_with_menu_and_saves_profile_birthday(self):
        self.run_all({"type": "message_allow", "object": {"user_id": CUSTOMER}},
                     message(CUSTOMER, CUSTOMER, "Начать", {"command": "start"}))
        texts = self.api.texts_to(CUSTOMER)
        greetings = [t for t in texts if t.startswith("Здравствуйте")]
        self.assertEqual(len(greetings), 1)  # «разрешил сообщения» + «Начать» → одно приветствие
        self.assertIn("GASTRODOM", greetings[0])
        menu = [k for p, t, k in self.api.sent if t.startswith("Здравствуйте")][0]
        self.assertFalse(menu.get("inline", False))
        self.assertTrue(any("15 апреля" in t for t in texts))
        bd = asyncio.run(self.ctx.db.birthday_of("vk", CUSTOMER))
        self.assertEqual((bd.day, bd.month, bd.name), (15, 4, "Ольга Смирнова"))

    def test_full_order_flow_and_manager_status(self):
        self.run_all(
            message(CUSTOMER, CUSTOMER, "каталог"),
            event(CUSTOMER, {"a": "cat", "v": "deli", "p": 1}),
            event(CUSTOMER, {"a": "prod", "v": "neck"}),
            event(CUSTOMER, {"a": "add", "v": "neck"}),
            event(CUSTOMER, {"a": "add", "v": "neck"}),
            event(CUSTOMER, {"a": "add", "v": "neck"}),
            event(CUSTOMER, {"a": "prod", "v": "set-intro"}),
            event(CUSTOMER, {"a": "add", "v": "set-intro"}),
            event(CUSTOMER, {"a": "cart"}),
            event(CUSTOMER, {"a": "checkout"}),
            message(CUSTOMER, CUSTOMER, "не скажу"),
            message(CUSTOMER, CUSTOMER, "+7 999 123-45-67"),
            message(CUSTOMER, CUSTOMER, "Без комментария", {"cmd": "no_comment"}),
            event(CUSTOMER, {"a": "co_ok"}),
        )
        self.assertIn("В корзине: 1,5 кг", self.api.snackbars)
        self.assertIn("В корзине: 1 шт", self.api.snackbars)
        cart_edit = [t for _, _, t, _ in self.api.edits if t.startswith("🛒 Ваша корзина")][-1]
        self.assertIn("Шейка классическая в/к — 1,5 кг", cart_edit)
        self.assertIn("≈ 5 040 ₽", cart_edit)
        customer = self.api.texts_to(CUSTOMER)
        self.assertTrue(any(t.startswith("Не похоже на номер") for t in customer))
        self.assertTrue(any("Заказ №1 оформлен" in t for t in customer))
        # Менеджер (vk.ru/artdin93) получил уведомление с кнопками статусов
        note = [(t, k) for p, t, k in self.api.sent if p == MANAGER]
        self.assertEqual(len(note), 1)
        self.assertIn("Новый заказ", note[0][0])
        self.assertIn("[id100|Ольга Смирнова]", note[0][0])
        self.assertIn("+7 999 123-45-67", note[0][0])
        self.assertIn("ost", json.dumps(note[0][1]))
        order = asyncio.run(self.ctx.db.order(1))
        self.assertEqual((order.platform, order.total), ("vk", 1700 * 1.5 + 2490))
        self.assertEqual(asyncio.run(self.ctx.db.cart("vk", CUSTOMER)), {})

        # Чужой не может менять статус, менеджер — может; покупателю приходит сообщение
        self.run_all(event(STRANGER, {"a": "ost", "o": 1, "s": "accepted"}),
                     event(MANAGER, {"a": "ost", "o": 1, "s": "accepted"}))
        self.assertEqual(self.api.snackbars[-2], "Только для менеджеров")
        self.assertEqual(asyncio.run(self.ctx.db.order(1)).status, "accepted")
        self.assertTrue(any("принят в работу" in t for t in self.api.texts_to(CUSTOMER)))

    def test_menu_button_interrupts_checkout(self):
        self.run_all(event(CUSTOMER, {"a": "add", "v": "neck"}),
                     event(CUSTOMER, {"a": "checkout"}),
                     message(CUSTOMER, CUSTOMER, "🌤 Погода", {"cmd": "weather"}))
        self.assertNotIn(CUSTOMER, self.bot.checkout)
        self.assertTrue(any("Погода на сегодня" in t for t in self.api.texts_to(CUSTOMER)))
        self.assertEqual(asyncio.run(self.ctx.db.cart("vk", CUSTOMER)), {"neck": 0.5})

    def test_chat_welcome_and_commands(self):
        self.run_all(
            message(CHAT, CUSTOMER, "", action={"type": "chat_invite_user", "member_id": STRANGER}),
            message(CHAT + 5, CUSTOMER, "", action={"type": "chat_invite_user", "member_id": STRANGER}),
            message(CHAT, CUSTOMER, "просто болтаем"),
            message(CHAT, CUSTOMER, "[club239343586|@gastrodomru] погода"),
            message(CHAT, CUSTOMER, "каталог"),
        )
        chat = self.api.texts_to(CHAT)
        self.assertEqual(len(self.api.texts_to(CHAT + 5)), 0)   # чужая беседа — не здороваемся
        self.assertIn("[id300|Пётр]", chat[0])
        self.assertTrue(any("Погода на сегодня" in t for t in chat))
        self.assertTrue(any("в личных сообщениях сообщества" in t for t in chat))
        self.assertEqual(len(chat), 3)  # «просто болтаем» без обращения — молчим

    def test_llm_reply_in_chat_mentions_author(self):
        class FakeLLM:
            enabled = True

            async def chat(self, key, text, extra_context=""):
                return "Шейку берите 😉"

        self.ctx.llm = FakeLLM()
        self.run_all(message(CHAT, CUSTOMER, "[club239343586|@gastrodomru] что взять к столу?"))
        self.assertEqual(self.api.texts_to(CHAT)[-1], "[id100|Ольга], Шейку берите 😉")

    def test_birthday_job_posts_to_vk_chat_with_mention(self):
        asyncio.run(self.ctx.db.add_birthday("Ольга Смирнова", 15, 4, "vk", CUSTOMER))
        asyncio.run(jobs.birthdays(self.ctx, datetime(2027, 4, 15, 10, 0)))
        chat = self.api.texts_to(CHAT)
        self.assertEqual(len(chat), 1)
        self.assertIn("[id100|Ольга Смирнова]", chat[0])
        self.assertNotIn("<a", chat[0])

    def test_admin_birthday_sync_and_rights(self):
        self.run_all(message(ADMIN, ADMIN, "/bd_sync"))
        self.assertTrue(any("добавлено 1" in t for t in self.api.texts_to(ADMIN)))  # у менеджера дата скрыта
        self.assertEqual(asyncio.run(self.ctx.db.birthday_of("vk", STRANGER)).day, 1)
        self.run_all(message(STRANGER, STRANGER, "/bd_sync"))  # не админ — тишина
        self.assertFalse(any("Готово" in t for t in self.api.texts_to(STRANGER)))

    def test_my_birthday_commands(self):
        self.run_all(message(MANAGER, MANAGER, "др 3.4"), message(MANAGER, MANAGER, "др удалить"))
        texts = self.api.texts_to(MANAGER)
        self.assertTrue(any("3 апреля" in t for t in texts))
        self.assertTrue(any("Удалил" in t for t in texts))

    def test_market_order_notifies_managers(self):
        self.run_all({"type": "market_order_new", "object": {
            "id": 5, "display_order_id": "239343586-5", "user_id": CUSTOMER,
            "total_price": {"text": "3 490 ₽"}, "recipient": {"phone": "+79990000000"}}})
        note = self.api.texts_to(MANAGER)[-1]
        self.assertIn("через «Товары»", note)
        self.assertIn("3 490 ₽", note)


class VkUnitTests(unittest.TestCase):
    def test_resolve_command(self):
        cases = {
            ("Начать", '{"command":"start"}'): ("start", ""),
            ("погода", None): ("weather", ""),
            ("Погода завтра!", None): ("tomorrow", ""),
            ("/mybd 15.04", None): ("mybd", "15.04"),
            ("др 15.04", None): ("mybd", "15.04"),
            ("др у Маши когда?", None): (None, ""),
            ("/bd_add 1.2 Иван", None): ("bd_add", "1.2 Иван"),
            ("/test_morning", None): ("test_morning", ""),
            ("test_morning", None): (None, ""),
            ("как дела?", None): (None, ""),
        }
        for (text, payload), expected in cases.items():
            self.assertEqual(resolve_command(text, json.loads(payload) if payload else None), expected, text)

    def test_html_to_vk(self):
        self.assertEqual(html_to_vk('<b>Итого</b>: 5 &lt; 6 &amp; <i>да</i>'), "Итого: 5 < 6 & да")
        self.assertEqual(html_to_vk('<a href="https://vk.ru/id5">Ан|на</a>'), "[id5|Ан/на]")
        self.assertEqual(html_to_vk('<a href="tg://user?id=5">Аня</a> (@a)'), "Аня (@a)")
        self.assertEqual(html_to_vk('<a href="https://t.me/x">канал</a>'), "канал (https://t.me/x)")

    def test_keyboards_fit_vk_limits(self):
        catalog = Catalog.load(ROOT / "config" / "catalog.json")
        boards = [kb.categories(catalog), kb.confirm(), kb.comment_skip(), kb.order_status(1)]
        for c in catalog.categories:
            pages = (len(catalog.in_category(c.id)) + kb.PAGE_SIZE - 1) // kb.PAGE_SIZE
            boards += [kb.products(catalog, c.id, p) for p in range(pages)]
        for p in catalog.products:
            boards += [kb.product(catalog, p.id, 0), kb.product(catalog, p.id, 1.5)]
        from app.core.orders import build_lines
        boards.append(kb.cart(build_lines({p.id: 1 for p in catalog.products}, catalog)))
        for board in boards:
            rows, buttons = kb.count_buttons(board)
            self.assertLessEqual(rows, kb.INLINE_MAX_ROWS)
            self.assertLessEqual(buttons, kb.INLINE_MAX_BUTTONS)
            for row in board["buttons"]:
                for b in row:
                    self.assertLessEqual(len(b["action"]["label"]), kb.LABEL_MAX)
                    self.assertLessEqual(len(b["action"].get("payload", "")), 255)
        # главное меню — обычная клавиатура, лимиты у неё больше
        self.assertFalse(kb.main_menu().get("inline", False))

    def test_manager_handles_from_links(self):
        import os
        os.environ["GM_TEST_MANAGERS"] = "https://t.me/artdin93, @Other, 12345, vk.ru/id77/"
        self.assertEqual(_handles("GM_TEST_MANAGERS"), ("artdin93", "Other", "12345", "id77"))


if __name__ == "__main__":
    unittest.main()
