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
from datetime import timedelta
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram.types import LabeledPrice

import db
from config import (
    TITLES, PRICES, PAID_RAZBORY, RAZBOR_DESCRIPTIONS, TWO_DATE_KEYS,
    SECTION_DESTINY, SECTION_MONEY, SECTION_LOVE, SECTION_HEALTH, SECTION_PAST,
    ADMIN_ID, REF_BONUS_PERCENT,
    PREMIUM_PRICE, PREMIUM_PERIOD, PREMIUM_PAYLOAD, PREMIUM_TITLE, ASK_DAILY_LIMIT, YESNO_FREE_LIMIT,
    PREMIUM_PRICE_RUB, YOOKASSA_SHOP_ID, STARS_TO_RUB_RATE, rub_price, price_of, get_discount,
)
from numerology import numerology_summary, is_valid_date, build_numerology_context, calculate_destiny, normalize_date
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
    day_today = None
    if user.get("birth_date"):
        try:
            matrix = numerology_summary(db.default_name(user), user["birth_date"])
        except Exception as e:
            logging.warning(f"webapp numerology_summary error: {e}")
        try:
            from numerology import personal_day_info
            info = personal_day_info(user["birth_date"])
            day_today = {"number": info["number"], "energy": info["energy"], "advice": info["advice"]}
            if db.is_premium(user):
                dn = user.get("destiny_number") or calculate_destiny(user["birth_date"])
                day_today["destiny_note"] = _WEB_DESTINY_DAY_NOTES.get(dn, _WEB_DESTINY_DAY_NOTES[9])
        except Exception as e:
            logging.warning(f"webapp personal_day_info error: {e}")
    return web.json_response({
        "day_today":     day_today,
        "user_id":       user_id,
        "bot_username":  await _get_bot_username(),
        "first_name":    user.get("first_name"),
        "gender":        user.get("gender") or "f",
        "birth_date":    user.get("birth_date"),
        "destiny_number": user.get("destiny_number"),
        "purchased":     user.get("purchased", []),
        "ref_balance":   user.get("ref_balance", 0),
        "notifications": user.get("notifications", True),
        "is_premium":    db.is_premium(user),
        "premium_until": user["premium_until"].isoformat() if user.get("premium_until") else None,
        "is_admin":      user_id == ADMIN_ID,
        "matrix":        matrix,
        "email":         user.get("email"),
        "yookassa":      bool(YOOKASSA_SHOP_ID),
        "premium_price_rub": PREMIUM_PRICE_RUB if YOOKASSA_SHOP_ID else None,
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
    date_str = normalize_date(body.get("birth_date") or "")
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
    if result == "used":
        return _json_error("Ты уже активировала этот промокод — он даётся один раз в руки")

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
                    "key":       k,
                    "title":     TITLES.get(k, k),
                    "price":     price_of(k, 49),
                    "base":      PRICES.get(k, 49),
                    "price_rub": rub_price(price_of(k, 49)) if YOOKASSA_SHOP_ID else None,
                    "base_rub":  rub_price(PRICES.get(k, 49)) if YOOKASSA_SHOP_ID else None,
                    "desc":      RAZBOR_DESCRIPTIONS.get(k, ""),
                }
                for k in items
            ],
        })
    return web.json_response({"sections": sections, "yookassa": bool(YOOKASSA_SHOP_ID),
                              "discount": get_discount()})

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
    price = price_of(key, 49)
    title = PAID_RAZBORY[key]
    desc  = RAZBOR_DESCRIPTIONS.get(key, title)
    link = await _bot.create_invoice_link(
        title=title, description=desc, payload=key,
        currency="XTR", prices=[LabeledPrice(label=title, amount=price)],
    )
    return web.json_response({"invoice_url": link})

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

async def api_buy_rub(request: web.Request) -> web.Response:
    """Создаёт платёж рублями через ЮKassa (карта или СБП) для разбора или
    премиума. Возвращает confirmation_url — фронт открывает его через
    tg.openLink. email обязателен для чека (54-ФЗ); сохраняем, чтобы в
    следующий раз не спрашивать. Выдача разбора/премиума — в вебхуке
    /webhook/yookassa после успешной оплаты, как и для бота."""
    if not YOOKASSA_SHOP_ID:
        return _json_error("Оплата рублями недоступна", 400)
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    key = request.match_info["key"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    method = body.get("method")
    if method not in ("bank_card", "sbp"):
        return _json_error("bad method", 400)
    user  = await db.get_user(user_id)
    email = (body.get("email") or user.get("email") or "").strip()
    if not _EMAIL_RE.match(email):
        return _json_error("Введи корректный email для чека", 400)
    if email != user.get("email"):
        await db.set_email(user_id, email)

    if key in (PREMIUM_PAYLOAD, "premium"):
        if db.is_premium(user):
            return web.json_response({"already_premium": True})
        payload, price_rub, title = PREMIUM_PAYLOAD, PREMIUM_PRICE_RUB, PREMIUM_TITLE
        desc = "Безлимитный доступ ко всем разборам, личный прогноз каждое утро и приоритетная генерация."
    else:
        if key not in PAID_RAZBORY:
            return _json_error("unknown reading", 404)
        if key in user.get("purchased", []):
            return web.json_response({"already_purchased": True})
        payload, price_rub = key, rub_price(price_of(key, 49))
        title = PAID_RAZBORY[key]
        desc  = RAZBOR_DESCRIPTIONS.get(key, title)

    from yookassa_pay import create_payment
    try:
        _, url = await create_payment(
            amount_rub=price_rub,
            description=desc or title,
            return_url=f"https://t.me/{await _get_bot_username()}",
            metadata={"user_id": str(user_id), "payload": payload},
            email=email,
            method=method,
        )
    except Exception as e:
        logging.error(f"web YooKassa create_payment error: {e}", exc_info=True)
        return _json_error("Не удалось создать оплату — попробуй позже 🙏", 500)
    return web.json_response({"confirmation_url": url})

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

async def api_reading_generate(request: web.Request) -> web.Response:
    """Генерирует купленный разбор ПРЯМО в вебе (без ухода в чат с ботом).
    Берёт дату(-ы) из тела запроса, гоняет то же ядро генерации, что и бот
    (generation.generate_single/generate_compat — ИИ + кэш), и возвращает
    готовый текст фронту. Оплата уже была при покупке — повторный вызов на ту
    же дату бесплатно вернёт тот же текст из кэша, на новую — сгенерирует
    заново. compat принимает date1+date2, остальные — date."""
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    key = request.match_info["key"]
    if key not in PAID_RAZBORY:
        return _json_error("unknown reading", 404)
    user = await db.get_user(user_id)
    if key not in user.get("purchased", []):
        return _json_error("not purchased", 403)

    try:
        body = await request.json()
    except Exception:
        body = {}

    from generation import generate_single, generate_compat, generate_name, GenerationBusy
    try:
        if key in TWO_DATE_KEYS:
            date1 = normalize_date(body.get("date1") or "")
            date2 = normalize_date(body.get("date2") or "")
            if not is_valid_date(date1) or not is_valid_date(date2):
                return _json_error("Введи две корректные даты в формате ДД.ММ.ГГГГ", 400)
            title, text, from_cache = await generate_compat(user_id, user, date1, date2, key=key)
        elif key == "business_name":
            # Разбор по НАЗВАНИЮ бизнеса — текст, а не дата (см. generate_name).
            subject = re.sub(r"[\r\n\t]+", " ", (body.get("subject") or "")).strip()[:50]
            if len(subject) < 2:
                return _json_error("Введи название бизнеса/бренда", 400)
            title, text, from_cache = await generate_name(user_id, user, "business_name", subject)
        else:
            date_str = normalize_date(body.get("date") or "")
            if not date_str and user.get("birth_date"):
                date_str = user["birth_date"]
            if not is_valid_date(date_str):
                return _json_error("Введи корректную дату в формате ДД.ММ.ГГГГ", 400)
            # Первая дата в жизни аккаунта — запоминаем как дату рождения.
            if not user.get("birth_date"):
                user["birth_date"]     = date_str
                user["destiny_number"] = calculate_destiny(date_str)
                await db.save_user(user_id, user)
            title, text, from_cache = await generate_single(user_id, user, key, date_str)
    except GenerationBusy:
        return _json_error("Разбор уже готовится — подожди пару секунд 🔮", 409)
    except Exception as e:
        logging.error(f"web reading generate error [{key}]: {e}", exc_info=True)
        return _json_error("Не удалось составить разбор — попробуй ещё раз чуть позже 🙏", 500)

    return web.json_response({"title": title, "text": text, "from_cache": from_cache})

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

async def api_achievements(request: web.Request) -> web.Response:
    """Значки-достижения (лёгкая геймификация) — считаются на лету из данных
    пользователя (см. achievements.compute_achievements)."""
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    user  = await db.get_user(user_id)
    stats = await db.get_referral_stats(user_id)
    digest = await _digest_status(user_id, user)
    from achievements import compute_achievements
    items = compute_achievements(
        purchased_count=len(user.get("purchased", [])),
        ref_count=stats["count"],
        is_premium=db.is_premium(user),
        has_birthdate=bool(user.get("birth_date")),
        has_spun=user.get("last_spin_date") is not None,
        digest_ready=bool(digest.get("digest_ready")),
    )
    return web.json_response({"items": items})

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
    """Оплата разбора бонусным балансом. Списываем баланс, открываем разбор —
    саму генерацию потом запустит форма даты в вебе (api_reading_generate),
    без ухода в чат с ботом."""
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    key = request.match_info["key"]
    if key not in PAID_RAZBORY:
        return _json_error("unknown reading", 404)
    user = await db.get_user(user_id)
    if key in user.get("purchased", []):
        return web.json_response({"already_purchased": True})

    price = price_of(key, 49)
    # Замок на юзера (см. db.user_lock): двойной клик по кнопке иначе прошёл бы
    # обе проверки «не куплено» и списал бы баланс дважды за один разбор.
    async with db.user_lock(user_id):
        user = await db.get_user(user_id)
        if key in user.get("purchased", []):
            return web.json_response({"already_purchased": True})
        spent = await db.spend_balance(user_id, price)
        if not spent:
            return _json_error("Недостаточно звёзд на балансе", 400)
        user = await db.get_user(user_id)  # перечитываем — баланс уже списан
        user["purchased"].append(key)
        await db.save_user(user_id, user)
    # Баланс — бонусные (реферальные) звёзды, а не живые деньги: в выручку
    # не пишем, иначе двойной счёт с покупкой, что этот бонус породила.
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

    try:
        from generation import answer_ask
        async with _web_gen_semaphore:
            answer = await answer_ask(user_id, user, question)
    except Exception as e:
        logging.error(f"webapp api_ask error: {e}", exc_info=True)
        await db.refund_ask_try(user_id)
        return _json_error("Что-то пошло не так — попробуй ещё раз чуть позже", 500)
    return web.json_response({"answer": answer})

async def api_day_number(request: web.Request) -> web.Response:
    """Число дня для веб-кабинета: личное число + энергия + совет. Премиум
    получает ещё персональную строку под число судьбы (как в боте)."""
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    user = await db.get_user(user_id)
    if not user.get("birth_date"):
        return _json_error("Сначала укажи дату рождения", 400)
    from numerology import personal_day_info
    info = personal_day_info(user["birth_date"])
    resp = {
        "number": info["number"],
        "energy": info["energy"],
        "advice": info["advice"],
        "is_premium": db.is_premium(user),
    }
    if db.is_premium(user):
        dn = user.get("destiny_number") or calculate_destiny(user["birth_date"])
        resp["destiny_note"] = _WEB_DESTINY_DAY_NOTES.get(dn, _WEB_DESTINY_DAY_NOTES[9])
    return web.json_response(resp)

_WEB_DESTINY_DAY_NOTES = {
    1:  "Для твоей единицы важно сегодня не ждать разрешения — первый шаг за тобой.",
    2:  "Твоя двойка сегодня читает людей особенно тонко — доверяй первому впечатлению.",
    3:  "Твоя тройка сегодня ярче обычного — говори, показывай, будь на виду.",
    4:  "Твоя четвёрка найдёт опору в делах — то, что заложишь сегодня, будет прочным.",
    5:  "Твоя пятёрка сегодня тянется к новому — не отказывай себе в перемене.",
    6:  "Твоя шестёрка сегодня нужна близким — тепло, отданное сегодня, вернётся.",
    7:  "Твоей семёрке сегодня стоит побыть в тишине — там придёт нужный ответ.",
    8:  "Твоя восьмёрка сегодня сильна в деньгах и решениях — действуй уверенно.",
    9:  "Твоя девятка сегодня видит суть — отпусти то, что уже отжило.",
    11: "Твоё число 11 сегодня резонирует с днём — будь внимательна к знакам и снам.",
    22: "Твоё число 22 сегодня даёт особую практическую силу — берись за большое.",
    33: "Твоё число 33 сегодня светит ярче — твоя забота сегодня целительна.",
}

async def api_destiny_matrix(request: web.Request) -> web.Response:
    """Бесплатная схема «Матрица судьбы» (ромб/октаграмма Ладини) — лид-магнит.
    Визуал + краткие значения арканов; полное толкование даёт платный разбор
    matrix_full (покупается отдельно)."""
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    user = await db.get_user(user_id)
    if not user.get("birth_date"):
        return _json_error("Сначала укажи дату рождения", 400)
    from numerology import destiny_matrix
    matrix = destiny_matrix(user["birth_date"])
    return web.json_response({
        "matrix":     matrix,
        "owned":      "matrix_full" in user.get("purchased", []),
        "price":      price_of("matrix_full", 149),
        "base":       PRICES.get("matrix_full", 149),
        "price_rub":  rub_price(price_of("matrix_full", 149)) if YOOKASSA_SHOP_ID else None,
        "base_rub":   rub_price(PRICES.get("matrix_full", 149)) if YOOKASSA_SHOP_ID else None,
    })

async def api_psychomatrix(request: web.Request) -> web.Response:
    """Бесплатный визуал «Квадрат Пифагора» (психоматрица) — лид-магнит.
    Таблица 3×3 с качествами и их силой; полное толкование даёт платный
    разбор psychomatrix (покупается отдельно)."""
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    user = await db.get_user(user_id)
    if not user.get("birth_date"):
        return _json_error("Сначала укажи дату рождения", 400)
    from numerology import psychomatrix_view
    view = psychomatrix_view(user["birth_date"])
    return web.json_response({
        "cells":     view["cells"],
        "lines":     view["lines"],
        "owned":     "psychomatrix" in user.get("purchased", []),
        "price":     price_of("psychomatrix", 99),
        "base":      PRICES.get("psychomatrix", 99),
        "price_rub": rub_price(price_of("psychomatrix", 99)) if YOOKASSA_SHOP_ID else None,
        "base_rub":  rub_price(PRICES.get("psychomatrix", 99)) if YOOKASSA_SHOP_ID else None,
    })

async def api_yesno(request: web.Request) -> web.Response:
    """Быстрый ответ «Да/Нет» по числам. Бесплатно YESNO_FREE_LIMIT в день,
    премиум — безлимит (та же логика, что в боте)."""
    user_id = await _authed_user_id(request)
    if not user_id:
        return _json_error("unauthorized", 401)
    user = await db.get_user(user_id)
    if not user.get("birth_date"):
        return _json_error("Сначала укажи дату рождения", 400)
    body = await request.json()
    question = (body.get("question") or "").strip()
    if len(question) < 3:
        return _json_error("Напиши вопрос текстом, хотя бы пару слов")
    if len(question) > ASK_QUESTION_MAX_LEN:
        return _json_error(f"Вопрос слишком длинный — сократи до {ASK_QUESTION_MAX_LEN} символов")
    if is_rude(question):
        return web.json_response({"answer": rude_reply()})
    if not db.is_premium(user):
        allowed = await db.yesno_try_consume(user_id, YESNO_FREE_LIMIT)
        if not allowed:
            return _json_error(f"На сегодня {YESNO_FREE_LIMIT} вопроса «Да/Нет» уже задано. С премиумом — без ограничений 🌸", 429)
    try:
        from generation import answer_yes_no
        name = db.default_name(user)
        answer = await answer_yes_no(name, user["birth_date"], question, male=db.is_male(user))
    except Exception as e:
        logging.error(f"webapp api_yesno error: {e}", exc_info=True)
        if not db.is_premium(user):
            await db.refund_yesno_try(user_id)
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

    reading_map = await db.get_reading_texts(user_id, purchased)
    texts = []
    for key in purchased:
        r = reading_map.get(key)
        if r and r.get("text"):
            title = TITLES.get(key, key)
            texts.append(f"### {title}\n{r['text'][:1200]}")
    if len(texts) < MIN_DIGEST_READINGS:
        return _json_error("Разборы ещё готовятся — попробуй чуть позже 🌸", 409)

    name = db.default_name(user)
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
# Версия статики = момент старта процесса. Подставляется в ссылки app.js/style.css
# как ?v=..., поэтому после каждого деплоя Telegram-вебвью видит НОВЫЙ url и
# перекачивает файлы — больше не нужно вручную закрывать Mini App, чтобы
# подтянулись обновления. Сам index.html отдаём с no-cache по той же причине.
_STATIC_VERSION = str(int(time.time()))

async def index(request: web.Request) -> web.Response:
    path = os.path.join(WEBAPP_DIR, "index.html")
    if not os.path.exists(path):
        return web.Response(text="webapp not built", status=404)
    with open(path, encoding="utf-8") as f:
        html = f.read()
    html = html.replace('/app/static/style.css', f'/app/static/style.css?v={_STATIC_VERSION}')
    html = html.replace('/app/static/app.js',    f'/app/static/app.js?v={_STATIC_VERSION}')
    return web.Response(
        text=html, content_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )

# ── RATE LIMIT ────────────────────────────────────────────────────────────────
# Простой in-memory sliding-window лимитер на /api/*. Ключ — X-Telegram-Init-Data
# если есть (стабильно про пользователя), иначе IP. Главная цель — не дать
# перебирать промокоды через /api/promo/check без ограничений (см. аудит).
_RATE_LIMIT = 30          # запросов
_RATE_WINDOW = 60         # секунд
_rate_buckets: dict[str, list[float]] = {}

@web.middleware
async def _rate_limit_middleware(request: web.Request, handler):
    # Лимитируем и /api/*, и вебхук ЮKassa (он вне /api/, но публичный: без
    # лимита кто угодно мог бы флудить его POST-ами, а бот на каждый дёргал бы
    # API ЮKassa через fetch_payment).
    if not (request.path.startswith("/api/") or request.path == "/webhook/yookassa"):
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

# ── ЮKASSA WEBHOOK (прямой API) ──────────────────────────────────────────────
async def yookassa_webhook(request: web.Request) -> web.Response:
    """Уведомление об оплате рублями через прямой API ЮKassa. НЕ доверяем телу
    вебхука напрямую (его легко подделать POST-запросом на публичный урл) —
    переспрашиваем статус и сумму у самой ЮKassa по payment_id (fetch_payment,
    сервер-сервер) и только тогда выдаём премиум/разбор. mark_yookassa_payment —
    идемпотентность: ЮKassa повторяет вебхук, пока не получит 200, повторная
    обработка того же payment_id не должна выдать премиум/разбор дважды."""
    if not YOOKASSA_SHOP_ID:
        return web.Response(status=200)
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400)
    payment_id = (body.get("object") or {}).get("id")
    if not payment_id:
        return web.Response(status=200)

    from yookassa_pay import fetch_payment
    payment = await fetch_payment(payment_id)
    if not payment or payment.status != "succeeded":
        return web.Response(status=200)

    metadata = payment.metadata or {}
    try:
        user_id = int(metadata.get("user_id", 0))
    except (TypeError, ValueError):
        user_id = 0
    payload = metadata.get("payload")
    if not user_id or not payload:
        logging.warning(f"yookassa webhook без metadata: payment_id={payment_id}")
        return web.Response(status=200)

    amount_rub = int(float(payment.amount.value))
    is_new = await db.mark_yookassa_payment(payment_id, user_id, payload, amount_rub)
    if not is_new:
        return web.Response(status=200)

    # Реф-бонус считаем в звёздах-эквиваленте (см. ту же нормализацию в
    # bot.py successful_payment) — amount_rub уже в рублях, не в копейках.
    stars_equiv = round(amount_rub / STARS_TO_RUB_RATE)

    if payload == PREMIUM_PAYLOAD:
        user = await db.get_user(user_id)
        # Продлеваем от максимума текущей даты и "сейчас" — повторная оплата
        # до истечения срока добавляет 31 день сверху, а не затирает остаток.
        current_until = user.get("premium_until")
        base = current_until if current_until and current_until > db.utc_now() else db.utc_now()
        until = base + timedelta(days=31)
        await db.set_premium(user_id, until)
        await db.log_payment(user_id, None, stars_equiv, "RUB")

        referrer_id = user.get("referred_by")
        if referrer_id:
            bonus = max(1, round(stars_equiv * REF_BONUS_PERCENT / 100))
            try:
                await db.add_ref_bonus(referrer_id, user_id, bonus, "premium")
            except Exception as e:
                logging.warning(f"yookassa premium ref bonus error: {e}")

        try:
            await _bot.send_message(
                user_id,
                f"💎 Оплата прошла! Премиум активен до {until.strftime('%d.%m.%Y')} — "
                "все разборы открыты, генерация без очереди 🌸\n\n"
                "Это разовый платёж (не автоподписка, как со звёздами) — через "
                "месяц пришлю напоминание продлить."
            )
        except Exception as e:
            logging.warning(f"yookassa premium notify error: {e}")
        return web.Response(status=200)

    if payload not in PAID_RAZBORY:
        logging.warning(f"yookassa webhook с неизвестным payload={payload!r}")
        return web.Response(status=200)

    # Под user_lock: та же покупка могла идти параллельно через баланс/бот —
    # read-modify-write без замка мог бы затереть их изменения.
    async with db.user_lock(user_id):
        user = await db.get_user(user_id)
        if payload not in user["purchased"]:
            user["purchased"].append(payload)
        user["waiting"] = payload
        await db.save_user(user_id, user)
    await db.log_payment(user_id, payload, stars_equiv, "RUB")

    referrer_id = user.get("referred_by")
    if referrer_id:
        bonus = max(1, round(stars_equiv * REF_BONUS_PERCENT / 100))
        try:
            await db.add_ref_bonus(referrer_id, user_id, bonus, payload)
            buyer_name = user.get("first_name") or ("Друг" if db.is_male(user) else "Подруга")
            bought_verb = "купил" if db.is_male(user) else "купила"
            title_ref  = TITLES.get(payload, "разбор")
            await _bot.send_message(
                referrer_id,
                f"🎉 +{bonus} ⭐ на твой баланс!\n\n"
                f"{buyer_name} {bought_verb} «{title_ref}» по твоей реферальной ссылке.\n"
                f"Проверить баланс: /balance"
            )
        except Exception as e:
            logging.warning(f"yookassa reading ref bonus error: {e}")

    title = PAID_RAZBORY[payload]
    # compat (две даты) и business_name (название) нельзя запускать датным
    # меню «для себя/другая дата» — им нужен свой ввод. Для них шлём кнопку
    # «Начать разбор» (startreading_ роутит правильно в bot.py). Обычным
    # датным разборам оставляем привычное date_choice_menu.
    try:
        if payload in TWO_DATE_KEYS or payload == "business_name":
            from aiogram.types import InlineKeyboardMarkup as _IKM, InlineKeyboardButton as _IKB
            await _bot.send_message(
                user_id,
                f"✅ Оплата «{title}» прошла! Нажми кнопку, чтобы начать 👇",
                reply_markup=_IKM(inline_keyboard=[[
                    _IKB(text="▶️ Начать разбор", callback_data=f"startreading_{payload}")
                ]])
            )
        else:
            await _bot.send_message(
                user_id,
                f"✅ Оплата «{title}» прошла!\n\n"
                f"Делаешь разбор для себя ({user['birth_date']}) или введёшь другую дату?"
                if user.get("birth_date") else
                f"✅ Оплата «{title}» прошла! Открой чат со мной и укажи дату рождения.",
                reply_markup=date_choice_menu() if user.get("birth_date") else None
            )
    except Exception as e:
        logging.warning(f"yookassa reading notify error: {e}")
    return web.Response(status=200)

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
    app.router.add_post("/api/buy_rub/{key}", api_buy_rub)
    app.router.add_get("/api/reading/{key}", api_reading)
    app.router.add_post("/api/reading/{key}/generate", api_reading_generate)
    app.router.add_post("/api/premium/buy", api_premium_buy)
    app.router.add_post("/api/ask", api_ask)
    app.router.add_get("/api/day_number", api_day_number)
    app.router.add_get("/api/destiny_matrix", api_destiny_matrix)
    app.router.add_get("/api/psychomatrix", api_psychomatrix)
    app.router.add_post("/api/yesno", api_yesno)
    app.router.add_post("/webhook/yookassa", yookassa_webhook)
    app.router.add_post("/api/profile_digest", api_profile_digest)
    app.router.add_get("/api/referral", api_referral)
    app.router.add_get("/api/achievements", api_achievements)
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
