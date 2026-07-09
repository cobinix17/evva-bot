# webapp.py — API и статика Telegram Mini App (личный кабинет Евы).
# Отдельный модуль, подключается к общему aiohttp-серверу из bot.py (run_web).
# Переиспользует db.py/numerology.py/config.py — платёжный поток (Stars) и
# генерация разборов остаются в bot.py/ai.py, веб только читает/инициирует.
import os
import json
import hmac
import hashlib
import asyncio
import logging
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram.types import LabeledPrice

import db
from config import (
    TITLES, PRICES, PAID_RAZBORY, RAZBOR_DESCRIPTIONS,
    SECTION_DESTINY, SECTION_MONEY, SECTION_LOVE, SECTION_HEALTH, SECTION_PAST,
    ADMIN_ID, REF_BONUS_PERCENT,
    PREMIUM_PRICE, PREMIUM_PERIOD, PREMIUM_PAYLOAD, PREMIUM_TITLE, ASK_DAILY_LIMIT,
)
from numerology import numerology_summary, is_valid_date, build_numerology_context
from keyboards import date_choice_menu
from ai import ask_ai

ASK_QUESTION_MAX_LEN = 300
# Отдельная, более узкая полоса генерации именно для веб-чата "Спроси Еву" —
# не общая с ботом (там свои _gen_semaphore/_priority_semaphore), чтобы не
# тащить циклический импорт bot.py <-> webapp.py. Небольшой размер осознанно:
# это дополнительный канал поверх бота, а не основной.
_web_gen_semaphore = asyncio.Semaphore(3)

BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
WEBAPP_DIR  = os.path.join(os.path.dirname(__file__), "webapp")

# Ссылается на aiogram Bot из bot.py — присваивается в setup_webapp_routes().
# Нужен для create_invoice_link (оплата инициируется с веба, но сам платёж и
# доставка разбора остаются в Telegram-чате, чтобы не дублировать логику
# генерации/семафоров/PDF в двух местах).
_bot = None
_bot_username: str | None = None

async def _get_bot_username() -> str:
    global _bot_username
    if _bot_username is None:
        me = await _bot.get_me()
        _bot_username = me.username
    return _bot_username

# ── ВАЛИДАЦИЯ initData ────────────────────────────────────────────────────────
# Telegram подписывает данные Mini App секретом = HMAC-SHA256(bot_token, "WebAppData").
# Без этой проверки любой мог бы прислать чужой user_id и читать/покупать за него.
def _check_init_data(init_data: str) -> dict | None:
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc_hash  = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, received_hash):
        return None
    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except json.JSONDecodeError:
        return None

async def _authed_user_id(request: web.Request) -> int | None:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = _check_init_data(init_data)
    if not tg_user:
        return None
    return tg_user.get("id")

def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)

# ── API ────────────────────────────────────────────────────────────────────────
async def api_me(request: web.Request) -> web.Response:
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    user = await db.get_user(user_id)
    matrix = None
    if user.get("birth_date"):
        try:
            matrix = numerology_summary(user.get("first_name") or "дорогая", user["birth_date"])
        except Exception as e:
            logging.warning(f"webapp numerology_summary error: {e}")
    return web.json_response({
        "user_id":       user_id,
        "bot_username":  await _get_bot_username(),
        "first_name":    user.get("first_name"),
        "birth_date":    user.get("birth_date"),
        "destiny_number": user.get("destiny_number"),
        "purchased":     user.get("purchased", []),
        "ref_balance":   user.get("ref_balance", 0),
        "is_premium":    db.is_premium(user),
        "premium_until": user["premium_until"].isoformat() if user.get("premium_until") else None,
        "is_admin":      user_id == ADMIN_ID,
        "matrix":        matrix,
    })

async def api_set_birthdate(request: web.Request) -> web.Response:
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    body = await request.json()
    date_str = (body.get("birth_date") or "").strip()
    name     = (body.get("first_name") or "").strip()
    if not is_valid_date(date_str):
        return _json_error("Неверная дата. Формат ДД.ММ.ГГГГ")
    from numerology import calculate_destiny
    user = await db.get_user(user_id)
    user["birth_date"] = date_str
    user["destiny_number"] = calculate_destiny(date_str)
    if name and len(name) <= 30:
        user["first_name"] = name
    await db.save_user(user_id, user)
    return web.json_response({"ok": True})

_SECTIONS = [
    ("destiny", "🔮 Судьба и личность", SECTION_DESTINY),
    ("money",   "💰 Деньги и карьера",   SECTION_MONEY),
    ("love",    "💑 Любовь и отношения", SECTION_LOVE),
    ("health",  "🌙 Здоровье и энергия", SECTION_HEALTH),
    ("past",    "✨ Прошлое и будущее",  SECTION_PAST),
]

async def api_catalog(request: web.Request) -> web.Response:
    """Каталог разборов сгруппированный по разделам — с ценами и описаниями.
    Не требует авторизации (используется и на превью-экране без initData)."""
    sections = []
    for key, title, items in _SECTIONS:
        sections.append({
            "key": key,
            "title": title,
            "items": [
                {
                    "key":   k,
                    "title": TITLES.get(k, k),
                    "price": PRICES.get(k, 49),
                    "desc":  RAZBOR_DESCRIPTIONS.get(k, ""),
                }
                for k in items
            ],
        })
    return web.json_response({"sections": sections})

async def api_buy(request: web.Request) -> web.Response:
    """Создаёт invoice-ссылку на разбор — фронт открывает её через
    Telegram.WebApp.openInvoice(). Сама оплата и доставка разбора (текст +
    PDF) идут в чат с ботом, как при покупке из главного меню."""
    from aiogram.types import LabeledPrice
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    key = request.match_info["key"]
    if key not in PAID_RAZBORY:
        return _json_error("unknown reading", 404)
    user = await db.get_user(user_id)
    if key in user.get("purchased", []):
        return web.json_response({"already_purchased": True})
    price = PRICES.get(key, 49)
    title = PAID_RAZBORY[key]
    desc  = RAZBOR_DESCRIPTIONS.get(key, title)
    link = await _bot.create_invoice_link(
        title=title, description=desc, payload=key,
        currency="XTR", prices=[LabeledPrice(label=title, amount=price)],
    )
    return web.json_response({"invoice_url": link})

async def api_reading(request: web.Request) -> web.Response:
    """Отдаёт сохранённый текст уже сгенерированного разбора — открывается
    прямо в веб-кабинете, без перехода в чат с ботом."""
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    key  = request.match_info["key"]
    user = await db.get_user(user_id)
    if key not in user.get("purchased", []):
        return _json_error("not purchased", 403)
    reading = await db.get_reading_text(user_id, key)
    if not reading:
        return _json_error("Разбор ещё готовится — открой его в чате с ботом", 404)
    return web.json_response({
        "title": reading["title"],
        "text":  reading["text"],
    })

async def api_premium_buy(request: web.Request) -> web.Response:
    """Invoice-ссылка на подписку с subscription_period — открывается через
    Telegram.WebApp.openInvoice(), Telegram сам оформит рекуррентное
    списание. Обработка оплаты и продлений остаётся в bot.py
    (successful_payment) — общая для веба и бота, не дублируется."""
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    user = await db.get_user(user_id)
    if db.is_premium(user):
        return web.json_response({"already_premium": True})
    link = await _bot.create_invoice_link(
        title=PREMIUM_TITLE,
        description="Безлимитный доступ ко всем разборам, личный прогноз каждое утро и приоритетная генерация.",
        payload=PREMIUM_PAYLOAD,
        currency="XTR",
        prices=[LabeledPrice(label="Ева Премиум — месяц", amount=PREMIUM_PRICE)],
        subscription_period=PREMIUM_PERIOD,
    )
    return web.json_response({"invoice_url": link})

async def api_referral(request: web.Request) -> web.Response:
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    username = await _get_bot_username()
    stats = await db.get_referral_stats(user_id)
    bonuses = [
        {
            "amount":     b["amount"],
            "razbor":     TITLES.get(b["razbor_key"], b["razbor_key"] or "разбор"),
            "from_name":  b["first_name"] or "Подруга",
            "created_at": b["created_at"].isoformat(),
        }
        for b in stats["bonuses"]
    ]
    return web.json_response({
        "ref_link":     f"https://t.me/{username}?start=ref_{user_id}",
        "count":        stats["count"],
        "earned":       stats["earned"],
        "balance":      stats["balance"],
        "bonus_percent": REF_BONUS_PERCENT,
        "bonuses":      bonuses,
    })

async def api_balance_buy(request: web.Request) -> web.Response:
    """Оплата разбора бонусным балансом. Само списание и открытие разбора
    происходит здесь, но фактическую генерацию (ИИ + PDF) всё ещё делает
    бот — чтобы не дублировать эту логику, после оплаты отправляем в чат
    сообщение с кнопкой 'Для себя/другая дата' (use_my_date/use_new_date —
    их обработчики в bot.py не привязаны к FSM-состоянию, сработают
    независимо от того, что именно прислало эту клавиатуру)."""
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    key = request.match_info["key"]
    if key not in PAID_RAZBORY:
        return _json_error("unknown reading", 404)
    user = await db.get_user(user_id)
    if key in user.get("purchased", []):
        return web.json_response({"already_purchased": True})
    if not user.get("birth_date"):
        return _json_error("Сначала укажи дату рождения на вкладке «Матрица»", 400)

    price = PRICES.get(key, 49)
    spent = await db.spend_balance(user_id, price)
    if not spent:
        return _json_error("Недостаточно звёзд на балансе", 400)

    user = await db.get_user(user_id)  # перечитываем — баланс уже списан
    user["purchased"].append(key)
    user["waiting"] = key
    await db.save_user(user_id, user)

    title = PAID_RAZBORY[key]
    desc  = RAZBOR_DESCRIPTIONS.get(key, "")
    intro = f"💬 {desc}\n\n" if desc else ""
    try:
        await _bot.send_message(
            user_id,
            f"✅ «{title}» оплачен балансом ({price} ⭐)!\n\n"
            f"{intro}Делаешь разбор для себя ({user['birth_date']}) или введёшь другую дату?",
            reply_markup=date_choice_menu()
        )
    except Exception as e:
        logging.warning(f"balance buy notify error: {e}")
    return web.json_response({"ok": True})

async def api_ask(request: web.Request) -> web.Response:
    """AI-чат «Спроси Еву» — премиум-фича, доступна из веб-кабинета так же,
    как /ask в боте. Тот же дневной лимит (db.ask_try_consume), та же
    проверка подписки и наличия даты рождения."""
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    user = await db.get_user(user_id)
    if not db.is_premium(user):
        return _json_error("premium_required", 403)
    if not user.get("birth_date"):
        return _json_error("Сначала укажи дату рождения на вкладке «Матрица»", 400)

    body = await request.json()
    question = (body.get("question") or "").strip()
    if len(question) < 3:
        return _json_error("Напиши вопрос текстом, хотя бы пару слов")
    if len(question) > ASK_QUESTION_MAX_LEN:
        return _json_error(f"Вопрос слишком длинный — сократи до {ASK_QUESTION_MAX_LEN} символов")

    allowed = await db.ask_try_consume(user_id, ASK_DAILY_LIMIT)
    if not allowed:
        return _json_error(f"На сегодня использовано {ASK_DAILY_LIMIT} вопросов — дневной лимит. Возвращайся завтра 🌸", 429)

    name    = user.get("first_name") or "дорогая"
    context = build_numerology_context(name, user["birth_date"])
    prompt  = (
        f"Вот нумерологические данные {name}:\n{context}\n\n"
        f"Она задаёт тебе личный вопрос в переписке: «{question}»\n\n"
        "Ответь как Ева — тепло, конкретно, опираясь на её числа. Это часть живого "
        "диалога, а не отдельный разбор: не используй emoji-заголовки, не структурируй "
        "ответ на блоки, пиши связным текстом. 3-6 предложений, по делу, без воды."
    )
    try:
        async with _web_gen_semaphore:
            answer = await ask_ai(prompt)
    except Exception as e:
        logging.error(f"webapp api_ask error: {e}", exc_info=True)
        return _json_error("Что-то пошло не так — попробуй ещё раз чуть позже", 500)
    return web.json_response({"answer": answer})

async def api_matrix(request: web.Request) -> web.Response:
    """Публичный расчёт матрицы по дате — для гостевого превью без входа
    (человек ещё не открывал бота, но зашёл по ссылке на веб)."""
    date_str = request.query.get("date", "")
    name     = request.query.get("name", "дорогая")
    if not is_valid_date(date_str):
        return _json_error("Неверная дата. Формат ДД.ММ.ГГГГ")
    try:
        return web.json_response(numerology_summary(name[:30], date_str))
    except Exception as e:
        logging.error(f"webapp api_matrix error: {e}", exc_info=True)
        return _json_error("Ошибка расчёта", 500)

# ── СТАТИКА ───────────────────────────────────────────────────────────────────
async def index(request: web.Request) -> web.Response:
    path = os.path.join(WEBAPP_DIR, "index.html")
    if not os.path.exists(path):
        return web.Response(text="webapp not built", status=404)
    return web.FileResponse(path)

def setup_webapp_routes(app: web.Application, bot):
    global _bot
    _bot = bot
    app.router.add_get("/api/me", api_me)
    app.router.add_post("/api/me/birthdate", api_set_birthdate)
    app.router.add_get("/api/catalog", api_catalog)
    app.router.add_get("/api/matrix", api_matrix)
    app.router.add_post("/api/buy/{key}", api_buy)
    app.router.add_get("/api/reading/{key}", api_reading)
    app.router.add_post("/api/premium/buy", api_premium_buy)
    app.router.add_post("/api/ask", api_ask)
    app.router.add_get("/api/referral", api_referral)
    app.router.add_post("/api/balance/buy/{key}", api_balance_buy)
    app.router.add_get("/app", index)
    app.router.add_get("/app/", index)
    if os.path.isdir(WEBAPP_DIR):
        app.router.add_static("/app/static", os.path.join(WEBAPP_DIR, "static"))
