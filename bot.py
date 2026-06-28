import os
import re
import time
import logging
import asyncio
import json
import random
from datetime import datetime, date, timedelta, timezone
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    TelegramObject, BufferedInputFile
)
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

from readings import PROMPTS
from broadcasts import MORNING

import db
from config import TITLES, PRICES, PDF_KEYS, PAID_RAZBORY, FREE_ELIGIBLE, RAZBOR_DESCRIPTIONS, ADMIN_ID
from ai import ask_ai
from pdf import generate_pdf
from numerology import (
    calculate_destiny, calculate_day_number, is_valid_date,
    build_numerology_context,
)
from keyboards import (
    check_menu, date_choice_menu, notifications_menu, main_menu,
    free_choose_menu, section_destiny_menu, section_money_menu,
    section_love_menu, section_health_menu, section_past_menu,
    my_readings_menu, upsell_menu, retry_menu, coupon_razboy_menu,
    notif_off_menu,
)

BOT_TOKEN    = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not DATABASE_URL:
    raise EnvironmentError("BOT_TOKEN и DATABASE_URL должны быть установлены!")
if not any([os.getenv("CEREBRAS_API_KEY"), os.getenv("GROQ_API_KEY"), os.getenv("OPENROUTER_API_KEY")]):
    raise EnvironmentError(
        "Нужен хотя бы один ИИ-провайдер: CEREBRAS_API_KEY, GROQ_API_KEY или OPENROUTER_API_KEY."
    )

CHANNEL         = "@eva_numerologg"
REVIEWS_CHANNEL = "@eva_numerolog_otz"

logging.basicConfig(level=logging.INFO)
# fontTools.subset выводит десятки строк уровня INFO на каждую вставку шрифта
# в PDF (список ID глифов и т.п.) — это не ошибки, но они забивают логи
# Railway и прячут реальные warning/error. Поднимаем порог только для этого
# модуля, остальное логирование бота не трогаем.
logging.getLogger("fontTools.subset").setLevel(logging.WARNING)

bot     = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)

# ─── АНТИФЛУД MIDDLEWARE ─────────────────────────────────────────────────────
class AntiFloodMiddleware(BaseMiddleware):
    def __init__(self, timeout: float = 3.0):
        self.timeout      = timeout
        self.last_request = {}
        self._call_count  = 0

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user:
            now  = time.time()
            last = self.last_request.get(user.id, 0)
            if now - last < self.timeout:
                if isinstance(event, Message):
                    await event.answer("⏳ Не так быстро! Подожди пару секунд.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("⏳ Подожди немного!", show_alert=False)
                return
            self.last_request[user.id] = now
            self._call_count += 1
            if self._call_count >= 500:
                self._call_count = 0
                cutoff = now - 60
                old    = [uid for uid, t in self.last_request.items() if t < cutoff]
                for uid in old:
                    del self.last_request[uid]
        return await handler(event, data)

dp.message.middleware(AntiFloodMiddleware(3.0))
dp.callback_query.middleware(AntiFloodMiddleware(1.0))

# ─── РАЗБИВКА ДЛИННЫХ СООБЩЕНИЙ ──────────────────────────────────────────────
async def send_long(chat_id, text: str):
    limit = 4000
    if len(text) <= limit:
        await bot.send_message(chat_id, text)
        return
    parts = []
    while len(text) > limit:
        split_at = text.rfind('\n', 0, limit)
        if split_at == -1:
            split_at = limit
        parts.append(text[:split_at])
        text = text[split_at:].lstrip()
    if text:
        parts.append(text)
    for part in parts:
        await bot.send_message(chat_id, part)
        await asyncio.sleep(0.3)

# ─── FSM ─────────────────────────────────────────────────────────────────────
class Form(StatesGroup):
    waiting_name        = State()
    waiting_birth_date  = State()
    waiting_date        = State()
    waiting_second_date = State()
    waiting_review      = State()
    waiting_free_date   = State()

# ─── ЗАМОК ГЕНЕРАЦИИ ─────────────────────────────────────────────────────────
# Один платный разбор за раз на пользователя. Защищает от параллельного
# запуска двух генераций (юзер во время 90-сек ожидания уходит в меню и
# запускает второй разбор) — это удваивает расход токенов и рассинхронизирует
# поле waiting. Храним в памяти процесса: бот однопроцессный (один polling),
# поэтому простого set достаточно — внешний стор (Redis) не нужен.
_generating: set[int] = set()

# ─── ВСПОМОГАТЕЛЬНЫЕ ─────────────────────────────────────────────────────────
async def check_subscription(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def build_prompt(key: str, **kwargs) -> str:
    """Собирает промпт по ключу. Бросает ValueError если ключ не найден."""
    kwargs.setdefault("year", datetime.now().year)
    template = PROMPTS.get(key)
    if not template:
        logging.error(f"build_prompt: промпт не найден для ключа '{key}'")
        raise ValueError(f"Промпт '{key}' не существует в PROMPTS")
    return template.format(**kwargs)

_NAME_ALLOWED_RE = re.compile(r"[^а-яёА-ЯЁa-zA-Z\s\-]")

def sanitize_name(raw: str) -> str:
    """Имя пользователя идёт прямо в промпт через {name}. Без очистки
    юзер может ввести 'имя' с инструкциями для ИИ (prompt injection) или
    переводами строк, ломающими структуру промпта. Оставляем только буквы,
    пробел и дефис, схлопываем пробелы, режем длину."""
    cleaned = _NAME_ALLOWED_RE.sub("", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:30]

# ─── УВЕДОМЛЕНИЯ ─────────────────────────────────────────────────────────────
@dp.message(Command("notifications"), StateFilter("*"))
async def notifications_cmd(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    notif_on = user.get("notifications", True)
    status = "включены 🔔" if notif_on else "отключены 🔕"
    await message.answer(
        f"Утренние уведомления сейчас {status}.\n\nУправляй настройкой 👇",
        reply_markup=notifications_menu(notif_on)
    )

@dp.callback_query(F.data == "notif_off")
async def notif_off(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    user["notifications"] = False
    await db.save_user(callback.from_user.id, user)
    await callback.answer("🔕 Уведомления отключены", show_alert=True)
    await callback.message.answer(
        "🔕 Утренние уведомления отключены.\n\nВключить обратно: /notifications",
        reply_markup=notif_off_menu()
    )

@dp.callback_query(F.data == "notif_on")
async def notif_on(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    user["notifications"] = True
    await db.save_user(callback.from_user.id, user)
    await callback.answer("🔔 Уведомления включены!", show_alert=True)
    await callback.message.answer(
        "🔔 Утренние уведомления включены!\n\nКаждое утро буду присылать нумерологический прогноз 🌅",
        reply_markup=notif_off_menu()
    )

# ─── РАЗДЕЛЫ МЕНЮ ────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "section_destiny")
async def section_destiny(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    await callback.message.answer("🔮 Судьба и личность — выбери разбор:", reply_markup=section_destiny_menu(user))
    await callback.answer()

@dp.callback_query(F.data == "section_money")
async def section_money(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    await callback.message.answer("💰 Деньги и карьера — выбери разбор:", reply_markup=section_money_menu(user))
    await callback.answer()

@dp.callback_query(F.data == "section_love")
async def section_love(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    await callback.message.answer("💑 Любовь и отношения — выбери разбор:", reply_markup=section_love_menu(user))
    await callback.answer()

@dp.callback_query(F.data == "section_health")
async def section_health(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    await callback.message.answer("🌙 Здоровье и энергия — выбери разбор:", reply_markup=section_health_menu(user))
    await callback.answer()

@dp.callback_query(F.data == "section_past")
async def section_past(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    await callback.message.answer("✨ Прошлое и будущее — выбери разбор:", reply_markup=section_past_menu(user))
    await callback.answer()

# ─── МОИ РАЗБОРЫ ─────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "my_readings")
async def my_readings(callback: CallbackQuery):
    user      = await db.get_user(callback.from_user.id)
    purchased = user.get("purchased", [])
    if not purchased:
        await callback.answer("У тебя пока нет купленных разборов 🔮", show_alert=True)
        return
    await callback.message.answer(
        f"📚 Твои разборы ({len(purchased)}) — нажми на любой чтобы получить снова 👇",
        reply_markup=my_readings_menu(user)
    )
    await callback.answer()

# ─── БЕСПЛАТНЫЙ РАЗБОР НА ВЫБОР ─────────────────────────────────────────────
@dp.callback_query(F.data == "free_choose")
async def free_choose_handler(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not user["subscribed_channel"]:
        is_sub = await check_subscription(callback.from_user.id)
        if not is_sub:
            await callback.message.answer(
                f"💫 Подпишись на {CHANNEL} чтобы получить бесплатный разбор 👇",
                reply_markup=check_menu()
            )
            await callback.answer()
            return
        user["subscribed_channel"] = True
        await db.save_user(callback.from_user.id, user)

    if user["free_used"]:
        await callback.answer("Бесплатный разбор уже использован 🔮", show_alert=True)
        return

    if not user.get("first_name"):
        await callback.message.answer("✨ Как мне тебя называть? Введи своё имя 👇")
        await state.set_state(Form.waiting_name)
        await callback.answer()
        return

    await callback.message.answer(
        "🎁 Выбери любой разбор — он будет бесплатным!\n\n"
        "Это твой подарок за подписку на канал 💫",
        reply_markup=free_choose_menu()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("free_pick_"))
async def free_pick_handler(callback: CallbackQuery, state: FSMContext):
    key  = callback.data.replace("free_pick_", "")
    user = await db.get_user(callback.from_user.id)

    if user["free_used"]:
        await callback.answer("Бесплатный разбор уже использован!", show_alert=True)
        return

    if key not in FREE_ELIGIBLE:
        await callback.answer("Этот разбор не входит в бесплатные!", show_alert=True)
        return

    user["waiting"] = key
    await db.save_user(callback.from_user.id, user)
    await callback.answer()

    if key == "compat":
        await callback.message.answer(
            "💑 Введи две даты через запятую:\nНапример: 15.03.1995, 22.07.1998"
        )
        await state.set_state(Form.waiting_second_date)
    else:
        await _ask_date(callback.message, user, key=key)
        await state.set_state(Form.waiting_free_date)

@dp.message(StateFilter(Form.waiting_free_date))
async def handle_free_date(message: Message, state: FSMContext):
    user = await db.get_user(message.from_user.id)
    text = message.text.strip()
    if not is_valid_date(text):
        await message.answer("❌ Неверная дата. Введи в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
        return
    user["free_used"] = True
    await db.save_user(message.from_user.id, user)
    await _process_date(message, message.from_user.id, user, text, state, is_free=True)

# ─── ОНБОРДИНГ ───────────────────────────────────────────────────────────────
@dp.message(Command("start"), StateFilter("*"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user.get("first_name") and message.from_user.first_name:
        tg_name = sanitize_name(message.from_user.first_name)
        if len(tg_name) >= 2:
            user["first_name"] = tg_name
            await db.save_user(message.from_user.id, user)
    if not user["subscribed_channel"]:
        is_sub = await check_subscription(message.from_user.id)
        if is_sub:
            user["subscribed_channel"] = True
            await db.save_user(message.from_user.id, user)
    if not user["subscribed_channel"]:
        await message.answer(
            "🔮 Привет! Я Ева — твой личный нумеролог.\n\n"
            "✨ Что я умею:\n\n"
            "• Бесплатный разбор на выбор (любой до 99 ⭐)\n"
            "• Полная матрица судьбы и кармический долг\n"
            "• Финансовый прогноз и блоки богатства\n"
            "• Путь к своему делу и призванию\n"
            "• Совместимость, любовь, отношения\n"
            "• Здоровье, энергия и интуиция\n"
            "• Прошлые жизни, родовой код, прогноз на 3 года\n\n"
            "Всё это по твоей дате рождения — точно и личностно 🌸\n\n"
            f"Подпишись на {CHANNEL} и получи бесплатный разбор на выбор 👇",
            reply_markup=check_menu()
        )
        return
    if not user["free_used"]:
        name     = user.get("first_name") or ""
        greeting = f"✨ Привет, {name}! " if name else "✨ Привет! "
        await message.answer(
            greeting + "Давай познакомимся.\n\nКак мне тебя называть? Введи своё имя 👇"
        )
        await state.set_state(Form.waiting_name)
        return
    await message.answer("🔮 Выбери свой разбор 👇", reply_markup=main_menu(user))

@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery, state: FSMContext):
    user   = await db.get_user(callback.from_user.id)
    is_sub = await check_subscription(callback.from_user.id)
    if not is_sub:
        await callback.answer("❌ Ты ещё не подписалась!", show_alert=True)
        return
    user["subscribed_channel"] = True
    await db.save_user(callback.from_user.id, user)
    await callback.answer()
    if user["free_used"]:
        await callback.message.answer("✅ Подписка подтверждена!", reply_markup=main_menu(user))
        return
    await callback.message.answer("✅ Отлично! Как мне тебя называть? Введи своё имя 👇")
    await state.set_state(Form.waiting_name)

@dp.message(StateFilter(Form.waiting_name))
async def handle_name(message: Message, state: FSMContext):
    name = sanitize_name(message.text or "")
    if len(name) < 2 or len(name) > 30:
        await message.answer("Введи настоящее имя — только буквы, от 2 до 30 символов 😊")
        return
    user = await db.get_user(message.from_user.id)
    user["first_name"] = name
    await db.save_user(message.from_user.id, user)

    if not user.get("free_used"):
        await message.answer(
            f"Приятно познакомиться, {name}! 🌸\n\n"
            "🎁 Выбери любой разбор — он будет бесплатным!\n\n"
            "Это твой подарок за подписку на канал 💫",
            reply_markup=free_choose_menu()
        )
        return

    await message.answer(
        f"Приятно познакомиться, {name}! 🌸\n\n"
        "Введи свою дату рождения в формате ДД.ММ.ГГГГ\n"
        "Например: 15.03.1995"
    )
    await state.set_state(Form.waiting_birth_date)

@dp.message(StateFilter(Form.waiting_birth_date))
async def handle_birth_date(message: Message, state: FSMContext):
    """Запасной хендлер — сохраняет дату и ведёт в меню.
    В основном флоу дата вводится уже в _ask_date/_process_date,
    этот стейт может остаться только если пользователь добрался
    сюда нестандартным путём."""
    text = message.text.strip()
    if not is_valid_date(text):
        await message.answer("❌ Неверная дата. Введи в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
        return
    user   = await db.get_user(message.from_user.id)
    number = calculate_destiny(text)
    user["birth_date"]     = text
    user["destiny_number"] = number
    await db.save_user(message.from_user.id, user)
    await state.clear()
    await message.answer("🔮 Выбери разбор:", reply_markup=main_menu(user))

# ─── КОМАНДЫ ─────────────────────────────────────────────────────────────────
@dp.message(Command("menu"), StateFilter("*"))
async def menu_cmd(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    await message.answer("🔮 Выбери разбор:", reply_markup=main_menu(user))

@dp.message(Command("promo"), StateFilter("*"))
async def promo_cmd(message: Message, state: FSMContext):
    await state.clear()
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Введи промокод так: /promo КОД")
        return
    code = parts[1].upper()
    row  = await db.db_pool.fetchrow('SELECT * FROM coupons WHERE code = $1', code)
    if not row:
        await message.answer("❌ Такого промокода не существует.")
        return
    if row['expires_at'] and row['expires_at'] < utc_now():
        await message.answer("❌ Этот промокод уже истёк.")
        return
    remaining = row['max_uses'] - row['uses_count']
    if remaining <= 0:
        await message.answer("❌ Этот промокод исчерпан — все использования закончились.")
        return
    user = await db.get_user(message.from_user.id)
    await message.answer(
        f"🎁 Промокод активирован! Доступно бесплатных разборов: {remaining}.\n\n"
        "Выбирай из списка — после каждого выбора будет списываться одно "
        "использование промокода 👇",
        reply_markup=coupon_razboy_menu(code, user)
    )

@dp.message(Command("coupon"), StateFilter("*"))
async def coupon_cmd(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer(
            "Использование:\n"
            "/coupon КОД — создать на 1 использование\n"
            "/coupon КОД 20 — создать на 20 использований\n\n"
            "Один код можно вводить /promo несколько раз — каждый выбранный "
            "разбор спишет одно использование, пока не закончится лимит.\n\n"
            "Пример: /coupon FRIEND20 20"
        )
        return
    code     = parts[1].upper()
    max_uses = 1
    if len(parts) >= 3:
        try:
            max_uses = max(1, int(parts[2]))
        except ValueError:
            await message.answer("❌ Число использований должно быть целым числом.\nПример: /coupon FRIEND20 20")
            return
    result = await db.create_coupon(code, max_uses)
    if result == 'ok':
        expires  = (utc_now() + timedelta(hours=48)).strftime("%d.%m.%Y %H:%M")
        uses_str = f"{max_uses} раз" if max_uses > 1 else "1 раз"
        await message.answer(
            f"✅ Промокод создан!\n\n"
            f"Код: <code>{code}</code>\n"
            f"Лимит использований: {uses_str}\n"
            f"Действует до: {expires}\n\n"
            f"Юзер вводит: /promo {code} — и выбирает разборы из списка, "
            f"пока не закончится лимит.",
            parse_mode="HTML"
        )
    elif result == 'exists':
        await message.answer("❌ Такой промокод уже существует.")
    else:
        await message.answer("❌ Ошибка создания промокода — проверь логи Railway.")

@dp.message(Command("coupon_stat"), StateFilter("*"))
async def coupon_stat_cmd(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Использование: /coupon_stat КОД")
        return
    code = parts[1].upper()
    row  = await db.db_pool.fetchrow('SELECT * FROM coupons WHERE code = $1', code)
    if not row:
        await message.answer(f"❌ Промокод {code} не найден.")
        return
    uses = await db.db_pool.fetch(
        'SELECT user_id, used_at FROM coupon_uses WHERE code = $1 ORDER BY used_at DESC LIMIT 20',
        code
    )
    expires_str = row['expires_at'].strftime("%d.%m.%Y %H:%M") if row['expires_at'] else "бессрочно"
    lines = [
        f"📊 Промокод: <code>{code}</code>",
        f"Использований: {row['uses_count']} / {row['max_uses']}",
        f"Действует до: {expires_str}",
    ]
    if uses:
        lines.append("\nПоследние активации:")
        for u in uses:
            dt = u['used_at'].strftime("%d.%m %H:%M")
            lines.append(f"  • user_id {u['user_id']} — {dt}")
    await message.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("admin"), StateFilter("*"))
async def admin_panel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    total         = await db.db_pool.fetchval('SELECT COUNT(*) FROM users')
    free_used     = await db.db_pool.fetchval('SELECT COUNT(*) FROM users WHERE free_used = TRUE')
    reviews       = await db.db_pool.fetchval("SELECT COUNT(*) FROM users WHERE reviews_left != '[]'")
    notif_on      = await db.db_pool.fetchval('SELECT COUNT(*) FROM users WHERE notifications = TRUE')
    coupons_total = await db.db_pool.fetchval('SELECT COUNT(*) FROM coupons')
    coupons_used  = await db.db_pool.fetchval('SELECT COUNT(*) FROM coupon_uses')
    rows = await db.db_pool.fetch('SELECT purchased FROM users WHERE user_id != $1', ADMIN_ID)
    total_purch = 0
    razbory_cnt = {}
    bought      = 0
    stars_total = 0
    for row in rows:
        p = json.loads(row['purchased'])
        if p:
            bought      += 1
            total_purch += len(p)
            for r in p:
                razbory_cnt[r] = razbory_cnt.get(r, 0) + 1
                stars_total   += PRICES.get(r, 49)
    top      = sorted(razbory_cnt.items(), key=lambda x: x[1], reverse=True)
    top_text = "\n".join([f"  {TITLES.get(k,k)}: {v}" for k, v in top[:5]]) if top else "  нет"
    await message.answer(
        f"📊 Статистика бота Ева\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"💫 Прошли онбординг: {free_used}\n"
        f"💳 Купили хотя бы раз: {bought}\n"
        f"🛒 Всего покупок: {total_purch}\n"
        f"⭐ Примерная выручка: ~{stars_total} Stars\n"
        f"🔔 Уведомления включены: {notif_on}\n"
        f"🎟 Купонов: создано {coupons_total} / активаций {coupons_used}\n"
        f"📝 Оставили отзывы: {reviews}\n\n"
        f"🏆 Топ разборов:\n{top_text}"
    )

# ─── КУПОН — ВЫБОР РАЗБОРА ───────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("coupon::"))
async def coupon_razboy_handler(callback: CallbackQuery, state: FSMContext):
    try:
        _, code, key = callback.data.split("::", 2)
    except ValueError:
        await callback.answer("Ошибка промокода.", show_alert=True)
        return

    user = await db.get_user(callback.from_user.id)

    if key in user["purchased"]:
        user["waiting"] = key
        await db.save_user(callback.from_user.id, user)
        await callback.answer("Этот разбор уже у тебя — пришлю заново 🔮")
    else:
        result = await db.use_coupon(code, callback.from_user.id)
        if result == 'not_found':
            await callback.answer("❌ Промокод не найден.", show_alert=True)
            return
        if result == 'expired':
            await callback.answer("❌ Промокод истёк.", show_alert=True)
            return
        if result == 'limit':
            await callback.answer("❌ Лимит этого промокода исчерпан.", show_alert=True)
            return
        user["purchased"].append(key)
        user["waiting"] = key
        await db.save_user(callback.from_user.id, user)
        remaining = await db.coupon_remaining(code)
        await callback.answer(f"✅ Добавлено! Осталось использований промокода: {remaining}")

    if key == "compat":
        await callback.message.answer(
            "💑 Введи две даты через запятую:\nНапример: 15.03.1995, 22.07.1998"
        )
        await state.set_state(Form.waiting_second_date)
    else:
        await _ask_date(callback.message, user, key=key)
        await state.set_state(Form.waiting_date)

# ─── УМНАЯ ДАТА ──────────────────────────────────────────────────────────────
async def _ask_date(message: Message, user: dict, key: str | None = None):
    """key — какой разбор выбран. Если есть короткое описание для него,
    показываем его перед запросом даты, чтобы было понятнее за что платишь
    до того как вводить дату рождения."""
    desc = RAZBOR_DESCRIPTIONS.get(key, "") if key else ""
    intro = f"💬 {desc}\n\n" if desc else ""
    if user.get("birth_date"):
        await message.answer(
            f"{intro}Делаешь разбор для себя ({user['birth_date']}) или введёшь другую дату?",
            reply_markup=date_choice_menu()
        )
    else:
        await message.answer(
            f"{intro}📅 Введи дату рождения в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995"
        )

@dp.callback_query(F.data == "use_my_date")
async def use_my_date(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await callback.answer()
    current_state = await state.get_state()
    is_free = (current_state == Form.waiting_free_date.state)
    if is_free and not user["free_used"]:
        user["free_used"] = True
        await db.save_user(callback.from_user.id, user)
    await _process_date(callback.message, callback.from_user.id, user, user["birth_date"], state, is_free=is_free)

@dp.callback_query(F.data == "use_new_date")
async def use_new_date(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("📅 Введи дату рождения в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
    await state.set_state(Form.waiting_date)

# ─── ПОКУПКИ ─────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "noop")
async def noop_handler(callback: CallbackQuery):
    await callback.answer()

@dp.callback_query(F.data == "free")
async def free_handler(callback: CallbackQuery, state: FSMContext):
    await free_choose_handler(callback, state)

async def send_invoice(chat_id, title, description, payload, amount):
    await bot.send_invoice(
        chat_id=chat_id, title=title, description=description,
        payload=payload, currency="XTR",
        prices=[LabeledPrice(label=title, amount=amount)],
    )

@dp.callback_query(F.data.startswith("buy_"))
async def buy_handler(callback: CallbackQuery, state: FSMContext):
    key  = callback.data.replace("buy_", "")
    user = await db.get_user(callback.from_user.id)
    if key in user["purchased"]:
        user["waiting"] = key
        await db.save_user(callback.from_user.id, user)
        await callback.answer()
        if key == "compat":
            await callback.message.answer("💑 Введи две даты через запятую:\nНапример: 15.03.1995, 22.07.1998")
            await state.set_state(Form.waiting_second_date)
        else:
            await _ask_date(callback.message, user, key=key)
            await state.set_state(Form.waiting_date)
        return
    if key in PAID_RAZBORY:
        price = PRICES.get(key, 49)
        title = PAID_RAZBORY[key]
        desc  = RAZBOR_DESCRIPTIONS.get(key, title)
        await send_invoice(callback.message.chat.id, title, desc, key, price)
    await callback.answer()

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message, state: FSMContext):
    user    = await db.get_user(message.from_user.id)
    payload = message.successful_payment.invoice_payload
    if payload not in user["purchased"]:
        user["purchased"].append(payload)
    user["waiting"] = payload
    await db.save_user(message.from_user.id, user)
    if payload == "compat":
        await message.answer("✅ Оплата прошла! Введи две даты через запятую:\nНапример: 15.03.1995, 22.07.1998")
        await state.set_state(Form.waiting_second_date)
    else:
        await _ask_date(message, user, key=payload)
        await state.set_state(Form.waiting_date)

# ─── ОБРАБОТКА ДАТ ───────────────────────────────────────────────────────────
async def _process_date(message: Message, user_id: int, user: dict, date_str: str,
                        state: FSMContext, is_free: bool = False):
    number  = calculate_destiny(date_str)
    waiting = user.get("waiting")
    name    = user.get("first_name") or "дорогая"
    if not waiting:
        await message.answer("Выбери разбор из меню 👇", reply_markup=main_menu(user))
        await state.clear()
        return

    # Замок: не запускаем вторую генерацию пока идёт первая
    if user_id in _generating:
        await message.answer("⏳ Твой разбор уже готовится — дождись его, пожалуйста 🔮")
        return
    _generating.add(user_id)

    if not user.get("birth_date"):
        user["birth_date"]     = date_str
        user["destiny_number"] = number
        await db.save_user(user_id, user)

    wait_msg = await message.answer(f"⏳ Ева составляет разбор для {name}... Подожди немного ✨")

    async def send_intermediate():
        await asyncio.sleep(20)
        try:
            await bot.edit_message_text(
                "⏳ Ева углубляется в твои числа... Ещё немного, разбор почти готов 🔮",
                chat_id=message.chat.id,
                message_id=wait_msg.message_id
            )
        except Exception:
            pass

    intermediate_task = asyncio.create_task(send_intermediate())

    async def stop_intermediate():
        """Отменяет промежуточное сообщение и ДОЖИДАЕТСЯ завершения,
        чтобы edit_message_text не сработал уже после готового разбора."""
        intermediate_task.cancel()
        try:
            await intermediate_task
        except (asyncio.CancelledError, Exception):
            pass

    try:
        context = build_numerology_context(name, date_str)
        prompt  = build_prompt(waiting, name=name, context=context, date=date_str)
        answer  = await ask_ai(prompt)
        await stop_intermediate()

        title = TITLES.get(waiting, "🔮 Разбор")
        await send_long(message.chat.id, f"{title}\n\n{answer}")

        if waiting in PDF_KEYS:
            try:
                pdf_bytes = generate_pdf(title, answer, user_name=name, destiny_number=number)
                pdf_file  = BufferedInputFile(pdf_bytes, filename=f"{title}.pdf")
                await bot.send_document(
                    message.chat.id,
                    pdf_file,
                    caption="📄 Твой разбор в PDF — сохрани себе!"
                )
            except Exception as pdf_err:
                logging.warning(f"PDF generation failed for {waiting}: {pdf_err}")

        kb = upsell_menu(waiting, user)
        has_upsells = any(
            btn.callback_data and btn.callback_data.startswith("buy_")
            for row in kb.inline_keyboard for btn in row
        )
        upsell_text = "✨ Тебе также может подойти 👇" if has_upsells else "🔮 Хочешь ещё разбор?"
        # сбрасываем waiting чтобы повторный use_my_date не запустил этот же разбор
        user["waiting"] = None
        await db.save_user(user_id, user)
        await message.answer(upsell_text, reply_markup=kb)
        await state.clear()
    except Exception as e:
        await stop_intermediate()
        logging.error(f"Date handler error [{waiting}]: {e}", exc_info=True)
        await message.answer(
            "❌ Что-то пошло не так. Твоя покупка сохранена — нажми кнопку и попробуй снова 👇",
            reply_markup=retry_menu(waiting)
        )
        await state.clear()
    finally:
        _generating.discard(user_id)

@dp.message(StateFilter(Form.waiting_second_date))
async def handle_two_dates(message: Message, state: FSMContext):
    user  = await db.get_user(message.from_user.id)
    text  = message.text.strip()
    if "," not in text:
        await message.answer("❌ Введи две даты через запятую.\nНапример: 15.03.1995, 22.07.1998")
        return
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2 or not all(is_valid_date(p) for p in parts):
        await message.answer("❌ Неверный формат. Используй ДД.ММ.ГГГГ, ДД.ММ.ГГГГ")
        return
    name    = user.get("first_name") or "дорогая"
    user_id = message.from_user.id

    if user_id in _generating:
        await message.answer("⏳ Твой разбор уже готовится — дождись его, пожалуйста 🔮")
        return
    _generating.add(user_id)

    wait_msg = await message.answer("⏳ Ева составляет разбор совместимости...")

    async def send_intermediate():
        await asyncio.sleep(20)
        try:
            await bot.edit_message_text(
                "⏳ Разбираю энергетику двух людей... Ещё немного 🔮",
                chat_id=message.chat.id,
                message_id=wait_msg.message_id
            )
        except Exception:
            pass

    intermediate_task = asyncio.create_task(send_intermediate())

    async def stop_intermediate():
        intermediate_task.cancel()
        try:
            await intermediate_task
        except (asyncio.CancelledError, Exception):
            pass

    try:
        n1      = calculate_destiny(parts[0])
        n2      = calculate_destiny(parts[1])
        context = build_numerology_context(name, parts[0])
        prompt  = build_prompt("compat", name=name, context=context, date1=parts[0], date2=parts[1], n2=n2)
        answer  = await ask_ai(prompt)
        await stop_intermediate()

        await send_long(message.chat.id, f"💑 Совместимость\n\n{answer}")

        try:
            pdf_bytes = generate_pdf("💑 Совместимость", answer, user_name=name, destiny_number=n1)
            pdf_file  = BufferedInputFile(pdf_bytes, filename="Совместимость.pdf")
            await bot.send_document(message.chat.id, pdf_file, caption="📄 Разбор в PDF — сохрани себе!")
        except Exception as pdf_err:
            logging.warning(f"PDF compat error: {pdf_err}")

        kb = upsell_menu("compat", user)
        has_upsells = any(
            btn.callback_data and btn.callback_data.startswith("buy_")
            for row in kb.inline_keyboard for btn in row
        )
        upsell_text = "✨ Тебе также может подойти 👇" if has_upsells else "🔮 Хочешь ещё разбор?"
        user["waiting"] = None
        await db.save_user(user_id, user)
        await message.answer(upsell_text, reply_markup=kb)
        await state.clear()
    except Exception as e:
        await stop_intermediate()
        logging.error(f"Compat error: {e}", exc_info=True)
        await message.answer(
            "❌ Что-то пошло не так. Твоя покупка сохранена — нажми кнопку и попробуй снова 👇",
            reply_markup=retry_menu("compat")
        )
        await state.clear()
    finally:
        _generating.discard(user_id)

@dp.message(StateFilter(Form.waiting_date))
async def handle_date(message: Message, state: FSMContext):
    user = await db.get_user(message.from_user.id)
    text = message.text.strip()
    if not is_valid_date(text):
        await message.answer("❌ Неверная дата. Введи в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
        return
    await _process_date(message, message.from_user.id, user, text, state)

# ─── ОТЗЫВЫ ──────────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("leave_review_"))
async def leave_review(callback: CallbackQuery, state: FSMContext):
    key  = callback.data.replace("leave_review_", "")
    user = await db.get_user(callback.from_user.id)
    if key not in user.get("purchased", []):
        await callback.answer("Отзыв можно оставить только после покупки!", show_alert=True)
        return
    if key in user.get("reviews_left", []):
        await callback.answer("Ты уже оставила отзыв по этому разбору 💫", show_alert=True)
        return
    await state.update_data(review_key=key)
    await callback.message.answer("💬 Напиши свой отзыв — опубликую его в канале!")
    await state.set_state(Form.waiting_review)
    await callback.answer()

@dp.message(StateFilter(Form.waiting_review))
async def handle_review(message: Message, state: FSMContext):
    user        = await db.get_user(message.from_user.id)
    name        = user.get("first_name") or "Аноним"
    data        = await state.get_data()
    review_key  = data.get("review_key", "")
    title       = TITLES.get(review_key, "разбор")
    review_text = f"⭐ Отзыв о боте @nnumerology_bot\n👤 {name}\n💫 Разбор: {title}\n\n{message.text}"
    reviews_left = user.get("reviews_left", [])
    if review_key and review_key not in reviews_left:
        reviews_left.append(review_key)
    user["reviews_left"] = reviews_left
    user["review_left"]  = True
    await db.save_user(message.from_user.id, user)
    try:
        await bot.send_message(REVIEWS_CHANNEL, review_text)
        await message.answer("✅ Спасибо! Твой отзыв опубликован 💫")
    except Exception as e:
        logging.error(f"Review channel error: {e}")
        await message.answer("✅ Спасибо за отзыв!")
    await state.clear()

@dp.callback_query(F.data == "show_menu")
async def show_menu(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    await callback.message.answer("🔮 Выбери разбор:", reply_markup=main_menu(user))
    await callback.answer()

# ─── РАССЫЛКИ ────────────────────────────────────────────────────────────────
async def send_daily_horoscope():
    """UTC 8:00 = Москва 11:00 — утренняя рассылка из статичных шаблонов."""
    while True:
        now    = utc_now()
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            rows = await db.db_pool.fetch(
                'SELECT user_id, first_name, destiny_number FROM users '
                'WHERE birth_date IS NOT NULL AND destiny_number IS NOT NULL '
                'AND notifications = TRUE'
            )
            for row in rows:
                try:
                    number   = row['destiny_number']
                    name     = row['first_name'] or "дорогая"
                    variants = MORNING.get(number, MORNING.get(9, []))
                    text     = random.choice(variants).format(name=name)
                    await bot.send_message(row['user_id'], text, reply_markup=notif_off_menu())
                    await asyncio.sleep(0.05)
                except TelegramForbiddenError:
                    await db.db_pool.execute(
                        'UPDATE users SET notifications = FALSE WHERE user_id = $1',
                        row['user_id']
                    )
                    logging.info(f"User {row['user_id']} blocked bot, notifications disabled")
                except TelegramBadRequest:
                    pass
                except Exception as e:
                    logging.error(f"Horoscope error {row['user_id']}: {e}")
        except Exception as e:
            logging.error(f"Horoscope batch error: {e}")

async def send_daily_channel_post():
    """UTC 7:00 = Москва 10:00 — пост в канал."""
    _CHANNEL_FALLBACK = [
        "🔮 Сегодня особый день для тех, кто слушает своё сердце.\n\nЧисла говорят: доверяй интуиции — она не подведёт. Сделай один шаг к тому, что давно откладывала. Именно сегодня он будет правильным.\n\n✨ Узнайте свой личный разбор → @nnumerology_bot",
        "🌟 День, который напоминает: ты сильнее, чем думаешь.\n\nЧисловая энергия сегодня поддерживает смелые решения. Не жди идеального момента — он уже здесь.\n\n✨ Узнайте свой личный разбор → @nnumerology_bot",
        "💫 Числа сегодня говорят о переменах к лучшему.\n\nЕсли что-то давно требует твоего внимания — самое время действовать. Вселенная поддерживает тех, кто делает первый шаг.\n\n✨ Узнайте свой личный разбор → @nnumerology_bot",
    ]
    while True:
        now    = utc_now()
        target = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            today   = date.today()
            day_num = calculate_day_number(today)
            prompt  = (
                f"Напиши нумерологический пост для Телеграм канала на {today.strftime('%d.%m.%Y')}. "
                f"Число дня по нумерологии: {day_num}. "
                f"Расскажи что означает число {day_num}, какая энергия сегодня, дай практичные советы на день. "
                "Обращайся к читательницам на ВЫ, уважительно и тепло. "
                "Не используй фамильярные обращения. "
                "Пиши красиво, с эмодзи, атмосферно. 150-200 слов. Только кириллица."
            )
            try:
                post = await ask_ai(prompt)
                text = (
                    f"🔮 Нумерология дня — {today.strftime('%d.%m.%Y')}\n"
                    f"Число дня: {day_num}\n\n"
                    f"{post}\n\n"
                    f"✨ Узнайте свой личный разбор → @nnumerology_bot"
                )
            except Exception as ai_err:
                logging.warning(f"Channel post AI failed, using fallback: {ai_err}")
                text = random.choice(_CHANNEL_FALLBACK)
            await bot.send_message(CHANNEL, text)
        except Exception as e:
            logging.error(f"Channel post error: {e}")
        await asyncio.sleep(60)

# ─── WEB ─────────────────────────────────────────────────────────────────────
async def healthcheck(request):
    return web.Response(text="OK")

async def run_web():
    app    = web.Application()
    app.router.add_get("/", healthcheck)
    port   = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    await db.init_db(DATABASE_URL)
    asyncio.create_task(run_web())
    asyncio.create_task(send_daily_horoscope())
    asyncio.create_task(send_daily_channel_post())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
