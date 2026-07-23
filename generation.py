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
from collections import deque
from datetime import datetime

import db
from readings import PROMPTS
from config import TITLES
from numerology import (
    build_numerology_context, build_name_context, calculate_destiny,
    calculate_personal_year, calculate_personal_month,
)
from ai import ask_ai

# Память диалога «Спроси Еву»: последние реплики на пользователя, чтобы фича
# ощущалась как настоящий чат («а почему?» → Ева продолжает мысль), а не набор
# разовых ответов. Общая для бота и веба (один процесс). В памяти, а не в БД:
# при рестарте очищается — для живого диалога это допустимо.
_ASK_HISTORY: dict[int, deque] = {}
_ASK_HISTORY_MAX = 8  # 8 сообщений = 4 пары «вопрос-ответ»

_RU_MONTHS = ["январе", "феврале", "марте", "апреле", "мае", "июне", "июле",
              "августе", "сентябре", "октябре", "ноябре", "декабре"]


def reset_ask_history(user_id: int) -> None:
    """Сбросить память диалога (например, когда пользователь заходит в фичу заново)."""
    _ASK_HISTORY.pop(user_id, None)


async def answer_ask(user_id: int, user: dict, question: str) -> str:
    """Ответ Евы на личный премиум-вопрос («Спроси Еву»).

    Отличия от простого промпта: (1) персонализация под ТЕКУЩИЙ период —
    личный год + все 12 личных месяцев этого года, чтобы на «что с деньгами в
    марте?» Ева отвечала по реальному личному месяцу, а не гадала; (2) память
    последних реплик — держим живой диалог. Общая функция для бота и веба."""
    name = _default_name(user)
    bd   = user["birth_date"]
    male = db.is_male(user)
    context = build_numerology_context(name, bd)

    now = datetime.now()
    py_now  = calculate_personal_year(bd, now.year)
    py_next = calculate_personal_year(bd, now.year + 1)
    months  = ", ".join(
        f"{_RU_MONTHS[m - 1]} — {calculate_personal_month(bd, now.year, m)}"
        for m in range(1, 13)
    )
    period = (
        f"Сейчас {now.day} {_RU_MONTHS[now.month - 1]} {now.year} года.\n"
        f"Её личный год сейчас — {py_now} (в {now.year + 1} будет {py_next}).\n"
        f"Личные месяцы {now.year} года по числам: {months}.\n"
        "Личный месяц — это реальная помесячная энергия; опирайся на него, "
        "когда вопрос про конкретное время."
    )

    # Память о разборах: Ева «помнит», что уже рассказывала — может ссылаться на
    # купленные разборы. Тексты длинные, поэтому подрезаем каждый и берём до 6
    # свежих, чтобы не раздувать промпт (и расходы на токены).
    readings_block = ""
    try:
        readings = await db.get_all_reading_texts(user_id)
    except Exception:
        readings = []
    if readings:
        parts = []
        for r in readings[:6]:
            txt = (r.get("text") or "").strip().replace("\n", " ")
            if len(txt) > 500:
                txt = txt[:500].rsplit(" ", 1)[0] + "…"
            parts.append(f"• {r.get('title', 'Разбор')}: {txt}")
        readings_block = (
            "Ты уже делала для неё разборы — помни их содержание и ссылайся, "
            "если к месту (не пересказывай целиком):\n" + "\n".join(parts) + "\n\n"
        )

    hist = _ASK_HISTORY.get(user_id)
    history_block = ""
    if hist:
        history_block = (
            "Вы уже общаетесь, вот последние реплики (учитывай контекст, "
            "если это продолжение разговора):\n" + "\n".join(hist) + "\n\n"
        )

    gender_line = (
        "КЛИЕНТ — МУЖЧИНА: обращайся к нему только в мужском роде, а все "
        "местоимения «она/её/ей» в этой инструкции читай как «он/его/ему».\n\n"
    ) if male else ""
    writes = "Он пишет тебе" if male else "Она пишет тебе"
    prompt = (
        f"{gender_line}"
        f"Ты — Ева, тёплый личный нумеролог {name} и одновременно опытный, чуткий "
        f"психолог. Ты знаешь её числа, текущий период и разборы, которые уже для неё "
        f"делала.\n\n"
        f"Её нумерологические данные:\n{context}\n\n"
        f"{period}\n\n"
        f"{readings_block}"
        f"{history_block}"
        f"{writes}: «{question}»\n\n"
        "Ответь как живая помощница: РЕАГИРУЙ ИМЕННО НА ТО, ЧТО ОНА НАПИСАЛА. "
        "Если это осмысленный вопрос — ответь на него по делу, привлекая её числа и "
        "личный период ТОЛЬКО там, где это реально помогает (если вопрос про "
        "конкретный месяц — назови её личный месяц на это время). Если это "
        "приветствие, одно слово, набор букв или что-то непонятное — НЕ вываливай "
        "разбор по числам: по-человечески поздоровайся или мягко уточни, что именно "
        "она хочет узнать. Если вопрос совсем не про её жизнь (просят код, факты, "
        "постороннюю тему) — по-доброму скажи, что это не по твоей части.\n\n"
        "ВАЖНО: не придумывай факты о её жизни — работу, увлечения, события, "
        "которых нет в её числах, разборах или в этом разговоре. Если чего-то не "
        "знаешь — спроси, а не предполагай. И никогда не обращайся к ней по имени "
        "«Ева» — это ТВОЁ имя; если её имя Ева или ты не уверена в имени, обходись "
        "без обращения по имени («слушай», «смотри»).\n\n"
        "Когда она делится переживаниями, тревогой, отношениями или трудным выбором — "
        "включай навыки опытного психолога: сначала признай и назови её чувства "
        "(валидация), поддержи, при уместности мягко отрази ситуацию или задай "
        "бережный уточняющий вопрос, дай спокойную опору и, если просят, простой "
        "выполнимый шаг. Числа используй как поддержку смысла, а не вместо человеческого "
        "участия. Если чувствуешь, что ей по-настоящему тяжело (признаки отчаяния, мыслей "
        "о том, чтобы навредить себе) — отнесись серьёзно, тепло поддержи и мягко "
        "предложи опереться на близких или специалиста; не обесценивай и не своди к числам.\n\n"
        "Тон тёплый, живой, как близкая подруга. Пиши простыми точными словами; перед "
        "ответом мысленно перечитай его и убери нелепые, двусмысленные или неверно "
        "подобранные слова. Без emoji-заголовков, без блоков — связный текст, обычно "
        "2-6 предложений."
    )
    async with premium_gen_semaphore(user):
        answer = await ask_ai(prompt)

    dq = _ASK_HISTORY.setdefault(user_id, deque(maxlen=_ASK_HISTORY_MAX))
    dq.append(f"{'Он' if male else 'Она'}: {question}")
    dq.append(f"Ева: {answer}")
    return answer

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


def _default_name(user: dict) -> str:
    return user.get("first_name") or ("дорогой" if db.is_male(user) else "дорогая")


def _gender_note(user: dict) -> str:
    """Префикс к промпту для мужчин. SYSTEM_PROMPT по умолчанию требует женский
    род, но содержит явное исключение по этой фразе — все шаблоны разборов
    остаются без изменений, род переключается одной строкой."""
    if db.is_male(user):
        return ("ВАЖНО: клиент — мужчина. Обращайся к нему только в мужском роде "
                "('ты родился', 'ты пришёл', 'ты сильный'), о партнёре пиши в "
                "женском роде ('твоя партнёрша', 'твоя женщина').\n\n")
    return ""


async def generate_single(user_id: int, user: dict, key: str, date_str: str,
                          subject_name: str | None = None) -> tuple[str, str, bool]:
    """Генерирует (или достаёт из кэша) одиночный разбор. Возвращает
    (title, text, from_cache). from_cache=True — тот же текст на ту же дату мы
    уже делали, показываем его без перегенерации, чтобы не было противоречий.
    Бросает GenerationBusy если у юзера уже идёт генерация. Замок/кэш/сохранение
    здесь; доставка (сообщение или веб-рендер) — на вызывающем коде.

    subject_name — когда разбор не для владельца аккаунта, а для кого-то ещё
    (флоу «другая дата»): без этого числа имени/души/личности считались бы по
    имени владельца аккаунта на ЧУЖУЮ дату рождения — числовая мешанина."""
    if user_id in _generating:
        raise GenerationBusy()
    _generating.add(user_id)
    try:
        name  = subject_name or _default_name(user)
        title = TITLES.get(key, "🔮 Разбор")
        cached = await db.get_reading_text(user_id, key)
        if cached and cached.get("date_str") == date_str:
            return title, cached["text"], True
        context = build_numerology_context(name, date_str)
        prompt  = _gender_note(user) + build_prompt(key, name=name, context=context, date=date_str)
        async with premium_gen_semaphore(user):
            answer = await ask_ai(prompt)
        await db.save_reading_text(user_id, key, title, answer, date_str)
        return title, answer, False
    finally:
        _generating.discard(user_id)


async def generate_name(user_id: int, user: dict, key: str, subject: str) -> tuple[str, str, bool]:
    """Генерирует разбор, который считается по ИМЕНИ/НАЗВАНИЮ, а не по дате
    (name_secret, business_name). subject — имя или название бизнеса. Контекст
    строится из букв (build_name_context), не из даты. Кэш ключа хранит subject
    в поле date_str — повтор на то же название отдаёт тот же текст."""
    if user_id in _generating:
        raise GenerationBusy()
    _generating.add(user_id)
    try:
        title = TITLES.get(key, "🔮 Разбор")
        cached = await db.get_reading_text(user_id, key)
        if cached and cached.get("date_str") == subject:
            return title, cached["text"], True
        name    = _default_name(user)
        context = build_name_context(subject)
        prompt  = _gender_note(user) + build_prompt(key, name=name, subject=subject, context=context)
        async with premium_gen_semaphore(user):
            answer = await ask_ai(prompt)
        await db.save_reading_text(user_id, key, title, answer, subject)
        return title, answer, False
    finally:
        _generating.discard(user_id)


async def answer_yes_no(name: str, birth_date: str, question: str, male: bool = False) -> str:
    """Короткий ответ «Да/Нет» с 1-2 предложениями обоснования по числам —
    отдельная микро-фича (не структурированный разбор). Не кэшируется и не
    занимает замок генерации: это лёгкий частый запрос, а не тяжёлый разбор."""
    context = build_numerology_context(name, birth_date)
    p3 = "Он" if male else "Она"
    poss = "его" if male else "её"
    note = ("ВАЖНО: клиент — мужчина, обращайся в мужском роде.\n" if male else "")
    prompt = (
        f"{note}Нумерологические данные {name}:\n{context}\n\n"
        f"{p3} задаёт вопрос, на который нужен ответ ДА или НЕТ: «{question}»\n\n"
        "Ответь как Ева: начни РОВНО с одного слова — «Да», «Нет» или «Скорее да» / "
        "«Скорее нет», затем с новой строки 1-2 коротких предложения обоснования по "
        f"{poss} числам. Без заголовков, без emoji, без воды, тёплым живым тоном. "
        f"Если вопрос не про {poss} жизнь/выбор (просят код, факты, посторонняя тема) — "
        "не гадай, ответь одним предложением, что это не по твоей части."
    )
    async with _gen_semaphore:
        return await ask_ai(prompt)


async def generate_compat(user_id: int, user: dict, date1: str, date2: str, key: str = "compat") -> tuple[str, str, bool]:
    """То же, что generate_single, но для разборов на ДВЕ даты (совместимость).
    key различает разные разборы этого рода, если появятся ещё варианты на две
    даты — каждый со своим промптом, кэшем и заголовком."""
    if user_id in _generating:
        raise GenerationBusy()
    _generating.add(user_id)
    try:
        name  = _default_name(user)
        title = TITLES.get(key, "💑 Совместимость")
        compat_date_str = f"{date1},{date2}"
        cached = await db.get_reading_text(user_id, key)
        if cached and cached.get("date_str") == compat_date_str:
            return title, cached["text"], True
        n2 = calculate_destiny(date2)
        context = build_numerology_context(name, date1)
        prompt  = _gender_note(user) + build_prompt(key, name=name, context=context, date1=date1, date2=date2, n2=n2)
        async with premium_gen_semaphore(user):
            answer = await ask_ai(prompt)
        await db.save_reading_text(user_id, key, title, answer, compat_date_str)
        return title, answer, False
    finally:
        _generating.discard(user_id)
