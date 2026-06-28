# numerology.py — нумерологические расчёты для бота Ева Нумеролог.
# Импортируется из bot.py: calculate_destiny, calculate_day_number,
# is_valid_date, build_numerology_context.
# Самодостаточный модуль — не импортирует ничего из других файлов проекта.

from datetime import date as _date_type, datetime

# ─── ВАЛИДАЦИЯ ДАТЫ ───────────────────────────────────────────────────────────

def is_valid_date(text: str) -> bool:
    """Проверяет формат ДД.ММ.ГГГГ и реальность даты."""
    text = text.strip()
    parts = text.split(".")
    if len(parts) != 3:
        return False
    try:
        d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
        _date_type(y, m, d)  # бросает ValueError если дата невалидна
        if y < 1900 or y > datetime.now().year:
            return False
        return True
    except (ValueError, TypeError):
        return False

# ─── ЧИСЛО СУДЬБЫ ─────────────────────────────────────────────────────────────

def _digit_sum(n: int) -> int:
    """Сумма цифр числа."""
    return sum(int(c) for c in str(n))

def _reduce_to_single(n: int) -> int:
    """Сводит число к однозначному (мастер-числа 11, 22, 33 не сохраняем —
    нам нужна простая классическая схема 1–9)."""
    while n > 9:
        n = _digit_sum(n)
    return n

def calculate_destiny(date_str: str) -> int:
    """Число судьбы (число жизненного пути) из даты ДД.ММ.ГГГГ.
    Алгоритм: складываем все цифры даты и сводим к 1–9."""
    date_str = date_str.strip()
    digits = date_str.replace(".", "")
    total = sum(int(c) for c in digits)
    return _reduce_to_single(total)

# ─── ЧИСЛО ДНЯ ────────────────────────────────────────────────────────────────

def calculate_day_number(d: _date_type) -> int:
    """Число дня для утреннего поста в канал."""
    total = _digit_sum(d.day) + _digit_sum(d.month) + _digit_sum(d.year)
    return _reduce_to_single(total)

# ─── КАРМИЧЕСКОЕ ЧИСЛО ────────────────────────────────────────────────────────

def calculate_karmic_number(date_str: str) -> int:
    """Кармическое число = число дня рождения (DD), сведённое к 1–9."""
    day = int(date_str.strip().split(".")[0])
    return _reduce_to_single(day)

# ─── ЧИСЛО ЛИЧНОГО ГОДА ───────────────────────────────────────────────────────

def calculate_personal_year(date_str: str, year: int | None = None) -> int:
    """Число личного года = DD + MM + год запроса."""
    if year is None:
        year = datetime.now().year
    parts = date_str.strip().split(".")
    d, m = int(parts[0]), int(parts[1])
    total = _digit_sum(d) + _digit_sum(m) + _digit_sum(year)
    return _reduce_to_single(total)

# ─── ЧИСЛО ИМЕНИ (ПИФАГОРЕЙСКАЯ ТАБЛИЦА) ─────────────────────────────────────

_PYTHAGOREAN_RU = {
    "а": 1, "й": 1, "с": 1,
    "б": 2, "к": 2, "т": 2,
    "в": 3, "л": 3, "у": 3,
    "г": 4, "м": 4, "ф": 4,
    "д": 5, "н": 5, "х": 5,
    "е": 6, "о": 6, "ц": 6,
    "ё": 6, "п": 7, "ч": 7,
    "ж": 7, "р": 8, "ш": 8,
    "з": 8, "щ": 9,
    "и": 9, "ъ": 9,
    "ы": 1, "ь": 2, "э": 5,
    "ю": 6, "я": 7,
}

def calculate_name_number(name: str) -> int:
    """Число имени по пифагорейской таблице для русского языка."""
    name = name.lower().strip()
    total = sum(_PYTHAGOREAN_RU.get(ch, 0) for ch in name if ch.isalpha())
    if total == 0:
        return 1
    return _reduce_to_single(total)

# ─── ПОЗИЦИИ В МАТРИЦЕ (упрощённая схема для контекста) ──────────────────────

def _matrix_positions(date_str: str) -> dict:
    """Возвращает основные числа матрицы судьбы как dict."""
    parts = date_str.strip().split(".")
    d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
    destiny = calculate_destiny(date_str)
    karmic  = calculate_karmic_number(date_str)
    py_year = calculate_personal_year(date_str)
    return {
        "destiny":      destiny,
        "karmic":       karmic,
        "day":          _reduce_to_single(d),
        "month":        _reduce_to_single(m),
        "year_num":     _reduce_to_single(_digit_sum(y)),
        "personal_year": py_year,
    }

# ─── КОНТЕКСТ ДЛЯ ПРОМПТА ─────────────────────────────────────────────────────

_DESTINY_MEANING = {
    1: "лидер, первопроходец, сильная воля",
    2: "дипломат, чуткость, партнёрство",
    3: "творчество, общение, самовыражение",
    4: "стабильность, труд, практичность",
    5: "свобода, перемены, авантюризм",
    6: "забота, гармония, ответственность",
    7: "мудрость, анализ, духовность",
    8: "власть, деньги, бизнес-мышление",
    9: "миссия, альтруизм, завершение цикла",
}

_KARMIC_MEANING = {
    1: "научиться действовать самостоятельно, без одобрения",
    2: "научиться принимать помощь и доверять другим",
    3: "раскрыть творческий потенциал и выражать себя",
    4: "принять дисциплину и построить устойчивую жизнь",
    5: "найти свободу без бегства от ответственности",
    6: "научиться любить без жертвенности",
    7: "доверять интуиции и уединению",
    8: "правильно обращаться с властью и деньгами",
    9: "отпустить прошлое и служить другим",
}

def build_numerology_context(name: str, date_str: str) -> str:
    """Строит текстовый контекст для промпта — числа и их значения.
    Этот текст вставляется в начало каждого промпта через {context}."""
    pos  = _matrix_positions(date_str)
    year = datetime.now().year

    name_num = calculate_name_number(name) if name and name not in ("дорогая", "") else None

    lines = [
        f"Имя: {name}",
        f"Дата рождения: {date_str}",
        f"",
        f"Число судьбы (жизненного пути): {pos['destiny']} — {_DESTINY_MEANING.get(pos['destiny'], '')}",
        f"Число дня рождения: {pos['day']}",
        f"Число месяца: {pos['month']}",
        f"Кармическое число: {pos['karmic']} — {_KARMIC_MEANING.get(pos['karmic'], '')}",
        f"Число личного года ({year}): {pos['personal_year']}",
    ]
    if name_num:
        lines.append(f"Число имени '{name}': {name_num}")

    return "\n".join(lines)
