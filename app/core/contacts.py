"""Контактные данные покупателя: телефон в любом привычном написании → один вид для базы."""
from __future__ import annotations

import re

# Что похоже на телефон: цифры, возможный «+», пробелы, скобки, дефисы и точки между ними.
_CANDIDATE = re.compile(r"\+?\d[\d\s().\-–—]{7,22}\d")


def _normalize(raw: str) -> str | None:
    plus = raw.lstrip().startswith("+")
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits[:2] in {"78", "87"}:   # «+7 8 999…» — лишний «8» после кода страны
        digits = digits[1:]
    national = None
    if len(digits) == 11 and digits[0] in "78":             # +7…, 7…, 8…
        national = digits[1:]
    elif len(digits) == 10:                                  # 999 123-45-67 — без «+7» и без «8»
        national = digits
    elif plus and digits[:1] not in {"7", "8"} and 8 <= len(digits) <= 15:
        return "+" + digits                                  # номер другой страны оставляем как есть
    if national is None or national[0] not in "3456789":
        return None
    return f"+7 {national[0:3]} {national[3:6]}-{national[6:8]}-{national[8:10]}"


def normalize_phone(text: str | None) -> str | None:
    """«89991234567», «+7(999)123-45-67», «8 (999) 123 45 67», «9991234567» → «+7 999 123-45-67».

    Находит телефон и внутри фразы («мой номер 8-999-123-45-67, звоните после 18»).
    None — в тексте нет похожего на телефон."""
    if not text:
        return None
    for match in _CANDIDATE.finditer(str(text)):
        phone = _normalize(match.group(0))
        if phone:
            return phone
    return None


def phone_digits(phone: str | None) -> str:
    """Только цифры телефона в едином виде (для сравнения и поиска): «79991234567»."""
    normalized = normalize_phone(phone)
    return re.sub(r"\D", "", normalized or phone or "")
