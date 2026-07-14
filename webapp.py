# webapp.py — API и статика Telegram Mini App (личный кабинет Евы).
# Отдельный модуль, подключается к общему aiohttp-серверу из bot.py (run_web).
# Переиспользует db.py/numerology.py/config.py — платёжный поток (Stars) и
# генерация разборов остаются в bot.py/ai.py, веб только читает/инициирует.
import os
import re
import time
import json
import hmac
import random
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
from ai import ask_ai, is_rude, rude_reply

# Та же санитизация что в bot.py:sanitize_name — не импортируем оттуда
# напрямую, чтобы не создавать циклическую зависимость bot.py <-> webapp.py
# (bot.py уже импортирует webapp.py внутри run_web()).
_NAME_ALLOWED_RE = re.compile(r"[^а-яёА-ЯЁa-zA-Z\s\-]")

def _sanitize_name(raw: str) -> str:
    cleaned = _NAME_ALLOWED_RE.sub("", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:30]

FEEDBACK_MAX_LEN = 800
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
INIT_DATA_MAX_AGE = 24 * 3600  # Telegram сам обновляет initData при каждом открытии Mini App

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
    # Защита от replay: утёкшая (лог, прокси, чужое устройство) initData не
    # должна работать вечно — Telegram выдаёт свежую строку при каждом открытии.
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date <= 0 or time.time() - auth_date > INIT_DATA_MAX_AGE:
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
        "notifications": user.get("notifications", True),
        "is_premium":    db.is_premium(user),
        "premium_until": user["premium_until"].isoformat() if user.get("premium_until") else None,
        "is_admin":      user_id == ADMIN_ID,
        "matrix":        matrix,
        "can_spin":      user.get("last_spin_date") is None or user["last_spin_date"] < db.utc_now().date(),
        **await _digest_status(user_id, user),
    })

async def _digest_status(user_id: int, user: dict) -> dict:
    """Лёгкая проверка для карточки 'Портрет' на Матрице — не запускает
    генерацию, только смотрит сколько разборов куплено и есть ли уже
    актуальный (совпадающий по составу) кэш."""
    purchased = sorted(k for k in user.get("purchased", []) if k in PAID_RAZBORY)
    result = {"digest_count": len(purchased), "digest_min": MIN_DIGEST_READINGS, "digest_ready": False}
    if len(purchased) >= MIN_DIGEST_READINGS:
        cached = await db.get_reading_text(user_id, _DIGEST_KEY)
        result["digest_ready"] = bool(cached and cached.get("date_str") == ",".join(purchased))
    return result

# ── ЕЖЕДНЕВНЫЙ БОНУС ──────────────────────────────────────────────────────────
# Веса подобраны так, чтобы матожидание было ~2 звезды/день — на самый
# дешёвый разбор (49⭐) нужен примерно месяц ежедневных заходов. Цель —
# повод открывать веб-апп каждый день, а не бесплатные разборы всем.
_SPIN_REWARDS  = [1, 2, 4, 8, 20]
_SPIN_WEIGHTS  = [60, 25, 10, 4, 1]

async def api_spin(request: web.Request) -> web.Response:
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    amount = random.choices(_SPIN_REWARDS, weights=_SPIN_WEIGHTS, k=1)[0]
    ok = await db.daily_spin_try(user_id, amount)
    if not ok:
        return _json_error("Бонус уже забран — приходи завтра 🌸", 429)
    return web.json_response({"ok": True, "amount": amount})

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
    if name:
        clean_name = _sanitize_name(name)
        if clean_name:
            user["first_name"] = clean_name
    await db.save_user(user_id, user)
    return web.json_response({"ok": True})

async def api_set_name(request: web.Request) -> web.Response:
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    body = await request.json()
    name = _sanitize_name((body.get("name") or "").strip())
    if len(name) < 2 or len(name) > 30:
        return _json_error("Введи настоящее имя — только буквы, от 2 до 30 символов")
    user = await db.get_user(user_id)
    user["first_name"] = name
    await db.save_user(user_id, user)
    return web.json_response({"ok": True, "name": name})

async def api_set_notifications(request: web.Request) -> web.Response:
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    body = await request.json()
    user = await db.get_user(user_id)
    user["notifications"] = bool(body.get("enabled", True))
    await db.save_user(user_id, user)
    return web.json_response({"ok": True})

async def api_feedback(request: web.Request) -> web.Response:
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    body = await request.json()
    text = (body.get("text") or "").strip()
    category = body.get("category") if body.get("category") in ("idea", "bug") else "idea"
    if len(text) < 3:
        return _json_error("Напиши текстом, хотя бы пару слов")
    if len(text) > FEEDBACK_MAX_LEN:
        return _json_error(f"Слишком длинно — сократи до {FEEDBACK_MAX_LEN} символов")
    await db.add_feedback(user_id, text, category)
    user = await db.get_user(user_id)
    name  = user.get("first_name") or "Аноним"
    label = "💡 Идея" if category == "idea" else "🐞 Баг"
    try:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        await _bot.send_message(
            ADMIN_ID, f"{label} от {name} (id {user_id}, из веб-кабинета)\n\n{text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬 Ответить", callback_data=f"admin_reply_{user_id}")]
            ])
        )
    except Exception as e:
        logging.warning(f"webapp feedback notify error: {e}")
    return web.json_response({"ok": True})

async def api_promo_check(request: web.Request) -> web.Response:
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    body = await request.json()
    code = (body.get("code") or "").strip().upper()
    if not code:
        return _json_error("Введи код промокода")
    row = await db.db_pool.fetchrow('SELECT * FROM coupons WHERE code = $1', code)
    if not row:
        return _json_error("Такого промокода не существует")
    if row["expires_at"] and row["expires_at"] < db.utc_now():
        return _json_error("Этот промокод уже истёк")
    remaining = row["max_uses"] - row["uses_count"]
    if remaining <= 0:
        return _json_error("Этот промокод исчерпан")
    return web.json_response({"remaining": remaining})

async def api_promo_redeem(request: web.Request) -> web.Response:
    """Активация промокода на конкретный разбор. Как и с оплатой балансом,
    саму генерацию делает бот — после списания использования промокода
    отправляем в чат клавиатуру выбора даты."""
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    body = await request.json()
    code = (body.get("code") or "").strip().upper()
    key  = (body.get("key") or "").strip()
    if key not in PAID_RAZBORY:
        return _json_error("unknown reading", 404)
    user = await db.get_user(user_id)

    if key in user.get("purchased", []):
        return web.json_response({"already_purchased": True})

    result = await db.use_coupon(code, user_id)
    if result == "not_found":
        return _json_error("Промокод не найден")
    if result == "expired":
        return _json_error("Промокод истёк")
    if result == "limit":
        return _json_error("Лимит промокода исчерпан")

    user["purchased"].append(key)
    user["waiting"] = key
    await db.save_user(user_id, user)

    if not user.get("birth_date"):
        return web.json_response({"ok": True, "needs_birthdate": True})

    title = PAID_RAZBORY[key]
    desc  = RAZBOR_DESCRIPTIONS.get(key, "")
    intro = f"💬 {desc}\n\n" if desc else ""
    try:
        await _bot.send_message(
            user_id,
            f"✅ «{title}» получен по промокоду!\n\n"
            f"{intro}Делаешь разбор для себя ({user['birth_date']}) или введёшь другую дату?",
            reply_markup=date_choice_menu()
        )
    except Exception as e:
        logging.warning(f"promo redeem notify error: {e}")
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

async def api_reading_regenerate(request: web.Request) -> web.Response:
    """Повторный заказ уже купленного разбора — бесплатно, т.к. оплата уже
    была. Саму генерацию делает бот: если дата совпадёт с прошлой, бот
    вернёт тот же сохранённый текст (см. _process_date), а если пользователь
    введёт новую дату — сгенерирует заново."""
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    key = request.match_info["key"]
    if key not in PAID_RAZBORY:
        return _json_error("unknown reading", 404)
    user = await db.get_user(user_id)
    if key not in user.get("purchased", []):
        return _json_error("not purchased", 403)

    user["waiting"] = key
    await db.save_user(user_id, user)

    if not user.get("birth_date"):
        return web.json_response({"ok": True, "needs_birthdate": True})

    title = PAID_RAZBORY[key]
    try:
        await _bot.send_message(
            user_id,
            f"🔁 «{title}» — заказываешь заново.\n\n"
            f"Делаешь разбор для себя ({user['birth_date']}) или введёшь другую дату?",
            reply_markup=date_choice_menu()
        )
    except Exception as e:
        logging.warning(f"reading regenerate notify error: {e}")
    return web.json_response({"ok": True})

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

    if is_rude(question):
        return web.json_response({"answer": rude_reply()})

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
        await db.refund_ask_try(user_id)
        return _json_error("Что-то пошло не так — попробуй ещё раз чуть позже", 500)
    return web.json_response({"answer": answer})

# ── ПОРТРЕТ ИЗ КУПЛЕННЫХ РАЗБОРОВ ─────────────────────────────────────────────
MIN_DIGEST_READINGS = 3
_DIGEST_KEY = "profile_digest"

async def api_profile_digest(request: web.Request) -> web.Response:
    """Собирает связную выжимку из ВСЕХ купленных разборов пользователя —
    не новый продукт, а способ показать ценность уже купленного и мягко
    подтолкнуть докупить недостающее (ниже MIN_DIGEST_READINGS фича заперта).
    Кэшируется как обычный разбор (db.save_reading_text/get_reading_text) под
    служебным ключом profile_digest; date_str там же используется НЕ под дату,
    а под отпечаток набора купленных ключей — это дешёвый способ понять,
    изменился ли состав покупок с прошлой сборки, без отдельной колонки/таблицы."""
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    user = await db.get_user(user_id)
    purchased = sorted(k for k in user.get("purchased", []) if k in PAID_RAZBORY)
    if len(purchased) < MIN_DIGEST_READINGS:
        return _json_error(
            f"Нужно минимум {MIN_DIGEST_READINGS} купленных разбора, чтобы собрать портрет "
            f"(сейчас {len(purchased)}) — загляни в «Разборы» 🌸",
            409
        )
    signature = ",".join(purchased)

    cached = await db.get_reading_text(user_id, _DIGEST_KEY)
    if cached and cached.get("date_str") == signature:
        return web.json_response({"title": cached["title"], "text": cached["text"], "cached": True})

    texts = []
    for key in purchased:
        r = await db.get_reading_text(user_id, key)
        if r and r.get("text"):
            title = TITLES.get(key, key)
            texts.append(f"### {title}\n{r['text'][:1200]}")
    if len(texts) < MIN_DIGEST_READINGS:
        return _json_error("Разборы ещё готовятся — попробуй чуть позже 🌸", 409)

    name = user.get("first_name") or "дорогая"
    prompt = (
        f"Вот тексты {len(texts)} разборов, которые уже получила {name} по своим числам:\n\n"
        + "\n\n".join(texts) +
        "\n\nСведи это всё в ОДИН связный портрет личности — не пересказывай каждый разбор "
        "по очереди, а найди общее: повторяющиеся черты, ключевые таланты и уязвимости, "
        "главное, что стоит знать о себе прямо сейчас. Не используй emoji-заголовки и "
        "структуру по блокам — свяжный текст на 6-10 предложений, тепло и по делу, "
        "как итоговое резюме консультации."
    )
    try:
        async with _web_gen_semaphore:
            digest_text = await ask_ai(prompt)
    except Exception as e:
        logging.error(f"profile_digest ask_ai error: {e}", exc_info=True)
        return _json_error("Не получилось собрать портрет — попробуй чуть позже 🙏", 500)

    title = "📋 Твой портрет"
    await db.save_reading_text(user_id, _DIGEST_KEY, title, digest_text, signature)
    return web.json_response({"title": title, "text": digest_text, "cached": False})

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

# ── RATE LIMIT ────────────────────────────────────────────────────────────────
# Простой in-memory sliding-window лимитер на /api/*. Ключ — X-Telegram-Init-Data
# если есть (стабильно про пользователя), иначе IP. Главная цель — не дать
# перебирать промокоды через /api/promo/check без ограничений (см. аудит).
_RATE_LIMIT = 30          # запросов
_RATE_WINDOW = 60         # секунд
_rate_buckets: dict[str, list[float]] = {}

@web.middleware
async def _rate_limit_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    key = request.headers.get("X-Telegram-Init-Data") or request.remote or "unknown"
    now = time.time()
    bucket = _rate_buckets.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < _RATE_WINDOW]
    if len(bucket) >= _RATE_LIMIT:
        return _json_error("Слишком много запросов — подожди немного 🙏", 429)
    bucket.append(now)
    # Периодически подчищаем мусорные ключи, чтобы словарь не рос бесконечно
    if len(_rate_buckets) > 5000:
        for k in list(_rate_buckets.keys()):
            if not _rate_buckets[k] or now - _rate_buckets[k][-1] > _RATE_WINDOW:
                del _rate_buckets[k]
    return await handler(request)

def setup_webapp_routes(app: web.Application, bot):
    global _bot
    _bot = bot
    app.middlewares.append(_rate_limit_middleware)
    app.router.add_get("/api/me", api_me)
    app.router.add_post("/api/spin", api_spin)
    app.router.add_post("/api/me/birthdate", api_set_birthdate)
    app.router.add_get("/api/catalog", api_catalog)
    app.router.add_get("/api/matrix", api_matrix)
    app.router.add_post("/api/buy/{key}", api_buy)
    app.router.add_get("/api/reading/{key}", api_reading)
    app.router.add_post("/api/reading/{key}/regenerate", api_reading_regenerate)
    app.router.add_post("/api/premium/buy", api_premium_buy)
    app.router.add_post("/api/ask", api_ask)
    app.router.add_post("/api/profile_digest", api_profile_digest)
    app.router.add_get("/api/referral", api_referral)
    app.router.add_post("/api/balance/buy/{key}", api_balance_buy)
    app.router.add_post("/api/me/name", api_set_name)
    app.router.add_post("/api/me/notifications", api_set_notifications)
    app.router.add_post("/api/feedback", api_feedback)
    app.router.add_post("/api/promo/check", api_promo_check)
    app.router.add_post("/api/promo/redeem", api_promo_redeem)
    app.router.add_get("/app", index)
    app.router.add_get("/app/", index)
    if os.path.isdir(WEBAPP_DIR):
        app.router.add_static("/app/static", os.path.join(WEBAPP_DIR, "static"))
