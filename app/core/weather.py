"""Погода: разбор ответа Open-Meteo, тексты сводок и поиск важных изменений.

Только стандартная библиотека — модуль целиком покрыт тестами.
Open-Meteo бесплатный и без ключа: https://open-meteo.com
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from html import escape

# Коды погоды WMO → (описание, эмодзи, «тяжесть» для выбора главного явления дня)
WMO: dict[int, tuple[str, str, int]] = {
    0: ("ясно", "☀️", 0),
    1: ("преимущественно ясно", "🌤", 1),
    2: ("переменная облачность", "⛅️", 2),
    3: ("пасмурно", "☁️", 3),
    45: ("туман", "🌫", 4),
    48: ("туман с изморозью", "🌫", 5),
    51: ("слабая морось", "🌦", 6),
    53: ("морось", "🌦", 7),
    55: ("сильная морось", "🌧", 8),
    56: ("ледяная морось", "🧊", 12),
    57: ("сильная ледяная морось", "🧊", 13),
    61: ("небольшой дождь", "🌦", 9),
    63: ("дождь", "🌧", 10),
    65: ("сильный дождь", "🌧", 11),
    66: ("ледяной дождь", "🧊", 14),
    67: ("сильный ледяной дождь", "🧊", 15),
    71: ("небольшой снег", "🌨", 9),
    73: ("снег", "❄️", 10),
    75: ("сильный снег", "❄️", 12),
    77: ("снежные зёрна", "🌨", 9),
    80: ("кратковременный дождь", "🌦", 9),
    81: ("ливень", "🌧", 11),
    82: ("сильный ливень", "⛈", 13),
    85: ("снегопад", "🌨", 10),
    86: ("сильный снегопад", "❄️", 12),
    95: ("гроза", "⛈", 14),
    96: ("гроза с градом", "⛈", 15),
    99: ("сильная гроза с градом", "⛈", 16),
}

RAIN_CODES = {51, 53, 55, 61, 63, 65, 80, 81, 82}
SNOW_CODES = {71, 73, 75, 77, 85, 86}
ICE_CODES = {56, 57, 66, 67}
STORM_CODES = {95, 96, 99}

PERIODS = (("Утро", 6, 12), ("День", 12, 18), ("Вечер", 18, 24))

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря"]


def describe(code: int) -> tuple[str, str]:
    text, emoji, _ = WMO.get(code, ("без осадков", "🌡", 0))
    return text, emoji


def severity(code: int) -> int:
    return WMO.get(code, ("", "", 0))[2]


def ru_date(d: date) -> str:
    return f"{d.day} {MONTHS_RU[d.month - 1]}, {WEEKDAYS_RU[d.weekday()]}"


def fmt_temp(t: float) -> str:
    v = round(t)
    if v == 0:
        return "0°"
    return f"+{v}°" if v > 0 else f"−{abs(v)}°"


@dataclass
class Hour:
    time: datetime
    temp: float
    feels: float
    code: int
    precip_prob: int
    precip: float
    gust: float


@dataclass
class Current:
    temp: float
    feels: float
    code: int
    wind: float


@dataclass
class Forecast:
    hours: list[Hour]
    current: Current | None = None

    def hours_for(self, d: date, start: int = 0, end: int = 24) -> list[Hour]:
        return [h for h in self.hours if h.time.date() == d and start <= h.time.hour < end]


@dataclass
class DaySummary:
    day: str  # ISO-дата
    tmin: float
    tmax: float
    code: int
    precip_prob: int
    precip_sum: float
    gust_max: float
    first_rain_hour: int | None = None
    periods: dict[str, tuple[float, int]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DaySummary":
        data = dict(data)
        data["periods"] = {k: tuple(v) for k, v in data.get("periods", {}).items()}
        return cls(**data)


def _num(values: list, i: int, default: float = 0.0) -> float:
    try:
        v = values[i]
    except (IndexError, TypeError):
        return default
    return default if v is None else float(v)


def parse_open_meteo(payload: dict) -> Forecast:
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    hours = []
    for i, t in enumerate(times):
        hours.append(Hour(
            time=datetime.fromisoformat(t),
            temp=_num(hourly.get("temperature_2m"), i),
            feels=_num(hourly.get("apparent_temperature"), i),
            code=int(_num(hourly.get("weather_code"), i)),
            precip_prob=int(_num(hourly.get("precipitation_probability"), i)),
            precip=_num(hourly.get("precipitation"), i),
            gust=_num(hourly.get("wind_gusts_10m"), i),
        ))
    current = None
    cur = payload.get("current")
    if cur:
        current = Current(
            temp=float(cur.get("temperature_2m", 0) or 0),
            feels=float(cur.get("apparent_temperature", 0) or 0),
            code=int(cur.get("weather_code", 0) or 0),
            wind=float(cur.get("wind_speed_10m", 0) or 0),
        )
    return Forecast(hours=hours, current=current)


def _main_code(hours: list[Hour]) -> int:
    """Главное явление: самое «тяжёлое», но осадки учитываем, только если их реально ждут."""
    best = 0
    for h in hours:
        code = h.code
        if (code in RAIN_CODES or code in SNOW_CODES) and h.precip_prob < 40 and h.precip < 0.2:
            continue  # «возможна морось» с вероятностью 10% не делаем главным событием дня
        if severity(code) > severity(best):
            best = code
    if best == 0 and hours:
        # нет значимых явлений — берём облачность середины дня
        mid = [h for h in hours if 11 <= h.time.hour <= 16] or hours
        best = max((h.code for h in mid if h.code <= 3), default=0, key=severity)
    return best


def summarize_day(fc: Forecast, d: date, from_hour: int = 0) -> DaySummary | None:
    hours = fc.hours_for(d, from_hour, 24)
    if not hours:
        return None
    periods: dict[str, tuple[float, int]] = {}
    for title, start, end in PERIODS:
        chunk = [h for h in hours if start <= h.time.hour < end]
        if chunk:
            avg = sum(h.temp for h in chunk) / len(chunk)
            periods[title] = (round(avg, 1), _main_code(chunk))
    rainy = [h for h in hours if h.precip_prob >= 50 and
             (h.code in RAIN_CODES | SNOW_CODES | ICE_CODES | STORM_CODES or h.precip >= 0.3)]
    return DaySummary(
        day=d.isoformat(),
        tmin=min(h.temp for h in hours),
        tmax=max(h.temp for h in hours),
        code=_main_code(hours),
        precip_prob=max(h.precip_prob for h in hours),
        precip_sum=round(sum(h.precip for h in hours), 1),
        gust_max=max(h.gust for h in hours),
        first_rain_hour=rainy[0].time.hour if rainy else None,
        periods=periods,
    )


def advice(s: DaySummary) -> list[str]:
    tips = []
    if s.code in STORM_CODES:
        tips.append("⚡️ Возможна гроза — лучше переждать под крышей.")
    if s.code in ICE_CODES or (s.precip_sum >= 0.5 and s.tmin <= 1 and s.tmax >= -2):
        tips.append("🧊 Возможна гололедица — осторожнее на дорогах и тротуарах.")
    if s.precip_prob >= 60 and s.code in RAIN_CODES | STORM_CODES:
        tips.append("☂️ Возьмите зонт.")
    elif s.precip_prob >= 60 and s.code in SNOW_CODES:
        tips.append("❄️ Ожидается снег — заложите время на дорогу.")
    if s.gust_max >= 15:
        tips.append(f"💨 Сильный ветер, порывы до {round(s.gust_max)} м/с.")
    if s.tmax <= -15:
        tips.append("🥶 Сильный мороз — одевайтесь максимально тепло.")
    elif s.tmax >= 30:
        tips.append("🥵 Жара — пейте больше воды.")
    if s.tmax - s.tmin >= 10:
        tips.append("🧥 Большой перепад температур за день — одевайтесь слоями.")
    return tips


def _day_block(s: DaySummary) -> list[str]:
    text, emoji = describe(s.code)
    lines = [f"{emoji} <b>{fmt_temp(s.tmin)}…{fmt_temp(s.tmax)}</b>, {text}"]
    for title, (temp, code) in s.periods.items():
        p_text, p_emoji = describe(code)
        lines.append(f"• {title}: {fmt_temp(temp)} {p_emoji} {p_text}")
    extra = []
    if s.precip_prob >= 30:
        extra.append(f"осадки до {s.precip_prob}%")
    if s.gust_max >= 10:
        extra.append(f"порывы до {round(s.gust_max)} м/с")
    if extra:
        lines.append("• " + ", ".join(extra))
    return lines


def _region_line(name: str, s: DaySummary) -> str:
    text, emoji = describe(s.code)
    rain = f", осадки {s.precip_prob}%" if s.precip_prob >= 40 else ""
    return f"{emoji} {escape(name)}: {fmt_temp(s.tmin)}…{fmt_temp(s.tmax)}, {text}{rain}"


def format_report(title: str, main_name: str, main: DaySummary,
                  region: list[tuple[str, DaySummary]], current: Current | None = None) -> str:
    lines = [title, ""]
    if current is not None:
        c_text, c_emoji = describe(current.code)
        lines.append(f"Сейчас ({escape(main_name)}): {c_emoji} <b>{fmt_temp(current.temp)}</b> "
                     f"(ощущается как {fmt_temp(current.feels)}), {c_text}")
        lines.append("")
    lines.append(f"🏙 <b>{escape(main_name)}</b>")
    lines.extend(_day_block(main))
    if region:
        lines.append("")
        lines.append("🌲 <b>Подмосковье</b>")
        lines.extend(_region_line(name, s) for name, s in region)
    tips = advice(main)
    if tips:
        lines.append("")
        lines.extend(tips)
    return "\n".join(lines)


def morning_title(d: date) -> str:
    return f"🌅 <b>Доброе утро! Погода на сегодня, {ru_date(d)}</b>"


def evening_title(d: date) -> str:
    return f"🌙 <b>Погода на завтра, {ru_date(d)}</b>"


def detect_changes(old: DaySummary, new: DaySummary, now_hour: int) -> list[tuple[str, str]]:
    """Сравнивает утренний прогноз с актуальным на остаток дня.

    Возвращает список (ключ, текст). Ключ нужен, чтобы не присылать одно и то же дважды.
    """
    changes: list[tuple[str, str]] = []

    def precip_kind(code: int) -> str:
        if code in STORM_CODES:
            return "гроза"
        if code in ICE_CODES:
            return "ледяной дождь"
        if code in SNOW_CODES:
            return "снег"
        return "дождь"

    became_wet = new.first_rain_hour is not None and new.precip_prob >= 60 and (
        old.first_rain_hour is None or old.precip_prob < 40)
    if became_wet and new.first_rain_hour >= now_hour:
        kind = precip_kind(new.code)
        when = "уже скоро" if new.first_rain_hour - now_hour <= 1 else f"примерно с {new.first_rain_hour}:00"
        changes.append((f"precip:{kind}", f"☔️ Прогноз изменился: {kind} {when} "
                                         f"(вероятность {new.precip_prob}%)."))

    if new.code in STORM_CODES and old.code not in STORM_CODES:
        changes.append(("storm", "⚡️ Добавилась гроза — будьте осторожны на улице."))

    if new.gust_max >= 15 and old.gust_max < 12:
        changes.append(("wind", f"💨 Усиливается ветер: порывы до {round(new.gust_max)} м/с."))

    if old.tmax - new.tmax >= 4:
        changes.append(("colder", f"🥶 Будет холоднее, чем обещали утром: "
                                  f"максимум {fmt_temp(new.tmax)} вместо {fmt_temp(old.tmax)}."))
    elif new.tmax - old.tmax >= 4:
        changes.append(("warmer", f"🌡 Будет теплее, чем обещали утром: "
                                  f"до {fmt_temp(new.tmax)} вместо {fmt_temp(old.tmax)}."))

    if (new.code in ICE_CODES or (new.precip_sum >= 0.5 and new.tmin <= 1 and new.tmax >= -2)) and not (
            old.code in ICE_CODES or (old.precip_sum >= 0.5 and old.tmin <= 1 and old.tmax >= -2)):
        changes.append(("ice", "🧊 Появилась угроза гололедицы — осторожнее на дорогах."))
    return changes


def format_changes(main_name: str, changes: list[tuple[str, str]]) -> str:
    lines = [f"🔔 <b>Погода меняется — {escape(main_name)}</b>", ""]
    lines.extend(text for _, text in changes)
    return "\n".join(lines)


def day_snapshot(fc: Forecast, d: date) -> list[list]:
    """Почасовой прогноз на день в компактном виде — чтобы сохранить в БД."""
    return [[h.time.isoformat(), h.temp, h.feels, h.code, h.precip_prob, h.precip, h.gust]
            for h in fc.hours_for(d)]


def forecast_from_snapshot(rows: list[list]) -> Forecast:
    return Forecast(hours=[Hour(datetime.fromisoformat(t), temp, feels, int(code), int(prob), precip, gust)
                           for t, temp, feels, code, prob, precip, gust in rows])
