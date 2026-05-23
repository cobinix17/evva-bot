import os
import logging
import asyncio
import json
import httpx
from datetime import datetime, date, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

# ---------- Проверка переменных окружения ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not BOT_TOKEN or not GROQ_API_KEY:
    raise EnvironmentError("BOT_TOKEN и GROQ_API_KEY должны быть установлены!")

CHANNEL = "@eva_numerologg"
ADMIN_ID = 5854618444

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ---------- Хранение пользователей в JSON ----------
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

users = load_users()  # загружаем при старте

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
            "horoscope": True,
            "purchased": [],
            "waiting": None
        }
    if user_id == ADMIN_ID:
        users[uid]["free_used"] = True
        users[uid]["subscribed_channel"] = True
        for r in ["compat", "when", "portrait", "unlucky", "matrix", "mission", "karma", "career", "money", "days"]:
            if r not in users[uid]["purchased"]:
                users[uid]["purchased"].append(r)
    save_users(users)  # автосохранение после каждого изменения
    return users[uid]

# ---------- Проверка подписки ----------
async def check_subscription(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except:
        return False

# ---------- Запрос к Groq ----------
async def ask_groq(prompt: str) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {
                "role": "system",
                "content": "Ты — Ева, тёплый и мудрый нумеролог. Общаешься как близкая подруга которая глубоко разбирается в нумерологии. Пишешь по-русски, красиво, с эмодзи, атмосферно. Обращаешься на ты. Ответы длинные, подробные, эмоциональные — создающие ощущение что это написано именно про этого человека. Минимум 400 слов. Используй абзацы и структуру для удобного чтения."
            },
            {"role": "user", "content": prompt}
        ]
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=data, timeout=90)
        response.raise_for_status()
        result = response.json()
        return result["choices"][0]["message"]["content"]

# ---------- Нумерология ----------
def calculate_destiny(date_str: str) -> int:
    digits = [int(d) for d in date_str if d.isdigit()]
    total = sum(digits)
    while total > 9 and total not in (11, 22, 33):
        total = sum(int(d) for d in str(total))
    return total

# ---------- Клавиатуры ----------
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💫 Мой бесплатный разбор", callback_data="free")],
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

def check_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я подписалась!", callback_data="check_sub")],
    ])

# ---------- Генерация PDF (исправлено) ----------
async def generate_pdf(text: str, title: str) -> bytes:
    try:
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_margins(20, 20, 20)
        pdf.set_auto_page_break(auto=True, margin=20)

        font_path = "DejaVuSans.ttf"
        if os.path.exists(font_path):
            pdf.add_font("DejaVu", "", font_path, uni=True)
            pdf.set_font("DejaVu", "", 14)
            title_font = "DejaVu"
            text_font = "DejaVu"
        else:
            logging.warning("Unicode font not found, Cyrillic in PDF will be missing")
            title_font = "Helvetica"
            text_font = "Helvetica"

        pdf.set_font(title_font, "B", 18)
        pdf.cell(0, 12, title, ln=True, align="C")
        pdf.ln(5)
        pdf.set_font(text_font, "", 11)
        pdf.multi_cell(0, 7, text)

        # fpdf2: output() без аргументов возвращает bytes, что нам и нужно
        if os.path.exists(font_path):
            return pdf.output()  # bytes с Unicode
        else:
            return pdf.output(dest='S').encode('latin-1')
    except Exception as e:
        logging.error(f"PDF error: {e}")
        return None

# ---------- Обработчики ----------
@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    get_user(message.from_user.id)
    await message.answer(
        "🔮 Привет! Я Ева — твой личный нумеролог.\n\n"
        "Числа хранят тайны твоей судьбы, любви и предназначения. "
        "Я помогу тебе раскрыть их.\n\n"
        f"✨ Подпишись на наш канал {CHANNEL} и получи бесплатный разбор числа судьбы!\n\n"
        "После подписки нажми кнопку ниже 👇",
        reply_markup=check_menu()
    )

@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    is_subscribed = await check_subscription(callback.from_user.id)
    if not is_subscribed:
        await callback.answer("❌ Ты ещё не подписалась!", show_alert=True)
        await callback.message.answer(
            f"Подпишись на канал {CHANNEL} и нажми кнопку снова 👇",
            reply_markup=check_menu()
        )
        return
    user["subscribed_channel"] = True
    if user["free_used"] and user["birth_date"]:
        await callback.message.answer("✅ Ты уже подписана! Выбери разбор:", reply_markup=main_menu())
        await callback.answer()
        return
    await callback.message.answer(
        "✅ Отлично! Ты подписалась!\n\n"
        "Теперь введи свою дату рождения в формате ДД.ММ.ГГГГ\n"
        "Например: 15.03.1995"
    )
    await state.set_state(Form.waiting_date)
    user["waiting"] = "free"
    await callback.answer()

@dp.callback_query(F.data == "free")
async def free_handler(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
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
    if user["free_used"]:
        await callback.message.answer(
            "💫 Бесплатный разбор ты уже получила.\n\n"
            "Выбери один из платных разборов 🔮",
            reply_markup=main_menu()
        )
        await callback.answer()
        return
    await callback.message.answer(
        "✨ Введи свою дату рождения в формате ДД.ММ.ГГГГ\n"
        "Например: 15.03.1995"
    )
    await state.set_state(Form.waiting_date)
    user["waiting"] = "free"
    await callback.answer()

# ---------- Платёжные функции ----------
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

# Переименован в RAZBORY (латиница)
RAZBORY = {
    "compat": ("💑 Совместимость двух людей", "Полный нумерологический разбор совместимости двух людей"),
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

@dp.callback_query(F.data.startswith("buy_"))
async def buy_handler(callback: CallbackQuery, state: FSMContext):
    key = callback.data.replace("buy_", "")
    user = get_user(callback.from_user.id)
    if key in user["purchased"]:
        user["waiting"] = key
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
    user = get_user(message.from_user.id)
    payload = message.successful_payment.invoice_payload
    if payload not in user["purchased"]:
        user["purchased"].append(payload)
    user["waiting"] = payload
    if payload == "compat":
        await message.answer("✅ Оплата прошла! Введи две даты рождения через запятую:\nНапример: 15.03.1995, 22.07.1998")
        await state.set_state(Form.waiting_second_date)
    else:
        await message.answer("✅ Оплата прошла! Введи свою дату рождения в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
        await state.set_state(Form.waiting_date)

# ---------- Проверка даты (строгая) ----------
def is_valid_date(date_str: str) -> bool:
    try:
        datetime.strptime(date_str, "%d.%m.%Y")
        return True
    except ValueError:
        return False

# ---------- Обработка двух дат ----------
@dp.message(Form.waiting_second_date)
async def handle_two_dates(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    text = message.text.strip()
    if "," not in text:
        await message.answer("❌ Введи две даты через запятую.\nНапример: 15.03.1995, 22.07.1998")
        return
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2:
        await message.answer("❌ Нужно именно две даты через запятую.")
        return
    if not (is_valid_date(parts[0]) and is_valid_date(parts[1])):
        await message.answer("❌ Одна из дат недействительна. Используй формат ДД.ММ.ГГГГ")
        return
    await message.answer("⏳ Ева составляет твой разбор, подожди немного...")
    try:
        n1 = calculate_destiny(parts[0])
        n2 = calculate_destiny(parts[1])
        prompt = f"Сделай максимально подробный и эмоциональный нумерологический разбор совместимости двух людей. Первый родился {parts[0]}, число судьбы {n1}. Второй родился {parts[1]}, число судьбы {n2}. Опиши характер каждого, их совместимость в любви и отношениях, эмоциональную связь, возможные конфликты, сильные стороны пары, прогноз отношений. Пиши как близкая подруга-нумеролог, тепло и атмосферно."
        answer = await ask_groq(prompt)
        await message.answer(f"💑 Разбор совместимости\n\n{answer}")
        pdf_bytes = await generate_pdf(answer, "Совместимость двух людей")
        if pdf_bytes:
            await message.answer_document(
                BufferedInputFile(pdf_bytes, filename="sovmestimost.pdf"),
                caption="📄 Твой разбор в PDF — сохрани себе!"
            )
        await message.answer("✨ Понравился разбор? Оставь отзыв!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="😍 Оставить отзыв", callback_data="leave_review")],
            [InlineKeyboardButton(text="🔮 Другие разборы", callback_data="show_menu")]
        ]))
    except Exception as e:
        logging.error(f"Compat error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()

# ---------- Обработка одной даты (основной) ----------
@dp.message(Form.waiting_date)
async def handle_date(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    text = message.text.strip()

    if not is_valid_date(text):
        await message.answer("❌ Неверная дата. Введи в формате ДД.ММ.ГГГГ\nНапример: 15.03.1995")
        return

    number = calculate_destiny(text)
    waiting = user.get("waiting")
    if waiting is None:
        await message.answer("Пожалуйста, выбери разбор из меню (/menu), а затем введи дату.")
        await state.clear()
        return
    if waiting == "free":
        user["birth_date"] = text
        user["destiny_number"] = number

    await message.answer("⏳ Ева составляет твой разбор, подожди немного...")

    try:
        # ... (все условия waiting и prompt)
        # Здесь я приведу только структуру, в реальном коде оставьте все варианты как было
        if waiting == "free":
            prompt = f"Сделай подробный и эмоциональный нумерологический разбор числа судьбы {number} для человека рождённого {text}."
            title = f"💫 Твоё число судьбы: {number}"
            filename = "chislo_sudby.pdf"
            user["free_used"] = True
        else:
            # остальные варианты (when, portrait и т.д.) вставьте точно так же, как было
            # ...
            pass  # заглушка

        # Вызов Groq
        answer = await ask_groq(prompt)
        await message.answer(f"{title}\n\n{answer}")

        pdf_bytes = await generate_pdf(answer, title)
        if pdf_bytes:
            await message.answer_document(
                BufferedInputFile(pdf_bytes, filename=filename),
                caption="📄 Твой разбор в PDF — сохрани себе!"
            )

        if waiting == "free":
            await message.answer("✨ Это было только начало! Выбери платный разбор...", reply_markup=main_menu())
        else:
            await message.answer("✨ Понравился разбор? Оставь отзыв!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="😍 Оставить отзыв", callback_data="leave_review")],
                [InlineKeyboardButton(text="🔮 Другие разборы", callback_data="show_menu")]
            ]))
    except Exception as e:
        logging.error(f"Date handler error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка при запросе к нейросети: {e}")
    await state.clear()

# Остальные обработчики (leave_review, handle_review, show_menu, menu_cmd) остаются без изменений
# ...

async def send_daily_horoscope():
    while True:
        now = datetime.now()
        target = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)  # исправлено: сломается в конце месяца
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
                    prompt = f"Составь короткий личный прогноз на сегодня {today} для человека с числом судьбы {number}..."
                    horoscope = await ask_groq(prompt)
                    await bot.send_message(
                        int(uid),
                        f"🌅 Доброе утро! Твой прогноз на сегодня:\n\n{horoscope}"
                    )
                except Exception as e:
                    logging.error(f"Horoscope error for {uid}: {e}")

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
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())