"""Тесты чистой логики. Запуск: python -m unittest discover -s tests"""
from __future__ import annotations

import asyncio
import os
import random
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from app.config import load_dotenv, parse_locations, _weekdays
from app.core import birthdays as bd
from app.core import holidays as hol
from app.core import orders as od
from app.core import weather as w
from app.scheduler import next_daily_run
from app.services import weather_reports
from app.services.db import Database
from app.services.llm import LLMError, build_payload, extract_text

ROOT = Path(__file__).resolve().parent.parent


def make_payload(day: date, temps, codes, probs, precs=None, gusts=None) -> dict:
    """24 часа на день `day` + 24 часа на следующий день."""
    times, t_all, c_all, p_all, pr_all, g_all = [], [], [], [], [], []
    for offset in (0, 1):
        d = day + timedelta(days=offset)
        for h in range(24):
            times.append(f"{d.isoformat()}T{h:02d}:00")
            t_all.append(temps[h] if callable(temps) is False else temps(h))
            c_all.append(codes[h])
            p_all.append(probs[h])
            pr_all.append((precs or [0] * 24)[h])
            g_all.append((gusts or [5] * 24)[h])
    return {
        "hourly": {"time": times, "temperature_2m": t_all, "apparent_temperature": t_all,
                   "weather_code": c_all, "precipitation_probability": p_all,
                   "precipitation": pr_all, "wind_gusts_10m": g_all},
        "current": {"temperature_2m": 3.2, "apparent_temperature": 0.1, "weather_code": 2,
                    "wind_speed_10m": 3},
    }


DAY = date(2026, 10, 26)
CALM = make_payload(DAY, [2 + h * 0.3 for h in range(24)], [2] * 24, [10] * 24)


class WeatherTests(unittest.TestCase):
    def test_fmt_temp(self):
        self.assertEqual(w.fmt_temp(3.4), "+3°")
        self.assertEqual(w.fmt_temp(-0.4), "0°")
        self.assertEqual(w.fmt_temp(-7.6), "−8°")

    def test_summary_and_report(self):
        fc = w.parse_open_meteo(CALM)
        s = w.summarize_day(fc, DAY)
        self.assertEqual(s.code, 2)
        self.assertEqual(set(s.periods), {"Утро", "День", "Вечер"})
        self.assertIsNone(s.first_rain_hour)
        text = w.format_report(w.morning_title(DAY), "Москва", s, [("Юг МО", s)], fc.current)
        self.assertIn("26 октября, понедельник", text)
        self.assertIn("Подмосковье", text)
        self.assertIn("Сейчас (Москва)", text)
        self.assertNotIn("Возьмите зонт", text)

    def test_low_probability_drizzle_is_not_main_event(self):
        codes = [2] * 24
        codes[14] = 51
        fc = w.parse_open_meteo(make_payload(DAY, [5] * 24, codes, [10] * 24))
        self.assertEqual(w.summarize_day(fc, DAY).code, 2)

    def test_rain_advice(self):
        codes, probs = [3] * 24, [20] * 24
        for h in range(13, 18):
            codes[h], probs[h] = 63, 85
        fc = w.parse_open_meteo(make_payload(DAY, [8] * 24, codes, probs, [1.0 if 13 <= h < 18 else 0 for h in range(24)]))
        s = w.summarize_day(fc, DAY)
        self.assertEqual(s.first_rain_hour, 13)
        self.assertIn("☂️ Возьмите зонт.", w.advice(s))

    def test_ice_advice(self):
        codes, probs = [3] * 24, [70] * 24
        for h in range(6, 10):
            codes[h] = 71
        fc = w.parse_open_meteo(make_payload(DAY, [-1 + h * 0.15 for h in range(24)], codes, probs,
                                             [0.4 if 6 <= h < 10 else 0 for h in range(24)]))
        self.assertTrue(any("гололед" in t for t in w.advice(w.summarize_day(fc, DAY))))

    def test_detect_changes(self):
        old = w.summarize_day(w.parse_open_meteo(CALM), DAY, from_hour=12)
        codes, probs = [2] * 24, [10] * 24
        for h in range(16, 20):
            codes[h], probs[h] = 63, 80
        gusts = [5] * 12 + [17] * 12
        new_fc = w.parse_open_meteo(make_payload(DAY, [2 + h * 0.3 - 4 for h in range(24)], codes, probs,
                                                 [1.0 if 16 <= h < 20 else 0 for h in range(24)], gusts))
        new = w.summarize_day(new_fc, DAY, from_hour=12)
        keys = {k for k, _ in w.detect_changes(old, new, 12)}
        self.assertEqual(keys, {"precip:дождь", "wind", "colder"})

    def test_no_changes_when_same(self):
        s = w.summarize_day(w.parse_open_meteo(CALM), DAY, from_hour=10)
        self.assertEqual(w.detect_changes(s, s, 10), [])

    def test_snapshot_roundtrip(self):
        fc = w.parse_open_meteo(CALM)
        restored = w.forecast_from_snapshot(w.day_snapshot(fc, DAY))
        self.assertEqual(w.summarize_day(restored, DAY), w.summarize_day(fc, DAY))


class FakeWeather:
    """Подменяет Open-Meteo: отдаёт заранее заданный прогноз."""
    def __init__(self, payload):
        from app.config import Location
        self.locations = [Location("Москва", 0, 0), Location("Юг МО", 0, 0)]
        self.payload = payload

    @property
    def main(self):
        return self.locations[0]

    async def get(self, loc, fresh=False):
        return w.parse_open_meteo(self.payload)

    async def get_all(self, fresh=False):
        return [(loc, w.parse_open_meteo(self.payload)) for loc in self.locations]


class WeatherReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_morning_then_alerts_once(self):
        ws = FakeWeather(CALM)
        text = asyncio.run(weather_reports.morning_report(ws, self.db, DAY))
        self.assertIn("Доброе утро", text)
        now = datetime(2026, 10, 26, 13, 0)
        self.assertIsNone(asyncio.run(weather_reports.check_changes(ws, self.db, now)))
        # Прогноз поменялся: вечером дождь
        codes, probs = [2] * 24, [10] * 24
        for h in range(17, 21):
            codes[h], probs[h] = 63, 90
        ws.payload = make_payload(DAY, [2 + h * 0.3 for h in range(24)], codes, probs,
                                  [1.2 if 17 <= h < 21 else 0 for h in range(24)])
        alert = asyncio.run(weather_reports.check_changes(ws, self.db, now))
        self.assertIn("дождь примерно с 17:00", alert)
        # Повторно то же самое не шлём
        self.assertIsNone(asyncio.run(weather_reports.check_changes(ws, self.db, now)))

    def test_evening_is_for_tomorrow(self):
        text = asyncio.run(weather_reports.evening_report(FakeWeather(CALM), DAY))
        self.assertIn("27 октября, вторник", text)


class BirthdayTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(bd.parse_day_month("5.3"), (5, 3))
        self.assertEqual(bd.parse_day_month("29/02"), (29, 2))
        self.assertIsNone(bd.parse_day_month("31.04"))
        self.assertIsNone(bd.parse_day_month("абв"))

    def test_next_occurrence_and_leap(self):
        self.assertEqual(bd.next_occurrence(29, 2, date(2027, 1, 10)), date(2027, 2, 28))
        self.assertEqual(bd.next_occurrence(1, 1, date(2026, 12, 31)), date(2027, 1, 1))

    def test_upcoming_and_words(self):
        items = [bd.Birthday(1, "А", 30, 10), bd.Birthday(2, "Б", 26, 10), bd.Birthday(3, "В", 1, 1)]
        ranked = bd.upcoming(items, DAY)
        self.assertEqual([b.name for b, _ in ranked], ["Б", "А", "В"])
        self.assertEqual(bd.days_word(0), "сегодня")
        self.assertEqual(bd.days_word(1), "завтра")
        self.assertEqual(bd.days_word(4), "через 4 дня")
        self.assertEqual(bd.days_word(11), "через 11 дней")
        self.assertEqual(bd.days_word(21), "через 21 день")

    def test_mentions_are_escaped(self):
        b = bd.Birthday(1, "Иван <script>", 1, 1, user_id=42)
        text = bd.insert_mentions("С днём рождения, Иван <script>! <b>ура</b>", [b])
        self.assertIn('<a href="tg://user?id=42">Иван &lt;script&gt;</a>', text)
        self.assertIn("&lt;b&gt;ура", text)
        self.assertIn("tg://user?id=42", bd.fallback_greeting([b], random.Random(1)))


class OrderTests(unittest.TestCase):
    def setUp(self):
        self.catalog = od.Catalog.load(ROOT / "config" / "catalog.json")

    def test_catalog_loads(self):
        self.assertEqual(len(self.catalog.products), 25)
        self.assertEqual([c.id for c in self.catalog.visible_categories()], ["deli", "sausage", "sets"])
        neck = self.catalog.product("neck")
        self.assertEqual((neck.price, neck.unit, neck.step), (1700, "кг", 0.5))
        self.assertIn("3 до 7 дней", od.catalog_intro(self.catalog))

    def test_cart_by_weight_and_pieces(self):
        lines = od.build_lines({"neck": 1.5, "set-intro": 2, "missing": 3}, self.catalog)
        self.assertEqual(len(lines), 2)
        self.assertEqual(od.cart_total(lines), 1700 * 1.5 + 2490 * 2)
        text = od.format_cart(lines, "₽")
        self.assertIn("1,5 кг", text)
        self.assertIn("≈ 7 530 ₽", text)          # есть весовой товар — сумма примерная
        only_sets = od.format_cart(od.build_lines({"set-week": 1}, self.catalog), "₽")
        self.assertNotIn("≈", only_sets)
        self.assertIn("пуста", od.format_cart([], "₽"))

    def test_fmt_qty(self):
        self.assertEqual([od.fmt_qty(x) for x in (1, 1.0, 0.5, 2.25)], ["1", "1", "0,5", "2,25"])

    def test_order_text(self):
        o = od.OrderView(7, "Ольга", "olga", 5, "+79990000000", "к пятнице",
                         [("Шейка", 1.5, 1700.0, "кг")], 2550, "new", "26.10.2026 12:00")
        text = od.format_order(o, "₽", for_owner=True)
        self.assertIn("Заказ №7", text)
        self.assertIn("@olga", text)
        self.assertIn("Telegram", text)
        self.assertNotIn("+7999", od.format_order(o, "₽"))
        o.platform = "vk"
        self.assertIn('href="https://vk.ru/id5"', od.format_order(o, "₽", for_owner=True))


class HolidayTests(unittest.TestCase):
    def test_due(self):
        items = hol.load_holidays(ROOT / "config" / "holidays.json")
        due = hol.holidays_to_remind(items, date(2027, 2, 28))  # за 8 дней до 8 Марта
        self.assertEqual([(h.name, left) for h, left in due], [("8 Марта", 8)])
        self.assertIn("8 Марта", hol.holiday_text(due[0][0], 8, random.Random(0)))
        self.assertEqual(hol.holidays_to_remind(items, date(2027, 2, 27)), [])


class DatabaseTests(unittest.TestCase):
    def test_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")

            async def scenario():
                self.assertEqual(await db.cart_change("tg", 1, "p", 0.5), 0.5)
                self.assertEqual(await db.cart_change("tg", 1, "p", 0.5), 1.0)
                self.assertEqual(await db.cart_change("vk", 1, "p", 1), 1.0)   # тот же ID, другая сеть
                self.assertEqual(await db.cart_change("tg", 1, "p", -5), 0)
                self.assertEqual(await db.cart("tg", 1), {})
                self.assertEqual(await db.cart("vk", 1), {"p": 1.0})
                oid = await db.create_order("vk", 1, "Имя", None, "+7", "", [("X", 1.5, 10.0, "кг")], 15)
                await db.set_order_status(oid, "accepted")
                order = await db.order(oid)
                self.assertEqual((order.status, order.platform, order.items[0][1]), ("accepted", "vk", 1.5))
                self.assertEqual(len(await db.recent_orders()), 1)
                self.assertEqual(await db.orders_of("tg", 1), [])
                first = await db.add_birthday("Иван", 1, 2, "tg", user_id=9)
                second = await db.add_birthday("Иван П.", 3, 4, "tg", user_id=9)  # тот же человек — обновление
                self.assertEqual(first, second)
                await db.add_birthday("Иван ВК", 5, 6, "vk", user_id=9)
                self.assertEqual(len(await db.list_birthdays()), 2)
                self.assertTrue(await db.delete_birthday_of("vk", 9))
                self.assertEqual(len(await db.list_birthdays()), 1)
                self.assertTrue(await db.usage_hit("d", "tg", 1, 2))
                self.assertTrue(await db.usage_hit("d", "tg", 1, 2))
                self.assertFalse(await db.usage_hit("d", "tg", 1, 2))
                self.assertTrue(await db.usage_hit("d", "vk", 1, 2))
                self.assertTrue(await db.touch_user("tg", 5, "Аня", "Artdin93"))
                self.assertFalse(await db.touch_user("tg", 5, "Аня", "Artdin93"))
                self.assertEqual(await db.user_id_by_username("tg", "@artdin93"), 5)
                await db.kv_set("k", {"a": [1, 2]})
                self.assertEqual(await db.kv_get("k"), {"a": [1, 2]})

            asyncio.run(scenario())
            db.close()

    def test_migrates_first_version_database(self):
        """База первой версии (без колонки platform) обновляется без потери данных."""
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE users (user_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT, last_seen TEXT);
                CREATE TABLE birthdays (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, day INTEGER NOT NULL,
                    month INTEGER NOT NULL, user_id INTEGER UNIQUE, username TEXT);
                CREATE TABLE cart (user_id INTEGER NOT NULL, product_id TEXT NOT NULL, qty INTEGER NOT NULL,
                    PRIMARY KEY (user_id, product_id));
                CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, customer_name TEXT,
                    username TEXT, phone TEXT, comment TEXT, items_json TEXT NOT NULL, total REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL);
                CREATE TABLE usage (day TEXT NOT NULL, user_id INTEGER NOT NULL, count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (day, user_id));
                CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO users VALUES (5, 'Аня', 'artdin93', '2026-10-01');
                INSERT INTO birthdays(name, day, month, user_id) VALUES ('Аня', 3, 4, 5);
                INSERT INTO cart VALUES (5, 'neck', 2);
                INSERT INTO orders(user_id, customer_name, items_json, total, created_at)
                    VALUES (5, 'Аня', '[["X", 1, 10.0, "шт"]]', 10, '01.10.2026 10:00');
            """)
            conn.commit()
            conn.close()
            db = Database(path)

            async def check():
                self.assertEqual(await db.user_id_by_username("tg", "artdin93"), 5)
                self.assertEqual((await db.birthday_of("tg", 5)).name, "Аня")
                self.assertEqual(await db.cart("tg", 5), {"neck": 2.0})
                self.assertEqual((await db.order(1)).platform, "tg")
                self.assertTrue(await db.usage_hit("d", "tg", 5, 1))

            asyncio.run(check())
            db.close()


class ConfigAndMiscTests(unittest.TestCase):
    def test_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            p.write_text("GM_TEST_A=hello   # комментарий\nGM_TEST_B=\"x # y\"\n# GM_TEST_C=1\n"
                         "GM_TEST_D=            # подсказка\n", encoding="utf-8")
            load_dotenv(p)
            self.assertEqual(os.environ["GM_TEST_A"], "hello")
            self.assertEqual(os.environ["GM_TEST_B"], "x # y")
            self.assertNotIn("GM_TEST_C", os.environ)
            self.assertEqual(os.environ["GM_TEST_D"], "")

    def test_locations_and_weekdays(self):
        locs = parse_locations("Москва:55.75:37.61; Юг: МО:54.9:37.4")
        self.assertEqual([l.name for l in locs], ["Москва", "Юг: МО"])
        self.assertEqual(_weekdays("пт, сб", (0,)), (4, 5))
        self.assertEqual(_weekdays("", (4,)), (4,))

    def test_scheduler_next_run(self):
        now = datetime(2026, 10, 26, 8, 0)  # понедельник
        self.assertEqual(next_daily_run(now, 7, 0), datetime(2026, 10, 27, 7, 0))
        self.assertEqual(next_daily_run(now, 21, 0), datetime(2026, 10, 26, 21, 0))
        self.assertEqual(next_daily_run(now, 12, 0, weekdays=(4,)), datetime(2026, 10, 30, 12, 0))

    def test_llm_payload_has_no_provider_specific_fields(self):
        payload = build_payload("m", [{"role": "user", "content": "hi"}], 100, 0.5)
        self.assertEqual(set(payload), {"model", "messages", "max_tokens", "temperature"})
        self.assertEqual(extract_text({"choices": [{"message": {"content": "<think>x</think> Привет"}}]}), "Привет")
        with self.assertRaises(LLMError):
            extract_text({"error": "quota"})


if __name__ == "__main__":
    unittest.main()


class WeighingAndRepeatCoreTests(unittest.TestCase):
    """Взвешивание заказа (итог по факту) и подбор позиций для «Повторить заказ»."""

    def setUp(self):
        self.catalog = od.Catalog.load(ROOT / "config" / "catalog.json")
        self.items = [("Шейка классическая в/к", 1.5, 1700.0, "кг"),
                      ("Набор «Знакомство с GASTRODOM»", 1.0, 2490.0, "шт"),
                      ("Карбонад классический в/к", 1.0, 1500.0, "кг")]
        self.order = od.OrderView(5, "Ольга", None, 100, "+7999", "", self.items, 1700 * 1.5 + 2490 + 1500,
                                  "accepted", "07.11.2026 10:00", "vk")

    def test_parse_weight(self):
        cases = {"0,95": 0.95, "0.95 кг": 0.95, "950 г": 0.95, "950": 0.95, "1": 1.0, "1,5кг": 1.5,
                 "1.2 КГ": 1.2, "950гр": 0.95, "0": 0.0, "  2  ": 2.0}
        for text, expected in cases.items():
            self.assertEqual(od.parse_weight(text), expected, text)
        for bad in ("", "abc", "60 кг", "1,2,3", "-1", "кг"):
            self.assertIsNone(od.parse_weight(bad), bad)
        self.assertEqual(od.parse_weight("0,95", "г"), 950.0)   # если товар считается в граммах

    def test_weight_indexes_and_has_weight(self):
        self.assertEqual(od.weight_indexes(self.order), [0, 2])
        self.assertTrue(od.order_has_weight(self.order))
        sets_only = od.OrderView(6, "", None, 1, "", "", [("Набор", 1.0, 2490.0, "шт")], 2490, "new", "", "vk")
        self.assertFalse(od.order_has_weight(sets_only))

    def test_apply_weights_and_total(self):
        final = od.apply_weights(self.items, {0: 1.35, 2: 0.0})   # шейка полегче, карбоната не нашлось
        self.assertEqual([n for n, *_ in final], ["Шейка классическая в/к", "Набор «Знакомство с GASTRODOM»"])
        self.assertAlmostEqual(od.items_total(final), 1700 * 1.35 + 2490)
        self.assertEqual(od.apply_weights(self.items, {})[0][1], 1.5)   # без правок — как заказано

    def test_final_texts(self):
        final = od.apply_weights(self.items, {0: 1.35, 2: 0.0})
        preview = od.format_weighing(self.order, final, "₽")
        self.assertIn("Шейка классическая в/к — 1,35 кг × 1 700 ₽ = <b>2 295 ₽</b>", preview)
        self.assertIn("Карбонад классический в/к — убрано из заказа", preview)
        self.assertIn("Итого: <b>4 785 ₽</b>", preview)
        self.order.final_items, self.order.final_total = final, od.items_total(final)
        message = od.format_final_message(self.order, "₽", "Карта 1234\nПолучатель: И. Иванов")
        self.assertIn("Итого к оплате: <b>4 785 ₽</b>", message)
        self.assertIn("при заказе было ≈ 6 540 ₽", message)
        self.assertIn("Карта 1234\nПолучатель: И. Иванов", message)
        self.assertIn("пришлёт менеджер", od.format_final_message(self.order, "₽", ""))
        # покупатель и менеджер видят уже фактический вес
        self.assertIn("⚖️ Взвешено. Итого к оплате: <b>4 785 ₽</b>", od.format_order(self.order, "₽"))
        self.assertIn("по факту", od.format_manager_order(self.order, "₽"))
        self.assertNotIn("Карбонад", od.format_manager_order(self.order, "₽"))

    def test_match_repeat(self):
        items = [("шейка КЛАССИЧЕСКАЯ в/к", 1.0, 1.0, "кг"),            # другой регистр
                 ("Мясной «орешек» в/к", 0.5, 1.0, "кг"),
                 ("Позиция, которой больше нет", 1.0, 1.0, "шт")]
        found, missing = od.match_repeat(items, self.catalog)
        self.assertEqual([(p.id, q) for p, q in found], [("neck", 1.0), ("nut", 0.5)])
        self.assertEqual(missing, ["Позиция, которой больше нет"])
        # временно недоступный товар не добавляем
        import dataclasses
        self.catalog.products[self.catalog.products.index(self.catalog.product("neck"))] = \
            dataclasses.replace(self.catalog.product("neck"), available=False)
        found, missing = od.match_repeat(items, self.catalog)
        self.assertIn("шейка КЛАССИЧЕСКАЯ в/к", missing)

    def test_db_stores_final_and_migrates_old_orders_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            import sqlite3
            old = sqlite3.connect(path)     # база до появления взвешивания: в orders нет final_json
            old.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL DEFAULT 'tg', "
                        "user_id INTEGER NOT NULL, customer_name TEXT, username TEXT, phone TEXT, comment TEXT, "
                        "items_json TEXT NOT NULL, total REAL NOT NULL, status TEXT NOT NULL DEFAULT 'new', "
                        "created_at TEXT NOT NULL)")
            old.execute("INSERT INTO orders(platform, user_id, items_json, total, created_at) "
                        "VALUES('vk', 1, '[[\"X\", 1.5, 10.0, \"кг\"]]', 15, '01.01.2026 10:00')")
            old.commit()
            old.close()
            db = Database(path)

            async def scenario():
                order = await db.order(1)
                self.assertIsNone(order.final_items)              # старый заказ читается как раньше
                self.assertIsNone(order.final_total)
                await db.set_order_final(1, [("X", 1.2, 10.0, "кг")], 12.0)
                order = await db.order(1)
                self.assertEqual(order.final_items, [("X", 1.2, 10.0, "кг")])
                self.assertEqual(order.final_total, 12.0)
                self.assertEqual(order.items, [("X", 1.5, 10.0, "кг")])   # заказанное не потерялось

            asyncio.run(scenario())
            db.close()


class StatsAndExportCoreTests(unittest.TestCase):
    """Статистика для админов, таблица Excel и новые выборки из базы."""

    NOW = datetime(2026, 11, 8, 12, 0)

    @staticmethod
    def make(order_id, user, when, items, status="done", platform="vk", final=None):
        total = sum(q * p for _n, q, p, _u in items)
        o = od.OrderView(order_id, f"Покупатель {user}", None, user, "+7 900", "", items, total, status, when, platform)
        if final is not None:
            o.final_items, o.final_total = final, sum(q * p for _n, q, p, _u in final)
        return o

    def orders(self):
        shejka, nabor = ("Шейка классическая в/к", 1.5, 1700.0, "кг"), ("Набор «Мясной стол»", 1.0, 3490.0, "шт")
        return [
            self.make(1, 10, "01.09.2026 10:00", [shejka]),                                   # давно, покупатель 10 — старый
            self.make(2, 10, "05.11.2026 10:00", [shejka], final=[("Шейка классическая в/к", 1.35, 1700.0, "кг")]),
            self.make(3, 11, "06.11.2026 11:00", [nabor], status="accepted"),                 # новый покупатель
            self.make(4, 12, "07.11.2026 12:00", [nabor], status="cancelled"),                # отменён — не в выручке
            self.make(5, 13, "20.10.2026 09:00", [nabor]),                                    # вне недели
        ]

    def test_parse_days(self):
        from app.core.stats import parse_days
        cases = {"": 7, "30": 30, "30 дней": 30, "неделя": 7, "месяц": 30, "0": 1, "9999": 366, "abc": 7}
        for text, expected in cases.items():
            self.assertEqual(parse_days(text, 7), expected, text)

    def test_compute_stats_week(self):
        from app.core.stats import compute_stats
        s = compute_stats(self.orders(), self.NOW, 7, ratings=[5, 4, 2], joined=[(1.0, 11), (2.0, 99)])
        self.assertEqual((s.orders, s.cancelled, s.weighed), (2, 1, 1))
        self.assertAlmostEqual(s.revenue, 1700 * 1.35 + 3490)            # у взвешенного — сумма по факту
        self.assertAlmostEqual(s.avg_check, (1700 * 1.35 + 3490) / 2)
        self.assertEqual((s.buyers, s.new_buyers), (2, 1))                # покупатель 10 уже заказывал раньше
        self.assertEqual(s.top[0][0], "Набор «Мясной стол»")
        self.assertEqual(s.by_status, {"done": 1, "accepted": 1, "cancelled": 1})
        self.assertEqual((s.rating_count, round(s.rating_avg, 2)), (3, 3.67))
        self.assertEqual((s.joined, s.joined_ordered), (2, 1))            # из двух подписчиков заказал один

    def test_format_stats_and_empty_period(self):
        from app.core.stats import compute_stats, format_stats
        text = format_stats(compute_stats(self.orders(), self.NOW, 7, ratings=[5]), "₽")
        self.assertIn("Статистика за 7 дней", text)
        self.assertIn("Заказов: <b>2</b> (отменено: 1)", text)
        self.assertIn("средний чек", text)
        self.assertIn("новых: 1, повторных: 1", text)
        self.assertIn("Оценки покупателей: <b>5,0</b>", text)
        empty = format_stats(compute_stats([], self.NOW, 30, None), "₽")
        self.assertIn("за 30 дней", empty.lower().replace("статистика ", ""))
        self.assertIn("заказов не было", empty)

    def test_export_tables_period_and_weighed_items(self):
        from app.core.stats import export_tables
        tables = export_tables(self.orders(), self.NOW, 7, "₽")
        order_rows, item_rows = tables["Заказы"], tables["Позиции"]
        self.assertEqual([r[0] for r in order_rows[1:]], [2, 3, 4])      # только за неделю, статусы любые
        weighed = order_rows[1]
        self.assertEqual((weighed[8], weighed[9]), (2550.0, 2295.0))      # заказано / по факту
        self.assertIn("Шейка классическая в/к — 1,35 кг", weighed[7])    # в таблице — фактический вес
        self.assertEqual(len(item_rows), 4)
        self.assertEqual(item_rows[1][-1], "да")

    def test_xlsx_is_valid_and_escapes_text(self):
        import zipfile
        import xml.etree.ElementTree as ET
        from app.core.xlsx import write_xlsx
        with tempfile.TemporaryDirectory() as tmp:
            path = write_xlsx(Path(tmp) / "t.xlsx", {"Заказы": [["№", "Состав"], [1, 'Шейка & <b> "в/к"\nНабор'], [2, 1700.5]],
                                                      "Позиции": [["Товар"], ["Карбонад"]]})
            with zipfile.ZipFile(path) as z:
                self.assertIsNone(z.testzip())
                for name in z.namelist():
                    ET.fromstring(z.read(name))                           # каждый кусок — корректный XML
                sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
                ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                texts = ["".join(t.itertext()) for t in sheet.iterfind(".//m:is", ns)]
                self.assertIn('Шейка & <b> "в/к"\nНабор', texts)
                self.assertEqual(sheet.find(".//m:c[@r='B3']/m:v", ns).text, "1700.5")
                self.assertIn("Позиции", z.read("xl/workbook.xml").decode("utf-8"))

    def test_db_carts_reviews_kv_and_migration(self):
        import sqlite3
        import time
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            old = sqlite3.connect(path)       # база до этих функций: у корзины и заказов нет новых колонок
            old.execute("CREATE TABLE cart (platform TEXT NOT NULL DEFAULT 'tg', user_id INTEGER NOT NULL, "
                        "product_id TEXT NOT NULL, qty REAL NOT NULL, PRIMARY KEY (platform, user_id, product_id))")
            old.execute("INSERT INTO cart VALUES('vk', 1, 'neck', 1.5)")
            old.commit()
            old.close()
            db = Database(path)

            async def scenario():
                now = time.time()
                # старая корзина без времени — не считается брошенной
                self.assertEqual(await db.stale_carts("vk", now, now - 10 * 86400), [])
                await db.cart_change("vk", 2, "neck", 1.0)
                await db._a("UPDATE cart SET updated_at=? WHERE user_id=2", (now - 30 * 3600,))
                await db.cart_change("vk", 3, "neck", 1.0)                      # свежая
                self.assertEqual([u for u, _ in await db.stale_carts("vk", now - 24 * 3600, now - 72 * 3600)], [2])
                # выдача заказа запоминает время; оценка ставится один раз
                oid = await db.create_order("vk", 2, "О", None, "+7", "", [("X", 1.0, 10.0, "кг")], 10.0)
                self.assertEqual(await db.orders_to_review("vk", now + 1, now - 1), [])
                await db.set_order_status(oid, "done")
                self.assertEqual([o.id for o in await db.orders_to_review("vk", now + 5, now - 5)], [oid])
                self.assertEqual(await db.orders_to_review("tg", now + 5, now - 5), [])
                self.assertTrue(await db.add_review(oid, "vk", 2, 5))
                self.assertFalse(await db.add_review(oid, "vk", 2, 1))
                await db.set_review_comment(oid, "Вкусно")
                self.assertEqual((await db.review(oid))["comment"], "Вкусно")
                self.assertEqual(await db.ratings_since(now - 5), [5])
                # выборка kv по префиксу (подчёркивания и проценты — обычные символы)
                await db.kv_set("welcome:vk:1", {"ts": 1.0})
                await db.kv_set("welcome:vk:2", {"ts": 2.0})
                await db.kv_set("other:vk:3", 1)
                self.assertEqual(set(await db.kv_prefix("welcome:vk:")), {"welcome:vk:1", "welcome:vk:2"})
                self.assertEqual(len(await db.all_orders()), 1)

            asyncio.run(scenario())
            db.close()


class GreetingTextsTests(unittest.TestCase):
    """Тексты поздравлений: общие (по 3 → 6) и личные для покупателя."""

    def test_variants_are_complete_and_unique(self):
        from app.core import birthdays as bd
        self.assertEqual(len(bd.FALLBACK_GREETINGS), 6)
        self.assertEqual(len(bd.PERSONAL_GREETINGS), 6)
        for text in bd.FALLBACK_GREETINGS:
            self.assertIn("{names}", text)
            self.assertNotIn("{", text.replace("{names}", ""))
        for text in bd.PERSONAL_GREETINGS:
            self.assertIn("{name}", text)
            self.assertNotIn("{", text.replace("{name}", ""))
            self.assertIn("подар", text)                              # в каждом — напоминание про подарок к заказу
            self.assertTrue(text.split("{name}")[0].strip() in {"🎂", "🥳", "🐻", "🎉", "✨", "🌟"})   # имя в начале фразы
            self.assertNotIn("&", text)
            self.assertNotIn("<", text)
        self.assertEqual(len(set(bd.FALLBACK_GREETINGS)), 6)
        self.assertEqual(len(set(bd.PERSONAL_GREETINGS)), 6)

    def test_personal_name(self):
        from app.core import birthdays as bd
        person = lambda name: bd.Birthday(1, name, 1, 1, 10, None, "vk")
        self.assertEqual(bd.personal_name(person("Ольга Смирнова")), "Ольга")
        self.assertEqual(bd.personal_name(person("  Пётр ")), "Пётр")
        self.assertEqual(bd.personal_name(person("id12345")), "Дорогой друг")
        self.assertEqual(bd.personal_name(person("")), "Дорогой друг")
        self.assertEqual(bd.personal_name(person("<b>Оля</b> Иванова")), "&lt;b&gt;Оля&lt;/b&gt;")   # без разметки от пользователя
        text = bd.personal_greeting(person("Ольга Смирнова"), index=2)
        self.assertTrue(text.startswith("🐻 Ольга, это мишка из GASTRODOM"))
        for i in range(6):                                           # каждый вариант собирается без ошибок
            self.assertIn("Ольга", bd.personal_greeting(person("Ольга"), index=i))
        import random
        seen = {bd.personal_greeting(person("Ольга"), random.Random(seed)) for seed in range(60)}
        self.assertEqual(len(seen), 6)                              # при случайном выборе встречаются все шесть
