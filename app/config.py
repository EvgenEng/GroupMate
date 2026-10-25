"""Настройки бота из переменных окружения / файла .env.

Без сторонних зависимостей: свой маленький парсер .env, чтобы бот
одинаково запускался и в Docker, и просто через `python main.py` на Windows.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Читает KEY=VALUE из файла. Уже заданные переменные окружения не перетирает."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] in "\"'" and value[-1:] == value[0]:
            value = value[1:-1]
        elif " #" in value:  # комментарий в конце строки
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int = 0) -> int:
    value = _str(name)
    try:
        return int(value) if value else default
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    value = _str(name).lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on", "да"}


def _time(name: str, default: str) -> tuple[int, int]:
    value = _str(name, default) or default
    hh, mm = value.split(":")
    return int(hh), int(mm)


@dataclass(frozen=True)
class Location:
    name: str
    lat: float
    lon: float


def parse_locations(raw: str) -> list[Location]:
    """Формат: «Москва:55.7558:37.6173;Дмитров (север):56.344:37.52»."""
    result: list[Location] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, lat, lon = chunk.rsplit(":", 2)
        result.append(Location(name.strip(), float(lat), float(lon)))
    return result


DEFAULT_LOCATIONS = (
    "Москва:55.7558:37.6173;"
    "Север МО (Дмитров):56.3440:37.5200;"
    "Юг МО (Серпухов):54.9130:37.4160;"
    "Запад МО (Можайск):55.5060:36.0240;"
    "Восток МО (Орехово-Зуево):55.8060:38.9800"
)


@dataclass(frozen=True)
class LLMProvider:
    url: str
    model: str
    api_key: str


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_id: int
    group_chat_id: int
    telegram_proxy: str
    timezone: str
    db_path: Path
    catalog_path: Path
    holidays_path: Path

    llm_providers: list[LLMProvider] = field(default_factory=list)
    llm_daily_cap: int = 30
    llm_system_prompt: str = ""

    weather_enabled: bool = True
    weather_locations: list[Location] = field(default_factory=list)
    weather_morning: tuple[int, int] = (7, 0)
    weather_evening: tuple[int, int] = (21, 0)
    weather_alerts_enabled: bool = True
    weather_alerts_from: int = 9
    weather_alerts_to: int = 21
    weather_alerts_every_min: int = 120

    birthday_time: tuple[int, int] = (10, 0)

    welcome_enabled: bool = True
    order_reminders_enabled: bool = True
    order_reminder_weekdays: tuple[int, ...] = (4,)  # 0=пн … 4=пт
    order_reminder_time: tuple[int, int] = (12, 0)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_providers)


_WEEKDAYS = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
             "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _weekdays(raw: str, default: tuple[int, ...]) -> tuple[int, ...]:
    if not raw.strip():
        return default
    days = []
    for part in raw.replace(" ", "").lower().split(","):
        if part in _WEEKDAYS:
            days.append(_WEEKDAYS[part])
        elif part.isdigit() and 0 <= int(part) <= 6:
            days.append(int(part))
    return tuple(sorted(set(days))) or default


def _providers() -> list[LLMProvider]:
    providers = []
    for prefix in ("LLM", "LLM_FALLBACK"):
        key = _str(f"{prefix}_API_KEY")
        url = _str(f"{prefix}_API_URL")
        model = _str(f"{prefix}_MODEL")
        if key and url and model:
            providers.append(LLMProvider(url=url, model=model, api_key=key))
    return providers


def _path(name: str, default: str) -> Path:
    p = Path(_str(name, default) or default)
    return p if p.is_absolute() else BASE_DIR / p


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    prompt_file = _path("LLM_SYSTEM_PROMPT_FILE", "config/prompt.txt")
    prompt = prompt_file.read_text(encoding="utf-8").strip() if prompt_file.exists() else ""
    return Settings(
        bot_token=_str("BOT_TOKEN"),
        owner_id=_int("OWNER_ID"),
        group_chat_id=_int("GROUP_CHAT_ID"),
        telegram_proxy=_str("TELEGRAM_PROXY"),
        timezone=_str("TIMEZONE", "Europe/Moscow") or "Europe/Moscow",
        db_path=_path("DB_PATH", "data/bot.db"),
        catalog_path=_path("CATALOG_PATH", "config/catalog.json"),
        holidays_path=_path("HOLIDAYS_PATH", "config/holidays.json"),
        llm_providers=_providers(),
        llm_daily_cap=_int("LLM_DAILY_CAP", 30),
        llm_system_prompt=prompt,
        weather_enabled=_bool("WEATHER_ENABLED", True),
        weather_locations=parse_locations(_str("WEATHER_LOCATIONS") or DEFAULT_LOCATIONS),
        weather_morning=_time("WEATHER_MORNING_TIME", "07:00"),
        weather_evening=_time("WEATHER_EVENING_TIME", "21:00"),
        weather_alerts_enabled=_bool("WEATHER_ALERTS_ENABLED", True),
        weather_alerts_from=_int("WEATHER_ALERTS_FROM_HOUR", 9),
        weather_alerts_to=_int("WEATHER_ALERTS_TO_HOUR", 21),
        weather_alerts_every_min=max(30, _int("WEATHER_ALERTS_EVERY_MIN", 120)),
        birthday_time=_time("BIRTHDAY_TIME", "10:00"),
        welcome_enabled=_bool("WELCOME_ENABLED", True),
        order_reminders_enabled=_bool("ORDER_REMINDERS_ENABLED", True),
        order_reminder_weekdays=_weekdays(_str("ORDER_REMINDER_WEEKDAYS"), (4,)),
        order_reminder_time=_time("ORDER_REMINDER_TIME", "12:00"),
    )
