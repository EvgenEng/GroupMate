"""Статистика для админов: сводка за период и таблицы для выгрузки в Excel."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape

from app.core.orders import STATUSES, OrderView, fmt_qty, money

DATE_FORMAT = "%d.%m.%Y %H:%M"
PLATFORMS = {"vk": "ВКонтакте", "tg": "Telegram"}


def order_dt(o: OrderView) -> datetime | None:
    try:
        return datetime.strptime(o.created_at, DATE_FORMAT)
    except (TypeError, ValueError):
        return None


def order_sum(o: OrderView) -> float:
    """Сумма заказа: по факту после взвешивания, а если не взвешен — по заказу."""
    return o.final_total if o.final_total is not None else o.total


def order_items(o: OrderView) -> list[tuple[str, float, float, str]]:
    return o.final_items if o.final_items is not None else o.items


def parse_days(arg: str, default: int) -> int:
    """«7», «30 дней», «неделя», «месяц» → число дней (1…366)."""
    word = arg.strip().lower()
    if not word:
        return default
    names = {"день": 1, "сегодня": 1, "неделя": 7, "неделю": 7, "месяц": 30, "квартал": 90, "год": 365}
    if word in names:
        return names[word]
    digits = "".join(ch for ch in word.split()[0] if ch.isdigit())
    return min(max(int(digits), 1), 366) if digits else default


def _days_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    return "дня" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "дней"


@dataclass
class Stats:
    days: int
    since: datetime
    until: datetime
    orders: int = 0
    cancelled: int = 0
    revenue: float = 0.0
    weighed: int = 0
    buyers: int = 0
    new_buyers: int = 0
    top: list[tuple[str, float, str, float]] = field(default_factory=list)   # название, кол-во, ед., выручка
    by_status: dict[str, int] = field(default_factory=dict)
    rating_avg: float | None = None
    rating_count: int = 0
    joined: int = 0
    joined_ordered: int = 0

    @property
    def avg_check(self) -> float:
        return self.revenue / self.orders if self.orders else 0.0


def compute_stats(orders: list[OrderView], now: datetime, days: int, ratings: list[int] | None = None,
                  joined: list[tuple[float, int]] | None = None) -> Stats:
    """orders — все заказы за всё время (нужны, чтобы отличить новых покупателей от повторных).
    ratings — оценки за период. joined — [(время подписки, user_id)] подписчиков ВК за период."""
    until = now.replace(tzinfo=None)
    since = until - timedelta(days=days)
    stats = Stats(days=days, since=since, until=until)
    first_order: dict[tuple[str, int], datetime] = {}
    for o in orders:
        dt = order_dt(o)
        if o.status != "cancelled" and dt and ((o.platform, o.user_id) not in first_order
                                               or dt < first_order[(o.platform, o.user_id)]):
            first_order[(o.platform, o.user_id)] = dt
    buyers: set[tuple[str, int]] = set()
    products: dict[str, list] = {}
    for o in orders:
        dt = order_dt(o)
        if not dt or not (since <= dt <= until):
            continue
        stats.by_status[o.status] = stats.by_status.get(o.status, 0) + 1
        if o.status == "cancelled":
            stats.cancelled += 1
            continue
        stats.orders += 1
        stats.revenue += order_sum(o)
        stats.weighed += o.final_items is not None
        buyers.add((o.platform, o.user_id))
        for name, qty, price, unit in order_items(o):
            row = products.setdefault(name, [0.0, unit, 0.0])
            row[0] += qty
            row[2] += qty * price
    stats.buyers = len(buyers)
    stats.new_buyers = sum(1 for key in buyers if first_order.get(key, since) >= since)
    stats.top = sorted(((n, q, u, r) for n, (q, u, r) in products.items()), key=lambda x: -x[3])[:5]
    if ratings:
        stats.rating_count = len(ratings)
        stats.rating_avg = sum(ratings) / len(ratings)
    if joined:
        stats.joined = len(joined)
        stats.joined_ordered = sum(1 for _ts, uid in joined if ("vk", uid) in first_order)
    return stats


def format_stats(s: Stats, currency: str) -> str:
    rows = [f"📊 <b>Статистика за {s.days} {_days_word(s.days)}</b> "
            f"({s.since:%d.%m} — {s.until:%d.%m.%Y})", ""]
    if not s.orders and not s.cancelled:
        rows.append("За этот период заказов не было.")
    else:
        rows.append(f"🧾 Заказов: <b>{s.orders}</b>" + (f" (отменено: {s.cancelled})" if s.cancelled else ""))
        rows.append(f"💰 Выручка: <b>{money(s.revenue, currency)}</b> · средний чек: {money(s.avg_check, currency)}")
        if s.weighed:
            rows.append(f"⚖️ Из них взвешено: {s.weighed} (в выручке — суммы по факту)")
        rows.append(f"👥 Покупателей: <b>{s.buyers}</b> (новых: {s.new_buyers}, повторных: {s.buyers - s.new_buyers})")
    if s.joined:
        rows.append(f"🔔 Новых подписчиков: <b>{s.joined}</b>, из них уже заказали: {s.joined_ordered}")
    if s.rating_count:
        rows.append(f"⭐ Оценки покупателей: <b>{s.rating_avg:.1f}</b> из 5 ({s.rating_count})".replace(".", ","))
    if s.top:
        rows += ["", "🏆 <b>Что берут чаще (по выручке)</b>"]
        for i, (name, qty, unit, revenue) in enumerate(s.top, start=1):
            rows.append(f"{i}. {escape(name)} — {fmt_qty(qty)} {escape(unit)} · {money(revenue, currency)}")
    if s.by_status:
        rows += ["", "Статусы: " + ", ".join(f"{STATUSES.get(k, k)} — {v}" for k, v in s.by_status.items())]
    rows += ["", "Подробнее в таблице: <code>/export</code>"]
    return "\n".join(rows)


def export_tables(orders: list[OrderView], now: datetime, days: int,
                  currency: str = "₽") -> dict[str, list[list]]:
    """Листы для Excel: «Заказы» (по строке на заказ) и «Позиции» (по строке на товар)."""
    since = now.replace(tzinfo=None) - timedelta(days=days)
    head_orders = ["№", "Дата", "Статус", "Покупатель", "Телефон", "Где заказал", "Комментарий", "Состав",
                   f"Заказано, {currency}", f"Итог по факту, {currency}"]
    head_items = ["№ заказа", "Дата", "Статус", "Товар", "Количество", "Ед.", f"Цена, {currency}",
                  f"Сумма, {currency}", "Взвешено"]
    order_rows, item_rows = [head_orders], [head_items]
    for o in sorted(orders, key=lambda x: x.id):
        dt = order_dt(o)
        if dt and dt < since:
            continue
        status = STATUSES.get(o.status, o.status)
        items = order_items(o)
        order_rows.append([o.id, o.created_at, status, o.customer_name, o.phone, PLATFORMS.get(o.platform, o.platform),
                           o.comment, "\n".join(f"{n} — {fmt_qty(q)} {u}" for n, q, _p, u in items),
                           round(o.total, 2), round(o.final_total, 2) if o.final_total is not None else None])
        for name, qty, price, unit in items:
            item_rows.append([o.id, o.created_at, status, name, qty, unit, price, round(qty * price, 2),
                              "да" if o.final_items is not None else "нет"])
    return {"Заказы": order_rows, "Позиции": item_rows}
