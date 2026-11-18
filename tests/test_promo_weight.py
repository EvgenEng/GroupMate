"""Промокоды (один раз на человека, уведомление менеджеру) и вес с шагом 100 г."""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.core import orders as od
from app.core.promo import PromoBook, PromoCode, normalize_code
from app.services.db import Database
from app.vk import keyboards as kb
from app.vk.bot import resolve_command
from tests.test_core import ROOT
from tests.test_vk import CHAT, CUSTOMER, MANAGER, STRANGER, VkTestCase, event, message

PROMO_BTN = {"cmd": "promo"}


class WeightParseTests(unittest.TestCase):
    def setUp(self):
        self.neck = od.Catalog.load(ROOT / "config" / "catalog.json").product("neck")

    def qty(self, text):
        return od.parse_order_qty(text, self.neck, 20)

    def test_valid_inputs(self):
        cases = {"100": 0.1, "200": 0.2, "300 г": 0.3, "400гр": 0.4, "500": 0.5, "600 грамм": 0.6, "700": 0.7,
                 "800": 0.8, "900": 0.9, "1000": 1.0, "1000 грамм": 1.0, "1 кг": 1.0, "1,5 кг": 1.5,
                 "1.2": 1.2, "0,3": 0.3, "2 кг": 2.0, "2300": 2.3, "0": 0.0}
        for text, expected in cases.items():
            self.assertEqual(self.qty(text), (expected, ""), text)

    def test_invalid_inputs(self):
        for text in ("350", "50 г", "0,25", "много", "", "25 кг", "1 кг 200 г"):
            qty, error = self.qty(text)
            self.assertIsNone(qty, text)
            self.assertTrue(error, text)

    def test_step_label_and_card_text(self):
        self.assertEqual(od.step_label(self.neck), "100 г")
        self.assertIn("кратно 100 г", od.format_product(self.neck, "₽"))
        self.assertIn("окончательную назовёт менеджер", od.format_product(self.neck, "₽"))
        self.assertEqual(od.step_label(od.Product("p", "c", "n", 1, "кг", step=0.5)), "500 г")


class PromoCoreTests(unittest.TestCase):
    def test_normalize(self):
        for raw in ("flyer", " FLYER ", "fly-er", "Fly er!", "FLYЕR"):   # последняя — русская «Е»
            self.assertEqual(normalize_code(raw), "FLYER", raw)

    def test_book(self):
        book = PromoBook([PromoCode("GASTRO-10", "Скидка"), PromoCode("OLD", active=False)])
        self.assertEqual(book.find("gastro10").code, "GASTRO-10")
        self.assertIsNone(book.find("nope"))
        self.assertFalse(book.find("old").active)
        self.assertTrue(PromoBook.load(ROOT / "config" / "promo.json").codes)
        self.assertEqual(PromoBook.load(ROOT / "config" / "nothing.json").codes, {})

    def test_db_one_use_per_person_and_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")

            async def go():
                self.assertTrue(await db.promo_activate("GASTROWELCOM", "vk", 1))
                self.assertFalse(await db.promo_activate("GASTROWELCOM", "vk", 1))     # тот же человек — нельзя
                self.assertTrue(await db.promo_activate("GASTROWELCOM", "vk", 2))      # другой — можно
                self.assertTrue(await db.promo_activate("GASTROWELCOM", "tg", 1))      # другая сеть — другой человек
                self.assertTrue(await db.promo_activate("OTHER", "vk", 1))      # другой код — можно
                self.assertEqual(await db.promo_pending("vk", 1), ["GASTROWELCOM", "OTHER"])
            asyncio.run(go())
            db.close()

    def test_old_database_gets_promo_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL DEFAULT 'tg', "
                         "user_id INTEGER NOT NULL, customer_name TEXT, username TEXT, phone TEXT, comment TEXT, "
                         "items_json TEXT NOT NULL, total REAL NOT NULL, status TEXT NOT NULL DEFAULT 'new', "
                         "created_at TEXT NOT NULL, final_json TEXT, final_total REAL, done_at REAL)")
            conn.execute("INSERT INTO orders(user_id, items_json, total, created_at) VALUES(1, '[]', 0, 'x')")
            conn.commit()
            conn.close()
            db = Database(path)
            order = asyncio.run(db.order(1))
            self.assertEqual(order.promo, "")
            oid = asyncio.run(db.create_order("vk", 2, "О", None, "+7", "", [("X", 1.0, 10.0, "кг")], 10, "GASTROWELCOM"))
            self.assertEqual(asyncio.run(db.order(oid)).promo, "GASTROWELCOM")
            db.close()

    def test_resolve_command(self):
        self.assertEqual(resolve_command("промокод", None), ("promo", ""))
        self.assertEqual(resolve_command("Промокод flyer", None), ("promo", "flyer"))
        self.assertEqual(resolve_command("/promo GASTROWELCOM", None), ("promo", "GASTROWELCOM"))
        self.assertEqual(resolve_command("промокод на скидку у вас есть?", None), (None, ""))


class VkPromoTests(VkTestCase):
    def manager_notices(self):
        return [t for t in self.api.texts_to(MANAGER) if "Введён промокод" in t]

    def test_button_flow_notifies_manager_and_blocks_repeat(self):
        self.run_all(message(CUSTOMER, CUSTOMER, "🎟 Промокод", PROMO_BTN))
        self.assertEqual(self.bot.checkout[CUSTOMER]["step"], "promo")
        self.run_all(message(CUSTOMER, CUSTOMER, "gastrowelcom"))
        self.assertNotIn(CUSTOMER, self.bot.checkout)
        self.assertIn("Промокод GASTROWELCOM принят", self.api.texts_to(CUSTOMER)[-1])
        notices = self.manager_notices()
        self.assertEqual(len(notices), 1)
        self.assertIn("[id100|Ольга Смирнова]", notices[0])
        self.assertIn("GASTROWELCOM", notices[0])
        # Тот же человек вводит тот же код ещё раз — отказ, менеджеру ничего не уходит
        self.run_all(message(CUSTOMER, CUSTOMER, "промокод GASTROWELCOM"))
        self.assertIn("уже вводили", self.api.texts_to(CUSTOMER)[-1])
        self.assertEqual(len(self.manager_notices()), 1)
        # Другой человек тот же код — может
        self.run_all(message(STRANGER, STRANGER, "GASTROWELCOM"))
        self.assertEqual(len(self.manager_notices()), 2)

    def test_welcome_code_is_for_first_order_only(self):
        self.run_all(message(STRANGER, STRANGER, "gastrowelcom"))                  # новый покупатель
        self.assertIn("принят", self.api.texts_to(STRANGER)[-1])
        self.assertIn("Скидка 10% на первый заказ", self.manager_notices()[0])
        asyncio.run(self.ctx.db.create_order("vk", CUSTOMER, "О", None, "+7", "", [("X", 1.0, 10.0, "кг")], 10))
        self.run_all(message(CUSTOMER, CUSTOMER, "GASTROWELCOM"))                  # у Ольги заказ уже есть
        self.assertIn("только на первый заказ", self.api.texts_to(CUSTOMER)[-1])
        self.assertEqual(len(self.manager_notices()), 1)

    def test_just_typing_the_code_works(self):
        self.run_all(message(CUSTOMER, CUSTOMER, "Gastrowelcom"))
        self.assertIn("принят", self.api.texts_to(CUSTOMER)[-1])
        self.assertEqual(len(self.manager_notices()), 1)

    def test_wrong_code_can_retry_and_is_rate_limited(self):
        self.run_all(message(CUSTOMER, CUSTOMER, "", PROMO_BTN), message(CUSTOMER, CUSTOMER, "ABC"))
        self.assertIn("Не нашёл", self.api.texts_to(CUSTOMER)[-1])
        self.assertEqual(self.bot.checkout[CUSTOMER]["step"], "promo")          # можно ввести другой
        self.run_all(*[message(CUSTOMER, CUSTOMER, f"X{i}") for i in range(12)])
        self.assertTrue(any("Слишком много" in t for t in self.api.texts_to(CUSTOMER)))
        self.run_all(message(CUSTOMER, CUSTOMER, "GASTROWELCOM"))                    # даже верный код сегодня не примут
        self.assertEqual(self.manager_notices(), [])

    def test_cancel_and_chat(self):
        self.run_all(message(CUSTOMER, CUSTOMER, "", PROMO_BTN), message(CUSTOMER, CUSTOMER, "отмена"))
        self.assertNotIn(CUSTOMER, self.bot.checkout)
        self.run_all(message(CHAT, CUSTOMER, "/promo GASTROWELCOM"))
        self.assertIn("личных сообщениях", self.api.texts_to(CHAT)[-1])
        self.assertEqual(self.manager_notices(), [])

    def test_promo_goes_to_next_order_only(self):
        def order():
            self.run_all(event(CUSTOMER, {"a": "add", "v": "set-week"}), event(CUSTOMER, {"a": "checkout"}),
                         message(CUSTOMER, CUSTOMER, "Смирнова Ольга"), message(CUSTOMER, CUSTOMER, "89991234567"),
                         message(CUSTOMER, CUSTOMER, "", {"cmd": "no_comment"}), event(CUSTOMER, {"a": "co_ok"}))
        self.run_all(message(CUSTOMER, CUSTOMER, "GASTROWELCOM"))
        order()
        order()
        first, second = asyncio.run(self.ctx.db.order(1)), asyncio.run(self.ctx.db.order(2))
        self.assertEqual((first.promo, second.promo), ("GASTROWELCOM", ""))
        manager_orders = [t for t in self.api.texts_to(MANAGER) if t.startswith("🔔 Заказ")]
        self.assertIn("Промокод: GASTROWELCOM", manager_orders[0])
        self.assertNotIn("Промокод", manager_orders[1])
        self.assertIn("Промокод: GASTROWELCOM", [t for t in self.api.texts_to(CUSTOMER) if "Заказ №1 оформлен" in t][0])


class VkWeightTests(VkTestCase):
    def cart(self):
        return asyncio.run(self.ctx.db.cart("vk", CUSTOMER))

    def type_weight(self, text):
        self.run_all(event(CUSTOMER, {"a": "setw", "v": "neck"}), message(CUSTOMER, CUSTOMER, text))

    def test_plus_minus_by_100_grams(self):
        self.run_all(*[event(CUSTOMER, {"a": "add", "v": "neck"}) for _ in range(7)])
        self.assertEqual(self.cart(), {"neck": 0.7})
        self.run_all(event(CUSTOMER, {"a": "sub", "v": "neck"}))
        self.assertEqual(self.cart(), {"neck": 0.6})

    def test_typed_weight_sets_cart(self):
        for text, expected in (("300", 0.3), ("1000 грамм", 1.0), ("1,5 кг", 1.5), ("900", 0.9)):
            self.type_weight(text)
            self.assertEqual(self.cart(), {"neck": expected}, text)
            self.assertNotIn(CUSTOMER, self.bot.checkout)
        self.assertIn("В корзине: 0,9 кг", [t for t in self.api.texts_to(CUSTOMER)][-1])

    def test_bad_weight_is_explained_and_cart_unchanged(self):
        self.type_weight("300")
        self.type_weight("350")
        self.assertEqual(self.cart(), {"neck": 0.3})
        self.assertIn("не кратен 100 г", self.api.texts_to(CUSTOMER)[-1])
        self.assertEqual(self.bot.checkout[CUSTOMER]["step"], "q_weight")     # можно сразу поправить
        self.run_all(message(CUSTOMER, CUSTOMER, "25 кг"))
        self.assertIn("слишком много", self.api.texts_to(CUSTOMER)[-1])
        self.run_all(message(CUSTOMER, CUSTOMER, "0"))
        self.assertEqual(self.cart(), {})

    def test_other_button_stops_waiting_for_weight(self):
        self.run_all(event(CUSTOMER, {"a": "setw", "v": "neck"}), event(CUSTOMER, {"a": "cart"}))
        self.assertNotIn(CUSTOMER, self.bot.checkout)

    def test_pieces_have_no_weight_input_and_keyboard_fits_vk(self):
        self.run_all(event(CUSTOMER, {"a": "setw", "v": "set-intro"}))
        self.assertNotIn(CUSTOMER, self.bot.checkout)
        for pid in ("neck", "set-intro"):
            for qty in (0, 0.3):
                rows, _ = kb.count_buttons(kb.product(self.ctx.catalog, pid, qty))
                self.assertLessEqual(rows, kb.INLINE_MAX_ROWS)
        self.assertLessEqual(kb.count_buttons(kb.categories(self.ctx.catalog))[0], kb.INLINE_MAX_ROWS)


if __name__ == "__main__":
    unittest.main()
