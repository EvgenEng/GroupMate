"""Перевод HTML-текстов бота (как для Telegram) в обычный текст ВКонтакте."""
from __future__ import annotations

import html
import re

_VK_PROFILE = re.compile(r'<a href="https?://vk\.(?:ru|com)/id(\d+)">(.*?)</a>', re.S)
_TG_PROFILE = re.compile(r'<a href="tg://user\?id=\d+">(.*?)</a>', re.S)
_LINK = re.compile(r'<a href="([^"]+)">(.*?)</a>', re.S)
_TAG = re.compile(r"<[^>]+>")

VK_TEXT_LIMIT = 4000


def _mention_name(name: str) -> str:
    # «|» и «]» сломали бы упоминание [id1|Имя]
    return html.unescape(name).replace("|", "/").replace("]", ")").replace("[", "(")


def html_to_vk(text: str) -> str:
    text = _VK_PROFILE.sub(lambda m: f"[id{m.group(1)}|{_mention_name(m.group(2))}]", text)
    text = _TG_PROFILE.sub(lambda m: m.group(1), text)

    def link(m: re.Match) -> str:
        url, label = m.group(1), _TAG.sub("", m.group(2))
        return url if html.unescape(label) == url else f"{label} ({url})"

    text = _LINK.sub(link, text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    if len(text) > VK_TEXT_LIMIT:
        text = text[:VK_TEXT_LIMIT - 1] + "…"
    return text
