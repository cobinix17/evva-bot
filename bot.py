import os
import re
import time
import logging
import asyncio
import json
import httpx
import asyncpg
import random
from collections import defaultdict
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
from readings import MATRIX_LITE

BOT_TOKEN    = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not GROQ_API_KEY:
    raise EnvironmentError("BOT_TOKEN и GROQ_API_KEY должны быть установлены!")

CHANNEL         = "@eva_numerologg"
REVIEWS_CHANNEL = "@eva_numerolog_otz"
ADMIN_ID        = 5854618444
CONTACT_URL     = "https://t.me/eva_numer"

logging.basicConfig(level=logging.INFO)
bot     = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)
db_pool = None

# ─── АНТИФЛУД ────────────────────────────────────────────────────────────────
user_last_request = defaultdict(float)
FLOOD_TIMEOUT = 3

def is_flood(user_id: int) -> bool:
    now = time.time()
    if now - user_last_request[user_id] < FLOOD_TIMEOUT:
        return True
    user_last_request[user_id] = now
    return False

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

# ─── НУМЕРОЛОГИЧЕСКИЕ РАСЧЁТЫ ────────────────────────────────────────────────
def calculate_destiny(date_str: str) -> int:
    digits = [int(d) for d in date_str if d.isdigit()]
    total  = sum(digits)
    while total > 9 and total not in (11, 22, 33):
        total = sum(int(d) for d in str(total))
    return total

def calculate_personal_year(date_str: str) -> int:
    parts = date_str.split(".")
    day, month = int(parts[0]), int(parts[1])
    current_year = datetime.now().year
    total = sum(int(d) for d in str(day)) + sum(int(d) for d in str(month)) + sum(int(d) for d in str(current_year))
    while total > 9 and total not in (11, 22, 33):
        total = sum(int(d) for d in str(total))
    return total

def calculate_karmic_numbers(date_str: str) -> list:
    digits_present = set(int(d) for d in date_str if d.isdigit() and d != '0')
    all_digits = set(range(1, 10))
    missing = sorted(all_digits - digits_present)
    return missing

def calculate_matrix(date_str: str) -> dict:
    parts  = date_str.split(".")
    day    = int(parts[0])
    month  = int(parts[1])
    year   = int(parts[2])
    destiny = calculate_destiny(date_str)

    def reduce(n):
        while n > 22:
            n = sum(int(d) for d in str(n))
        return n

    a = day
    b = month
    c = sum(int(d) for d in str(year))
    while c > 22:
        c = sum(int(d) for d in str(c))
    d = reduce(a + b + c)
    e = reduce(a + b + c + d)

    return {
        "день": a,
        "месяц": b,
        "год": c,
        "первое_число": d,
        "второе_число": e,
        "число_судьбы": destiny,
    }

GЛАСНЫЕ = set("аеёиоуыэюяАЕЁИОУЫЭЮЯ")
СОГЛАСНЫЕ = set("бвгджзйклмнпрстфхцчшщБВГДЖЗЙКЛМНПРСТФХЦЧШЩ")

def calculate_name_number(name: str) -> int:
    # Простое число имени — сумма порядковых номеров букв
    ru_alphabet = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    total = 0
    for ch in name.lower():
        if ch in ru_alphabet:
            total += ru_alphabet.index(ch) + 1
    while total > 9 and total not in (11, 22, 33):
        total = sum(int(d) for d in str(total))
    return total if total > 0 else 0

def build_numerology_context(name: str, date_str: str) -> str:
    destiny      = calculate_destiny(date_str)
    personal_yr  = calculate_personal_year(date_str)
    karmic       = calculate_karmic_numbers(date_str)
    matrix       = calculate_matrix(date_str)
    name_number  = calculate_name_number(name)
    karmic_str   = ", ".join(map(str, karmic)) if karmic else "отсутствуют"

    return (
       
        f"Пол: женский. Всегда обращайся в женском роде.\n"
        f"Имя: {name}\n"
        f"Дата рождения: {date_str}\n"
        f"Число судьбы: {destiny}\n"
        f"Число имени: {name_number}\n"
        f"Личный год ({datetime.now().year}): {personal_yr}\n"
        f"Кармические числа (отсутствующие): {karmic_str}\n"
        f"Матрица судьбы — день: {matrix['день']}, месяц: {matrix['месяц']}, "
        f"год: {matrix['год']}, первое число: {matrix['первое_число']}, "
        f"второе число: {matrix['второе_число']}\n"
    )

# ─── ПРОМТЫ ──────────────────────────────────────────────────────────────────
PROMPTS = {
    "matrix_full": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Сделай полный глубокий разбор матрицы судьбы. "
        "Обращайся к ней по имени {name}. "
        "Опиши: характер и личность, таланты и способности, денежный код, "
        "любовь и отношения, кармические задачи, предназначение и миссия, "
        "что означает её личный год сейчас, кармические числа и что они говорят. "
        "Пиши подробно, атмосферно, около 1200 слов."
    ),
    "finance": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Составь детальный финансовый прогноз на ближайший год. "
        "Обращайся к ней по имени {name}. "
        "Опиши денежные циклы, когда ожидать подъём доходов, "
        "какие сферы принесут деньги, чего избегать. "
        "Пиши конкретно и вдохновляюще, около 700 слов."
    ),
    "wealth_blocks": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Раскрой блоки богатства. Обращайся к ней по имени {name}. "
        "Какие убеждения и нумерологические паттерны мешают финансовому росту. "
        "Как убрать каждый блок. Пиши честно и глубоко, около 1000 слов."
    ),
    "freedom_path": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Опиши путь к финансовой свободе. Обращайся к ней по имени {name}. "
        "Какой путь начертан в её числах. Какие шаги сделать уже сейчас. "
        "Пиши вдохновляюще и практично, около 1000 слов."
    ),
    "calling": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Раскрой истинное призвание. Обращайся к ней по имени {name}. "
        "Какой вид деятельности приносит ей и радость, и деньги. "
        "Пиши глубоко и с теплом, около 700 слов."
    ),
    "promotion": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Дай нумерологический разбор карьерного роста. Обращайся к ней по имени {name}. "
        "Когда лучший период для повышения, как представить себя руководству. "
        "Пиши конкретно, около 700 слов."
    ),
    "own_business": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Составь разбор по открытию своего дела. Обращайся к ней по имени {name}. "
        "Подходит ли ей предпринимательство, в каких нишах успех, когда стартовать. "
        "Пиши вдохновляюще, около 1000 слов."
    ),
    "hidden_talents": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Раскрой скрытые таланты. Обращайся к ней по имени {name}. "
        "Что она умеет но недооценивает, какие способности приносят успех. "
        "Пиши восхищённо и тепло, около 700 слов."
    ),
    "main_fear": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Раскрой главный страх с точки зрения нумерологии. Обращайся к ней по имени {name}. "
        "Откуда он берётся, как мешает жизни и как преодолеть. "
        "Пиши бережно и глубоко, около 400 слов."
    ),
    "forecast_2026": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Составь подробный нумерологический прогноз на 2026 год. Обращайся к ней по имени {name}. "
        "Опиши ключевые темы года, лучшие месяцы, любовь, финансы, карьеру, здоровье. "
        "Пиши структурированно и вдохновляюще, около 1200 слов."
    ),
    "strong_weak": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Опиши сильные и слабые стороны личности. Обращайся к ней по имени {name}. "
        "Что помогает достигать целей, что тянет назад и как с этим работать. "
        "Пиши честно и с поддержкой, около 400 слов."
    ),
    "compat": (
        "Первый человек — имя {name}, дата рождения {date1}.\n"
        "Нумерологические данные первой:\n{context}\n\n"
        "Второй человек — дата рождения {date2}, число судьбы {n2}.\n\n"
        "Сделай максимально подробный разбор совместимости. Обращайся к первой по имени {name}. "
        "Опиши характер каждого, совместимость в любви, конфликты, прогноз отношений. "
        "Пиши тепло и атмосферно, около 1000 слов."
    ),
    "when": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Сделай прогноз когда она встретит своего партнёра. Обращайся к ней по имени {name}. "
        "Опиши в каком периоде жизни, при каких обстоятельствах, какие знаки укажут. "
        "Пиши романтично и атмосферно, около 700 слов."
    ),
    "portrait": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Составь портрет идеального партнёра. Обращайся к ней по имени {name}. "
        "Опиши его характер, внешность, профессию. "
        "Пиши романтично, около 700 слов."
    ),
    "unlucky": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Объясни почему ей не везёт в любви. Обращайся к ней по имени {name}. "
        "Какие кармические причины, какие паттерны мешают, как исправить. "
        "Пиши тепло и с поддержкой, около 400 слов."
    ),
    "mission": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Раскрой предназначение и жизненную миссию. Обращайся к ней по имени {name}. "
        "Что она пришла сделать в этот мир, какие таланты раскрыть. "
        "Пиши вдохновляюще и глубоко, около 1000 слов."
    ),
    "karma": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Опиши кармический долг. Обращайся к ней по имени {name}. "
        "Что мешает в жизни, какие уроки пройти, как освободиться от кармических блоков. "
        "Пиши глубоко, около 1000 слов."
    ),
    "career": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Опиши идеальный карьерный путь. Обращайся к ней по имени {name}. "
        "Какие профессии подходят, сильные стороны на работе, как достичь успеха. "
        "Пиши конкретно, около 700 слов."
    ),
    "money": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Раскрой денежный код. Обращайся к ней по имени {name}. "
        "Какие отношения с деньгами заложены в числах, как активировать поток. "
        "Пиши практично, около 700 слов."
    ),
    "days": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Составь разбор сильных и слабых дней месяца. Обращайся к ней по имени {name}. "
        "Какие числа месяца благоприятны для дел, любви, финансов. "
        "Пиши структурированно, около 700 слов."
    ),
    "ex": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Проанализируй — вернётся ли бывший. Обращайся к ней по имени {name}. "
        "Энергетика их связи, шанс на воссоединение, что нужно сделать или отпустить. "
        "Пиши тепло, около 400 слов."
    ),
    "cold": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Объясни почему партнёр охладел. Обращайся к ней по имени {name}. "
        "Числовые несовместимости, что происходит на энергетическом уровне, как изменить. "
        "Пиши тепло и честно, около 400 слов."
    ),
    "toxic": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Проанализируй является ли связь токсичной или кармической. Обращайся по имени {name}. "
        "Признаки токсичности в числах, кармические уроки, как освободиться. "
        "Пиши глубоко, около 700 слов."
    ),
    "lonely": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Объясни почему она чувствует себя одинокой. Обращайся по имени {name}. "
        "Какие числовые паттерны создают одиночество, как изменить энергетику. "
        "Пиши тепло, около 400 слов."
    ),
    "breakup": (
        "Вот нумерологические данные для {name}:\n{context}\n\n"
        "Сделай разбор после расставания. Обращайся к ней по имени {name}. "
        "Почему это произошло по числам, уроки расставания, что ждёт впереди. "
        "Пиши с теплом и надеждой, около 700 слов."
    ),
}

TITLES = {
    "free":          "💫 Матрица судьбы (Лайт)",
    "matrix_full":   "🔮 Матрица судьбы (Полная)",
    "finance":       "💹 Финансовый прогноз",
    "wealth_blocks": "🚧 Блоки богатства",
    "freedom_path":  "🗺 Путь к свободе",
    "calling":       "🌠 Призвание",
    "promotion":     "📈 Повышение",
    "own_business":  "🏢 Свой бизнес",
    "hidden_talents":"✨ Скрытые таланты",
    "main_fear":     "😨 Главный страх",
    "forecast_2026": "🗓 Прогноз на 2026 год",
    "strong_weak":   "⚖️ Сильная и слабая сторона",
    "compat":        "💑 Совместимость двух людей",
    "when":          "💘 Когда встретишь того самого",
    "portrait":      "💍 Портрет идеального партнёра",
    "unlucky":       "💔 Почему не везёт в любви",
    "mission":       "🌟 Предназначение и миссия",
    "karma":         "🔴 Кармический долг",
    "career":        "💼 Карьерный путь",
    "money":         "💰 Денежный код",
    "days":          "🌙 Сильные и слабые дни",
    "ex":            "💔 Вернётся ли бывший",
    "cold":          "❄️ Почему он охладел",
    "toxic":         "☠️ Токсичная или кармическая связь",
    "lonely":        "😔 Почему ты одинока",
    "breakup":       "💔 Разбор после расставания",
}

# цена каждого разбора
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
}

PAID_RAZBORY = {k: v for k, v in TITLES.items() if k != "free"}

# ─── DB ──────────────────────────────────────────────────────────────────────
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id            BIGINT PRIMARY KEY,
            first_name         TEXT,
            free_used          BOOLEAN DEFAULT FALSE,
            subscribed_channel BOOLEAN DEFAULT FALSE,
            birth_date         TEXT,
            destiny_number     INTEGER,
            purchased          TEXT DEFAULT '[]',
            waiting            TEXT,
            review_left        BOOLEAN DEFAULT FALSE
        )
    ''')
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS coupons (
            code       TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT NOW(),
            expires_at TIMESTAMP,
            used_by    BIGINT DEFAULT NULL
        )
    ''')
    try:
        await db_pool.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT")
    except Exception:
        pass

async def get_user(user_id: int) -> dict:
    row = await db_pool.fetchrow('SELECT * FROM users WHERE user_id = $1', user_id)
    if not row:
        await db_pool.execute('INSERT INTO users (user_id) VALUES ($1)', user_id)
        row = await db_pool.fetchrow('SELECT * FROM users WHERE user_id = $1', user_id)
    user = dict(row)
    user['purchased'] = json.loads(user['purchased'])
    if user_id == ADMIN_ID:
        user['free_used'] = True
        user['subscribed_channel'] = True
        for r in list(PAID_RAZBORY.keys()):
            if r not in user['purchased']:
                user['purchased'].append(r)
    return user

async def save_user(user_id: int, user: dict):
    await db_pool.execute('''
        UPDATE users SET
            first_name         = $1,
            free_used          = $2,
            subscribed_channel = $3,
            birth_date         = $4,
            destiny_number     = $5,
            purchased          = $6,
            waiting            = $7,
            review_left        = $8
        WHERE user_id = $9
    ''',
        user.get('first_name'),
        user['free_used'],
        user['subscribed_channel'],
        user.get('birth_date'),
        user.get('destiny_number'),
        json.dumps(user['purchased']),
        user.get('waiting'),
        user['review_left'],
        user_id
    )

# ─── FSM ─────────────────────────────────────────────────────────────────────
class Form(StatesGroup):
    waiting_name        = State()
    waiting_birth_date  = State()
    waiting_date        = State()
    waiting_second_date = State()
    waiting_review      = State()

# ─── ВСПОМОГАТЕЛЬНЫЕ ─────────────────────────────────────────────────────────
def is_valid_date(date_str: str) -> bool:
    try:
        datetime.strptime(date_str, "%d.%m.%Y")
        return True
    except ValueError:
        return False

async def check_subscription(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False

# ─── GROQ + RETRY ────────────────────────────────────────────────────────────
FOREIGN_RE = re.compile(r'[a-zA-ZÀ-ÿ\u0080-\u024F\u1E00-\u1EFF\u3000-\u9FFF\u0250-\u02AF]')

def has_foreign(text: str) -> bool:
    return bool(FOREIGN_RE.search(text))

async def ask_ai(prompt: str) -> str:
    models = [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "llama3-8b-8192",
    ]
    url     = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    system_prompt = (
        "Ты — Ева, тёплый и мудрый нумеролог. Общаешься как близкая подруга. "
        "КРИТИЧЕСКИ ВАЖНО: пишешь ТОЛЬКО на русском языке. "
        "Никаких иероглифов, никакого английского и других зарубежных языков, никакого другого алфавита — вообще. "
        "Весь ответ от первого до последнего символа — только кириллица. "
        "Пишешь красиво, с эмодзи, атмосферно. Обращаешься на ты. "
        "Используй абзацы. Заканчивай полным предложением."
    )
    last_error = None
    for model in models:
        for attempt in range(2):  # 2 попытки на каждую модель
            try:
                data = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": prompt},
                    ],
                    "max_tokens": 2000,
                }
                async with httpx.AsyncClient() as client:
                    response = await client.post(url, headers=headers, json=data, timeout=90)
                    response.raise_for_status()
                    raw = response.json()["choices"][0]["message"]["content"]
                    if has_foreign(raw):
                        logging.warning(f"Model {model} attempt {attempt+1} returned foreign chars, retrying...")
                        continue
                    return raw
            except Exception as e:
                last_error = e
                logging.warning(f"Model {model} attempt {attempt+1} failed: {e}")
                break
    raise last_error or Exception("Все модели вернули иностранные символы")

def build_prompt(key: str, **kwargs) -> str:
    return PROMPTS.get(key, "").format(**kwargs)

# ─── КЛАВИАТУРЫ ──────────────────────────────────────────────────────────────
def check_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я подписалась!", callback_data="check_sub")],
    ])

def date_choice_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Для себя",      callback_data="use_my_date")],
        [InlineKeyboardButton(text="📅 Другая дата",   callback_data="use_new_date")],
    ])

def main_menu(user=None) -> InlineKeyboardMarkup:
    buttons = []
    if user and not user.get("free_used"):
        buttons.append([InlineKeyboardButton(
            text="💫 Матрица судьбы (Лайт) — бесплатно",
            callback_data="free"
        )])

    buttons.append([InlineKeyboardButton(text="── Судьба и личность ──", callback_data="noop")])
    buttons += [
        [InlineKeyboardButton(text="🔮 Матрица судьбы (Полная) — 149 ⭐",  callback_data="buy_matrix_full")],
        [InlineKeyboardButton(text="🌟 Предназначение и миссия — 99 ⭐",   callback_data="buy_mission")],
        [InlineKeyboardButton(text="✨ Скрытые таланты — 79 ⭐",           callback_data="buy_hidden_talents")],
        [InlineKeyboardButton(text="⚖️ Сильная/слабая сторона — 49 ⭐",   callback_data="buy_strong_weak")],
        [InlineKeyboardButton(text="😨 Главный страх — 49 ⭐",             callback_data="buy_main_fear")],
        [InlineKeyboardButton(text="🔴 Кармический долг — 99 ⭐",          callback_data="buy_karma")],
        [InlineKeyboardButton(text="🗓 Прогноз на 2026 год — 149 ⭐",      callback_data="buy_forecast_2026")],
    ]

    buttons.append([InlineKeyboardButton(text="── Деньги и карьера ──", callback_data="noop")])
    buttons += [
        [InlineKeyboardButton(text="💹 Финансовый прогноз — 99 ⭐",        callback_data="buy_finance")],
        [InlineKeyboardButton(text="🚧 Блоки богатства — 149 ⭐",          callback_data="buy_wealth_blocks")],
        [InlineKeyboardButton(text="🗺 Путь к финансовой свободе — 149 ⭐",callback_data="buy_freedom_path")],
        [InlineKeyboardButton(text="🌠 Призвание — 79 ⭐",                 callback_data="buy_calling")],
        [InlineKeyboardButton(text="📈 Повышение — 99 ⭐",                 callback_data="buy_promotion")],
        [InlineKeyboardButton(text="🏢 Свой бизнес — 99 ⭐",              callback_data="buy_own_business")],
        [InlineKeyboardButton(text="💼 Карьерный путь — 79 ⭐",            callback_data="buy_career")],
        [InlineKeyboardButton(text="💰 Денежный код — 79 ⭐",              callback_data="buy_money")],
        [InlineKeyboardButton(text="🌙 Сильные и слабые дни — 79 ⭐",     callback_data="buy_days")],
    ]

    buttons.append([InlineKeyboardButton(text="── Любовь и отношения ──", callback_data="noop")])
    buttons += [
        [InlineKeyboardButton(text="💑 Совместимость двух людей — 99 ⭐",  callback_data="buy_compat")],
        [InlineKeyboardButton(text="💘 Когда встретишь того самого — 79 ⭐",callback_data="buy_when")],
        [InlineKeyboardButton(text="💍 Портрет идеального партнёра — 79 ⭐",callback_data="buy_portrait")],
        [InlineKeyboardButton(text="💔 Почему не везёт в любви — 49 ⭐",   callback_data="buy_unlucky")],
        [InlineKeyboardButton(text="💔 Вернётся ли бывший — 49 ⭐",        callback_data="buy_ex")],
        [InlineKeyboardButton(text="❄️ Почему он охладел — 49 ⭐",         callback_data="buy_cold")],
        [InlineKeyboardButton(text="☠️ Токсичная связь — 79 ⭐",           callback_data="buy_toxic")],
        [InlineKeyboardButton(text="😔 Почему ты одинока — 49 ⭐",         callback_data="buy_lonely")],
        [InlineKeyboardButton(text="💔 Разбор после расставания — 79 ⭐",  callback_data="buy_breakup")],
    ]

    buttons.append([InlineKeyboardButton(
        text="🌸 Личный разбор от Евы (за рубли)",
        url=CONTACT_URL
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def review_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😍 Оставить отзыв", callback_data="leave_review")],
        [InlineKeyboardButton(text="🔮 Другие разборы",  callback_data="show_menu")],
    ])

def coupon_razboy_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔮 Матрица судьбы (Полная)", callback_data="coupon_matrix_full")],
        [InlineKeyboardButton(text="💹 Финансовый прогноз",      callback_data="coupon_finance")],
        [InlineKeyboardButton(text="🌟 Предназначение",          callback_data="coupon_mission")],
        [InlineKeyboardButton(text="✨ Скрытые таланты",         callback_data="coupon_hidden_talents")],
        [InlineKeyboardButton(text="🗓 Прогноз на 2026",         callback_data="coupon_forecast_2026")],
        [InlineKeyboardButton(text="💑 Совместимость",           callback_data="coupon_compat")],
        [InlineKeyboardButton(text="💘 Когда встретишь его",     callback_data="coupon_when")],
        [InlineKeyboardButton(text="💔 Вернётся ли бывший",      callback_data="coupon_ex")],
    ])

# ─── КУПОНЫ ──────────────────────────────────────────────────────────────────
async def create_coupon(code: str) -> bool:
    expires = datetime.now() + timedelta(hours=48)
    try:
        await db_pool.execute(
            'INSERT INTO coupons (code, expires_at) VALUES ($1, $2)',
            code.upper(), expires
        )
        return True
    except Exception:
        return False

async def use_coupon(code: str, user_id: int) -> str:
    row = await db_pool.fetchrow('SELECT * FROM coupons WHERE code = $1', code.upper())
    if not row:
        return 'not_found'
    if row['used_by'] is not None:
        return 'used'
    if row['expires_at'] < datetime.now():
        return 'expired'
    await db_pool.execute(
        'UPDATE coupons SET used_by = $1 WHERE code = $2', user_id, code.upper()
    )
    return 'ok'

# ─── ОНБОРДИНГ ───────────────────────────────────────────────────────────────
@dp.message(Command("start"), StateFilter("*"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    if not user.get("first_name") and message.from_user.first_name:
        user["first_name"] = message.from_user.first_name
        await save_user(message.from_user.id, user)
    if not user["subscribed_channel"]:
        is_sub = await check_subscription(message.from_user.id)
        if is_sub:
            user["subscribed_channel"] = True
            await save_user(message.from_user.id, user)
    if not user["subscribed_channel"]:
        await message.answer(
            "🔮 Привет! Я Ева — твой личный нумеролог.\n\n"
            "✨ Что я умею:\n\n"
            "• Бесплатный разбор матрицы судьбы (Лайт)\n"
            "• Полная матрица судьбы и кармический долг\n"
            "• Финансовый прогноз и блоки богатства\n"
            "• Путь к своему делу и призванию\n"
            "• Совместимость, любовь, отношения\n"
            "• Прогноз на 2026 год\n\n"
            "Всё это по твоей дате рождения — точно и личностно 🌸\n\n"
            f"Подпишись на {CHANNEL} и получи бесплатный разбор 👇",
            reply_markup=check_menu()
        )
        return
    if not user["free_used"]:
        name = user.get("first_name") or ""
        greeting = f"✨ Привет, {name}! " if name else "✨ Привет! "
        await message.answer(
            greeting + "Давай познакомимся.\n\nКак мне тебя называть? Введи своё имя 👇"
        )
        await state.set_state(Form.waiting_name)
        return
    await message.answer("🔮 Выбери свой разбор 👇", reply_markup=main_menu(user))

@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery, state: FSMContext):
    user   = await get_user(callback.from_user.id)
    is_sub = await check_subscription(callback.from_user.id)
    if not is_sub:
        await callback.answer("❌ Ты ещё не подписалась!", show_alert=True)
        return
    user["subscribed_channel"] = True
    await save_user(callback.from_user.id, user)
    await callback.answer()
    if user["free_used"]:
        await callback.message.answer("✅ Подписка подтверждена!", reply_markup=main_menu(user))
        return
    await callback.message.answer("✅ Отлично! Как мне тебя называть? Введи своё имя 👇")
    await state.set_state(Form.waiting_name)

@dp.message(StateFilter(Form.waiting_name))
async def handle_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2 or len(name) > 30:
        await message.answer("Введи настоящее имя (от 2 до 30 символов) 😊")
        return
    user = await get_user(message.from_user.id)
    user["first_name"] = name
    await save_user(message.from_user.id, user)
    await message.answer(
        f"Приятно познакомиться, {name}! 🌸\n\n"
        "Введи свою дату рождения в формате ДД.ММ.ГГГГ\n"
        "Например: 15.03.1995"
    )
    await state.set_state(Form.waiting_birth_date)

@dp.message(StateFilter(Form.waiting_birth_date))
async def handle_birth_date(message: Message, state: FSMContext):
    text = message.text.strip()
    if not is_valid_date(text):
        await message.answer("❌ Неверная дата. Введи в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
        return
    user   = await get_user(message.from_user.id)
    number = calculate_destiny(text)
    user["birth_date"]     = text
    user["destiny_number"] = number
    user["free_used"]      = True
    user["waiting"]        = None
    await save_user(message.from_user.id, user)
    name = user.get("first_name") or "дорогая"
    await message.answer(f"⏳ Составляю твой разбор, {name}... Подожди немного ✨")
    try:
        template = MATRIX_LITE.get(number, MATRIX_LITE.get(9, ""))
        answer   = template.format(name=name)
        await send_long(message.chat.id, f"💫 Матрица судьбы (Лайт)\nЧисло судьбы {name}: {number}\n\n{answer}")
        await message.answer(
            "✨ Это была Лайт версия!\n\n"
            "Выбери полный разбор и узнай всё о своей судьбе 🔮",
            reply_markup=main_menu(user)
        )
    except Exception as e:
        logging.error(f"Onboarding error: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка, попробуй ещё раз /start")
    await state.clear()

# ─── КОМАНДЫ ─────────────────────────────────────────────────────────────────
@dp.message(Command("menu"), StateFilter("*"))
async def menu_cmd(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    await message.answer("🔮 Выбери разбор:", reply_markup=main_menu(user))

@dp.message(Command("promo"), StateFilter("*"))
async def promo_cmd(message: Message, state: FSMContext):
    await state.clear()
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Введи промокод так: /promo КОД")
        return
    code   = parts[1].upper()
    result = await use_coupon(code, message.from_user.id)
    if result == 'not_found':
        await message.answer("❌ Такого промокода не существует.")
    elif result == 'expired':
        await message.answer("❌ Этот промокод уже истёк.")
    elif result == 'used':
        await message.answer("❌ Этот промокод уже был использован.")
    elif result == 'ok':
        await message.answer(
            "🎁 Промокод активирован! Выбери свой бесплатный разбор 👇",
            reply_markup=coupon_razboy_menu()
        )

@dp.message(Command("coupon"), StateFilter("*"))
async def coupon_cmd(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Использование: /coupon КОД\nНапример: /coupon INSTAGRAM2026")
        return
    code    = parts[1].upper()
    success = await create_coupon(code)
    if success:
        expires = (datetime.now() + timedelta(hours=48)).strftime("%d.%m.%Y %H:%M")
        await message.answer(
            f"✅ Промокод создан!\n\n"
            f"Код: <code>{code}</code>\n"
            f"Действует до: {expires}\n\n"
            f"Юзер вводит: /promo {code}",
            parse_mode="HTML"
        )
    else:
        await message.answer("❌ Такой промокод уже существует.")

@dp.message(Command("admin"), StateFilter("*"))
async def admin_panel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    total         = await db_pool.fetchval('SELECT COUNT(*) FROM users')
    free_used     = await db_pool.fetchval('SELECT COUNT(*) FROM users WHERE free_used = TRUE')
    reviews       = await db_pool.fetchval('SELECT COUNT(*) FROM users WHERE review_left = TRUE')
    coupons_total = await db_pool.fetchval('SELECT COUNT(*) FROM coupons')
    coupons_used  = await db_pool.fetchval('SELECT COUNT(*) FROM coupons WHERE used_by IS NOT NULL')
    # Исключаем админа из подсчёта
    rows = await db_pool.fetch(
        'SELECT purchased FROM users WHERE user_id != $1', ADMIN_ID
    )
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
        f"🎟 Купонов: создано {coupons_total} / использовано {coupons_used}\n"
        f"📝 Отзывов: {reviews}\n\n"
        f"🏆 Топ разборов:\n{top_text}"
    )

# ─── КУПОН — ВЫБОР РАЗБОРА ───────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("coupon_"))
async def coupon_razboy_handler(callback: CallbackQuery, state: FSMContext):
    key  = callback.data.replace("coupon_", "")
    user = await get_user(callback.from_user.id)
    if key not in user["purchased"]:
        user["purchased"].append(key)
    user["waiting"] = key
    await save_user(callback.from_user.id, user)
    await callback.answer()
    if key == "compat":
        await callback.message.answer(
            "💑 Введи две даты через запятую:\nНапример: 15.03.1995, 22.07.1998"
        )
        await state.set_state(Form.waiting_second_date)
    else:
        await _ask_date(callback.message, user)
        await state.set_state(Form.waiting_date)

# ─── УМНАЯ ДАТА ──────────────────────────────────────────────────────────────
async def _ask_date(message: Message, user: dict):
    if user.get("birth_date"):
        await message.answer(
            f"Делаешь разбор для себя ({user['birth_date']}) или введёшь другую дату?",
            reply_markup=date_choice_menu()
        )
    else:
        await message.answer(
            "📅 Введи дату рождения в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995"
        )

@dp.callback_query(F.data == "use_my_date")
async def use_my_date(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    await callback.answer()
    await _process_date(callback.message, user, user["birth_date"], state)

@dp.callback_query(F.data == "use_new_date")
async def use_new_date(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "📅 Введи дату рождения в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995"
    )
    await state.set_state(Form.waiting_date)

# ─── ПОКУПКИ ─────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "noop")
async def noop_handler(callback: CallbackQuery):
    await callback.answer()

@dp.callback_query(F.data == "free")
async def free_handler(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
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
        await save_user(callback.from_user.id, user)
    if user["free_used"]:
        await callback.message.answer(
            "💫 Бесплатный разбор ты уже получила.\n\nВыбери платный разбор 🔮",
            reply_markup=main_menu(user)
        )
        await callback.answer()
        return
    await callback.message.answer("✨ Как мне тебя называть? Введи своё имя 👇")
    await state.set_state(Form.waiting_name)
    await callback.answer()

async def send_invoice(chat_id, title, description, payload, amount):
    await bot.send_invoice(
        chat_id=chat_id, title=title, description=description,
        payload=payload, currency="XTR",
        prices=[LabeledPrice(label=title, amount=amount)],
    )

@dp.callback_query(F.data.startswith("buy_"))
async def buy_handler(callback: CallbackQuery, state: FSMContext):
    key  = callback.data.replace("buy_", "")
    user = await get_user(callback.from_user.id)
    if key in user["purchased"]:
        user["waiting"] = key
        await save_user(callback.from_user.id, user)
        await callback.answer()
        if key == "compat":
            await callback.message.answer(
                "💑 Введи две даты через запятую:\nНапример: 15.03.1995, 22.07.1998"
            )
            await state.set_state(Form.waiting_second_date)
        else:
            await _ask_date(callback.message, user)
            await state.set_state(Form.waiting_date)
        return
    if key in PAID_RAZBORY:
        price = PRICES.get(key, 49)
        title = PAID_RAZBORY[key]
        await send_invoice(callback.message.chat.id, title, TITLES.get(key, title), key, price)
    await callback.answer()

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message, state: FSMContext):
    user    = await get_user(message.from_user.id)
    payload = message.successful_payment.invoice_payload
    if payload not in user["purchased"]:
        user["purchased"].append(payload)
    user["waiting"] = payload
    await save_user(message.from_user.id, user)
    if payload == "compat":
        await message.answer(
            "✅ Оплата прошла! Введи две даты через запятую:\nНапример: 15.03.1995, 22.07.1998"
        )
        await state.set_state(Form.waiting_second_date)
    else:
        await _ask_date(message, user)
        await state.set_state(Form.waiting_date)

# ─── ОБРАБОТКА ДАТ ───────────────────────────────────────────────────────────
async def _process_date(message: Message, user: dict, date_str: str, state: FSMContext):
    number  = calculate_destiny(date_str)
    waiting = user.get("waiting")
    name    = user.get("first_name") or "дорогая"
    if not waiting:
        await message.answer("Выбери разбор из меню 👇", reply_markup=main_menu(user))
        await state.clear()
        return
    if not user.get("birth_date"):
        user["birth_date"]     = date_str
        user["destiny_number"] = number
        await save_user(message.from_user.id if hasattr(message, 'from_user') else user.get('user_id'), user)
    await message.answer(f"⏳ Ева составляет разбор для {name}... Подожди немного ✨")
    try:
        context = build_numerology_context(name, date_str)
        prompt  = build_prompt(waiting, name=name, context=context, date=date_str)
        answer  = await ask_ai(prompt)
        title   = TITLES.get(waiting, "🔮 Разбор")
        await send_long(message.chat.id, f"{title}\n\n{answer}")
        await message.answer("✨ Понравился разбор?", reply_markup=review_menu())
    except Exception as e:
        logging.error(f"Date handler error [{waiting}]: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка, попробуй ещё раз.")
    await state.clear()

@dp.message(StateFilter(Form.waiting_second_date))
async def handle_two_dates(message: Message, state: FSMContext):
    if is_flood(message.from_user.id):
        await message.answer("⏳ Не так быстро! Подожди пару секунд.")
        return
    user  = await get_user(message.from_user.id)
    text  = message.text.strip()
    if "," not in text:
        await message.answer("❌ Введи две даты через запятую.\nНапример: 15.03.1995, 22.07.1998")
        return
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2 or not all(is_valid_date(p) for p in parts):
        await message.answer("❌ Неверный формат. Используй ДД.ММ.ГГГГ, ДД.ММ.ГГГГ")
        return
    name = user.get("first_name") or "дорогая"
    await message.answer("⏳ Ева составляет разбор совместимости...")
    try:
        n2      = calculate_destiny(parts[1])
        context = build_numerology_context(name, parts[0])
        prompt  = build_prompt("compat", name=name, context=context,
                               date1=parts[0], date2=parts[1], n2=n2)
        answer  = await ask_ai(prompt)
        await send_long(message.chat.id, f"💑 Совместимость\n\n{answer}")
        await message.answer("✨ Понравился разбор?", reply_markup=review_menu())
    except Exception as e:
        logging.error(f"Compat error: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка, попробуй ещё раз.")
    await state.clear()

@dp.message(StateFilter(Form.waiting_date))
async def handle_date(message: Message, state: FSMContext):
    if is_flood(message.from_user.id):
        await message.answer("⏳ Не так быстро! Подожди пару секунд.")
        return
    user = await get_user(message.from_user.id)
    text = message.text.strip()
    if not is_valid_date(text):
        await message.answer("❌ Неверная дата. Введи в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
        return
    await _process_date(message, user, text, state)

# ─── ОТЗЫВЫ ──────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "leave_review")
async def leave_review(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    if user.get("review_left"):
        await callback.answer("Ты уже оставила отзыв, спасибо! 💫", show_alert=True)
        return
    if not user.get("purchased"):
        await callback.answer("Отзыв можно оставить только после покупки!", show_alert=True)
        return
    await callback.message.answer("💬 Напиши свой отзыв — опубликую его в канале!")
    await state.set_state(Form.waiting_review)
    await callback.answer()

@dp.message(StateFilter(Form.waiting_review))
async def handle_review(message: Message, state: FSMContext):
    user        = await get_user(message.from_user.id)
    name        = user.get("first_name") or "Аноним"
    review_text = f"⭐ Отзыв о боте @nnumerology_bot\n👤 {name}\n\n{message.text}"
    try:
        await bot.send_message(REVIEWS_CHANNEL, review_text)
        user["review_left"] = True
        await save_user(message.from_user.id, user)
        await message.answer("✅ Спасибо! Твой отзыв опубликован 💫")
    except Exception:
        await message.answer("✅ Спасибо за отзыв!")
    await state.clear()

@dp.callback_query(F.data == "show_menu")
async def show_menu(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.message.answer("🔮 Выбери разбор:", reply_markup=main_menu(user))
    await callback.answer()

# ─── РАССЫЛКИ ────────────────────────────────────────────────────────────────
async def send_daily_horoscope():
    while True:
        now    = datetime.utcnow()
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            rows  = await db_pool.fetch(
                'SELECT user_id, first_name, destiny_number FROM users '
                'WHERE birth_date IS NOT NULL AND destiny_number IS NOT NULL'
            )
            today = date.today().strftime("%d.%m.%Y")
            for row in rows:
                try:
                    number = row['destiny_number']
                    name   = row['first_name'] or "дорогая"
                    prompt = (
                        f"Составь короткий личный прогноз на сегодня {today} для {name} "
                        f"с числом судьбы {number}. Обращайся к ней по имени {name}. "
                        "Что принесёт этот день в любви, делах и энергии. "
                        "Пиши тепло, 150-200 слов, с эмодзи. Только кириллица."
                    )
                    horoscope = await ask_ai(prompt)
                    await bot.send_message(
                        row['user_id'],
                        f"🌅 Доброе утро, {name}! Твой прогноз на сегодня:\n\n{horoscope}\n\n🔮 /menu"
                    )
                    await asyncio.sleep(0.05)
                except Exception as e:
                    logging.error(f"Horoscope error {row['user_id']}: {e}")
        except Exception as e:
            logging.error(f"Horoscope batch error: {e}")

async def send_daily_tip():
    while True:
        now    = datetime.utcnow()
        target = now.replace(hour=11, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            rows = await db_pool.fetch(
                'SELECT user_id, first_name, destiny_number FROM users WHERE destiny_number IS NOT NULL'
            )
            for row in rows:
                try:
                    number = row['destiny_number']
                    name   = row['first_name'] or "дорогая"
                    tips   = [
                        f"{name}, знаешь ли ты, что число судьбы {number} даёт тебе особый дар? Сегодня хороший день чтобы его раскрыть ✨",
                        f"Числа говорят — сегодня твоя энергия числа {number} особенно сильна 🔮 Используй это, {name}!",
                        f"Маленький секрет числа {number}: ты притягиваешь то о чём думаешь чаще всего 💫 Думай о лучшем, {name}!",
                        f"Число судьбы {number} — это не случайность. Это твой уникальный код вселенной 🌟",
                        f"Сегодня идеальный день прислушаться к своей интуиции — число {number} усиливает её, {name} 🌙",
                    ]
                    await bot.send_message(
                        row['user_id'],
                        f"💜 Ева напоминает:\n\n{random.choice(tips)}\n\n🔮 /menu"
                    )
                    await asyncio.sleep(0.05)
                except Exception as e:
                    logging.error(f"Tip error {row['user_id']}: {e}")
        except Exception as e:
            logging.error(f"Tip batch error: {e}")

async def send_daily_channel_post():
    while True:
        now    = datetime.utcnow()
        target = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            today   = date.today().strftime("%d.%m.%Y")
            day_num = date.today().day
            prompt  = (
                f"Напиши нумерологический пост для Телеграм канала на сегодня {today}. "
                f"Число дня: {day_num}. "
                "Что значит это число, какая энергия сегодня, советы на день. "
                "Пиши красиво, с эмодзи, атмосферно. 150-200 слов. Только кириллица."
            )
            post = await ask_ai(prompt)
            await bot.send_message(
                CHANNEL,
                f"🔮 Нумерология дня\n\n{post}\n\n✨ Узнай свой личный разбор @nnumerology_bot"
            )
        except Exception as e:
            logging.error(f"Channel post error: {e}")

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
    await init_db()
    asyncio.create_task(run_web())
    asyncio.create_task(send_daily_horoscope())
    asyncio.create_task(send_daily_tip())
    asyncio.create_task(send_daily_channel_post())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
 