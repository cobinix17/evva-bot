import os
import logging
import asyncio
import json
import httpx
import random
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

CHANNEL = "@eva_numerologg"
REVIEWS_CHANNEL = "@eva_numerolog_otz"
ADMIN_ID = 5854618444

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ===================== USERS.JSON =====================
DATA_FILE = "users.json"
users_data = {}

def load_users():
    global users_data
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                users_data = json.load(f)
    except:
        users_data = {}

def save_users():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(users_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Save error: {e}")

load_users()

async def get_user(user_id: int) -> dict:
    uid = str(user_id)
    if uid not in users_data:
        users_data[uid] = {
            "name": None,
            "birth_date": None,
            "destiny_number": None,
            "free_used": False,
            "subscribed_channel": False,
            "purchased": [],
            "review_left": False
        }
        if user_id == ADMIN_ID:
            users_data[uid]["free_used"] = True
            users_data[uid]["subscribed_channel"] = True
        save_users()
    return users_data[uid]

async def save_user(user_id: int, user: dict):
    users_data[str(user_id)] = user
    save_users()

# ===================== PROMPTS =====================
PROMPTS = {
    "free_matrix": """Ты — Ева, тёплый и мудрый нумеролог. Сделай очень подробный разбор Матрицы Судьбы для {name} (дата рождения {date}, число судьбы {number}). 
Пиши тепло, эмоционально, как близкая подруга. Минимум 500 слов. Структура: характер, сильные стороны, кармические задачи, любовь, предназначение, рекомендации.""",

    "financial_forecast": """Финансовый прогноз на 2026 год и ближайшие годы для {name} ({date}).""",
    "blocks_wealth": """Разбери блоки богатства у {name} ({date}) и как их преодолеть.""",
    "path_freedom": """Путь к внутренней свободе и предназначению для {name} ({date}).""",
    "vocation": """Призвание, скрытые таланты и миссия {name} ({date}).""",
    "business": """Перспективы открытия и ведения своего бизнеса для {name} ({date}).""",
    "fear": """Главный страх {name} и как его трансформировать.""",
    "strong_weak": """Сильные и слабые стороны личности {name} ({date}).""",
    "promotion": """Возможности повышения по карьере и профессионального роста для {name} ({date}).""",
}

# ===================== HELPERS =====================
def clean_text(text: str) -> str:
    import re
    return re.sub(r'\b[a-zA-Z]{4,}\b', '[слово]', text)

async def ask_ai(prompt: str) -> str:
    models = ["llama-3.3-70b-versatile", "gemma2-9b-it", "llama-3.1-8b-instant"]
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    
    system_prompt = (
        "Ты — Ева, тёплый и мудрый нумеролог. "
        "Пишешь ТОЛЬКО на русском языке, никаких иностранных слов. "
        "Обращаешься по имени, тепло, эмоционально, как близкая подруга. "
        "Ответы длинные, подробные, от 450-600 слов."
    )
    
    for model in models:
        try:
            data = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 2000,
                "temperature": 0.75
            }
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(url, headers=headers, json=data)
                resp.raise_for_status()
                return clean_text(resp.json()["choices"][0]["message"]["content"])
        except:
            continue
    return "Извини, сейчас немного перегружено. Попробуй через минуту ✨"

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
    except:
        return False

# ===================== KEYBOARDS =====================
def main_menu(user=None):
    buttons = []
    if user and not user.get("free_used"):
        buttons.append([InlineKeyboardButton(text="💫 Бесплатная Матрица Судьбы", callback_data="free")])

    # Старые + Новые разборы
    items = [
        ("💑 Совместимость двух людей — 49⭐", "compat"),
        ("💘 Когда встретишь того самого — 49⭐", "when"),
        ("💍 Портрет идеального партнёра — 49⭐", "portrait"),
        ("💔 Почему не везёт в любви — 49⭐", "unlucky"),
        ("🔮 Матрица судьбы — 49⭐", "matrix"),
        ("🌟 Предназначение и миссия — 49⭐", "mission"),
        ("💰 Финансовый прогноз 2026 — 49⭐", "financial"),
        ("🛡️ Блоки богатства — 49⭐", "blocks"),
        ("🕊️ Путь к свободе — 49⭐", "freedom"),
        ("🌟 Призвание и таланты — 49⭐", "vocation"),
        ("🚀 Свой бизнес — 49⭐", "business"),
        ("😨 Главный страх — 49⭐", "fear"),
        ("⚖️ Сильные и слабые стороны — 49⭐", "strongweak"),
        ("📈 Повышение по карьере — 49⭐", "promotion"),
    ]
    
    for text, data in items:
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"buy_{data}")])
    
    buttons.append([InlineKeyboardButton(text="📞 Личный разбор у Евы", callback_data="personal")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def check_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я подписалась!", callback_data="check_sub")]
    ])

def review_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😍 Оставить отзыв", callback_data="leave_review")],
        [InlineKeyboardButton(text="🔮 Другие разборы", callback_data="show_menu")]
    ])

# ===================== STATES =====================
class Form(StatesGroup):
    waiting_name = State()
    waiting_date = State()
    waiting_second_date = State()
    waiting_review = State()

# ===================== HANDLERS =====================
@dp.message(Command("start"), StateFilter("*"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    
    if not user.get("subscribed_channel"):
        await message.answer(
            "🔮 Привет! Я Ева — твой личный нумеролог.\n\n"
            "Чтобы получить **бесплатный разбор Матрицы Судьбы**, подпишись на канал:",
            reply_markup=check_menu()
        )
    else:
        await message.answer("🔮 Рада тебя видеть! Выбери разбор 👇", reply_markup=main_menu(user))

@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    user["subscribed_channel"] = True
    await save_user(callback.from_user.id, user)
    
    await callback.message.answer("✅ Отлично! Как тебя зовут?")
    await state.set_state(Form.waiting_name)
    await callback.answer()

@dp.message(Form.waiting_name)
async def process_name(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    user["name"] = message.text.strip()[:30]
    await save_user(message.from_user.id, user)
    
    await message.answer(f"Приятно познакомиться, {user['name']}!\n\nНапиши дату рождения в формате ДД.ММ.ГГГГ\nПример: 15.03.1995")
    await state.set_state(Form.waiting_date)

@dp.message(Form.waiting_date)
async def process_date(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not is_valid_date(message.text):
        await message.answer("❌ Неверный формат даты. Пример: 15.03.1995")
        return

    date_str = message.text
    number = calculate_destiny(date_str)
    user["birth_date"] = date_str
    user["destiny_number"] = number
    
    waiting = user.get("waiting")
    name = user.get("name", "друг")

    if waiting == "free" or not waiting:
        user["free_used"] = True
        title = "💫 Твоя Матрица Судьбы"
        prompt = PROMPTS["free_matrix"].format(name=name, date=date_str, number=number)
    else:
        title = "🔮 Твой разбор"
        prompt = PROMPTS.get(waiting, PROMPTS["free_matrix"]).format(name=name, date=date_str)

    await save_user(message.from_user.id, user)
    await message.answer("⏳ Ева составляет твой разбор, пожалуйста подожди...")
    
    answer = await ask_ai(prompt)
    await message.answer(f"{title}\n\n{answer}")
    await message.answer("✨ Понравился разбор?", reply_markup=review_menu())
    await state.clear()

@dp.callback_query(F.data == "free")
async def free_handler(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    if user.get("free_used"):
        await callback.message.answer("Ты уже получила бесплатный разбор ✨", reply_markup=main_menu(user))
        await callback.answer()
        return
    user["waiting"] = "free"
    await save_user(callback.from_user.id, user)
    await callback.message.answer("Напиши дату рождения (ДД.ММ.ГГГГ)")
    await state.set_state(Form.waiting_date)
    await callback.answer()

@dp.callback_query(F.data.startswith("buy_"))
async def buy_handler(callback: CallbackQuery, state: FSMContext):
    key = callback.data.replace("buy_", "")
    user = await get_user(callback.from_user.id)
    user["waiting"] = key
    await save_user(callback.from_user.id, user)
    
    await callback.message.answer("Напиши дату рождения (ДД.ММ.ГГГГ)")
    await state.set_state(Form.waiting_date)
    await callback.answer()

@dp.callback_query(F.data == "personal")
async def personal_reading(callback: CallbackQuery):
    await callback.message.answer(
        "Для глубокого **личного разбора** напиши мне напрямую:\n"
        "@cifry.zhizni ✨"
    )
    await callback.answer()

@dp.callback_query(F.data == "leave_review")
async def leave_review(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    if user.get("review_left"):
        await callback.answer("Ты уже оставляла отзыв ❤️", show_alert=True)
        return
    await callback.message.answer("💬 Напиши свой отзыв:")
    await state.set_state(Form.waiting_review)
    await callback.answer()

@dp.message(Form.waiting_review)
async def handle_review(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    try:
        await bot.send_message(
            REVIEWS_CHANNEL, 
            f"⭐ Новый отзыв!\n\n{message.text}\n\n— {user.get('name', 'Аноним')}"
        )
        user["review_left"] = True
        await save_user(message.from_user.id, user)
        await message.answer("✅ Спасибо! Твой отзыв опубликован в канале 💫")
    except:
        await message.answer("✅ Спасибо за отзыв!")
    await state.clear()

@dp.callback_query(F.data == "show_menu")
async def show_menu(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.message.answer("🔮 Выбери разбор:", reply_markup=main_menu(user))
    await callback.answer()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())