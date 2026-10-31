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
        if value.startswith("#"):  # «KEY=   # подсказка» — значение пустое
            value = ""
        elif value and value[0] in "\"'" and value[-1:] == value[0]:
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


def _handles(name: str) -> tuple[str, ...]:
    """Список людей: числовые ID, @username или ссылки t.me/…, vk.ru/… через запятую.

    «https://t.me/artdin93, 123» → ("artdin93", "123")."""
    result = []
    for part in _str(name).replace(";", ",").split(","):
        part = part.strip().rstrip("/")
        for prefix in ("https://", "http://", "www.", "t.me/", "vk.ru/", "vk.com/", "m.vk.ru/", "m.vk.com/", "@"):
            if part.startswith(prefix):
                part = part[len(prefix):]
        if part:
            result.append(part)
    return tuple(result)


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
    catalog_image: Path | None = None   # картинка-прайс, которую бот показывает при «Каталог»
    faq_path: Path | None = None        # справка для нейросети: доставка, оплата, сроки

    llm_providers: list[LLMProvider] = field(default_factory=list)
    llm_daily_cap: int = 30
    llm_system_prompt: str = ""

    weather_enabled: bool = True
    weather_locations: list[Location] = field(default_factory=list)
    weather_morning: tuple[int, int] = (7, 0)
    weather_evening: tuple[int, int] = (21, 0)
    weather_evening_enabled: bool = False   # «погода на завтра» вечером — выключена по умолчанию
    weather_alerts_enabled: bool = True
    weather_alerts_from: int = 9
    weather_alerts_to: int = 21
    weather_alerts_every_min: int = 120

    birthday_time: tuple[int, int] = (10, 0)

    welcome_enabled: bool = True
    order_reminders_enabled: bool = True
    order_reminder_weekdays: tuple[int, ...] = (4,)  # 0=пн … 4=пт
    order_reminder_time: tuple[int, int] = (12, 0)

    # Кому приходят уведомления о заказах (и кто может менять их статус)
    order_notify_tg: tuple[str, ...] = ()
    order_notify_vk: tuple[str, ...] = ()

    # ВКонтакте
    vk_token: str = ""
    vk_group_id: int = 0
    vk_chat_peer_id: int = 0      # беседа, куда бот пишет погоду, поздравления, напоминания
    vk_admins: tuple[str, ...] = ()

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_providers)

    @property
    def tg_enabled(self) -> bool:
        return bool(self.bot_token)

    @property
    def vk_enabled(self) -> bool:
        return bool(self.vk_token and self.vk_group_id)

    @property
    def tg_managers(self) -> tuple[str, ...]:
        """Менеджеры заказов в Telegram. Не заданы — уведомления получает владелец."""
        return self.order_notify_tg or ((str(self.owner_id),) if self.owner_id else ())

    @property
    def vk_managers(self) -> tuple[str, ...]:
        return self.order_notify_vk or self.vk_admins


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
        catalog_image=_path("CATALOG_IMAGE", "config/catalog.jpg") if _str("CATALOG_IMAGE", "x") else None,
        faq_path=_path("FAQ_PATH", "config/faq.txt"),
        llm_providers=_providers(),
        llm_daily_cap=_int("LLM_DAILY_CAP", 30),
        llm_system_prompt=prompt,
        weather_enabled=_bool("WEATHER_ENABLED", True),
        weather_locations=parse_locations(_str("WEATHER_LOCATIONS") or DEFAULT_LOCATIONS),
        weather_morning=_time("WEATHER_MORNING_TIME", "07:00"),
        weather_evening=_time("WEATHER_EVENING_TIME", "21:00"),
        weather_evening_enabled=_bool("WEATHER_EVENING_ENABLED", False),
        weather_alerts_enabled=_bool("WEATHER_ALERTS_ENABLED", True),
        weather_alerts_from=_int("WEATHER_ALERTS_FROM_HOUR", 9),
        weather_alerts_to=_int("WEATHER_ALERTS_TO_HOUR", 21),
        weather_alerts_every_min=max(30, _int("WEATHER_ALERTS_EVERY_MIN", 120)),
        birthday_time=_time("BIRTHDAY_TIME", "10:00"),
        welcome_enabled=_bool("WELCOME_ENABLED", True),
        order_reminders_enabled=_bool("ORDER_REMINDERS_ENABLED", True),
        order_reminder_weekdays=_weekdays(_str("ORDER_REMINDER_WEEKDAYS"), (4,)),
        order_reminder_time=_time("ORDER_REMINDER_TIME", "12:00"),
        order_notify_tg=_handles("ORDER_NOTIFY_TG"),
        order_notify_vk=_handles("ORDER_NOTIFY_VK"),
        vk_token=_str("VK_TOKEN"),
        vk_group_id=int("".join(ch for ch in _str("VK_GROUP_ID") if ch.isdigit()) or 0),  # «club123» → 123
        vk_chat_peer_id=_int("VK_CHAT_PEER_ID"),
        vk_admins=_handles("VK_ADMINS"),
    )
