# keyboards.py — все inline-клавиатуры бота.
# Зависит от config.py (TITLES, PRICES, PAID_RAZBORY, FREE_ELIGIBLE, UPSELLS).
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import TITLES, PRICES, PAID_RAZBORY, FREE_ELIGIBLE, UPSELLS

CONTACT_URL = "https://t.me/eva_numer"

def check_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я подписалась!", callback_data="check_sub")],
    ])

def date_choice_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Для себя",    callback_data="use_my_date")],
        [InlineKeyboardButton(text="📅 Другая дата", callback_data="use_new_date")],
    ])

def notifications_menu(notifications_on: bool) -> InlineKeyboardMarkup:
    if notifications_on:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔕 Отключить уведомления", callback_data="notif_off")],
            [InlineKeyboardButton(text="🔮 Меню разборов", callback_data="show_menu")],
        ])
    else:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔔 Включить уведомления", callback_data="notif_on")],
            [InlineKeyboardButton(text="🔮 Меню разборов", callback_data="show_menu")],
        ])

def main_menu(user=None) -> InlineKeyboardMarkup:
    buttons = []

    if user and not user.get("free_used"):
        buttons.append([InlineKeyboardButton(
            text="🎁 Бесплатный разбор на выбор",
            callback_data="free_choose"
        )])

    purchased = user.get("purchased", []) if user else []
    if purchased:
        count = len(purchased)
        buttons.append([InlineKeyboardButton(
            text=f"📚 Мои разборы ({count})",
            callback_data="my_readings"
        )])

    buttons.append([InlineKeyboardButton(text="── Выбери тему ──", callback_data="noop")])
    buttons.append([InlineKeyboardButton(text="🔮 Судьба и личность",    callback_data="section_destiny")])
    buttons.append([InlineKeyboardButton(text="💰 Деньги и карьера",     callback_data="section_money")])
    buttons.append([InlineKeyboardButton(text="💑 Любовь и отношения",   callback_data="section_love")])
    buttons.append([InlineKeyboardButton(text="🌙 Здоровье и энергия",   callback_data="section_health")])
    buttons.append([InlineKeyboardButton(text="✨ Прошлое и будущее",    callback_data="section_past")])
    buttons.append([InlineKeyboardButton(
        text="👥 Пригласи подругу — получи ⭐",
        callback_data="ref_promo"
    )])
    buttons.append([InlineKeyboardButton(
        text="🌸 Личный разбор от Евы (за рубли)",
        url=CONTACT_URL
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def free_choose_menu() -> InlineKeyboardMarkup:
    buttons = []
    for key in sorted(FREE_ELIGIBLE):
        title = TITLES.get(key, key)
        price = PRICES.get(key, 0)
        buttons.append([InlineKeyboardButton(
            text=f"{title} ({price} ⭐ — бесплатно)",
            callback_data=f"free_pick_{key}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def section_destiny_menu(user=None) -> InlineKeyboardMarkup:
    purchased = user.get("purchased", []) if user else []
    buttons = []
    items = [
        ("matrix_full",   "🔮 Матрица судьбы — 149 ⭐"),
        ("mission",       "🌟 Предназначение и миссия — 99 ⭐"),
        ("hidden_talents","✨ Скрытые таланты — 79 ⭐"),
        ("strong_weak",   "⚖️ Сильная/слабая сторона — 49 ⭐"),
        ("main_fear",     "😨 Главный страх — 49 ⭐"),
        ("karma",         "🔴 Кармический долг — 99 ⭐"),
        ("forecast_2026", "🗓 Прогноз на 2026 год — 149 ⭐"),
    ]
    for key, label in items:
        prefix = "✅ " if key in purchased else ""
        buttons.append([InlineKeyboardButton(text=prefix + label, callback_data=f"buy_{key}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def section_money_menu(user=None) -> InlineKeyboardMarkup:
    purchased = user.get("purchased", []) if user else []
    buttons = []
    items = [
        ("finance",       "💹 Финансовый прогноз — 99 ⭐"),
        ("wealth_blocks", "🚧 Блоки богатства — 149 ⭐"),
        ("freedom_path",  "🗺 Путь к финансовой свободе — 149 ⭐"),
        ("calling",       "🌠 Призвание — 79 ⭐"),
        ("promotion",     "📈 Повышение — 99 ⭐"),
        ("own_business",  "🏢 Свой бизнес — 99 ⭐"),
        ("career",        "💼 Карьерный путь — 79 ⭐"),
        ("money",         "💰 Денежный код — 79 ⭐"),
        ("days",          "🌙 Сильные и слабые дни — 79 ⭐"),
    ]
    for key, label in items:
        prefix = "✅ " if key in purchased else ""
        buttons.append([InlineKeyboardButton(text=prefix + label, callback_data=f"buy_{key}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def section_love_menu(user=None) -> InlineKeyboardMarkup:
    purchased = user.get("purchased", []) if user else []
    buttons = []
    items = [
        ("compat",   "💑 Совместимость двух людей — 99 ⭐"),
        ("when",     "💘 Когда встретишь того самого — 79 ⭐"),
        ("portrait", "💍 Портрет идеального партнёра — 79 ⭐"),
        ("unlucky",  "💔 Почему не везёт в любви — 49 ⭐"),
        ("ex",       "💔 Вернётся ли бывший — 49 ⭐"),
        ("cold",     "❄️ Почему он охладел — 49 ⭐"),
        ("toxic",    "☠️ Токсичная связь — 79 ⭐"),
        ("lonely",   "😔 Почему ты одинока — 49 ⭐"),
        ("breakup",  "💔 Разбор после расставания — 79 ⭐"),
    ]
    for key, label in items:
        prefix = "✅ " if key in purchased else ""
        buttons.append([InlineKeyboardButton(text=prefix + label, callback_data=f"buy_{key}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def section_health_menu(user=None) -> InlineKeyboardMarkup:
    purchased = user.get("purchased", []) if user else []
    buttons = []
    items = [
        ("health_code",   "💚 Код здоровья — 79 ⭐"),
        ("energy_drain",  "⚡ Что крадёт энергию — 49 ⭐"),
        ("body_message",  "🫀 Послания тела — 49 ⭐"),
        ("stress_number", "😤 Число стресса — 49 ⭐"),
        ("intuition",     "🔮 Интуиция и внутренний голос — 79 ⭐"),
    ]
    for key, label in items:
        prefix = "✅ " if key in purchased else ""
        buttons.append([InlineKeyboardButton(text=prefix + label, callback_data=f"buy_{key}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def section_past_menu(user=None) -> InlineKeyboardMarkup:
    purchased = user.get("purchased", []) if user else []
    buttons = []
    items = [
        ("past_life",     "📜 Прошлые жизни — 99 ⭐"),
        ("future_portal", "🌟 Прогноз на 3 года — 149 ⭐"),
        ("turning_point", "🔄 Поворотные точки судьбы — 79 ⭐"),
        ("ancestor_code", "🌳 Родовой код — 99 ⭐"),
    ]
    for key, label in items:
        prefix = "✅ " if key in purchased else ""
        buttons.append([InlineKeyboardButton(text=prefix + label, callback_data=f"buy_{key}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def my_readings_menu(user: dict) -> InlineKeyboardMarkup:
    purchased = user.get("purchased", [])
    buttons   = []
    for key in purchased:
        title = TITLES.get(key, key)
        buttons.append([InlineKeyboardButton(
            text=f"✅ {title}",
            callback_data=f"buy_{key}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def upsell_menu(key: str, user: dict) -> InlineKeyboardMarkup:
    buttons     = []
    suggestions = UPSELLS.get(key, ())
    for s in suggestions:
        if s not in user.get("purchased", []):
            title = TITLES.get(s, s)
            price = PRICES.get(s, 49)
            buttons.append([InlineKeyboardButton(
                text=f"{title} — {price} ⭐",
                callback_data=f"buy_{s}"
            )])
    reviews_left = user.get("reviews_left", [])
    if key not in reviews_left:
        buttons.append([InlineKeyboardButton(
            text="😍 Оставить отзыв",
            callback_data=f"leave_review_{key}"
        )])
    buttons.append([InlineKeyboardButton(text="🔮 Все разборы", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def retry_menu(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Попробовать ещё раз", callback_data=f"buy_{key}")],
        [InlineKeyboardButton(text="🔮 Меню", callback_data="show_menu")],
    ])

def coupon_razboy_menu(code: str, user: dict = None) -> InlineKeyboardMarkup:
    purchased = user.get("purchased", []) if user else []
    buttons   = []
    for key, title in PAID_RAZBORY.items():
        prefix = "✅ " if key in purchased else ""
        buttons.append([InlineKeyboardButton(
            text=prefix + title,
            callback_data=f"coupon::{code}::{key}"
        )])
    buttons.append([InlineKeyboardButton(text="🔮 Меню разборов", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def notif_off_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔕 Отключить уведомления", callback_data="notif_off")],
        [InlineKeyboardButton(text="🔮 Меню разборов",          callback_data="show_menu")],
    ])
