# db.py — работа с PostgreSQL: пользователи и купоны.
# Использует пул соединений asyncpg, создаваемый в init_db().
# Зависит от config.py (PAID_RAZBORY) для авто-выдачи всех разборов админу.
import json
import logging
from datetime import timedelta, datetime, timezone

import asyncpg

from config import PAID_RAZBORY, ADMIN_ID

db_pool = None

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
            referred_by        BIGINT
        )
    ''')
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
    ]:
        try:
            await db_pool.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {definition}")
        except Exception:
            pass
    for col, definition in [
        ("max_uses",   "INTEGER DEFAULT 1"),
        ("uses_count", "INTEGER DEFAULT 0"),
    ]:
        try:
            await db_pool.execute(f"ALTER TABLE coupons ADD COLUMN IF NOT EXISTS {col} {definition}")
        except Exception:
            pass
    try:
        await db_pool.execute("ALTER TABLE coupons DROP COLUMN IF EXISTS used_by")
    except Exception:
        pass

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

# ─── КУПОНЫ ──────────────────────────────────────────────────────────────────
async def create_coupon(code: str, max_uses: int = 1) -> str:
    expires = utc_now() + timedelta(hours=48)
    try:
        await db_pool.execute(
            'INSERT INTO coupons (code, expires_at, max_uses) VALUES ($1, $2, $3)',
            code.upper(), expires, max_uses
        )
        return 'ok'
    except asyncpg.UniqueViolationError:
        return 'exists'
    except Exception as e:
        logging.error(f"create_coupon error: {e}", exc_info=True)
        return 'error'

async def use_coupon(code: str, user_id: int) -> str:
    row = await db_pool.fetchrow('SELECT * FROM coupons WHERE code = $1', code.upper())
    if not row:
        return 'not_found'
    if row['expires_at'] and row['expires_at'] < utc_now():
        return 'expired'
    if row['uses_count'] >= row['max_uses']:
        return 'limit'
    updated = await db_pool.fetchval(
        '''UPDATE coupons SET uses_count = uses_count + 1
           WHERE code = $1 AND uses_count < max_uses
           RETURNING uses_count''',
        code.upper()
    )
    if updated is None:
        return 'limit'
    await db_pool.execute(
        'INSERT INTO coupon_uses (code, user_id) VALUES ($1, $2)',
        code.upper(), user_id
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

async def premium_stats() -> dict:
    active = await db_pool.fetchval(
        'SELECT COUNT(*) FROM users WHERE premium_until IS NOT NULL AND premium_until > NOW()'
    )
    ever = await db_pool.fetchval(
        'SELECT COUNT(*) FROM users WHERE premium_until IS NOT NULL'
    )
    return {"active": active or 0, "ever": ever or 0}
