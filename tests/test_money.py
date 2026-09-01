#!/usr/bin/env python3
"""Проверки всего, что связано с деньгами и лимитами, на НАСТОЯЩЕМ PostgreSQL.

    DATABASE_URL="postgresql://..." python3 tests/test_money.py

Как поднять локальную базу — см. tests/README.md. На боевую базу НЕ НАПРАВЛЯТЬ:
скрипт пишет и удаляет строки. Защита от этого — проверка на явное разрешение
ниже, но полагаться лучше на собственную внимательность.

Почему тут гонки, а не просто «вызвали функцию и проверили результат»: все
находки в этой части кода были именно про одновременность — двойной клик,
повторный вебхук, две вкладки. Последовательный вызов их не ловит.
"""
import asyncio
import os
import sys
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db  # noqa: E402

FAILURES: list[str] = []
PASSED = 0


def check(name: str, got, expected) -> None:
    global PASSED
    if got == expected:
        PASSED += 1
    else:
        FAILURES.append(f"{name}: получили {got!r}, ждали {expected!r}")


async def fresh_user(uid: int, balance: int = 0) -> None:
    await db.db_pool.execute("DELETE FROM users WHERE user_id = $1", uid)
    await db.get_user(uid)
    await db.db_pool.execute(
        "UPDATE users SET ref_balance = $2, last_spin_date = NULL WHERE user_id = $1",
        uid, balance,
    )


async def balance_of(uid: int) -> int:
    return await db.db_pool.fetchval("SELECT ref_balance FROM users WHERE user_id = $1", uid)


# ── КУПОНЫ ───────────────────────────────────────────────────────────────────
async def test_coupons() -> None:
    async def make(code, max_uses, multi, expired=False):
        await db.db_pool.execute("DELETE FROM coupons WHERE code = $1", code)
        await db.db_pool.execute("DELETE FROM coupon_uses WHERE code = $1", code)
        exp = db.utc_now() + (timedelta(hours=-1) if expired else timedelta(hours=48))
        await db.db_pool.execute(
            "INSERT INTO coupons (code, expires_at, max_uses, multi_per_user) VALUES ($1,$2,$3,$4)",
            code, exp, max_uses, multi,
        )

    await make("TA", 10, False)
    check("публичный код: повтор тем же человеком",
          [await db.use_coupon("TA", 1), await db.use_coupon("TA", 1)], ["ok", "used"])

    await make("TB", 2, False)
    check("общий лимит 2 делится между тремя людьми",
          [await db.use_coupon("TB", u) for u in (1, 2, 3)], ["ok", "ok", "limit"])

    await make("TC", 10, False)
    r = await asyncio.gather(*[db.use_coupon("TC", 7) for _ in range(10)])
    check("гонка: один человек, 10 одновременных активаций", r.count("ok"), 1)

    await make("TD", 3, False)
    r = await asyncio.gather(*[db.use_coupon("TD", 100 + i) for i in range(20)])
    check("гонка: 20 человек при лимите 3", r.count("ok"), 3)

    await make("TE", 3, True)
    check("личный код: тот же человек до конца лимита",
          [await db.use_coupon("TE", 5) for _ in range(4)], ["ok", "ok", "ok", "limit"])

    await make("TF", 10, False, expired=True)
    check("просроченный код", await db.use_coupon("TF", 1), "expired")
    check("несуществующий код", await db.use_coupon("QQQQ", 1), "not_found")

    await make("TG", 5, False)
    check("код в нижнем регистре", await db.use_coupon("tg", 1), "ok")

    # Исчерпанный лимит не должен «прилипать» к человеку: он ничего не получил,
    # значит и активация за ним числиться не должна.
    await make("TH", 1, False)
    await db.use_coupon("TH", 1)
    check("лимит исчерпан", await db.use_coupon("TH", 2), "limit")
    check("активация не прилипла к человеку",
          await db.db_pool.fetchval(
              "SELECT count(*) FROM coupon_uses WHERE code='TH' AND user_id=2"), 0)
    await db.db_pool.execute("UPDATE coupons SET max_uses = 5 WHERE code = 'TH'")
    check("после расширения лимита тот же человек проходит",
          await db.use_coupon("TH", 2), "ok")


# ── БАЛАНС, БОНУС, РЕФЕРАЛЫ ──────────────────────────────────────────────────
async def test_balance() -> None:
    await fresh_user(201, 100)
    r = await asyncio.gather(*[db.spend_balance(201, 60) for _ in range(5)])
    check("гонка списаний: проходит одно", r.count(True), 1)
    check("баланс не ушёл в минус", await balance_of(201), 40)

    await fresh_user(202, 50)
    check("списание больше баланса", await db.spend_balance(202, 51), False)
    check("баланс при отказе не тронут", await balance_of(202), 50)
    check("списание ровно в баланс", await db.spend_balance(202, 50), True)


async def test_daily_spin() -> None:
    await fresh_user(203, 0)
    r = await asyncio.gather(*[db.daily_spin_try(203, 3) for _ in range(10)])
    check("гонка рулетки: начисление одно", r.count(True), 1)
    check("баланс после рулетки", await balance_of(203), 3)
    check("вторая попытка в тот же день", await db.daily_spin_try(203, 3), False)
    await db.db_pool.execute(
        "UPDATE users SET last_spin_date = last_spin_date - 1 WHERE user_id = 203")
    check("на следующий день снова можно", await db.daily_spin_try(203, 3), True)


async def test_referrals() -> None:
    await fresh_user(204, 0)
    await fresh_user(205, 0)
    r = await asyncio.gather(*[db.register_referral(204, 205, 20) for _ in range(8)])
    check("гонка приглашений: бонус один раз", [x for x in r if x], [20])
    check("баланс приглашённого", await balance_of(205), 20)
    check("повторное приглашение", await db.register_referral(204, 205, 20), 0)
    check("связь в таблице одна", await db.db_pool.fetchval(
        "SELECT count(*) FROM referrals WHERE referrer_id=204 AND referred_id=205"), 1)

    await fresh_user(206, 0)
    await db.register_referral(204, 206, 20)
    check("перепривязка к другому пригласившему", await db.register_referral(999, 206, 20), 0)
    check("пригласивший не сменился", await db.db_pool.fetchval(
        "SELECT referred_by FROM users WHERE user_id = 206"), 204)


# ── ОТЗЫВЫ, ПОДАРКИ, ВЕБХУК ──────────────────────────────────────────────────
async def test_reviews() -> None:
    await db.db_pool.execute("TRUNCATE pending_reviews")
    rid = await db.add_pending_review(301, "текст отзыва", "")
    r = await asyncio.gather(*[db.delete_pending_review(rid) for _ in range(6)])
    check("двойное «Одобрить»: забирает один", r.count(True), 1)
    new_id = await db.restore_pending_review(301, "текст отзыва", "")
    check("отзыв вернулся в очередь при сбое публикации",
          (await db.get_pending_review(new_id))["review_text"], "текст отзыва")
    check("в очереди ровно один",
          await db.db_pool.fetchval("SELECT count(*) FROM pending_reviews"), 1)


async def test_gifts() -> None:
    await db.db_pool.execute("TRUNCATE gifts")
    await db.create_gift("TEST", "matrix_full", 401)
    r = await asyncio.gather(*[db.redeem_gift("TEST", 402) for _ in range(6)])
    check("подарок забирается один раз", len([x for x in r if x]), 1)
    check("повторный забор", await db.redeem_gift("TEST", 403), None)
    check("несуществующий код подарка", await db.redeem_gift("ZZZZ", 404), None)


async def test_yookassa_idempotency() -> None:
    await db.db_pool.execute("TRUNCATE yookassa_payments")
    r = await asyncio.gather(
        *[db.mark_yookassa_payment("pay_1", 501, "matrix_full", 500) for _ in range(6)])
    check("повтор вебхука обрабатывается один раз", r.count(True), 1)
    check("другой платёж проходит",
          await db.mark_yookassa_payment("pay_2", 501, "matrix_full", 500), True)


async def test_premium_reminders() -> None:
    now = db.utc_now()

    async def setup(uid, hours, currency):
        await fresh_user(uid)
        await db.db_pool.execute(
            "UPDATE users SET premium_until = $2 WHERE user_id = $1",
            uid, now + timedelta(hours=hours))
        if currency:
            await db.log_payment(uid, None, 100, currency)

    # premium_expiring_rub смотрит по всей таблице, а не по нашим id, поэтому
    # чужой пользователь с подходящей подпиской попал бы в выборку и завалил
    # проверку. В одноразовой тестовой базе просто снимаем премиум со всех.
    await db.db_pool.execute("UPDATE users SET premium_until = NULL")
    await db.db_pool.execute("DELETE FROM payments WHERE user_id BETWEEN 701 AND 706")
    await setup(701, 24, "RUB")   # попадает
    await setup(702, 24, "XTR")   # звёзды продлеваются сами
    await setup(703, 6, "RUB")    # раньше окна
    await setup(704, 48, "RUB")   # позже окна
    await setup(705, 24, None)    # платежей нет
    await setup(706, 24, "RUB")
    await db.log_payment(706, None, 100, "XTR")   # последняя оплата — звёздами
    got = sorted(r["user_id"] for r in await db.premium_expiring_rub())
    check("напоминание о продлении только рублёвым", got, [701])


# ── ЛИМИТЫ ───────────────────────────────────────────────────────────────────
async def test_limits() -> None:
    await fresh_user(601)
    r = await asyncio.gather(*[db.ask_try_consume(601, 3) for _ in range(15)])
    check("гонка: 15 вопросов Еве при лимите 3", r.count(True), 3)
    await db.db_pool.execute("UPDATE users SET ask_day = ask_day - 1 WHERE user_id = 601")
    check("новый день обнуляет лимит вопросов", await db.ask_try_consume(601, 3), True)

    await fresh_user(602)
    r = await asyncio.gather(*[db.yesno_try_consume(602, 2) for _ in range(10)])
    check("гонка: да/нет при лимите 2", r.count(True), 2)
    await db.refund_yesno_try(602)
    check("возврат вопроса после сбоя ИИ", await db.yesno_try_consume(602, 2), True)

    await fresh_user(603)
    r = await asyncio.gather(*[db.regen_try_consume(603, 3) for _ in range(12)])
    check("гонка: повторные генерации при лимите 3", r.count(True), 3)
    await db.refund_regen_try(603)
    check("возврат повторной генерации", await db.regen_try_consume(603, 3), True)

    await fresh_user(604)
    r = await asyncio.gather(*[db.premium_try_consume(604, 2, 10) for _ in range(10)])
    check("премиум: дневной лимит 2", r.count("ok"), 2)
    check("сообщение про дневной лимит", await db.premium_try_consume(604, 2, 10), "day")

    await fresh_user(605)
    check("премиум: месячный лимит 3",
          [await db.premium_try_consume(605, 100, 3) for _ in range(4)],
          ["ok", "ok", "ok", "month"])


async def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("Нужен DATABASE_URL. Как поднять локальную базу — tests/README.md")
        return 2
    if "railway" in url or "amvera" in url:
        print("Похоже на боевую базу — скрипт пишет и удаляет строки. Отказываюсь.")
        return 2

    await db.init_db(url)
    for t in (test_coupons, test_balance, test_daily_spin, test_referrals,
              test_reviews, test_gifts, test_yookassa_idempotency,
              test_premium_reminders, test_limits):
        await t()

    total = PASSED + len(FAILURES)
    print(f"пройдено {PASSED} из {total}")
    for f in FAILURES:
        print("  ✗", f)
    print("ЧИСТО" if not FAILURES else "ЕСТЬ ПРОБЛЕМЫ")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
