"""Простой планировщик на asyncio: «каждый день в ЧЧ:ММ» и «каждые N минут».

Без сторонних библиотек. Время считается в часовом поясе бота (по умолчанию Москва).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

Job = Callable[[datetime], Awaitable[None]]


def next_daily_run(now: datetime, hour: int, minute: int, weekdays: tuple[int, ...] | None = None) -> datetime:
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while weekdays and candidate.weekday() not in weekdays:
        candidate += timedelta(days=1)
    return candidate


class Scheduler:
    def __init__(self, timezone: str):
        self.tz = ZoneInfo(timezone)
        self._tasks: list[asyncio.Task] = []
        self._specs: list[tuple[str, Callable[[], Awaitable[None]]]] = []

    def now(self) -> datetime:
        return datetime.now(self.tz)

    async def _safe(self, name: str, job: Job, at: datetime) -> None:
        try:
            await job(at)
        except Exception:  # noqa: BLE001 — одна упавшая задача не должна ронять бота
            logger.exception("Задача «%s» упала", name)

    def daily(self, name: str, at: tuple[int, int], job: Job, weekdays: tuple[int, ...] | None = None) -> None:
        async def loop() -> None:
            while True:
                run_at = next_daily_run(self.now(), at[0], at[1], weekdays)
                logger.info("«%s»: следующий запуск %s", name, run_at.strftime("%d.%m %H:%M"))
                await asyncio.sleep(max(1.0, (run_at - self.now()).total_seconds()))
                await self._safe(name, job, self.now())
        self._specs.append((name, loop))

    def every(self, name: str, minutes: int, job: Job,
              hours: tuple[int, int] | None = None, first_delay: int = 60) -> None:
        async def loop() -> None:
            await asyncio.sleep(first_delay)
            while True:
                now = self.now()
                if hours is None or hours[0] <= now.hour < hours[1]:
                    await self._safe(name, job, now)
                await asyncio.sleep(minutes * 60)
        self._specs.append((name, loop))

    def start(self) -> None:
        for name, loop in self._specs:
            self._tasks.append(asyncio.create_task(loop(), name=name))
        logger.info("Планировщик запущен: %s", ", ".join(n for n, _ in self._specs) or "нет задач")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
