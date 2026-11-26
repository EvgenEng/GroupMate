"""Журнал работы бота: консоль + файл с ротацией. Секреты и персональные данные маскируются.

Маскируются: токены и ключи из настроек, телефоны, длинные номера (карты, счета).
Фильтр стоит на самом форматтере, поэтому защищены и сообщения, и тексты ошибок (traceback).
"""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FILE_NAME = "bot.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5

_PHONE = re.compile(r"(?<![\w])(?:\+7|\+?8|7)[\s\-().]*\d{3}[\s\-().]*\d{3}[\s\-().]*\d{2}[\s\-().]*\d{2}(?!\d)")
_INTL_PHONE = re.compile(r"\+\d[\d\s\-().]{8,}\d")
_LONG_NUMBER = re.compile(r"(?<!\d)(?:\d[ -]?){13,24}(?!\d)")


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Прячет секреты, телефоны и номера карт. Остальное (номера заказов, суммы) остаётся читаемым."""
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    text = _PHONE.sub("[телефон]", text)
    text = _INTL_PHONE.sub("[телефон]", text)
    return _LONG_NUMBER.sub("[номер]", text)


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: tuple[str, ...] = ()):
        super().__init__(fmt)
        self.secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self.secrets)


def collect_secrets(settings) -> tuple[str, ...]:
    """Всё, что нельзя показывать в журнале: токены, ключи нейросетей, реквизиты, адрес «сторожа»."""
    values = [settings.bot_token, settings.vk_token, settings.healthcheck_url, settings.payment_details]
    values += [p.api_key for p in settings.llm_providers]
    values += [line.strip() for line in settings.payment_details.splitlines()]
    return tuple(v for v in values if v and len(v) >= 6)


def setup_logging(level: str = "INFO", log_dir: Path | None = None, secrets: tuple[str, ...] = ()) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = RedactingFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", secrets)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_dir:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(log_dir / FILE_NAME, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                               encoding="utf-8")
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:   # нет прав на папку и т.п.: бот работает, просто без файла
            print(f"Журнал в файл не пишется ({exc}); остаётся только консоль.", file=sys.stderr)
