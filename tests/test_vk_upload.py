"""Загрузка файлов в ВК переживает пустые ответы сервера загрузки (ошибка «Expecting value»)."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path

from app.vk import api as vk_api

try:
    import aiohttp  # noqa: F401
except ImportError:   # окружение без aiohttp: хватит заглушки с теми же именами
    stub = types.ModuleType("aiohttp")

    class _Form:
        def add_field(self, *a, **k):
            pass
    stub.FormData = _Form
    stub.ClientError = type("ClientError", (Exception,), {})
    sys.modules["aiohttp"] = stub


class FakeResponse:
    def __init__(self, status: int, body: str):
        self.status, self._body = status, body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Отвечает по списку: (статус, тело) или исключение; последний ответ повторяется."""

    def __init__(self, answers):
        self.answers, self.hits = answers, 0

    def post(self, url, data=None):
        answer = self.answers[min(self.hits, len(self.answers) - 1)]
        self.hits += 1
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(*answer)


class FakeUploadApi(vk_api.VkApi):
    def __init__(self, answers):
        super().__init__("token", 1)
        self._session = FakeSession(answers)

    async def call(self, method, **params):
        if method.endswith("UploadServer"):
            return {"upload_url": "http://upload.test/up"}
        return [{"owner_id": 7, "id": 8, "access_key": "k"}]


OK = (200, '{"photo": "[{x}]", "server": 1, "hash": "h"}')


class UploadRetryTests(unittest.TestCase):
    def upload(self, answers):
        api = FakeUploadApi(answers)
        orig = api._upload_file

        async def fast(*a, **k):
            return await orig(*a, **{**k, "pause": 0.001})
        api._upload_file = fast

        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                img = Path(tmp) / "c.jpg"
                img.write_bytes(b"\xff\xd8\xff\xe0fake")
                try:
                    return await api.upload_message_photo(5, img), None
                except Exception as exc:  # noqa: BLE001
                    return None, str(exc)
        result, error = asyncio.run(go())
        return result, error, api._session.hits

    def test_empty_answer_then_success(self):
        self.assertEqual(self.upload([(200, ""), OK]), ("photo7_8_k", None, 2))

    def test_bad_gateway_and_empty_then_success(self):
        self.assertEqual(self.upload([(502, "<html>Bad gateway</html>"), (200, ""), OK]), ("photo7_8_k", None, 3))

    def test_permanent_failure_explains_reason(self):
        result, error, hits = self.upload([(200, "")])
        self.assertIsNone(result)
        self.assertEqual(hits, 3)
        self.assertIn("не JSON", error)
        self.assertIn("HTTP 200", error)

    def test_file_not_accepted(self):
        _, error, hits = self.upload([(200, '{"photo": "[]", "server": 1, "hash": "h"}')])
        self.assertEqual(hits, 3)
        self.assertIn("файл не принят", error)

    def test_doc_upload_uses_same_retries(self):
        api = FakeUploadApi([(200, ""), (200, '{"file": "abc"}')])
        orig = api._upload_file

        async def fast(*a, **k):
            return await orig(*a, **{**k, "pause": 0.001})
        api._upload_file = fast

        async def call(method, **params):
            if method.endswith("UploadServer"):
                return {"upload_url": "http://upload.test/up"}
            return {"doc": {"owner_id": 1, "id": 2}}
        api.call = call
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.xlsx"
            f.write_bytes(b"x")
            self.assertEqual(asyncio.run(api.upload_message_doc(5, f)), "doc1_2")
        self.assertEqual(api._session.hits, 2)


if __name__ == "__main__":
    unittest.main()
