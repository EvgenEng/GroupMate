"""SQLite-хранилище. Стандартный sqlite3, вызовы уходят в отдельный поток,
чтобы не блокировать бота. Одна база data/bot.db переживает перезапуски."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from app.core.birthdays import Birthday
from app.core.orders import OrderView

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    first_name TEXT,
    username TEXT,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS birthdays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    day INTEGER NOT NULL,
    month INTEGER NOT NULL,
    user_id INTEGER UNIQUE,
    username TEXT
);
CREATE TABLE IF NOT EXISTS cart (
    user_id INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    qty INTEGER NOT NULL,
    PRIMARY KEY (user_id, product_id)
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    customer_name TEXT,
    username TEXT,
    phone TEXT,
    comment TEXT,
    items_json TEXT NOT NULL,
    total REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_id)
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
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

    # ── пользователи ───────────────────────────────────────
    async def touch_user(self, user_id: int, first_name: str, username: str | None) -> None:
        await self._a("INSERT INTO users(user_id, first_name, username, last_seen) VALUES(?,?,?,?) "
                      "ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name, "
                      "username=excluded.username, last_seen=excluded.last_seen",
                      (user_id, first_name, username, datetime.now().isoformat(timespec="seconds")))

    # ── дни рождения ───────────────────────────────────────
    @staticmethod
    def _bd(row) -> Birthday:
        return Birthday(id=row["id"], name=row["name"], day=row["day"], month=row["month"],
                        user_id=row["user_id"], username=row["username"])

    async def add_birthday(self, name: str, day: int, month: int,
                           user_id: int | None = None, username: str | None = None) -> int:
        if user_id:
            existing = await self._a("SELECT id FROM birthdays WHERE user_id=?", (user_id,), "one")
            if existing:
                await self._a("UPDATE birthdays SET name=?, day=?, month=?, username=? WHERE id=?",
                              (name, day, month, username, existing["id"]))
                return existing["id"]
        return await self._a("INSERT INTO birthdays(name, day, month, user_id, username) VALUES(?,?,?,?,?)",
                             (name, day, month, user_id, username))

    async def list_birthdays(self) -> list[Birthday]:
        rows = await self._a("SELECT * FROM birthdays ORDER BY month, day", fetch="all")
        return [self._bd(r) for r in rows]

    async def delete_birthday(self, bd_id: int) -> bool:
        row = await self._a("SELECT id FROM birthdays WHERE id=?", (bd_id,), "one")
        if not row:
            return False
        await self._a("DELETE FROM birthdays WHERE id=?", (bd_id,))
        return True

    # ── корзина ────────────────────────────────────────────
    async def cart(self, user_id: int) -> dict[str, int]:
        rows = await self._a("SELECT product_id, qty FROM cart WHERE user_id=?", (user_id,), "all")
        return {r["product_id"]: r["qty"] for r in rows}

    async def cart_change(self, user_id: int, product_id: str, delta: int) -> int:
        current = (await self.cart(user_id)).get(product_id, 0)
        new_qty = max(0, min(99, current + delta))
        if new_qty == 0:
            await self._a("DELETE FROM cart WHERE user_id=? AND product_id=?", (user_id, product_id))
        else:
            await self._a("INSERT INTO cart(user_id, product_id, qty) VALUES(?,?,?) "
                          "ON CONFLICT(user_id, product_id) DO UPDATE SET qty=excluded.qty",
                          (user_id, product_id, new_qty))
        return new_qty

    async def cart_clear(self, user_id: int) -> None:
        await self._a("DELETE FROM cart WHERE user_id=?", (user_id,))

    # ── заказы ─────────────────────────────────────────────
    async def create_order(self, user_id: int, customer_name: str, username: str | None, phone: str,
                           comment: str, items: list[tuple[str, int, float, str]], total: float) -> int:
        return await self._a(
            "INSERT INTO orders(user_id, customer_name, username, phone, comment, items_json, total, "
            "status, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (user_id, customer_name, username, phone, comment, json.dumps(items, ensure_ascii=False),
             total, "new", datetime.now().strftime("%d.%m.%Y %H:%M")))

    @staticmethod
    def _order(row) -> OrderView:
        return OrderView(id=row["id"], customer_name=row["customer_name"] or "", username=row["username"],
                         user_id=row["user_id"], phone=row["phone"] or "", comment=row["comment"] or "",
                         items=[tuple(x) for x in json.loads(row["items_json"])], total=row["total"],
                         status=row["status"], created_at=row["created_at"])

    async def order(self, order_id: int) -> OrderView | None:
        row = await self._a("SELECT * FROM orders WHERE id=?", (order_id,), "one")
        return self._order(row) if row else None

    async def orders_of(self, user_id: int, limit: int = 5) -> list[OrderView]:
        rows = await self._a("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?",
                             (user_id, limit), "all")
        return [self._order(r) for r in rows]

    async def recent_orders(self, limit: int = 10, only_open: bool = True) -> list[OrderView]:
        where = "WHERE status IN ('new','accepted','ready')" if only_open else ""
        rows = await self._a(f"SELECT * FROM orders {where} ORDER BY id DESC LIMIT ?", (limit,), "all")
        return [self._order(r) for r in rows]

    async def set_order_status(self, order_id: int, status: str) -> None:
        await self._a("UPDATE orders SET status=? WHERE id=?", (status, order_id))

    # ── лимиты обращений к нейросети ───────────────────────
    async def usage_hit(self, day: str, user_id: int, cap: int) -> bool:
        """Засчитывает обращение. False — лимит на сегодня исчерпан."""
        row = await self._a("SELECT count FROM usage WHERE day=? AND user_id=?", (day, user_id), "one")
        count = row["count"] if row else 0
        if count >= cap:
            return False
        await self._a("INSERT INTO usage(day, user_id, count) VALUES(?,?,1) "
                      "ON CONFLICT(day, user_id) DO UPDATE SET count=count+1", (day, user_id))
        return True
