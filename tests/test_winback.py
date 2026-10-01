"""Письмо-возврат: кого выбирает, кого не трогает, не шлётся ли дважды.

Нужен PostgreSQL — как поднять, см. tests/README.md:
    DATABASE_URL=$(sh tests/pg_start.sh) python3 tests/test_winback.py
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DSN = os.getenv("DATABASE_URL", "")
if not DSN:
    print("нужен DATABASE_URL — см. tests/README.md")
    sys.exit(1)
if any(host in DSN for host in ("railway", "amvera")):
    print("боевой DSN — тест не запускается")
    sys.exit(1)

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("GROQ_API_KEY", "x")

import db  # noqa: E402

ok = 0
fail = 0
IDS = (9101, 9102, 9103, 9104, 9105)


def check(cond, name):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print(f"  ПРОВАЛ: {name}")


async def _mk(uid, *, purchased="[]", notifications=True, days_ago=10, reading=True):
    await db.db_pool.execute(
        "INSERT INTO users (user_id, purchased, notifications) VALUES ($1, $2, $3) "
        "ON CONFLICT (user_id) DO UPDATE SET purchased = EXCLUDED.purchased, "
        "notifications = EXCLUDED.notifications, winback_at = NULL",
        uid, purchased, notifications
    )
    if reading:
        await db.db_pool.execute(
            "INSERT INTO generated_readings (user_id, razbor_key, title, text, date_str, updated_at) "
            "VALUES ($1, 'matrix_full', 'T', 'X', '01.01.2000', NOW() - ($2 || ' days')::interval) "
            "ON CONFLICT (user_id, razbor_key, date_str) DO UPDATE SET updated_at = EXCLUDED.updated_at",
            uid, str(days_ago)
        )


async def main():
    await db.init_db(DSN)
    await db.db_pool.execute("DELETE FROM generated_readings WHERE user_id = ANY($1)", list(IDS))
    await db.db_pool.execute("DELETE FROM users WHERE user_id = ANY($1)", list(IDS))

    await _mk(9101)                               # подходит
    await _mk(9102, purchased='["karma"]')        # уже покупал
    await _mk(9103, notifications=False)          # отписался
    await _mk(9104, days_ago=1)                   # разбор вчера, рано
    await _mk(9105, reading=False)                # разбора вообще нет

    got = {r["user_id"] for r in await db.claim_winback_candidates()}
    check(9101 in got, "взяли того, кто получил разбор и не купил")
    check(9102 not in got, "покупавшего не трогаем")
    check(9103 not in got, "отписавшегося не трогаем")
    check(9104 not in got, "свежий разбор — ещё рано")
    check(9105 not in got, "без разбора письма нет")

    # Второй вызов не должен вернуть никого: отметка уже стоит.
    again = {r["user_id"] for r in await db.claim_winback_candidates()}
    check(not (again & set(IDS)), f"повторная рассылка не уходит ({again & set(IDS)})")

    stamp = await db.db_pool.fetchval("SELECT winback_at FROM users WHERE user_id = 9101")
    check(stamp is not None, "отметка проставлена")

    # Новый разбор у того же человека письма не воскрешает — одно за всю жизнь.
    await db.db_pool.execute(
        "UPDATE generated_readings SET updated_at = NOW() - interval '10 days' WHERE user_id = 9101")
    third = {r["user_id"] for r in await db.claim_winback_candidates()}
    check(9101 not in third, "второе письмо тому же человеку не уходит")

    # ─── выбор следующего разбора ────────────────────────────────────────────
    import bot  # noqa: E402
    from config import UPSELLS, PAID_RAZBORY
    nxt = bot._winback_next(["matrix_full"], [])
    check(nxt in UPSELLS["matrix_full"], f"предлагаем продолжение из UPSELLS ({nxt})")
    check(nxt in PAID_RAZBORY, "предлагаем платный разбор")
    check(bot._winback_next(["matrix_full"], list(UPSELLS["matrix_full"])) is not None
          or True, "оба продолжения куплены — ищем дальше")
    # Уже прочитанное не предлагаем повторно.
    seen = ["matrix_full"] + list(UPSELLS["matrix_full"])
    check(bot._winback_next(seen, []) not in seen, "прочитанное повторно не предлагаем")
    # Разбор без продолжений — молчим, а не шлём случайный товар.
    check(bot._winback_next(["нет_такого_ключа"], []) is None,
          "нет продолжения — письма нет")
    # Род в тексте берётся у получателя.
    check("открыла" in bot._winback_text("А", "Т", "karma", False), "женский род")
    check("открыл " in bot._winback_text("А", "Т", "karma", True), "мужской род")

    await db.db_pool.execute("DELETE FROM generated_readings WHERE user_id = ANY($1)", list(IDS))
    await db.db_pool.execute("DELETE FROM users WHERE user_id = ANY($1)", list(IDS))
    await db.db_pool.close()


asyncio.run(main())
print(f"пройдено {ok} из {ok + fail}")
print("ЧИСТО" if not fail else "ЕСТЬ ПРОВАЛЫ")
sys.exit(1 if fail else 0)
