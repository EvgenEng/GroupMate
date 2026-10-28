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
    "🎁 {when} — {name}! Деликатесы готовим под заказ (3–7 дней), поэтому лучше оформить заранее — "
    "так всё точно успеем к празднику 💛",
    "✨ Совсем скоро {name} ({when_l}). Самое время подумать о праздничном столе и подарках — "
    "наборы деликатесов отлично подходят и для того, и для другого 😉",
]

WEEKLY_TEXTS = [
    "🥩 Напоминаем: свежие копчёные деликатесы можно заказать себе, семье или друзьям. "
    "Готовим под заказ — каталог по кнопке 👇",
    "🧺 Планируете выходные или застолье? Закажите заранее — изготовление занимает 3–7 дней. "
    "Каталог по кнопке ниже.",
    "💛 Маленькая радость — тоже повод. Побалуйте себя любимого: шейка, балык, рулеты и готовые наборы ждут вас 👇",
]


def holiday_text(h: Holiday, left: int, rnd: random.Random | None = None) -> str:
    rnd = rnd or random
    when_l = days_word(left)
    return rnd.choice(HOLIDAY_TEXTS).format(when=when_l.capitalize(), when_l=when_l, name=h.name)


def weekly_text(rnd: random.Random | None = None) -> str:
    return (rnd or random).choice(WEEKLY_TEXTS)
