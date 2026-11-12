"""SQLite-хранилище: заказы, корзины, дни рождения, лимиты, служебные данные.

Один файл data/bot.db переживает перезапуски. Telegram и ВКонтакте пишут в одну базу,
у каждой записи есть поле platform (tg | vk) — так ID пользователей двух сетей не путаются.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from app.core.birthdays import Birthday
from app.core.contacts import normalize_phone
from app.core.orders import OrderView

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    platform TEXT NOT NULL DEFAULT 'tg',
    user_id INTEGER NOT NULL,
    first_name TEXT,
    username TEXT,
    last_seen TEXT,
    PRIMARY KEY (platform, user_id)
);
CREATE TABLE IF NOT EXISTS birthdays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    day INTEGER NOT NULL,
    month INTEGER NOT NULL,
    platform TEXT NOT NULL DEFAULT 'tg',
    user_id INTEGER,
    username TEXT,
    UNIQUE (platform, user_id)
);
CREATE TABLE IF NOT EXISTS cart (
    platform TEXT NOT NULL DEFAULT 'tg',
    user_id INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    qty REAL NOT NULL,
    updated_at REAL,
    PRIMARY KEY (platform, user_id, product_id)
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL DEFAULT 'tg',
    user_id INTEGER NOT NULL,
    customer_name TEXT,
    username TEXT,
    phone TEXT,
    comment TEXT,
    items_json TEXT NOT NULL,
    total REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL,
    final_json TEXT,
    final_total REAL,
    done_at REAL
);
CREATE TABLE IF NOT EXISTS reviews (
    order_id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    rating INTEGER NOT NULL,
    comment TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'tg',
    user_id INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, platform, user_id)
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Таблицы из первой версии (без колонки platform), которые пересоздаём с переносом данных.
_REBUILD = {
    "users": "platform, user_id, first_name, username, last_seen",
    "birthdays": "id, name, day, month, platform, user_id, username",
    "cart": "platform, user_id, product_id, qty",
    "usage": "day, platform, user_id, count",
}


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._migrate()
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            self._normalize_phones()

    def _normalize_phones(self) -> None:
        """Один раз: телефоны в старых заказах и сохранённых контактах → единый вид «+7 999 123-45-67».
        То, что не похоже на телефон, остаётся как было."""
        if self._conn.execute("SELECT 1 FROM kv WHERE key='migrate:phones:v1'").fetchone():
            return
        changed = 0
        for row in self._conn.execute("SELECT id, phone FROM orders WHERE phone IS NOT NULL AND phone != ''").fetchall():
            new = normalize_phone(row["phone"])
            if new and new != row["phone"]:
                self._conn.execute("UPDATE orders SET phone=? WHERE id=?", (new, row["id"]))
                changed += 1
        for row in self._conn.execute("SELECT key, value FROM kv WHERE substr(key, 1, 8) = 'contact:'").fetchall():
            try:
                data = json.loads(row["value"])
            except ValueError:
                continue
            new = normalize_phone(data.get("phone")) if isinstance(data, dict) else None
            if new and new != data.get("phone"):
                data["phone"] = new
                self._conn.execute("UPDATE kv SET value=? WHERE key=?", (json.dumps(data, ensure_ascii=False), row["key"]))
                changed += 1
        self._conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('migrate:phones:v1', ?)",
                           (json.dumps({"changed": changed}),))
        self._conn.commit()
        if changed:
            logger.info("Телефоны приведены к единому виду: %s записей", changed)

    # ── миграция базы первой версии ────────────────────────
    def _columns(self, table: str) -> list[str]:
        return [r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")]

    def _migrate(self) -> None:
        for table, cols in _REBUILD.items():
            existing = self._columns(table)
            if not existing or "platform" in existing:
                continue
            logger.info("Обновляю таблицу %s под Telegram + ВКонтакте", table)
            self._conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
            self._conn.executescript(SCHEMA)
            select = ", ".join("'tg'" if c.strip() == "platform" else c.strip() for c in cols.split(","))
            self._conn.execute(f"INSERT INTO {table} ({cols}) SELECT {select} FROM {table}_old")
            self._conn.execute(f"DROP TABLE {table}_old")
        if self._columns("orders") and "platform" not in self._columns("orders"):
            self._conn.execute("ALTER TABLE orders ADD COLUMN platform TEXT NOT NULL DEFAULT 'tg'")
        existing = self._columns("orders")
        if existing and "final_json" not in existing:   # взвешивание заказов: фактические позиции и итог
            self._conn.execute("ALTER TABLE orders ADD COLUMN final_json TEXT")
            self._conn.execute("ALTER TABLE orders ADD COLUMN final_total REAL")
        if existing and "done_at" not in existing:      # когда заказ выдан — для просьбы об отзыве
            self._conn.execute("ALTER TABLE orders ADD COLUMN done_at REAL")
        cart_cols = self._columns("cart")
        if cart_cols and "updated_at" not in cart_cols:  # когда корзину трогали в последний раз — для напоминаний
            self._conn.execute("ALTER TABLE cart ADD COLUMN updated_at REAL")
        self._conn.commit()

    # ── низкоуровневое ─────────────────────────────────────
    def _run(self, sql: str, params: tuple = (), fetch: str | None = None):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return cur.lastrowid

    async def _a(self, sql: str, params: tuple = (), fetch: str | None = None):
        return await asyncio.to_thread(self._run, sql, params, fetch)

    def close(self) -> None:
        self._conn.close()

    # ── key-value (состояние планировщика, снимки прогноза) ─
    async def kv_get(self, key: str, default=None):
        row = await self._a("SELECT value FROM kv WHERE key=?", (key,), "one")
        return json.loads(row["value"]) if row else default

    async def kv_set(self, key: str, value) -> None:
        await self._a("INSERT INTO kv(key, value) VALUES(?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value, ensure_ascii=False)))

    async def kv_prefix(self, prefix: str) -> dict[str, object]:
        """Все записи kv, ключ которых начинается с prefix."""
        rows = await self._a("SELECT key, value FROM kv WHERE substr(key, 1, ?) = ?", (len(prefix), prefix), "all")
        return {r["key"]: json.loads(r["value"]) for r in rows}

    # ── пользователи ───────────────────────────────────────
    async def touch_user(self, platform: str, user_id: int, first_name: str, username: str | None) -> bool:
        """Запоминает пользователя. True — если видим его впервые."""
        row = await self._a("SELECT 1 FROM users WHERE platform=? AND user_id=?", (platform, user_id), "one")
        await self._a("INSERT INTO users(platform, user_id, first_name, username, last_seen) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(platform, user_id) DO UPDATE SET first_name=excluded.first_name, "
                      "username=excluded.username, last_seen=excluded.last_seen",
                      (platform, user_id, first_name, username, datetime.now().isoformat(timespec="seconds")))
        return row is None

    async def user_id_by_username(self, platform: str, username: str) -> int | None:
        row = await self._a("SELECT user_id FROM users WHERE platform=? AND lower(username)=lower(?)",
                            (platform, username.lstrip("@")), "one")
        return row["user_id"] if row else None

    # ── дни рождения ───────────────────────────────────────
    @staticmethod
    def _bd(row) -> Birthday:
        return Birthday(id=row["id"], name=row["name"], day=row["day"], month=row["month"],
                        user_id=row["user_id"], username=row["username"], platform=row["platform"])

    async def add_birthday(self, name: str, day: int, month: int, platform: str = "tg",
                           user_id: int | None = None, username: str | None = None) -> int:
        if user_id:
            existing = await self._a("SELECT id FROM birthdays WHERE platform=? AND user_id=?",
                                     (platform, user_id), "one")
            if existing:
                await self._a("UPDATE birthdays SET name=?, day=?, month=?, username=? WHERE id=?",
                              (name, day, month, username, existing["id"]))
                return existing["id"]
        return await self._a("INSERT INTO birthdays(name, day, month, platform, user_id, username) "
                             "VALUES(?,?,?,?,?,?)", (name, day, month, platform, user_id, username))

    async def birthday_of(self, platform: str, user_id: int) -> Birthday | None:
        row = await self._a("SELECT * FROM birthdays WHERE platform=? AND user_id=?", (platform, user_id), "one")
        return self._bd(row) if row else None

    async def list_birthdays(self) -> list[Birthday]:
        rows = await self._a("SELECT * FROM birthdays ORDER BY month, day", fetch="all")
        return [self._bd(r) for r in rows]

    async def delete_birthday_of(self, platform: str, user_id: int) -> bool:
        row = await self.birthday_of(platform, user_id)
        if not row:
            return False
        await self._a("DELETE FROM birthdays WHERE id=?", (row.id,))
        return True

    async def delete_birthday(self, bd_id: int) -> bool:
        row = await self._a("SELECT id FROM birthdays WHERE id=?", (bd_id,), "one")
        if not row:
            return False
        await self._a("DELETE FROM birthdays WHERE id=?", (bd_id,))
        return True

    # ── корзина ────────────────────────────────────────────
    async def cart(self, platform: str, user_id: int) -> dict[str, float]:
        rows = await self._a("SELECT product_id, qty FROM cart WHERE platform=? AND user_id=?",
                             (platform, user_id), "all")
        return {r["product_id"]: float(r["qty"]) for r in rows}

    async def cart_change(self, platform: str, user_id: int, product_id: str, delta: float,
                          max_qty: float = 99) -> float:
        current = (await self.cart(platform, user_id)).get(product_id, 0.0)
        new_qty = round(max(0.0, min(max_qty, current + delta)), 3)
        if new_qty <= 0:
            await self._a("DELETE FROM cart WHERE platform=? AND user_id=? AND product_id=?",
                          (platform, user_id, product_id))
            return 0.0
        await self._a("INSERT INTO cart(platform, user_id, product_id, qty, updated_at) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(platform, user_id, product_id) DO UPDATE SET qty=excluded.qty, "
                      "updated_at=excluded.updated_at",
                      (platform, user_id, product_id, new_qty, time.time()))
        return new_qty

    async def stale_carts(self, platform: str, older_than: float, newer_than: float) -> list[tuple[int, float]]:
        """Брошенные корзины: [(user_id, когда трогали в последний раз)] — не позже older_than и не раньше newer_than."""
        rows = await self._a("SELECT user_id, MAX(updated_at) AS ts FROM cart WHERE platform=? AND updated_at IS NOT NULL "
                             "GROUP BY user_id HAVING ts<=? AND ts>=? ORDER BY ts",
                             (platform, older_than, newer_than), "all")
        return [(r["user_id"], float(r["ts"])) for r in rows]

    async def cart_clear(self, platform: str, user_id: int) -> None:
        await self._a("DELETE FROM cart WHERE platform=? AND user_id=?", (platform, user_id))

    # ── заказы ─────────────────────────────────────────────
    async def create_order(self, platform: str, user_id: int, customer_name: str, username: str | None,
                           phone: str, comment: str, items: list[tuple[str, float, float, str]],
                           total: float) -> int:
        return await self._a(
            "INSERT INTO orders(platform, user_id, customer_name, username, phone, comment, items_json, "
            "total, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (platform, user_id, customer_name, username, phone, comment,
             json.dumps(items, ensure_ascii=False), total, "new", datetime.now().strftime("%d.%m.%Y %H:%M")))

    @staticmethod
    def _order(row) -> OrderView:
        final = row["final_json"]
        return OrderView(id=row["id"], customer_name=row["customer_name"] or "", username=row["username"],
                         user_id=row["user_id"], phone=row["phone"] or "", comment=row["comment"] or "",
                         items=[tuple(x) for x in json.loads(row["items_json"])], total=row["total"],
                         status=row["status"], created_at=row["created_at"], platform=row["platform"],
                         final_items=[tuple(x) for x in json.loads(final)] if final else None,
                         final_total=row["final_total"])

    async def order(self, order_id: int) -> OrderView | None:
        row = await self._a("SELECT * FROM orders WHERE id=?", (order_id,), "one")
        return self._order(row) if row else None

    async def orders_of(self, platform: str, user_id: int, limit: int = 5) -> list[OrderView]:
        rows = await self._a("SELECT * FROM orders WHERE platform=? AND user_id=? ORDER BY id DESC LIMIT ?",
                             (platform, user_id, limit), "all")
        return [self._order(r) for r in rows]

    async def recent_orders(self, limit: int = 10, only_open: bool = True) -> list[OrderView]:
        where = "WHERE status IN ('new','accepted','ready')" if only_open else ""
        rows = await self._a(f"SELECT * FROM orders {where} ORDER BY id DESC LIMIT ?", (limit,), "all")
        return [self._order(r) for r in rows]

    async def set_order_status(self, order_id: int, status: str) -> None:
        await self._a("UPDATE orders SET status=?, done_at=CASE WHEN ?='done' THEN ? ELSE done_at END WHERE id=?",
                      (status, status, time.time(), order_id))

    async def all_orders(self) -> list[OrderView]:
        rows = await self._a("SELECT * FROM orders ORDER BY id", (), "all")
        return [self._order(r) for r in rows]

    async def orders_to_review(self, platform: str, done_before: float, done_after: float) -> list[OrderView]:
        """Выданные заказы, по которым пора просить отзыв: выданы не позже done_before и не раньше done_after."""
        rows = await self._a("SELECT * FROM orders WHERE platform=? AND status='done' AND done_at IS NOT NULL "
                             "AND done_at<=? AND done_at>=? ORDER BY id", (platform, done_before, done_after), "all")
        return [self._order(r) for r in rows]

    # ── оценки заказов ─────────────────────────────────────
    async def add_review(self, order_id: int, platform: str, user_id: int, rating: int) -> bool:
        """False — этот заказ уже оценён."""
        if await self.review(order_id):
            return False
        await self._a("INSERT INTO reviews(order_id, platform, user_id, rating, created_at) VALUES(?,?,?,?,?)",
                      (order_id, platform, user_id, rating, time.time()))
        return True

    async def set_review_comment(self, order_id: int, comment: str) -> None:
        await self._a("UPDATE reviews SET comment=? WHERE order_id=?", (comment, order_id))

    async def review(self, order_id: int) -> dict | None:
        row = await self._a("SELECT * FROM reviews WHERE order_id=?", (order_id,), "one")
        return dict(row) if row else None

    async def ratings_since(self, since: float) -> list[int]:
        rows = await self._a("SELECT rating FROM reviews WHERE created_at>=?", (since,), "all")
        return [r["rating"] for r in rows]

    async def set_order_final(self, order_id: int, items: list[tuple[str, float, float, str]],
                              total: float) -> None:
        """Итог после взвешивания: позиции с фактическим весом и сумма к оплате."""
        await self._a("UPDATE orders SET final_json=?, final_total=? WHERE id=?",
                      (json.dumps(items, ensure_ascii=False), total, order_id))

    # ── лимиты обращений к нейросети ───────────────────────
    async def usage_hit(self, day: str, platform: str, user_id: int, cap: int) -> bool:
        """Засчитывает обращение. False — лимит на сегодня исчерпан."""
        row = await self._a("SELECT count FROM usage WHERE day=? AND platform=? AND user_id=?",
                            (day, platform, user_id), "one")
        if (row["count"] if row else 0) >= cap:
            return False
        await self._a("INSERT INTO usage(day, platform, user_id, count) VALUES(?,?,?,1) "
                      "ON CONFLICT(day, platform, user_id) DO UPDATE SET count=count+1", (day, platform, user_id))
        return True
