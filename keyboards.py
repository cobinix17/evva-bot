# keyboards.py — все inline-клавиатуры бота.
# Зависит от config.py (TITLES, PRICES, PAID_RAZBORY, FREE_ELIGIBLE, UPSELLS).
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import TITLES, PRICES, PAID_RAZBORY, FREE_ELIGIBLE, UPSELLS, YOOKASSA_SHOP_ID, rub_price

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

def main_menu(user=None, is_admin=False, is_premium=False) -> InlineKeyboardMarkup:
    buttons = []

    if user and not user.get("free_used"):
        buttons.append([InlineKeyboardButton(
            text="🎁 Бесплатный разбор на выбор",
            callback_data="free_choose"
        )])

    if is_premium:
        buttons.append([InlineKeyboardButton(
            text="💎 Премиум активен — все разборы открыты",
            callback_data="premium_info"
        )])
        buttons.append([InlineKeyboardButton(
            text="💬 Спросить Еву",
            callback_data="ask_eva"
        )])
    else:
        buttons.append([InlineKeyboardButton(
            text="💎 Ева Премиум — 30 разборов за 399 ⭐/мес",
            callback_data="premium_info"
        )])

    buttons.append([InlineKeyboardButton(text="── Выбери тему ──", callback_data="noop")])
    buttons.append([InlineKeyboardButton(text="🔮 Судьба и личность",    callback_data="section_destiny")])
    buttons.append([InlineKeyboardButton(text="💰 Деньги и карьера",     callback_data="section_money")])
    buttons.append([InlineKeyboardButton(text="💑 Любовь и отношения",   callback_data="section_love")])
    buttons.append([InlineKeyboardButton(text="🌙 Здоровье и энергия",   callback_data="section_health")])
    buttons.append([InlineKeyboardButton(text="✨ Прошлое и будущее",    callback_data="section_past")])
    buttons.append([InlineKeyboardButton(
        text="👤 Мой профиль",
        callback_data="my_profile"
    )])
    buttons.append([InlineKeyboardButton(
        text="🌸 Личный разбор от Евы (за рубли)",
        url=CONTACT_URL
    )])
    buttons.append([InlineKeyboardButton(
        text="💡 Предложить идею / сообщить об ошибке",
        callback_data="feedback_start"
    )])
    if is_admin:
        buttons.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика",        callback_data="admin_stats")],
        [InlineKeyboardButton(text="📨 Рассылка",          callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🎟 Создать купон",     callback_data="admin_coupon_create")],
        [InlineKeyboardButton(text="📋 Список купонов",    callback_data="admin_coupon_list")],
        [InlineKeyboardButton(text="👥 Рефералы",          callback_data="admin_refs")],
        [InlineKeyboardButton(text="🔍 Найти пользователя", callback_data="admin_find_user")],
        [InlineKeyboardButton(text="📝 Отзывы на модерации", callback_data="admin_reviews")],
        [InlineKeyboardButton(text="💡 Обратная связь",     callback_data="admin_feedback")],
        [InlineKeyboardButton(text="⬅️ Главное меню",      callback_data="show_menu")],
    ])

def profile_menu(notifications_on: bool, purchased_count: int = 0) -> InlineKeyboardMarkup:
    """Единая точка входа во всё, что раньше было раскидано по главному меню
    и отдельным командам: мои разборы, рефералка, промокод, подарок, имя,
    уведомления."""
    notif_text = "🔕 Отключить уведомления" if notifications_on else "🔔 Включить уведомления"
    notif_cb   = "notif_off" if notifications_on else "notif_on"
    buttons = []
    if purchased_count:
        buttons.append([InlineKeyboardButton(
            text=f"📚 Мои разборы ({purchased_count})",
            callback_data="my_readings"
        )])
    buttons += [
        [InlineKeyboardButton(text="👥 Реферальная ссылка", callback_data="ref_promo")],
        [InlineKeyboardButton(text="🎁 Ввести промокод", callback_data="promo_start")],
        [InlineKeyboardButton(text="🎁 Подарить разбор подруге", callback_data="gift_start")],
        [InlineKeyboardButton(text="✏️ Изменить имя", callback_data="name_start")],
        [InlineKeyboardButton(text=notif_text, callback_data=notif_cb)],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="show_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def premium_subscribe_menu(invoice_url: str, rub_price: int | None = None) -> InlineKeyboardMarkup:
    """Кнопка оплаты подписки — ведёт на invoice-ссылку с subscription_period,
    Telegram сам оформит рекуррентное списание раз в месяц. Вторая кнопка
    (если передан rub_price) — оплата рублями через прямой API ЮKassa (см.
    premium_pay_rub_cb в bot.py): там это РАЗОВЫЙ платёж на месяц, не
    автопродление (в отличие от Stars-подписки), поэтому подписан отдельным
    текстом. callback, не url — сначала нужно спросить email для чека."""
    buttons = [[InlineKeyboardButton(text="💎 Оформить за 399 ⭐/мес", url=invoice_url)]]
    if rub_price:
        buttons.append([InlineKeyboardButton(
            text=f"💳 Картой — {rub_price}₽ (на месяц)",
            callback_data="premium_pay_rub_card"
        )])
        buttons.append([InlineKeyboardButton(
            text=f"📱 СБП / QR — {rub_price}₽ (на месяц)",
            callback_data="premium_pay_rub_sbp"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def premium_active_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔮 К разборам", callback_data="show_menu")],
    ])

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

def _section_menu(keys_with_emoji: list[tuple[str, str]], user=None, gift: bool = False) -> InlineKeyboardMarkup:
    """Строит меню раздела из (ключ, 'эмодзи Название'), а цену берёт
    ЖИВЬЁМ из config.PRICES — раньше цена дублировалась текстом в каждом
    пункте вручную и могла разъехаться с той, что реально списывается.
    gift=True — та же структура, но callback ведёт в подарочную покупку
    (gift_{key}) вместо обычной (buy_{key}), для /gift-флоу."""
    purchased = user.get("purchased", []) if user else []
    prefix_cb = "gift_" if gift else "buy_"
    buttons   = []
    for key, emoji_title in keys_with_emoji:
        prefix = ("✅ " if key in purchased else "") if not gift else ""
        price  = PRICES.get(key, 49)
        price_line = f"{price} ⭐"
        if YOOKASSA_SHOP_ID and not gift:
            price_line += f" / {rub_price(price)}₽"
        buttons.append([InlineKeyboardButton(
            text=f"{prefix}{emoji_title} — {price_line}",
            callback_data=f"{prefix_cb}{key}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def section_destiny_menu(user=None, gift: bool = False) -> InlineKeyboardMarkup:
    items = [
        ("matrix_full",    "🔮 Матрица судьбы"),
        ("mission",        "🌟 Предназначение и миссия"),
        ("hidden_talents", "✨ Скрытые таланты"),
        ("strong_weak",    "⚖️ Сильная/слабая сторона"),
        ("main_fear",      "😨 Главный страх"),
        ("karma",          "🔴 Кармический долг"),
        ("forecast_2026",  "🗓 Прогноз на 2026 год"),
    ]
    return _section_menu(items, user, gift)

def section_money_menu(user=None, gift: bool = False) -> InlineKeyboardMarkup:
    items = [
        ("finance",       "💹 Финансовый прогноз"),
        ("wealth_blocks", "🚧 Блоки богатства"),
        ("freedom_path",  "🗺 Путь к финансовой свободе"),
        ("calling",       "🌠 Призвание"),
        ("promotion",     "📈 Повышение"),
        ("own_business",  "🏢 Свой бизнес"),
        ("career",        "💼 Карьерный путь"),
        ("money",         "💰 Денежный код"),
        ("days",          "🌙 Сильные и слабые дни"),
    ]
    return _section_menu(items, user, gift)

def section_love_menu(user=None, gift: bool = False) -> InlineKeyboardMarkup:
    items = [
        ("compat",   "💑 Совместимость двух людей"),
        ("when",     "💘 Когда встретишь того самого"),
        ("portrait", "💍 Портрет идеального партнёра"),
        ("unlucky",  "💔 Почему не везёт в любви"),
        ("ex",       "💔 Вернётся ли бывший"),
        ("cold",     "❄️ Почему он охладел"),
        ("toxic",    "☠️ Токсичная связь"),
        ("lonely",   "😔 Почему ты одинока"),
        ("breakup",  "💔 Разбор после расставания"),
    ]
    return _section_menu(items, user, gift)

def section_health_menu(user=None, gift: bool = False) -> InlineKeyboardMarkup:
    items = [
        ("health_code",   "💚 Код здоровья"),
        ("energy_drain",  "⚡ Что крадёт энергию"),
        ("body_message",  "🫀 Послания тела"),
        ("stress_number", "😤 Число стресса"),
        ("intuition",     "🔮 Интуиция и внутренний голос"),
    ]
    return _section_menu(items, user, gift)

def section_past_menu(user=None, gift: bool = False) -> InlineKeyboardMarkup:
    items = [
        ("past_life",     "📜 Прошлые жизни"),
        ("future_portal", "🌟 Прогноз на 3 года"),
        ("turning_point", "🔄 Поворотные точки судьбы"),
        ("ancestor_code", "🌳 Родовой код"),
    ]
    return _section_menu(items, user, gift)

def gift_sections_menu() -> InlineKeyboardMarkup:
    """Выбор раздела для подарка — те же 5 тем, что и в обычном меню,
    но каждая кнопка ведёт в giftsection_* вместо section_*."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔮 Судьба и личность",  callback_data="giftsection_destiny")],
        [InlineKeyboardButton(text="💰 Деньги и карьера",   callback_data="giftsection_money")],
        [InlineKeyboardButton(text="💑 Любовь и отношения", callback_data="giftsection_love")],
        [InlineKeyboardButton(text="🌙 Здоровье и энергия", callback_data="giftsection_health")],
        [InlineKeyboardButton(text="✨ Прошлое и будущее",  callback_data="giftsection_past")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="show_menu")],
    ])

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

def retry_menu(key: str, is_free: bool = False) -> InlineKeyboardMarkup:
    retry_callback = f"free_pick_{key}" if is_free else f"buy_{key}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Попробовать ещё раз", callback_data=retry_callback)],
        [InlineKeyboardButton(text="🔮 Меню", callback_data="show_menu")],
    ])

def balance_pay_menu(key: str, price: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ Оплатить балансом ({price} ⭐)", callback_data=f"balance_buy_{key}")],
        [InlineKeyboardButton(text="💳 Оплатить звёздами Telegram", callback_data=f"stars_buy_{key}")],
    ])

def payment_choice_menu(key: str, price: int, price_rub: int | None = None,
                         balance: int = 0) -> InlineKeyboardMarkup:
    """Выбор способа оплаты разбора — показывается когда есть хотя бы один
    вариант помимо обычной звёздной оплаты (баланс или рубли), иначе просто
    сразу шлют звёздный invoice без лишнего экрана выбора. Карта и СБП/QR —
    отдельные кнопки (сразу ведут на нужный способ на странице ЮKassa),
    а не один пункт с выбором внутри."""
    buttons = []
    if balance >= price:
        buttons.append([InlineKeyboardButton(text=f"⭐ Оплатить балансом ({price} ⭐)", callback_data=f"balance_buy_{key}")])
    buttons.append([InlineKeyboardButton(text=f"⭐ Оплатить {price} звёзд Telegram", callback_data=f"stars_buy_{key}")])
    if price_rub:
        buttons.append([InlineKeyboardButton(text=f"💳 Картой — {price_rub}₽", callback_data=f"rub_card_buy_{key}")])
        buttons.append([InlineKeyboardButton(text=f"📱 СБП / QR — {price_rub}₽", callback_data=f"rub_sbp_buy_{key}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

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
