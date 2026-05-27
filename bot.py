import os
import logging
import asyncio
import json
import httpx
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
if not BOT_TOKEN or not GROQ_API_KEY:
    raise EnvironmentError("BOT_TOKEN и GROQ_API_KEY должны быть установлены!")

CHANNEL = "@eva_numerologg"
REVIEWS_CHANNEL = "@eva_numerolog_otz"
ADMIN_ID = 5854618444

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

USERS_FILE = "users.json"

def load_users():
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_users(users_dict):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users_dict, f, ensure_ascii=False, indent=2)

users = load_users()

class Form(StatesGroup):
    waiting_date = State()
    waiting_second_date = State()
    waiting_review = State()

def get_user(user_id):
    uid = str(user_id)
    if uid not in users:
        users[uid] = {
            "free_used": False,
            "subscribed_channel": False,
            "birth_date": None,
            "destiny_number": None,
            "purchased": [],
            "waiting": None,
            "review_left": False
        }
    if user_id == ADMIN_ID:
        users[uid]["free_used"] = True
        users[uid]["subscribed_channel"] = True
        for r in ["compat", "when", "portrait", "unlucky", "matrix", "mission", "karma", "career", "money", "days"]:
            if r not in users[uid]["purchased"]:
                users[uid]["purchased"].append(r)
    save_users(users)
    return users[uid]

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
        "Пишешь ИСКЛЮЧИТЕЛЬНО на русском языке — никаких иностранных слов, никаких английских, испанских или китайских слов. "
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

async def send_invoice(chat_id, title, description, payload, amount):
    prices = [LabeledPrice(label=title, amount=amount)]
    
    try:
        await bot.send_invoice(
            chat_id=chat_id,
            title=title,
            description=description,
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=prices
        )
    except Exception as e:
        error_text = str(e).lower()
        if "this payment method is not available for the selected product" in error_text or "payment" in error_text:
            await bot.send_message(
                chat_id=chat_id,
                text="⭐ Для покупки разбора нужно 49 звёзд.\n\n"
                     "Купить звёзды можно прямо в Telegram:\n"
                     "→ Зайди в <b>Настройки → Звёзды</b>\n"
                     "→ Купи нужное количество\n\n"
                     "После покупки просто вернись в бот и нажми на нужный разбор снова.",
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text="❌ Произошла ошибка при оплате. Попробуй позже."
            )
            logging.error(f"Payment error: {e}")

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
}

async def send_daily_horoscope():
    while True:
        now = datetime.now()
        target = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        wait = (target - now).total_seconds()
        await asyncio.sleep(wait)
        user_ids = list(users.keys())
        for uid in user_ids:
            user = users.get(uid)
            if not user:
                continue
            if user.get("birth_date") and user.get("destiny_number"):
                try:
                    number = user["destiny_number"]
                    today = date.today().strftime("%d.%m.%Y")
                    prompt = f"Составь короткий личный прогноз на сегодня {today} для человека с числом судьбы {number}. Что принесёт этот день в любви, делах и энергии. Пиши тепло, коротко 150-200 слов, с эмодзи. Заканчивай полным предложением."
                    horoscope = await ask_ai(prompt)
                    await bot.send_message(
                        int(uid),
                        f"🌅 Доброе утро! Твой прогноз на сегодня:\n\n{horoscope}\n\n🔮 Хочешь больше? /menu"
                    )
                except Exception as e:
                    logging.error(f"Horoscope error for {uid}: {e}")

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
    asyncio.create_task(run_web())
    asyncio.create_task(send_daily_horoscope())
    asyncio.create_task(send_daily_channel_post())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())