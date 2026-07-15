# config.py — все константы бота: названия, цены, апселлы, описания разборов
# Синхронизировано с актуальным bot.py.

TITLES = {
    "free":           "💫 Матрица судьбы",
    "matrix_full":    "🔮 Матрица судьбы",
    "finance":        "💹 Финансовый прогноз",
    "wealth_blocks":  "🚧 Блоки богатства",
    "freedom_path":   "🗺 Путь к свободе",
    "calling":        "🌠 Призвание",
    "promotion":      "📈 Повышение",
    "own_business":   "🏢 Свой бизнес",
    "hidden_talents": "✨ Скрытые таланты",
    "main_fear":      "😨 Главный страх",
    "forecast_2026":  "🗓 Прогноз на 2026 год",
    "strong_weak":    "⚖️ Сильная и слабая сторона",
    "compat":         "💑 Совместимость двух людей",
    "when":           "💘 Когда встретишь того самого",
    "portrait":       "💍 Портрет идеального партнёра",
    "unlucky":        "💔 Почему не везёт в любви",
    "mission":        "🌟 Предназначение и миссия",
    "karma":          "🔴 Кармический долг",
    "career":         "💼 Карьерный путь",
    "money":          "💰 Денежный код",
    "days":           "🌙 Сильные и слабые дни",
    "ex":             "💔 Вернётся ли бывший",
    "cold":           "❄️ Почему он охладел",
    "toxic":          "☠️ Токсичная или кармическая связь",
    "lonely":         "😔 Почему ты одинока",
    "breakup":        "💔 Разбор после расставания",
    "health_code":    "💚 Код здоровья",
    "energy_drain":   "⚡ Что крадёт энергию",
    "body_message":   "🫀 Послания тела",
    "stress_number":  "😤 Число стресса",
    "intuition":      "🔮 Интуиция и внутренний голос",
    "past_life":      "📜 Прошлые жизни",
    "future_portal":  "🌟 Прогноз на 3 года",
    "turning_point":  "🔄 Поворотные точки судьбы",
    "ancestor_code":  "🌳 Родовой код",
}

PRICES = {
    "matrix_full":   149,
    "forecast_2026": 149,
    "wealth_blocks": 149,
    "freedom_path":  149,
    "mission":       99,
    "karma":         99,
    "compat":        99,
    "own_business":  99,
    "finance":       99,
    "promotion":     99,
    "calling":       79,
    "career":        79,
    "money":         79,
    "when":          79,
    "portrait":      79,
    "breakup":       79,
    "toxic":         79,
    "hidden_talents":79,
    "days":          79,
    "unlucky":       49,
    "ex":            49,
    "cold":          49,
    "lonely":        49,
    "main_fear":     49,
    "strong_weak":   49,
    "health_code":   79,
    "energy_drain":  49,
    "body_message":  49,
    "stress_number": 49,
    "intuition":     79,
    "past_life":     99,
    "future_portal": 149,
    "turning_point": 79,
    "ancestor_code": 99,
}

PAID_RAZBORY  = {k: v for k, v in TITLES.items() if k != "free"}

# Разборы которые могут быть бесплатными (до 99⭐ включительно)
FREE_ELIGIBLE = {k for k, v in PRICES.items() if v <= 99}

UPSELLS = {
    "matrix_full":    ("forecast_2026", "mission"),
    "forecast_2026":  ("matrix_full",   "karma"),
    "finance":        ("wealth_blocks",  "freedom_path"),
    "wealth_blocks":  ("finance",        "own_business"),
    "freedom_path":   ("calling",        "own_business"),
    "calling":        ("career",         "own_business"),
    "career":         ("promotion",      "money"),
    "money":          ("finance",        "wealth_blocks"),
    "karma":          ("mission",        "matrix_full"),
    "mission":        ("karma",          "hidden_talents"),
    "hidden_talents": ("calling",        "strong_weak"),
    "promotion":      ("career",         "own_business"),
    "own_business":   ("freedom_path",   "finance"),
    "compat":         ("when",           "portrait"),
    "when":           ("portrait",       "compat"),
    "portrait":       ("when",           "unlucky"),
    "unlucky":        ("ex",             "lonely"),
    "ex":             ("toxic",          "compat"),
    "cold":           ("toxic",          "ex"),
    "toxic":          ("cold",           "breakup"),
    "lonely":         ("unlucky",        "portrait"),
    "breakup":        ("ex",             "toxic"),
    "days":           ("finance",        "forecast_2026"),
    "strong_weak":    ("hidden_talents", "main_fear"),
    "main_fear":      ("strong_weak",    "karma"),
    "health_code":    ("energy_drain",   "intuition"),
    "energy_drain":   ("health_code",    "stress_number"),
    "body_message":   ("energy_drain",   "health_code"),
    "stress_number":  ("energy_drain",   "body_message"),
    "intuition":      ("health_code",    "past_life"),
    "past_life":      ("ancestor_code",  "karma"),
    "future_portal":  ("turning_point",  "forecast_2026"),
    "turning_point":  ("future_portal",  "past_life"),
    "ancestor_code":  ("past_life",      "karma"),
}

# Разделы меню
SECTION_DESTINY = ["matrix_full", "mission", "hidden_talents", "strong_weak", "main_fear", "karma", "forecast_2026"]
SECTION_MONEY   = ["finance", "wealth_blocks", "freedom_path", "calling", "promotion", "own_business", "career", "money", "days"]
SECTION_LOVE    = ["compat", "when", "portrait", "unlucky", "ex", "cold", "toxic", "lonely", "breakup"]
SECTION_HEALTH  = ["health_code", "energy_drain", "body_message", "stress_number", "intuition"]
SECTION_PAST    = ["past_life", "future_portal", "turning_point", "ancestor_code"]

# Короткое описание того, что получит человек в разборе — показывается
# сразу после выбора темы, перед запросом даты, чтобы было понятнее за что
# платишь до того как вводить дату рождения.
ADMIN_ID = 5854618444
REF_BONUS_PERCENT = 25  # % от суммы покупки реферала, начисляется в виртуальных звёздах

# ─── ПРЕМИУМ-ПОДПИСКА (Telegram Stars, рекуррентная) ─────────────────────────
# Подписка списывает звёзды раз в месяц автоматически. PREMIUM_PERIOD —
# единственный разрешённый Telegram период для Stars-подписок (ровно 30 суток).
PREMIUM_PRICE         = 399        # ⭐ в месяц
PREMIUM_PERIOD        = 2592000    # 30*24*60*60 — обязательное значение для Stars
# С даты ниже цена для НОВЫХ подписчиков поднимется — у кого уже активна
# подписка по старой цене, останутся на ней навсегда (Telegram привязывает
# цену к конкретному invoice в момент оформления, а не читает её заново).
# Показывается в оффере как стимул оформить пораньше. Когда дата наступит —
# просто подними PREMIUM_PRICE и убери/обнови этот блок.
PREMIUM_PRICE_INCREASE      = 499
PREMIUM_PRICE_INCREASE_DATE = "1 октября 2026"  # полный запуск конец июля — весь август и сентябрь по старой цене
PREMIUM_DAILY_LIMIT   = 5          # сколько НОВЫХ разборов открывать в день
PREMIUM_MONTHLY_LIMIT = 30         # сколько НОВЫХ разборов открывать за месяц подписки
PREMIUM_PAYLOAD       = "premium_sub"
PREMIUM_TITLE         = "💎 Ева Премиум"
ASK_DAILY_LIMIT       = 10         # сколько вопросов Еве премиум может задать в день
FOLLOWUP_LIMIT        = 3          # бесплатных уточняющих вопросов по КАЖДОМУ купленному разбору

# ─── ЮKASSA ЧЕРЕЗ TELEGRAM PAYMENTS (оплата рублями) ─────────────────────────
# Подключено через BotFather (Payments → Connect ЮKassa), НЕ напрямую через
# API ЮKassa — значит платёж идёт тем же путём, что и Stars: send_invoice
# + тот же successful_payment хендлер. Никакого отдельного вебхука не нужно,
# Telegram сам всё проверяет на своей стороне.
import os as _os
YOOKASSA_PROVIDER_TOKEN = _os.getenv("YOOKASSA_PROVIDER_TOKEN", "")
# Фиксированный курс ⭐→₽ — цены в рублях считаются из уже заданных цен в Stars,
# чтобы не вести два независимых прайс-листа. При желании сделать вручную
# заданные рублёвые цены — замени формулу на словарь.
# 1.8₽ — реальная цена звезды по актуальным данным (июль 2026): официальный
# @PremiumBot ~1.8₽/⭐, маркетплейсы-перепродавцы ~1.6-1.8₽/⭐, сторы с
# наценкой 30% ~2.6-3₽/⭐ — 1.8 честно отражает то, что люди реально платят.
STARS_TO_RUB_RATE = 1.8

def rub_price(price_stars: int) -> int:
    """Округляем до десятков рублей — ровные цифры выглядят опрятнее."""
    return round(price_stars * STARS_TO_RUB_RATE / 10) * 10

PREMIUM_PRICE_RUB = rub_price(PREMIUM_PRICE)  # 399⭐ → 640₽

RAZBOR_DESCRIPTIONS = {
    "matrix_full":   "Полная картина личности: характер, таланты, деньги, любовь, карма и предназначение в одном разборе.",
    "finance":       "Когда ждать рост доходов, какие источники сработают и чего избегать в деньгах.",
    "wealth_blocks": "Какие внутренние блоки мешают разбогатеть и как их снять.",
    "freedom_path":  "Конкретный путь к финансовой независимости — через найм, бизнес или творчество.",
    "calling":       "Твоё истинное призвание и как превратить его в доход.",
    "promotion":     "Лучшее время для повышения и как себя показать руководству.",
    "own_business":  "Подходит ли тебе своё дело, в какой нише и когда стартовать.",
    "hidden_talents":"Скрытые способности, которые ты недооцениваешь — и как их монетизировать.",
    "main_fear":     "Главный страх, который тормозит твою жизнь, и как от него освободиться.",
    "forecast_2026": "Подробный прогноз на 2026 год: любовь, деньги, рост, лучшие месяцы.",
    "strong_weak":   "Твои сильные и слабые стороны — честно и по числам.",
    "compat":        "Совместимость с конкретным человеком: сильные стороны пары и зоны риска.",
    "when":          "Когда встретишь своего человека и каким он будет.",
    "portrait":      "Нумерологический портрет твоего идеального партнёра.",
    "unlucky":       "Истинная причина неудач в любви и как разорвать паттерн.",
    "mission":       "Твоя жизненная миссия — для чего ты пришла в этот мир.",
    "karma":         "Кармический долг этой жизни и как его закрыть.",
    "career":        "Идеальный карьерный путь именно для твоих чисел.",
    "money":         "Твой денежный код: как приходят и уходят деньги, и что это активирует.",
    "days":          "Сильные и слабые дни месяца — когда действовать, когда отдыхать.",
    "ex":            "Вернётся ли бывший — прямой ответ по числам.",
    "cold":          "Почему партнёр охладел и что с этим делать.",
    "toxic":         "Токсичная связь или кармический урок — точный диагноз.",
    "lonely":        "Истинная причина одиночества и как её изменить.",
    "breakup":       "Разбор расставания: что произошло и что ждёт дальше.",
    "health_code":   "Твой код здоровья — природная конституция и слабые места.",
    "energy_drain":  "Что крадёт твою энергию и как её восстановить.",
    "body_message":  "Что тело пытается сказать через симптомы и состояния.",
    "stress_number": "Как ты реагируешь на стресс и что реально помогает.",
    "intuition":     "Насколько сильна твоя интуиция и как её развить.",
    "past_life":     "Прошлые жизни и их след в нынешней судьбе.",
    "future_portal": "Подробный прогноз на ближайшие 3 года.",
    "turning_point": "Когда наступит следующий поворотный момент судьбы.",
    "ancestor_code": "Родовые программы — что досталось от предков и как это использовать.",
}
 