import os
import re
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

# ─── ENV ────────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not GROQ_API_KEY:
    raise EnvironmentError("BOT_TOKEN и GROQ_API_KEY должны быть установлены!")

CHANNEL         = "@eva_numerologg"
REVIEWS_CHANNEL = "@eva_numerolog_otz"
ADMIN_ID        = 5854618444
CONTACT_URL     = "https://t.me/eva_numer"  # ← замени на свой контакт

logging.basicConfig(level=logging.INFO)
bot     = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)
db_pool = None

# ─── ПРОМТЫ (словарь) ────────────────────────────────────────────────────────────
PROMPTS = {
    "free": (
        "Сделай подробный и эмоциональный нумерологический разбор числа судьбы {number} "
        "для человека по имени {name}, рождённого {date}. "
        "Обращайся к ней по имени. Опиши характер, сильные и слабые стороны, жизненный путь, "
        "отношение к любви и отношениям. Пиши тепло, как близкая подруга-нумеролог."
    ),
    "matrix": (
        "Сделай полный разбор матрицы судьбы для {name}, рождённой {date}, число судьбы {number}. "
        "Обращайся к ней по имени. Опиши личный потенциал, кармические задачи, таланты, деньги, "
        "любовь, предназначение. Пиши подробно и атмосферно."
    ),
    "finance": (
        "Составь детальный финансовый прогноз на ближайший год для {name}, рождённой {date}, "
        "число судьбы {number}. Обращайся к ней по имени. Опиши денежные циклы, когда ожидать "
        "подъём доходов, какие сферы принесут деньги, чего избегать. Пиши конкретно и вдохновляюще."
    ),
    "wealth_blocks": (
        "Раскрой блоки богатства {name} (дата рождения {date}, число судьбы {number}). "
        "Обращайся к ней по имени. Какие убеждения, страхи и нумерологические паттерны мешают "
        "финансовому росту. Как убрать каждый блок. Пиши честно, глубоко и с поддержкой."
    ),
    "freedom_path": (
        "Опиши путь к финансовой свободе для {name}, рождённой {date}, число судьбы {number}. "
        "Обращайся к ней по имени. Какой путь — работа на себя, инвестиции, творчество — "
        "начертан в её числах. Какие шаги сделать уже сейчас. Пиши вдохновляюще и практично."
    ),
    "calling": (
        "Раскрой истинное призвание {name} (дата рождения {date}, число судьбы {number}). "
        "Обращайся к ней по имени. Какой вид деятельности приносит ей и радость, и деньги, "
        "что говорят числа о её главном таланте. Пиши глубоко и с теплом."
    ),
    "promotion": (
        "Дай нумерологический разбор карьерного роста для {name}, рождённой {date}, "
        "число судьбы {number}. Обращайся к ней по имени. Когда лучший период для повышения, "
        "как выгодно представить себя руководству, какие числа усиливают карьеру. Пиши конкретно."
    ),
    "own_business": (
        "Составь нумерологический разбор по открытию своего бизнеса для {name}, "
        "рождённой {date}, число судьбы {number}. Обращайся к ней по имени. "
        "Подходит ли ей предпринимательство, в каких нишах максимальный успех, "
        "когда лучшее время стартовать. Пиши вдохновляюще и практично."
    ),
    "hidden_talents": (
        "Раскрой скрытые таланты {name} (дата рождения {date}, число судьбы {number}). "
        "Обращайся к ней по имени. Что она умеет, но недооценивает, какие способности "
        "приносят ей успех без особых усилий. Пиши восхищённо и тепло."
    ),
    "main_fear": (
        "Раскрой главный страх {name} (дата рождения {date}, число судьбы {number}) "
        "с точки зрения нумерологии. Обращайся к ней по имени. Откуда он берётся, "
        "как мешает жизни и как преодолеть. Пиши с пониманием, глубоко и бережно."
    ),
    "forecast_2026": (
        "Составь подробный нумерологический прогноз на 2026 год для {name}, "
        "рождённой {date}, число судьбы {number}. Обращайся к ней по имени. "
        "Опиши ключевые темы года, лучшие месяцы, любовь, финансы, карьеру, здоровье. "
        "Пиши структурированно, с эмодзи, вдохновляюще."
    ),
    "strong_weak": (
        "Опиши сильные и слабые стороны личности {name} (дата рождения {date}, "
        "число судьбы {number}) с точки зрения нумерологии. Обращайся к ней по имени. "
        "Что помогает ей достигать целей, что тянет назад и как с этим работать. "
        "Пиши честно и с поддержкой."
    ),
    "compat": (
        "Сделай максимально подробный нумерологический разбор совместимости двух людей. "
        "Имя первого: {name}, дата рождения {date1}, число судьбы {n1}. "
        "Второй родился {date2}, число судьбы {n2}. "
        "Обращайся к первому по имени. Опиши характер каждого, совместимость в любви, "
        "эмоциональную связь, возможные конфликты, сильные стороны пары, прогноз отношений. "
        "Пиши как близкая подруга-нумеролог, тепло и атмосферно."
    ),
    "when": (
        "Сделай подробный нумерологический прогноз когда {name} с числом судьбы {number}, "
        "рождённая {date}, встретит своего партнёра. Обращайся к ней по имени. "
        "Опиши в каком возрасте или периоде жизни это произойдёт, при каких обстоятельствах, "
        "какие знаки укажут что это тот самый. Пиши тепло, романтично, атмосферно."
    ),
    "portrait": (
        "Составь подробный нумерологический портрет идеального партнёра для {name} "
        "с числом судьбы {number}, рождённой {date}. Обращайся к ней по имени. "
        "Опиши его характер, внешность, профессию, как он будет относиться к своей второй половине. "
        "Пиши романтично и атмосферно."
    ),
    "unlucky": (
        "Объясни с точки зрения нумерологии почему {name} с числом судьбы {number}, "
        "рождённой {date}, не везёт в любви. Обращайся к ней по имени. "
        "Какие кармические причины, какие паттерны поведения мешают, как это исправить. "
        "Пиши тепло, с пониманием и поддержкой."
    ),
    "mission": (
        "Раскрой предназначение и жизненную миссию {name} с числом судьбы {number}, "
        "рождённой {date}. Обращайся к ней по имени. "
        "Что она пришла сделать в этот мир, какие таланты должна раскрыть, какой след оставить. "
        "Пиши вдохновляюще и глубоко."
    ),
    "karma": (
        "Опиши кармический долг {name} с числом судьбы {number}, рождённой {date}. "
        "Обращайся к ней по имени. "
        "Что мешает ей в жизни, какие уроки она должна пройти, как освободиться от кармических блоков. "
        "Пиши с пониманием и глубиной."
    ),
    "career": (
        "Опиши идеальный карьерный путь для {name} с числом судьбы {number}, рождённой {date}. "
        "Обращайся к ней по имени. "
        "Какие профессии подходят, в чём её сильные стороны на работе, как достичь успеха. "
        "Пиши конкретно и вдохновляюще."
    ),
    "money": (
        "Раскрой денежный код {name} с числом судьбы {number}, рождённой {date}. "
        "Обращайся к ней по имени. "
        "Какие отношения с деньгами заложены в числах, как активировать денежный поток, "
        "какие блоки мешают финансовому успеху. Пиши практично и вдохновляюще."
    ),
    "days": (
        "Составь разбор сильных и слабых дней месяца для {name} с числом судьбы {number}, "
        "рождённой {date}. Обращайся к ней по имени. "
        "Какие числа месяца самые благоприятные для важных дел, любви, финансов, "
        "а какие лучше провести спокойно. Пиши структурированно и понятно."
    ),
    "ex": (
        "Сделай нумерологический анализ — вернётся ли бывший к {name} с числом судьбы {number}, "
        "рождённой {date}. Обращайся к ней по имени. "
        "Опиши энергетику их связи, есть ли шанс на воссоединение, что нужно сделать или отпустить. "
        "Пиши с теплом и пониманием."
    ),
    "cold": (
        "Объясни нумерологически почему партнёр охладел к {name} с числом судьбы {number}, "
        "рождённой {date}. Обращайся к ней по имени. "
        "Какие числовые несовместимости могли привести к этому, что происходит на энергетическом уровне, "
        "как изменить ситуацию. Пиши тепло и честно."
    ),
    "toxic": (
        "Проанализируй нумерологически является ли связь токсичной или кармической для {name} "
        "с числом судьбы {number}, рождённой {date}. Обращайся к ней по имени. "
        "Опиши признаки токсичности в числах, кармические уроки этих отношений, как освободиться. "
        "Пиши глубоко и с пониманием."
    ),
    "lonely": (
        "Объясни нумерологически почему {name} с числом судьбы {number}, рождённая {date}, "
        "чувствует себя одинокой. Обращайся к ней по имени. "
        "Какие числовые паттерны создают одиночество, как изменить энергетику и привлечь нужных людей. "
        "Пиши с теплом и поддержкой."
    ),
    "breakup": (
        "Сделай нумерологический разбор после расставания для {name} с числом судьбы {number}, "
        "рождённой {date}. Обращайся к ней по имени. "
        "Объясни почему это произошло с точки зрения чисел, какие уроки несёт это расставание, "
        "что ждёт впереди в личной жизни. Пиши с теплом и надеждой."
    ),
}

# Заголовки разборов
TITLES = {
    "free":          "💫 Твоё число судьбы",
    "matrix":        "🔮 Матрица судьбы",
    "finance":       "💹 Финансовый прогноз",
    "wealth_blocks": "🚧 Блоки богатства",
    "freedom_path":  "🗺 Путь к свободе",
    "calling":       "🌠 Призвание",
    "promotion":     "📈 Повышение и карьерный рост",
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
    "days":          "🌙 Сильные и слабые дни месяца",
    "ex":            "💔 Вернётся ли бывший",
    "cold":          "❄️ Почему он охладел",
    "toxic":         "☠️ Токсичная или кармическая связь",
    "lonely":        "😔 Почему ты одинока",
    "breakup":       "💔 Разбор после расставания",
}

# Разборы с ценой 49 звёзд
PAID_RAZBORY = {
    "matrix":        "🔮 Матрица судьбы",
    "finance":       "💹 Финансовый прогноз",
    "wealth_blocks": "🚧 Блоки богатства",
    "freedom_path":  "🗺 Путь к свободе",
    "calling":       "🌠 Призвание",
    "promotion":     "📈 Повышение",
    "own_business":  "🏢 Свой бизнес",
    "hidden_talents":"✨ Скрытые таланты",
    "main_fear":     "😨 Главный страх",
    "forecast_2026": "🗓 Прогноз 2026",
    "strong_weak":   "⚖️ Сильная/слабая сторона",
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
    "toxic":         "☠️ Токсичная связь",
    "lonely":        "😔 Почему ты одинока",
    "breakup":       "💔 Разбор после расставания",
}

# ─── DB ─────────────────────────────────────────────────────────────────────────
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
    # Добавляем колонку first_name если её нет (для существующих баз)
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
    purchased = json.dumps(user['purchased'])
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
        purchased,
        user.get('waiting'),
        user['review_left'],
        user_id
    )

# ─── FSM ────────────────────────────────────────────────────────────────────────
class Form(StatesGroup):
    waiting_name        = State()   # онбординг: имя
    waiting_birth_date  = State()   # онбординг: дата рождения после имени
    waiting_date        = State()   # ввод даты для платного разбора
    waiting_second_date = State()   # ввод двух дат (compat)
    waiting_review      = State()   # отзыв

# ─── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ─────────────────────────────────────────────────────
def calculate_destiny(date_str: str) -> int:
    digits = [int(d) for d in date_str if d.isdigit()]
    total  = sum(digits)
    while total > 9 and total not in (11, 22, 33):
        total = sum(int(d) for d in str(total))
    return total

def is_valid_date(date_str: str) -> bool:
    try:
        datetime.strptime(date_str, "%d.%m.%Y")
        return True
    except ValueError:
        return False

# Защита от иностранных слов
FOREIGN_PATTERN = re.compile(r'[a-zA-ZÀ-ÿ]{3,}')

def strip_foreign_words(text: str) -> str:
    """Удаляет слова из латиницы длиннее 2 символов (оставляет цифры, эмодзи, знаки)."""
    def replace(match):
        word = match.group()
        # Разрешаем часто встречающиеся аббревиатуры/числа контекст — лучше убрать всё
        return ''
    cleaned = FOREIGN_PATTERN.sub(replace, text)
    # Убираем двойные пробелы
    cleaned = re.sub(r' {2,}', ' ', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()

async def check_subscription(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False

async def ask_ai(prompt: str) -> str:
    models = [
        "llama-3.3-70b-versatile",
        "gemma2-9b-it",
        "llama-3.1-8b-instant",
    ]
    url     = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json",
    }
    system_prompt = (
        "Ты — Ева, тёплый и мудрый нумеролог. Общаешься как близкая подруга, "
        "которая глубоко разбирается в нумерологии. "
        "ВАЖНО: пишешь ИСКЛЮЧИТЕЛЬНО на русском языке. "
        "Ни одного слова на английском или любом другом языке — даже терминов. "
        "Все иностранные термины заменяй русскими аналогами. "
        "Пишешь красиво, с эмодзи, атмосферно. Обращаешься на ты. "
        "Ответы длинные, подробные, эмоциональные — создающие ощущение, "
        "что написано именно про этого человека. "
        "Минимум 400 слов. Используй абзацы и структуру. "
        "Заканчивай ответ полным предложением, никогда не обрывай на середине."
    )
    last_error = None
    for model in models:
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
                result = response.json()
                raw    = result["choices"][0]["message"]["content"]
                return strip_foreign_words(raw)
        except Exception as e:
            last_error = e
            logging.warning(f"Model {model} failed: {e}, trying next...")
    raise last_error

def build_prompt(key: str, **kwargs) -> str:
    template = PROMPTS.get(key, "")
    return template.format(**kwargs)

# ─── КЛАВИАТУРЫ ──────────────────────────────────────────────────────────────────
def check_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я подписалась!", callback_data="check_sub")],
    ])

def main_menu(user=None) -> InlineKeyboardMarkup:
    buttons = []
    if user and not user.get("free_used"):
        buttons.append([InlineKeyboardButton(text="💫 Мой бесплатный разбор", callback_data="free")])

    # Блок: судьба и личность
    buttons.append([InlineKeyboardButton(text="── Судьба и личность ──", callback_data="noop")])
    buttons.extend([
        [InlineKeyboardButton(text="🔮 Матрица судьбы — 49 ⭐",             callback_data="buy_matrix")],
        [InlineKeyboardButton(text="🌟 Предназначение и миссия — 49 ⭐",    callback_data="buy_mission")],
        [InlineKeyboardButton(text="✨ Скрытые таланты — 49 ⭐",            callback_data="buy_hidden_talents")],
        [InlineKeyboardButton(text="⚖️ Сильная/слабая сторона — 49 ⭐",    callback_data="buy_strong_weak")],
        [InlineKeyboardButton(text="😨 Главный страх — 49 ⭐",              callback_data="buy_main_fear")],
        [InlineKeyboardButton(text="🔴 Кармический долг — 49 ⭐",           callback_data="buy_karma")],
        [InlineKeyboardButton(text="🗓 Прогноз на 2026 год — 49 ⭐",        callback_data="buy_forecast_2026")],
    ])

    # Блок: деньги и карьера
    buttons.append([InlineKeyboardButton(text="── Деньги и карьера ──", callback_data="noop")])
    buttons.extend([
        [InlineKeyboardButton(text="💹 Финансовый прогноз — 49 ⭐",         callback_data="buy_finance")],
        [InlineKeyboardButton(text="🚧 Блоки богатства — 49 ⭐",            callback_data="buy_wealth_blocks")],
        [InlineKeyboardButton(text="🗺 Путь к финансовой свободе — 49 ⭐",  callback_data="buy_freedom_path")],
        [InlineKeyboardButton(text="🌠 Призвание — 49 ⭐",                  callback_data="buy_calling")],
        [InlineKeyboardButton(text="📈 Повышение — 49 ⭐",                  callback_data="buy_promotion")],
        [InlineKeyboardButton(text="🏢 Свой бизнес — 49 ⭐",               callback_data="buy_own_business")],
        [InlineKeyboardButton(text="💼 Карьерный путь — 49 ⭐",             callback_data="buy_career")],
        [InlineKeyboardButton(text="💰 Денежный код — 49 ⭐",               callback_data="buy_money")],
        [InlineKeyboardButton(text="🌙 Сильные и слабые дни — 49 ⭐",      callback_data="buy_days")],
    ])

    # Блок: любовь и отношения
    buttons.append([InlineKeyboardButton(text="── Любовь и отношения ──", callback_data="noop")])
    buttons.extend([
        [InlineKeyboardButton(text="💑 Совместимость двух людей — 49 ⭐",   callback_data="buy_compat")],
        [InlineKeyboardButton(text="💘 Когда встретишь того самого — 49 ⭐",callback_data="buy_when")],
        [InlineKeyboardButton(text="💍 Портрет идеального партнёра — 49 ⭐",callback_data="buy_portrait")],
        [InlineKeyboardButton(text="💔 Почему не везёт в любви — 49 ⭐",    callback_data="buy_unlucky")],
        [InlineKeyboardButton(text="💔 Вернётся ли бывший — 49 ⭐",         callback_data="buy_ex")],
        [InlineKeyboardButton(text="❄️ Почему он охладел — 49 ⭐",          callback_data="buy_cold")],
        [InlineKeyboardButton(text="☠️ Токсичная связь — 49 ⭐",            callback_data="buy_toxic")],
        [InlineKeyboardButton(text="😔 Почему ты одинока — 49 ⭐",          callback_data="buy_lonely")],
        [InlineKeyboardButton(text="💔 Разбор после расставания — 49 ⭐",   callback_data="buy_breakup")],
    ])

    # Личный разбор за рубли
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

# ─── ОНБОРДИНГ ───────────────────────────────────────────────────────────────────
@dp.message(Command("start"), StateFilter("*"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)

    # Сохраняем имя из Telegram если ещё нет
    if not user.get("first_name") and message.from_user.first_name:
        user["first_name"] = message.from_user.first_name
        await save_user(message.from_user.id, user)

    # Шаг 1: проверка подписки
    if not user["subscribed_channel"]:
        is_sub = await check_subscription(message.from_user.id)
        if is_sub:
            user["subscribed_channel"] = True
            await save_user(message.from_user.id, user)

    if not user["subscribed_channel"]:
        await message.answer(
            "🔮 Привет! Я Ева — твой личный нумеролог.\n\n"
            "Числа хранят тайны твоей судьбы, любви и предназначения. "
            "Я помогу тебе их раскрыть.\n\n"
            f"✨ Подпишись на канал {CHANNEL} — и получи бесплатный разбор числа судьбы!\n\n"
            "После подписки нажми кнопку ниже 👇",
            reply_markup=check_menu()
        )
        return

    # Уже подписана — проверяем онбординг
    if not user["free_used"]:
        await _start_onboarding(message,state,user)
        return

    await message.answer("🔮 Выбери свой разбор 👇", reply_markup=main_menu(user))

async def _start_onboarding(message: Message, user: dict):
    """Запускает онбординг: просим имя."""
    name = user.get("first_name") or ""
    if name:
        await message.answer(
            f"✨ Привет, {name}! Давай познакомимся поближе.\n\n"
            "Как мне тебя называть? Введи своё имя 👇"
        )
    else:
        await message.answer(
            "✨ Привет! Давай познакомимся поближе.\n\n"
            "Как мне тебя называть? Введи своё имя 👇"
        )
    await Form.waiting_name.set() 

@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    is_sub = await check_subscription(callback.from_user.id)
    if not is_sub:
        await callback.answer("❌ Ты ещё не подписалась!", show_alert=True)
        return
    user["subscribed_channel"] = True
    await save_user(callback.from_user.id, user)
    await callback.answer()

    if user["free_used"]:
        await callback.message.answer("✅ Ты уже подписана! Выбери разбор:", reply_markup=main_menu(user))
        return

    # Запускаем онбординг
    await callback.message.answer(
        "✅ Отлично! Теперь введи своё имя — как мне тебя называть? 👇"
    )
    await state.set_state(Form.waiting_name)

# Шаг онбординга 1: получаем имя
@dp.message(Form.waiting_name)
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
        "Теперь введи свою дату рождения в формате ДД.ММ.ГГГГ\n"
        "Например: 15.03.1995"
    )
    await state.set_state(Form.waiting_birth_date)

# Шаг онбординга 2: получаем дату → бесплатный разбор
@dp.message(Form.waiting_birth_date)
async def handle_birth_date(message: Message, state: FSMContext):
    text = message.text.strip()
    if not is_valid_date(text):
        await message.answer("❌ Неверная дата. Введи в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
        return
    user = await get_user(message.from_user.id)
    number = calculate_destiny(text)
    user["birth_date"]     = text
    user["destiny_number"] = number
    user["free_used"]      = True
    user["waiting"]        = None
    await save_user(message.from_user.id, user)

    name = user.get("first_name") or "дорогая"
    await message.answer(f"⏳ Составляю твой разбор, {name}... Подожди немного ✨")
    try:
        prompt = build_prompt("free", name=name, number=number, date=text)
        answer = await ask_ai(prompt)
        await message.answer(f"💫 Число судьбы {name}: {number}\n\n{answer}")
        await message.answer(
            "✨ Это было только начало!\n\n"
            "Выбери платный разбор и узнай больше о своей судьбе, деньгах и любви 🔮",
            reply_markup=main_menu(user)
        )
    except Exception as e:
        logging.error(f"Onboarding razor error: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка, попробуй ещё раз /start")
    await state.clear()

# ─── КОМАНДЫ ─────────────────────────────────────────────────────────────────────
@dp.message(Command("menu"), StateFilter("*"))
async def menu_cmd(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    await message.answer("🔮 Выбери разбор:", reply_markup=main_menu(user))

@dp.message(Command("admin"), StateFilter("*"))
async def admin_panel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    total       = await db_pool.fetchval('SELECT COUNT(*) FROM users')
    free_used   = await db_pool.fetchval('SELECT COUNT(*) FROM users WHERE free_used = TRUE')
    reviews     = await db_pool.fetchval('SELECT COUNT(*) FROM users WHERE review_left = TRUE')
    rows        = await db_pool.fetch('SELECT purchased FROM users')
    total_purch = 0
    razbory_cnt = {}
    bought      = 0
    for row in rows:
        p = json.loads(row['purchased'])
        if p:
            bought += 1
            total_purch += len(p)
            for r in p:
                razbory_cnt[r] = razbory_cnt.get(r, 0) + 1
    top      = sorted(razbory_cnt.items(), key=lambda x: x[1], reverse=True)
    top_text = "\n".join([f"  {k}: {v}" for k, v in top[:5]]) if top else "  нет покупок"
    await message.answer(
        f"📊 Статистика бота Ева\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"💫 Получили бесплатный разбор: {free_used}\n"
        f"💳 Купили хотя бы один разбор: {bought}\n"
        f"🛒 Всего покупок: {total_purch}\n"
        f"⭐ Оставили отзыв: {reviews}\n\n"
        f"🏆 Топ разборов:\n{top_text}"
    )

# ─── ПОКУПКИ ─────────────────────────────────────────────────────────────────────
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
    # Запускаем онбординг
    await callback.message.answer(
        "✨ Введи своё имя — как мне тебя называть? 👇"
    )
    await state.set_state(Form.waiting_name)
    await callback.answer()

async def send_invoice(chat_id, title, description, payload, amount):
    prices = [LabeledPrice(label=title, amount=amount)]
    await bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        currency="XTR",
        prices=prices,
    )

@dp.callback_query(F.data.startswith("buy_"))
async def buy_handler(callback: CallbackQuery, state: FSMContext):
    key  = callback.data.replace("buy_", "")
    user = await get_user(callback.from_user.id)

    if key in user["purchased"]:
        # Уже куплено — сразу запрашиваем дату
        user["waiting"] = key
        await save_user(callback.from_user.id, user)
        if key == "compat":
            await callback.message.answer(
                "💑 Введи две даты рождения через запятую:\nНапример: 15.03.1995, 22.07.1998"
            )
            await state.set_state(Form.waiting_second_date)
        else:
            await callback.message.answer(
                "📅 Введи свою дату рождения в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995"
            )
            await state.set_state(Form.waiting_date)
        await callback.answer()
        return

    if key in PAID_RAZBORY:
        title = PAID_RAZBORY[key]
        await send_invoice(callback.message.chat.id, title, TITLES.get(key, title), key, 49)
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
            "✅ Оплата прошла! Введи две даты рождения через запятую:\n"
            "Например: 15.03.1995, 22.07.1998"
        )
        await state.set_state(Form.waiting_second_date)
    else:
        await message.answer(
            "✅ Оплата прошла! Введи свою дату рождения в формате ДД.ММ.ГГГГ\n"
            "Например: 15.03.1995"
        )
        await state.set_state(Form.waiting_date)

# ─── ОБРАБОТКА ДАТ ───────────────────────────────────────────────────────────────
@dp.message(Form.waiting_second_date)
async def handle_two_dates(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    text = message.text.strip()
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
        n1 = calculate_destiny(parts[0])
        n2 = calculate_destiny(parts[1])
        prompt = build_prompt("compat", name=name, date1=parts[0], n1=n1, date2=parts[1], n2=n2)
        answer = await ask_ai(prompt)
        await message.answer(f"💑 Совместимость\n\n{answer}")
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
    number  = calculate_destiny(text)
    waiting = user.get("waiting")
    name    = user.get("first_name") or "дорогая"

    if not waiting:
        await message.answer("Выбери разбор из меню 👇", reply_markup=main_menu(user))
        await state.clear()
        return

    # Если дата рождения не сохранена — сохраняем
    if not user.get("birth_date"):
        user["birth_date"]     = text
        user["destiny_number"] = number
        await save_user(message.from_user.id, user)

    await message.answer(f"⏳ Ева составляет разбор для {name}... Подожди немного ✨")
    try:
        prompt = build_prompt(waiting, name=name, number=number, date=text)
        answer = await ask_ai(prompt)
        title  = TITLES.get(waiting, "🔮 Разбор")
        await message.answer(f"{title}\n\n{answer}")
        await message.answer("✨ Понравился разбор?", reply_markup=review_menu())
    except Exception as e:
        logging.error(f"Date handler error for {waiting}: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка, попробуй ещё раз.")
    await state.clear()

# ─── ОТЗЫВЫ ──────────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "leave_review")
async def leave_review(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    if user.get("review_left"):
        await callback.answer("Ты уже оставила отзыв, спасибо! 💫", show_alert=True)
        return
    if not user.get("purchased"):
        await callback.answer("Отзыв можно оставить только после покупки разбора!", show_alert=True)
        return
    await callback.message.answer("💬 Напиши свой отзыв — опубликую его в канале отзывов!")
    await state.set_state(Form.waiting_review)
    await callback.answer()

@dp.message(Form.waiting_review)
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

# ─── РАССЫЛКИ ────────────────────────────────────────────────────────────────────
async def send_daily_horoscope():
    while True:
        now    = datetime.now()
        target = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            rows = await db_pool.fetch(
                'SELECT user_id, first_name, birth_date, destiny_number FROM users '
                'WHERE birth_date IS NOT NULL AND destiny_number IS NOT NULL'
            )
            today = date.today().strftime("%d.%m.%Y")
            for row in rows:
                try:
                    number = row['destiny_number']
                    name   = row['first_name'] or "дорогая"
                    prompt = (
                        f"Составь короткий личный прогноз на сегодня {today} для {name} "
                        f"с числом судьбы {number}. Обращайся к ней по имени. "
                        "Что принесёт этот день в любви, делах и энергии. "
                        "Пиши тепло, коротко 150-200 слов, с эмодзи. Заканчивай полным предложением."
                    )
                    horoscope = await ask_ai(prompt)
                    await bot.send_message(
                        row['user_id'],
                        f"🌅 Доброе утро, {name}! Твой прогноз на сегодня:\n\n{horoscope}\n\n🔮 /menu"
                    )
                except Exception as e:
                    logging.error(f"Horoscope error for {row['user_id']}: {e}")
        except Exception as e:
            logging.error(f"Horoscope batch error: {e}")

async def send_daily_tip():
    while True:
        now    = datetime.now()
        target = now.replace(hour=17, minute=0, second=0, microsecond=0)
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
                        f"{name}, знаешь ли ты что число судьбы {number} даёт тебе особый дар? Сегодня хороший день чтобы его раскрыть ✨",
                        f"Числа говорят — сегодня твоя энергия числа {number} особенно сильна 🔮 Используй это, {name}!",
                        f"Маленький секрет числа {number}: ты притягиваешь то о чём думаешь чаще всего, {name} 💫",
                        f"Число судьбы {number} — это не случайность. Это твой уникальный код вселенной 🌟",
                        f"Сегодняшний вечер идеален чтобы прислушаться к своей интуиции — число {number} усиливает её 🌙",
                    ]
                    await bot.send_message(
                        row['user_id'],
                        f"💜 Ева напоминает:\n\n{random.choice(tips)}\n\n🔮 /menu"
                    )
                except Exception as e:
                    logging.error(f"Tip error for {row['user_id']}: {e}")
        except Exception as e:
            logging.error(f"Tip batch error: {e}")

async def send_daily_channel_post():
    while True:
        now    = datetime.now()
        target = now.replace(hour=10, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            today   = date.today().strftime("%d.%m.%Y")
            day_num = date.today().day
            prompt  = (
                f"Напиши интересный нумерологический пост для Телеграм канала на сегодня {today}. "
                f"Число дня: {day_num}. "
                "Тема: что значит это число, какая энергия сегодня, советы на день. "
                "Пиши красиво, с эмодзи, атмосферно. 150-200 слов. ТОЛЬКО на русском языке."
            )
            post = await ask_ai(prompt)
            await bot.send_message(
                CHANNEL,
                f"🔮 Нумерология дня\n\n{post}\n\n✨ Узнай свой личный разбор @nnumerology_bot"
            )
        except Exception as e:
            logging.error(f"Channel post error: {e}")

# ─── WEB HEALTHCHECK ─────────────────────────────────────────────────────────────
async def healthcheck(request):
    return web.Response(text="OK")

async def run_web():
    app = web.Application()
    app.router.add_get("/", healthcheck)
    port   = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# ─── MAIN ────────────────────────────────────────────────────────────────────────
async def main():
    await init_db()
    asyncio.create_task(run_web())
    asyncio.create_task(send_daily_horoscope())
    asyncio.create_task(send_daily_tip())
    asyncio.create_task(send_daily_channel_post())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
