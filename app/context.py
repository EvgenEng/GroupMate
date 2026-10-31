"""Общие объекты бота: настройки, база, нейросеть, погода, каталог, заказы, рассылки.

Один объект на оба мессенджера. В Telegram он лежит в диспетчере (dp["ctx"]), и aiogram
сам передаёт его в хендлеры аргументом `ctx`; ВК-бот получает его в конструкторе.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.core.holidays import Holiday
from app.core.knowledge import build_knowledge, load_faq
from app.core.orders import Catalog
from app.messenger import Messenger
from app.services.db import Database
from app.services.llm import LLMClient
from app.services.orders import OrderService
from app.services.weather import WeatherService


@dataclass
class AppContext:
    settings: Settings
    db: Database
    llm: LLMClient
    weather: WeatherService
    catalog: Catalog
    holidays: list[Holiday]
    orders: OrderService
    messenger: Messenger

    @property
    def bot_username(self) -> str:
        return self.messenger.tg_username

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.timezone))

    def today(self) -> date:
        return self.now().date()

    def is_owner(self, user_id: int | None) -> bool:
        """Владелец бота в Telegram."""
        return bool(user_id) and user_id == self.settings.owner_id

    def deep_link(self, payload: str) -> str:
        return f"https://t.me/{self.bot_username}?start={payload}"

    async def llm_allowed(self, platform: str, user_id: int, privileged: bool = False) -> bool:
        """Дневной лимит обращений к нейросети на человека. Владельца и админов не ограничиваем."""
        if privileged:
            return True
        return await self.db.usage_hit(self.today().isoformat(), platform, user_id, self.settings.llm_daily_cap)


def build_context(settings: Settings) -> AppContext:
    from app.core.holidays import load_holidays

    db = Database(settings.db_path)
    catalog = Catalog.load(settings.catalog_path)
    llm = LLMClient(settings.llm_providers, settings.llm_system_prompt)
    llm.knowledge = build_knowledge(catalog, load_faq(settings.faq_path or settings.catalog_path.with_name("faq.txt")))
    return AppContext(
        settings=settings,
        db=db,
        llm=llm,
        weather=WeatherService(settings.weather_locations, settings.timezone),
        catalog=catalog,
        holidays=load_holidays(settings.holidays_path),
        orders=OrderService(db, catalog),
        messenger=Messenger(settings, db, catalog),
    )
