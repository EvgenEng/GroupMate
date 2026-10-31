"""Ответы на частые вопросы без нейросети: ключевые слова + цены из каталога.

Правила лежат в config/answers.json — их можно править без программиста.
Чистая логика, без обращений к сети: легко тестируется.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core.orders import Catalog, fmt_qty

logger = logging.getLogger(__name__)

MAX_PRODUCTS = 7
SILENT_WORDS = {"ок", "окей", "ага", "угу", "да", "нет", "хорошо", "ладно", "понятно", "ясно", "ок спасибо"}


@dataclass
class Reply:
    text: str = ""                 # что ответить
    manager: bool = False          # предложить передать вопрос менеджеру
    action: str = ""               # «catalog» / «start», если ответ — только действие
    silent: bool = False           # «ок», «да» — отвечать не нужно


@dataclass
class Intent:
    id: str
    keywords: list[str]
    answer: str = ""
    manager: bool = False
    action: str = ""


def normalize(text: str) -> str:
    low = text.lower().replace("ё", "е")
    low = re.sub(r"[^a-zа-я0-9]+", " ", low)
    return f" {low.strip()} "


def _words(text: str) -> list[str]:
    return [w for w in normalize(text).split() if len(w) >= 4]


def _common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _same_word(a: str, b: str) -> bool:
    """«шейка» ≈ «шейку», «курица» ≈ «куриные»: общее начало и расхождение только в окончании."""
    p = _common_prefix(a, b)
    return p >= 4 and p >= min(len(a), len(b)) - 2


class FaqBot:
    def __init__(self, catalog: Catalog, intents: list[Intent] | None = None,
                 price_keywords: list[str] | None = None, aliases: dict[str, list[str]] | None = None):
        self.catalog = catalog
        self.intents = intents or []
        self.price_keywords = [normalize(k).strip() for k in (price_keywords or [])]
        self.aliases = {tuple(normalize(k).split()): v for k, v in (aliases or {}).items()
                        for k in [k.replace("|", " ")]}
        self._name_words = {p.id: _words(p.name) for p in catalog.products}

    @classmethod
    def load(cls, catalog: Catalog, path: Path | None) -> "FaqBot":
        if not path or not path.exists():
            logger.warning("Нет файла ответов %s — бот отвечает только про цены", path)
            return cls(catalog)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            intents = [Intent(id=i["id"], keywords=[normalize(k).strip() if not k.endswith(" ") else normalize(k)
                                                    for k in i.get("keywords", [])],
                              answer=i.get("answer", ""), manager=bool(i.get("manager")),
                              action=i.get("action", ""))
                       for i in data.get("intents", [])]
            return cls(catalog, intents, data.get("price_keywords"), data.get("aliases"))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.error("Не удалось прочитать %s: %s — бот отвечает только про цены", path, exc)
            return cls(catalog)

    # ── поиск ────────────────────────────────────────────────
    def _has(self, norm: str, keyword: str) -> bool:
        kw = keyword.strip()
        if not kw:
            return False
        end_ok = keyword.endswith(" ")  # «опт » — слово целиком
        pattern = r"(?<![a-zа-я0-9])" + re.escape(kw) + (r"(?![a-zа-я0-9])" if end_ok else "")
        return re.search(pattern, norm) is not None

    def find_products(self, text: str) -> list:
        words = _words(text)
        if not words:
            return []
        scored: dict[str, int] = {}
        for p in self.catalog.products:
            score = 0
            for w in words:
                best = max((_common_prefix(w, pw) for pw in self._name_words[p.id] if _same_word(w, pw)), default=0)
                score += best
            if score:
                scored[p.id] = score
        for key_words, id_prefixes in self.aliases.items():
            if any(_same_word(w, k) for w in words for k in key_words if len(k) >= 4):
                for p in self.catalog.products:
                    if any(p.id.startswith(x) for x in id_prefixes):
                        scored[p.id] = max(scored.get(p.id, 0), 4)
        if scored:  # «грудинка» не должна тянуть за собой «грудку»: берём только близкие к лучшему совпадения
            top = max(scored.values())
            scored = {i: v for i, v in scored.items() if v >= top * 0.6}
        order = {p.id: i for i, p in enumerate(self.catalog.products)}
        ids = sorted(scored, key=lambda i: (-scored[i], order[i]))
        by_id = {p.id: p for p in self.catalog.products}
        return [by_id[i] for i in ids]

    def price_text(self, products: list) -> str:
        cur = self.catalog.currency
        lines = []
        for p in products[:MAX_PRODUCTS]:
            note = "" if p.available else " (сейчас нет в наличии)"
            lines.append(f"• {p.name} — {fmt_qty(p.price)} {cur}/{p.unit}{note}")
        text = "\n".join(lines)
        if len(products) > MAX_PRODUCTS:
            text += f"\nи ещё {len(products) - MAX_PRODUCTS} — полный список в «🛍 Каталог»"
        if any(p.unit == "кг" for p in products[:MAX_PRODUCTS]):
            text += "\n\nВес весовых позиций примерный — точная сумма после взвешивания."
        return text

    def answer(self, text: str) -> Reply | None:
        """Текст человека → ответ. None — правила не подошли."""
        norm = normalize(text)
        if norm.strip() in SILENT_WORDS:
            return Reply(silent=True)
        parts: list[str] = []
        manager = False
        action = ""
        matched = [i for i in self.intents if any(self._has(norm, k) for k in i.keywords)]
        products = self.find_products(text)
        asked_price = any(self._has(norm, k) for k in self.price_keywords)
        if products and not asked_price and len(norm.split()) > 3:
            products = []   # длинная фраза без «сколько стоит» — это скорее разговор, а не вопрос про цену
        if products:
            parts.append(self.price_text(products))
        elif asked_price:
            parts.append("Цены на все позиции — в «🛍 Каталог», там же можно сразу оформить заказ 🙂")
            action = "catalog"
        for it in matched:
            if it.answer:
                parts.append(it.answer)
            manager = manager or it.manager
            action = action or it.action
        if not parts and not action:
            return None
        if parts:
            action = action if action == "catalog" and len(matched) <= 1 and not products else ""
        return Reply(text="\n\n".join(parts), manager=manager, action=action)
