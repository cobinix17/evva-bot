# db.py — работа с PostgreSQL: пользователи и купоны.
# Использует пул соединений asyncpg, создаваемый в init_db().
# Зависит от config.py (PAID_RAZBORY) для авто-выдачи всех разборов админу.
import json
import asyncio
import logging
from collections import defaultdict
from datetime import timedelta, datetime, timezone

import asyncpg

from config import PAID_RAZBORY, ADMIN_ID

db_pool = None

# Пер-пользовательские блокировки для критических секций «прочитать-проверить-
# списать-сохранить» (оплата балансом). spend_balance атомарен сам по себе, но
# проверка «ещё не куплено» и сохранение purchased идут отдельными шагами —
# без этого замка двойной клик мог бы списать баланс дважды за один разбор.
# Бот и веб в одном процессе, поэтому один и тот же lock их сериализует.
_user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

def user_lock(user_id: int) -> asyncio.Lock:
    return _user_locks[user_id]

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

# ─── ИНИЦИАЛИЗАЦИЯ ───────────────────────────────────────────────────────────
async def init_db(database_url: str):
    global db_pool
    # min_size=5 держит соединения тёплыми, max_size=20 даёт запас на всплески
    # (дефолт asyncpg — всего 10). Пул переиспользуется, лишние закрываются.
    db_pool = await asyncpg.create_pool(database_url, min_size=5, max_size=20)
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
            review_left        BOOLEAN DEFAULT FALSE,
            notifications      BOOLEAN DEFAULT TRUE,
            reviews_left       TEXT DEFAULT '[]',
            ref_balance        INTEGER DEFAULT 0,
            referred_by        BIGINT,
            email              TEXT
        )
    ''')
    await db_pool.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT')
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS referrals (
            id          SERIAL PRIMARY KEY,
            referrer_id BIGINT NOT NULL,
            referred_id BIGINT NOT NULL UNIQUE,
            joined_at   TIMESTAMP DEFAULT NOW()
        )
    ''')
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS ref_bonuses (
            id          SERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL,
            from_user   BIGINT NOT NULL,
            amount      INTEGER NOT NULL,
            razbor_key  TEXT,
            created_at  TIMESTAMP DEFAULT NOW()
        )
    ''')
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS coupons (
            code       TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT NOW(),
            expires_at TIMESTAMP,
            max_uses   INTEGER DEFAULT 1,
            uses_count INTEGER DEFAULT 0
        )
    ''')
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS coupon_uses (
            id         SERIAL PRIMARY KEY,
            code       TEXT NOT NULL,
            user_id    BIGINT NOT NULL,
            used_at    TIMESTAMP DEFAULT NOW()
        )
    ''')
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS pending_reviews (
            id          SERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL,
            review_text TEXT NOT NULL,
            flags       TEXT DEFAULT '',
            created_at  TIMESTAMP DEFAULT NOW()
        )
    ''')
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS reading_followups (
            user_id    BIGINT NOT NULL,
            razbor_key TEXT   NOT NULL,
            count      INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, razbor_key)
        )
    ''')
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS generated_readings (
            user_id    BIGINT NOT NULL,
            razbor_key TEXT   NOT NULL,
            title      TEXT   NOT NULL,
            text       TEXT   NOT NULL,
            date_str   TEXT,
            updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (user_id, razbor_key)
        )
    ''')
    await db_pool.execute('''
        ALTER TABLE generated_readings ADD COLUMN IF NOT EXISTS date_str TEXT
    ''')
    # Раньше ключом было (user_id, razbor_key), поэтому разбор на ДРУГУЮ дату
    # затирал уже купленный: человек смотрел дату подруги и терял свой разбор
    # навсегда. Дата входит в ключ — тексты на разные даты живут параллельно.
    # date_str NOT NULL обязателен для первичного ключа, старые NULL → ''.
    try:
        await db_pool.execute("UPDATE generated_readings SET date_str = '' WHERE date_str IS NULL")
        await db_pool.execute("ALTER TABLE generated_readings ALTER COLUMN date_str SET DEFAULT ''")
        await db_pool.execute("ALTER TABLE generated_readings ALTER COLUMN date_str SET NOT NULL")
        pk = await db_pool.fetchval('''
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'generated_readings'::regclass AND contype = 'p'
        ''')
        # Меняем ключ только если он ещё старый (две колонки вместо трёх).
        cols = await db_pool.fetchval('''
            SELECT COUNT(*) FROM pg_constraint c
            JOIN unnest(c.conkey) AS k ON TRUE
            WHERE c.conrelid = 'generated_readings'::regclass AND c.contype = 'p'
        ''')
        if pk and cols == 2:
            await db_pool.execute(f'ALTER TABLE generated_readings DROP CONSTRAINT "{pk}"')
            await db_pool.execute(
                'ALTER TABLE generated_readings '
                'ADD PRIMARY KEY (user_id, razbor_key, date_str)'
            )
            logging.info("generated_readings: ключ расширен датой — разборы больше не затираются")
    except Exception as e:
        logging.warning(f"миграция ключа generated_readings не выполнена: {e}")
    try:
        await db_pool.execute(
            "CREATE INDEX IF NOT EXISTS generated_readings_recent_idx "
            "ON generated_readings (user_id, razbor_key, updated_at DESC)"
        )
    except Exception as e:
        logging.warning(f"generated_readings_recent_idx не создан: {e}")
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS feedback (
            id         SERIAL PRIMARY KEY,
            user_id    BIGINT NOT NULL,
            text       TEXT NOT NULL,
            category   TEXT DEFAULT 'idea',
            created_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    await db_pool.execute('''
        ALTER TABLE feedback ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'idea'
    ''')
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS gifts (
            code         TEXT PRIMARY KEY,
            razbor_key   TEXT   NOT NULL,
            from_user_id BIGINT NOT NULL,
            redeemed_by  BIGINT,
            created_at   TIMESTAMP DEFAULT NOW(),
            redeemed_at  TIMESTAMP
        )
    ''')
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS yookassa_payments (
            payment_id TEXT PRIMARY KEY,
            user_id    BIGINT NOT NULL,
            payload    TEXT   NOT NULL,
            amount_rub INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id          SERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL,
            razbor_key  TEXT,
            amount_xtr  INTEGER NOT NULL,
            currency    TEXT NOT NULL,
            created_at  TIMESTAMP DEFAULT NOW()
        )
    ''')
    # Простое key-value хранилище настроек (скидка/акция и т.п.) — переживает
    # рестарты Railway, в отличие от переменных в памяти процесса.
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    for col, definition in [
        ("first_name",    "TEXT"),
        ("notifications", "BOOLEAN DEFAULT TRUE"),
        ("reviews_left",  "TEXT DEFAULT '[]'"),
        ("ref_balance",   "INTEGER DEFAULT 0"),
        ("referred_by",   "BIGINT"),
        ("premium_until", "TIMESTAMP"),
        ("prem_day",       "DATE"),
        ("prem_day_count", "INTEGER DEFAULT 0"),
        ("prem_month",     "TEXT"),
        ("prem_month_count","INTEGER DEFAULT 0"),
        ("ask_day",       "DATE"),
        ("ask_day_count", "INTEGER DEFAULT 0"),
        ("yesno_day",       "DATE"),
        ("yesno_day_count", "INTEGER DEFAULT 0"),
        ("created_at",    "TIMESTAMP DEFAULT NOW()"),
        ("last_spin_date", "DATE"),
        ("gender",        "TEXT"),
        ("regen_day",       "DATE"),
        ("regen_day_count", "INTEGER DEFAULT 0"),
    ]:
        try:
            await db_pool.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {definition}")
        except Exception:
            pass
    for col, definition in [
        ("max_uses",   "INTEGER DEFAULT 1"),
        ("uses_count", "INTEGER DEFAULT 0"),
        # FALSE (по умолчанию) — публичный промокод: КАЖДЫЙ юзер может
        # активировать его лишь один раз, пока не кончится общий лимит.
        # TRUE — личный/тестовый: один и тот же человек может активировать
        # повторно (так админ выдаёт доступ своему второму аккаунту).
        ("multi_per_user", "BOOLEAN DEFAULT FALSE"),
    ]:
        try:
            await db_pool.execute(f"ALTER TABLE coupons ADD COLUMN IF NOT EXISTS {col} {definition}")
        except Exception:
            pass
    try:
        await db_pool.execute("ALTER TABLE coupons DROP COLUMN IF EXISTS used_by")
    except Exception:
        pass
    # once=TRUE помечает активацию публичного промокода. Частичный уникальный
    # индекс именно по таким строкам не даёт одному юзеру активировать
    # публичный код дважды даже при гонке из двух параллельных запросов,
    # а активациям личных кодов (once=FALSE) повторяться не мешает.
    try:
        await db_pool.execute("ALTER TABLE coupon_uses ADD COLUMN IF NOT EXISTS once BOOLEAN DEFAULT TRUE")
    except Exception:
        pass
    # В базе, созданной ДО появления режимов, один юзер мог активировать код
    # несколько раз — такие дубли не дали бы создать уникальный индекс. Не
    # удаляем историю, а помечаем все повторы кроме первого как once=FALSE:
    # индекс после этого строится, а старые активации остаются видны в статистике.
    try:
        await db_pool.execute('''
            UPDATE coupon_uses SET once = FALSE
            WHERE once AND id NOT IN (
                SELECT MIN(id) FROM coupon_uses GROUP BY code, user_id
            )
        ''')
    except Exception as e:
        logging.warning(f"дедупликация coupon_uses не выполнена: {e}")
    try:
        await db_pool.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS coupon_uses_once_uniq "
            "ON coupon_uses (code, user_id) WHERE once"
        )
    except Exception as e:
        # Даже если индекс почему-то не создался, защиту держит явная
        # проверка в use_coupon — она не зависит от наличия индекса.
        logging.warning(f"coupon_uses_once_uniq не создан: {e}")
    # Индексы под запросы, которые иначе сканируют таблицу целиком.
    for idx, ddl in [
        ("referrals_referrer_idx",  "CREATE INDEX IF NOT EXISTS referrals_referrer_idx ON referrals (referrer_id)"),
        ("ref_bonuses_user_idx",    "CREATE INDEX IF NOT EXISTS ref_bonuses_user_idx ON ref_bonuses (user_id)"),
        ("coupon_uses_code_idx",    "CREATE INDEX IF NOT EXISTS coupon_uses_code_idx ON coupon_uses (code)"),
        ("payments_user_idx",       "CREATE INDEX IF NOT EXISTS payments_user_idx ON payments (user_id)"),
    ]:
        try:
            await db_pool.execute(ddl)
        except Exception as e:
            logging.warning(f"Индекс {idx} не создан: {e}")

# ─── ПОЛЬЗОВАТЕЛИ ────────────────────────────────────────────────────────────
async def user_exists(user_id: int) -> bool:
    """Существует ли пользователь в БД ДО автосоздания строки в get_user.
    Нужно чтобы отличить первый /start от повторного — реферала можно
    засчитать только по-настоящему новому пользователю."""
    row = await db_pool.fetchval('SELECT 1 FROM users WHERE user_id = $1', user_id)
    return row is not None

async def get_user(user_id: int) -> dict:
    row = await db_pool.fetchrow('SELECT * FROM users WHERE user_id = $1', user_id)
    if not row:
        await db_pool.execute('INSERT INTO users (user_id) VALUES ($1)', user_id)
        row = await db_pool.fetchrow('SELECT * FROM users WHERE user_id = $1', user_id)
    user = dict(row)
    user['purchased']    = json.loads(user['purchased'] or '[]')
    user['reviews_left'] = json.loads(user.get('reviews_left') or '[]')
    if user.get('notifications') is None:
        user['notifications'] = True
    if user_id == ADMIN_ID:
        user['free_used']          = True
        user['subscribed_channel'] = True
        user['premium_until']      = utc_now() + timedelta(days=3650)
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
            birth_date          = $4,
            destiny_number      = $5,
            purchased           = $6,
            waiting             = $7,
            review_left         = $8,
            notifications       = $9,
            reviews_left        = $10,
            ref_balance         = $11,
            referred_by         = $12
        WHERE user_id = $13
    ''',
        user.get('first_name'),
        user['free_used'],
        user['subscribed_channel'],
        user.get('birth_date'),
        user.get('destiny_number'),
        json.dumps(user['purchased']),
        user.get('waiting'),
        user.get('review_left', False),
        user.get('notifications', True),
        json.dumps(user.get('reviews_left', [])),
        user.get('ref_balance', 0),
        user.get('referred_by'),
        user_id
    )

async def set_gender(user_id: int, gender: str):
    """Пол для обращений: 'f' (по умолчанию) или 'm'. Отдельный setter, как
    set_email — чтобы save_user из вебхуков/старых флоу не затирал значение."""
    await db_pool.execute('UPDATE users SET gender = $1 WHERE user_id = $2', gender, user_id)

async def get_setting(key: str, default: str | None = None) -> str | None:
    row = await db_pool.fetchval('SELECT value FROM app_settings WHERE key = $1', key)
    return row if row is not None else default

async def set_setting(key: str, value: str):
    await db_pool.execute(
        '''INSERT INTO app_settings (key, value) VALUES ($1, $2)
           ON CONFLICT (key) DO UPDATE SET value = $2''',
        key, value
    )

def is_male(user: dict) -> bool:
    """Мужское обращение. NULL/отсутствие колонки = женское (основная аудитория,
    существующие пользователи ничего не заметят)."""
    return (user.get("gender") or "f") == "m"

def default_name(user: dict) -> str:
    """Имя для обращения с фолбэком по полу — вместо разбросанных по коду
    `user.get("first_name") or "дорогая"`."""
    return user.get("first_name") or ("дорогой" if is_male(user) else "дорогая")

async def set_email(user_id: int, email: str):
    """Сохраняет email для чеков ЮKassa — чтобы не спрашивать заново на
    каждой рублёвой оплате (см. handle_rub_email в bot.py)."""
    await db_pool.execute('UPDATE users SET email = $1 WHERE user_id = $2', email, user_id)

# ─── ПОДАРКИ ─────────────────────────────────────────────────────────────────
async def create_gift(code: str, razbor_key: str, from_user_id: int):
    await db_pool.execute(
        'INSERT INTO gifts (code, razbor_key, from_user_id) VALUES ($1, $2, $3)',
        code, razbor_key, from_user_id
    )

async def get_gift(code: str) -> dict | None:
    row = await db_pool.fetchrow('SELECT * FROM gifts WHERE code = $1', code)
    return dict(row) if row else None

async def redeem_gift(code: str, user_id: int) -> str | None:
    """Атомарно отмечает подарок забранным — тот же guard-в-WHERE паттерн:
    если redeemed_by уже не NULL, повторный клик по той же ссылке ничего
    не сделает. Возвращает razbor_key при успехе, None если код не найден
    или уже использован."""
    row = await db_pool.fetchrow(
        '''UPDATE gifts SET redeemed_by = $1, redeemed_at = NOW()
           WHERE code = $2 AND redeemed_by IS NULL
           RETURNING razbor_key''',
        user_id, code
    )
    return row['razbor_key'] if row else None

async def mark_yookassa_payment(payment_id: str, user_id: int, payload: str, amount_rub: int) -> bool:
    """INSERT ... ON CONFLICT DO NOTHING — если этот payment_id уже обработан
    (ЮKassa шлёт вебхук с повтором, пока не получит 200), возвращает False,
    и вызывающий код не выдаёт премиум/разбор второй раз."""
    result = await db_pool.execute(
        '''INSERT INTO yookassa_payments (payment_id, user_id, payload, amount_rub)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (payment_id) DO NOTHING''',
        payment_id, user_id, payload, amount_rub
    )
    return result == "INSERT 0 1"

# ─── КУПОНЫ ──────────────────────────────────────────────────────────────────
async def create_coupon(code: str, max_uses: int = 1, multi_per_user: bool = False) -> str:
    """multi_per_user=False — публичный код (каждому по одной активации),
    True — личный/тестовый (один человек может активировать много раз)."""
    expires = utc_now() + timedelta(hours=48)
    try:
        await db_pool.execute(
            'INSERT INTO coupons (code, expires_at, max_uses, multi_per_user) VALUES ($1, $2, $3, $4)',
            code.upper(), expires, max_uses, multi_per_user
        )
        return 'ok'
    except asyncpg.UniqueViolationError:
        return 'exists'
    except Exception as e:
        logging.error(f"create_coupon error: {e}", exc_info=True)
        return 'error'

async def use_coupon(code: str, user_id: int) -> str:
    """Активирует промокод. Два режима (см. coupons.multi_per_user):

    • публичный (multi_per_user = FALSE) — каждый юзер активирует код ровно
      один раз, общий лимит max_uses делится между разными людьми. Раньше
      этого ограничения не было, и один человек мог выгрести весь купон;
    • личный/тестовый (multi_per_user = TRUE) — один и тот же человек может
      активировать повторно, пока не кончится max_uses (так админ выдаёт
      доступ своему второму аккаунту).

    Возвращает 'ok' | 'not_found' | 'expired' | 'limit' | 'used'."""
    code = code.upper()
    row = await db_pool.fetchrow('SELECT * FROM coupons WHERE code = $1', code)
    if not row:
        return 'not_found'
    if row['expires_at'] and row['expires_at'] < utc_now():
        return 'expired'
    once = not row.get('multi_per_user')

    if once:
        # Явная проверка — работает даже если уникальный индекс не создался
        # (старая база с дублями), поэтому не полагаемся только на ON CONFLICT.
        already = await db_pool.fetchval(
            'SELECT 1 FROM coupon_uses WHERE code = $1 AND user_id = $2 AND once',
            code, user_id
        )
        if already:
            return 'used'
        # «Занимаем» активацию за юзером. Уникальный частичный индекс делает
        # это неделимым: параллельный повтор не вставится и получит 'used'.
        claimed = await db_pool.execute(
            '''INSERT INTO coupon_uses (code, user_id, once) VALUES ($1, $2, TRUE)
               ON CONFLICT DO NOTHING''',
            code, user_id
        )
        if claimed != "INSERT 0 1":
            return 'used'

    updated = await db_pool.fetchval(
        '''UPDATE coupons SET uses_count = uses_count + 1
           WHERE code = $1 AND uses_count < max_uses
           RETURNING uses_count''',
        code
    )
    if updated is None:
        if once:
            # Лимит кода исчерпан — снимаем только что занятую активацию,
            # иначе юзер остался бы «использовавшим» код, ничего не получив.
            await db_pool.execute(
                'DELETE FROM coupon_uses WHERE code = $1 AND user_id = $2 AND once',
                code, user_id
            )
        return 'limit'

    if not once:
        await db_pool.execute(
            'INSERT INTO coupon_uses (code, user_id, once) VALUES ($1, $2, FALSE)',
            code, user_id
        )
    return 'ok'

# ─── РЕФЕРАЛЫ ────────────────────────────────────────────────────────────────
async def register_referral(referrer_id: int, referred_id: int):
    """Записывает реферальную связь если её ещё нет."""
    try:
        await db_pool.execute(
            'INSERT INTO referrals (referrer_id, referred_id) VALUES ($1, $2) ON CONFLICT DO NOTHING',
            referrer_id, referred_id
        )
        await db_pool.execute(
            'UPDATE users SET referred_by = $1 WHERE user_id = $2 AND referred_by IS NULL',
            referrer_id, referred_id
        )
    except Exception as e:
        logging.error(f"register_referral error: {e}")

async def spend_balance(user_id: int, amount: int) -> bool:
    """Атомарно списывает бонусные звёзды с баланса, если их хватает.
    Условие ref_balance >= amount проверяется прямо в WHERE, поэтому
    два параллельных списания не могут увести баланс в минус."""
    result = await db_pool.fetchval(
        '''UPDATE users SET ref_balance = ref_balance - $1
           WHERE user_id = $2 AND ref_balance >= $1
           RETURNING ref_balance''',
        amount, user_id
    )
    return result is not None

async def daily_spin_try(user_id: int, amount: int) -> bool:
    """Атомарно начисляет ежедневный бонус и отмечает дату — тот же неделимый
    UPDATE-с-guard паттерн, что и в ask_try_consume: гонка из двух быстрых
    нажатий кнопки не даст начислить дважды за один день."""
    today = utc_now().date()
    result = await db_pool.fetchval(
        '''UPDATE users SET
               ref_balance    = ref_balance + $1,
               last_spin_date = $2
           WHERE user_id = $3
             AND (last_spin_date IS NULL OR last_spin_date < $2)
           RETURNING ref_balance''',
        amount, today, user_id
    )
    return result is not None

async def add_ref_bonus(referrer_id: int, from_user_id: int, amount: int, razbor_key: str):
    """Начисляет виртуальные звёзды рефереру и записывает в историю."""
    await db_pool.execute(
        'UPDATE users SET ref_balance = ref_balance + $1 WHERE user_id = $2',
        amount, referrer_id
    )
    await db_pool.execute(
        'INSERT INTO ref_bonuses (user_id, from_user, amount, razbor_key) VALUES ($1, $2, $3, $4)',
        referrer_id, from_user_id, amount, razbor_key
    )

async def get_referral_stats(user_id: int) -> dict:
    """Статистика по рефералам: количество приглашённых, суммарно заработано."""
    count = await db_pool.fetchval(
        'SELECT COUNT(*) FROM referrals WHERE referrer_id = $1', user_id
    )
    earned = await db_pool.fetchval(
        'SELECT COALESCE(SUM(amount), 0) FROM ref_bonuses WHERE user_id = $1', user_id
    )
    balance = await db_pool.fetchval(
        'SELECT ref_balance FROM users WHERE user_id = $1', user_id
    )
    bonuses = await db_pool.fetch(
        '''SELECT rb.amount, rb.razbor_key, rb.created_at, u.first_name
           FROM ref_bonuses rb
           LEFT JOIN users u ON u.user_id = rb.from_user
           WHERE rb.user_id = $1
           ORDER BY rb.created_at DESC LIMIT 10''',
        user_id
    )
    return {
        'count':   count or 0,
        'earned':  earned or 0,
        'balance': balance or 0,
        'bonuses': bonuses,
    }

async def coupon_remaining(code: str) -> int:
    row = await db_pool.fetchrow('SELECT max_uses, uses_count FROM coupons WHERE code = $1', code.upper())
    if not row:
        return 0
    return max(0, row['max_uses'] - row['uses_count'])

# ─── МОДЕРАЦИЯ ОТЗЫВОВ ───────────────────────────────────────────────────────
async def add_pending_review(user_id: int, review_text: str, flags: str = "") -> int:
    """Кладёт отзыв в очередь на модерацию, возвращает id записи —
    он же используется в callback_data кнопок Одобрить/Отклонить."""
    return await db_pool.fetchval(
        'INSERT INTO pending_reviews (user_id, review_text, flags) VALUES ($1, $2, $3) RETURNING id',
        user_id, review_text, flags
    )

async def get_pending_review(review_id: int) -> dict | None:
    row = await db_pool.fetchrow('SELECT * FROM pending_reviews WHERE id = $1', review_id)
    return dict(row) if row else None

async def delete_pending_review(review_id: int):
    await db_pool.execute('DELETE FROM pending_reviews WHERE id = $1', review_id)

# ─── ПРЕМИУМ-ПОДПИСКА ────────────────────────────────────────────────────────
async def set_premium(user_id: int, until: datetime):
    """Продлевает премиум до указанной даты. Пишется отдельно от save_user,
    чтобы обычное сохранение пользователя не могло случайно затереть подписку
    (save_user не трогает колонку premium_until)."""
    await db_pool.execute(
        'UPDATE users SET premium_until = $1 WHERE user_id = $2', until, user_id
    )

def is_premium(user: dict) -> bool:
    """Активна ли подписка. Работает с dict из get_user."""
    pu = user.get("premium_until")
    return pu is not None and pu > utc_now()

async def premium_try_consume(user_id: int, daily_limit: int, monthly_limit: int) -> str:
    """Атомарно пытается списать один слот открытия нового разбора по подписке.
    Счётчики хранятся в БД (переживают перезапуск бота), период сбрасывается
    сам: новый день обнуляет дневной счётчик, новый месяц — месячный.
    Возвращает 'ok' если слот списан, 'day' или 'month' если лимит исчерпан.

    Один UPDATE с guard в WHERE делает проверку и инкремент неделимыми —
    два быстрых нажатия не могут проскочить лимит."""
    today = utc_now().date()
    month = today.strftime("%Y-%m")
    row = await db_pool.fetchrow(
        '''
        UPDATE users SET
            prem_day_count   = CASE WHEN prem_day  = $2 THEN prem_day_count  + 1 ELSE 1 END,
            prem_month_count = CASE WHEN prem_month = $3 THEN prem_month_count + 1 ELSE 1 END,
            prem_day   = $2,
            prem_month = $3
        WHERE user_id = $1
          AND NOT (prem_day  = $2 AND prem_day_count  >= $4)
          AND NOT (prem_month = $3 AND prem_month_count >= $5)
        RETURNING prem_day_count
        ''',
        user_id, today, month, daily_limit, monthly_limit
    )
    if row is not None:
        return 'ok'
    # слот не списан — выясняем, какой лимит уперся, для точного сообщения
    cur = await db_pool.fetchrow(
        'SELECT prem_day, prem_day_count, prem_month, prem_month_count FROM users WHERE user_id = $1',
        user_id
    )
    if cur and cur['prem_day'] == today and cur['prem_day_count'] >= daily_limit:
        return 'day'
    return 'month'

async def ask_try_consume(user_id: int, daily_limit: int) -> bool:
    """Атомарно списывает один вопрос из дневного лимита AI-чата «Спроси Еву».
    Тот же паттерн неделимого UPDATE, что и в premium_try_consume — проверка
    и инкремент одним запросом, гонка из двух быстрых сообщений исключена."""
    today = utc_now().date()
    row = await db_pool.fetchrow(
        '''
        UPDATE users SET
            ask_day_count = CASE WHEN ask_day = $2 THEN ask_day_count + 1 ELSE 1 END,
            ask_day       = $2
        WHERE user_id = $1
          AND NOT (ask_day = $2 AND ask_day_count >= $3)
        RETURNING ask_day_count
        ''',
        user_id, today, daily_limit
    )
    return row is not None

async def regen_try_consume(user_id: int, daily_limit: int) -> bool:
    """Атомарно списывает одну ПОВТОРНУЮ генерацию разбора (на другую дату).
    Первая генерация после покупки лимит не трогает — она уже оплачена.

    Без этого лимита купивший один разбор за 49 ⭐ мог гонять генерацию
    бесконечно, подставляя новые даты: каждый прогон — полный запрос к ИИ,
    так что счёт за токены рос, а выручка нет. Тот же неделимый
    UPDATE-с-guard, что и в ask_try_consume."""
    today = utc_now().date()
    row = await db_pool.fetchrow(
        '''
        UPDATE users SET
            regen_day_count = CASE WHEN regen_day = $2 THEN regen_day_count + 1 ELSE 1 END,
            regen_day       = $2
        WHERE user_id = $1
          AND NOT (regen_day = $2 AND regen_day_count >= $3)
        RETURNING regen_day_count
        ''',
        user_id, today, daily_limit
    )
    return row is not None

async def refund_regen_try(user_id: int):
    """Возвращает списанную регенерацию, если ИИ так и не отдал текст."""
    today = utc_now().date()
    await db_pool.execute(
        '''UPDATE users SET regen_day_count = GREATEST(regen_day_count - 1, 0)
           WHERE user_id = $1 AND regen_day = $2''',
        user_id, today
    )

async def regen_left(user_id: int, daily_limit: int) -> int:
    """Сколько повторных генераций осталось сегодня (для текста подсказки)."""
    row = await db_pool.fetchrow(
        'SELECT regen_day, regen_day_count FROM users WHERE user_id = $1', user_id
    )
    if not row or row['regen_day'] != utc_now().date():
        return daily_limit
    return max(0, daily_limit - (row['regen_day_count'] or 0))

async def refund_ask_try(user_id: int):
    """Возвращает списанный вопрос дневного лимита, если генерация не удалась —
    чтобы сбой AI-провайдера не сжигал лимит пользователя впустую."""
    today = utc_now().date()
    await db_pool.execute(
        '''UPDATE users SET ask_day_count = GREATEST(ask_day_count - 1, 0)
           WHERE user_id = $1 AND ask_day = $2''',
        user_id, today
    )

async def yesno_try_consume(user_id: int, daily_limit: int) -> bool:
    """Атомарно списывает один вопрос из дневного лимита «Да/Нет» — тот же
    неделимый UPDATE-с-guard, что и ask_try_consume. Премиум лимит не трогает
    (вызывающий код просто не проверяет его для премиума)."""
    today = utc_now().date()
    row = await db_pool.fetchrow(
        '''
        UPDATE users SET
            yesno_day_count = CASE WHEN yesno_day = $2 THEN yesno_day_count + 1 ELSE 1 END,
            yesno_day       = $2
        WHERE user_id = $1
          AND NOT (yesno_day = $2 AND yesno_day_count >= $3)
        RETURNING yesno_day_count
        ''',
        user_id, today, daily_limit
    )
    return row is not None

async def refund_yesno_try(user_id: int):
    """Возвращает списанный вопрос «Да/Нет», если генерация упала — сбой ИИ не
    должен сжигать дневной лимит."""
    today = utc_now().date()
    await db_pool.execute(
        '''UPDATE users SET yesno_day_count = GREATEST(yesno_day_count - 1, 0)
           WHERE user_id = $1 AND yesno_day = $2''',
        user_id, today
    )

async def followup_try_consume(user_id: int, razbor_key: str, limit: int) -> bool:
    """Атомарно списывает один бесплатный уточняющий вопрос по КОНКРЕТНОМУ
    разбору. INSERT ... ON CONFLICT с guard в WHERE — та же неделимая
    проверка-и-инкремент, что в premium_try_consume/ask_try_consume, только
    ключ здесь составной (user_id, razbor_key), а не по дате."""
    row = await db_pool.fetchrow(
        '''
        INSERT INTO reading_followups (user_id, razbor_key, count)
        VALUES ($1, $2, 1)
        ON CONFLICT (user_id, razbor_key) DO UPDATE
            SET count = reading_followups.count + 1
            WHERE reading_followups.count < $3
        RETURNING count
        ''',
        user_id, razbor_key, limit
    )
    return row is not None

async def save_reading_text(user_id: int, razbor_key: str, title: str, text: str, date_str: str = None):
    """Сохраняет готовый текст разбора — чтобы веб-кабинет мог показать его
    напрямую, не отправляя пользователя каждый раз в чат с ботом, и чтобы
    повторный запрос того же разбора на ТУ ЖЕ дату не порождал новый
    (потенциально противоречивый) текст. date_str — дата(ы), с которой
    сгенерирован текст. Дата входит в ключ: разбор на ДРУГУЮ дату сохраняется
    отдельной записью и не затирает уже сделанный (раньше затирал — человек
    смотрел дату подруги и терял свой купленный разбор)."""
    await db_pool.execute(
        '''INSERT INTO generated_readings (user_id, razbor_key, title, text, date_str)
           VALUES ($1, $2, $3, $4, COALESCE($5, ''))
           ON CONFLICT (user_id, razbor_key, date_str) DO UPDATE
               SET title = $3, text = $4, updated_at = NOW()''',
        user_id, razbor_key, title, text, date_str
    )

async def get_reading_text(user_id: int, razbor_key: str, date_str: str | None = None) -> dict | None:
    """Сохранённый текст разбора. date_str=None — самый свежий по этому
    разбору (так работают все экраны «показать мой разбор»); с датой —
    ровно та версия, если она уже генерировалась (кэш в generation.py)."""
    if date_str is not None:
        row = await db_pool.fetchrow(
            'SELECT title, text, date_str, updated_at FROM generated_readings '
            'WHERE user_id = $1 AND razbor_key = $2 AND date_str = $3',
            user_id, razbor_key, date_str
        )
        return dict(row) if row else None
    row = await db_pool.fetchrow(
        'SELECT title, text, date_str, updated_at FROM generated_readings '
        'WHERE user_id = $1 AND razbor_key = $2 ORDER BY updated_at DESC LIMIT 1',
        user_id, razbor_key
    )
    return dict(row) if row else None

async def has_reading(user_id: int, razbor_key: str) -> bool:
    """Есть ли у юзера хоть один готовый текст этого разбора. По этому признаку
    отличаем первую (уже оплаченную) генерацию от повторной на другую дату."""
    return await db_pool.fetchval(
        'SELECT 1 FROM generated_readings WHERE user_id = $1 AND razbor_key = $2 LIMIT 1',
        user_id, razbor_key
    ) is not None

async def list_reading_dates(user_id: int, razbor_key: str) -> list[dict]:
    """Все даты, на которые юзер уже делал этот разбор — свежие сверху."""
    rows = await db_pool.fetch(
        'SELECT date_str, updated_at FROM generated_readings '
        'WHERE user_id = $1 AND razbor_key = $2 ORDER BY updated_at DESC',
        user_id, razbor_key
    )
    return [dict(r) for r in rows]

async def get_reading_texts(user_id: int, keys: list[str]) -> dict[str, dict]:
    """Пакетно достаёт тексты нескольких разборов одним запросом (вместо N
    отдельных get_reading_text в цикле — см. api_profile_digest). Возвращает
    {razbor_key: {title, text, date_str}}."""
    if not keys:
        return {}
    # DISTINCT ON — по каждому разбору берём только самую свежую версию:
    # с тех пор как дата входит в ключ, строк на один razbor_key может быть
    # несколько (разбор на себя, на подругу и т.д.).
    rows = await db_pool.fetch(
        'SELECT DISTINCT ON (razbor_key) razbor_key, title, text, date_str '
        'FROM generated_readings WHERE user_id = $1 AND razbor_key = ANY($2) '
        'ORDER BY razbor_key, updated_at DESC',
        user_id, keys
    )
    return {r["razbor_key"]: dict(r) for r in rows}

async def get_all_reading_texts(user_id: int) -> list[dict]:
    """Все сохранённые разборы пользователя (для «Спроси Еву» как памяти о том,
    что уже разбирали). Свежие сверху. По одному (самому свежему) тексту на
    разбор — иначе после генерации на другую дату Ева получала бы в память
    несколько версий одного разбора и путалась в них."""
    rows = await db_pool.fetch(
        'SELECT DISTINCT ON (razbor_key) razbor_key, title, text, date_str, updated_at '
        'FROM generated_readings WHERE user_id = $1 '
        'ORDER BY razbor_key, updated_at DESC',
        user_id
    )
    return sorted((dict(r) for r in rows), key=lambda r: r["updated_at"], reverse=True)

async def add_feedback(user_id: int, text: str, category: str = "idea") -> int:
    return await db_pool.fetchval(
        'INSERT INTO feedback (user_id, text, category) VALUES ($1, $2, $3) RETURNING id',
        user_id, text, category
    )

async def list_feedback(limit: int = 10) -> list:
    rows = await db_pool.fetch(
        '''SELECT f.id, f.user_id, f.text, f.category, f.created_at, u.first_name
           FROM feedback f LEFT JOIN users u ON u.user_id = f.user_id
           ORDER BY f.created_at DESC LIMIT $1''',
        limit
    )
    return [dict(r) for r in rows]

async def log_payment(user_id: int, razbor_key: str | None, amount_xtr: int, currency: str):
    """Лог РЕАЛЬНОЙ денежной оплаты (Stars или рубли через ЮKassa) —
    отдельно от users.purchased, куда попадают и бесплатные способы
    получения разбора (купон, разблокировка по подписке, баланс).
    Статистика в /admin (выручка, "всего покупок") считается по этой
    таблице, чтобы промокоды и премиум-безлимит не накручивали выручку.
    razbor_key=None — оплата премиума. amount_xtr — сумма в звёздах-
    эквиваленте (для RUB уже нормализована вызывающим кодом)."""
    await db_pool.execute(
        'INSERT INTO payments (user_id, razbor_key, amount_xtr, currency) VALUES ($1, $2, $3, $4)',
        user_id, razbor_key, amount_xtr, currency
    )

async def premium_stats() -> dict:
    active = await db_pool.fetchval(
        'SELECT COUNT(*) FROM users WHERE premium_until IS NOT NULL AND premium_until > NOW()'
    )
    ever = await db_pool.fetchval(
        'SELECT COUNT(*) FROM users WHERE premium_until IS NOT NULL'
    )
    return {"active": active or 0, "ever": ever or 0}
