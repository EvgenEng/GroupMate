"""Что бот знает о магазине и как передаёт вопросы менеджеру.

Справка для нейросети собирается из каталога (цены, состав) и файла config/faq.txt
(доставка, оплата, сроки). Бот отвечает только по ней, а на всё остальное зовёт менеджера:
нейросеть дописывает в конец ответа служебную метку HANDOFF_MARK, бот её убирает
и предлагает человеку оставить контакты.
"""
from __future__ import annotations

from html import escape
from pathlib import Path

from app.core.orders import Catalog, fmt_qty

HANDOFF_MARK = "[[МЕНЕДЖЕР]]"
HANDOFF_FALLBACK = "Этот вопрос лучше уточнить у менеджера — он подскажет точно."

RULES = f"""\
ПРАВИЛА ОТВЕТОВ (строго):
1. Отвечай только на основе справки ниже. Цены, состав, сроки, условия доставки и оплаты — только из справки. Ничего не выдумывай и не округляй цены.
2. Если в справке нет ответа, вопрос про доставку, самовывоз, точные сроки, скидки, индивидуальные условия, наличие конкретной позиции на дату — не гадай. Скажи, что это уточнит менеджер, и допиши в самом конце ответа метку {HANDOFF_MARK} (только её, без пояснений).
3. Метку {HANDOFF_MARK} добавляй, только если ответить сам не можешь или нужен менеджер. На обычные вопросы по справке отвечай сам, без метки.
4. Заказ оформляется кнопками бота: напомни про «🛍 Каталог» и «🛒 Корзина», если человек хочет заказать.
5. Пиши коротко (1–5 предложений), по-русски, на «вы». Без markdown, без списков со звёздочками.
6. Не обсуждай политику, религию и темы, не связанные с магазином: вежливо верни разговор к продукции.
"""


def load_faq(path: Path | None) -> str:
    if path and path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def catalog_digest(catalog: Catalog) -> str:
    """Компактный прайс для нейросети: «название — цена за единицу (в наличии/нет)»."""
    lines: list[str] = []
    for cat in catalog.categories:
        items = [p for p in catalog.products if p.category == cat.id]
        if not items:
            continue
        lines.append(f"\n{cat.title}:")
        for p in items:
            price = f"{fmt_qty(p.price)} {catalog.currency}/{p.unit}"
            extra = f" — {p.description}" if p.description else ""
            mark = "" if p.available else " (временно нет в наличии)"
            lines.append(f"- {p.name}: {price}{mark}{extra}")
    return "\n".join(lines).strip()


def build_knowledge(catalog: Catalog, faq: str) -> str:
    shop = catalog.shop
    parts = [RULES]
    head = [x for x in (shop.name and f"Магазин: {shop.name}", shop.about, shop.lead_time, shop.footnote) if x]
    if head:
        parts.append("О МАГАЗИНЕ:\n" + "\n".join(head))
    if faq:
        parts.append("СПРАВКА (доставка, оплата, сроки):\n" + faq)
    digest = catalog_digest(catalog)
    if digest:
        parts.append("КАТАЛОГ И ЦЕНЫ:\n" + digest)
    return "\n\n".join(parts)


def split_handoff(answer: str) -> tuple[str, bool]:
    """Ответ нейросети → (текст без метки, нужен ли менеджер)."""
    needs = HANDOFF_MARK in answer or "[[МЕНЕДЖЕР" in answer
    clean = answer.replace(HANDOFF_MARK, "")
    clean = clean.replace("[[МЕНЕДЖЕР]", "").replace("[[МЕНЕДЖЕР", "")
    return clean.strip(), needs


def format_handoff(name: str, link: str, phone: str, question: str, bot_answer: str = "",
                   no_phone_hint: str = "написать в ВК по ссылке выше") -> str:
    """Сообщение менеджеру: человек задал вопрос, на который бот не смог ответить."""
    who = f'<a href="{escape(link)}">{escape(name)}</a>' if link else escape(name)
    contact = f"📞 {escape(phone)}" if phone else f"📞 телефон не указан — {escape(no_phone_hint)}"
    text = (
        "🙋 <b>Нужна связь менеджера с покупателем</b>\n"
        "Бот не смог ответить на вопрос и передал его вам.\n\n"
        f"👤 {who}\n{contact}\n\n"
        f"❓ Вопрос: {escape(question[:1000])}"
    )
    if bot_answer:
        text += f"\n\n🤖 Что ответил бот: {escape(bot_answer[:300])}"
    return text
