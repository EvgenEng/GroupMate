"""Промокоды: список из config/promo.json, разбор ввода, тексты ответов. Без БД и мессенджеров."""
from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from pathlib import Path

# Русские буквы, похожие на латинские: «АКЦИЯ10» и «AKЦИЯ10» не должны расходиться из-за раскладки.
_LOOKALIKE = str.maketrans("АВЕКМНОРСТУХ", "ABEKMHOPCTYX")


def normalize_code(raw: str) -> str:
    """«gastro-10», «GASTRO 10», «Gastro10!» → «GASTRO10» (регистр, пробелы, дефисы, раскладка не важны)."""
    text = "".join((raw or "").split()).upper().strip(".,!?:;\"'«»()")
    return text.replace("-", "").replace("_", "").translate(_LOOKALIKE)


@dataclass(frozen=True)
class PromoCode:
    code: str            # как написано в файле (для показа)
    description: str = ""
    active: bool = True
    first_order_only: bool = False   # только покупателям без прошлых заказов

    @property
    def key(self) -> str:
        return normalize_code(self.code)


class PromoBook:
    def __init__(self, codes: list[PromoCode] | None = None):
        self.codes = {c.key: c for c in (codes or []) if c.key}

    @classmethod
    def load(cls, path: Path) -> "PromoBook":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls([PromoCode(code=str(c["code"]).strip(), description=str(c.get("description", "")).strip(),
                              active=bool(c.get("active", True)),
                              first_order_only=bool(c.get("first_order_only", False)))
                    for c in data.get("codes", []) if c.get("code")])

    def find(self, raw: str) -> PromoCode | None:
        return self.codes.get(normalize_code(raw))


@dataclass
class PromoResult:
    status: str                      # ok | used | unknown | inactive | not_first | blocked | empty
    promo: PromoCode | None = None


RETRY_STATUSES = {"unknown", "empty"}   # можно сразу попробовать ещё раз


def promo_reply(result: PromoResult) -> str:
    p = result.promo
    if result.status == "ok":
        text = f"🎉 Промокод <b>{escape(p.code)}</b> принят!"
        if p.description:
            text += f"\n🎁 {escape(p.description)}"
        return text + "\n\nОн будет учтён в вашем заказе — менеджер применит его, когда свяжется с вами."
    if result.status == "used":
        return "Этот промокод вы уже вводили — повторно он не действует 🙂"
    if result.status == "not_first":
        return "Этот промокод действует только на первый заказ — у вас уже есть заказы 🙂"
    if result.status == "inactive":
        return "Этот промокод больше не действует."
    if result.status == "blocked":
        return "Слишком много неверных попыток на сегодня. Попробуйте завтра или напишите менеджеру."
    if result.status == "empty":
        return "Напишите промокод текстом, пожалуйста."
    return "Не нашёл такой промокод 🤔 Проверьте написание на флаере и отправьте ещё раз."


def format_promo_notice(platform_name: str, customer: str, code: str, description: str, when: str) -> str:
    """Сообщение менеджерам: кто и какой промокод ввёл. `customer` — готовая HTML-ссылка на профиль."""
    rows = [f"🏷 <b>Введён промокод {escape(code)}</b>"]
    if description:
        rows.append(f"🎁 {escape(description)}")
    rows += ["", f"👤 {customer}", f"🕒 {escape(when)} · {escape(platform_name)}", "",
             "Промокод добавится в карточку ближайшего заказа этого покупателя."]
    return "\n".join(rows)
