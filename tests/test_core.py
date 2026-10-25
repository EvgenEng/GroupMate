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
        self.assertTrue(self.catalog.products)
        self.assertTrue(self.catalog.in_category("gifts"))

    def test_cart(self):
        lines = od.build_lines({"gift-s": 2, "cookies": 1, "missing": 3}, self.catalog)
        self.assertEqual(len(lines), 2)
        self.assertEqual(od.cart_total(lines), 3450)
        self.assertIn("3 450 ₽", od.format_cart(lines, "₽"))
        self.assertIn("пуста", od.format_cart([], "₽"))

    def test_order_text(self):
        o = od.OrderView(7, "Ольга", "olga", 5, "+79990000000", "к пятнице",
                         [("Торт", 2, 1800.0, "кг")], 3600, "new", "26.10.2026 12:00")
        text = od.format_order(o, "₽", for_owner=True)
        self.assertIn("Заказ №7", text)
        self.assertIn("@olga", text)
        self.assertNotIn("+7999", od.format_order(o, "₽"))


class HolidayTests(unittest.TestCase):
    def test_due(self):
        items = hol.load_holidays(ROOT / "config" / "holidays.json")
        due = hol.holidays_to_remind(items, date(2027, 2, 26))  # за 10 дней до 8 Марта
        self.assertEqual([(h.name, left) for h, left in due], [("8 Марта", 10)])
        self.assertIn("8 Марта", hol.holiday_text(due[0][0], 10, random.Random(0)))
        self.assertEqual(hol.holidays_to_remind(items, date(2027, 2, 27)), [])


class DatabaseTests(unittest.TestCase):
    def test_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")

            async def scenario():
                self.assertEqual(await db.cart_change(1, "p", 2), 2)
                self.assertEqual(await db.cart_change(1, "p", -5), 0)
                self.assertEqual(await db.cart(1), {})
                oid = await db.create_order(1, "Имя", None, "+7", "", [("X", 1, 10.0, "шт")], 10)
                await db.set_order_status(oid, "accepted")
                self.assertEqual((await db.order(oid)).status, "accepted")
                self.assertEqual(len(await db.recent_orders()), 1)
                first = await db.add_birthday("Иван", 1, 2, user_id=9)
                second = await db.add_birthday("Иван П.", 3, 4, user_id=9)  # тот же человек — обновление
                self.assertEqual(first, second)
                self.assertEqual(len(await db.list_birthdays()), 1)
                self.assertTrue(await db.usage_hit("d", 1, 2))
                self.assertTrue(await db.usage_hit("d", 1, 2))
                self.assertFalse(await db.usage_hit("d", 1, 2))
                await db.kv_set("k", {"a": [1, 2]})
                self.assertEqual(await db.kv_get("k"), {"a": [1, 2]})

            asyncio.run(scenario())
            db.close()


class ConfigAndMiscTests(unittest.TestCase):
    def test_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            p.write_text("GM_TEST_A=hello   # комментарий\nGM_TEST_B=\"x # y\"\n# GM_TEST_C=1\n", encoding="utf-8")
            load_dotenv(p)
            self.assertEqual(os.environ["GM_TEST_A"], "hello")
            self.assertEqual(os.environ["GM_TEST_B"], "x # y")
            self.assertNotIn("GM_TEST_C", os.environ)

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
