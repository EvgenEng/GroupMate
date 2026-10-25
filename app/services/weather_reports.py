"""Готовые тексты погоды: утро, вечер, «сейчас» и оповещения об изменениях."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.core import weather as w
from app.services.db import Database
from app.services.weather import WeatherService

SNAPSHOT_KEY = "weather:snapshot:{day}"
ALERTS_KEY = "weather:alerts:{day}"


async def report_for_day(ws: WeatherService, day: date, title: str, with_current: bool) -> str | None:
    data = await ws.get_all()
    main_loc, main_fc = data[0]
    if main_fc is None:
        return None
    main = w.summarize_day(main_fc, day)
    if main is None:
        return None
    region = []
    for loc, fc in data[1:]:
        if fc is not None and (s := w.summarize_day(fc, day)):
            region.append((loc.name, s))
    current = main_fc.current if with_current else None
    return w.format_report(title, main_loc.name, main, region, current)


async def morning_report(ws: WeatherService, db: Database, today: date) -> str | None:
    text = await report_for_day(ws, today, w.morning_title(today), with_current=True)
    # Запоминаем утренний прогноз — с ним потом сравниваем, изменилась ли погода.
    fc = await ws.get(ws.main)
    await db.kv_set(SNAPSHOT_KEY.format(day=today.isoformat()), w.day_snapshot(fc, today))
    return text


async def evening_report(ws: WeatherService, today: date) -> str | None:
    tomorrow = today + timedelta(days=1)
    return await report_for_day(ws, tomorrow, w.evening_title(tomorrow), with_current=False)


async def now_report(ws: WeatherService, today: date) -> str | None:
    return await report_for_day(ws, today, f"🌤 <b>Погода на сегодня, {w.ru_date(today)}</b>",
                                with_current=True)


async def check_changes(ws: WeatherService, db: Database, now: datetime) -> str | None:
    """Возвращает текст оповещения, если прогноз на остаток дня заметно изменился."""
    day = now.date().isoformat()
    snapshot = await db.kv_get(SNAPSHOT_KEY.format(day=day))
    fc = await ws.get(ws.main, fresh=True)
    if not snapshot:
        # Утренней сводки не было (бот запустили днём) — берём текущий прогноз за основу.
        await db.kv_set(SNAPSHOT_KEY.format(day=day), w.day_snapshot(fc, now.date()))
        return None
    # Сравниваем одинаковые отрезки: «что обещали утром на оставшиеся часы» vs «что обещают сейчас».
    old = w.summarize_day(w.forecast_from_snapshot(snapshot), now.date(), from_hour=now.hour)
    new = w.summarize_day(fc, now.date(), from_hour=now.hour)
    if old is None or new is None:
        return None
    sent: list[str] = await db.kv_get(ALERTS_KEY.format(day=day), [])
    fresh = [(key, text) for key, text in w.detect_changes(old, new, now.hour) if key not in sent]
    if not fresh:
        return None
    await db.kv_set(ALERTS_KEY.format(day=day), sent + [key for key, _ in fresh])
    return w.format_changes(ws.main.name, fresh)
