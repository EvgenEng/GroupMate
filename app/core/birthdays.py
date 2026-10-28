"""Дни рождения: разбор дат, поиск ближайших и запасные поздравления."""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date
from html import escape

from app.core.weather import MONTHS_RU


@dataclass
class Birthday:
    id: int
    name: str
    day: int
    month: int
    user_id: int | None = None
    username: str | None = None
    platform: str = "tg"  # tg | vk — откуда человек

    def mention(self) -> str:
        """HTML-ссылка на человека. Во ВКонтакте превращается в упоминание [id|Имя]."""
        name = escape(self.name)
        if self.user_id and self.platform == "vk":
            return f'<a href="https://vk.ru/id{self.user_id}">{name}</a>'
        if self.user_id:
            return f'<a href="tg://user?id={self.user_id}">{name}</a>'
        if self.username:
            return f"{name} (@{escape(self.username)})"
        return name

    def date_text(self) -> str:
        return f"{self.day} {MONTHS_RU[self.month - 1]}"


_DATE_RE = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})$")


def parse_day_month(text: str) -> tuple[int, int] | None:
    """«05.03», «5.3», «5/3» → (5, 3). Проверяет, что дата существует (29.02 разрешено)."""
    m = _DATE_RE.match(text.strip())
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    try:
        date(2024, month, day)  # високосный год, чтобы 29.02 проходило
    except ValueError:
        return None
    return day, month


def next_occurrence(day: int, month: int, today: date) -> date:
    for year in (today.year, today.year + 1, today.year + 2):
        try:
            d = date(year, month, day)
        except ValueError:  # 29.02 в невисокосный год → 28.02
            d = date(year, 2, 28)
        if d >= today:
            return d
    raise ValueError("unreachable")


def is_today(b: Birthday, today: date) -> bool:
    return next_occurrence(b.day, b.month, today) == today


def upcoming(items: list[Birthday], today: date, limit: int = 5) -> list[tuple[Birthday, int]]:
    """Ближайшие дни рождения: (кто, через сколько дней)."""
    ranked = [(b, (next_occurrence(b.day, b.month, today) - today).days) for b in items]
    ranked.sort(key=lambda x: x[1])
    return ranked[:limit]


def days_word(n: int) -> str:
    if n == 0:
        return "сегодня"
    if n == 1:
        return "завтра"
    tail = n % 100
    if 11 <= tail <= 14:
        word = "дней"
    elif n % 10 == 1:
        word = "день"
    elif 2 <= n % 10 <= 4:
        word = "дня"
    else:
        word = "дней"
    return f"через {n} {word}"


DEFAULT_BIRTHDAY_OFFER = ("🎁 Подскажите дату вашего дня рождения — в этот день поздравим вас "
                          "и приготовим приятный бонус к заказу!\n\nНапишите день и месяц, например: 15.04")

FALLBACK_GREETINGS = [
    "🎉 Сегодня день рождения у {names}!\n\nПусть этот год будет щедрым на радость, тёплых людей "
    "рядом и мечты, которые сбываются одна за другой. Здоровья, сил и вдохновения! 🎂",
    "🥳 Друзья, у нас праздник — {names} сегодня именинник(ца)!\n\nЖелаем, чтобы каждый день "
    "приносил поводы для улыбки, а все задуманное получалось легко и с удовольствием. С днём рождения! 🎈",
    "🎂 С днём рождения, {names}!\n\nПусть впереди будет год, полный приятных сюрпризов, "
    "интересных встреч и уверенности в себе. Пусть всё, что дорого, будет рядом! ✨",
]


def fallback_greeting(people: list[Birthday], rnd: random.Random | None = None) -> str:
    rnd = rnd or random
    names = ", ".join(b.mention() for b in people)
    return rnd.choice(FALLBACK_GREETINGS).format(names=names)


def greeting_prompt(people: list[Birthday]) -> str:
    names = ", ".join(b.name for b in people)
    many = len(people) > 1
    return (
        f"Напиши тёплое, красивое и немного торжественное поздравление с днём рождения для "
        f"{'участников' if many else 'участника'} группы: {names}. "
        "4–6 предложений, по-русски, на «ты», без банальностей и пафоса, с 2–4 уместными эмодзи. "
        "Не придумывай факты о человеке (возраст, профессию, хобби). "
        "Имена напиши ровно так, как они даны. Без заголовка и подписи."
    )


def insert_mentions(text: str, people: list[Birthday]) -> str:
    """Экранирует текст LLM и превращает первые упоминания имён в кликабельные ссылки."""
    result = escape(text)
    for b in people:
        name = escape(b.name)
        if name in result:
            result = result.replace(name, b.mention(), 1)
        else:
            result = f"{b.mention()}, " + result
    return result


def parse_vk_bdate(bdate: str | None) -> tuple[int, int] | None:
    """Дата рождения из профиля ВК: «15.4» или «15.4.1990» (год может быть скрыт)."""
    if not bdate:
        return None
    parts = bdate.split(".")
    if len(parts) < 2:
        return None
    return parse_day_month(f"{parts[0]}.{parts[1]}")
