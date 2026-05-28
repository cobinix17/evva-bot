import os
import re
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
    raise EnvironmentError("BOT_TOKEN \u0438 GROQ_API_KEY \u0434\u043e\u043b\u0436\u043d\u044b \u0431\u044b\u0442\u044c \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u044b!")

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

# ==================== \u041f\u041e\u0421\u0422\u041e\u0411\u0420\u0410\u0411\u041e\u0422\u041a\u0410 \u0422\u0415\u041a\u0421\u0422\u0410 ====================
# \u0421\u043b\u043e\u0432\u0430\u0440\u044c \u0437\u0430\u043c\u0435\u043d \u043b\u0430\u0442\u0438\u043d\u0441\u043a\u0438\u0445 \u0431\u0443\u043a\u0432 \u043d\u0430 \u043a\u0438\u0440\u0438\u043b\u043b\u0438\u0446\u0443 (\u043d\u0430 \u0441\u043b\u0443\u0447\u0430\u0439 \u0442\u0440\u0430\u043d\u0441\u043b\u0438\u0442\u0430)
LATIN_TO_CYRILLIC = {
    'a': '\u0430', 'e': '\u0435', 'o': '\u043e', 'p': '\u0440', 'c': '\u0441', 'x': '\u0445',
    'A': '\u0410', 'E': '\u0415', 'O': '\u041e', 'P': '\u0420', 'C': '\u0421', 'X': '\u0425',
    'B': '\u0412', 'H': '\u041d', 'K': '\u041a', 'M': '\u041c', 'T': '\u0422',
}

# \u0421\u0442\u043e\u043f-\u0441\u043b\u043e\u0432\u0430: \u0438\u043d\u043e\u0441\u0442\u0440\u0430\u043d\u043d\u044b\u0435 \u0441\u043b\u043e\u0432\u0430 \u043a\u043e\u0442\u043e\u0440\u044b\u0435 \u0438\u043d\u043e\u0433\u0434\u0430 \u043f\u0440\u043e\u0441\u043a\u0430\u043a\u0438\u0432\u0430\u044e\u0442
FOREIGN_STOPWORDS = [
    r'\band\b', r'\bthe\b', r'\bof\b', r'\bin\b', r'\bto\b', r'\ba\b',
    r'\bis\b', r'\bfor\b', r'\byou\b', r'\bwith\b', r'\bthis\b',
    r'\bthat\b', r'\bon\b', r'\bare\b', r'\bas\b', r'\bor\b',
    r'\bde\b', r'\bla\b', r'\bel\b', r'\bles\b', r'\bun\b',
    r'\bund\b', r'\bder\b', r'\bdie\b', r'\bdas\b',
    r'\b\u0438\b'  # \u043d\u0435 \u0442\u0440\u043e\u0433\u0430\u0435\u043c \u2014 \u044d\u0442\u043e \u0440\u0443\u0441\u0441\u043a\u0438\u0439 \u0441\u043e\u044e\u0437
]

def clean_foreign_text(text: str) -> str:
    """
    \u0423\u0431\u0438\u0440\u0430\u0435\u0442 \u0438\u0437 \u0442\u0435\u043a\u0441\u0442\u0430 \u0438\u043d\u043e\u0441\u0442\u0440\u0430\u043d\u043d\u044b\u0435 \u043b\u0430\u0442\u0438\u043d\u0441\u043a\u0438\u0435 \u0441\u043b\u043e\u0432\u0430 \u0438 \u0432\u0441\u0442\u0430\u0432\u043a\u0438.
    \u0421\u043e\u0445\u0440\u0430\u043d\u044f\u0435\u0442 \u044d\u043c\u043e\u0434\u0437\u0438, \u0446\u0438\u0444\u0440\u044b, \u0437\u043d\u0430\u043a\u0438 \u043f\u0440\u0435\u043f\u0438\u043d\u0430\u043d\u0438\u044f \u0438 \u0440\u0443\u0441\u0441\u043a\u0438\u0439 \u0442\u0435\u043a\u0441\u0442.
    """
    if not text:
        return text

    lines = text.split('\n')
    cleaned_lines = []

    for line in lines:
        # \u0423\u0431\u0438\u0440\u0430\u0435\u043c \u0446\u0435\u043b\u044b\u0435 \u0441\u043b\u043e\u0432\u0430 \u043d\u0430 \u043b\u0430\u0442\u0438\u043d\u0438\u0446\u0435 (\u043a\u0440\u043e\u043c\u0435 \u043e\u0434\u0438\u043d\u043e\u0447\u043d\u044b\u0445 \u0431\u0443\u043a\u0432-\u044d\u043c\u043e\u0434\u0437\u0438-\u0437\u0430\u043c\u0435\u043d)
        # \u041f\u0430\u0442\u0442\u0435\u0440\u043d: \u0441\u043b\u043e\u0432\u043e \u0438\u0437 \u043b\u0430\u0442\u0438\u043d\u0441\u043a\u0438\u0445 \u0431\u0443\u043a\u0432 \u0434\u043b\u0438\u043d\u043e\u0439 2+ \u0441\u0438\u043c\u0432\u043e\u043b\u0430
        line = re.sub(r'\b[a-zA-Z]{2,}\b', _replace_latin_word, line)
        cleaned_lines.append(line)

    result = '\n'.join(cleaned_lines)

    # \u0423\u0431\u0438\u0440\u0430\u0435\u043c \u043b\u0438\u0448\u043d\u0438\u0435 \u043f\u0440\u043e\u0431\u0435\u043b\u044b \u043a\u043e\u0442\u043e\u0440\u044b\u0435 \u043c\u043e\u0433\u043b\u0438 \u043e\u0441\u0442\u0430\u0442\u044c\u0441\u044f
    result = re.sub(r' {2,}', ' ', result)
    result = re.sub(r' ([,\.!?:;])', r'\1', result)

    return result.strip()

def _replace_latin_word(match):
    """
    \u0414\u043b\u044f \u043a\u0430\u0436\u0434\u043e\u0433\u043e \u043b\u0430\u0442\u0438\u043d\u0441\u043a\u043e\u0433\u043e \u0441\u043b\u043e\u0432\u0430 \u043f\u044b\u0442\u0430\u0435\u0442\u0441\u044f \u043d\u0430\u0439\u0442\u0438 \u0440\u0443\u0441\u0441\u043a\u0438\u0439 \u044d\u043a\u0432\u0438\u0432\u0430\u043b\u0435\u043d\u0442.
    \u0415\u0441\u043b\u0438 \u043d\u0435 \u0437\u043d\u0430\u0435\u0442 \u2014 \u043f\u0440\u043e\u0441\u0442\u043e \u0443\u0434\u0430\u043b\u044f\u0435\u0442 \u0441\u043b\u043e\u0432\u043e.
    """
    word = match.group(0)

    # \u0418\u0441\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u044f: \u043e\u0441\u0442\u0430\u0432\u043b\u044f\u0435\u043c \u043a\u0430\u043a \u0435\u0441\u0442\u044c (\u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u0438\u0435 \u043e\u0431\u043e\u0437\u043d\u0430\u0447\u0435\u043d\u0438\u044f)
    exceptions = {'AI', 'OK', 'VIP', 'SMS', 'PIN', 'ID', 'URL', 'PDF'}
    if word.upper() in exceptions:
        return word

    # \u0418\u0437\u0432\u0435\u0441\u0442\u043d\u044b\u0435 \u0437\u0430\u043c\u0435\u043d\u044b
    known_replacements = {
        'love': '\u043b\u044e\u0431\u043e\u0432\u044c',
        'Love': '\u041b\u044e\u0431\u043e\u0432\u044c',
        'LOVE': '\u041b\u042e\u0411\u041e\u0412\u042c',
        'life': '\u0436\u0438\u0437\u043d\u044c',
        'Life': '\u0416\u0438\u0437\u043d\u044c',
        'soul': '\u0434\u0443\u0448\u0430',
        'Soul': '\u0414\u0443\u0448\u0430',
        'karma': '\u043a\u0430\u0440\u043c\u0430',
        'Karma': '\u041a\u0430\u0440\u043c\u0430',
        'energy': '\u044d\u043d\u0435\u0440\u0433\u0438\u044f',
        'Energy': '\u042d\u043d\u0435\u0440\u0433\u0438\u044f',
        'destiny': '\u0441\u0443\u0434\u044c\u0431\u0430',
        'Destiny': '\u0421\u0443\u0434\u044c\u0431\u0430',
        'yes': '\u0434\u0430',
        'no': '\u043d\u0435\u0442',
        'very': '\u043e\u0447\u0435\u043d\u044c',
        'good': '\u0445\u043e\u0440\u043e\u0448\u043e',
        'bad': '\u043f\u043b\u043e\u0445\u043e',
        'new': '\u043d\u043e\u0432\u044b\u0439',
        'time': '\u0432\u0440\u0435\u043c\u044f',
        'day': '\u0434\u0435\u043d\u044c',
        'year': '\u0433\u043e\u0434',
        'path': '\u043f\u0443\u0442\u044c',
        'way': '\u043f\u0443\u0442\u044c',
        'light': '\u0441\u0432\u0435\u0442',
        'heart': '\u0441\u0435\u0440\u0434\u0446\u0435',
        'true': '\u0438\u0441\u0442\u0438\u043d\u043d\u044b\u0439',
        'power': '\u0441\u0438\u043b\u0430',
        'mind': '\u0440\u0430\u0437\u0443\u043c',
        'spirit': '\u0434\u0443\u0445',
        'world': '\u043c\u0438\u0440',
        'number': '\u0447\u0438\u0441\u043b\u043e',
        'Number': '\u0427\u0438\u0441\u043b\u043e',
        'strong': '\u0441\u0438\u043b\u044c\u043d\u044b\u0439',
        'deep': '\u0433\u043b\u0443\u0431\u043e\u043a\u0438\u0439',
        'free': '\u0441\u0432\u043e\u0431\u043e\u0434\u043d\u044b\u0439',
        'money': '\u0434\u0435\u043d\u044c\u0433\u0438',
        'work': '\u0440\u0430\u0431\u043e\u0442\u0430',
        'happy': '\u0441\u0447\u0430\u0441\u0442\u043b\u0438\u0432\u044b\u0439',
        'happiness': '\u0441\u0447\u0430\u0441\u0442\u044c\u0435',
    }

    if word in known_replacements:
        return known_replacements[word]

    # \u0415\u0441\u043b\u0438 \u0441\u043b\u043e\u0432\u043e \u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u043e \u2014 \u0443\u0434\u0430\u043b\u044f\u0435\u043c \u0435\u0433\u043e
    return ''

# ==============================================================

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
        "\u0422\u044b \u2014 \u0415\u0432\u0430, \u0442\u0451\u043f\u043b\u044b\u0439 \u0438 \u043c\u0443\u0434\u0440\u044b\u0439 \u043d\u0443\u043c\u0435\u0440\u043e\u043b\u043e\u0433. \u041e\u0431\u0449\u0430\u0435\u0448\u044c\u0441\u044f \u043a\u0430\u043a \u0431\u043b\u0438\u0437\u043a\u0430\u044f \u043f\u043e\u0434\u0440\u0443\u0433\u0430 \u043a\u043e\u0442\u043e\u0440\u0430\u044f \u0433\u043b\u0443\u0431\u043e\u043a\u043e \u0440\u0430\u0437\u0431\u0438\u0440\u0430\u0435\u0442\u0441\u044f \u0432 \u043d\u0443\u043c\u0435\u0440\u043e\u043b\u043e\u0433\u0438\u0438. "
        "\u0412\u0410\u0416\u041d\u0415\u0419\u0428\u0415\u0415 \u041f\u0420\u0410\u0412\u0418\u041b\u041e: \u043f\u0438\u0448\u0435\u0448\u044c \u0418\u0421\u041a\u041b\u042e\u0427\u0418\u0422\u0415\u041b\u042c\u041d\u041e \u043d\u0430 \u0440\u0443\u0441\u0441\u043a\u043e\u043c \u044f\u0437\u044b\u043a\u0435. "
        "\u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u0447\u0435\u0441\u043a\u0438 \u0437\u0430\u043f\u0440\u0435\u0449\u0435\u043d\u043e \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u044c \u043b\u044e\u0431\u044b\u0435 \u0438\u043d\u043e\u0441\u0442\u0440\u0430\u043d\u043d\u044b\u0435 \u0441\u043b\u043e\u0432\u0430: \u0430\u043d\u0433\u043b\u0438\u0439\u0441\u043a\u0438\u0435, \u0438\u0441\u043f\u0430\u043d\u0441\u043a\u0438\u0435, \u043d\u0435\u043c\u0435\u0446\u043a\u0438\u0435, \u0444\u0440\u0430\u043d\u0446\u0443\u0437\u0441\u043a\u0438\u0435, \u043a\u0438\u0442\u0430\u0439\u0441\u043a\u0438\u0435 \u2014 \u043b\u044e\u0431\u044b\u0435. "
        "\u0415\u0441\u043b\u0438 \u0445\u043e\u0447\u0435\u0448\u044c \u043d\u0430\u043f\u0438\u0441\u0430\u0442\u044c \u0441\u043b\u043e\u0432\u043e \u2014 \u043d\u0430\u0439\u0434\u0438 \u0440\u0443\u0441\u0441\u043a\u0438\u0439 \u0430\u043d\u0430\u043b\u043e\u0433. "
        "\u041d\u0430\u043f\u0440\u0438\u043c\u0435\u0440: \u0432\u043c\u0435\u0441\u0442\u043e 'love' \u043f\u0438\u0448\u0438 '\u043b\u044e\u0431\u043e\u0432\u044c', \u0432\u043c\u0435\u0441\u0442\u043e 'energy' \u043f\u0438\u0448\u0438 '\u044d\u043d\u0435\u0440\u0433\u0438\u044f', \u0432\u043c\u0435\u0441\u0442\u043e 'karma' \u043f\u0438\u0448\u0438 '\u043a\u0430\u0440\u043c\u0430'. "
        "\u0412\u0441\u0435 \u0441\u043b\u043e\u0432\u0430 \u0442\u043e\u043b\u044c\u043a\u043e \u0440\u0443\u0441\u0441\u043a\u0438\u0435. \u041f\u0438\u0448\u0435\u0448\u044c \u043a\u0440\u0430\u0441\u0438\u0432\u043e, \u0441 \u044d\u043c\u043e\u0434\u0437\u0438, \u0430\u0442\u043c\u043e\u0441\u0444\u0435\u0440\u043d\u043e. \u041e\u0431\u0440\u0430\u0449\u0430\u0435\u0448\u044c\u0441\u044f \u043d\u0430 \u0442\u044b. "
        "\u041e\u0442\u0432\u0435\u0442\u044b \u0434\u043b\u0438\u043d\u043d\u044b\u0435, \u043f\u043e\u0434\u0440\u043e\u0431\u043d\u044b\u0435, \u044d\u043c\u043e\u0446\u0438\u043e\u043d\u0430\u043b\u044c\u043d\u044b\u0435 \u2014 \u0441\u043e\u0437\u0434\u0430\u044e\u0449\u0438\u0435 \u043e\u0449\u0443\u0449\u0435\u043d\u0438\u0435 \u0447\u0442\u043e \u044d\u0442\u043e \u043d\u0430\u043f\u0438\u0441\u0430\u043d\u043e \u0438\u043c\u0435\u043d\u043d\u043e \u043f\u0440\u043e \u044d\u0442\u043e\u0433\u043e \u0447\u0435\u043b\u043e\u0432\u0435\u043a\u0430. "
        "\u041c\u0438\u043d\u0438\u043c\u0443\u043c 400 \u0441\u043b\u043e\u0432. \u0418\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439 \u0430\u0431\u0437\u0430\u0446\u044b \u0438 \u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0443 \u0434\u043b\u044f \u0443\u0434\u043e\u0431\u043d\u043e\u0433\u043e \u0447\u0442\u0435\u043d\u0438\u044f. "
        "\u0417\u0430\u043a\u0430\u043d\u0447\u0438\u0432\u0430\u0439 \u043e\u0442\u0432\u0435\u0442 \u043f\u043e\u043b\u043d\u044b\u043c \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u0435\u043c, \u043d\u0438\u043a\u043e\u0433\u0434\u0430 \u043d\u0435 \u043e\u0431\u0440\u044b\u0432\u0430\u0439 \u043d\u0430 \u0441\u0435\u0440\u0435\u0434\u0438\u043d\u0435. "
        "\u041f\u0435\u0440\u0435\u0434 \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u043e\u0439 \u043e\u0442\u0432\u0435\u0442\u0430 \u043f\u0440\u043e\u0432\u0435\u0440\u044c: \u043d\u0435\u0442 \u043b\u0438 \u0432 \u043d\u0451\u043c \u0445\u043e\u0442\u044c \u043e\u0434\u043d\u043e\u0433\u043e \u043d\u0435\u0440\u0443\u0441\u0441\u043a\u043e\u0433\u043e \u0441\u043b\u043e\u0432\u0430. \u0415\u0441\u043b\u0438 \u0435\u0441\u0442\u044c \u2014 \u0437\u0430\u043c\u0435\u043d\u0438 \u043d\u0430 \u0440\u0443\u0441\u0441\u043a\u043e\u0435."
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
                raw_text = result["choices"][0]["message"]["content"]
                # \u041f\u0440\u0438\u043c\u0435\u043d\u044f\u0435\u043c \u043f\u043e\u0441\u0442\u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0443 \u2014 \u0443\u0431\u0438\u0440\u0430\u0435\u043c \u0438\u043d\u043e\u0441\u0442\u0440\u0430\u043d\u043d\u044b\u0435 \u0441\u043b\u043e\u0432\u0430
                clean_text = clean_foreign_text(raw_text)
                return clean_text
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
        buttons.append([InlineKeyboardButton(text="\ud83d\udcab \u041c\u043e\u0439 \u0431\u0435\u0441\u043f\u043b\u0430\u0442\u043d\u044b\u0439 \u0440\u0430\u0437\u0431\u043e\u0440", callback_data="free")])
    buttons.extend([
        [InlineKeyboardButton(text="\ud83d\udc91 \u0421\u043e\u0432\u043c\u0435\u0441\u0442\u0438\u043c\u043e\u0441\u0442\u044c \u0434\u0432\u0443\u0445 \u043b\u044e\u0434\u0435\u0439 \u2014 49 \u2b50", callback_data="buy_compat")],
        [InlineKeyboardButton(text="\ud83d\udc98 \u041a\u043e\u0433\u0434\u0430 \u0432\u0441\u0442\u0440\u0435\u0442\u0438\u0448\u044c \u0442\u043e\u0433\u043e \u0441\u0430\u043c\u043e\u0433\u043e \u2014 49 \u2b50", callback_data="buy_when")],
        [InlineKeyboardButton(text="\ud83d\udc8d \u041f\u043e\u0440\u0442\u0440\u0435\u0442 \u0438\u0434\u0435\u0430\u043b\u044c\u043d\u043e\u0433\u043e \u043f\u0430\u0440\u0442\u043d\u0451\u0440\u0430 \u2014 49 \u2b50", callback_data="buy_portrait")],
        [InlineKeyboardButton(text="\ud83d\udc94 \u041f\u043e\u0447\u0435\u043c\u0443 \u043d\u0435 \u0432\u0435\u0437\u0451\u0442 \u0432 \u043b\u044e\u0431\u0432\u0438 \u2014 49 \u2b50", callback_data="buy_unlucky")],
        [InlineKeyboardButton(text="\ud83d\udd2e \u041c\u0430\u0442\u0440\u0438\u0446\u0430 \u0441\u0443\u0434\u044c\u0431\u044b \u2014 49 \u2b50", callback_data="buy_matrix")],
        [InlineKeyboardButton(text="\ud83c\udf1f \u041f\u0440\u0435\u0434\u043d\u0430\u0437\u043d\u0430\u0447\u0435\u043d\u0438\u0435 \u0438 \u043c\u0438\u0441\u0441\u0438\u044f \u2014 49 \u2b50", callback_data="buy_mission")],
        [InlineKeyboardButton(text="\ud83d\udd34 \u041a\u0430\u0440\u043c\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u0434\u043e\u043b\u0433 \u2014 49 \u2b50", callback_data="buy_karma")],
        [InlineKeyboardButton(text="\ud83d\udcbc \u041a\u0430\u0440\u044c\u0435\u0440\u043d\u044b\u0439 \u043f\u0443\u0442\u044c \u2014 49 \u2b50", callback_data="buy_career")],
        [InlineKeyboardButton(text="\ud83d\udcb0 \u0414\u0435\u043d\u0435\u0436\u043d\u044b\u0439 \u043a\u043e\u0434 \u2014 49 \u2b50", callback_data="buy_money")],
        [InlineKeyboardButton(text="\ud83c\udf19 \u0421\u0438\u043b\u044c\u043d\u044b\u0435 \u0438 \u0441\u043b\u0430\u0431\u044b\u0435 \u0434\u043d\u0438 \u043c\u0435\u0441\u044f\u0446\u0430 \u2014 49 \u2b50", callback_data="buy_days")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def check_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\u2705 \u042f \u043f\u043e\u0434\u043f\u0438\u0441\u0430\u043b\u0430\u0441\u044c!", callback_data="check_sub")],
    ])

def review_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\ud83d\ude0d \u041e\u0441\u0442\u0430\u0432\u0438\u0442\u044c \u043e\u0442\u0437\u044b\u0432", callback_data="leave_review")],
        [InlineKeyboardButton(text="\ud83d\udd2e \u0414\u0440\u0443\u0433\u0438\u0435 \u0440\u0430\u0437\u0431\u043e\u0440\u044b", callback_data="show_menu")]
    ])

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

RAZBORY = {
    "compat": ("\ud83d\udc91 \u0421\u043e\u0432\u043c\u0435\u0441\u0442\u0438\u043c\u043e\u0441\u0442\u044c \u0434\u0432\u0443\u0445 \u043b\u044e\u0434\u0435\u0439", "\u041f\u043e\u043b\u043d\u044b\u0439 \u043d\u0443\u043c\u0435\u0440\u043e\u043b\u043e\u0433\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u0440\u0430\u0437\u0431\u043e\u0440 \u0441\u043e\u0432\u043c\u0435\u0441\u0442\u0438\u043c\u043e\u0441\u0442\u0438"),
    "when": ("\ud83d\udc98 \u041a\u043e\u0433\u0434\u0430 \u0432\u0441\u0442\u0440\u0435\u0442\u0438\u0448\u044c \u0442\u043e\u0433\u043e \u0441\u0430\u043c\u043e\u0433\u043e", "\u041d\u0443\u043c\u0435\u0440\u043e\u043b\u043e\u0433\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u043f\u0440\u043e\u0433\u043d\u043e\u0437 \u0432\u0441\u0442\u0440\u0435\u0447\u0438 \u0441 \u043f\u0430\u0440\u0442\u043d\u0451\u0440\u043e\u043c"),
    "portrait": ("\ud83d\udc8d \u041f\u043e\u0440\u0442\u0440\u0435\u0442 \u0438\u0434\u0435\u0430\u043b\u044c\u043d\u043e\u0433\u043e \u043f\u0430\u0440\u0442\u043d\u0451\u0440\u0430", "\u041a\u0430\u043a\u043e\u0439 \u043e\u043d \u0431\u0443\u0434\u0435\u0442 \u2014 \u043f\u043e \u0442\u0432\u043e\u0438\u043c \u0447\u0438\u0441\u043b\u0430\u043c"),
    "unlucky": ("\ud83d\udc94 \u041f\u043e\u0447\u0435\u043c\u0443 \u043d\u0435 \u0432\u0435\u0437\u0451\u0442 \u0432 \u043b\u044e\u0431\u0432\u0438", "\u041d\u0443\u043c\u0435\u0440\u043e\u043b\u043e\u0433\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u0430\u043d\u0430\u043b\u0438\u0437 \u043f\u0440\u0438\u0447\u0438\u043d \u043d\u0435\u0443\u0434\u0430\u0447 \u0432 \u043b\u044e\u0431\u0432\u0438"),
    "matrix": ("\ud83d\udd2e \u041c\u0430\u0442\u0440\u0438\u0446\u0430 \u0441\u0443\u0434\u044c\u0431\u044b", "\u041f\u043e\u043b\u043d\u044b\u0439 \u0440\u0430\u0437\u0431\u043e\u0440 \u043c\u0430\u0442\u0440\u0438\u0446\u044b \u0441\u0443\u0434\u044c\u0431\u044b \u043f\u043e \u0434\u0430\u0442\u0435 \u0440\u043e\u0436\u0434\u0435\u043d\u0438\u044f"),
    "mission": ("\ud83c\udf1f \u041f\u0440\u0435\u0434\u043d\u0430\u0437\u043d\u0430\u0447\u0435\u043d\u0438\u0435 \u0438 \u043c\u0438\u0441\u0441\u0438\u044f", "\u0422\u0432\u043e\u0451 \u0438\u0441\u0442\u0438\u043d\u043d\u043e\u0435 \u043f\u0440\u0435\u0434\u043d\u0430\u0437\u043d\u0430\u0447\u0435\u043d\u0438\u0435 \u043f\u043e \u0447\u0438\u0441\u043b\u0430\u043c"),
    "karma": ("\ud83d\udd34 \u041a\u0430\u0440\u043c\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u0434\u043e\u043b\u0433", "\u0427\u0442\u043e \u043c\u0435\u0448\u0430\u0435\u0442 \u0442\u0435\u0431\u0435 \u0432 \u0436\u0438\u0437\u043d\u0438 \u0438 \u043a\u0430\u043a \u044d\u0442\u043e \u0438\u0441\u043f\u0440\u0430\u0432\u0438\u0442\u044c"),
    "career": ("\ud83d\udcbc \u041a\u0430\u0440\u044c\u0435\u0440\u043d\u044b\u0439 \u043f\u0443\u0442\u044c", "\u0422\u0432\u043e\u0439 \u0438\u0434\u0435\u0430\u043b\u044c\u043d\u044b\u0439 \u043a\u0430\u0440\u044c\u0435\u0440\u043d\u044b\u0439 \u043f\u0443\u0442\u044c \u043f\u043e \u0447\u0438\u0441\u043b\u0430\u043c"),
    "money": ("\ud83d\udcb0 \u0414\u0435\u043d\u0435\u0436\u043d\u044b\u0439 \u043a\u043e\u0434", "\u0422\u0432\u043e\u0439 \u043b\u0438\u0447\u043d\u044b\u0439 \u0434\u0435\u043d\u0435\u0436\u043d\u044b\u0439 \u043a\u043e\u0434 \u0438 \u043a\u0430\u043a \u0435\u0433\u043e \u0430\u043a\u0442\u0438\u0432\u0438\u0440\u043e\u0432\u0430\u0442\u044c"),
    "days": ("\ud83c\udf19 \u0421\u0438\u043b\u044c\u043d\u044b\u0435 \u0438 \u0441\u043b\u0430\u0431\u044b\u0435 \u0434\u043d\u0438 \u043c\u0435\u0441\u044f\u0446\u0430", "\u0422\u0432\u043e\u0438 \u043b\u0438\u0447\u043d\u044b\u0435 \u0441\u0438\u043b\u044c\u043d\u044b\u0435 \u0438 \u0441\u043b\u0430\u0431\u044b\u0435 \u0434\u043d\u0438 \u043f\u043e \u0447\u0438\u0441\u043b\u0430\u043c"),
}

@dp.message(Command("start"), StateFilter("*"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = get_user(message.from_user.id)
    if user["subscribed_channel"]:
        is_subscribed = await check_subscription(message.from_user.id)
        if not is_subscribed:
            user["subscribed_channel"] = False
            save_users(users)
    if not user["subscribed_channel"]:
        await message.answer(
            "\ud83d\udd2e \u041f\u0440\u0438\u0432\u0435\u0442! \u042f \u0415\u0432\u0430 \u2014 \u0442\u0432\u043e\u0439 \u043b\u0438\u0447\u043d\u044b\u0439 \u043d\u0443\u043c\u0435\u0440\u043e\u043b\u043e\u0433.\n\n"
            "\u0427\u0438\u0441\u043b\u0430 \u0445\u0440\u0430\u043d\u044f\u0442 \u0442\u0430\u0439\u043d\u044b \u0442\u0432\u043e\u0435\u0439 \u0441\u0443\u0434\u044c\u0431\u044b, \u043b\u044e\u0431\u0432\u0438 \u0438 \u043f\u0440\u0435\u0434\u043d\u0430\u0437\u043d\u0430\u0447\u0435\u043d\u0438\u044f. "
            "\u042f \u043f\u043e\u043c\u043e\u0433\u0443 \u0442\u0435\u0431\u0435 \u0440\u0430\u0441\u043a\u0440\u044b\u0442\u044c \u0438\u0445.\n\n"
            f"\u2728 \u041f\u043e\u0434\u043f\u0438\u0448\u0438\u0441\u044c \u043d\u0430 \u043d\u0430\u0448 \u043a\u0430\u043d\u0430\u043b {CHANNEL} \u0438 \u043f\u043e\u043b\u0443\u0447\u0438 \u0431\u0435\u0441\u043f\u043b\u0430\u0442\u043d\u044b\u0439 \u0440\u0430\u0437\u0431\u043e\u0440 \u0447\u0438\u0441\u043b\u0430 \u0441\u0443\u0434\u044c\u0431\u044b!\n\n"
            "\u041f\u043e\u0441\u043b\u0435 \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0438 \u043d\u0430\u0436\u043c\u0438 \u043a\u043d\u043e\u043f\u043a\u0443 \u043d\u0438\u0436\u0435 \ud83d\udc47",
            reply_markup=check_menu()
        )
    else:
        await message.answer(
            "\ud83d\udd2e \u041f\u0440\u0438\u0432\u0435\u0442! \u042f \u0415\u0432\u0430 \u2014 \u0442\u0432\u043e\u0439 \u043b\u0438\u0447\u043d\u044b\u0439 \u043d\u0443\u043c\u0435\u0440\u043e\u043b\u043e\u0433.\n\n\u0412\u044b\u0431\u0435\u0440\u0438 \u0441\u0432\u043e\u0439 \u0440\u0430\u0437\u0431\u043e\u0440 \ud83d\udc47",
            reply_markup=main_menu(user)
        )

@dp.message(Command("menu"), StateFilter("*"))
async def menu_cmd(message: Message, state: FSMContext):
    await state.clear()
    user = get_user(message.from_user.id)
    await message.answer("\ud83d\udd2e \u0412\u044b\u0431\u0435\u0440\u0438 \u0440\u0430\u0437\u0431\u043e\u0440:", reply_markup=main_menu(user))

@dp.message(Command("admin"), StateFilter("*"))
async def admin_panel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    total = len(users)
    free_used = sum(1 for u in users.values() if u.get("free_used"))
    bought = sum(1 for u in users.values() if u.get("purchased"))
    total_purchases = sum(len(u.get("purchased", [])) for u in users.values())
    reviews = sum(1 for u in users.values() if u.get("review_left"))
    razbory_count = {}
    for u in users.values():
        for r in u.get("purchased", []):
            razbory_count[r] = razbory_count.get(r, 0) + 1
    top = sorted(razbory_count.items(), key=lambda x: x[1], reverse=True)
    top_text = "\n".join([f"  {k}: {v}" for k, v in top[:5]]) if top else "  \u043d\u0435\u0442 \u043f\u043e\u043a\u0443\u043f\u043e\u043a"
    await message.answer(
        f"\ud83d\udcca \u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u0431\u043e\u0442\u0430 \u0415\u0432\u0430\n\n"
        f"\ud83d\udc65 \u0412\u0441\u0435\u0433\u043e \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0435\u0439: {total}\n"
        f"\ud83d\udcab \u041f\u043e\u043b\u0443\u0447\u0438\u043b\u0438 \u0431\u0435\u0441\u043f\u043b\u0430\u0442\u043d\u044b\u0439 \u0440\u0430\u0437\u0431\u043e\u0440: {free_used}\n"
        f"\ud83d\udcb3 \u041a\u0443\u043f\u0438\u043b\u0438 \u0445\u043e\u0442\u044f \u0431\u044b \u043e\u0434\u0438\u043d \u0440\u0430\u0437\u0431\u043e\u0440: {bought}\n"
        f"\ud83d\uded2 \u0412\u0441\u0435\u0433\u043e \u043f\u043e\u043a\u0443\u043f\u043e\u043a: {total_purchases}\n"
        f"\u2b50 \u041e\u0441\u0442\u0430\u0432\u0438\u043b\u0438 \u043e\u0442\u0437\u044b\u0432: {reviews}\n\n"
        f"\ud83c\udfc6 \u0422\u043e\u043f \u0440\u0430\u0437\u0431\u043e\u0440\u043e\u0432:\n{top_text}"
    )

@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    is_subscribed = await check_subscription(callback.from_user.id)
    if not is_subscribed:
        await callback.answer("\u274c \u0422\u044b \u0435\u0449\u0451 \u043d\u0435 \u043f\u043e\u0434\u043f\u0438\u0441\u0430\u043b\u0430\u0441\u044c!", show_alert=True)
        await callback.message.answer(
            f"\u041f\u043e\u0434\u043f\u0438\u0448\u0438\u0441\u044c \u043d\u0430 \u043a\u0430\u043d\u0430\u043b {CHANNEL} \u0438 \u043d\u0430\u0436\u043c\u0438 \u043a\u043d\u043e\u043f\u043a\u0443 \u0441\u043d\u043e\u0432\u0430 \ud83d\udc47",
            reply_markup=check_menu()
        )
        return
    user["subscribed_channel"] = True
    save_users(users)
    if user["free_used"]:
        await callback.message.answer("\u2705 \u0422\u044b \u0443\u0436\u0435 \u043f\u043e\u0434\u043f\u0438\u0441\u0430\u043d\u0430! \u0412\u044b\u0431\u0435\u0440\u0438 \u0440\u0430\u0437\u0431\u043e\u0440:", reply_markup=main_menu(user))
        await callback.answer()
        return
    await callback.message.answer(
        "\u2705 \u041e\u0442\u043b\u0438\u0447\u043d\u043e! \u0422\u044b \u043f\u043e\u0434\u043f\u0438\u0441\u0430\u043b\u0430\u0441\u044c!\n\n"
        "\u0422\u0435\u043f\u0435\u0440\u044c \u0432\u0432\u0435\u0434\u0438 \u0441\u0432\u043e\u044e \u0434\u0430\u0442\u0443 \u0440\u043e\u0436\u0434\u0435\u043d\u0438\u044f \u0432 \u0444\u043e\u0440\u043c\u0430\u0442\u0435 \u0414\u0414.\u041c\u041c.\u0413\u0413\u0413\u0413\n"
        "\u041d\u0430\u043f\u0440\u0438\u043c\u0435\u0440: 15.03.1995"
    )
    user["waiting"] = "free"
    save_users(users)
    await state.set_state(Form.waiting_date)
    await callback.answer()

@dp.callback_query(F.data == "free")
async def free_handler(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if not user["subscribed_channel"]:
        is_subscribed = await check_subscription(callback.from_user.id)
        if not is_subscribed:
            await callback.message.answer(
                f"\ud83d\udcab \u0427\u0442\u043e\u0431\u044b \u043f\u043e\u043b\u0443\u0447\u0438\u0442\u044c \u0431\u0435\u0441\u043f\u043b\u0430\u0442\u043d\u044b\u0439 \u0440\u0430\u0437\u0431\u043e\u0440 \u2014 \u043f\u043e\u0434\u043f\u0438\u0448\u0438\u0441\u044c \u043d\u0430 {CHANNEL} \ud83d\udc47",
                reply_markup=check_menu()
            )
            await callback.answer()
            return
        user["subscribed_channel"] = True
        save_users(users)
    if user["free_used"]:
        await callback.message.answer(
            "\ud83d\udcab \u0411\u0435\u0441\u043f\u043b\u0430\u0442\u043d\u044b\u0439 \u0440\u0430\u0437\u0431\u043e\u0440 \u0442\u044b \u0443\u0436\u0435 \u043f\u043e\u043b\u0443\u0447\u0438\u043b\u0430.\n\n\u0412\u044b\u0431\u0435\u0440\u0438 \u043f\u043b\u0430\u0442\u043d\u044b\u0439 \u0440\u0430\u0437\u0431\u043e\u0440 \ud83d\udd2e",
            reply_markup=main_menu(user)
        )
        await callback.answer()
        return
    user["waiting"] = "free"
    save_users(users)
    await callback.message.answer(
        "\u2728 \u0412\u0432\u0435\u0434\u0438 \u0441\u0432\u043e\u044e \u0434\u0430\u0442\u0443 \u0440\u043e\u0436\u0434\u0435\u043d\u0438\u044f \u0432 \u0444\u043e\u0440\u043c\u0430\u0442\u0435 \u0414\u0414.\u041c\u041c.\u0413\u0413\u0413\u0413\n\u041d\u0430\u043f\u0440\u0438\u043c\u0435\u0440: 15.03.1995"
    )
    await state.set_state(Form.waiting_date)
    await callback.answer()

@dp.callback_query(F.data.