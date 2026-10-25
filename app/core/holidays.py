"""Праздники и мягкие напоминания о заказах."""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.core.birthdays import days_word, next_occurrence


@dataclass(frozen=True)
class Holiday:
    day: int
    month: int
    name: str
    remind_days_before: tuple[int, ...] = (7, 2)


def load_holidays(path: Path) -> list[Holiday]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    result = []
    for item in data.get("holidays", []):
        day, month = (int(x) for x in item["date"].split("."))
        result.append(Holiday(day, month, item["name"], tuple(item.get("remind_days_before", (7, 2)))))
    return result


def holidays_to_remind(holidays: list[Holiday], today: date) -> list[tuple[Holiday, int]]:
    """Праздники, о которых сегодня пора напомнить: (праздник, сколько дней осталось)."""
    due = []
    for h in holidays:
        left = (next_occurrence(h.day, h.month, today) - today).days
        if left in h.remind_days_before:
            due.append((h, left))
    return due


HOLIDAY_TEXTS = [
    "🎁 {when} — {name}! Если хотите порадовать близких (или себя любимого), "
    "заказ можно оформить заранее — так точно всё успеем 💛",
    "✨ Совсем скоро {name} ({when_l}). Самое время подумать о подарках и угощениях — "
    "в каталоге есть из чего выбрать 😉",
]

WEEKLY_TEXTS = [
    "☕️ Напоминаем: у нас можно заказать что-нибудь приятное — себе, семье или друзьям. "
    "Каталог всегда под рукой 👇",
    "🛍 Хочется побаловать себя? Загляните в каталог — оформление заказа займёт минуту.",
    "💛 Маленькая радость — тоже повод. Заказы принимаем каждый день, каталог по кнопке ниже.",
]


def holiday_text(h: Holiday, left: int, rnd: random.Random | None = None) -> str:
    rnd = rnd or random
    when_l = days_word(left)
    return rnd.choice(HOLIDAY_TEXTS).format(when=when_l.capitalize(), when_l=when_l, name=h.name)


def weekly_text(rnd: random.Random | None = None) -> str:
    return (rnd or random).choice(WEEKLY_TEXTS)
