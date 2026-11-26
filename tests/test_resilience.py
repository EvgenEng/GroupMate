"""Надёжность и наблюдаемость: журнал без секретов, «сторож», уведомление о запуске, Long Poll после обрыва сети."""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

try:
    import aiohttp  # noqa: F401
except ImportError:   # окружение без aiohttp: хватит заглушки с теми же именами
    if "aiohttp" not in sys.modules:
        stub = types.ModuleType("aiohttp")
        stub.FormData = type("FormData", (), {"add_field": lambda self, *a, **k: None})
        stub.ClientError = type("ClientError", (Exception,), {})
        sys.modules["aiohttp"] = stub

from app import heartbeat
from app.config import Settings, LLMProvider
from app.logsetup import RedactingFormatter, collect_secrets, redact, setup_logging
from app.vk import api as vk_api
from tests.test_vk import MANAGER, VkTestCase


class RedactTests(unittest.TestCase):
    def test_phones_in_any_format_are_hidden(self):
        for phone in ("+7 999 123-45-67", "89991234567", "8 (999) 123-45-67", "+7(999)1234567", "7 999 123 45 67"):
            self.assertNotIn("123", redact(f"Телефон {phone}, заказ №5"), phone)
            self.assertIn("[телефон]", redact(f"Телефон {phone}, заказ №5"))

    def test_card_and_account_numbers_are_hidden(self):
        self.assertIn("[номер]", redact("Карта 2202 2000 1234 5678 оплачена"))
        self.assertNotIn("5678", redact("Карта 2202 2000 1234 5678 оплачена"))
        self.assertNotIn("40817810099910004312", redact("Счёт 40817810099910004312"))

    def test_useful_numbers_stay_readable(self):
        line = "Заказ №12 оформлен (vk): позиций 3, сумма 4200 | статус new → ready | 2026-11-26 10:06:00 | id 239343586"
        self.assertEqual(redact(line), line)

    def test_secrets_are_masked_everywhere(self):
        out = redact("GET https://hc-ping.com/abc-secret-123 token vk1.a.SECRETTOKEN", ("vk1.a.SECRETTOKEN", "abc-secret-123"))
        self.assertNotIn("SECRETTOKEN", out)
        self.assertNotIn("abc-secret-123", out)
        self.assertEqual(redact("кратко: ab", ("ab",)), "кратко: ab")   # слишком короткое не маскируем (иначе испортит текст)

    def test_traceback_is_redacted_too(self):
        logger = logging.getLogger("test.redact.tb")
        logger.propagate = False
        records: list[str] = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        handler = Grab()
        handler.setFormatter(RedactingFormatter("%(message)s", ("TOPSECRET-KEY",)))
        logger.addHandler(handler)
        try:
            raise RuntimeError("плохой ключ TOPSECRET-KEY, телефон +7 999 123-45-67")
        except RuntimeError:
            logger.exception("Ошибка")
        self.assertNotIn("TOPSECRET-KEY", records[0])
        self.assertNotIn("123-45-67", records[0])

    def test_collect_secrets_takes_tokens_keys_and_requisites(self):
        s = Settings(bot_token="123456:BOTTOKEN", owner_id=0, group_chat_id=0, telegram_proxy="", timezone="Europe/Moscow",
                     db_path=Path("x"), catalog_path=Path("x"), holidays_path=Path("x"),
                     vk_token="VKTOKEN-123", healthcheck_url="https://hc-ping.com/KEY-777",
                     payment_details="Сбер 2202 2000 1234 5678\nИП Иванов",
                     llm_providers=[LLMProvider(url="u", model="m", api_key="LLM-KEY-999")])
        secrets = collect_secrets(s)
        for expected in ("123456:BOTTOKEN", "VKTOKEN-123", "https://hc-ping.com/KEY-777", "LLM-KEY-999", "ИП Иванов"):
            self.assertIn(expected, secrets)


class LogFileTests(unittest.TestCase):
    def tearDown(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            h.close()
            root.removeHandler(h)

    def make_tmp(self) -> Path:
        """Папка, которую удалим уже после tearDown: на Windows открытый файл журнала удалить нельзя."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        return Path(tmp)

    def test_log_goes_to_rotating_file_without_secrets(self):
        log_dir = self.make_tmp() / "logs"   # папки ещё нет — бот создаёт сам
        setup_logging("INFO", log_dir, ("SECRET-VALUE",))
        logging.getLogger("t").info("ключ SECRET-VALUE, +7 999 123-45-67, заказ №7")
        for h in logging.getLogger().handlers:
            h.flush()
        text = (log_dir / "bot.log").read_text(encoding="utf-8")
        self.assertIn("заказ №7", text)
        self.assertNotIn("SECRET-VALUE", text)
        self.assertNotIn("123-45-67", text)
        from logging.handlers import RotatingFileHandler
        rot = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)][0]
        self.assertEqual((rot.maxBytes, rot.backupCount), (5 * 1024 * 1024, 5))

    def test_unwritable_dir_does_not_break_startup(self):
        blocker = self.make_tmp() / "file"
        blocker.write_text("x")
        setup_logging("INFO", blocker / "logs")   # «папка» внутри файла: создать нельзя
        logging.getLogger("t").info("работаем без файла")


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_ping_ok_and_failures(self):
        async def ok(url):
            return 200

        async def bad(url):
            return 500

        async def boom(url):
            raise OSError("нет сети")

        self.assertTrue(await heartbeat.ping("u", ok))
        self.assertFalse(await heartbeat.ping("u", bad))
        self.assertFalse(await heartbeat.ping("u", boom))

    async def test_logs_only_state_changes(self):
        answers = iter([200, OSError("нет сети"), OSError("нет сети"), 200, 200])
        calls = []

        async def fetch(url):
            calls.append(url)
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a

        async def sleep(seconds):
            assert seconds == 120
            if len(calls) == 5:
                raise asyncio.CancelledError

        with self.assertLogs("app.heartbeat", "INFO") as cap:
            with self.assertRaises(asyncio.CancelledError):
                await heartbeat.run_heartbeat("https://hc-ping.com/KEY", 2, fetch, sleep)
        messages = [r.getMessage() for r in cap.records]
        self.assertEqual(len(messages), 2)   # «пропали» один раз и «снова идут» один раз, без спама
        self.assertIn("не проходит", messages[0])
        self.assertIn("снова идут", messages[1])
        self.assertTrue(all("KEY" not in m for m in messages))   # адрес секретный


class StartupNoticeTests(VkTestCase):
    def test_managers_get_notice_once_per_ten_minutes(self):
        asyncio.run(self.bot.announce_start())
        self.assertEqual(len(self.api.texts_to(MANAGER)), 1)
        self.assertIn("запущен", self.api.texts_to(MANAGER)[0])
        asyncio.run(self.bot.announce_start())   # перезапуск по кругу не должен спамить
        self.assertEqual(len(self.api.texts_to(MANAGER)), 1)

    def test_notice_can_be_switched_off(self):
        import dataclasses
        self.ctx.settings = dataclasses.replace(self.ctx.settings, startup_notice=False)
        asyncio.run(self.bot.announce_start())
        self.assertEqual(self.api.texts_to(MANAGER), [])

    def test_notice_failure_does_not_break_startup(self):
        async def boom(html):
            raise RuntimeError("нет сети")
        self.ctx.messenger.notify_startup = boom
        asyncio.run(self.bot.announce_start())   # не должно упасть


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    async def json(self, content_type=None):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeLpSession:
    """Отвечает по сценарию: dict — ответ ВК, Exception — сбой сети; когда сценарий кончился — останавливаем цикл."""

    def __init__(self, script):
        self.script, self.requests = list(script), []

    def get(self, url, params=None):
        self.requests.append(dict(params))
        if not self.script:
            raise asyncio.CancelledError
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return FakeResp(step)


class FakeLpApi(vk_api.VkApi):
    def __init__(self, script, servers):
        super().__init__("token", 1)
        self._session = FakeLpSession(script)
        self.servers = list(servers)
        self.server_calls = 0

    async def call(self, method, **params):
        assert method == "groups.getLongPollServer"
        self.server_calls += 1
        return dict(self.servers.pop(0))


async def collect(api):
    got = []
    try:
        async for update in api.listen():
            got.append(update)
    except asyncio.CancelledError:
        pass
    return got


class LongPollTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch("asyncio.sleep", new=mock.AsyncMock())   # паузы по 5 с в тесте не ждём
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_short_network_outage_keeps_position_and_server(self):
        api = FakeLpApi(
            script=[{"ts": "101", "updates": [{"type": "message_new", "n": 1}]},
                    OSError("сеть пропала"),
                    {"ts": "105", "updates": [{"type": "message_new", "n": 2}]}],
            servers=[{"server": "https://lp", "key": "k1", "ts": "100"}])
        got = await collect(api)
        self.assertEqual([u["n"] for u in got], [1, 2])
        self.assertEqual(api.server_calls, 1)                      # после одного сбоя тот же сервер
        self.assertEqual([r["ts"] for r in api._session.requests], ["100", "101", "101", "105"])   # читаем с того же места

    async def test_long_outage_gets_new_server_but_keeps_position(self):
        api = FakeLpApi(
            script=[{"ts": "101", "updates": []}, OSError("1"), OSError("2"), OSError("3"),
                    {"ts": "110", "updates": [{"type": "message_new", "n": 3}]}],
            servers=[{"server": "https://lp", "key": "k1", "ts": "100"},
                     {"server": "https://lp2", "key": "k2", "ts": "9999"}])   # у нового сервера «свой» ts — он не должен победить
        got = await collect(api)
        self.assertEqual([u["n"] for u in got], [3])
        self.assertEqual(api.server_calls, 2)
        after_new_server = api._session.requests[4]   # 0–3: старый сервер и три сбоя подряд; 4: уже новый сервер
        self.assertEqual((after_new_server["key"], after_new_server["ts"]), ("k2", "101"))

    async def test_expired_key_keeps_position(self):
        api = FakeLpApi(
            script=[{"ts": "101", "updates": []}, {"failed": 2}, {"ts": "102", "updates": [{"type": "x", "n": 4}]}],
            servers=[{"server": "s", "key": "old", "ts": "100"}, {"server": "s", "key": "new", "ts": "500"}])
        got = await collect(api)
        self.assertEqual([u["n"] for u in got], [4])
        self.assertEqual(api._session.requests[2], {"act": "a_check", "key": "new", "ts": "101", "wait": 25})

    async def test_lost_history_starts_from_fresh_position(self):
        api = FakeLpApi(
            script=[{"ts": "101", "updates": []}, {"failed": 3}, {"ts": "501", "updates": []}],
            servers=[{"server": "s", "key": "k1", "ts": "100"}, {"server": "s", "key": "k2", "ts": "500"}])
        await collect(api)
        self.assertEqual(api._session.requests[2]["ts"], "500")

    async def test_failed_1_uses_ts_from_vk(self):
        api = FakeLpApi(
            script=[{"failed": 1, "ts": "77"}, {"ts": "78", "updates": []}],
            servers=[{"server": "s", "key": "k", "ts": "1"}])
        await collect(api)
        self.assertEqual(api._session.requests[1]["ts"], "77")


if __name__ == "__main__":
    unittest.main()
