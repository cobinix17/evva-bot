# generation.py — общее ЯДРО генерации разборов для бота и веб-кабинета.
# Раньше вся генерация жила в bot.py (_process_date/_process_two_dates) и была
# намертво завязана на aiogram Message/FSM/send_document — поэтому веб не мог
# сам сгенерировать разбор и отправлял пользователя в чат с ботом. Здесь
# вынесена чистая часть (числа → промпт → ИИ → кэш), не зависящая от способа
# доставки: бот шлёт результат сообщением+PDF, веб рендерит текст на странице.
# Бот и веб работают в ОДНОМ процессе (webapp поднимается тем же ботом), поэтому
# замок _generating и семафоры общие — двойная генерация одного юзера в боте и
# вебе одновременно не запустится.
import asyncio
import logging
from datetime import datetime

import db
from readings import PROMPTS
from config import TITLES
from numerology import build_numerology_context, calculate_destiny
from ai import ask_ai

# Юзеры, у которых прямо сейчас идёт генерация — защита от параллельного запуска
# (двойное списание слота премиума, гонки на кэше). Общий для бота и веба.
_generating: set[int] = set()

# Глобальный лимит одновременных генераций. Защищает от всплеска (сотни человек
# нажали «получить» в одну секунду → сотни параллельных запросов к OpenRouter).
_gen_semaphore = asyncio.Semaphore(8)
# Отдельная полоса для премиум-подписчиков — «приоритет генерации» из бонусов.
_priority_semaphore = asyncio.Semaphore(4)


def premium_gen_semaphore(user: dict):
    return _priority_semaphore if db.is_premium(user) else _gen_semaphore


def build_prompt(key: str, **kwargs) -> str:
    """Собирает промпт по ключу. Бросает ValueError если ключ не найден."""
    current_year = datetime.now().year
    kwargs.setdefault("year", current_year)
    kwargs.setdefault("year_next", current_year + 1)
    kwargs.setdefault("year_after_next", current_year + 2)
    template = PROMPTS.get(key)
    if not template:
        logging.error(f"build_prompt: промпт не найден для ключа '{key}'")
        raise ValueError(f"Промпт '{key}' не существует в PROMPTS")
    return template.format(**kwargs)


class GenerationBusy(Exception):
    """У этого пользователя уже идёт генерация — второй запуск отклонён."""


async def generate_single(user_id: int, user: dict, key: str, date_str: str) -> tuple[str, str, bool]:
    """Генерирует (или достаёт из кэша) одиночный разбор. Возвращает
    (title, text, from_cache). from_cache=True — тот же текст на ту же дату мы
    уже делали, показываем его без перегенерации, чтобы не было противоречий.
    Бросает GenerationBusy если у юзера уже идёт генерация. Замок/кэш/сохранение
    здесь; доставка (сообщение или веб-рендер) — на вызывающем коде."""
    if user_id in _generating:
        raise GenerationBusy()
    _generating.add(user_id)
    try:
        name  = user.get("first_name") or "дорогая"
        title = TITLES.get(key, "🔮 Разбор")
        cached = await db.get_reading_text(user_id, key)
        if cached and cached.get("date_str") == date_str:
            return title, cached["text"], True
        context = build_numerology_context(name, date_str)
        prompt  = build_prompt(key, name=name, context=context, date=date_str)
        async with premium_gen_semaphore(user):
            answer = await ask_ai(prompt)
        await db.save_reading_text(user_id, key, title, answer, date_str)
        return title, answer, False
    finally:
        _generating.discard(user_id)


async def answer_yes_no(name: str, birth_date: str, question: str) -> str:
    """Короткий ответ «Да/Нет» с 1-2 предложениями обоснования по числам —
    отдельная микро-фича (не структурированный разбор). Не кэшируется и не
    занимает замок генерации: это лёгкий частый запрос, а не тяжёлый разбор."""
    context = build_numerology_context(name, birth_date)
    prompt = (
        f"Нумерологические данные {name}:\n{context}\n\n"
        f"Она задаёт вопрос, на который нужен ответ ДА или НЕТ: «{question}»\n\n"
        "Ответь как Ева: начни РОВНО с одного слова — «Да», «Нет» или «Скорее да» / "
        "«Скорее нет», затем с новой строки 1-2 коротких предложения обоснования по "
        "её числам. Без заголовков, без emoji, без воды, тёплым живым тоном. "
        "Если вопрос не про её жизнь/выбор (просят код, факты, посторонняя тема) — "
        "не гадай, ответь одним предложением, что это не по твоей части."
    )
    async with _gen_semaphore:
        return await ask_ai(prompt)


async def generate_compat(user_id: int, user: dict, date1: str, date2: str) -> tuple[str, str, bool]:
    """То же, что generate_single, но для разбора совместимости (две даты).
    Ключ разбора — 'compat', в кэше даты хранятся как 'date1,date2'."""
    if user_id in _generating:
        raise GenerationBusy()
    _generating.add(user_id)
    try:
        name  = user.get("first_name") or "дорогая"
        title = "💑 Совместимость"
        compat_date_str = f"{date1},{date2}"
        cached = await db.get_reading_text(user_id, "compat")
        if cached and cached.get("date_str") == compat_date_str:
            return title, cached["text"], True
        n2 = calculate_destiny(date2)
        context = build_numerology_context(name, date1)
        prompt  = build_prompt("compat", name=name, context=context, date1=date1, date2=date2, n2=n2)
        async with premium_gen_semaphore(user):
            answer = await ask_ai(prompt)
        await db.save_reading_text(user_id, "compat", title, answer, compat_date_str)
        return title, answer, False
    finally:
        _generating.discard(user_id)
