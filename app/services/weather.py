"""Загрузка прогноза из Open-Meteo (бесплатно, без ключа) с коротким кешем."""
from __future__ import annotations

import asyncio
import logging
import time

from app.config import Location
from app.core.weather import Forecast, parse_open_meteo

logger = logging.getLogger(__name__)

API_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY = "temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,wind_gusts_10m"
CURRENT = "temperature_2m,apparent_temperature,weather_code,wind_speed_10m"


class WeatherService:
    def __init__(self, locations: list[Location], timezone: str, cache_ttl: int = 600):
        self.locations = locations
        self.timezone = timezone
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, Forecast]] = {}

    @property
    def main(self) -> Location:
        return self.locations[0]

    @property
    def region(self) -> list[Location]:
        return self.locations[1:]

    async def _fetch(self, session, loc: Location) -> Forecast:
        params = {
            "latitude": loc.lat, "longitude": loc.lon, "hourly": HOURLY, "current": CURRENT,
            "timezone": self.timezone, "forecast_days": 3, "wind_speed_unit": "ms",
        }
        async with session.get(API_URL, params=params) as resp:
            resp.raise_for_status()
            return parse_open_meteo(await resp.json())

    async def get(self, loc: Location, fresh: bool = False) -> Forecast:
        import aiohttp

        cached = self._cache.get(loc.name)
        if cached and not fresh and time.monotonic() - cached[0] < self.cache_ttl:
            return cached[1]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            fc = await self._fetch(session, loc)
        self._cache[loc.name] = (time.monotonic(), fc)
        return fc

    async def get_all(self, fresh: bool = False) -> list[tuple[Location, Forecast | None]]:
        results = await asyncio.gather(*(self.get(loc, fresh) for loc in self.locations),
                                       return_exceptions=True)
        out = []
        for loc, res in zip(self.locations, results):
            if isinstance(res, Exception):
                logger.warning("Погода для %s не загрузилась: %s", loc.name, res)
                out.append((loc, None))
            else:
                out.append((loc, res))
        return out
