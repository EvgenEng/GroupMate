"""Клиент нейросети по OpenAI-совместимому протоколу.

Работает с любым провайдером, у которого есть /chat/completions:
Google Gemini, OpenRouter, Groq, DeepSeek, локальная Ollama и т.д.
Если основной провайдер упал или упёрся в лимит — пробуем запасной.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict, deque

from app.config import LLMProvider

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class LLMError(Exception):
    pass


def build_payload(model: str, messages: list[dict], max_tokens: int, temperature: float) -> dict:
    # Только стандартные поля — их понимают все провайдеры.
    return {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}


def extract_text(data: dict) -> str:
    try:
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Неожиданный ответ LLM: {str(data)[:300]}") from exc
    text = _THINK_RE.sub("", content).strip()
    if not text:
        raise LLMError("LLM вернула пустой ответ")
    return text


class LLMClient:
    def __init__(self, providers: list[LLMProvider], system_prompt: str = "", timeout: float = 40):
        self.providers = providers
        self.system_prompt = system_prompt
        self.timeout = timeout
        self.knowledge = ""   # справка о магазине: каталог, FAQ, правила ответов (подмешивается только в диалог)
        # Короткая память диалога: последние 3 пары «вопрос–ответ» на чат.
        self._history: dict[str, deque] = defaultdict(lambda: deque(maxlen=6))

    @property
    def enabled(self) -> bool:
        return bool(self.providers)

    async def complete(self, messages: list[dict], max_tokens: int = 700, temperature: float = 0.8) -> str:
        import aiohttp  # импорт здесь, чтобы чистую логику можно было тестировать без зависимостей

        if not self.providers:
            raise LLMError("Нейросеть не настроена (нет LLM_API_KEY)")
        last_error: Exception | None = None
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for provider in self.providers:
                payload = build_payload(provider.model, messages, max_tokens, temperature)
                headers = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
                try:
                    async with session.post(provider.url, json=payload, headers=headers) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            raise LLMError(f"{provider.model}: HTTP {resp.status}: {body[:300]}")
                        return extract_text(await resp.json(content_type=None))
                except Exception as exc:  # noqa: BLE001 — пробуем следующего провайдера
                    logger.warning("LLM %s не ответила: %s", provider.model, exc)
                    last_error = exc
        raise LLMError(str(last_error))

    def _system(self, extra: str = "", knowledge: bool = False) -> list[dict]:
        prompt = self.system_prompt or DEFAULT_SYSTEM_PROMPT
        if knowledge and self.knowledge:
            prompt = f"{prompt}\n\n{self.knowledge}"
        if extra:
            prompt = f"{prompt}\n\n{extra}"
        return [{"role": "system", "content": prompt}]

    async def chat(self, chat_key: str, user_text: str, extra_context: str = "") -> str:
        """chat_key — «tg:123» или «vk:2000000001»: у каждого чата своя короткая память."""
        history = self._history[chat_key]
        messages = self._system(extra_context, knowledge=True) + list(history) + [{"role": "user", "content": user_text}]
        answer = await self.complete(messages)
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": answer})
        return answer

    async def one_shot(self, prompt: str, max_tokens: int = 500, temperature: float = 0.9) -> str:
        return await self.complete(self._system() + [{"role": "user", "content": prompt}],
                                   max_tokens=max_tokens, temperature=temperature)


DEFAULT_SYSTEM_PROMPT = (
    "Ты — дружелюбный помощник в Telegram-группе. Отвечай по-русски, коротко и по делу "
    "(обычно 1–5 предложений), тепло и с лёгким юмором, без канцелярита. "
    "Не используй markdown-разметку (звёздочки, решётки) — Telegram покажет её как есть. "
    "Если не знаешь ответа или вопрос про свежие события — честно скажи об этом."
)
