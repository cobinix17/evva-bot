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

from readings import PROMPTS

import db
from config import (
    TITLES, PRICES, UPSELLS, PAID_RAZBORY, FREE_ELIGIBLE, RAZBOR_DESCRIPTIONS,
    ADMIN_ID, REF_BONUS_PERCENT,
    PREMIUM_PRICE, PREMIUM_PERIOD, PREMIUM_DAILY_LIMIT, PREMIUM_MONTHLY_LIMIT,
    PREMIUM_PAYLOAD, PREMIUM_TITLE, ASK_DAILY_LIMIT, FOLLOWUP_LIMIT,
    PREMIUM_PRICE_INCREASE, PREMIUM_PRICE_INCREASE_DATE,
    YOOKASSA_PROVIDER_TOKEN, PREMIUM_PRICE_RUB,
)
from ai import ask_ai, is_rude, rude_reply
from pdf import generate_pdf
from numerology import (
    calculate_destiny, calculate_day_number, is_valid_date,
    build_numerology_context, calculate_personal_month, calculate_personal_day,
)
from keyboards import (
    check_menu, date_choice_menu, notifications_menu, main_menu,
    free_choose_menu, section_destiny_menu, section_money_menu,
    section_love_menu, section_health_menu, section_past_menu,
    my_readings_menu, upsell_menu, retry_menu, coupon_razboy_menu,
    notif_off_menu, admin_menu, balance_pay_menu,
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
            await update.message.answer("❌ Что-то пошло не так. Попробуй /menu или /cancel.")
        elif update.callback_query:
            await update.callback_query.answer("❌ Что-то пошло не так.", show_alert=True)
    except Exception:
        pass
    return True

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
    waiting_followup         = State()
    waiting_rename           = State()
    waiting_feedback         = State()
    waiting_admin_reply      = State()
    waiting_promo            = State()

# ─── ЗАМОК ГЕНЕРАЦИИ ─────────────────────────────────────────────────────────
# Один платный разбор за раз на пользователя. Защищает от параллельного
# запуска двух генераций (юзер во время 90-сек ожидания уходит в меню и
# запускает второй разбор) — это удваивает расход токенов и рассинхронизирует
# поле waiting. Храним в памяти процесса: бот однопроцессный (один polling),
# поэтому простого set достаточно — внешний стор (Redis) не нужен.
_generating: set[int] = set()

# Глобальный лимит одновременных генераций разборов. Защищает от всплеска
# (сотни человек нажали «купить» в одну секунду → сотни параллельных запросов
# к OpenRouter): лишние ждут своей очереди, а не бьют по rate-лимитам и бюджету.
_gen_semaphore = asyncio.Semaphore(8)

# Отдельная полоса для премиум-подписчиков — их генерации не встают в общую
# очередь за бесплатными пользователями, поэтому в часы пика премиум отвечает
# быстрее. Это один из бонусов подписки («приоритет генерации»).
_priority_semaphore = asyncio.Semaphore(4)

# Лимиты премиума (30 новых разборов в месяц, 5 в день) считаются в БД —
# см. db.premium_try_consume. Счётчики переживают перезапуск бота и период
# сбрасывается сам (новый день/месяц обнуляет соответствующий счётчик).
# Регенерация уже открытых разборов слот не тратит.

def _premium_gen_semaphore(user: dict):
    return _priority_semaphore if db.is_premium(user) else _gen_semaphore

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
    text = (message.text or "").strip()
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
    text = (message.text or "").strip()
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

async def _apply_promo(message: Message, code: str):
    code = code.strip().upper()
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
        "Если без числа — создам на 1 использование.\nДля отмены — /cancel"
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
    code     = parts[0].upper()
    max_uses = 1
    if len(parts) >= 2:
        try:
            max_uses = max(1, int(parts[1]))
        except ValueError:
            await message.answer("❌ Второй параметр должен быть числом. Пример: FRIEND20 10")
            return
    result = await db.create_coupon(code, max_uses)
    await state.clear()
    if result == 'ok':
        expires  = (utc_now() + timedelta(hours=48)).strftime("%d.%m.%Y %H:%M")
        uses_str = f"{max_uses} раз" if max_uses > 1 else "1 раз"
        await message.answer(
            f"✅ Купон создан!\n\nКод: {code}\nЛимит: {uses_str}\nДействует до: {expires}\n\n"
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
        'SELECT code, uses_count, max_uses, expires_at FROM coupons ORDER BY expires_at DESC LIMIT 20'
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
        lines.append(f"{status} {r['code']} — {r['uses_count']}/{r['max_uses']} исп. до {exp}")
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
                "price": PRICES.get(s, 49),
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

async def _start_date_flow(message: Message, state: FSMContext, user: dict, key: str, is_free: bool = False):
    """Общий переход к вводу даты(-ат) после того как разбор уже точно
    доступен пользователю (куплен, оплачен балансом, взят по купону/бесплатно).
    is_free переключает на free_-состояния, чтобы неудачная генерация не
    сжигала платный счёт за бесплатную попытку (см. _process_date/_process_two_dates)."""
    if key == "compat":
        await message.answer("💑 Введи две даты через запятую:\nНапример: 15.03.1995, 22.07.1998")
        await state.set_state(Form.waiting_free_second_date if is_free else Form.waiting_second_date)
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
        price   = PRICES.get(key, 49)
        title   = PAID_RAZBORY[key]
        balance = user.get("ref_balance", 0)
        if balance >= price:
            await callback.message.answer(
                f"У тебя {balance} ⭐ на бонусном балансе — хватает на «{title}» ({price} ⭐).\n\n"
                f"Как оплатить?",
                reply_markup=balance_pay_menu(key, price)
            )
        else:
            desc = RAZBOR_DESCRIPTIONS.get(key, title)
            await send_invoice(callback.message.chat.id, title, desc, key, price)
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
        price = PRICES.get(key, 49)
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
    price = PRICES.get(key, 49)
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

    price = PRICES.get(key, 49)
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
    amount  = sp.total_amount  # в Stars (XTR) это целое число

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
                f"💎 Добро пожаловать в Премиум, {user.get('first_name') or 'дорогая'}!\n\n"
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
            await message.answer("❌ Что-то пошло не так с оплатой — напиши в поддержку.")
            await state.clear()
            return
        code = secrets.token_hex(4)
        await db.create_gift(code, key, message.from_user.id)
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
        await message.answer("❌ Что-то пошло не так с оплатой — напиши в поддержку.")
        await state.clear()
        return

    if payload not in user["purchased"]:
        user["purchased"].append(payload)
    user["waiting"] = payload
    await db.save_user(message.from_user.id, user)

    # Начисляем реферальный бонус пригласившему
    referrer_id = user.get("referred_by")
    if referrer_id:
        bonus = max(1, round(amount * REF_BONUS_PERCENT / 100))
        try:
            await db.add_ref_bonus(referrer_id, message.from_user.id, bonus, payload)
            buyer_name = user.get("first_name") or "Подруга"
            title      = TITLES.get(payload, "разбор")
            await bot.send_message(
                referrer_id,
                f"🎉 +{bonus} ⭐ на твой баланс!\n\n"
                f"{buyer_name} купила «{title}» по твоей реферальной ссылке.\n"
                f"Проверить баланс: /balance"
            )
        except Exception as e:
            logging.warning(f"Ref bonus error for referrer {referrer_id}: {e}")

    if payload == "compat":
        await message.answer("✅ Оплата прошла!")
    await _start_date_flow(message, state, user, payload)

# ─── ОБРАБОТКА ДАТ ───────────────────────────────────────────────────────────
async def _process_date(message: Message, user_id: int, user: dict, date_str: str,
                        state: FSMContext, is_free: bool = False):
    number  = calculate_destiny(date_str)
    waiting = user.get("waiting")
    name    = user.get("first_name") or "дорогая"
    if not waiting:
        await message.answer("Выбери разбор из меню 👇", reply_markup=main_menu_for(message.from_user.id, user))
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
        title = TITLES.get(waiting, "🔮 Разбор")
        cached = await db.get_reading_text(user_id, waiting)
        if cached and cached.get("date_str") == date_str:
            await stop_intermediate()
            answer = cached["text"]
            await send_long(
                message.chat.id,
                f"{title}\n\n{answer}\n\n"
                f"ℹ️ Этот разбор на эту дату мы уже делали — показываю тот же текст, "
                f"чтобы не было противоречий."
            )
        else:
            context = build_numerology_context(name, date_str)
            prompt  = build_prompt(waiting, name=name, context=context, date=date_str)
            async with _premium_gen_semaphore(user):
                answer = await ask_ai(prompt)
            await stop_intermediate()
            await send_long(message.chat.id, f"{title}\n\n{answer}")
            await db.save_reading_text(user_id, waiting, title, answer, date_str)

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
    finally:
        _generating.discard(user_id)

def _parse_two_dates(text: str) -> list[str] | None:
    if "," not in text:
        return None
    parts = [p.strip() for p in text.split(",")]
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
    await _process_two_dates(message, message.from_user.id, user, parts, state, is_free=False)

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
    await _process_two_dates(message, message.from_user.id, user, parts, state, is_free=True)

async def _process_two_dates(message: Message, user_id: int, user: dict, parts: list[str],
                              state: FSMContext, is_free: bool = False):
    name    = user.get("first_name") or "дорогая"

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
        n1 = calculate_destiny(parts[0])
        n2 = calculate_destiny(parts[1])
        compat_date_str = f"{parts[0]},{parts[1]}"
        cached = await db.get_reading_text(user_id, "compat")
        if cached and cached.get("date_str") == compat_date_str:
            await stop_intermediate()
            answer = cached["text"]
            await send_long(
                message.chat.id,
                f"💑 Совместимость\n\n{answer}\n\n"
                f"ℹ️ Разбор совместимости для этих дат мы уже делали — показываю тот же текст, "
                f"чтобы не было противоречий."
            )
        else:
            context = build_numerology_context(name, parts[0])
            prompt  = build_prompt("compat", name=name, context=context, date1=parts[0], date2=parts[1], n2=n2)
            async with _premium_gen_semaphore(user):
                answer = await ask_ai(prompt)
            await stop_intermediate()
            await send_long(message.chat.id, f"💑 Совместимость\n\n{answer}")
            await db.save_reading_text(user_id, "compat", "💑 Совместимость", answer, compat_date_str)

        try:
            pdf_bytes = await _generate_pdf_async(
                "💑 Совместимость", answer, user_name=name, destiny_number=n1,
                birth_date=parts[0], upsells=_build_upsells("compat", user),
                ref_bonus_percent=REF_BONUS_PERCENT,
            )
            pdf_file  = BufferedInputFile(pdf_bytes, filename="Совместимость.pdf")
            await bot.send_document(message.chat.id, pdf_file, caption="📄 Разбор в PDF — сохрани себе!")
        except Exception as pdf_err:
            logging.warning(f"PDF compat error: {pdf_err}")

        await message.answer(
            "❓ Остались вопросы по этому разбору? Уточни у меня напрямую 👇",
            reply_markup=_followup_menu("compat")
        )

        kb = upsell_menu("compat", user)
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
    except Exception as e:
        await stop_intermediate()
        logging.error(f"Compat error: {e}", exc_info=True)
        retry_text = (
            "❌ Что-то пошло не так. Твоя бесплатная попытка не сгорела — "
            "нажми кнопку и попробуй снова 👇" if is_free else
            "❌ Что-то пошло не так. Твоя покупка сохранена — нажми кнопку и попробуй снова 👇"
        )
        await message.answer(retry_text, reply_markup=retry_menu("compat", is_free=is_free))
        await state.clear()
    finally:
        _generating.discard(user_id)

@dp.message(StateFilter(Form.waiting_date))
async def handle_date(message: Message, state: FSMContext):
    user = await db.get_user(message.from_user.id)
    text = (message.text or "").strip()
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
    f"Подписка за {PREMIUM_PRICE} ⭐ в месяц — и почти вся моя нумерология открыта:\n\n"
    f"✨ До {PREMIUM_MONTHLY_LIMIT} разборов в месяц без покупки поштучно\n"
    f"🔮 До {PREMIUM_DAILY_LIMIT} новых разборов в день, а уже открытые — сколько угодно раз\n"
    f"💬 «Спроси Еву» — до {ASK_DAILY_LIMIT} личных вопросов в день по твоим числам\n"
    "🌅 Твой личный прогноз каждое утро — по твоим числам, а не общий\n"
    "⚡ Приоритетная генерация — без очереди в часы пика\n"
    "🎁 Новые разборы — сразу и бесплатно\n\n"
    f"⏳ С {PREMIUM_PRICE_INCREASE_DATE} цена для новых подписчиков станет "
    f"{PREMIUM_PRICE_INCREASE} ⭐/мес. Оформи сейчас за {PREMIUM_PRICE} ⭐ — и эта цена "
    "останется твоей навсегда, даже после повышения.\n\n"
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

async def _create_premium_invoice_rub() -> str:
    """Оплата рублями через ЮKassa, подключённую как Telegram-провайдер
    (BotFather → Payments), а не напрямую через API ЮKassa — платёж идёт
    тем же путём, что и Stars: тот же successful_payment хендлер, никакого
    отдельного вебхука не нужно, Telegram сам всё проверяет. В отличие от
    Stars-подписки, это РАЗОВЫЙ платёж на месяц, не автопродление —
    payload тот же PREMIUM_PAYLOAD, successful_payment уже умеет его принять
    (subscription_expiration_date у обычного платежа отсутствует — код
    подставляет +31 день, см. successful_payment)."""
    receipt = {
        "receipt": {
            "items": [{
                "description": "Ева Премиум — месяц подписки",
                "quantity": "1.00",
                "amount": {"value": f"{PREMIUM_PRICE_RUB}.00", "currency": "RUB"},
                "vat_code": 1,
            }]
        }
    }
    return await bot.create_invoice_link(
        title=PREMIUM_TITLE,
        description="Безлимитный доступ ко всем разборам, личный прогноз каждое утро и приоритетная генерация.",
        payload=PREMIUM_PAYLOAD,
        provider_token=YOOKASSA_PROVIDER_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(label="Ева Премиум — месяц", amount=PREMIUM_PRICE_RUB * 100)],
        need_email=True,
        send_email_to_provider=True,
        provider_data=json.dumps(receipt),
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
        await target.answer("❌ Не удалось открыть оплату — попробуй чуть позже 🙏")
        return
    rub_link = None
    if YOOKASSA_PROVIDER_TOKEN:
        try:
            rub_link = await _create_premium_invoice_rub()
        except Exception as e:
            logging.warning(f"Premium RUB invoice error: {e}")
    await target.answer(_PREMIUM_OFFER, reply_markup=premium_subscribe_menu(link, rub_link, PREMIUM_PRICE_RUB))

@dp.callback_query(F.data == "premium_info")
async def premium_info_cb(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    await _show_premium(callback.message, user)
    await callback.answer()

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
        "💬 Спроси меня о чём угодно — по твоим числам отвечу лично.\n"
        "Например: «что с деньгами в марте?» или «стоит ли сейчас менять работу?»",
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
        name    = user.get("first_name") or "дорогая"
        context = build_numerology_context(name, user["birth_date"])
        prompt  = (
            f"Вот нумерологические данные {name}:\n{context}\n\n"
            f"Она задаёт тебе личный вопрос в переписке: «{question}»\n\n"
            "Ответь как Ева — тепло, конкретно, опираясь на её числа. Это часть живого "
            "диалога, а не отдельный разбор: не используй emoji-заголовки, не структурируй "
            "ответ на блоки, пиши связным текстом. 3-6 предложений, по делу, без воды."
        )
        async with _premium_gen_semaphore(user):
            answer = await ask_ai(prompt)
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
        await message.answer("Разбор не найден — выбери его заново в «Мои разборы» и нажми «Задать вопрос».")
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
        name     = user.get("first_name") or "дорогая"
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
        async with _premium_gen_semaphore(user):
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
        f"✨ Кстати, в веб-кабинете (кнопка «🔮 Кабинет» внизу чата) есть "
        f"ежедневный бонус звёзд — загляни, если ещё не пробовала."
    )
    await target.answer(text, reply_markup=profile_menu(notif_on))

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

    await message.answer("\n".join(lines))

# ─── РАССЫЛКИ ────────────────────────────────────────────────────────────────
_DAY_ENERGY = {
    1: ("энергия начала и лидерства", "день для смелых решений и новых стартов — действуй первой"),
    2: ("энергия интуиции и партнёрства", "день для диалога и прислушивания к себе — доверяй ощущениям"),
    3: ("энергия творчества и общения", "день для самовыражения и радости — позволь себе быть яркой"),
    4: ("энергия порядка и созидания", "день для планирования и дел — всё что начнёшь сегодня будет устойчивым"),
    5: ("энергия перемен и свободы", "день для нового опыта — будь открыта к неожиданному"),
    6: ("энергия любви и гармонии", "день для близких и заботы о себе — это твоя главная задача сегодня"),
    7: ("энергия мудрости и глубины", "день для размышлений и тишины — ответы уже внутри тебя"),
    8: ("энергия силы и изобилия", "день для финансовых решений и амбиций — действуй уверенно"),
    9: ("энергия завершения и мудрости", "день для отпускания старого — освободи место для нового"),
    11: ("энергия вдохновения и интуиции", "особый день — твоё чутьё работает на максимуме"),
    22: ("энергия мастера-строителя", "день для масштабных шагов — делай то что останется надолго"),
    33: ("энергия высшей любви", "день для сострадания и помощи — твои слова сегодня целительны"),
}

def _day_message(name: str, destiny_number: int, day_number: int) -> str:
    day_info = _DAY_ENERGY.get(day_number, _DAY_ENERGY[9])
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
        11: "Твоё мастер-число 11 резонирует с этим днём — будь внимательна к знакам.",
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
        "Используй этот день для того, что откладывала — важного разговора, "
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
                'SELECT user_id, first_name, destiny_number, birth_date, premium_until FROM users '
                'WHERE birth_date IS NOT NULL AND destiny_number IS NOT NULL '
                'AND notifications = TRUE'
            )
            now_naive = utc_now()
            for row in rows:
                try:
                    name    = row['first_name'] or "дорогая"
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
    await setup_bot_commands()
    asyncio.create_task(run_web())
    asyncio.create_task(send_daily_horoscope())
    asyncio.create_task(send_daily_channel_post())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
