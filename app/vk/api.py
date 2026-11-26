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

    async def _upload_file(self, peer_id: int, get_server: str, server_params: dict, field: str,
                           data: bytes, filename: str, content_type: str, required: tuple[str, ...],
                           attempts: int = 3, pause: float = 1.5) -> dict:
        """Отправляет файл на сервер загрузки ВК. Сервер иногда отвечает пусто или ошибкой —
        тогда берём новый адрес загрузки и пробуем ещё (с паузой). Понятная причина — в тексте ошибки."""
        import aiohttp

        last = "нет ответа"
        for attempt in range(1, attempts + 1):
            try:
                server = await self.call(get_server, peer_id=peer_id, **server_params)
                form = aiohttp.FormData()
                form.add_field(field, data, filename=filename, content_type=content_type)
                session = await self._http()
                async with session.post(server["upload_url"], data=form) as resp:
                    body = await resp.text()
                    status = resp.status
                try:
                    uploaded = json.loads(body)
                except ValueError:
                    last = f"сервер загрузки ответил HTTP {status}, не JSON: {body[:150]!r}"
                else:
                    missing = [k for k in required if not uploaded.get(k) or uploaded.get(k) == "[]"]
                    if not missing:
                        return uploaded
                    last = f"HTTP {status}, в ответе нет {missing}: {body[:150]!r} (файл не принят?)"
            except VkApiError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last = f"сеть: {type(exc).__name__}: {exc}"
            if attempt < attempts:
                logger.warning("ВК: загрузка файла, попытка %d из %d не удалась (%s)", attempt, attempts, last)
                await asyncio.sleep(pause * attempt)
        raise RuntimeError(f"не удалось загрузить файл в ВК после {attempts} попыток: {last}")

    async def upload_message_photo(self, peer_id: int, path) -> str:
        """Загружает картинку для сообщений и возвращает вложение вида photo-1_2_key."""
        with open(path, "rb") as f:
            data = f.read()
        uploaded = await self._upload_file(peer_id, "photos.getMessagesUploadServer", {}, "photo", data,
                                           "catalog.jpg", "image/jpeg", ("photo", "server", "hash"))
        saved = await self.call("photos.saveMessagesPhoto", photo=uploaded["photo"],
                                server=uploaded["server"], hash=uploaded["hash"])
        photo = saved[0]
        key = f"_{photo['access_key']}" if photo.get("access_key") else ""
        return f"photo{photo['owner_id']}_{photo['id']}{key}"

    async def upload_message_doc(self, peer_id: int, path, title: str | None = None) -> str:
        """Загружает файл (например, таблицу Excel) для отправки в сообщении и возвращает вложение вида doc1_2."""
        from pathlib import Path

        path = Path(path)
        uploaded = await self._upload_file(peer_id, "docs.getMessagesUploadServer", {"type": "doc"}, "file",
                                           path.read_bytes(), path.name, "application/octet-stream", ("file",))
        saved = await self.call("docs.save", file=uploaded["file"], title=title or path.name)
        doc = saved["doc"] if isinstance(saved, dict) and "doc" in saved else saved[0]
        return f"doc{doc['owner_id']}_{doc['id']}"

    async def answer_event(self, event_id: str, user_id: int, peer_id: int, text: str | None = None,
                           link: str | None = None):
        if link:
            data = {"type": "open_link", "link": link}
        else:
            data = {"type": "show_snackbar", "text": text[:90]} if text else None
        return await self.call("messages.sendMessageEventAnswer", event_id=event_id, user_id=user_id,
                               peer_id=peer_id, event_data=data)

    async def listen(self) -> AsyncIterator[dict]:
        """Бесконечный поток событий сообщества (Bots Long Poll API)."""
        server = None
        last_ts = None   # откуда продолжать чтение: пока сеть была недоступна, события ждут у ВКонтакте, и мы их заберём
        errors = 0
        while True:
            try:
                if server is None:
                    server = await self.call("groups.getLongPollServer", group_id=self.group_id)
                    if last_ts is not None:
                        server["ts"] = last_ts
                session = await self._http()
                params = {"act": "a_check", "key": server["key"], "ts": server["ts"], "wait": 25}
                async with session.get(server["server"], params=params) as resp:
                    data = await resp.json(content_type=None)
                errors = 0
                if "failed" in data:
                    failed = data["failed"]
                    if failed == 1:      # история устарела: ВК даёт верный ts
                        server["ts"] = last_ts = data["ts"]
                    elif failed == 2:    # ключ истёк: берём новый ключ, но читаем с прежнего места
                        last_ts = server["ts"]
                        server = None
                    else:                # 3: информация потеряна — начинаем с нуля
                        last_ts = None
                        server = None
                    continue
                server["ts"] = last_ts = data["ts"]
                for update in data.get("updates", []):
                    yield update
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — сеть пропала и т.п.: ждём и пробуем снова
                errors += 1
                logger.warning("VK Long Poll: %s — повтор через 5 с", exc)
                if errors >= 3:   # возможно, сервер сменился: берём новый, но читаем с прежнего места (last_ts)
                    server = None
                await asyncio.sleep(5)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
