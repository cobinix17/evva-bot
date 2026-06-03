import os
import logging
import asyncio
import json
import httpx
import asyncpg
import random
from datetime import datetime, date, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not GROQ_API_KEY:
    raise EnvironmentError("BOT_TOKEN и GROQ_API_KEY должны быть установлены!")

CHANNEL = "@eva_numerologg"
REVIEWS_CHANNEL = "@eva_numerolog_otz"
ADMIN_ID = 5854618444

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

db_pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            free_used BOOLEAN DEFAULT FALSE,
            subscribed_channel BOOLEAN DEFAULT FALSE,
            birth_date TEXT,
            destiny_number INTEGER,
            purchased TEXT DEFAULT '[]',
            waiting TEXT,
            review_left BOOLEAN DEFAULT FALSE
        )
    ''')

async def get_user(user_id: int) -> dict:
    row = await db_pool.fetchrow('SELECT * FROM users WHERE user_id = $1', user_id)
    if not row:
        await db_pool.execute(
            'INSERT INTO users (user_id) VALUES ($1)', user_id
        )
        row = await db_pool.fetchrow('SELECT * FROM users WHERE user_id = $1', user_id)
    user = dict(row)
    user['purchased'] = json.loads(user['purchased'])
    if user_id == ADMIN_ID:
        user['free_used'] = True
        user['subscribed_channel'] = True
        all_razbory = ["compat", "when", "portrait", "unlucky", "matrix", "mission", "karma", "career", "money", "days", "ex", "cold", "toxic", "lonely", "breakup"]
        for r in all_razbory:
            if r not in user['purchased']:
                user['purchased'].append(r)
    return user

async def save_user(user_id: int, user: dict):
    purchased = json.dumps(user['purchased'])
    await db_pool.execute('''
        UPDATE users SET
            free_used = $1,
            subscribed_channel = $2,
            birth_date = $3,
            destiny_number = $4,
            purchased = $5,
            waiting = $6,
            review_left = $7
        WHERE user_id = $8
    ''',
        user['free_used'],
        user['subscribed_channel'],
        user['birth_date'],
        user['destiny_number'],
        purchased,
        user['waiting'],
        user['review_left'],
        user_id
    )

class Form(StatesGroup):
    waiting_date = State()
    waiting_second_date = State()
    waiting_review = State()

async def check_subscription(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except:
        return False

async def ask_ai(prompt: str) -> str:
    models = [
        "llama-3.3-70b-versatile",
        "gemma2-9b-it",
        "llama-3.1-8b-instant"
    ]
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    system_prompt = (
        "Ты — Ева, тёплый и мудрый нумеролог. Общаешься как близкая подруга которая глубоко разбирается в нумерологии. "
        "Пишешь ИСКЛЮЧИТЕЛЬНО на русском языке — никаких иностранных слов. "
        "Все слова только русские. Пишешь красиво, с эмодзи, атмосферно. Обращаешься на ты. "
        "Ответы длинные, подробные, эмоциональные — создающие ощущение что это написано именно про этого человека. "
        "Минимум 400 слов. Используй абзацы и структуру для удобного чтения. "
        "Заканчивай ответ полным предложением, никогда не обрывай на середине."
    )
    last_error = None
    for model in models:
        try:
            data = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 2000
            }
            async with httpx.AsyncClient() as client:
                response = await client.post(url, headers=headers, json=data, timeout=90)
                response.raise_for_status()
                result = response.json()
                return result["choices"][0]["message"]["content"]
        except Exception as e:
            last_error = e
            logging.warning(f"Model {model} failed: {e}, trying next...")
            continue
    raise last_error

def calculate_destiny(date_str: str) -> int:
    digits = [int(d) for d in date_str if d.isdigit()]
    total = sum(digits)
    while total > 9 and total not in (11, 22, 33):
        total = sum(int(d) for d in str(total))
    return total

def is_valid_date(date_str: str) -> bool:
    try:
        datetime.strptime(date_str, "%d.%m.%Y")
        return True
    except ValueError:
        return False

def main_menu(user=None):
    buttons = []
    if user and not user.get("free_used"):
        buttons.append([InlineKeyboardButton(text="💫 Мой бесплатный разбор", callback_data="free")])
    buttons.extend([
        [InlineKeyboardButton(text="💑 Совместимость двух людей — 49 ⭐", callback_data="buy_compat")],
        [InlineKeyboardButton(text="💘 Когда встретишь того самого — 49 ⭐", callback_data="buy_when")],
        [InlineKeyboardButton(text="💍 Портрет идеального партнёра — 49 ⭐", callback_data="buy_portrait")],
        [InlineKeyboardButton(text="💔 Почему не везёт в любви — 49 ⭐", callback_data="buy_unlucky")],
        [InlineKeyboardButton(text="🔮 Матрица судьбы — 49 ⭐", callback_data="buy_matrix")],
        [InlineKeyboardButton(text="🌟 Предназначение и миссия — 49 ⭐", callback_data="buy_mission")],
        [InlineKeyboardButton(text="🔴 Кармический долг — 49 ⭐", callback_data="buy_karma")],
        [InlineKeyboardButton(text="💼 Карьерный путь — 49 ⭐", callback_data="buy_career")],
        [InlineKeyboardButton(text="💰 Денежный код — 49 ⭐", callback_data="buy_money")],
        [InlineKeyboardButton(text="🌙 Сильные и слабые дни месяца — 49 ⭐", callback_data="buy_days")],
        [InlineKeyboardButton(text="💔 Вернётся ли бывший — 49 ⭐", callback_data="buy_ex")],
        [InlineKeyboardButton(text="❄️ Почему он охладел — 49 ⭐", callback_data="buy_cold")],
        [InlineKeyboardButton(text="☠️ Токсичная или кармическая связь — 49 ⭐", callback_data="buy_toxic")],
        [InlineKeyboardButton(text="😔 Почему ты одинока — 49 ⭐", callback_data="buy_lonely")],
        [InlineKeyboardButton(text="💔 Разбор после расставания — 49 ⭐", callback_data="buy_breakup")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def check_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я подписалась!", callback_data="check_sub")],
    ])

def review_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😍 Оставить отзыв", callback_data="leave_review")],
        [InlineKeyboardButton(text="🔮 Другие разборы", callback_data="show_menu")]
    ])

@dp.message(Command("start"), StateFilter("*"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    if user["subscribed_channel"]:
        is_subscribed = await check_subscription(message.from_user.id)
        if not is_subscribed:
            user["subscribed_channel"] = False
            await save_user(message.from_user.id, user)
    if not user["subscribed_channel"]:
        await message.answer(
            "🔮 Привет! Я Ева — твой личный нумеролог.\n\n"
            "Числа хранят тайны твоей судьбы, любви и предназначения. "
            "Я помогу тебе раскрыть их.\n\n"
            f"✨ Подпишись на наш канал {CHANNEL} и получи бесплатный разбор числа судьбы!\n\n"
            "После подписки нажми кнопку ниже 👇",
            reply_markup=check_menu()
        )
    else:
        await message.answer(
            "🔮 Привет! Я Ева — твой личный нумеролог.\n\nВыбери свой разбор 👇",
            reply_markup=main_menu(user)
        )

@dp.message(Command("menu"), StateFilter("*"))
async def menu_cmd(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    await message.answer("🔮 Выбери разбор:", reply_markup=main_menu(user))

@dp.message(Command("admin"), StateFilter("*"))
async def admin_panel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    total = await db_pool.fetchval('SELECT COUNT(*) FROM users')
    free_used = await db_pool.fetchval('SELECT COUNT(*) FROM users WHERE free_used = TRUE')
    reviews = await db_pool.fetchval('SELECT COUNT(*) FROM users WHERE review_left = TRUE')
    rows = await db_pool.fetch('SELECT purchased FROM users')
    total_purchases = 0
    razbory_count = {}
    bought = 0
    for row in rows:
        purchased = json.loads(row['purchased'])
        if purchased:
            bought += 1
            total_purchases += len(purchased)
            for r in purchased:
                razbory_count[r] = razbory_count.get(r, 0) + 1
    top = sorted(razbory_count.items(), key=lambda x: x[1], reverse=True)
    top_text = "\n".join([f"  {k}: {v}" for k, v in top[:5]]) if top else "  нет покупок"
    await message.answer(
        f"📊 Статистика бота Ева\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"💫 Получили бесплатный разбор: {free_used}\n"
        f"💳 Купили хотя бы один разбор: {bought}\n"
        f"🛒 Всего покупок: {total_purchases}\n"
        f"⭐ Оставили отзыв: {reviews}\n\n"
        f"🏆 Топ разборов:\n{top_text}"
    )

@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    is_subscribed = await check_subscription(callback.from_user.id)
    if not is_subscribed:
        await callback.answer("❌ Ты ещё не подписалась!", show_alert=True)
        await callback.message.answer(
            f"Подпишись на канал {CHANNEL} и нажми кнопку снова 👇",
            reply_markup=check_menu()
        )
        return
    user["subscribed_channel"] = True
    await save_user(callback.from_user.id, user)
    if user["free_used"]:
        await callback.message.answer("✅ Ты уже подписана! Выбери разбор:", reply_markup=main_menu(user))
        await callback.answer()
        return
    await callback.message.answer(
        "✅ Отлично! Ты подписалась!\n\n"
        "Теперь введи свою дату рождения в формате ДД.ММ.ГГГГ\n"
        "Например: 15.03.1995"
    )
    user["waiting"] = "free"
    await save_user(callback.from_user.id, user)
    await state.set_state(Form.waiting_date)
    await callback.answer()

@dp.callback_query(F.data == "free")
async def free_handler(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    if not user["subscribed_channel"]:
        is_subscribed = await check_subscription(callback.from_user.id)
        if not is_subscribed:
            await callback.message.answer(
                f"💫 Чтобы получить бесплатный разбор — подпишись на {CHANNEL} 👇",
                reply_markup=check_menu()
            )
            await callback.answer()
            return
        user["subscribed_channel"] = True
        await save_user(callback.from_user.id, user)
    if user["free_used"]:
        await callback.message.answer(
            "💫 Бесплатный разбор ты уже получила.\n\nВыбери платный разбор 🔮",
            reply_markup=main_menu(user)
        )
        await callback.answer()
        return
    user["waiting"] = "free"
    await save_user(callback.from_user.id, user)
    await callback.message.answer(
        "✨ Введи свою дату рождения в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995"
    )
    await state.set_state(Form.waiting_date)
    await callback.answer()

RAZBORY = {
    "compat": ("💑 Совместимость двух людей", "Полный нумерологический разбор совместимости"),
    "when": ("💘 Когда встретишь того самого", "Нумерологический прогноз встречи с партнёром"),
    "portrait": ("💍 Портрет идеального партнёра", "Какой он будет — по твоим числам"),
    "unlucky": ("💔 Почему не везёт в любви", "Нумерологический анализ причин неудач в любви"),
    "matrix": ("🔮 Матрица судьбы", "Полный разбор матрицы судьбы по дате рождения"),
    "mission": ("🌟 Предназначение и миссия", "Твоё истинное предназначение по числам"),
    "karma": ("🔴 Кармический долг", "Что мешает тебе в жизни и как это исправить"),
    "career": ("💼 Карьерный путь", "Твой идеальный карьерный путь по числам"),
    "money": ("💰 Денежный код", "Твой личный денежный код и как его активировать"),
    "days": ("🌙 Сильные и слабые дни месяца", "Твои личные сильные и слабые дни по числам"),
    "ex": ("💔 Вернётся ли бывший", "Нумерологический анализ ситуации с бывшим"),
    "cold": ("❄️ Почему он охладел", "Нумерологический разбор причин охлаждения"),
    "toxic": ("☠️ Токсичная или кармическая связь", "Анализ токсичности ваших отношений"),
    "lonely": ("😔 Почему ты одинока", "Нумерологический разбор причин одиночества"),
    "breakup": ("💔 Разбор после расставания", "Нумерологический анализ расставания и что дальше"),
}

async def send_invoice(chat_id, title, description, payload, amount):
    prices = [LabeledPrice(label=title, amount=amount)]
    await bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        currency="XTR",
        prices=prices
    )

@dp.callback_query(F.data.startswith("buy_"))
async def buy_handler(callback: CallbackQuery, state: FSMContext):
    key = callback.data.replace("buy_", "")
    user = await get_user(callback.from_user.id)
    if key in user["purchased"]:
        user["waiting"] = key
        await save_user(callback.from_user.id, user)
        if key == "compat":
            await callback.message.answer("💑 Введи две даты рождения через запятую:\nНапример: 15.03.1995, 22.07.1998")
            await state.set_state(Form.waiting_second_date)
        else:
            await callback.message.answer("📅 Введи свою дату рождения в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
            await state.set_state(Form.waiting_date)
        await callback.answer()
        return
    if key in RAZBORY:
        title, desc = RAZBORY[key]
        await send_invoice(callback.message.chat.id, title, desc, key, 49)
    await callback.answer()

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    payload = message.successful_payment.invoice_payload
    if payload not in user["purchased"]:
        user["purchased"].append(payload)
    user["waiting"] = payload
    await save_user(message.from_user.id, user)
    if payload == "compat":
        await message.answer("✅ Оплата прошла! Введи две даты рождения через запятую:\nНапример: 15.03.1995, 22.07.1998")
        await state.set_state(Form.waiting_second_date)
    else:
        await message.answer("✅ Оплата прошла! Введи свою дату рождения в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
        await state.set_state(Form.waiting_date)

@dp.message(Form.waiting_second_date)
async def handle_two_dates(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    text = message.text.strip()
    if "," not in text:
        await message.answer("❌ Введи две даты через запятую.\nНапример: 15.03.1995, 22.07.1998")
        return
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2 or not (is_valid_date(parts[0]) and is_valid_date(parts[1])):
        await message.answer("❌ Неверный формат. Используй ДД.ММ.ГГГГ, ДД.ММ.ГГГГ")
        return
    await message.answer("⏳ Ева составляет твой разбор, подожди немного...")
    try:
        n1 = calculate_destiny(parts[0])
        n2 = calculate_destiny(parts[1])
        prompt = f"Сделай максимально подробный и эмоциональный нумерологический разбор совместимости двух людей. Первый родился {parts[0]}, число судьбы {n1}. Второй родился {parts[1]}, число судьбы {n2}. Опиши характер каждого, их совместимость в любви и отношениях, эмоциональную связь, возможные конфликты, сильные стороны пары, прогноз отношений. Пиши как близкая подруга-нумеролог, тепло и атмосферно."
        answer = await ask_ai(prompt)
        await message.answer(f"💑 Разбор совместимости\n\n{answer}")
        await message.answer("✨ Понравился разбор?", reply_markup=review_menu())
    except Exception as e:
        logging.error(f"Compat error: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка, попробуй ещё раз.")
    await state.clear()

@dp.message(Form.waiting_date)
async def handle_date(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    text = message.text.strip()
    if not is_valid_date(text):
        await message.answer("❌ Неверная дата. Введи в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
        return
    number = calculate_destiny(text)
    waiting = user.get("waiting")
    if waiting is None:
        await message.answer("Выбери разбор из меню 👇", reply_markup=main_menu(user))
        await state.clear()
        return
    if waiting == "free":
        user["birth_date"] = text
        user["destiny_number"] = number
        user["free_used"] = True
        await save_user(message.from_user.id, user)
    await message.answer("⏳ Ева составляет твой разбор, подожди немного...")
    try:
        if waiting == "free":
            prompt = f"Сделай подробный и эмоциональный нумерологический разбор числа судьбы {number} для человека рождённого {text}. Опиши характер, сильные и слабые стороны, жизненный путь, отношение к любви и отношениям. Пиши тепло, как близкая подруга-нумеролог."
            title = f"💫 Твоё число судьбы: {number}"
        elif waiting == "when":
            prompt = f"Сделай подробный нумерологический прогноз когда человек с числом судьбы {number}, рождённый {text}, встретит своего партнёра. Опиши в каком возрасте или периоде жизни это произойдёт, при каких обстоятельствах, какие знаки укажут что это тот самый. Пиши тепло, романтично, атмосферно."
            title = "💘 Когда встретишь того самого"
        elif waiting == "portrait":
            prompt = f"Составь подробный нумерологический портрет идеального партнёра для человека с числом судьбы {number}, рождённого {text}. Опиши его характер, внешность, профессию, как он будет относиться к своей второй половине. Пиши романтично и атмосферно."
            title = "💍 Портрет твоего идеального партнёра"
        elif waiting == "unlucky":
            prompt = f"Объясни с точки зрения нумерологии почему человеку с числом судьбы {number}, рождённому {text}, не везёт в любви. Какие кармические причины, какие паттерны поведения мешают, как это исправить. Пиши тепло, с пониманием и поддержкой."
            title = "💔 Почему не везёт в любви"
        elif waiting == "matrix":
            prompt = f"Сделай полный разбор матрицы судьбы для человека рождённого {text} с числом судьбы {number}. Опиши личный потенциал, кармические задачи, таланты, деньги, любовь, предназначение. Пиши подробно и атмосферно."
            title = "🔮 Матрица судьбы"
        elif waiting == "mission":
            prompt = f"Раскрой предназначение и жизненную миссию человека с числом судьбы {number}, рождённого {text}. Что он пришёл сделать в этот мир, какие таланты должен раскрыть, какой след оставить. Пиши вдохновляюще и глубоко."
            title = "🌟 Предназначение и миссия"
        elif waiting == "karma":
            prompt = f"Опиши кармический долг человека с числом судьбы {number}, рождённого {text}. Что мешает ему в жизни, какие уроки он должен пройти, как освободиться от кармических блоков. Пиши с пониманием и глубиной."
            title = "🔴 Кармический долг"
        elif waiting == "career":
            prompt = f"Опиши идеальный карьерный путь для человека с числом судьбы {number}, рождённого {text}. Какие профессии подходят, в чём его сильные стороны на работе, как достичь успеха. Пиши конкретно и вдохновляюще."
            title = "💼 Карьерный путь"
        elif waiting == "money":
            prompt = f"Раскрой денежный код человека с числом судьбы {number}, рождённого {text}. Какие отношения с деньгами заложены в числах, как активировать денежный поток, какие блоки мешают финансовому успеху. Пиши практично и вдохновляюще."
            title = "💰 Денежный код"
        elif waiting == "days":
            prompt = f"Составь разбор сильных и слабых дней месяца для человека с числом судьбы {number}, рождённого {text}. Какие числа месяца самые благоприятные для важных дел, любви, финансов, а какие лучше провести спокойно. Пиши структурированно и понятно."
            title = "🌙 Сильные и слабые дни месяца"
        elif waiting == "ex":
            prompt = f"Сделай нумерологический анализ — вернётся ли бывший к человеку с числом судьбы {number}, рождённому {text}. Опиши энергетику их связи, есть ли шанс на воссоединение, что нужно сделать или отпустить. Пиши с теплом и пониманием."
            title = "💔 Вернётся ли бывший"
        elif waiting == "cold":
            prompt = f"Объясни нумерологически почему партнёр охладел к человеку с числом судьбы {number}, рождённому {text}. Какие числовые несовместимости могли привести к этому, что происходит на энергетическом уровне, как изменить ситуацию. Пиши тепло и честно."
            title = "❄️ Почему он охладел"
        elif waiting == "toxic":
            prompt = f"Проанализируй нумерологически является ли связь токсичной или кармической для человека с числом судьбы {number}, рождённого {text}. Опиши признаки токсичности в числах, кармические уроки этих отношений, как освободиться. Пиши глубоко и с пониманием."
            title = "☠️ Токсичная или кармическая связь"
        elif waiting == "lonely":
            prompt = f"Объясни нумерологически почему человек с числом судьбы {number}, рождённый {text}, чувствует себя одиноким. Какие числовые паттерны создают одиночество, как изменить энергетику и привлечь нужных людей. Пиши с теплом и поддержкой."
            title = "😔 Почему ты одинока"
        elif waiting == "breakup":
            prompt = f"Сделай нумерологический разбор после расставания для человека с числом судьбы {number}, рождённого {text}. Объясни почему это произошло с точки зрения чисел, какие уроки несёт это расставание, что ждёт впереди в личной жизни. Пиши с теплом и надеждой."
            title = "💔 Разбор после расставания"
        else:
            await state.clear()
            return

        answer = await ask_ai(prompt)
        await message.answer(f"{title}\n\n{answer}")

        if waiting == "free":
            await message.answer(
                "✨ Это было только начало! Выбери платный разбор и узнай больше о своей судьбе 🔮",
                reply_markup=main_menu(user)
            )
        else:
            await message.answer("✨ Понравился разбор?", reply_markup=review_menu())
    except Exception as e:
        logging.error(f"Date handler error: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка, попробуй ещё раз.")
    await state.clear()

@dp.callback_query(F.data == "leave_review")
async def leave_review(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    if user.get("review_left"):
        await callback.answer("Ты уже оставила отзыв, спасибо! 💫", show_alert=True)
        return
    if not user.get("purchased"):
        await callback.answer("Отзыв можно оставить только после покупки разбора!", show_alert=True)
        return
    await callback.message.answer("💬 Напиши свой отзыв — опубликую его в канале!")
    await state.set_state(Form.waiting_review)
    await callback.answer()

@dp.message(Form.waiting_review)
async def handle_review(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    review_text = f"⭐ Отзыв о боте Ева:\n\n{message.text}"
    try:
        await bot.send_message(REVIEWS_CHANNEL, review_text)
        user["review_left"] = True
        await save_user(message.from_user.id, user)
        await message.answer("✅ Спасибо! Твой отзыв опубликован 💫")
    except:
        await message.answer("✅ Спасибо за отзыв!")
    await state.clear()

@dp.callback_query(F.data == "show_menu")
async def show_menu(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.message.answer("🔮 Выбери разбор:", reply_markup=main_menu(user))
    await callback.answer()

async def send_daily_horoscope():
    while True:
        now = datetime.now()
        target = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        wait = (target - now).total_seconds()
        await asyncio.sleep(wait)
        try:
            rows = await db_pool.fetch('SELECT user_id, birth_date, destiny_number FROM users WHERE birth_date IS NOT NULL AND destiny_number IS NOT NULL')
            for row in rows:
                try:
                    number = row['destiny_number']
                    today = date.today().strftime("%d.%m.%Y")
                    prompt = f"Составь короткий личный прогноз на сегодня {today} для человека с числом судьбы {number}. Что принесёт этот день в любви, делах и энергии. Пиши тепло, коротко 150-200 слов, с эмодзи. Заканчивай полным предложением."
                    horoscope = await ask_ai(prompt)
                    await bot.send_message(
                        row['user_id'],
                        f"🌅 Доброе утро! Твой прогноз на сегодня:\n\n{horoscope}\n\n🔮 Хочешь больше? /menu"
                    )
                except Exception as e:
                    logging.error(f"Horoscope error for {row['user_id']}: {e}")
        except Exception as e:
            logging.error(f"Horoscope batch error: {e}")

async def send_daily_tip():
    while True:
        now = datetime.now()
        target = now.replace(hour=17, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        wait = (target - now).total_seconds()
        await asyncio.sleep(wait)
        try:
            rows = await db_pool.fetch('SELECT user_id, destiny_number FROM users WHERE destiny_number IS NOT NULL')
            for row in rows:
                try:
                    number = row['destiny_number']
                    tips = [
                        f"Знаешь ли ты что число судьбы {number} даёт тебе особый дар? Сегодня хороший день чтобы его раскрыть ✨",
                        f"Числа говорят — сегодня твоя энергия числа {number} особенно сильна 🔮 Используй это!",
                        f"Маленький секрет числа {number}: ты притягиваешь то о чём думаешь чаще всего 💫",
                        f"Число судьбы {number} — это не случайность. Это твой уникальный код вселенной 🌟",
                        f"Сегодняшний вечер идеален для того чтобы прислушаться к своей интуиции — число {number} усиливает её 🌙",
                    ]
                    tip = random.choice(tips)
                    await bot.send_message(
                        row['user_id'],
                        f"💜 Ева напоминает:\n\n{tip}\n\n🔮 Узнай больше о своей судьбе — /menu"
                    )
                except Exception as e:
                    logging.error(f"Tip error for {row['user_id']}: {e}")
        except Exception as e:
            logging.error(f"Tip batch error: {e}")

async def send_daily_channel_post():
    while True:
        now = datetime.now()
        target = now.replace(hour=10, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        wait = (target - now).total_seconds()
        await asyncio.sleep(wait)
        try:
            today = date.today().strftime("%d.%m.%Y")
            day_num = date.today().day
            prompt = f"Напиши интересный нумерологический пост для Telegram канала на сегодня {today}. Число дня: {day_num}. Тема: что значит это число, какая энергия сегодня, советы на день. Пиши красиво, с эмодзи, атмосферно. 150-200 слов. ТОЛЬКО на русском языке."
            post = await ask_ai(prompt)
            await bot.send_message(
                CHANNEL,
                f"🔮 Нумерология дня\n\n{post}\n\n✨ Узнай свой личный разбор @nnumerology_bot"
            )
        except Exception as e:
            logging.error(f"Channel post error: {e}")

async def healthcheck(request):
    return web.Response(text="OK")

async def run_web():
    app = web.Application()
    app.router.add_get("/", healthcheck)
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    await init_db()
    asyncio.create_task(run_web())
    asyncio.create_task(send_daily_horoscope())
    asyncio.create_task(send_daily_tip())
    asyncio.create_task(send_daily_channel_post())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())