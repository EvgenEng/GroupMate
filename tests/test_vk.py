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
        self.uploads = 0
        self.links: list[str | None] = []
        self.allowed = {CUSTOMER, MANAGER, ADMIN}
        self.attachments: list[tuple[int, str]] = []

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
        if method == "messages.isMessagesFromGroupAllowed":
            return {"is_allowed": int(params["user_id"] in self.allowed)}
        if method == "messages.getConversationMembers":
            return {"items": [], "profiles": [USERS[STRANGER], USERS[MANAGER]]}
        return 1

    async def send(self, peer_id, text, keyboard=None, **extra):
        self.sent.append((peer_id, text, keyboard))
        if extra.get("attachment"):
            self.attachments.append((peer_id, extra["attachment"]))

    async def upload_message_photo(self, peer_id, path):
        self.uploads += 1
        return "photo-239343586_1_key"

    async def edit(self, peer_id, cmid, text, keyboard=None):
        self.edits.append((peer_id, cmid, text, keyboard))

    async def answer_event(self, event_id, user_id, peer_id, text=None, link=None):
        self.snackbars.append(text)
        self.links.append(link)

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
            holidays_path=ROOT / "config" / "holidays.json", catalog_image=ROOT / "config" / "catalog.jpg",
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
        greetings = [t for t in texts if t.startswith("🐻 Здравствуйте")]
        self.assertEqual(len(greetings), 1)  # «разрешил сообщения» + «Начать» → одно приветствие
        self.assertIn("GASTRODOM", greetings[0])
        menu = [k for p, t, k in self.api.sent if t.startswith("🐻 Здравствуйте")][0]
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
            message(CUSTOMER, CUSTOMER, "Оля"),                                   # одно слово — переспросим
            message(CUSTOMER, CUSTOMER, "✅ Смирнова Ольга", {"cmd": "use_name"}),  # ФИО из профиля кнопкой
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
        self.assertTrue(any(t.startswith("Напишите, пожалуйста, фамилию и имя") for t in customer))
        self.assertTrue(any(t.startswith("Не похоже на номер") for t in customer))
        self.assertTrue(any("Заказ №1 оформлен" in t for t in customer))
        self.assertFalse(any("дату вашего дня рождения" in t for t in customer))  # дата уже есть из профиля
        # Менеджер (vk.ru/artdin93): позиции — количество — ФИО — телефон, кнопки статусов
        note = [(t, k) for p, t, k in self.api.sent if p == MANAGER]
        self.assertEqual(len(note), 1)
        text = note[0][0]
        self.assertIn("• Шейка классическая в/к — 1,5 кг", text)
        self.assertIn("• Набор «Знакомство с GASTRODOM» — 1 шт", text)
        self.assertIn("👤 [id100|Смирнова Ольга]", text)
        self.assertIn("📞 +7 999 123-45-67", text)
        self.assertIn("🎂 День рождения: 15 апреля", text)
        self.assertIn("ost", json.dumps(note[0][1]))
        order = asyncio.run(self.ctx.db.order(1))
        self.assertEqual((order.platform, order.total, order.customer_name), ("vk", 1700 * 1.5 + 2490, "Смирнова Ольга"))
        self.assertEqual(asyncio.run(self.ctx.db.cart("vk", CUSTOMER)), {})

        # Чужой не может менять статус, менеджер — может; покупателю приходит сообщение
        self.run_all(event(STRANGER, {"a": "ost", "o": 1, "s": "accepted"}),
                     event(MANAGER, {"a": "ost", "o": 1, "s": "accepted"}))
        self.assertEqual(self.api.snackbars[-2], "Только для менеджеров")
        self.assertEqual(asyncio.run(self.ctx.db.order(1)).status, "accepted")
        self.assertTrue(any("принят в работу" in t for t in self.api.texts_to(CUSTOMER)))

    def test_repeat_order_offers_saved_contacts_and_asks_birthday(self):
        buyer = 400  # профиль без фамилии и без даты рождения
        flow = [event(buyer, {"a": "add", "v": "set-week"}), event(buyer, {"a": "checkout"}),
                message(buyer, buyer, "Петров Иван"), message(buyer, buyer, "89991112233"),
                message(buyer, buyer, "к субботе"), event(buyer, {"a": "co_ok"})]
        self.run_all(*flow)
        texts = self.api.texts_to(buyer)
        self.assertTrue(any("дату вашего дня рождения" in t for t in texts))
        self.run_all(message(buyer, buyer, "3.04"))
        self.assertEqual(asyncio.run(self.ctx.db.birthday_of("vk", buyer)).name, "Петров Иван")
        # повторный заказ: кнопки с сохранёнными ФИО и телефоном
        self.api.sent.clear()
        self.run_all(event(buyer, {"a": "add", "v": "set-week"}), event(buyer, {"a": "checkout"}),
                     message(buyer, buyer, "✅ Петров Иван", {"cmd": "use_name"}))
        boards = json.dumps([k for p, _, k in self.api.sent if p == buyer], ensure_ascii=False)
        self.assertIn("✅ Петров Иван", boards)
        self.assertIn("📞 89991112233", boards)
        self.run_all(message(buyer, buyer, "📞 89991112233", {"cmd": "use_phone"}),
                     message(buyer, buyer, "Без комментария", {"cmd": "no_comment"}), event(buyer, {"a": "co_ok"}))
        self.assertEqual(asyncio.run(self.ctx.db.order(2)).phone, "89991112233")
        self.assertFalse(any("дату вашего дня рождения" in t for t in self.api.texts_to(buyer)))

    def test_chat_catalog_button_sends_to_dm_or_opens_dialog(self):
        self.run_all(event(CUSTOMER, {"a": "dm_catalog"}, peer_id=CHAT),
                     event(STRANGER, {"a": "dm_catalog"}, peer_id=CHAT))
        self.assertTrue(any(t.startswith("🛍 GASTRODOM") for t in self.api.texts_to(CUSTOMER)))
        self.assertEqual(self.api.snackbars[0], "Отправил каталог вам в личные сообщения 📩")
        self.assertEqual(self.api.texts_to(STRANGER), [])
        self.assertEqual(self.api.links[1], "https://vk.me/gastrodomru")

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
        self.assertTrue(any("каталог вам в личные сообщения" in t for t in chat))
        # приветствие, погода, прайс + кнопка; «просто болтаем» без обращения — молчим
        self.assertEqual(len(chat), 4)

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


class VkCatalogImageTests(VkTestCase):
    def test_catalog_shows_price_image_uploaded_once(self):
        self.run_all(message(CUSTOMER, CUSTOMER, "🛍 Каталог", {"cmd": "catalog"}),
                     event(CUSTOMER, {"a": "cats"}),                      # «⬅️ Разделы» — без картинки
                     message(MANAGER, MANAGER, "каталог"),
                     message(CHAT, CUSTOMER, "/каталог"),
                     message(CHAT, CUSTOMER, "[club239343586|@gastrodomru] каталог"))
        self.assertEqual(self.api.uploads, 1)                              # загрузили один раз, дальше кеш
        self.assertEqual([p for p, _ in self.api.attachments], [CUSTOMER, MANAGER, CHAT, CHAT])
        # после картинки — кнопки разделов
        texts = self.api.texts_to(CUSTOMER)
        self.assertEqual(texts[texts.index("📋 Наш прайс") + 1].splitlines()[0], "🛍 GASTRODOM — каталог")

    def test_catalog_without_image_still_works(self):
        self.ctx.settings.__dict__["catalog_image"] = None
        self.run_all(message(CUSTOMER, CUSTOMER, "каталог"))
        self.assertEqual(self.api.uploads, 0)
        self.assertTrue(any(t.startswith("🛍 GASTRODOM") for t in self.api.texts_to(CUSTOMER)))


class VkUnitTests(unittest.TestCase):
    def test_resolve_command(self):
        cases = {
            ("Начать", '{"command":"start"}'): ("start", ""),
            ("погода", None): ("weather", ""),
            ("Погода завтра!", None): (None, ""),     # «погоду на завтра» убрали
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


class VkHandoffTests(VkTestCase):
    """Нейросеть отвечает по справке, а когда не может — передаёт вопрос менеджеру."""

    def fake_llm(self, reply):
        class FakeLLM:
            enabled = True

            async def chat(self, key, text, extra_context=""):
                return reply

        self.ctx.llm = FakeLLM()

    def test_marker_is_hidden_and_offers_handoff_button(self):
        self.fake_llm("По поводу доставки вам скажет менеджер. [[МЕНЕДЖЕР]]")
        self.run_all(message(CUSTOMER, CUSTOMER, "Расскажите про вашу компанию"))
        peer, text, keyboard = self.api.sent[-1]
        self.assertNotIn("МЕНЕДЖЕР]]", text)
        self.assertIn("По поводу доставки вам скажет менеджер.", text)
        self.assertEqual(json.loads(keyboard["buttons"][0][0]["action"]["payload"]), {"a": "hand"})

    def test_plain_answer_has_no_handoff_button(self):
        self.fake_llm("Шейка стоит 2400 ₽/кг.")
        self.run_all(message(CUSTOMER, CUSTOMER, "Посоветуйте что-нибудь на ужин"))
        peer, text, keyboard = self.api.sent[-1]
        self.assertEqual(text, "Шейка стоит 2400 ₽/кг.")
        self.assertIsNone(keyboard)

    def test_handoff_with_phone_reaches_manager(self):
        self.fake_llm("Уточнит менеджер. [[МЕНЕДЖЕР]]")
        self.run_all(message(CUSTOMER, CUSTOMER, "А самовывоз есть?"))
        self.run_all(event(CUSTOMER, {"a": "hand"}))
        self.run_all(message(CUSTOMER, CUSTOMER, "+7 999 123-45-67"))
        manager_msgs = self.api.texts_to(MANAGER)
        self.assertEqual(len(manager_msgs), 1)
        self.assertIn("Нужна связь менеджера с покупателем", manager_msgs[0])
        self.assertIn("А самовывоз есть?", manager_msgs[0])
        self.assertIn("+7 999 123-45-67", manager_msgs[0])
        self.assertIn("Ольга Смирнова", manager_msgs[0])
        self.assertTrue(any("Передал ваш вопрос менеджеру" in t for t in self.api.texts_to(CUSTOMER)))

    def test_handoff_without_phone_and_bad_phone(self):
        self.fake_llm("Уточнит менеджер. [[МЕНЕДЖЕР]]")
        self.run_all(message(CUSTOMER, CUSTOMER, "Когда будет готово?"))
        self.run_all(event(CUSTOMER, {"a": "hand"}))
        self.run_all(message(CUSTOMER, CUSTOMER, "не скажу"))
        self.assertEqual(self.api.texts_to(MANAGER), [])
        self.assertTrue(any("Не похоже на номер" in t for t in self.api.texts_to(CUSTOMER)))
        self.run_all(message(CUSTOMER, CUSTOMER, "", {"cmd": "no_phone"}))
        got = self.api.texts_to(MANAGER)
        self.assertEqual(len(got), 1)
        self.assertIn("телефон не указан", got[0])

    def test_handoff_cancel(self):
        self.fake_llm("Уточнит менеджер. [[МЕНЕДЖЕР]]")
        self.run_all(message(CUSTOMER, CUSTOMER, "А скидки есть?"))
        self.run_all(event(CUSTOMER, {"a": "hand"}))
        self.run_all(event(CUSTOMER, {"a": "h_cancel"}))
        self.assertEqual(self.api.texts_to(MANAGER), [])
        self.assertNotIn(CUSTOMER, self.bot.checkout)

    def test_manager_button_asks_question_then_phone(self):
        self.run_all(message(CUSTOMER, CUSTOMER, "", {"cmd": "manager"}),
                     message(CUSTOMER, CUSTOMER, "Хочу заказать банкет на 30 человек"),
                     message(CUSTOMER, CUSTOMER, "89991234567"))
        got = self.api.texts_to(MANAGER)
        self.assertEqual(len(got), 1)
        self.assertIn("банкет на 30 человек", got[0])

    def test_llm_disabled_offers_handoff_in_dm_and_silent_in_chat(self):
        self.run_all(message(CUSTOMER, CUSTOMER, "Расскажите про вашу компанию"))
        self.assertEqual(self.api.sent[-1][2]["buttons"][0][0]["action"]["label"], "📞 Передать вопрос менеджеру")
        before = len(self.api.sent)
        self.run_all(message(CHAT, CUSTOMER, "[club239343586|@gastrodomru] привет"))
        self.assertEqual(len(self.api.sent), before)

    def test_handoff_daily_cap(self):
        from app.vk.bot import HANDOFF_DAILY_CAP
        self.fake_llm("Уточнит менеджер. [[МЕНЕДЖЕР]]")
        for i in range(HANDOFF_DAILY_CAP + 1):
            self.run_all(message(CUSTOMER, CUSTOMER, f"вопрос {i}"))
            self.run_all(event(CUSTOMER, {"a": "hand"}))
            self.run_all(message(CUSTOMER, CUSTOMER, "+7 999 123-45-67"))
        self.assertEqual(len(self.api.texts_to(MANAGER)), HANDOFF_DAILY_CAP)


class KnowledgeTests(unittest.TestCase):
    def test_split_handoff(self):
        from app.core.knowledge import split_handoff
        self.assertEqual(split_handoff("Ок [[МЕНЕДЖЕР]]"), ("Ок", True))
        self.assertEqual(split_handoff("Просто ответ"), ("Просто ответ", False))
        self.assertEqual(split_handoff("[[МЕНЕДЖЕР]]"), ("", True))

    def test_knowledge_has_prices_faq_and_rules(self):
        from app.core.knowledge import build_knowledge, load_faq, HANDOFF_MARK
        catalog = Catalog.load(ROOT / "config" / "catalog.json")
        text = build_knowledge(catalog, load_faq(ROOT / "config" / "faq.txt"))
        self.assertIn(HANDOFF_MARK, text)
        self.assertIn("По поводу доставки и самовывоза вам скажет менеджер", text)
        self.assertIn("Минимальной суммы нет", text)
        self.assertIn("Курица (полутушка) в/к: 800 ₽/кг", text)
        for p in catalog.products:
            self.assertIn(p.name, text)

    def test_llm_gets_knowledge_only_in_chat(self):
        from app.services.llm import LLMClient
        llm = LLMClient([], "Персона")
        llm.knowledge = "СПРАВКА"
        self.assertIn("СПРАВКА", llm._system(knowledge=True)[0]["content"])
        self.assertNotIn("СПРАВКА", llm._system()[0]["content"])


class VkRuleAnswersTests(VkTestCase):
    """Ответы без нейросети: цены из каталога, оплата, сроки, доставка → менеджер."""

    def last(self):
        return self.api.sent[-1]

    def test_price_from_catalog(self):
        self.run_all(message(CUSTOMER, CUSTOMER, "Сколько стоит шейка?"))
        peer, text, keyboard = self.last()
        self.assertIn("Шейка классическая в/к — 1700 ₽/кг", text)
        self.assertIn("точная сумма после взвешивания", text)
        self.assertIsNotNone(keyboard)  # меню возвращается под ответом

    def test_delivery_goes_to_manager_with_button(self):
        self.run_all(message(CUSTOMER, CUSTOMER, "А вы доставляете?"))
        peer, text, keyboard = self.last()
        self.assertIn("По поводу доставки и самовывоза вам скажет менеджер", text)
        self.assertEqual(json.loads(keyboard["buttons"][0][0]["action"]["payload"]), {"a": "hand"})

    def test_payment_and_min_order(self):
        self.run_all(message(CUSTOMER, CUSTOMER, "Как оплатить?"))
        self.assertIn("любым удобным способом", self.last()[1])
        self.run_all(message(CUSTOMER, CUSTOMER, "Есть минимальный заказ?"))
        self.assertIn("Минимальной суммы заказа нет", self.last()[1])

    def test_lead_time_mentions_factors_and_manager(self):
        self.run_all(message(CUSTOMER, CUSTOMER, "Когда будет готово?"))
        text = self.last()[1]
        self.assertIn("точный срок вам сообщит менеджер", text)
        self.assertIn("менеджер", text)

    def test_price_and_delivery_in_one_message(self):
        self.run_all(message(CUSTOMER, CUSTOMER, "Сколько стоят наборы и есть ли доставка?"))
        text = self.last()[1]
        self.assertIn("Набор «Мясной стол» — 3490 ₽/шт", text)
        self.assertIn("вам скажет менеджер", text)

    def test_how_to_order_shows_catalog(self):
        self.run_all(message(CUSTOMER, CUSTOMER, "Хочу заказать"))
        self.assertTrue(any("Заказать просто" in t for t in self.api.texts_to(CUSTOMER)))
        self.assertTrue(any("Выберите раздел" in t or "каталог" in t.lower() for t in self.api.texts_to(CUSTOMER)[1:]))

    def test_greeting_shows_menu(self):
        self.run_all(message(CUSTOMER, CUSTOMER, "Здравствуйте!"))
        text = self.last()[1]
        self.assertIn("Здравствуйте", text)
        self.assertIn("напишите «меню»", text)

    def test_ok_is_ignored_and_unknown_offers_manager(self):
        self.run_all(message(CUSTOMER, CUSTOMER, "ок"))
        before = len(self.api.sent)
        self.run_all(message(CUSTOMER, CUSTOMER, "абракадабра"))
        self.assertEqual(len(self.api.sent), before + 1)
        self.assertIn("передать ваш вопрос менеджеру", self.last()[1])

    def test_long_chat_about_table_is_not_a_price_question(self):
        self.run_all(message(CUSTOMER, CUSTOMER, "Что бы взять к праздничному столу?"))
        self.assertNotIn("Мясной стол", self.last()[1])

    def test_rules_work_in_group_chat_only_when_addressed(self):
        self.run_all(message(CHAT, CUSTOMER, "сколько стоит шейка"))
        self.assertEqual(self.api.texts_to(CHAT), [])
        self.run_all(message(CHAT, CUSTOMER, "[club239343586|@gastrodomru] сколько стоит шейка"))
        self.assertIn("Шейка классическая", self.api.texts_to(CHAT)[-1])


class FaqBotTests(unittest.TestCase):
    def setUp(self):
        from app.core.faq_bot import FaqBot
        self.catalog = Catalog.load(ROOT / "config" / "catalog.json")
        self.faq = FaqBot.load(self.catalog, ROOT / "config" / "answers.json")

    def test_products_by_word_forms(self):
        names = lambda t: [p.id for p in self.faq.find_products(t)]
        self.assertEqual(names("шейку"), ["neck"])
        self.assertEqual(names("грудинка"), ["brisket", "bacon"])
        self.assertEqual(len(names("ветчина")), 3)
        self.assertEqual(len(names("курица")), 7)
        self.assertEqual(names("бекон"), ["bacon"])

    def test_every_product_is_found_by_its_own_name(self):
        for p in self.catalog.products:
            found = [x.id for x in self.faq.find_products(p.name)]
            self.assertIn(p.id, found, p.name)

    def test_unknown_and_silent(self):
        self.assertIsNone(self.faq.answer("абракадабра"))
        self.assertTrue(self.faq.answer("Ок!").silent)

    def test_bad_answers_file_does_not_crash(self):
        from app.core.faq_bot import FaqBot
        bot = FaqBot.load(self.catalog, ROOT / "config" / "catalog.jpg")  # не JSON
        self.assertIsNotNone(bot.answer("сколько стоит шейка"))
        self.assertIsNone(bot.answer("а вы доставляете?"))


class VkBirthdayOrderTests(VkTestCase):
    """Заказ в день рождения: менеджеру — пометка про подарок, покупателю — поздравление."""

    def order(self):
        self.run_all(
            event(CUSTOMER, {"a": "add", "v": "neck"}),
            event(CUSTOMER, {"a": "checkout"}),
            message(CUSTOMER, CUSTOMER, "✅ Смирнова Ольга", {"cmd": "use_name"}),
            message(CUSTOMER, CUSTOMER, "+7 999 123-45-67"),
            message(CUSTOMER, CUSTOMER, "Без комментария", {"cmd": "no_comment"}),
            event(CUSTOMER, {"a": "co_ok"}),
        )

    def test_birthday_today_adds_gift_note_for_manager_and_greeting(self):
        now = datetime.now()
        asyncio.run(self.ctx.db.add_birthday("Ольга Смирнова", now.day, now.month, "vk", CUSTOMER))
        self.order()
        manager = self.api.texts_to(MANAGER)
        self.assertTrue(any("Именинник" in t and "подарок" in t for t in manager))
        self.assertTrue(any("С днём рождения" in t and "подарок" in t for t in self.api.texts_to(CUSTOMER)))

    def test_other_day_has_no_gift_note(self):
        other = datetime.now().replace(day=1)
        month = other.month % 12 + 1
        asyncio.run(self.ctx.db.add_birthday("Ольга Смирнова", 1, month, "vk", CUSTOMER))
        self.order()
        self.assertFalse(any("Именинник" in t for t in self.api.texts_to(MANAGER)))
        self.assertFalse(any("С днём рождения" in t for t in self.api.texts_to(CUSTOMER)))
