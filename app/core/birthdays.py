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


def order_on_birthday(b: Birthday, created_at: str) -> bool:
    """Заказ сделан в день рождения покупателя? created_at — «31.10.2026 15:20»."""
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.", created_at or "")
    return bool(m) and (int(m.group(1)), int(m.group(2))) == (b.day, b.month)


# Месяц по первым буквам: «янв», «января», «январь», «сент.» → 1…12. «мая/май» — отдельно (рядом «мар»).
_MONTH_STEMS = (("янв", 1), ("фев", 2), ("мар", 3), ("апр", 4), ("июн", 6), ("июл", 7),
                ("авг", 8), ("сен", 9), ("окт", 10), ("ноя", 11), ("дек", 12))
_DAY = r"(\d{1,2})(?:-?(?:го|ого|ое|е|й|ый))?"      # «4», «04», «4-го», «4го»
_YEAR = r"(?:\s*(?:\d{4}|\d{2})\s*(?:г\.?|года?)?)?"  # необязательный год: его не храним
_NUMERIC_RE = re.compile(r"^(\d{1,2})\s*[.,/\-\s]\s*(\d{1,2})(?:\s*[.,/\-\s]\s*(?:\d{4}|\d{2}))?\.?$")
_NAMED_RE = re.compile(rf"^{_DAY}\s*[.\-]?\s*([а-яё]{{3,}})\.?{_YEAR}$")
_MONTH_FIRST_RE = re.compile(r"^([а-яё]{3,})\.?\s*(\d{1,2})$")
_DIGITS_RE = re.compile(r"^(\d{2})(\d{2})(?:\d{2}|\d{4})?$")   # 0401, 040190, 04011990


def month_from_word(word: str) -> int | None:
    w = word.lower().replace("ё", "е").strip(".,")
    if w in {"май", "мая", "мае", "маю", "маем"}:
        return 5
    for stem, number in _MONTH_STEMS:
        if w.startswith(stem):
            return number
    return None


def _valid(day: int, month: int) -> tuple[int, int] | None:
    try:
        date(2024, month, day)  # високосный год, чтобы 29.02 проходило
    except ValueError:
        return None
    return day, month


def _parse_exact(text: str) -> tuple[int, int] | None:
    s = text.strip().lower().replace("ё", "е").rstrip(",;:!")
    if not s:
        return None
    if m := _NUMERIC_RE.match(s):
        return _valid(int(m.group(1)), int(m.group(2)))
    if m := _NAMED_RE.match(s):
        month = month_from_word(m.group(2))
        return _valid(int(m.group(1)), month) if month else None
    if m := _MONTH_FIRST_RE.match(s):
        month = month_from_word(m.group(1))
        return _valid(int(m.group(2)), month) if month else None
    if m := _DIGITS_RE.match(s):
        return _valid(int(m.group(1)), int(m.group(2)))
    return None


def parse_date_prefix(text: str) -> tuple[int, int, str] | None:
    """Дата в начале строки + остаток: «4 января Иван Петров» → (4, 1, «Иван Петров»)."""
    tokens = text.split()
    for count in range(min(5, len(tokens)), 0, -1):          # самая длинная подходящая дата («4 января 1990 года»)
        parsed = _parse_exact(" ".join(tokens[:count]))
        if parsed:
            return parsed[0], parsed[1], " ".join(tokens[count:])
    return None


def parse_day_month(text: str) -> tuple[int, int] | None:
    """День и месяц в любом написании → (день, месяц). Год, если есть, не нужен.

    «04.01», «4.1», «04/01», «4-01», «4 01», «04 января», «4 янв», «4янв.», «4-го января», «январь 4»,
    «04.01.1990», «4 января 1990 года». Проверяет, что такая дата бывает (29.02 разрешено).
    Находит дату и внутри фразы: «мой день рождения 4 января»."""
    tokens = text.split()
    for start in range(len(tokens)):
        for count in range(min(5, len(tokens) - start), 0, -1):
            parsed = _parse_exact(" ".join(tokens[start:start + count]))
            if parsed:
                return parsed
    return None


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


DEFAULT_BIRTHDAY_OFFER = ("🎁 Подскажите дату вашего дня рождения — если закажете у нас в этот день, "
                          "добавим к заказу вкусный подарок и поздравим вас!\n\n"
                          "Напишите день и месяц, например: 15.04 или 15 апреля")

FALLBACK_GREETINGS = [
    "🎉 Сегодня день рождения у {names}!\n\nПусть этот год будет щедрым на радость, тёплых людей "
    "рядом и мечты, которые сбываются одна за другой. Здоровья, сил и вдохновения! 🎂",
    "🥳 Друзья, у нас праздник — {names} сегодня именинник(ца)!\n\nЖелаем, чтобы каждый день "
    "приносил поводы для улыбки, а все задуманное получалось легко и с удовольствием. С днём рождения! 🎈",
    "🎂 С днём рождения, {names}!\n\nПусть впереди будет год, полный приятных сюрпризов, "
    "интересных встреч и уверенности в себе. Пусть всё, что дорого, будет рядом! ✨",
    "🎈 {names} сегодня отмечает день рождения!\n\nПоздравляем от всей души! Пусть год будет добрым, "
    "тёплым и вкусным: с любимыми людьми рядом, с радостью в каждом дне и с мечтами, которые обязательно сбудутся. 🌿",
    "🥂 С днём рождения, {names}!\n\nЖелаем лёгкости, сил и вдохновения, чтобы всё задуманное получалось. "
    "Пусть рядом всегда будут те, с кем тепло и хорошо, а за столом — только настоящий вкус! 🎂",
    "✨ Сегодня особенный день — день рождения у {names}!\n\nПусть он будет наполнен улыбками, добрыми словами "
    "и приятными сюрпризами, а наступающий год подарит радость, уют и много поводов для праздника. С днём рождения! 🎉",
]

# Личное поздравление покупателю — уходит в личные сообщения сообщества. {name} всегда в начале предложения.
PERSONAL_GREETINGS = [
    "🎂 {name}, с днём рождения! 🎉\n\n"
    "Вся команда GASTRODOM от души поздравляет тебя! Желаем тёплых встреч, красивых моментов и того самого "
    "настоящего вкуса, который делает любой праздник особенным.\n\n"
    "🎁 А ещё у нас для тебя приятная новость: закажи сегодня, и мы добавим к заказу вкусный подарок!",
    "🥳 {name}, сегодня твой день! С днём рождения!\n\n"
    "Пусть этот год принесёт много радости, уюта и поводов собраться за хорошим столом с близкими людьми. "
    "Мы очень рады, что ты с нами. 🌿\n\n"
    "🎁 В день рождения к твоему заказу в GASTRODOM мы добавим вкусный подарок. Загляни в каталог — будем рады!",
    "🐻 {name}, это мишка из GASTRODOM — поздравляем тебя с днём рождения! 🎈\n\n"
    "Желаем здоровья, лёгкости и вдохновения, чтобы всё задуманное получалось и приносило удовольствие.\n\n"
    "🎁 Если закажешь у нас сегодня, положим к заказу подарок от нас.",
    "🎉 {name}, с днём рождения!\n\n"
    "Пусть в этот день будет много тепла, улыбок и приятных сюрпризов, а впереди ждёт год, полный хороших "
    "событий. Спасибо, что выбираешь натуральные продукты и настоящий вкус! ❤️\n\n"
    "🎁 Сегодня к твоему заказу у нас — вкусный подарок.",
    "✨ {name}, поздравляем с днём рождения! 🎂\n\n"
    "Пусть этот день получится именно таким, о каком мечтается: с добрыми словами, любимыми людьми и красивым "
    "столом. Желаем радости и сил на самое важное.\n\n"
    "🎁 А мы можем сделать праздник ещё вкуснее: закажи сегодня, и к заказу добавим подарок!",
    "🌟 {name}, с днём рождения от всей души! 🥂\n\n"
    "Пусть жизнь радует приятными неожиданностями, а каждый день приносит поводы для улыбки. Мы благодарны, "
    "что ты с нами, и желаем тебе здоровья, тепла и уюта.\n\n"
    "🎁 Загляни к нам сегодня: к твоему заказу в этот день мы приложим вкусный подарок.",
]


def personal_name(b: Birthday) -> str:
    """Имя для личного поздравления: первое слово. Если имени нет («id123») — «Дорогой друг»."""
    first = (b.name or "").split()[0] if (b.name or "").split() else ""
    if not first or re.fullmatch(r"id\d+", first, re.IGNORECASE):
        return "Дорогой друг"
    return escape(first)


def personal_greeting(b: Birthday, rnd: random.Random | None = None, index: int | None = None) -> str:
    """Личное поздравление покупателю (с напоминанием про подарок к заказу). index — конкретный вариант."""
    text = PERSONAL_GREETINGS[index] if index is not None else (rnd or random).choice(PERSONAL_GREETINGS)
    return text.format(name=personal_name(b))


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
