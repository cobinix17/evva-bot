import os
import re
import time
import logging
import asyncio
import json
import random
import secrets
from datetime import datetime, date, timedelta, timezone
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    TelegramObject, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
    MenuButtonWebApp, WebAppInfo,
    ErrorEvent,
)
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

import db
import config
from config import (
    TITLES, PRICES, UPSELLS, PAID_RAZBORY, FREE_ELIGIBLE, RAZBOR_DESCRIPTIONS, TWO_DATE_KEYS,
    ADMIN_ID, REF_BONUS_PERCENT,
    PREMIUM_PRICE, PREMIUM_PERIOD, PREMIUM_DAILY_LIMIT, PREMIUM_MONTHLY_LIMIT,
    PREMIUM_PAYLOAD, PREMIUM_TITLE, ASK_DAILY_LIMIT, FOLLOWUP_LIMIT, YESNO_FREE_LIMIT,
    YOOKASSA_SHOP_ID, PREMIUM_PRICE_RUB, rub_price, STARS_TO_RUB_RATE,
)
from ai import ask_ai, is_rude, rude_reply, bolden_headers
from pdf import generate_pdf
from generation import (
    premium_gen_semaphore, generate_single, generate_compat, generate_name, _generating,
    RegenLimitReached,
)
from numerology import (
    calculate_destiny, calculate_day_number, is_valid_date, normalize_date,
    build_numerology_context, calculate_personal_month, calculate_personal_day,
    DAY_ENERGY, personal_day_info, calculate_name_number,
)
from keyboards import (
    check_menu, date_choice_menu, notifications_menu, main_menu,
    free_choose_menu, section_destiny_menu, section_money_menu,
    section_love_menu, section_health_menu, section_past_menu,
    my_readings_menu, upsell_menu, retry_menu, coupon_razboy_menu,
    notif_off_menu, admin_menu, balance_pay_menu, payment_choice_menu,
    premium_subscribe_menu, premium_active_menu, gift_sections_menu, profile_menu,
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

# ─── ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК ────────────────────────────────────────────
# Без него необработанное исключение в хендлере (например message.text is
# None когда юзер прислал фото вместо даты) просто уходит в лог, а сам
# пользователь не получает ответа и виснет в незакрытом FSM-состоянии.
@dp.errors()
async def global_error_handler(event: ErrorEvent):
    logging.error(f"Unhandled error: {event.exception}", exc_info=event.exception)
    update = event.update
    try:
        if update.message:
            await update.message.answer("❌ Что-то пошло не так. Попробуй /menu или /cancel.", reply_markup=_MENU_BACK_MARKUP)
        elif update.callback_query:
            await update.callback_query.answer("❌ Что-то пошло не так.", show_alert=True)
    except Exception:
        pass
    return True

# ─── РАЗБИВКА ДЛИННЫХ СООБЩЕНИЙ ──────────────────────────────────────────────
async def send_long(chat_id, text: str):
    # Жирные emoji-заголовки разделов через HTML — разбор в чате читается как
    # документ, а не сплошной текст (см. ai.bolden_headers). Экранирование под
    # HTML там же, ДО разбиения — split идёт только по '\n', так что открывающий
    # и закрывающий <b> одной строки никогда не попадут в разные части.
    text  = bolden_headers(text)
    limit = 4000
    if len(text) <= limit:
        await bot.send_message(chat_id, text, parse_mode="HTML")
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
        await bot.send_message(chat_id, part, parse_mode="HTML")
        await asyncio.sleep(0.3)

# ─── FSM ─────────────────────────────────────────────────────────────────────
class Form(StatesGroup):
    waiting_name             = State()
    waiting_birth_date       = State()
    waiting_date             = State()
    waiting_second_date      = State()
    waiting_free_second_date = State()
    waiting_review           = State()
    waiting_free_date        = State()
    waiting_broadcast        = State()
    waiting_coupon           = State()
    waiting_user_search      = State()
    waiting_ai_question      = State()
    waiting_yesno            = State()
    waiting_business_name    = State()
    waiting_followup         = State()
    waiting_rename           = State()
    waiting_feedback         = State()
    waiting_admin_reply      = State()
    waiting_promo            = State()
    waiting_rub_email        = State()
    waiting_other_name       = State()

# ─── ЗАМОК ГЕНЕРАЦИИ ─────────────────────────────────────────────────────────
# Один платный разбор за раз на пользователя. Защищает от параллельного
# запуска двух генераций (юзер во время 90-сек ожидания уходит в меню и
# запускает второй разбор) — это удваивает расход токенов и рассинхронизирует
# поле waiting. Храним в памяти процесса: бот однопроцессный (один polling),
# поэтому простого set достаточно — внешний стор (Redis) не нужен.
# Замок генерации (_generating), семафоры и premium_gen_semaphore теперь общие
# с веб-кабинетом — см. generation.py. Лимиты премиума (30/мес, 5/день)
# считаются в БД (db.premium_try_consume); регенерация открытых разборов слот
# не тратит.

async def _generate_pdf_async(*args, **kwargs) -> bytes:
    """Сборка PDF (fontTools) — тяжёлая по CPU и СИНХРОННАЯ: при прямом вызове
    она блокирует весь event loop, и на эти секунды бот «подвисает» для всех
    остальных пользователей. Выносим в отдельный поток через executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: generate_pdf(*args, **kwargs))

# ─── ВСПОМОГАТЕЛЬНЫЕ ─────────────────────────────────────────────────────────
async def check_subscription(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logging.warning(f"check_subscription({user_id}) failed: {e}")
        return False

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def main_menu_for(user_id: int, user: dict):
    return main_menu(user, is_admin=(user_id == ADMIN_ID), is_premium=db.is_premium(user))

_NAME_ALLOWED_RE = re.compile(r"[^а-яёА-ЯЁa-zA-Z\s\-]")

def sanitize_name(raw: str) -> str:
    """Имя пользователя идёт прямо в промпт через {name}. Без очистки
    юзер может ввести 'имя' с инструкциями для ИИ (prompt injection) или
    переводами строк, ломающими структуру промпта. Оставляем только буквы,
    пробел и дефис, схлопываем пробелы, режем длину."""
    cleaned = _NAME_ALLOWED_RE.sub("", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:30]

# ─── ФИЛЬТР ОТЗЫВОВ (мат / реклама) ──────────────────────────────────────────
# Не блокирует отправку — только помечает отзыв флагами для админа при
# модерации. Реальное решение публиковать или нет остаётся за человеком,
# чтобы не резать честные отзывы ложными срабатываниями.
_MAT_RE = re.compile(
    r"(х[уy][йиеё]|пизд|бля[дт]|еба[тнл]|ебуч|мудак|мудил|гандон|скотин|"
    r"сука(?!рь)|хер(?:ня|ов)|залуп|уебан|долбо[её]б|пидор|шлюх)",
    re.IGNORECASE
)
_AD_RE = re.compile(
    r"(https?://|www\.|t\.me/|@[a-zA-Z][a-zA-Z0-9_]{4,}|подпи[шс][иы]\w*\s+на|"
    r"переходи(?:те)?\s+(?:по|на)|канал[еa]?\s+@|\+7\d{10}|\b8\d{10}\b)",
    re.IGNORECASE
)

def _review_flags(text: str) -> list[str]:
    """Возвращает список нарушений найденных в тексте отзыва: 'мат', 'реклама'."""
    flags = []
    if _MAT_RE.search(text):
        flags.append("мат")
    if _AD_RE.search(text):
        flags.append("реклама/ссылка")
    return flags

# ─── СМЕНА ИМЕНИ ──────────────────────────────────────────────────────────────
@dp.message(Command("name"), StateFilter("*"))
async def name_cmd(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    current = user.get("first_name") or "не указано"
    await message.answer(
        f"✏️ Сейчас я называю тебя «{current}».\n\n"
        "Как называть тебя теперь? Введи новое имя 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="cancel_to_menu")]
        ])
    )
    await state.set_state(Form.waiting_rename)

@dp.callback_query(F.data == "name_start")
async def name_start_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await db.get_user(callback.from_user.id)
    current = user.get("first_name") or "не указано"
    await callback.message.answer(
        f"✏️ Сейчас я называю тебя «{current}».\n\n"
        "Как называть тебя теперь? Введи новое имя 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="cancel_to_menu")]
        ])
    )
    await state.set_state(Form.waiting_rename)
    await callback.answer()

@dp.message(StateFilter(Form.waiting_rename))
async def handle_rename(message: Message, state: FSMContext):
    name = sanitize_name(message.text or "")
    if len(name) < 2 or len(name) > 30:
        await message.answer("Введи настоящее имя — только буквы, от 2 до 30 символов 😊")
        return
    user = await db.get_user(message.from_user.id)
    user["first_name"] = name
    await db.save_user(message.from_user.id, user)
    await state.clear()
    await message.answer(
        f"✅ Готово! Теперь буду называть тебя {name} 🌸",
        reply_markup=main_menu_for(message.from_user.id, user)
    )

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
    await _start_date_flow(callback.message, state, user, key, is_free=True)

@dp.message(StateFilter(Form.waiting_free_date))
async def handle_free_date(message: Message, state: FSMContext):
    user = await db.get_user(message.from_user.id)
    text = normalize_date(message.text or "")
    if not is_valid_date(text):
        await message.answer("❌ Неверная дата. Введи в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
        return
    # free_used выставляется внутри _process_date только при УСПЕШНОЙ генерации —
    # если все провайдеры ИИ недоступны, бесплатная попытка не сгорает и
    # ретрай снова бесплатный, а не платный счёт.
    await _process_date(message, message.from_user.id, user, text, state, is_free=True)

# ─── ОНБОРДИНГ ───────────────────────────────────────────────────────────────
@dp.message(Command("start"), StateFilter("*"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    # Проверяем ДО get_user (который автосоздаёт строку) — реферала можно
    # засчитать только по-настоящему новому пользователю, иначе давний
    # юзер может задним числом "стать рефералом" и отдать 25% с будущих
    # покупок тому, кто его на самом деле не приводил.
    is_new_user = not await db.user_exists(message.from_user.id)
    user = await db.get_user(message.from_user.id)

    # Реферальный payload: /start ref_12345678
    args = message.text.strip().split()
    if is_new_user and len(args) > 1 and args[1].startswith("ref_"):
        try:
            referrer_id = int(args[1][4:])
            if referrer_id != message.from_user.id and not user.get("referred_by"):
                await db.register_referral(referrer_id, message.from_user.id)
                user["referred_by"] = referrer_id
        except (ValueError, TypeError):
            pass

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

    # Deep link из веб-кабинета: /start open_<key> — открыть уже купленный
    # разбор (после оплаты в Mini App пользователя возвращают в чат сюда).
    if len(args) > 1 and args[1].startswith("open_"):
        key = args[1][len("open_"):]
        if key in user.get("purchased", []):
            await _resume_purchased_from_start(message, state, user, key)
            return

    # Подарок: /start gift_<code> — кто-то прислал ссылку на купленный для
    # неё разбор. Забираем атомарно (redeem_gift), чтобы повторный переход
    # по той же ссылке или гонка из двух кликов не выдали разбор дважды.
    if len(args) > 1 and args[1].startswith("gift_"):
        code = args[1][len("gift_"):]
        key = await db.redeem_gift(code, message.from_user.id)
        if key is None:
            await message.answer(
                "🎁 Эта ссылка уже использована или недействительна.",
                reply_markup=main_menu_for(message.from_user.id, user)
            )
            return
        if key not in user.get("purchased", []):
            user["purchased"].append(key)
        title = PAID_RAZBORY.get(key, "разбор")
        await message.answer(f"🎁 Тебе подарили «{title}»! Забираем 🌸")
        await _resume_purchased_from_start(message, state, user, key)
        return

    await message.answer("🔮 Выбери свой разбор 👇", reply_markup=main_menu_for(message.from_user.id, user))

async def _resume_purchased_from_start(message: Message, state: FSMContext, user: dict, key: str):
    user["waiting"] = key
    await db.save_user(message.from_user.id, user)
    await _start_date_flow(message, state, user, key)

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
        await callback.message.answer("✅ Подписка подтверждена!", reply_markup=main_menu_for(callback.from_user.id, user))
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
    await state.clear()

    # Один лёгкий вопрос про обращение — чтобы Ева говорила с мужчинами в
    # мужском роде. Кнопки, не текст: онбординг почти не удлиняется.
    await message.answer(
        f"Приятно познакомиться, {name}! 🌸\n\n"
        "Подскажи, как мне к тебе обращаться?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🙋‍♀️ Я девушка", callback_data="gender_f"),
             InlineKeyboardButton(text="🙋‍♂️ Я мужчина", callback_data="gender_m")],
        ])
    )

async def _continue_after_gender(message: Message, state: FSMContext, user: dict):
    """Продолжение онбординга после выбора обращения — тот же путь, что раньше
    шёл сразу после имени."""
    if not user.get("free_used"):
        await message.answer(
            "🎁 Выбери любой разбор — он будет бесплатным!\n\n"
            "Это твой подарок за подписку на канал 💫",
            reply_markup=free_choose_menu()
        )
        return
    await message.answer(
        "Введи свою дату рождения в формате ДД.ММ.ГГГГ\n"
        "Например: 15.03.1995"
    )
    await state.set_state(Form.waiting_birth_date)

@dp.callback_query(F.data.in_({"gender_f", "gender_m"}))
async def gender_cb(callback: CallbackQuery, state: FSMContext):
    gender = "m" if callback.data == "gender_m" else "f"
    await db.set_gender(callback.from_user.id, gender)
    user = await db.get_user(callback.from_user.id)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Запомнила 🌸" if gender == "f" else "Запомнила 🙌")
    await _continue_after_gender(callback.message, state, user)

@dp.message(StateFilter(Form.waiting_birth_date))
async def handle_birth_date(message: Message, state: FSMContext):
    """Запасной хендлер — сохраняет дату и ведёт в меню.
    В основном флоу дата вводится уже в _ask_date/_process_date,
    этот стейт может остаться только если пользователь добрался
    сюда нестандартным путём."""
    text = normalize_date(message.text or "")
    if not is_valid_date(text):
        await message.answer("❌ Неверная дата. Введи в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
        return
    user   = await db.get_user(message.from_user.id)
    number = calculate_destiny(text)
    user["birth_date"]     = text
    user["destiny_number"] = number
    await db.save_user(message.from_user.id, user)
    await state.clear()
    await message.answer("🔮 Выбери разбор:", reply_markup=main_menu_for(message.from_user.id, user))

# ─── КОМАНДЫ ─────────────────────────────────────────────────────────────────
@dp.message(Command("menu"), StateFilter("*"))
async def menu_cmd(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    await message.answer("🔮 Выбери разбор:", reply_markup=main_menu_for(message.from_user.id, user))

@dp.message(Command("cancel"), StateFilter("*"))
async def cancel_cmd(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Нечего отменять — ты не в процессе ввода 🙂")
        return
    await state.clear()
    if message.from_user.id == ADMIN_ID:
        await message.answer("❌ Отменено.", reply_markup=admin_menu())
    else:
        user = await db.get_user(message.from_user.id)
        await message.answer("❌ Отменено.", reply_markup=main_menu_for(message.from_user.id, user))

_MENU_BACK_MARKUP = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🔮 Главное меню", callback_data="show_menu")]
])

def _regen_limit_text(limit: int, user: dict | None = None) -> str:
    """Сообщение при исчерпании дневного лимита разборов на другие даты.
    Уже сделанные разборы остаются доступны — важно сказать это прямо,
    чтобы лимит не читался как «у меня всё пропало»."""
    did = "сделал" if (user and db.is_male(user)) else "сделала"
    return (
        f"🌙 На сегодня ты уже {did} {limit} разбора на другие даты — это дневной лимит.\n\n"
        "Все твои разборы никуда не делись, они открыты в «Мои разборы». "
        "Новые даты можно будет посмотреть завтра ✨"
    )

async def _apply_promo(message: Message, code: str):
    code = code.strip().upper()
    row  = await db.db_pool.fetchrow('SELECT * FROM coupons WHERE code = $1', code)
    if not row:
        await message.answer("❌ Такого промокода не существует.", reply_markup=_MENU_BACK_MARKUP)
        return
    if row['expires_at'] and row['expires_at'] < utc_now():
        await message.answer("❌ Этот промокод уже истёк.", reply_markup=_MENU_BACK_MARKUP)
        return
    remaining = row['max_uses'] - row['uses_count']
    if remaining <= 0:
        await message.answer("❌ Этот промокод исчерпан — все использования закончились.", reply_markup=_MENU_BACK_MARKUP)
        return
    user = await db.get_user(message.from_user.id)
    await message.answer(
        f"🎁 Промокод активирован! Доступно бесплатных разборов: {remaining}.\n\n"
        "Выбирай из списка — после каждого выбора будет списываться одно "
        "использование промокода 👇",
        reply_markup=coupon_razboy_menu(code, user)
    )

@dp.message(Command("promo"), StateFilter("*"))
async def promo_cmd(message: Message, state: FSMContext):
    await state.clear()
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Введи промокод так: /promo КОД")
        return
    await _apply_promo(message, parts[1])

@dp.callback_query(F.data == "promo_start")
async def promo_start_cb(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🎁 Введи промокод:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="cancel_to_menu")]
        ])
    )
    await state.set_state(Form.waiting_promo)
    await callback.answer()

@dp.message(StateFilter(Form.waiting_promo))
async def handle_promo_input(message: Message, state: FSMContext):
    await state.clear()
    await _apply_promo(message, (message.text or "").strip())

@dp.message(Command("coupon"), StateFilter("*"))
async def coupon_cmd(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer(
            "Использование:\n"
            "/coupon КОД — создать на 1 использование\n"
            "/coupon КОД 20 — создать на 20 использований\n"
            "/coupon КОД 50 multi — личный код (см. ниже)\n\n"
            "👥 Обычный код: каждый юзер активирует его ОДИН раз, лимит "
            "делится между разными людьми — так публичный промокод не "
            "выгребет один человек.\n"
            "🔁 Личный код (multi): один и тот же человек может активировать "
            "код много раз, пока не кончится лимит — для второго аккаунта "
            "и тестов.\n\n"
            "Пример: /coupon FRIEND20 20"
        )
        return
    code     = parts[1].upper()
    multi    = any(p.lower() in ("multi", "личный", "тест") for p in parts[2:])
    rest     = [p for p in parts[2:] if p.lower() not in ("multi", "личный", "тест")]
    max_uses = 1
    if rest:
        try:
            max_uses = max(1, int(rest[0]))
        except ValueError:
            await message.answer("❌ Число использований должно быть целым числом.\nПример: /coupon FRIEND20 20")
            return
    result = await db.create_coupon(code, max_uses, multi_per_user=multi)
    if result == 'ok':
        expires  = (utc_now() + timedelta(hours=48)).strftime("%d.%m.%Y %H:%M")
        uses_str = f"{max_uses} раз" if max_uses > 1 else "1 раз"
        await message.answer(
            f"✅ Промокод создан!\n\n"
            f"Код: <code>{code}</code>\n"
            f"Лимит использований: {uses_str}\n"
            f"Режим: {'🔁 личный — один человек может активировать много раз' if multi else '👥 обычный — каждый юзер активирует один раз'}\n"
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
async def admin_cmd(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("⚙️ Админ-панель", reply_markup=admin_menu())

@dp.callback_query(F.data == "admin_panel")
async def admin_panel_cb(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await callback.message.answer("⚙️ Админ-панель", reply_markup=admin_menu())
    await callback.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    total         = await db.db_pool.fetchval('SELECT COUNT(*) FROM users')
    free_used     = await db.db_pool.fetchval('SELECT COUNT(*) FROM users WHERE free_used = TRUE')
    reviews       = await db.db_pool.fetchval("SELECT COUNT(*) FROM users WHERE reviews_left != '[]'")
    notif_on      = await db.db_pool.fetchval('SELECT COUNT(*) FROM users WHERE notifications = TRUE')
    coupons_total = await db.db_pool.fetchval('SELECT COUNT(*) FROM coupons')
    coupons_used  = await db.db_pool.fetchval('SELECT COUNT(*) FROM coupon_uses')
    today         = utc_now().date()
    week_ago      = today - timedelta(days=7)
    new_today     = await db.db_pool.fetchval(
        "SELECT COUNT(*) FROM users WHERE DATE(created_at) = $1", today
    ) if await _column_exists('users', 'created_at') else '—'
    new_week      = await db.db_pool.fetchval(
        "SELECT COUNT(*) FROM users WHERE created_at >= $1", week_ago
    ) if await _column_exists('users', 'created_at') else '—'
    # Считаем по таблице payments (реальные денежные оплаты), а не по
    # users.purchased — туда попадают и купоны, и разблокировка по
    # премиум-подписке, которые не приносят выручку и раздували бы цифры.
    pay_rows = await db.db_pool.fetch(
        'SELECT user_id, razbor_key, amount_xtr FROM payments WHERE user_id != $1', ADMIN_ID
    )
    total_purch = 0
    razbory_cnt = {}
    buyers      = set()
    stars_total = 0
    for row in pay_rows:
        stars_total += row['amount_xtr']
        buyers.add(row['user_id'])
        if row['razbor_key']:
            total_purch += 1
            razbory_cnt[row['razbor_key']] = razbory_cnt.get(row['razbor_key'], 0) + 1
    bought   = len(buyers)
    top      = sorted(razbory_cnt.items(), key=lambda x: x[1], reverse=True)
    top_text = "\n".join([f"  {TITLES.get(k,k)}: {v}" for k, v in top[:5]]) if top else "  нет"
    prem = await db.premium_stats()
    await callback.message.answer(
        f"📊 Статистика бота Ева\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"🆕 Новых сегодня: {new_today}\n"
        f"📅 Новых за неделю: {new_week}\n"
        f"💫 Прошли онбординг: {free_used}\n"
        f"💳 Купили хотя бы раз: {bought}\n"
        f"🛒 Всего покупок: {total_purch}\n"
        f"💎 Премиум активных: {prem['active']} (всего оформляли: {prem['ever']})\n"
        f"⭐ Примерная выручка: ~{stars_total} Stars\n"
        f"🔔 Уведомления включены: {notif_on}\n"
        f"🎟 Купонов: создано {coupons_total} / активаций {coupons_used}\n"
        f"📝 Оставили отзывы: {reviews}\n\n"
        f"🏆 Топ разборов:\n{top_text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_models")
async def admin_models(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    from ai import model_usage_report
    await callback.message.answer(
        model_usage_report(),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
        ])
    )
    await callback.answer()

async def _column_exists(table: str, column: str) -> bool:
    try:
        result = await db.db_pool.fetchval(
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_name=$1 AND column_name=$2",
            table, column
        )
        return result > 0
    except Exception:
        return False

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_cb(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(Form.waiting_broadcast)
    await callback.message.answer(
        "✍️ Напиши текст для рассылки.\n\nМожно использовать эмодзи, переносы строк.\nДля отмены — /cancel"
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_coupon_create")
async def admin_coupon_create_cb(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(Form.waiting_coupon)
    await callback.message.answer(
        "🎟 Создание купона\n\n"
        "Напиши код и количество использований через пробел:\n"
        "Пример: FRIEND20 10\n\n"
        "Если без числа — создам на 1 использование.\n\n"
        "📌 Режимы:\n"
        "• обычный — каждый юзер активирует код ОДИН раз (для канала/рекламы)\n"
        "• личный — допиши слово multi в конце, тогда один человек сможет "
        "активировать код много раз (для своего второго аккаунта и тестов)\n"
        "Пример: TEST 50 multi\n\nДля отмены — /cancel"
    )
    await callback.answer()

@dp.message(StateFilter(Form.waiting_coupon))
async def handle_coupon_input(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").strip().split()
    if not parts:
        await message.answer("❌ Введи код купона.")
        return
    # Ключевое слово multi в любом месте — личный/тестовый режим.
    multi = any(p.lower() in ("multi", "личный", "тест") for p in parts[1:])
    parts = [p for p in parts if p.lower() not in ("multi", "личный", "тест")]
    code     = parts[0].upper()
    max_uses = 1
    if len(parts) >= 2:
        try:
            max_uses = max(1, int(parts[1]))
        except ValueError:
            await message.answer("❌ Второй параметр должен быть числом. Пример: FRIEND20 10")
            return
    result = await db.create_coupon(code, max_uses, multi_per_user=multi)
    await state.clear()
    if result == 'ok':
        expires  = (utc_now() + timedelta(hours=48)).strftime("%d.%m.%Y %H:%M")
        uses_str = f"{max_uses} раз" if max_uses > 1 else "1 раз"
        mode_str = ("🔁 личный — один человек может активировать много раз"
                    if multi else
                    "👥 обычный — каждый юзер активирует один раз")
        await message.answer(
            f"✅ Купон создан!\n\nКод: {code}\nЛимит: {uses_str}\nРежим: {mode_str}\n"
            f"Действует до: {expires}\n\n"
            f"Пользователь вводит: /promo {code}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin_panel")]
            ])
        )
    elif result == 'exists':
        await message.answer("❌ Такой купон уже существует.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin_panel")]
        ]))
    else:
        await message.answer("❌ Ошибка создания купона.")

@dp.callback_query(F.data == "admin_coupon_list")
async def admin_coupon_list_cb(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    rows = await db.db_pool.fetch(
        'SELECT code, uses_count, max_uses, expires_at, multi_per_user '
        'FROM coupons ORDER BY expires_at DESC LIMIT 20'
    )
    if not rows:
        await callback.message.answer("Купонов пока нет.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
        ]))
        await callback.answer()
        return
    lines = ["📋 Активные купоны:\n"]
    for r in rows:
        exp = r['expires_at'].strftime("%d.%m %H:%M") if r['expires_at'] else "∞"
        status = "✅" if r['uses_count'] < r['max_uses'] else "❌"
        mode   = "🔁" if r['multi_per_user'] else "👥"
        lines.append(f"{status} {mode} {r['code']} — {r['uses_count']}/{r['max_uses']} исп. до {exp}")
    lines.append("\n👥 каждому по одной активации · 🔁 личный (много раз одному)")
    await callback.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_reviews")
async def admin_reviews_cb(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    rows = await db.db_pool.fetch(
        'SELECT * FROM pending_reviews ORDER BY created_at ASC LIMIT 10'
    )
    if not rows:
        await callback.message.answer("✅ Очередь модерации пуста.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
        ]))
        await callback.answer()
        return
    for r in rows:
        warn = f"\n\n⚠️ Возможные нарушения: {r['flags']}" if r['flags'] else ""
        await callback.message.answer(
            f"📝 Отзыв на модерации (#{r['id']}){warn}\n\n{r['review_text']}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Одобрить",  callback_data=f"revmod_ok_{r['id']}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"revmod_no_{r['id']}"),
            ]])
        )
    await callback.answer()

@dp.callback_query(F.data == "admin_refs")
async def admin_refs_cb(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    try:
        total_refs   = await db.db_pool.fetchval('SELECT COUNT(*) FROM referrals')
        total_bonus  = await db.db_pool.fetchval('SELECT COALESCE(SUM(amount), 0) FROM ref_bonuses')
        users_w_refs = await db.db_pool.fetchval('SELECT COUNT(DISTINCT referrer_id) FROM referrals')
        top_rows     = await db.db_pool.fetch(
            'SELECT referrer_id, COUNT(*) as cnt FROM referrals GROUP BY referrer_id ORDER BY cnt DESC LIMIT 5'
        )
        lines = [
            f"👥 Реферальная статистика\n",
            f"Всего приглашений: {total_refs}",
            f"Пользователей-рефереров: {users_w_refs}",
            f"Начислено бонусов: ~{total_bonus} ⭐\n",
        ]
        if top_rows:
            lines.append("🏆 Топ рефереров:")
            for r in top_rows:
                lines.append(f"  user_id {r['referrer_id']} — {r['cnt']} приглашений")
        await callback.message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
            ])
        )
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {e}")
    await callback.answer()

@dp.callback_query(F.data == "admin_find_user")
async def admin_find_user_cb(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(Form.waiting_user_search)
    await callback.message.answer(
        "🔍 Введи user_id или имя пользователя для поиска:\nДля отмены — /cancel"
    )
    await callback.answer()

@dp.message(StateFilter(Form.waiting_user_search))
async def handle_user_search(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer("❌ Введи user_id или имя текстом.")
        return
    await state.clear()
    row = None
    if query.isdigit():
        row = await db.db_pool.fetchrow('SELECT * FROM users WHERE user_id = $1', int(query))
    if not row:
        row = await db.db_pool.fetchrow(
            "SELECT * FROM users WHERE first_name ILIKE $1 LIMIT 1", f"%{query}%"
        )
    if not row:
        await message.answer("❌ Пользователь не найден.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin_panel")]
        ]))
        return
    purchased = json.loads(row['purchased']) if row['purchased'] else []
    purch_list = "\n".join([f"  • {TITLES.get(k, k)}" for k in purchased]) if purchased else "  нет"
    ref_balance = row.get('ref_balance', 0) or 0
    await message.answer(
        f"👤 Пользователь найден\n\n"
        f"ID: {row['user_id']}\n"
        f"Имя: {row.get('first_name') or '—'}\n"
        f"Дата рождения: {row.get('birth_date') or '—'}\n"
        f"Число судьбы: {row.get('destiny_number') or '—'}\n"
        f"Подписан на канал: {'✅' if row.get('subscribed_channel') else '❌'}\n"
        f"Онбординг пройден: {'✅' if row.get('free_used') else '❌'}\n"
        f"Уведомления: {'🔔' if row.get('notifications', True) else '🔕'}\n"
        f"Реф. баланс: {ref_balance} ⭐\n"
        f"Куплено разборов: {len(purchased)}\n{purch_list}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin_panel")]
        ])
    )

# ─── РАССЫЛКА (/post) ────────────────────────────────────────────────────────
@dp.message(Command("post"), StateFilter("*"))
async def post_cmd(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(Form.waiting_broadcast)
    await message.answer(
        "✍️ Напиши текст для рассылки.\n\n"
        "Можно использовать эмодзи, переносы строк, ссылки.\n"
        "Для отмены — /cancel"
    )

@dp.message(StateFilter(Form.waiting_broadcast))
async def post_text_received(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text or message.caption or ""
    if not text.strip():
        await message.answer("Текст пустой, попробуй ещё раз.")
        return
    await state.update_data(broadcast_text=text)
    await message.answer(
        f"📋 Превью рассылки:\n\n{text}\n\n"
        f"Отправить всем пользователям?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отправить", callback_data="broadcast_confirm"),
                InlineKeyboardButton(text="❌ Отмена",    callback_data="broadcast_cancel"),
            ]
        ])
    )

@dp.callback_query(F.data == "broadcast_confirm")
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    text = data.get("broadcast_text", "")
    await state.clear()
    await callback.message.edit_text("📤 Начинаю рассылку...")

    user_ids = await db.db_pool.fetch(
        'SELECT user_id FROM users WHERE user_id != $1', ADMIN_ID
    )
    total = len(user_ids)
    sent = 0
    blocked = 0

    for row in user_ids:
        uid = row["user_id"]
        try:
            await callback.bot.send_message(uid, text)
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except Exception:
            blocked += 1
        await asyncio.sleep(0.05)  # 20 сообщений/сек — в пределах лимита Telegram

    await callback.message.answer(
        f"✅ Рассылка завершена\n\n"
        f"👥 Всего: {total}\n"
        f"📨 Отправлено: {sent}\n"
        f"🚫 Не доставлено: {blocked}"
    )

@dp.callback_query(F.data == "broadcast_cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await callback.message.edit_text("❌ Рассылка отменена.")

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
        if result == 'used':
            used_verb = "активировал" if db.is_male(user) else "активировала"
            await callback.answer(
                f"❌ Ты уже {used_verb} этот промокод — он даётся один раз в руки.",
                show_alert=True
            )
            return
        user["purchased"].append(key)
        user["waiting"] = key
        await db.save_user(callback.from_user.id, user)
        remaining = await db.coupon_remaining(code)
        await callback.answer(f"✅ Добавлено! Осталось использований промокода: {remaining}")

    await _start_date_flow(callback.message, state, user, key)

def _build_upsells(key: str, user: dict) -> list[dict]:
    """Список апселлов для страницы CTA в PDF — те же кандидаты, что и в
    upsell_menu(), но простыми dict без объектов aiogram (pdf.py не зависит
    от aiogram/config — принимает только голые данные)."""
    purchased = user.get("purchased", [])
    result = []
    for s in UPSELLS.get(key, ()):
        if s not in purchased:
            result.append({
                "title": TITLES.get(s, s),
                "desc":  RAZBOR_DESCRIPTIONS.get(s, ""),
                "price": config.price_of(s, 49),
            })
    return result

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
    await _process_date(callback.message, callback.from_user.id, user, user["birth_date"], state, is_free=is_free)

@dp.callback_query(F.data == "use_new_date")
async def use_new_date(callback: CallbackQuery, state: FSMContext):
    # Другая дата = чужой разбор — числа имени (душа/личность/имя) в контексте
    # промпта иначе считались бы по ИМЕНИ ВЛАДЕЛЬЦА АККАУНТА, а не того, о ком
    # разбор. Раньше это молча пролезало (см. _process_date): бот писал "разбор
    # для Руслана" даже когда дату вводили для другого человека.
    await callback.answer()
    await callback.message.answer(
        "👤 Для кого этот разбор? Введи имя.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Без имени", callback_data="other_name_skip")]
        ])
    )
    await state.set_state(Form.waiting_other_name)

@dp.message(StateFilter(Form.waiting_other_name))
async def handle_other_name(message: Message, state: FSMContext):
    name = sanitize_name(message.text or "")
    if len(name) < 2 or len(name) > 30:
        await message.answer("Введи имя текстом — только буквы, от 2 до 30 символов, или нажми «Без имени» 😊")
        return
    await state.update_data(other_name=name)
    await message.answer("📅 Введи дату рождения в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
    await state.set_state(Form.waiting_date)

@dp.callback_query(F.data == "other_name_skip")
async def other_name_skip_cb(callback: CallbackQuery, state: FSMContext):
    # Явно ставим нейтральное имя — иначе db.default_name(user) вернул бы
    # РЕАЛЬНОЕ имя владельца аккаунта (у него есть first_name), и разбор для
    # чужой даты снова подписался бы, например, «Руслан».
    await callback.answer()
    await state.update_data(other_name="дорогая")
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

async def _create_and_send_rub_payment(target: Message, user_id: int, payload: str, price_rub: int, title: str, description: str, method: str | None, email: str):
    """Создаёт платёж в ЮKassa и присылает кнопку оплаты. Общий хвост для
    случая с уже сохранённым email и для только что введённого."""
    from yookassa_pay import create_payment
    bot_info = await bot.get_me()
    try:
        payment_id, url = await create_payment(
            amount_rub=price_rub,
            description=description or title,
            return_url=f"https://t.me/{bot_info.username}",
            metadata={"user_id": str(user_id), "payload": payload},
            email=email,
            method=method,
        )
    except Exception as e:
        logging.error(f"YooKassa create_payment error: {e}", exc_info=True)
        await target.answer("❌ Не удалось создать оплату — попробуй чуть позже 🙏", reply_markup=_MENU_BACK_MARKUP)
        return
    method_line = {"bank_card": "картой", "sbp": "через СБП"}.get(method, "картой, СБП и другими способами")
    await target.answer(
        f"«{title}» — {price_rub}₽.\n\n💳 Оплати {method_line} по кнопке ниже.\n"
        "После оплаты доступ откроется автоматически в течение минуты.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Оплатить {price_rub}₽", url=url)]
        ])
    )

async def _pay_rub(message_or_callback, state: FSMContext, payload: str, price_rub: int, title: str, description: str, method: str | None = None):
    """Оплата рублями — email для чека ЮKassa обязателен на уровне API (без
    него платёж не создаётся, проверено на практике). Если email уже
    сохранён с прошлой оплаты — сразу создаём платёж, заново не спрашиваем."""
    user_id = message_or_callback.from_user.id
    target  = message_or_callback.message if isinstance(message_or_callback, CallbackQuery) else message_or_callback
    user = await db.get_user(user_id)
    saved_email = user.get("email")
    if saved_email:
        await _create_and_send_rub_payment(target, user_id, payload, price_rub, title, description, method, saved_email)
        return
    await state.update_data(rub_payload=payload, rub_amount=price_rub, rub_title=title, rub_desc=description, rub_method=method)
    await state.set_state(Form.waiting_rub_email)
    await target.answer(
        f"«{title}» — {price_rub}₽.\n\nВведи email — на него придёт чек об оплате (запомню на будущее):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="cancel_to_menu")]
        ])
    )

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

@dp.message(StateFilter(Form.waiting_rub_email))
async def handle_rub_email(message: Message, state: FSMContext):
    email = (message.text or "").strip()
    if not _EMAIL_RE.match(email):
        await message.answer("❌ Похоже на неверный email — введи ещё раз (например, name@mail.ru):")
        return
    data       = await state.get_data()
    payload    = data.get("rub_payload")
    price_rub  = data.get("rub_amount")
    title      = data.get("rub_title")
    description = data.get("rub_desc")
    method     = data.get("rub_method")
    await state.clear()
    if not payload:
        await message.answer("❌ Что-то пошло не так — попробуй заново из меню.", reply_markup=_MENU_BACK_MARKUP)
        return
    await db.set_email(message.from_user.id, email)
    await _create_and_send_rub_payment(message, message.from_user.id, payload, price_rub, title, description, method, email)

async def _start_date_flow(message: Message, state: FSMContext, user: dict, key: str, is_free: bool = False):
    """Общий переход к вводу даты(-ат) после того как разбор уже точно
    доступен пользователю (куплен, оплачен балансом, взят по купону/бесплатно).
    is_free переключает на free_-состояния, чтобы неудачная генерация не
    сжигала платный счёт за бесплатную попытку (см. _process_date/_process_two_dates)."""
    if key in TWO_DATE_KEYS:
        intro = "💑 Введи две даты через запятую"
        await message.answer(f"{intro}:\nНапример: 15.03.1995, 22.07.1998")
        await state.set_state(Form.waiting_free_second_date if is_free else Form.waiting_second_date)
    elif key == "business_name":
        # Разбор по НАЗВАНИЮ, а не по дате — просим текст, а не дату рождения.
        await message.answer(
            "💼 Введи название бизнеса, бренда или проекта, которое разобрать:\n"
            "Например: Ромашка, EvaShop, Мой салон"
        )
        await state.set_state(Form.waiting_business_name)
    else:
        await _ask_date(message, user, key=key)
        await state.set_state(Form.waiting_free_date if is_free else Form.waiting_date)

async def _resume_already_purchased(callback: CallbackQuery, state: FSMContext, user: dict, key: str):
    user["waiting"] = key
    await db.save_user(callback.from_user.id, user)
    await callback.answer()
    await _start_date_flow(callback.message, state, user, key)

async def _premium_unlock(callback: CallbackQuery, state: FSMContext, user: dict, key: str) -> bool:
    """Пытается открыть платный разбор по подписке. Возвращает True если
    разбор открыт (или лимит исчерпан — в обоих случаях покупку показывать не
    надо). False — подписки нет, идём обычным путём оплаты."""
    if not db.is_premium(user) or key not in PAID_RAZBORY:
        return False
    reason = await db.premium_try_consume(
        callback.from_user.id, PREMIUM_DAILY_LIMIT, PREMIUM_MONTHLY_LIMIT
    )
    if reason == "day":
        await callback.answer(
            f"💎 На сегодня открыто {PREMIUM_DAILY_LIMIT} новых разбора — это дневной лимит подписки. "
            "Уже открытые разборы доступны без ограничений, а новые — завтра 🌸",
            show_alert=True
        )
        return True
    if reason == "month":
        await callback.answer(
            f"💎 В этом месяце открыто {PREMIUM_MONTHLY_LIMIT} разборов — это месячный лимит подписки. "
            "Уже открытые доступны без ограничений, а новые — со следующего месяца 🌸",
            show_alert=True
        )
        return True
    user["purchased"].append(key)
    user["waiting"] = key
    await db.save_user(callback.from_user.id, user)
    await callback.answer("💎 Открыто по подписке")
    await _start_date_flow(callback.message, state, user, key)
    return True

@dp.callback_query(F.data.startswith("buy_"))
async def buy_handler(callback: CallbackQuery, state: FSMContext):
    key  = callback.data.replace("buy_", "")
    user = await db.get_user(callback.from_user.id)
    if key in user["purchased"]:
        await _resume_already_purchased(callback, state, user, key)
        return
    if await _premium_unlock(callback, state, user, key):
        return
    if key in PAID_RAZBORY:
        price     = config.price_of(key, 49)
        title     = PAID_RAZBORY[key]
        balance   = user.get("ref_balance", 0)
        price_rub = rub_price(price) if YOOKASSA_SHOP_ID else None
        if balance >= price or price_rub:
            price_line = f"{price} ⭐" + (f" / {price_rub}₽" if price_rub else "")
            base = config.PRICES.get(key, 49)
            disc_note = ""
            if config.get_discount() and base != price:
                old = f"{base} ⭐" + (f" / {rub_price(base)}₽" if price_rub else "")
                disc_note = f"\n🔥 Акция −{config.get_discount()}%: было {old}"
            await callback.message.answer(
                f"«{title}» — {price_line}.{disc_note}\n\nКак оплатить?",
                reply_markup=payment_choice_menu(key, price, price_rub, balance)
            )
        else:
            desc = RAZBOR_DESCRIPTIONS.get(key, title)
            await send_invoice(callback.message.chat.id, title, desc, key, price)
    await callback.answer()

@dp.message(Command("buy_preview"), StateFilter("*"))
async def buy_preview_cmd(message: Message, state: FSMContext):
    """Только для админа — показывает экран выбора оплаты (⭐/₽/баланс) для
    конкретного разбора в обход авто-владения всеми разборами у ADMIN_ID
    (см. db.get_user). /buy_preview matrix_full. Для скриншотов в поддержку."""
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    parts = message.text.strip().split()
    key = parts[1] if len(parts) > 1 else "matrix_full"
    if key not in PAID_RAZBORY:
        await message.answer(f"Нет такого разбора: {key}")
        return
    price     = config.price_of(key, 49)
    title     = PAID_RAZBORY[key]
    price_rub = rub_price(price) if YOOKASSA_SHOP_ID else None
    price_line = f"{price} ⭐" + (f" / {price_rub}₽" if price_rub else "")
    await message.answer(
        f"«{title}» — {price_line}.\n\nКак оплатить?",
        reply_markup=payment_choice_menu(key, price, price_rub, balance=0)
    )

@dp.callback_query(F.data.startswith("rub_card_buy_") | F.data.startswith("rub_sbp_buy_"))
async def rub_buy_handler(callback: CallbackQuery, state: FSMContext):
    """Оплата разбора рублями через прямой API ЮKassa. Карта и СБП/QR —
    отдельные кнопки, каждая сразу ведёт на свой способ оплаты на странице
    ЮKassa (см. _pay_rub)."""
    if callback.data.startswith("rub_card_buy_"):
        key, method = callback.data.replace("rub_card_buy_", ""), "bank_card"
    else:
        key, method = callback.data.replace("rub_sbp_buy_", ""), "sbp"
    user = await db.get_user(callback.from_user.id)
    if key in user["purchased"]:
        await _resume_already_purchased(callback, state, user, key)
        return
    if key in PAID_RAZBORY and YOOKASSA_SHOP_ID:
        price     = config.price_of(key, 49)
        price_rub = rub_price(price)
        title     = PAID_RAZBORY[key]
        desc      = RAZBOR_DESCRIPTIONS.get(key, title)
        await _pay_rub(callback, state, key, price_rub, title, desc, method=method)
    await callback.answer()

@dp.callback_query(F.data.startswith("stars_buy_"))
async def stars_buy_handler(callback: CallbackQuery, state: FSMContext):
    """Явный выбор оплаты звёздами Telegram вместо бонусного баланса —
    показывается когда баланса хватало бы на разбор, но юзер всё равно
    хочет заплатить обычным способом."""
    key  = callback.data.replace("stars_buy_", "")
    user = await db.get_user(callback.from_user.id)
    if key in user["purchased"]:
        await _resume_already_purchased(callback, state, user, key)
        return
    if key in PAID_RAZBORY:
        price = config.price_of(key, 49)
        title = PAID_RAZBORY[key]
        desc  = RAZBOR_DESCRIPTIONS.get(key, title)
        await send_invoice(callback.message.chat.id, title, desc, key, price)
    await callback.answer()

@dp.callback_query(F.data == "gift_start")
async def gift_start_cb(callback: CallbackQuery):
    await callback.message.answer(
        "🎁 Выбери разбор, который хочешь подарить — пришлю тебе ссылку, "
        "которую перешлёшь подруге. Она сама выберет дату и заберёт подарок.",
        reply_markup=gift_sections_menu()
    )
    await callback.answer()

_GIFT_SECTION_MENUS = {
    "destiny": section_destiny_menu,
    "money":   section_money_menu,
    "love":    section_love_menu,
    "health":  section_health_menu,
    "past":    section_past_menu,
}

@dp.callback_query(F.data.startswith("giftsection_"))
async def gift_section_cb(callback: CallbackQuery):
    name = callback.data.replace("giftsection_", "")
    menu_fn = _GIFT_SECTION_MENUS.get(name)
    if not menu_fn:
        await callback.answer()
        return
    await callback.message.answer("🎁 Выбери разбор для подарка:", reply_markup=menu_fn(gift=True))
    await callback.answer()

@dp.callback_query(F.data.startswith("gift_") & ~F.data.in_({"gift_start"}))
async def gift_buy_handler(callback: CallbackQuery):
    key = callback.data.replace("gift_", "")
    if key not in PAID_RAZBORY:
        await callback.answer()
        return
    price = config.price_of(key, 49)
    title = PAID_RAZBORY[key]
    desc  = f"Подарок: {RAZBOR_DESCRIPTIONS.get(key, title)}"
    await send_invoice(callback.message.chat.id, f"🎁 {title}", desc, f"gift_{key}", price)
    await callback.answer()

@dp.callback_query(F.data.startswith("balance_buy_"))
async def balance_buy_handler(callback: CallbackQuery, state: FSMContext):
    key  = callback.data.replace("balance_buy_", "")
    user = await db.get_user(callback.from_user.id)
    if key not in PAID_RAZBORY:
        await callback.answer()
        return
    if key in user["purchased"]:
        await _resume_already_purchased(callback, state, user, key)
        return

    price = config.price_of(key, 49)
    # Замок на юзера: без него двойной клик прошёл бы обе проверки «не куплено»
    # и списал бы баланс дважды за один разбор (см. db.user_lock).
    async with db.user_lock(callback.from_user.id):
        user = await db.get_user(callback.from_user.id)
        if key in user["purchased"]:
            await _resume_already_purchased(callback, state, user, key)
            return
        spent = await db.spend_balance(callback.from_user.id, price)
        if not spent:
            await callback.answer("❌ На балансе уже не хватает звёзд — обнови баланс командой /balance.", show_alert=True)
            return
        user = await db.get_user(callback.from_user.id)  # перечитываем — баланс уже списан
        user["purchased"].append(key)
        user["waiting"] = key
        await db.save_user(callback.from_user.id, user)
    await callback.answer(f"✅ Оплачено балансом! Списано {price} ⭐")
    await _start_date_flow(callback.message, state, user, key)

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message, state: FSMContext):
    user    = await db.get_user(message.from_user.id)
    sp      = message.successful_payment
    payload = sp.invoice_payload
    # total_amount — минимальные единицы валюты: для Stars (XTR) это сами
    # звёзды, для RUB — копейки. Реф-бонус считаем в звёздах-эквиваленте.
    # Сейчас сюда приходят ТОЛЬКО Stars-платежи (все наши Telegram-инвойсы —
    # currency="XTR"); рубли идут мимо, через отдельный вебхук ЮKassa. Ветку RUB
    # оставляем защитно — на случай если когда-то вернём Telegram-инвойс в рублях.
    if sp.currency == "RUB":
        amount = round(sp.total_amount / 100 / STARS_TO_RUB_RATE)
    else:
        amount = sp.total_amount

    # ── ПРЕМИУМ-ПОДПИСКА (первая оплата и все ежемесячные продления) ──
    # Telegram присылает successful_payment и при оформлении, и при каждом
    # автосписании — оба раза с subscription_expiration_date. Продлеваем до
    # этой даты; если её вдруг нет — страхуемся 31 днём от текущего момента.
    if payload == PREMIUM_PAYLOAD:
        until = sp.subscription_expiration_date
        if until is not None:
            until = until.replace(tzinfo=None)
        else:
            until = utc_now() + timedelta(days=31)
        await db.set_premium(message.from_user.id, until)
        await db.log_payment(message.from_user.id, None, amount, sp.currency)

        # Реф-бонус начисляем только с ПЕРВОЙ оплаты подписки, не с продлений.
        referrer_id = user.get("referred_by")
        if referrer_id and getattr(sp, "is_first_recurring", False):
            bonus = max(1, round(amount * REF_BONUS_PERCENT / 100))
            try:
                await db.add_ref_bonus(referrer_id, message.from_user.id, bonus, "premium")
            except Exception as e:
                logging.warning(f"Premium ref bonus error: {e}")

        if getattr(sp, "is_first_recurring", False) or not getattr(sp, "is_recurring", False):
            await message.answer(
                f"💎 Добро пожаловать в Премиум, {db.default_name(user)}!\n\n"
                f"Все разборы открыты до {until.strftime('%d.%m.%Y')}, каждое утро будет "
                "приходить твой личный прогноз, а генерация идёт без очереди 🌸\n\n"
                "Выбирай любой разбор 👇",
                reply_markup=main_menu_for(message.from_user.id, user)
            )
        else:
            await message.answer(f"💎 Премиум продлён до {until.strftime('%d.%m.%Y')} — спасибо, что со мной 🌸")
        await state.clear()
        return

    # ── ПОДАРОК РАЗБОРА ──────────────────────────────────────────────────
    if payload.startswith("gift_"):
        key = payload.removeprefix("gift_")
        if key not in PAID_RAZBORY:
            logging.warning(f"gift payment с неизвестным key={key!r} от user {message.from_user.id}")
            await message.answer("❌ Что-то пошло не так с оплатой — напиши в поддержку.", reply_markup=_MENU_BACK_MARKUP)
            await state.clear()
            return
        code = secrets.token_hex(4)
        await db.create_gift(code, key, message.from_user.id)
        await db.log_payment(message.from_user.id, key, amount, sp.currency)
        bot_info = await bot.get_me()
        gift_link = f"https://t.me/{bot_info.username}?start=gift_{code}"
        title = PAID_RAZBORY[key]
        await message.answer(
            f"🎁 Готово! «{title}» ждёт получателя.\n\n"
            f"Перешли эту ссылку подруге — она откроет бота и заберёт подарок:\n{gift_link}",
        )
        await state.clear()
        return

    if payload not in PAID_RAZBORY:
        logging.warning(f"successful_payment с неизвестным payload={payload!r} от user {message.from_user.id}")
        await message.answer("❌ Что-то пошло не так с оплатой — напиши в поддержку.", reply_markup=_MENU_BACK_MARKUP)
        await state.clear()
        return

    if payload not in user["purchased"]:
        user["purchased"].append(payload)
    user["waiting"] = payload
    await db.save_user(message.from_user.id, user)
    await db.log_payment(message.from_user.id, payload, amount, sp.currency)

    # Начисляем реферальный бонус пригласившему
    referrer_id = user.get("referred_by")
    if referrer_id:
        bonus = max(1, round(amount * REF_BONUS_PERCENT / 100))
        try:
            await db.add_ref_bonus(referrer_id, message.from_user.id, bonus, payload)
            buyer_name = user.get("first_name") or ("Друг" if db.is_male(user) else "Подруга")
            bought_verb = "купил" if db.is_male(user) else "купила"
            title      = TITLES.get(payload, "разбор")
            await bot.send_message(
                referrer_id,
                f"🎉 +{bonus} ⭐ на твой баланс!\n\n"
                f"{buyer_name} {bought_verb} «{title}» по твоей реферальной ссылке.\n"
                f"Проверить баланс: /balance"
            )
        except Exception as e:
            logging.warning(f"Ref bonus error for referrer {referrer_id}: {e}")

    if payload in TWO_DATE_KEYS:
        await message.answer("✅ Оплата прошла!")
    await _start_date_flow(message, state, user, payload)

# ─── ОБРАБОТКА ДАТ ───────────────────────────────────────────────────────────
def _repeat_choice_menu(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Показать этот разбор", callback_data=f"showcache_{key}")],
        [InlineKeyboardButton(text="📅 Сделать на другую дату", callback_data=f"redate_{key}")],
        [InlineKeyboardButton(text="🔮 Меню разборов", callback_data="show_menu")],
    ])

async def _process_date(message: Message, user_id: int, user: dict, date_str: str,
                        state: FSMContext, is_free: bool = False, confirmed_repeat: bool = False):
    number  = calculate_destiny(date_str)
    waiting = user.get("waiting")
    fsm_data = await state.get_data()
    subject_name = fsm_data.get("other_name")
    name    = subject_name or db.default_name(user)
    if not waiting:
        await message.answer("Выбери разбор из меню 👇", reply_markup=main_menu_for(message.from_user.id, user))
        await state.clear()
        return

    # Этот разбор на эту же дату уже делали — не перегенерируем (иначе ИИ мог бы
    # противоречить прошлому тексту), но и не вываливаем старый текст с сухой
    # оговоркой: спрашиваем заранее — показать тот же или взять другую дату.
    if not confirmed_repeat:
        cached = await db.get_reading_text(user_id, waiting)
        if cached and cached.get("date_str") == date_str:
            await state.clear()
            await message.answer(
                f"🌸 Этот разбор для {date_str} у тебя уже готов — твои числа не меняются, "
                "поэтому и разбор останется тем же.\n\nОткрыть его снова или сделать на другую дату?",
                reply_markup=_repeat_choice_menu(waiting)
            )
            return

    # Замок: не запускаем вторую генерацию пока идёт первая
    if user_id in _generating:
        await message.answer("⏳ Твой разбор уже готовится — дождись его, пожалуйста 🔮")
        return

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
        title, answer, from_cache = await generate_single(user_id, user, waiting, date_str, subject_name=subject_name)
        await stop_intermediate()
        await send_long(message.chat.id, f"{title}\n\n{answer}")

        try:
            pdf_bytes = await _generate_pdf_async(
                title, answer, user_name=name, destiny_number=number,
                birth_date=date_str, upsells=_build_upsells(waiting, user),
                ref_bonus_percent=REF_BONUS_PERCENT,
            )
            pdf_file = BufferedInputFile(pdf_bytes, filename=f"{title}.pdf")
            await bot.send_document(
                message.chat.id,
                pdf_file,
                caption="📄 Твой разбор в PDF — сохрани себе!"
            )
        except Exception as pdf_err:
            logging.warning(f"PDF generation failed for {waiting}: {pdf_err}")

        await message.answer(
            "❓ Остались вопросы по этому разбору? Уточни у меня напрямую 👇",
            reply_markup=_followup_menu(waiting)
        )

        kb = upsell_menu(waiting, user)
        has_upsells = any(
            btn.callback_data and btn.callback_data.startswith("buy_")
            for row in kb.inline_keyboard for btn in row
        )
        upsell_text = "✨ Тебе также может подойти 👇" if has_upsells else "🔮 Хочешь ещё разбор?"
        # сбрасываем waiting чтобы повторный use_my_date не запустил этот же разбор
        user["waiting"] = None
        if is_free:
            user["free_used"] = True
        await db.save_user(user_id, user)
        await message.answer(upsell_text, reply_markup=kb)
        await state.clear()
    except RegenLimitReached as e:
        await stop_intermediate()
        user["waiting"] = None
        await db.save_user(user_id, user)
        await message.answer(_regen_limit_text(e.limit, user), reply_markup=_MENU_BACK_MARKUP)
        await state.clear()
    except Exception as e:
        await stop_intermediate()
        logging.error(f"Date handler error [{waiting}]: {e}", exc_info=True)
        retry_text = (
            "❌ Что-то пошло не так. Твоя бесплатная попытка не сгорела — "
            "нажми кнопку и попробуй снова 👇" if is_free else
            "❌ Что-то пошло не так. Твоя покупка сохранена — нажми кнопку и попробуй снова 👇"
        )
        await message.answer(retry_text, reply_markup=retry_menu(waiting, is_free=is_free))
        await state.clear()

@dp.callback_query(F.data.startswith("showcache_"))
async def showcache_cb(callback: CallbackQuery, state: FSMContext):
    """«Показать этот разбор» из меню повтора — отдаём тот же текст (+PDF,
    уточнения, апселлы), без перегенерации и без сухой оговорки."""
    key    = callback.data.replace("showcache_", "")
    user   = await db.get_user(callback.from_user.id)
    cached = await db.get_reading_text(callback.from_user.id, key)
    await callback.answer()
    if not cached or not cached.get("date_str"):
        user["waiting"] = key
        await db.save_user(callback.from_user.id, user)
        await _start_date_flow(callback.message, state, user, key)
        return
    user["waiting"] = key
    await db.save_user(callback.from_user.id, user)
    if key in TWO_DATE_KEYS:
        parts = cached["date_str"].split(",")
        if len(parts) == 2:
            await _process_two_dates(callback.message, callback.from_user.id, user, parts, state, confirmed_repeat=True, key=key)
    elif key == "business_name":
        # date_str тут хранит название, а не дату — не гоним через _process_date
        # (там calculate_destiny упал бы). Просто просим название заново
        # (generate_name отдаст из кэша, если название то же).
        await _start_date_flow(callback.message, state, user, key)
    else:
        await _process_date(callback.message, callback.from_user.id, user, cached["date_str"], state, confirmed_repeat=True)

@dp.callback_query(F.data.startswith("redate_"))
async def redate_cb(callback: CallbackQuery, state: FSMContext):
    """«Сделать на другую дату» из меню повтора — снова спрашиваем дату(-ы)."""
    key  = callback.data.replace("redate_", "")
    user = await db.get_user(callback.from_user.id)
    user["waiting"] = key
    await db.save_user(callback.from_user.id, user)
    await callback.answer()
    await _start_date_flow(callback.message, state, user, key)

@dp.callback_query(F.data.startswith("startreading_"))
async def startreading_cb(callback: CallbackQuery, state: FSMContext):
    """Универсальный запуск ввода данных для купленного разбора — правильно
    роутит по типу (дата / две даты / название). Нужен рублёвому вебхуку
    ЮKassa: там нет FSM-контекста, поэтому даём кнопку, а флоу поднимает бот."""
    key = callback.data.replace("startreading_", "")
    user = await db.get_user(callback.from_user.id)
    if key not in user.get("purchased", []):
        await callback.answer()
        return
    user["waiting"] = key
    await db.save_user(callback.from_user.id, user)
    await callback.answer()
    await _start_date_flow(callback.message, state, user, key)

_SUBJECT_RE = re.compile(r"[\r\n\t]+")

def _sanitize_subject(raw: str) -> str:
    """Название бизнеса/бренда для разбора business_name. В отличие от имени
    человека тут допустимы цифры и латиница (EvaShop, Студия 5), поэтому режем
    только переводы строк (защита промпта) и длину. Само название идёт в промпт
    в кавычках «{subject}»."""
    cleaned = _SUBJECT_RE.sub(" ", raw or "").strip()
    return re.sub(r"\s{2,}", " ", cleaned)[:50]

@dp.message(StateFilter(Form.waiting_business_name))
async def handle_business_name(message: Message, state: FSMContext):
    """Разбор нумерологии названия бизнеса — считается по введённому названию,
    а не по дате (см. generation.generate_name / _start_date_flow)."""
    user_id = message.from_user.id
    subject = _sanitize_subject(message.text or "")
    if len(subject) < 2:
        await message.answer("Введи название текстом — хотя бы пару букв 🙂")
        return
    user = await db.get_user(user_id)
    if user_id in _generating:
        await message.answer("⏳ Твой разбор уже готовится — дождись его, пожалуйста 🔮")
        return
    wait_msg = await message.answer("⏳ Ева разбирает название по числам... Подожди немного ✨")
    try:
        title, answer, _ = await generate_name(user_id, user, "business_name", subject)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await send_long(message.chat.id, f"{title} «{subject}»\n\n{answer}")
        try:
            pdf_bytes = await _generate_pdf_async(
                f"{title} «{subject}»", answer,
                user_name=user.get("first_name") or "", destiny_number=calculate_name_number(subject),
                birth_date=subject, upsells=_build_upsells("business_name", user),
                ref_bonus_percent=REF_BONUS_PERCENT,
            )
            pdf_file = BufferedInputFile(pdf_bytes, filename="Нумерология названия.pdf")
            await bot.send_document(message.chat.id, pdf_file, caption="📄 Разбор в PDF — сохрани себе!")
        except Exception as pdf_err:
            logging.warning(f"PDF business_name error: {pdf_err}")
        await message.answer(
            "❓ Остались вопросы по этому разбору? Уточни у меня напрямую 👇",
            reply_markup=_followup_menu("business_name")
        )
        user["waiting"] = None
        await db.save_user(user_id, user)
        await message.answer("✨ Тебе также может подойти 👇", reply_markup=upsell_menu("business_name", user))
        await state.clear()
    except RegenLimitReached as e:
        try:
            await wait_msg.delete()
        except Exception:
            pass
        user["waiting"] = None
        await db.save_user(user_id, user)
        await message.answer(_regen_limit_text(e.limit, user), reply_markup=_MENU_BACK_MARKUP)
        await state.clear()
    except Exception as e:
        logging.error(f"business_name error: {e}", exc_info=True)
        await message.answer(
            "❌ Что-то пошло не так. Твоя покупка сохранена — нажми кнопку и попробуй снова 👇",
            reply_markup=retry_menu("business_name")
        )
        await state.clear()

def _parse_two_dates(text: str) -> list[str] | None:
    # Две даты разделяются запятой; нормализуем КАЖДУЮ дату отдельно (не всю
    # строку — иначе запятая-разделитель тоже стала бы точкой). Возвращаем уже
    # нормализованные части, чтобы дальше calculate_destiny разбил их по точке.
    if "," not in text:
        return None
    parts = [normalize_date(p) for p in text.split(",")]
    if len(parts) != 2 or not all(is_valid_date(p) for p in parts):
        return None
    return parts

@dp.message(StateFilter(Form.waiting_second_date))
async def handle_two_dates(message: Message, state: FSMContext):
    user  = await db.get_user(message.from_user.id)
    text  = (message.text or "").strip()
    parts = _parse_two_dates(text)
    if parts is None:
        await message.answer("❌ Введи две даты через запятую.\nНапример: 15.03.1995, 22.07.1998")
        return
    key = user.get("waiting") if user.get("waiting") in TWO_DATE_KEYS else "compat"
    await _process_two_dates(message, message.from_user.id, user, parts, state, is_free=False, key=key)

@dp.message(StateFilter(Form.waiting_free_second_date))
async def handle_free_two_dates(message: Message, state: FSMContext):
    user  = await db.get_user(message.from_user.id)
    text  = (message.text or "").strip()
    parts = _parse_two_dates(text)
    if parts is None:
        await message.answer("❌ Введи две даты через запятую.\nНапример: 15.03.1995, 22.07.1998")
        return
    # free_used выставляется в _process_two_dates только при успехе —
    # тот же принцип, что и в одиночном бесплатном флоу (см. _process_date).
    key = user.get("waiting") if user.get("waiting") in TWO_DATE_KEYS else "compat"
    await _process_two_dates(message, message.from_user.id, user, parts, state, is_free=True, key=key)

async def _process_two_dates(message: Message, user_id: int, user: dict, parts: list[str],
                              state: FSMContext, is_free: bool = False, confirmed_repeat: bool = False,
                              key: str = "compat"):
    name    = db.default_name(user)
    title_default = TITLES.get(key, "💑 Совместимость")
    wait_word = "энергетику двух людей"

    # Тот же разбор совместимости на те же две даты уже есть — спрашиваем,
    # а не перегенерируем (см. _process_date).
    if not confirmed_repeat:
        cached = await db.get_reading_text(user_id, key)
        if cached and cached.get("date_str") == f"{parts[0]},{parts[1]}":
            await state.clear()
            await message.answer(
                f"🌸 Разбор для {parts[0]} и {parts[1]} у тебя уже готов.\n\n"
                "Открыть его снова или взять другие даты?",
                reply_markup=_repeat_choice_menu(key)
            )
            return

    if user_id in _generating:
        await message.answer("⏳ Твой разбор уже готовится — дождись его, пожалуйста 🔮")
        return

    wait_msg = await message.answer(f"⏳ Ева составляет {title_default.split(' ', 1)[-1].lower()}...")

    async def send_intermediate():
        await asyncio.sleep(20)
        try:
            await bot.edit_message_text(
                f"⏳ Разбираю {wait_word}... Ещё немного 🔮",
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
        n1 = calculate_destiny(parts[0])
        title, answer, from_cache = await generate_compat(user_id, user, parts[0], parts[1], key=key)
        await stop_intermediate()
        await send_long(message.chat.id, f"{title}\n\n{answer}")

        try:
            pdf_bytes = await _generate_pdf_async(
                title_default, answer, user_name=name, destiny_number=n1,
                birth_date=parts[0], upsells=_build_upsells(key, user),
                ref_bonus_percent=REF_BONUS_PERCENT,
            )
            pdf_file  = BufferedInputFile(pdf_bytes, filename=f"{title_default.split(' ', 1)[-1]}.pdf")
            await bot.send_document(message.chat.id, pdf_file, caption="📄 Разбор в PDF — сохрани себе!")
        except Exception as pdf_err:
            logging.warning(f"PDF {key} error: {pdf_err}")

        await message.answer(
            "❓ Остались вопросы по этому разбору? Уточни у меня напрямую 👇",
            reply_markup=_followup_menu(key)
        )

        kb = upsell_menu(key, user)
        has_upsells = any(
            btn.callback_data and btn.callback_data.startswith("buy_")
            for row in kb.inline_keyboard for btn in row
        )
        upsell_text = "✨ Тебе также может подойти 👇" if has_upsells else "🔮 Хочешь ещё разбор?"
        user["waiting"] = None
        if is_free:
            user["free_used"] = True
        await db.save_user(user_id, user)
        await message.answer(upsell_text, reply_markup=kb)
        await state.clear()
    except RegenLimitReached as e:
        await stop_intermediate()
        user["waiting"] = None
        await db.save_user(user_id, user)
        await message.answer(_regen_limit_text(e.limit, user), reply_markup=_MENU_BACK_MARKUP)
        await state.clear()
    except Exception as e:
        await stop_intermediate()
        logging.error(f"Compat error ({key}): {e}", exc_info=True)
        retry_text = (
            "❌ Что-то пошло не так. Твоя бесплатная попытка не сгорела — "
            "нажми кнопку и попробуй снова 👇" if is_free else
            "❌ Что-то пошло не так. Твоя покупка сохранена — нажми кнопку и попробуй снова 👇"
        )
        await message.answer(retry_text, reply_markup=retry_menu(key, is_free=is_free))
        await state.clear()

@dp.message(StateFilter(Form.waiting_date))
async def handle_date(message: Message, state: FSMContext):
    user = await db.get_user(message.from_user.id)
    text = normalize_date(message.text or "")
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

REVIEW_MAX_LEN = 600

@dp.message(StateFilter(Form.waiting_review))
async def handle_review(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if len(text) < 3:
        await message.answer("Напиши отзыв текстом, хотя бы пару слов 🙂")
        return
    if len(text) > REVIEW_MAX_LEN:
        await message.answer(
            f"Отзыв слишком длинный ({len(text)} символов) — максимум {REVIEW_MAX_LEN}. "
            f"Сократи, пожалуйста, и отправь ещё раз."
        )
        return
    user        = await db.get_user(message.from_user.id)
    name        = user.get("first_name") or "Аноним"
    data        = await state.get_data()
    review_key  = data.get("review_key", "")
    title       = TITLES.get(review_key, "разбор")
    review_text = f"⭐ Отзыв о боте @nnumerology_bot\n👤 {name}\n💫 Разбор: {title}\n\n{text}"
    reviews_left = user.get("reviews_left", [])
    if review_key and review_key not in reviews_left:
        reviews_left.append(review_key)
    user["reviews_left"] = reviews_left
    user["review_left"]  = True
    await db.save_user(message.from_user.id, user)
    await state.clear()

    flags = _review_flags(text)
    review_id = await db.add_pending_review(message.from_user.id, review_text, ", ".join(flags))
    warn = f"\n\n⚠️ Возможные нарушения: {', '.join(flags)}" if flags else ""
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📝 Новый отзыв на модерацию (#{review_id}){warn}\n\n{review_text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Одобрить",  callback_data=f"revmod_ok_{review_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"revmod_no_{review_id}"),
            ]])
        )
    except Exception as e:
        logging.error(f"Review moderation notify error: {e}")
    await message.answer("✅ Спасибо! Твой отзыв отправлен на проверку и скоро появится в канале 💫")

@dp.callback_query(F.data.startswith("revmod_"))
async def review_moderation_cb(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    action, _, review_id_str = callback.data.replace("revmod_", "").partition("_")
    try:
        review_id = int(review_id_str)
    except ValueError:
        await callback.answer("Ошибка id.", show_alert=True)
        return
    review = await db.get_pending_review(review_id)
    if not review:
        await callback.answer("Отзыв уже обработан.", show_alert=True)
        return
    await db.delete_pending_review(review_id)
    if action == "ok":
        try:
            await bot.send_message(REVIEWS_CHANNEL, review["review_text"])
            await bot.send_message(review["user_id"], "🎉 Твой отзыв одобрен и опубликован в канале!")
        except Exception as e:
            logging.error(f"Review publish error: {e}")
        await callback.message.edit_text(callback.message.text + "\n\n✅ ОДОБРЕНО")
    else:
        try:
            await bot.send_message(review["user_id"], "Твой отзыв не прошёл модерацию — проверь, нет ли в нём ссылок или нецензурной лексики, и попробуй ещё раз 🙏")
        except Exception:
            pass
        await callback.message.edit_text(callback.message.text + "\n\n❌ ОТКЛОНЕНО")
    await callback.answer()

@dp.callback_query(F.data == "show_menu")
async def show_menu(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    await callback.message.answer("🔮 Выбери разбор:", reply_markup=main_menu_for(callback.from_user.id, user))
    await callback.answer()

# ─── ПРЕМИУМ-ПОДПИСКА ────────────────────────────────────────────────────────
_PREMIUM_OFFER = (
    "💎 Ева Премиум\n\n"
    f"Подписка за {PREMIUM_PRICE} ⭐ в месяц — и я рядом каждый день, без ограничений:\n\n"
    "💬 «Спроси Еву» без лимита — задавай личные вопросы по своим числам когда угодно\n"
    "🎱 «Да / Нет» без лимита — быстрые ответы в любой момент, а не 3 в день\n"
    "🌟 Число дня с личным толкованием под твоё число судьбы\n"
    "🌅 Твой личный прогноз каждое утро — по твоим числам, а не общий\n"
    f"✨ До {PREMIUM_MONTHLY_LIMIT} разборов в месяц без покупки поштучно "
    f"(до {PREMIUM_DAILY_LIMIT} новых в день, открытые — сколько угодно раз)\n"
    "⚡ Приоритетная генерация — без очереди в часы пика\n\n"
    "Списывается раз в месяц автоматически, отменить можно в любой момент "
    "в настройках Telegram. Оформляется звёздами 👇"
)

async def _create_premium_invoice() -> str:
    return await bot.create_invoice_link(
        title=PREMIUM_TITLE,
        description="Безлимитный доступ ко всем разборам, личный прогноз каждое утро и приоритетная генерация.",
        payload=PREMIUM_PAYLOAD,
        currency="XTR",
        prices=[LabeledPrice(label="Ева Премиум — месяц", amount=PREMIUM_PRICE)],
        subscription_period=PREMIUM_PERIOD,
    )

async def _show_premium(target: Message, user: dict):
    if db.is_premium(user):
        until = user["premium_until"].strftime("%d.%m.%Y")
        await target.answer(
            f"💎 Премиум активен до {until}.\n\n"
            "Тебе открыты все разборы, каждое утро приходит личный прогноз, "
            "а генерация идёт без очереди. Спасибо, что со мной 🌸",
            reply_markup=premium_active_menu()
        )
        return
    try:
        link = await _create_premium_invoice()
    except Exception as e:
        logging.error(f"Premium invoice error: {e}", exc_info=True)
        await target.answer("❌ Не удалось открыть оплату — попробуй чуть позже 🙏", reply_markup=_MENU_BACK_MARKUP)
        return
    await target.answer(_PREMIUM_OFFER, reply_markup=premium_subscribe_menu(link, PREMIUM_PRICE_RUB if YOOKASSA_SHOP_ID else None))

@dp.callback_query(F.data == "premium_info")
async def premium_info_cb(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    await _show_premium(callback.message, user)
    await callback.answer()

@dp.callback_query(F.data.in_({"premium_pay_rub_card", "premium_pay_rub_sbp"}))
async def premium_pay_rub_cb(callback: CallbackQuery, state: FSMContext):
    """Разовая оплата премиума рублями через прямой API ЮKassa (не
    авто-подписка, как со Stars). Карта и СБП/QR — отдельные кнопки."""
    if not YOOKASSA_SHOP_ID:
        await callback.answer("Оплата рублями временно недоступна", show_alert=True)
        return
    method = "bank_card" if callback.data == "premium_pay_rub_card" else "sbp"
    await _pay_rub(
        callback, state, PREMIUM_PAYLOAD, PREMIUM_PRICE_RUB, PREMIUM_TITLE,
        "Безлимитный доступ ко всем разборам, личный прогноз каждое утро и приоритетная генерация.",
        method=method,
    )
    await callback.answer()

@dp.message(Command("premium_preview"), StateFilter("*"))
async def premium_preview_cmd(message: Message, state: FSMContext):
    """Только для админа — показывает офер с ценами (Stars + ₽) в обход
    авто-премиума, который для ADMIN_ID всегда включён (см. db.get_user).
    Нужно для скриншотов в поддержку ЮKassa: 'цены рядом с ценами звёзд'."""
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    try:
        link = await _create_premium_invoice()
    except Exception as e:
        logging.error(f"Premium invoice error: {e}", exc_info=True)
        await message.answer("❌ Не удалось открыть оплату — попробуй чуть позже 🙏")
        return
    await message.answer(_PREMIUM_OFFER, reply_markup=premium_subscribe_menu(link, PREMIUM_PRICE_RUB if YOOKASSA_SHOP_ID else None))

@dp.message(Command("premium"), StateFilter("*"))
async def premium_cmd(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    await _show_premium(message, user)

# ─── AI-ЧАТ «СПРОСИ ЕВУ» (премиум) ───────────────────────────────────────────
ASK_QUESTION_MAX_LEN = 300

def _ask_again_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Спросить ещё", callback_data="ask_eva")],
        [InlineKeyboardButton(text="🔮 Меню разборов", callback_data="show_menu")],
    ])

async def _start_ask_eva(target: Message, state: FSMContext, user: dict):
    if not db.is_premium(user):
        await _show_premium(target, user)
        return
    if not user.get("birth_date"):
        await target.answer("Сначала укажи дату рождения — выбери любой разбор в меню, чтобы я узнала твои числа 🔮")
        return
    await target.answer(
        "💬 Спроси меня о чём угодно — я твой личный нумеролог.\n\n"
        "Я знаю твои числа, твой период и все разборы, что делала для тебя, и помню "
        "наш разговор. Любовь, деньги, работа, переезд, важное решение — пиши как "
        + ("близкому другу" if db.is_male(user) else "близкой подруге")
        + ". Например: «что с деньгами в марте?» или «стоит ли сейчас менять работу?»",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="cancel_to_menu")]
        ])
    )
    await state.set_state(Form.waiting_ai_question)

@dp.callback_query(F.data == "ask_eva")
async def ask_eva_cb(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await _start_ask_eva(callback.message, state, user)
    await callback.answer()

@dp.message(Command("ask"), StateFilter("*"))
async def ask_cmd(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    await _start_ask_eva(message, state, user)

@dp.message(StateFilter(Form.waiting_ai_question))
async def handle_ai_question(message: Message, state: FSMContext):
    user_id  = message.from_user.id
    question = (message.text or "").strip()
    if len(question) < 3:
        await message.answer("Напиши вопрос текстом, хотя бы пару слов 🙂")
        return
    if len(question) > ASK_QUESTION_MAX_LEN:
        await message.answer(f"Вопрос слишком длинный — сократи до {ASK_QUESTION_MAX_LEN} символов, пожалуйста.")
        return

    user = await db.get_user(user_id)
    if not db.is_premium(user):
        await state.clear()
        await _show_premium(message, user)
        return

    if is_rude(question):
        await state.clear()
        await message.answer(rude_reply())
        return

    if user_id in _generating:
        await message.answer("⏳ Дождись, пожалуйста, пока закончится текущая генерация 🔮")
        return

    allowed = await db.ask_try_consume(user_id, ASK_DAILY_LIMIT)
    if not allowed:
        await state.clear()
        await message.answer(
            f"💎 На сегодня использовано {ASK_DAILY_LIMIT} вопросов — это дневной лимит. "
            "Возвращайся завтра, буду рада ответить снова 🌸"
        )
        return

    _generating.add(user_id)
    wait_msg = await message.answer("⏳ Ева думает над ответом...")
    try:
        from generation import answer_ask
        answer = await answer_ask(user_id, user, question)
        await message.answer(answer, reply_markup=_ask_again_menu())
    except Exception as e:
        logging.error(f"Ask Eva error: {e}", exc_info=True)
        await db.refund_ask_try(user_id)
        await message.answer("❌ Что-то пошло не так — попробуй ещё раз чуть позже 🙏", reply_markup=_ask_again_menu())
    finally:
        _generating.discard(user_id)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await state.clear()

# ─── ЧИСЛО ДНЯ ───────────────────────────────────────────────────────────────
# Ежедневный крючок возврата: личное число дня + его энергия. Бесплатно —
# число, энергия и короткий совет; премиум получает ещё персональную строку
# под его число судьбы (мягкая воронка). Ничего не хранится — считается из
# даты рождения и сегодняшней даты (см. numerology.personal_day_info).
def _day_number_back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎱 Спросить Да / Нет", callback_data="yesno_start")],
        [InlineKeyboardButton(text="🔮 Меню разборов",      callback_data="show_menu")],
    ])

@dp.callback_query(F.data == "day_number")
async def day_number_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_day_number(callback.message, callback.from_user.id)
    await callback.answer()

@dp.message(Command("today"), StateFilter("*"))
async def today_cmd(message: Message, state: FSMContext):
    await state.clear()
    await _show_day_number(message, message.from_user.id)

async def _show_day_number(target: Message, user_id: int):
    user = await db.get_user(user_id)
    if not user.get("birth_date"):
        await target.answer(
            "🌟 Чтобы узнать своё число дня, сначала укажи дату рождения — "
            "выбери любой разбор в меню, и я запомню твои числа 🌸",
            reply_markup=_day_number_back_menu()
        )
        return
    name = db.default_name(user)
    info = personal_day_info(user["birth_date"])
    today = utc_now().strftime("%d.%m")
    text = (
        f"🌟 {name}, твоё число дня на {today} — {info['number']}.\n\n"
        f"Сегодня у тебя {info['energy']}.\n"
        f"✨ {info['advice'].capitalize()}."
    )
    if db.is_premium(user):
        dn = user.get("destiny_number") or calculate_destiny(user["birth_date"])
        note = _DESTINY_DAY_NOTES.get(dn, _DESTINY_DAY_NOTES[9])
        text += f"\n\n💎 {note}"
    else:
        text += (
            "\n\n💎 С премиумом число дня приходит с личным толкованием под твоё "
            "число судьбы — и каждое утро само."
        )
    await target.answer(text, reply_markup=_day_number_back_menu())

_DESTINY_DAY_NOTES = {
    1:  "Для твоей единицы важно сегодня не ждать разрешения — первый шаг за тобой.",
    2:  "Твоя двойка сегодня читает людей особенно тонко — доверяй первому впечатлению.",
    3:  "Твоя тройка сегодня ярче обычного — говори, показывай, будь на виду.",
    4:  "Твоя четвёрка найдёт опору в делах — то, что заложишь сегодня, будет прочным.",
    5:  "Твоя пятёрка сегодня тянется к новому — не отказывай себе в перемене.",
    6:  "Твоя шестёрка сегодня нужна близким — тепло, отданное сегодня, вернётся.",
    7:  "Твоей семёрке сегодня стоит побыть в тишине — там придёт нужный ответ.",
    8:  "Твоя восьмёрка сегодня сильна в деньгах и решениях — действуй уверенно.",
    9:  "Твоя девятка сегодня видит суть — отпусти то, что уже отжило.",
    11: "Твоё число 11 сегодня резонирует с днём — будь внимательна к знакам и снам.",
    22: "Твоё число 22 сегодня даёт особую практическую силу — берись за большое.",
    33: "Твоё число 33 сегодня светит ярче — твоя забота сегодня целительна.",
}

# ─── ДА / НЕТ ────────────────────────────────────────────────────────────────
# Микро-фича: быстрый вопрос → мгновенный ответ Да/Нет по числам. Частый
# лёгкий крючок. Бесплатно YESNO_FREE_LIMIT в день, премиум — безлимит.
@dp.callback_query(F.data == "yesno_start")
async def yesno_start_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await db.get_user(callback.from_user.id)
    if not user.get("birth_date"):
        await callback.message.answer(
            "🎱 Чтобы я отвечала по твоим числам, сначала укажи дату рождения — "
            "выбери любой разбор в меню 🌸",
            reply_markup=_day_number_back_menu()
        )
        await callback.answer()
        return
    await state.set_state(Form.waiting_yesno)
    await callback.message.answer(
        "🎱 Задай вопрос, на который нужен ответ Да или Нет.\n"
        "Например: «стоит ли соглашаться на эту работу?» или «позвонить ему сегодня?»",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="cancel_to_menu")]
        ])
    )
    await callback.answer()

@dp.message(Command("yesno"), StateFilter("*"))
async def yesno_cmd(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user.get("birth_date"):
        await message.answer(
            "🎱 Сначала укажи дату рождения — выбери любой разбор в меню 🌸",
            reply_markup=_day_number_back_menu()
        )
        return
    await state.set_state(Form.waiting_yesno)
    await message.answer(
        "🎱 Задай вопрос, на который нужен ответ Да или Нет 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="cancel_to_menu")]
        ])
    )

def _yesno_again_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎱 Ещё вопрос", callback_data="yesno_start")],
        [InlineKeyboardButton(text="🔮 Меню разборов", callback_data="show_menu")],
    ])

@dp.message(StateFilter(Form.waiting_yesno))
async def handle_yesno(message: Message, state: FSMContext):
    user_id  = message.from_user.id
    question = (message.text or "").strip()
    if len(question) < 3:
        await message.answer("Напиши вопрос текстом, хотя бы пару слов 🙂")
        return
    if len(question) > ASK_QUESTION_MAX_LEN:
        await message.answer(f"Вопрос длинноват — сократи до {ASK_QUESTION_MAX_LEN} символов, пожалуйста.")
        return
    user = await db.get_user(user_id)
    if is_rude(question):
        await state.clear()
        await message.answer(rude_reply())
        return
    if user_id in _generating:
        await message.answer("⏳ Дождись, пожалуйста, пока закончится текущая генерация 🔮")
        return
    # Премиум — безлимит; бесплатным списываем из дневного лимита.
    if not db.is_premium(user):
        allowed = await db.yesno_try_consume(user_id, YESNO_FREE_LIMIT)
        if not allowed:
            await state.clear()
            await message.answer(
                f"🎱 На сегодня {YESNO_FREE_LIMIT} вопроса «Да/Нет» уже задано.\n\n"
                "💎 С премиумом можно спрашивать без ограничений — и днём, и ночью 🌸",
                reply_markup=_yesno_again_menu()
            )
            await _show_premium(message, user)
            return
    await state.clear()
    _generating.add(user_id)
    wait_msg = await message.answer("🎱 Смотрю в твои числа...")
    try:
        from generation import answer_yes_no
        name = db.default_name(user)
        answer = await answer_yes_no(name, user["birth_date"], question, male=db.is_male(user))
        await message.answer(answer, reply_markup=_yesno_again_menu())
    except Exception as e:
        logging.error(f"YesNo error: {e}", exc_info=True)
        if not db.is_premium(user):
            await db.refund_yesno_try(user_id)
        await message.answer("❌ Что-то пошло не так — попробуй ещё раз чуть позже 🙏", reply_markup=_yesno_again_menu())
    finally:
        _generating.discard(user_id)
        try:
            await wait_msg.delete()
        except Exception:
            pass

# ─── УТОЧНЯЮЩИЕ ВОПРОСЫ ПО КУПЛЕННОМУ РАЗБОРУ ────────────────────────────────
# FOLLOWUP_LIMIT бесплатных вопросов на каждый разбор — воронка в премиум:
# после лимита предлагаем безлимитный AI-чат "Спроси Еву" из подписки.
# Премиум сразу без ограничений (у них уже есть общий безлимитный чат).
def _followup_menu(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❓ Задать вопрос по разбору", callback_data=f"followup_{key}")],
    ])

@dp.callback_query(F.data.startswith("followup_"))
async def followup_cb(callback: CallbackQuery, state: FSMContext):
    key  = callback.data.replace("followup_", "")
    user = await db.get_user(callback.from_user.id)
    if key not in user.get("purchased", []):
        await callback.answer("Этот разбор больше не в списке купленных.", show_alert=True)
        return
    await state.update_data(followup_key=key)
    title = TITLES.get(key, "разбор")
    await callback.message.answer(
        f"❓ Что уточнить по разбору «{title}»? Спрашивай прямо 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="cancel_to_menu")]
        ])
    )
    await state.set_state(Form.waiting_followup)
    await callback.answer()

@dp.message(StateFilter(Form.waiting_followup))
async def handle_followup(message: Message, state: FSMContext):
    user_id  = message.from_user.id
    question = (message.text or "").strip()
    if len(question) < 3:
        await message.answer("Напиши вопрос текстом, хотя бы пару слов 🙂")
        return
    if len(question) > ASK_QUESTION_MAX_LEN:
        await message.answer(f"Вопрос слишком длинный — сократи до {ASK_QUESTION_MAX_LEN} символов, пожалуйста.")
        return

    data = await state.get_data()
    key  = data.get("followup_key")
    user = await db.get_user(user_id)
    if not key or key not in user.get("purchased", []):
        await state.clear()
        await message.answer("Разбор не найден — выбери его заново в «Мои разборы» и нажми «Задать вопрос».", reply_markup=_MENU_BACK_MARKUP)
        return

    if is_rude(question):
        await state.clear()
        await message.answer(rude_reply())
        return

    if user_id in _generating:
        await message.answer("⏳ Дождись, пожалуйста, пока закончится текущая генерация 🔮")
        return

    premium = db.is_premium(user)
    if not premium:
        allowed = await db.followup_try_consume(user_id, key, FOLLOWUP_LIMIT)
        if not allowed:
            await state.clear()
            await message.answer(
                f"💎 Бесплатные вопросы по этому разбору закончились ({FOLLOWUP_LIMIT} на разбор).\n\n"
                "В Ева Премиум — безлимитный AI-помощник «Спроси Еву» по любому разбору и в любое время.",
                reply_markup=premium_subscribe_menu(await _create_premium_invoice())
            )
            return

    _generating.add(user_id)
    wait_msg = await message.answer("⏳ Ева думает над ответом...")
    try:
        name     = db.default_name(user)
        title    = TITLES.get(key, "разбор")
        context  = build_numerology_context(name, user["birth_date"])
        saved    = await db.get_reading_text(user_id, key)
        reading_block = (
            f"\n\nВот текст самого разбора, который ты ей уже прислала — отвечай ИМЕННО по нему, "
            f"не противоречь и не повторяй общие фразы, если в разборе уже есть конкретика:\n«{saved['text']}»"
            if saved and saved.get("text") else ""
        )
        prompt  = (
            f"Вот нумерологические данные {name}:\n{context}"
            f"{reading_block}\n\n"
            f"Она только что получила от тебя платный разбор «{title}» и теперь уточняет: «{question}»\n\n"
            "Ответь как Ева — тепло, конкретно, опираясь на её числа и на то, что уже написано в разборе. "
            "Это часть живого диалога: не используй emoji-заголовки, не структурируй ответ на "
            "блоки, пиши связным текстом. 3-6 предложений, по делу, без воды."
        )
        async with premium_gen_semaphore(user):
            answer = await ask_ai(prompt)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❓ Спросить ещё по этому разбору", callback_data=f"followup_{key}")],
        ])
        await message.answer(answer, reply_markup=kb)
    except Exception as e:
        logging.error(f"Followup error: {e}", exc_info=True)
        await message.answer("❌ Что-то пошло не так — попробуй ещё раз чуть позже 🙏", reply_markup=_followup_menu(key))
    finally:
        _generating.discard(user_id)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await state.clear()

# ─── ОБРАТНАЯ СВЯЗЬ (приватно админу, без модерации/публикации) ──────────────
FEEDBACK_MAX_LEN = 800

_FEEDBACK_PROMPTS = {
    "idea": (
        "💡 Есть идея, чего не хватает, или что было бы круто добавить? "
        "Пиши — читаю каждое сообщение лично и часто беру в работу."
    ),
    "bug": (
        "🐞 Что-то не работает или ведёт себя странно? Опиши, что произошло "
        "(и по возможности — в каком разборе или разделе) — разберусь."
    ),
}

def _feedback_choice_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💡 Предложить идею", callback_data="feedback_cat_idea"),
         InlineKeyboardButton(text="🐞 Сообщить об ошибке", callback_data="feedback_cat_bug")],
        [InlineKeyboardButton(text="🔮 Главное меню", callback_data="show_menu")],
    ])

@dp.callback_query(F.data == "feedback_start")
async def feedback_start_cb(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Что хочешь написать?", reply_markup=_feedback_choice_menu())
    await callback.answer()

@dp.message(Command("feedback"), StateFilter("*"))
async def feedback_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Что хочешь написать?", reply_markup=_feedback_choice_menu())

@dp.callback_query(F.data.startswith("feedback_cat_"))
async def feedback_category_cb(callback: CallbackQuery, state: FSMContext):
    category = callback.data.replace("feedback_cat_", "")
    await state.update_data(feedback_category=category)
    await callback.message.answer(
        _FEEDBACK_PROMPTS.get(category, _FEEDBACK_PROMPTS["idea"]),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="cancel_to_menu")]
        ])
    )
    await state.set_state(Form.waiting_feedback)
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_reply_"))
async def admin_reply_start_cb(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    target_id = int(callback.data.replace("admin_reply_", ""))
    await state.update_data(admin_reply_to=target_id)
    await state.set_state(Form.waiting_admin_reply)
    await callback.message.answer(f"✍️ Введи ответ для пользователя {target_id} (или /cancel):")
    await callback.answer()

@dp.message(StateFilter(Form.waiting_admin_reply))
async def admin_reply_send(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    target_id = data.get("admin_reply_to")
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши текстом, пожалуйста.")
        return
    await state.clear()
    try:
        await bot.send_message(target_id, f"💬 Ответ от Евы:\n\n{text}")
        await message.answer("✅ Отправлено.")
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}")

@dp.callback_query(F.data == "cancel_to_menu")
async def cancel_to_menu_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await db.get_user(callback.from_user.id)
    await callback.message.answer("❌ Отменено.", reply_markup=main_menu_for(callback.from_user.id, user))
    await callback.answer()

@dp.message(StateFilter(Form.waiting_feedback))
async def handle_feedback(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if len(text) < 3:
        await message.answer("Напиши текстом, хотя бы пару слов 🙂")
        return
    if len(text) > FEEDBACK_MAX_LEN:
        await message.answer(f"Слишком длинно — сократи до {FEEDBACK_MAX_LEN} символов, пожалуйста.")
        return
    data = await state.get_data()
    category = data.get("feedback_category", "idea")
    await state.clear()
    user_id = message.from_user.id
    await db.add_feedback(user_id, text, category)
    user = await db.get_user(user_id)
    name = user.get("first_name") or "Аноним"
    label = "💡 Идея" if category == "idea" else "🐞 Баг"
    try:
        await bot.send_message(
            ADMIN_ID,
            f"{label} от {name} (id {user_id})\n\n{text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬 Ответить", callback_data=f"admin_reply_{user_id}")]
            ])
        )
    except Exception as e:
        logging.warning(f"Feedback notify error: {e}")
    await message.answer("✅ Спасибо! Обязательно учту это 🌸", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔮 Главное меню", callback_data="show_menu")]
    ]))

@dp.callback_query(F.data == "admin_feedback")
async def admin_feedback_cb(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    items = await db.list_feedback(10)
    if not items:
        await callback.message.answer("Пока пусто.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
        ]))
        await callback.answer()
        return
    lines = ["💡 Последняя обратная связь:\n"]
    for it in items:
        name  = it["first_name"] or "Аноним"
        dt    = it["created_at"].strftime("%d.%m %H:%M")
        label = "🐞" if it.get("category") == "bug" else "💡"
        lines.append(f"{label} — {name} ({dt}): {it['text']}")
    await callback.message.answer(
        "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
        ])
    )
    await callback.answer()

# ─── МОЙ ПРОФИЛЬ ─────────────────────────────────────────────────────────────
# Единая точка входа во всё, что раньше было раскидано по главному меню и
# отдельным командам: рефералка, промокод, подарок, имя, уведомления.
async def _show_profile(target: Message, user: dict):
    name    = user.get("first_name") or "не указано"
    bdate   = user.get("birth_date") or "не указана"
    number  = user.get("destiny_number")
    balance = user.get("ref_balance", 0)
    premium = db.is_premium(user)
    premium_line = (
        f"💎 Премиум до {user['premium_until'].strftime('%d.%m.%Y')}"
        if premium else "💎 Премиум не активен"
    )
    notif_on = user.get("notifications", True)
    text = (
        f"👤 Твой профиль\n\n"
        f"Имя: {name}\n"
        f"Дата рождения: {bdate}"
        + (f" (число судьбы {number})" if number is not None else "") + "\n"
        f"⭐ Баланс: {balance}\n"
        f"{premium_line}\n"
        f"🔔 Уведомления: {'включены' if notif_on else 'отключены'}\n\n"
        f"✨ Кстати, во вкладке «Матрица» веб-кабинета (кнопка «🔮 Кабинет» "
        f"под полем ввода) есть ежедневный бонус звёзд — загляни, если ещё не "
        + ("пробовал." if db.is_male(user) else "пробовала.")
    )
    await target.answer(text, reply_markup=profile_menu(
        notif_on, len(user.get("purchased", [])), is_male=db.is_male(user)))

@dp.callback_query(F.data == "gender_toggle")
async def gender_toggle_cb(callback: CallbackQuery):
    """Переключатель обращения в профиле — для существующих пользователей,
    которым не задавали вопрос при онбординге."""
    user = await db.get_user(callback.from_user.id)
    new_gender = "f" if db.is_male(user) else "m"
    await db.set_gender(callback.from_user.id, new_gender)
    user["gender"] = new_gender
    await callback.answer(
        "Теперь обращаюсь в мужском роде 🙌" if new_gender == "m"
        else "Теперь обращаюсь в женском роде 🌸"
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=profile_menu(
            user.get("notifications", True), len(user.get("purchased", [])),
            is_male=(new_gender == "m")))
    except Exception:
        pass

@dp.message(Command("revoke_premium"), StateFilter("*"))
async def revoke_premium_cmd(message: Message, state: FSMContext):
    """Только для админа — снимает премиум с конкретного user_id.
    /revoke_premium 123456789. Нужно для чистки тестовых оплат (пока
    ЮKassa была в тестовом режиме, успешные тестовые оплаты выдавали
    настоящий премиум — бот не может отличить тестовую оплату от боевой)."""
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /revoke_premium USER_ID")
        return
    target_id = int(parts[1])
    await db.set_premium(target_id, None)
    await message.answer(f"✅ Премиум снят с {target_id}.")

@dp.message(Command("discount"), StateFilter("*"))
async def discount_cmd(message: Message, state: FSMContext):
    """Админ: включить/выключить акцию-скидку на ВСЕ разборы.
    /discount 30 — скидка 30%, /discount 0 — убрать акцию.
    Применяется сразу и к витрине, и к оплате; переживает рестарт (БД)."""
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer(
            f"Текущая скидка: {config.get_discount()}%\n\n"
            "Использование:\n/discount 30 — включить −30% на все разборы\n"
            "/discount 0 — убрать акцию"
        )
        return
    pct = max(0, min(90, int(parts[1])))
    config.set_discount(pct)
    await db.set_setting("discount_percent", str(pct))
    if pct:
        await message.answer(
            f"🔥 Акция включена: −{pct}% на все разборы.\n"
            "Цены пересчитаны везде — в меню, апселлах и оплате."
        )
    else:
        await message.answer("✅ Акция выключена — цены вернулись к обычным.")

def _discount_menu() -> InlineKeyboardMarkup:
    cur = config.get_discount()
    def _lbl(p: int) -> str:
        base = f"−{p}%" if p else "Выключить"
        return ("✅ " + base) if p == cur else base
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_lbl(10), callback_data="admin_disc_10"),
         InlineKeyboardButton(text=_lbl(20), callback_data="admin_disc_20"),
         InlineKeyboardButton(text=_lbl(30), callback_data="admin_disc_30")],
        [InlineKeyboardButton(text=_lbl(40), callback_data="admin_disc_40"),
         InlineKeyboardButton(text=_lbl(50), callback_data="admin_disc_50"),
         InlineKeyboardButton(text=_lbl(0),  callback_data="admin_disc_0")],
        [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin_panel")],
    ])

@dp.callback_query(F.data == "admin_discount")
async def admin_discount_cb(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    cur = config.get_discount()
    status = f"🔥 Сейчас активна акция −{cur}%" if cur else "Акция выключена (обычные цены)"
    await callback.message.answer(
        f"🔥 Акция / скидка на все разборы\n\n{status}\n\n"
        "Выбери размер скидки — применится сразу к меню, апселлам и оплате. "
        "Тонкую настройку можно задать командой /discount 35.",
        reply_markup=_discount_menu()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_disc_"))
async def admin_disc_set_cb(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    pct = max(0, min(90, int(callback.data.rsplit("_", 1)[1])))
    config.set_discount(pct)
    await db.set_setting("discount_percent", str(pct))
    if pct:
        await callback.answer(f"🔥 Акция −{pct}% включена", show_alert=False)
    else:
        await callback.answer("✅ Акция выключена", show_alert=False)
    try:
        await callback.message.edit_reply_markup(reply_markup=_discount_menu())
    except Exception:
        pass

@dp.message(Command("profile"), StateFilter("*"))
async def profile_cmd(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    await _show_profile(message, user)

@dp.callback_query(F.data == "my_profile")
async def my_profile_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await db.get_user(callback.from_user.id)
    await _show_profile(callback.message, user)
    await callback.answer()

async def _show_achievements(target: Message, user_id: int):
    from achievements import compute_achievements, achievements_summary
    user  = await db.get_user(user_id)
    stats = await db.get_referral_stats(user_id)
    digest = await db.get_reading_text(user_id, "profile_digest")
    purchased_paid = [k for k in user.get("purchased", []) if k in PAID_RAZBORY]
    items = compute_achievements(
        purchased_count=len(purchased_paid),
        ref_count=stats["count"],
        is_premium=db.is_premium(user),
        has_birthdate=bool(user.get("birth_date")),
        has_spun=user.get("last_spin_date") is not None,
        digest_ready=bool(digest),
    )
    await target.answer(achievements_summary(items), reply_markup=_MENU_BACK_MARKUP)

@dp.message(Command("achievements"), StateFilter("*"))
async def achievements_cmd(message: Message, state: FSMContext):
    await state.clear()
    await _show_achievements(message, message.from_user.id)

@dp.callback_query(F.data == "my_achievements")
async def my_achievements_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_achievements(callback.message, callback.from_user.id)
    await callback.answer()

# ─── РЕФЕРАЛЬНАЯ СИСТЕМА ─────────────────────────────────────────────────────
@dp.callback_query(F.data == "ref_promo")
async def ref_promo_callback(callback: CallbackQuery):
    user_id  = callback.from_user.id
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
    stats    = await db.get_referral_stats(user_id)
    user     = await db.get_user(user_id)
    balance  = user.get("ref_balance", 0)
    text = (
        f"👥 Реферальная программа\n\n"
        f"Приглашай подруг — получай {REF_BONUS_PERCENT}% звёздами с каждой их покупки!\n\n"
        f"🔗 Твоя ссылка:\n{ref_link}\n\n"
        f"📊 Статистика:\n"
        f"• Приглашено: {stats['count']} чел.\n"
        f"• Заработано всего: {stats['earned']} ⭐\n"
        f"• Баланс сейчас: {balance} ⭐\n\n"
        f"💡 {REF_BONUS_PERCENT}% от суммы каждой покупки подруги — автоматически на твой баланс ⭐\n"
        f"Использовать баланс: /balance"
    )
    await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔮 Главное меню", callback_data="show_menu")]
    ]))
    await callback.answer()

@dp.message(Command("ref"), StateFilter("*"))
async def ref_cmd(message: Message, state: FSMContext):
    await state.clear()
    user_id  = message.from_user.id
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
    stats    = await db.get_referral_stats(user_id)
    user     = await db.get_user(user_id)
    balance  = user.get("ref_balance", 0)

    text = (
        f"👥 Реферальная программа\n\n"
        f"Приглашай подруг — получай {REF_BONUS_PERCENT}% звёздами с каждой их покупки!\n\n"
        f"🔗 Твоя ссылка:\n{ref_link}\n\n"
        f"📊 Статистика:\n"
        f"• Приглашено: {stats['count']} чел.\n"
        f"• Заработано всего: {stats['earned']} ⭐\n"
        f"• Баланс сейчас: {balance} ⭐\n\n"
        f"💡 Как это работает:\n"
        f"Подруга переходит по твоей ссылке и покупает любой разбор — "
        f"ты автоматически получаешь {REF_BONUS_PERCENT}% от суммы её покупки "
        f"на свой баланс виртуальных звёзд.\n\n"
        f"Баланс можно использовать для оплаты своих разборов — команда /balance"
    )
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔮 Главное меню", callback_data="show_menu")]
    ]))

@dp.message(Command("balance"), StateFilter("*"))
async def balance_cmd(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    user    = await db.get_user(user_id)
    balance = user.get("ref_balance", 0)
    stats   = await db.get_referral_stats(user_id)

    lines = [
        f"⭐ Твой баланс: {balance} звёзд",
        f"",
        f"👥 Приглашено подруг: {stats['count']}",
        f"💰 Заработано за всё время: {stats['earned']} ⭐",
    ]

    if stats['bonuses']:
        lines.append("\n📋 Последние начисления:")
        for b in stats['bonuses']:
            name     = b['first_name'] or "Подруга"
            title    = TITLES.get(b['razbor_key'], b['razbor_key'] or "разбор")
            dt       = b['created_at'].strftime("%d.%m %H:%M")
            lines.append(f"  +{b['amount']} ⭐ от {name} за «{title}» — {dt}")

    if balance > 0:
        lines.append(f"\n💡 Используй звёзды при покупке разбора — выбери разбор и нажми «Оплатить балансом»")
    else:
        bot_info = await bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
        lines.append(f"\n🔗 Пригласи подругу и начни зарабатывать:\n{ref_link}")

    await message.answer("\n".join(lines), reply_markup=_MENU_BACK_MARKUP)

# ─── РАССЫЛКИ ────────────────────────────────────────────────────────────────
def _day_message(name: str, destiny_number: int, day_number: int) -> str:
    day_info = DAY_ENERGY.get(day_number, DAY_ENERGY[9])
    day_energy, day_advice = day_info

    destiny_notes = {
        1:  "Для твоей единицы этот день особенно важен — не упусти момент для инициативы.",
        2:  "Твоя двойка сегодня чувствует людей особенно тонко — используй это.",
        3:  "Твоя тройка расцветает в такие дни — выражай себя смело.",
        4:  "Твоя четвёрка найдёт опору в энергии этого дня — строй и создавай.",
        5:  "Твоя пятёрка обожает такие дни — следуй за интересом.",
        6:  "Твоя шестёрка сегодня особенно нужна близким — подари им своё внимание.",
        7:  "Твоя семёрка углубляется в этот день — дай себе время побыть наедине с мыслями.",
        8:  "Твоя восьмёрка усиливается сегодня — смело берись за важные дела.",
        9:  "Твоя девятка видит дальше других — доверяй этому взгляду сегодня.",
        11: "Твоё мастер-число 11 резонирует с этим днём — обращай внимание на знаки.",
        22: "Твоё мастер-число 22 даёт тебе сегодня особую практическую силу.",
        33: "Твоё мастер-число 33 сегодня светит ярче — позволь себе быть тем светом.",
    }
    personal = destiny_notes.get(destiny_number, destiny_notes[9])

    return (
        f"🌅 Доброе утро, {name}!\n\n"
        f"Сегодня {day_number}-й день по нумерологии — {day_energy}.\n\n"
        f"✨ {day_advice.capitalize()}.\n\n"
        f"{personal}\n\n"
        f"🔮 /menu"
    )

_PMONTH_ENERGY = {
    1: "месяц новых начинаний — самое время закладывать то, что хочешь вырастить",
    2: "месяц отношений и союзов — важные разговоры и партнёрства идут легче",
    3: "месяц творчества и общения — твоя энергия притягивает людей и идеи",
    4: "месяц труда и порядка — то что построишь сейчас, будет стоять долго",
    5: "месяц перемен и движения — не бойся сказать да новому",
    6: "месяц дома, любви и заботы — вкладывайся в близких и в себя",
    7: "месяц паузы и анализа — замедлись, ответы приходят в тишине",
    8: "месяц денег и результатов — время собирать плоды и принимать решения",
    9: "месяц завершения — отпусти лишнее, освободи место для нового цикла",
}

def _premium_day_message(name: str, destiny_number: int, day_number: int, birth_date: str) -> str:
    base = _day_message(name, destiny_number, day_number).replace("🌅", "💎", 1)
    base = base.replace("\n🔮 /menu", "")
    try:
        pm = calculate_personal_month(birth_date)
        pm_line = f"\n🌙 Твой личный месяц — {pm}: {_PMONTH_ENERGY.get(pm, '')}.\n"
    except Exception:
        pm_line = ""
    return base + pm_line + "\n🔮 Все разборы открыты — /menu"

def _power_day_message(name: str, destiny_number: int) -> str:
    """Точечное уведомление premium — шлём вместо обычного утреннего поста
    только когда личный день совпадает с числом судьбы ('день силы',
    статистически 2-4 раза в месяц). Редкость важнее частоты — так
    сообщение воспринимается как ценный сигнал, а не рассылка."""
    return (
        f"⚡ {name}, сегодня твой день силы!\n\n"
        f"Личное число дня совпадает с твоим числом судьбы — {destiny_number}. "
        "Такое бывает всего пару раз в месяц: энергия дня и твоя внутренняя "
        "энергия звучат в унисон, поэтому всё, что ты начнёшь или решишь "
        "сегодня, пойдёт легче и с меньшим сопротивлением.\n\n"
        "Используй этот день для того, что давно ждёт своего часа — важного разговора, "
        "решения, первого шага. Момент редкий, не трать его на мелочи 🌸\n\n"
        "💎 Все разборы открыты — /menu"
    )

async def send_daily_horoscope():
    """UTC 8:00 = Москва 11:00 — утренняя рассылка с нумерологией дня."""
    while True:
        now    = utc_now()
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            today      = date.today()
            day_number = calculate_day_number(today)
            rows = await db.db_pool.fetch(
                'SELECT user_id, first_name, destiny_number, birth_date, premium_until, gender FROM users '
                'WHERE birth_date IS NOT NULL AND destiny_number IS NOT NULL '
                'AND notifications = TRUE'
            )
            now_naive = utc_now()
            for row in rows:
                try:
                    name    = row['first_name'] or ("дорогой" if (row['gender'] or 'f') == 'm' else "дорогая")
                    number  = row['destiny_number']
                    premium = row['premium_until'] is not None and row['premium_until'] > now_naive
                    if premium and calculate_personal_day(row['birth_date'], today) == number:
                        text = _power_day_message(name, number)
                    elif premium:
                        text = _premium_day_message(name, number, day_number, row['birth_date'])
                    else:
                        text = _day_message(name, number, day_number)
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
                f"Число дня по нумерологии: {day_num}.\n\n"
                f"Строго соблюдай эту структуру из трёх блоков, каждый с emoji-заголовком:\n"
                f"🌟 Энергия дня — что несёт число {day_num} сегодня, какая атмосфера (2-3 предложения)\n"
                f"💡 Что сделать сегодня — 3 конкретных практических совета под эту энергию\n"
                f"✨ Настрой дня — одна тёплая мотивирующая фраза на день\n\n"
                f"Обращайся на ТЫ, к женщине, в женском роде — тёплый живой тон, как подруга-нумеролог. "
                f"Ровно эти три блока, не больше и не меньше. Каждый блок с новой строки. "
                f"150-200 слов. Только кириллица, эмодзи только перед заголовками блоков."
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

async def send_monthly_days_post():
    """1-го числа каждого месяца, UTC 8:00 = Москва 11:00 — пост «числа месяца»:
    сильные/осторожные дни + денежный день и день отношений. Считается
    детерминированно по числу дня (calculate_day_number), без ИИ — стабильно и
    бесплатно. Люди сохраняют такой пост и возвращаются к нему весь месяц."""
    import calendar
    _MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
                  "августа", "сентября", "октября", "ноября", "декабря"]
    # Числа дня по энергии: сильные — для стартов/действий, осторожные — переждать.
    STRONG = {1, 3, 5, 6, 8}   # начало, творчество, движение, гармония, результат
    CAREFUL = {4, 7, 9}        # труд/рутина, пауза, завершение — не для новых дел
    while True:
        now    = utc_now()
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        # ближайшее 1-е число месяца в 08:00 UTC
        if now.day == 1 and now < target:
            pass
        else:
            y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
            target = target.replace(year=y, month=m, day=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            y, m = target.year, target.month
            ndays = calendar.monthrange(y, m)[1]
            strong, careful, money_day, love_day = [], [], None, None
            for d in range(1, ndays + 1):
                dn = calculate_day_number(date(y, m, d))
                if dn in STRONG:
                    strong.append(d)
                elif dn in CAREFUL:
                    careful.append(d)
                if dn == 8 and money_day is None:
                    money_day = d
                if dn == 6 and love_day is None:
                    love_day = d
            mn = _MONTHS_RU[m - 1]
            text = (
                f"📅 ЧИСЛА {mn.upper()} — когда действовать, а когда переждать\n\n"
                f"🟢 Сильные дни (начинай важное, проси, действуй):\n"
                f"{', '.join(map(str, strong[:8]))}\n\n"
                f"🔴 Осторожные дни (не начинай нового, отдыхай):\n"
                f"{', '.join(map(str, careful[:6]))}\n\n"
                + (f"💰 Денежный день месяца: {money_day} {mn} — проси повышение, запускай продажи\n" if money_day else "")
                + (f"❤️ День для отношений: {love_day} {mn} — важные разговоры пройдут мягко\n" if love_day else "")
                + f"\n🔖 Сохрани, чтобы не забыть\n\n"
                f"✨ Твой персональный календарь дней — в боте @nnumerology_bot"
            )
            await bot.send_message(CHANNEL, text)
        except Exception as e:
            logging.error(f"Monthly days post error: {e}")
        await asyncio.sleep(60)

async def send_weekly_poll():
    """Пятница, UTC 9:00 = Москва 12:00 — опрос в канал: и вовлечение (реакции
    поднимают охват), и исследование — что аудитории интереснее разбирать."""
    while True:
        now    = utc_now()
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        # weekday(): понедельник=0 … пятница=4
        days_ahead = (4 - now.weekday()) % 7
        target += timedelta(days=days_ahead)
        if target <= now:
            target += timedelta(days=7)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await bot.send_poll(
                CHANNEL,
                question="🔮 Какую тему разобрать на следующей неделе?",
                options=[
                    "💑 Любовь и отношения",
                    "💰 Деньги и карьера",
                    "🔮 Характер и предназначение",
                    "🌙 Здоровье и энергия",
                ],
                is_anonymous=True,
            )
        except Exception as e:
            logging.error(f"Weekly poll error: {e}")
        await asyncio.sleep(60)

# ─── WEB ─────────────────────────────────────────────────────────────────────
async def healthcheck(request):
    return web.Response(text="OK")

async def run_web():
    app    = web.Application()
    app.router.add_get("/", healthcheck)
    from webapp import setup_webapp_routes
    setup_webapp_routes(app, bot)
    port   = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()

# ─── МЕНЮ КОМАНД (синяя кнопка слева) ────────────────────────────────────────
async def setup_bot_commands():
    """Команды в меню Telegram. Обычным пользователям — базовый набор,
    админу дополнительно /admin через персональный scope."""
    user_commands = [
        BotCommand(command="menu",          description="🔮 Меню разборов"),
        BotCommand(command="premium",       description="💎 Ева Премиум — все разборы"),
        BotCommand(command="ask",           description="💬 Спросить Еву (премиум)"),
        BotCommand(command="profile",       description="👤 Мой профиль"),
        BotCommand(command="feedback",      description="💡 Идея или баг"),
    ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
    try:
        admin_commands = user_commands + [
            BotCommand(command="admin", description="⚙️ Админ-панель"),
        ]
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    except Exception as e:
        logging.warning(f"Не удалось установить команды админа: {e}")

    # Кнопка меню (слева от поля ввода) открывает Mini App — личный кабинет
    # с матрицей чисел и каталогом. WEBAPP_URL обязателен для этой кнопки:
    # Telegram требует HTTPS, поэтому локально/без домена кнопка просто не
    # ставится, остальной функционал бота это не затрагивает.
    webapp_url = os.getenv("WEBAPP_URL")
    if webapp_url:
        # Защита от опечатки в env-переменной: если туда случайно вписали
        # уже с /app на конце — не дублируем его повторно.
        base_url = webapp_url.rstrip("/")
        if base_url.endswith("/app"):
            base_url = base_url[:-len("/app")]
        full_url = f"{base_url}/app"
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="🔮 Кабинет", web_app=WebAppInfo(url=full_url))
            )
            logging.info(f"Кнопка Mini App установлена: {full_url}")
        except Exception as e:
            logging.warning(f"Не удалось установить кнопку Mini App: {e}")
    else:
        logging.warning("WEBAPP_URL не задан — кнопка Mini App не установлена")

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    await db.init_db(DATABASE_URL)
    # Восстанавливаем активную скидку из БД (переживает рестарты Railway).
    try:
        saved = await db.get_setting("discount_percent", "0")
        config.set_discount(int(saved or 0))
    except Exception as e:
        logging.warning(f"Не удалось загрузить скидку: {e}")
    await setup_bot_commands()
    asyncio.create_task(run_web())
    asyncio.create_task(send_daily_horoscope())
    asyncio.create_task(send_daily_channel_post())
    asyncio.create_task(send_monthly_days_post())
    asyncio.create_task(send_weekly_poll())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
