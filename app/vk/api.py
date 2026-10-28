"""Тонкий клиент VK API для бота сообщества: вызовы методов и Bots Long Poll.

Своя маленькая обёртка вместо сторонней библиотеки: нужно всего несколько методов,
а так меньше зависимостей и проще разбираться.
Документация: https://dev.vk.com/ru/api/bots/getting-started
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

API_URL = "https://api.vk.com/method/"
API_VERSION = "5.199"
CHAT_PEER_OFFSET = 2_000_000_000  # peer_id беседы = 2000000000 + номер беседы


class VkApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"VK API error {code}: {message}")
        self.code = code


def is_chat(peer_id: int) -> bool:
    return peer_id > CHAT_PEER_OFFSET


class VkApi:
    def __init__(self, token: str, group_id: int, version: str = API_VERSION):
        self.token = token
        self.group_id = group_id
        self.version = version
        self._session = None

    async def _http(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
        return self._session

    async def call(self, method: str, **params):
        clean = {}
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            elif isinstance(value, bool):
                value = int(value)
            clean[key] = value
        clean.update(access_token=self.token, v=self.version)
        session = await self._http()
        async with session.post(API_URL + method, data=clean) as resp:
            data = await resp.json(content_type=None)
        if "error" in data:
            err = data["error"]
            raise VkApiError(err.get("error_code", 0), err.get("error_msg", ""))
        return data.get("response")

    async def send(self, peer_id: int, text: str, keyboard: dict | None = None, **extra):
        return await self.call("messages.send", peer_id=peer_id, message=text, keyboard=keyboard,
                               random_id=random.randint(1, 2**31 - 1), dont_parse_links=1, **extra)

    async def edit(self, peer_id: int, conversation_message_id: int, text: str, keyboard: dict | None = None):
        return await self.call("messages.edit", peer_id=peer_id, conversation_message_id=conversation_message_id,
                               message=text, keyboard=keyboard, dont_parse_links=1)

    async def upload_message_photo(self, peer_id: int, path) -> str:
        """Загружает картинку для сообщений и возвращает вложение вида photo-1_2_key."""
        import aiohttp

        server = await self.call("photos.getMessagesUploadServer", peer_id=peer_id)
        session = await self._http()
        form = aiohttp.FormData()
        with open(path, "rb") as f:
            form.add_field("photo", f.read(), filename="catalog.jpg", content_type="image/jpeg")
        async with session.post(server["upload_url"], data=form) as resp:
            uploaded = await resp.json(content_type=None)
        saved = await self.call("photos.saveMessagesPhoto", photo=uploaded["photo"],
                                server=uploaded["server"], hash=uploaded["hash"])
        photo = saved[0]
        key = f"_{photo['access_key']}" if photo.get("access_key") else ""
        return f"photo{photo['owner_id']}_{photo['id']}{key}"

    async def answer_event(self, event_id: str, user_id: int, peer_id: int, text: str | None = None):
        data = {"type": "show_snackbar", "text": text[:90]} if text else None
        return await self.call("messages.sendMessageEventAnswer", event_id=event_id, user_id=user_id,
                               peer_id=peer_id, event_data=data)

    async def listen(self) -> AsyncIterator[dict]:
        """Бесконечный поток событий сообщества (Bots Long Poll API)."""
        server = None
        while True:
            try:
                if server is None:
                    server = await self.call("groups.getLongPollServer", group_id=self.group_id)
                session = await self._http()
                params = {"act": "a_check", "key": server["key"], "ts": server["ts"], "wait": 25}
                async with session.get(server["server"], params=params) as resp:
                    data = await resp.json(content_type=None)
                if "failed" in data:
                    if data["failed"] == 1:
                        server["ts"] = data["ts"]
                    else:  # ключ устарел или потерялась информация — берём новый сервер
                        server = None
                    continue
                server["ts"] = data["ts"]
                for update in data.get("updates", []):
                    yield update
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — сеть пропала и т.п.: ждём и пробуем снова
                logger.warning("VK Long Poll: %s — повтор через 5 с", exc)
                server = None
                await asyncio.sleep(5)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
