"""«Сторож»: раз в несколько минут бот открывает адрес сервиса мониторинга.

Пока пинги идут, сервис считает бота живым. Пропали — он сам напишет вам (почта, Telegram…).
Адрес (HEALTHCHECK_URL) в журнал не пишется: он содержит секретный ключ.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


async def _http_get(url: str) -> int:
    import aiohttp   # импорт здесь: без «сторожа» библиотека для этого модуля не нужна

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.get(url) as resp:
            return resp.status


async def ping(url: str, fetch: Callable[[str], Awaitable[int]] = _http_get) -> bool:
    try:
        return await fetch(url) < 400
    except Exception as exc:  # noqa: BLE001 — нет сети и т.п.: это не повод ронять бота
        logger.debug("Сторож: пинг не прошёл (%s)", type(exc).__name__)
        return False


async def run_heartbeat(url: str, interval_min: int,
                        fetch: Callable[[str], Awaitable[int]] = _http_get,
                        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Бесконечный цикл. Пишет в журнал только смену состояния: «пинги пропали» и «пинги снова идут»."""
    failing = False
    while True:
        ok = await ping(url, fetch)
        if not ok and not failing:
            logger.warning("Сторож: пинг не проходит (нет сети или неверный адрес) — повторю через %d мин", interval_min)
        elif ok and failing:
            logger.info("Сторож: пинги снова идут")
        failing = not ok
        await sleep(interval_min * 60)
