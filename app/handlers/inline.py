"""Инлайн-режим: «@бот вопрос?» в любом чате.

Включается у @BotFather: /setinline → выбрать бота → текст-подсказка.
Telegram присылает запрос на каждую набранную букву, поэтому нейросеть
зовём, только когда вопрос закончен знаком «?», «!» или «.».
"""
from __future__ import annotations

import hashlib
import logging
from html import escape

from aiogram import Router
from aiogram.types import InlineQuery, InlineQueryResultArticle, InlineQueryResultsButton, InputTextMessageContent

from app import keyboards as kb
from app.context import AppContext
from app.services import weather_reports
from app.services.llm import LLMError

logger = logging.getLogger(__name__)
router = Router(name="inline")


def _article(key: str, title: str, text: str, description: str = "", markup=None) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=hashlib.md5(key.encode()).hexdigest(),
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(message_text=text, parse_mode="HTML"),
        reply_markup=markup,
    )


def _pm_button(text: str) -> InlineQueryResultsButton:
    return InlineQueryResultsButton(text=text, start_parameter="help")


async def _default_results(ctx: AppContext) -> list[InlineQueryResultArticle]:
    results = []
    try:
        weather = await weather_reports.now_report(ctx.weather, ctx.today())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Погода для инлайна: %s", exc)
        weather = None
    if weather:
        results.append(_article("weather", "🌤 Погода в Москве и области", weather,
                                "Отправить прогноз на сегодня в этот чат"))
    if ctx.catalog.products:
        results.append(_article("catalog", "🛍 Каталог и заказ",
                                "🛍 <b>Заказы принимаю в личке</b> — каталог по кнопке ниже 👇",
                                "Поделиться ссылкой на каталог",
                                kb.order_button(ctx.deep_link("order"))))
    return results


@router.inline_query()
async def inline(query: InlineQuery, ctx: AppContext) -> None:
    text = query.query.strip()
    if not text:
        await query.answer(await _default_results(ctx), cache_time=300, is_personal=False)
        return

    if text.lower() in {"погода", "погода?"}:
        await query.answer(await _default_results(ctx), cache_time=300)
        return

    if not ctx.llm.enabled:
        await query.answer([], cache_time=60, button=_pm_button("Нейросеть пока не подключена"))
        return

    if text[-1] not in "?!.":
        hint = _article("hint", "✍️ Допишите вопрос и поставьте «?»",
                        escape(text), "Я отвечу, когда вопрос будет закончен")
        await query.answer([hint], cache_time=5, is_personal=True)
        return

    if not await ctx.llm_allowed(query.from_user.id):
        await query.answer([], cache_time=30, is_personal=True,
                           button=_pm_button("Лимит вопросов на сегодня исчерпан"))
        return

    try:
        answer = await ctx.llm.one_shot(
            f"Ответь коротко (до 600 символов), это ответ для вставки в чат. Вопрос: {text}",
            max_tokens=400, temperature=0.7)
    except LLMError as exc:
        logger.warning("Инлайн LLM: %s", exc)
        await query.answer([], cache_time=5, is_personal=True,
                           button=_pm_button("Не получилось ответить, попробуйте ещё раз"))
        return

    message = f"❓ <i>{escape(text)}</i>\n\n{escape(answer)}"[:4000]
    result = _article(f"ai:{text}", "🤖 Ответ готов — нажмите, чтобы отправить", message, answer[:100])
    await query.answer([result], cache_time=300, is_personal=True)
