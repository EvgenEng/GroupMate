"""Общие объекты бота: настройки, база, нейросеть, погода, каталог.

Объект кладётся в диспетчер (dp["ctx"]), и aiogram сам передаёт его в хендлеры
аргументом `ctx`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.core.holidays import Holiday
from app.core.orders import Catalog
from app.services.db import Database
from app.services.llm import LLMClient
from app.services.weather import WeatherService


@dataclass
class AppContext:
    settings: Settings
    db: Database
    llm: LLMClient
    weather: WeatherService
    catalog: Catalog
    holidays: list[Holiday]
    bot_username: str = ""

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.timezone))

    def today(self) -> date:
        return self.now().date()

    def is_owner(self, user_id: int | None) -> bool:
        return bool(user_id) and user_id == self.settings.owner_id

    def deep_link(self, payload: str) -> str:
        return f"https://t.me/{self.bot_username}?start={payload}"

    async def llm_allowed(self, user_id: int) -> bool:
        """Дневной лимит обращений к нейросети на человека. Владельца не ограничиваем."""
        if self.is_owner(user_id):
            return True
        return await self.db.usage_hit(self.today().isoformat(), user_id, self.settings.llm_daily_cap)
