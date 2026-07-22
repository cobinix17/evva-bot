# numerology.py — нумерологические расчёты для бота Ева Нумеролог.
# Импортируется из bot.py: calculate_destiny, calculate_day_number,
# is_valid_date, build_numerology_context.
# Самодостаточный модуль — не импортирует ничего из других файлов проекта.

from datetime import date as _date_type, datetime

# ─── ВАЛИДАЦИЯ ДАТЫ ───────────────────────────────────────────────────────────

def normalize_date(text: str) -> str:
    """Приводит дату к каноничному ДД.ММ.ГГГГ: люди привычно вводят разделитель
    по-разному (15/03/1995, 15-03-1995, 15 03 1995, 15,03,1995) — заменяем любой
    из них на точку, чтобы не отказывать в валидации из-за формата. Всё, что ниже
    по коду, разбивает дату по точке, поэтому нормализуем в точках ввода."""
    text = (text or "").strip()
    for sep in ("/", "-", " ", ","):
        text = text.replace(sep, ".")
    while ".." in text:
        text = text.replace("..", ".")
    return text.strip(".")

def is_valid_date(text: str) -> bool:
    """Проверяет формат ДД.ММ.ГГГГ и реальность даты."""
    text = normalize_date(text)
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

def _reduce_keep_master(n: int) -> int:
    """Сводит к однозначному, но сохраняет мастер-числа 11, 22, 33 —
    для тех чисел где они нумерологически значимы (душа, личность, зрелость)."""
    while n > 9 and n not in (11, 22, 33):
        n = _digit_sum(n)
    return n

def _reduce_to_arcana(n: int) -> int:
    """Сводит число не к 1–9, а к диапазону 1–22 — под количество Старших
    Арканов Таро. Это отдельная, более широкая шкала свода: обычные числа
    судьбы/личного года используют классическую пифагорейскую редукцию
    (см. _reduce_to_single), а Аркан — самостоятельный, более образный слой
    поверх неё (см. build_numerology_context)."""
    while n > 22:
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

# ─── АРКАН (образный слой поверх пифагорейских чисел, шкала 1–22) ────────────

def calculate_life_arcana(date_str: str) -> int:
    """Жизненный Аркан — по полной дате рождения, статичный на всю жизнь.
    Та же арифметика что и число судьбы (сумма всех цифр даты), но сведение
    к диапазону 1–22, а не 1–9 — под количество Старших Арканов Таро."""
    digits = date_str.strip().replace(".", "")
    total  = sum(int(c) for c in digits)
    return _reduce_to_arcana(total)

def calculate_year_arcana(date_str: str, year: int | None = None) -> int:
    """Аркан года — тот же принцип что личный год (calculate_personal_year),
    но со сведением к 1–22. Меняется каждый год, используется как заголовок
    годовых прогнозов вместо сухого 'личный год N'."""
    if year is None:
        year = datetime.now().year
    parts = date_str.strip().split(".")
    d, m = int(parts[0]), int(parts[1])
    total = _digit_sum(d) + _digit_sum(m) + _digit_sum(year)
    return _reduce_to_arcana(total)

# Название и краткий архетип каждого Аркана — общественное достояние
# (классическая колода Таро), собственной закрытой методики здесь нет.
# Конвенция нумерации 1–22 (а не 0–21): 22 = Шут — это принятая практика для
# систем на основе сложения цифр даты (в отличие от классической колоды, где
# Шут стоит первым под номером 0 — здесь он замыкает цикл).
ARCANA = {
    1:  ("I",     "Маг",              "воля и начало — ты умеешь превращать замысел в результат"),
    2:  ("II",    "Верховная Жрица",  "интуиция и тайна — ты видишь то, что скрыто от других"),
    3:  ("III",   "Императрица",      "изобилие и творчество — через тебя рождается новое"),
    4:  ("IV",    "Император",        "порядок и власть — ты создаёшь устойчивые структуры"),
    5:  ("V",     "Иерофант",         "традиция и наставничество — ты передаёшь знание дальше"),
    6:  ("VI",    "Влюблённые",       "выбор и союз — твой путь про глубокие, осознанные связи"),
    7:  ("VII",   "Колесница",        "движение и воля — ты побеждаешь через решимость"),
    8:  ("VIII",  "Сила",             "мягкая сила — ты укрощаешь трудности терпением, а не давлением"),
    9:  ("IX",    "Отшельник",        "поиск и мудрость — ответы приходят когда ты остаёшься с собой"),
    10: ("X",     "Колесо Фортуны",   "цикличность и поворот — твоя жизнь движется через переломные моменты"),
    11: ("XI",    "Справедливость",   "баланс и причина-следствие — ты живёшь по своим честным законам"),
    12: ("XII",   "Повешенный",       "новый взгляд — сила приходит через паузу и смену угла зрения"),
    13: ("XIII",  "Смерть",           "трансформация — ты умеешь завершать одно, чтобы начать другое"),
    14: ("XIV",   "Умеренность",      "гармония и мера — твоя сила в умении соединять противоположности"),
    15: ("XV",    "Дьявол",           "искушение и свобода — твой урок в том чтобы не путать привычку с судьбой"),
    16: ("XVI",   "Башня",            "внезапный слом — то что рушится, освобождает место для настоящего"),
    17: ("XVII",  "Звезда",           "надежда и вдохновение — после трудностей к тебе всегда возвращается свет"),
    18: ("XVIII", "Луна",             "интуиция и подсознание — ты чувствуешь то, что нельзя доказать логикой"),
    19: ("XIX",   "Солнце",           "ясность и признание — то что ты прятала, выходит на свет и начинает работать на тебя"),
    20: ("XX",    "Суд",              "пробуждение и итог — ты подводишь черту и слышишь собственный внутренний зов"),
    21: ("XXI",   "Мир",              "завершение и целостность — ты собираешь разрозненное в единую картину"),
    22: ("XXII",  "Шут",              "свобода и новое начало — ты идёшь непроторенным путём, доверяя себе"),
}

def arcana_info(num: int) -> dict:
    roman, name, keyword = ARCANA.get(num, ARCANA[22])
    return {"num": num, "roman": roman, "name": name, "keyword": keyword}

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

_RU_VOWELS = set("аеёиоуыэюя")

def calculate_soul_number(name: str) -> int:
    """Число души — сумма ГЛАСНЫХ имени. Отражает внутренние желания,
    то что движет человеком изнутри. Мастер-числа сохраняются."""
    name = name.lower().strip()
    total = sum(_PYTHAGOREAN_RU.get(ch, 0) for ch in name if ch in _RU_VOWELS)
    if total == 0:
        return 1
    return _reduce_keep_master(total)

def calculate_personality_number(name: str) -> int:
    """Число личности — сумма СОГЛАСНЫХ имени. Отражает то как человека
    видят окружающие, внешнюю маску. Мастер-числа сохраняются."""
    name = name.lower().strip()
    total = sum(
        _PYTHAGOREAN_RU.get(ch, 0)
        for ch in name
        if ch.isalpha() and ch not in _RU_VOWELS
    )
    if total == 0:
        return 1
    return _reduce_keep_master(total)

def calculate_maturity_number(date_str: str, name: str) -> int:
    """Число зрелости — судьба + имя. Раскрывается во второй половине жизни
    (после ~35 лет), показывает к чему человек приходит."""
    base = calculate_destiny(date_str) + calculate_name_number(name)
    return _reduce_keep_master(base)

def calculate_personal_month(date_str: str, year: int | None = None,
                             month: int | None = None) -> int:
    """Число личного месяца = личный год + номер месяца. Даёт реальную
    помесячную динамику, а не выдуманные моделью месяцы."""
    now = datetime.now()
    if year is None:
        year = now.year
    if month is None:
        month = now.month
    py = calculate_personal_year(date_str, year)
    return _reduce_to_single(py + _digit_sum(month))

def calculate_personal_day(date_str: str, on_date: "_date_type | None" = None) -> int:
    """Число личного дня = личный месяц + число дня (сегодня, если on_date
    не передан). Используется точечными уведомлениями premium ('день силы')
    и фичей «Число дня» (personal_day_info)."""
    d = on_date or datetime.now().date()
    pm = calculate_personal_month(date_str, year=d.year, month=d.month)
    return _reduce_to_single(pm + _digit_sum(d.day))

# Смысл каждого числа личного дня: (энергия, короткий совет). Таблица общая
# для утренней рассылки (bot._day_message), фичи «Число дня» и веб-кабинета —
# держим здесь, чтобы бот и веб брали один источник, а не дублировали текст.
DAY_ENERGY = {
    1:  ("энергия начала и лидерства", "день для смелых решений и новых стартов — действуй без промедления"),
    2:  ("энергия интуиции и партнёрства", "день для диалога и прислушивания к себе — доверяй ощущениям"),
    3:  ("энергия творчества и общения", "день для самовыражения и радости — позволь себе яркость"),
    4:  ("энергия порядка и созидания", "день для планирования и дел — всё что начнёшь сегодня будет устойчивым"),
    5:  ("энергия перемен и свободы", "день для нового опыта — открывайся неожиданному"),
    6:  ("энергия любви и гармонии", "день для близких и заботы о себе — это твоя главная задача сегодня"),
    7:  ("энергия мудрости и глубины", "день для размышлений и тишины — ответы уже внутри тебя"),
    8:  ("энергия силы и изобилия", "день для финансовых решений и амбиций — действуй уверенно"),
    9:  ("энергия завершения и мудрости", "день для отпускания старого — освободи место для нового"),
    11: ("энергия вдохновения и интуиции", "особый день — твоё чутьё работает на максимуме"),
    22: ("энергия мастера-строителя", "день для масштабных шагов — делай то что останется надолго"),
    33: ("энергия высшей любви", "день для сострадания и помощи — твои слова сегодня целительны"),
}

def personal_day_info(date_str: str, on_date: "_date_type | None" = None) -> dict:
    """Всё для фичи «Число дня»: само число личного дня + его энергия и совет.
    Считается из даты рождения и текущей даты — ничего хранить не нужно."""
    num = calculate_personal_day(date_str, on_date)
    energy, advice = DAY_ENERGY.get(num, DAY_ENERGY[9])
    return {"number": num, "energy": energy, "advice": advice}

_RU_MONTHS_NOM = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)

def personal_month_table(date_str: str, months_ahead: int = 36) -> str:
    """Таблица чисел личного месяца на months_ahead месяцев вперёд от
    текущего, готовыми значениями. Модель периодически ошибается,
    досчитывая личный месяц сама для месяцев за пределами текущего —
    особенно на стыке личных лет (например декабрь одного личного года
    и январь следующего), когда нужно поменять базу расчёта. Отдаём
    готовые числа, чтобы модели не приходилось считать самой."""
    now = datetime.now()
    year, month = now.year, now.month
    lines = []
    for _ in range(months_ahead):
        pm = calculate_personal_month(date_str, year=year, month=month)
        lines.append(f"{_RU_MONTHS_NOM[month - 1]} {year}: {pm}")
        month += 1
        if month > 12:
            month = 1
            year += 1
    return "\n".join(lines)

def calculate_pinnacles(date_str: str) -> list[int]:
    """4 пика (вершины) жизни — классический расчёт. Каждый пик это
    энергия определённого жизненного периода."""
    parts = date_str.strip().split(".")
    d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
    rd, rm, ry = _reduce_to_single(d), _reduce_to_single(m), _reduce_to_single(_digit_sum(y))
    p1 = _reduce_to_single(rm + rd)
    p2 = _reduce_to_single(rd + ry)
    p3 = _reduce_to_single(p1 + p2)
    p4 = _reduce_to_single(rm + ry)
    return [p1, p2, p3, p4]

def calculate_challenges(date_str: str) -> list[int]:
    """3 главных вызова (испытания) — препятствия которые нужно преодолеть.
    Считаются как разности приведённых частей даты."""
    parts = date_str.strip().split(".")
    d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
    rd, rm, ry = _reduce_to_single(d), _reduce_to_single(m), _reduce_to_single(_digit_sum(y))
    c1 = abs(rm - rd)
    c2 = abs(rd - ry)
    c3 = abs(c1 - c2)
    return [c1, c2, c3]

# ─── ВИЗУАЛЬНАЯ МАТРИЦА СУДЬБЫ (метод Ладини) ───────────────────────────────
# Разные школы (Ладини, Гладков и последователи) сходятся в базовых 4 точках,
# но расходятся в трактовке производных по жизненным сферам — берём один
# консистентный вариант расчёта. Полную трактовку даёт платный разбор
# matrix_full; бесплатная схема (destiny_matrix) — визуальный лид-магнит.

def calculate_matrix_full(date_str: str) -> dict:
    """Полная матрица судьбы — 12 точек (метод Ладини), сведённых в диапазон
    1-22 (Старшие Арканы). Слой 1 (a,b,v,g) — из даты рождения; слои 2-3 —
    производные суммы соседних точек по кругу."""
    parts = date_str.strip().split(".")
    d, m, y = int(parts[0]), int(parts[1]), int(parts[2])

    a = _reduce_to_arcana(d)
    b = _reduce_to_arcana(m)
    v = _reduce_to_arcana(_digit_sum(y))
    g = _reduce_to_arcana(a + b + v)

    dd = _reduce_to_arcana(a + b)
    e  = _reduce_to_arcana(b + v)
    zh = _reduce_to_arcana(v + g)
    z  = _reduce_to_arcana(g + a)

    i  = _reduce_to_arcana(dd + e)
    k  = _reduce_to_arcana(e + zh)
    l  = _reduce_to_arcana(zh + z)
    m_ = _reduce_to_arcana(z + dd)

    return {
        "a": a, "b": b, "v": v, "g": g,
        "d": dd, "e": e, "zh": zh, "z": z,
        "i": i, "k": k, "l": l, "m": m_,
    }

def destiny_matrix(date_str: str) -> dict:
    """Матрица судьбы для веб-визуала: 8 внешних точек звезды (по кругу, каждая
    производная стоит между своими «родителями»), 4 внутренние точки ядра и
    центральный Аркан предназначения (g). К каждому числу — название и краткий
    архетип Аркана (ARCANA), чтобы бесплатная схема сразу что-то говорила."""
    mx = calculate_matrix_full(date_str)
    def pt(num, label):
        info = arcana_info(num)
        return {"num": num, "label": label, "name": info["name"], "keyword": info["keyword"]}
    # Порядок по кругу: каждая производная (d,e,zh,z) стоит между родителями.
    outer = [
        pt(mx["a"],  "День"),
        pt(mx["d"],  "День+Месяц"),
        pt(mx["b"],  "Месяц"),
        pt(mx["e"],  "Месяц+Год"),
        pt(mx["v"],  "Год"),
        pt(mx["zh"], "Год+Предназн."),
        pt(mx["g"],  "Предназначение"),
        pt(mx["z"],  "Предназн.+День"),
    ]
    inner = [pt(mx["i"], "Ядро 1"), pt(mx["k"], "Ядро 2"),
             pt(mx["l"], "Ядро 3"), pt(mx["m"], "Ядро 4")]
    center = pt(mx["g"], "Предназначение")
    return {"outer": outer, "inner": inner, "center": center}

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

_MONTH_ENERGY = {
    1: "начало и инициатива",
    2: "партнёрство и чуткость",
    3: "творчество и общение",
    4: "порядок и труд",
    5: "перемены и свобода",
    6: "забота и гармония",
    7: "анализ и уединение",
    8: "деньги и власть",
    9: "завершение и отдача",
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

_PINNACLE_MEANING = {
    1: "период самостоятельности — учишься опираться на себя, начинаешь новое",
    2: "период отношений и сотрудничества — рядом важны союзники, а не соло-рывки",
    3: "период творчества и общения — энергия идёт через самовыражение и связи",
    4: "период труда и фундамента — то, что строишь сейчас, держит тебя дальше",
    5: "период перемен и движения — новый опыт важнее стабильности",
    6: "период дома и ответственности — в центре семья и забота о близких",
    7: "период внутренней работы — глубина и уединение дают больше, чем суета",
    8: "период результатов — власть, деньги и статус выходят на первый план",
    9: "период завершения — отпускаешь старое, чтобы освободить место новому",
}

_SOUL_MEANING = {
    1: "жажда независимости и признания",
    2: "потребность в любви, гармонии и близости",
    3: "желание самовыражения и радости",
    4: "стремление к порядку и надёжности",
    5: "тяга к свободе и новым впечатлениям",
    6: "потребность заботиться и быть нужной",
    7: "поиск глубины, истины и уединения",
    8: "желание достигать и влиять",
    9: "стремление помогать и отдавать миру",
    11: "потребность вдохновлять и чувствовать тонкое",
    22: "желание построить что-то великое",
    33: "стремление исцелять и любить безусловно",
}

_PERSONALITY_MEANING = {
    1: "сильная, уверенная, лидерская",
    2: "мягкая, тактичная, располагающая",
    3: "яркая, обаятельная, лёгкая",
    4: "надёжная, серьёзная, основательная",
    5: "живая, динамичная, притягательная",
    6: "тёплая, заботливая, домашняя",
    7: "загадочная, сдержанная, глубокая",
    8: "статусная, влиятельная, деловая",
    9: "благородная, мудрая, открытая",
    11: "особенная, харизматичная, чувствующая",
    22: "масштабная, внушающая доверие",
    33: "тёплая, исцеляющая, вдохновляющая",
}

def build_numerology_context(name: str, date_str: str) -> str:
    """Строит текстовый контекст для промпта — числа и их значения.
    Этот текст вставляется в начало каждого промпта через {context}."""
    pos  = _matrix_positions(date_str)
    now  = datetime.now()
    year = now.year

    has_name   = bool(name and name not in ("дорогая", ""))
    name_num   = calculate_name_number(name) if has_name else None
    soul_num   = calculate_soul_number(name) if has_name else None
    pers_num   = calculate_personality_number(name) if has_name else None
    maturity   = calculate_maturity_number(date_str, name) if has_name else None
    pmonth     = calculate_personal_month(date_str)
    pinnacles  = calculate_pinnacles(date_str)
    challenges = calculate_challenges(date_str)

    life_arc = arcana_info(calculate_life_arcana(date_str))
    year_arc = arcana_info(calculate_year_arcana(date_str, year))

    lines = [
        f"Имя: {name}",
        f"Дата рождения: {date_str}",
        f"",
        f"Жизненный Аркан: {life_arc['roman']}. {life_arc['name']} — {life_arc['keyword']}",
        f"Аркан года {year}: {year_arc['roman']}. {year_arc['name']} — {year_arc['keyword']}",
        f"(Аркан — образный архетип поверх чисел. Можешь использовать его название "
        f"как яркий заголовок или образ в начале разбора, но раскрывай суть через "
        f"конкретные числа ниже — Аркан это дополнение, а не замена цифрам.)",
        f"",
        f"Число судьбы (жизненного пути): {pos['destiny']} — {_DESTINY_MEANING.get(pos['destiny'], '')}",
        f"Число дня рождения: {pos['day']}",
        f"Число месяца: {pos['month']}",
        f"Кармическое число: {pos['karmic']} — {_KARMIC_MEANING.get(pos['karmic'], '')}",
        f"Число личного года ({year}): {pos['personal_year']}",
        f"Число личного месяца (сейчас): {pmonth}",
    ]
    if name_num:
        lines.append(f"Число имени '{name}': {name_num}")
    if soul_num:
        lines.append(f"Число души (по гласным): {soul_num} — {_SOUL_MEANING.get(soul_num, '')}")
    if pers_num:
        lines.append(f"Число личности (по согласным): {pers_num} — {_PERSONALITY_MEANING.get(pers_num, '')}")
    if maturity:
        lines.append(f"Число зрелости (вторая половина жизни): {maturity}")

    lines.append(
        f"Пики жизни (4 периода): {pinnacles[0]}, {pinnacles[1]}, {pinnacles[2]}, {pinnacles[3]}"
    )
    lines.append(
        f"Главные вызовы (испытания): {challenges[0]}, {challenges[1]}, {challenges[2]}"
    )

    lines.append("")
    lines.append(
        "Числа личного месяца на 3 года вперёд (готовые значения — "
        "используй именно их для любых месяцев, которые упоминаешь, "
        "не пересчитывай сама):"
    )
    lines.append(personal_month_table(date_str))

    return "\n".join(lines)

def build_name_context(subject: str) -> str:
    """Контекст для разборов, которые считаются по ИМЕНИ/НАЗВАНИЮ, а не по дате
    (разбор имени, нумерология названия бизнеса). Даты нет — только числа,
    выведенные из букв: число имени (все буквы), число души (гласные) и число
    впечатления/личности (согласные). Вставляется в промпт через {context}."""
    n    = calculate_name_number(subject)
    soul = calculate_soul_number(subject)
    pers = calculate_personality_number(subject)
    return "\n".join([
        f"Анализируемое имя/название: «{subject}»",
        f"Число имени (сумма всех букв): {n} — {_DESTINY_MEANING.get(n, '')}",
        f"Число души (по гласным, внутренняя суть): {soul} — {_SOUL_MEANING.get(soul, '')}",
        f"Число впечатления (по согласным, как воспринимают со стороны): {pers} — {_PERSONALITY_MEANING.get(pers, '')}",
    ])

# ─── СТРУКТУРИРОВАННЫЙ КОНТЕКСТ ДЛЯ PDF ──────────────────────────────────────
# Те же данные, что и build_numerology_context, но как dict с готовыми
# карточками — используется в pdf.py для страницы "карта твоих чисел".
# Год ставится в заголовок карточки по умолчанию — 2026, если явный не передан
# (совпадает с логикой build_prompt в bot.py: kwargs.setdefault("year", ...)).

def numerology_summary(name: str, date_str: str) -> dict:
    """Структурированная версия числового портрета — для PDF-карточек.
    Возвращает число судьбы (с заголовком и описанием для обложки), список
    карточек 'число — подпись — короткое описание' и пики/вызовы."""
    pos  = _matrix_positions(date_str)
    now  = datetime.now()
    year = now.year

    pers_month = calculate_personal_month(date_str)
    pers_month_label = f"{_RU_MONTHS_NOM[now.month - 1].capitalize()} {year}"

    has_name = bool(name and name not in ("дорогая", ""))
    name_num = calculate_name_number(name) if has_name else None
    soul_num = calculate_soul_number(name) if has_name else None
    pers_num = calculate_personality_number(name) if has_name else None
    maturity = calculate_maturity_number(date_str, name) if has_name else None
    pinnacles  = calculate_pinnacles(date_str)
    challenges = calculate_challenges(date_str)

    destiny_words = _DESTINY_MEANING.get(pos["destiny"], "")
    destiny_title = " · ".join(w.strip().capitalize() for w in destiny_words.split(","))

    cards = []
    if soul_num is not None:
        cards.append({"value": soul_num, "label": "Число души",
                      "desc": _SOUL_MEANING.get(soul_num, "").capitalize()})
    if pers_num is not None:
        cards.append({"value": pers_num, "label": "Число личности",
                      "desc": (_PERSONALITY_MEANING.get(pers_num, "") + " — так тебя видят окружающие").capitalize()})
    if name_num is not None:
        cards.append({"value": name_num, "label": "Число имени", "desc": "Энергия, заложенная в твоё имя"})
    cards.append({"value": pos["karmic"], "label": "Кармическое",
                  "desc": _KARMIC_MEANING.get(pos["karmic"], "").capitalize()})
    if maturity is not None:
        cards.append({"value": maturity, "label": "Число зрелости", "desc": "К чему приходишь после 35 лет"})
    cards.append({"value": pos["personal_year"], "label": f"Личный год {year}", "desc": "Главная тема этого года"})

    life_arc = arcana_info(calculate_life_arcana(date_str))
    year_arc = arcana_info(calculate_year_arcana(date_str, year))

    _pin_ages = ["0–32", "32–41", "41–50", "50+"]
    pinnacles_info = [
        {"value": p, "age": _pin_ages[i], "desc": _PINNACLE_MEANING.get(p, "").capitalize()}
        for i, p in enumerate(pinnacles)
    ]

    return {
        "destiny":       pos["destiny"],
        "destiny_title": destiny_title,
        "destiny_desc":  f"Число судьбы — {destiny_words}. Это главное число всей твоей матрицы.",
        "cards":         cards[:6],
        "pinnacles":     pinnacles,
        "pinnacles_info": pinnacles_info,
        "challenges":    challenges,
        "personal_month":      pers_month,
        "personal_month_label": pers_month_label,
        "personal_month_desc": _MONTH_ENERGY.get(pers_month, ""),
        "life_arcana":   life_arc,
        "year_arcana":   year_arc,
    }
